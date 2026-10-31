from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.github_api import GitHubAPI
from app.matching import classify_match, normalize_title, score_candidate, split_title_year, title_similarity
from app.tineye_evidence import extract_tineye_evidence
from app.searxng import SearXNGClient
from app.tmdb import TMDBClient


SCHEMA_VERSION = 2


def safe_segment(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return (value or "untitled")[:140]


class MetadataEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._details_cache: dict[int, dict[str, Any]] = {}
        self.github = GitHubAPI(
            settings.posters_repo,
            settings.posters_branch,
            token=settings.github_token,
            timeout=settings.http_timeout_seconds,
        )
        self.tmdb = TMDBClient(
            settings.tmdb_bearer_token,
            settings.tmdb_language,
            settings.http_timeout_seconds,
        )
        self.searxng = (
            SearXNGClient(
                settings.searxng_url,
                settings.searxng_token,
                timeout=max(45, settings.http_timeout_seconds),
            )
            if settings.searxng_url
            else None
        )

    def close(self) -> None:
        self.github.close()
        self.tmdb.close()
        if self.searxng:
            self.searxng.close()

    def _tineye_results(self, image_url: str) -> list[dict[str, Any]]:
        if not self.searxng:
            return []
        try:
            return self.searxng.tineye_by_image_url(image_url)
        except Exception as exc:
            # A reverse-image provider failure must never invent or block TMDB identity.
            return [{"error": type(exc).__name__, "message": str(exc)[:300]}]

    def _find_movie(
        self,
        title: str,
        year: int | None,
        tineye_results: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        evidence = extract_tineye_evidence(tineye_results)
        tineye_failed = any(
            isinstance(item, dict) and item.get("error")
            for item in tineye_results
        )
        candidates: dict[str, dict[str, Any]] = {}
        sources: dict[str, set[str]] = {}
        folder_candidates: dict[str, dict[str, Any]] = {}
        resolution_errors: list[dict[str, str]] = []

        def add_candidate(candidate: dict[str, Any], source: str) -> None:
            candidate_id = candidate.get("id")
            if candidate_id is None:
                return
            key = str(candidate_id)
            candidates.setdefault(key, candidate)
            sources.setdefault(key, set()).add(source)
            if source == "folder_search":
                folder_candidates[key] = candidate

        # The original folder name remains the primary text-search signal.
        for candidate in self.tmdb.search_movies(title, year):
            add_candidate(candidate, "folder_search")

        # TinEye image filenames can carry a reliable film title even when the
        # local folder name is abbreviated or misspelled. Keep fan-out bounded.
        seen_hints = {normalize_title(title)}
        for hint in evidence["title_hints"][:3]:
            normalized_hint = normalize_title(hint)
            if not normalized_hint or normalized_hint in seen_hints:
                continue
            seen_hints.add(normalized_hint)
            for candidate in self.tmdb.search_movies(hint, year):
                add_candidate(candidate, "tineye_title")

        # Trusted IMDb / TMDB URLs found in TinEye backlinks are stronger than
        # a guessed filename. IDs are accepted only from the official domains.
        for imdb_id in evidence["imdb_ids"][:3]:
            try:
                for candidate in self.tmdb.find_by_imdb_id(imdb_id):
                    add_candidate(candidate, "tineye_imdb")
            except Exception as exc:
                resolution_errors.append(
                    {"type": "imdb_lookup", "id": imdb_id, "error": type(exc).__name__}
                )

        for tmdb_id in evidence["tmdb_ids"][:3]:
            try:
                details = self.tmdb.movie_details(int(tmdb_id))
                self._details_cache[int(tmdb_id)] = details
                add_candidate(details, "tineye_tmdb")
            except Exception as exc:
                resolution_errors.append(
                    {"type": "tmdb_lookup", "id": str(tmdb_id), "error": type(exc).__name__}
                )

        evidence["resolved_sources"] = {
            key: sorted(value) for key, value in sources.items()
        }
        evidence["resolution_errors"] = resolution_errors

        if not candidates:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "TMDB returned no candidates from folder or TinEye evidence",
            }, evidence

        def candidate_score(candidate: dict[str, Any]) -> dict[str, Any]:
            folder_detail = score_candidate(title, year, candidate)
            folder_similarity = float(folder_detail["title_similarity"])
            hint_scores = [
                title_similarity(hint, str(candidate.get("title") or ""))
                for hint in evidence["title_hints"][:3]
            ] + [
                title_similarity(hint, str(candidate.get("original_title") or ""))
                for hint in evidence["title_hints"][:3]
            ]
            tineye_similarity = max(hint_scores, default=0.0)
            best_similarity = max(folder_similarity, tineye_similarity)

            tmdb_year = folder_detail["tmdb_year"]
            year_delta = folder_detail["year_delta"]
            confidence = best_similarity
            if year is not None and tmdb_year is not None:
                confidence = best_similarity * 0.90 + (0.10 if year_delta == 0 else 0.0)
                if year_delta is not None and year_delta > 1:
                    confidence *= 0.82

            return {
                "confidence": round(confidence, 4),
                "title_similarity": round(best_similarity, 4),
                "folder_title_similarity": round(folder_similarity, 4),
                "tineye_title_similarity": round(tineye_similarity, 4),
                "best_tineye_hint": (
                    max(
                        evidence["title_hints"][:3],
                        key=lambda hint: max(
                            title_similarity(hint, str(candidate.get("title") or "")),
                            title_similarity(hint, str(candidate.get("original_title") or "")),
                        ),
                    )
                    if evidence["title_hints"][:3] and tineye_similarity > 0
                    else None
                ),
                "source_year": year,
                "tmdb_year": tmdb_year,
                "year_delta": year_delta,
                "evidence_sources": sorted(sources.get(str(candidate.get("id")), set())),
            }

        scored = [
            (candidate_score(candidate)["confidence"], str(candidate_id), candidate, candidate_score(candidate))
            for candidate_id, candidate in candidates.items()
        ]
        scored.sort(key=lambda row: row[0], reverse=True)

        if not tineye_results:
            return None, {
                "status": "review",
                **scored[0][3],
                "reason": "TinEye returned no reverse-image matches; title-only candidate requires review",
                "candidate_id": scored[0][2].get("id"),
                "candidate_title": scored[0][2].get("title"),
            }, evidence

        if tineye_failed or resolution_errors:
            return None, {
                "status": "review",
                **scored[0][3],
                "reason": (
                    "TinEye reverse-image request failed; folder-only candidate must be reviewed"
                    if tineye_failed
                    else "TMDB could not resolve a TinEye external movie ID; candidate must be reviewed"
                ),
                "candidate_id": scored[0][2].get("id"),
                "candidate_title": scored[0][2].get("title"),
            }, evidence

        direct_ids = {
            candidate_id
            for candidate_id, candidate_sources in sources.items()
            if {"tineye_imdb", "tineye_tmdb"} & candidate_sources
        }

        # Multiple distinct direct IDs from image backlinks are conflicting
        # evidence. Do not select one automatically.
        if len(direct_ids) > 1:
            return None, {
                "status": "review",
                "confidence": scored[0][0],
                "reason": "TinEye backlinks resolved to multiple different TMDB movie IDs",
                "candidate_ids": sorted(direct_ids, key=int),
            }, evidence

        if len(direct_ids) == 1:
            direct_id = next(iter(direct_ids))
            direct_candidate = candidates[direct_id]
            direct_detail = candidate_score(direct_candidate)

            # If the folder name independently identifies another high-confidence
            # film, the disagreement requires human review.
            folder_scored = [
                (score_candidate(title, year, candidate)["confidence"], candidate_id)
                for candidate_id, candidate in folder_candidates.items()
            ]
            folder_scored.sort(reverse=True)
            if (
                folder_scored
                and folder_scored[0][1] != direct_id
                and folder_scored[0][0] >= self.settings.match_accept_score
            ):
                return None, {
                    "status": "review",
                    **direct_detail,
                    "reason": "TinEye external ID conflicts with a high-confidence folder-title match",
                    "candidate_ids": [direct_id, folder_scored[0][1]],
                }, evidence

            if direct_detail["year_delta"] is not None and direct_detail["year_delta"] > 1:
                return None, {
                    "status": "review",
                    **direct_detail,
                    "reason": "TinEye external ID conflicts with the source folder year",
                    "candidate_id": direct_id,
                    "candidate_title": direct_candidate.get("title"),
                }, evidence

            return direct_candidate, {
                "status": "ready",
                **direct_detail,
                "confidence": 1.0,
                "reason": "TinEye backlink resolved to a single trusted IMDb/TMDB movie ID",
            }, evidence

        top_score, top_id, top_candidate, top_detail = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        # If folder-name matching and TinEye-title matching strongly disagree,
        # preserve both candidates for review rather than blending the signals.
        folder_ranked = sorted(
            (
                (score_candidate(title, year, candidate)["confidence"], candidate_id)
                for candidate_id, candidate in folder_candidates.items()
            ),
            reverse=True,
        )
        if (
            folder_ranked
            and folder_ranked[0][1] != top_id
            and folder_ranked[0][0] >= self.settings.match_accept_score
            and top_score >= self.settings.match_accept_score
        ):
            return None, {
                "status": "review",
                **top_detail,
                "reason": "TinEye title evidence conflicts with a high-confidence folder-title match",
                "candidate_ids": [top_id, folder_ranked[0][1]],
            }, evidence

        if len(scored) > 1 and top_score - second_score < 0.025:
            return None, {
                "status": "review",
                **top_detail,
                "reason": "Ambiguous TMDB candidates have nearly equal combined scores",
                "candidate_ids": [scored[0][2].get("id"), scored[1][2].get("id")],
            }, evidence

        status = classify_match(top_detail, self.settings.match_accept_score)
        if status != "ready":
            return None, {
                "status": "review",
                **top_detail,
                "reason": "Combined folder/TinEye title and year confidence is below the automatic-accept threshold",
                "candidate_id": top_candidate.get("id"),
                "candidate_title": top_candidate.get("title"),
            }, evidence

        return top_candidate, {
            "status": "ready",
            **top_detail,
            "reason": "High-confidence folder title or TinEye image-title match",
        }, evidence

    def _record_for_folder(self, item: dict[str, str]) -> dict[str, Any]:
        folder = item["folder"]
        image_path = item["image_path"]
        title_hint, year_hint = split_title_year(folder)
        image_url = self.github.raw_url(image_path)

        tineye_results = self._tineye_results(image_url)
        candidate, match, tineye_evidence = self._find_movie(title_hint, year_hint, tineye_results)

        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "processed_utc": datetime.now(timezone.utc).isoformat(),
            "source": {
                "repository": self.settings.posters_repo,
                "branch": self.settings.posters_branch,
                "folder": folder,
                "image_path": image_path,
                "image_url": image_url,
            },
            "original": {
                "folder_name": folder,
                "title_hint": title_hint,
                "year_hint": year_hint,
            },
            "match": match,
            "tineye": {
                "enabled": self.searxng is not None,
                "results": tineye_results,
                "evidence": tineye_evidence,
            },
        }

        if candidate:
            tmdb_id = int(candidate["id"])
            details = self._details_cache.get(tmdb_id)
            if details is None:
                details = self.tmdb.movie_details(tmdb_id)
                self._details_cache[tmdb_id] = details
            normalized = self.tmdb.normalize_details(details)
            canonical_title = normalized.get("title") or candidate.get("title") or title_hint
            canonical_year = normalized.get("year")
            record["tmdb"] = normalized
            record["correction"] = {
                "canonical_folder_name": (
                    f"{canonical_title} ({canonical_year})" if canonical_year else canonical_title
                ),
                "rename_required": safe_segment(folder).casefold()
                != safe_segment(
                    f"{canonical_title} ({canonical_year})" if canonical_year else canonical_title
                ).casefold(),
                "source_folder_unchanged": True,
            }
            record["status"] = "ready"
        else:
            record["tmdb"] = None
            record["correction"] = None
            record["status"] = "review"

        return record

    def run_batch(self, limit: int, apply: bool) -> dict[str, Any]:
        self.settings.validate_for_processing(apply=apply)

        print("[KafiaNet] Scanning poster repository...", flush=True)
        folders = self.github.list_film_folders()
        print(f"[KafiaNet] Found {len(folders)} source/review folders.", flush=True)
        index = self.github.get_json_file("metadata/index.json") or {"schema_version": 1, "items": []}
        old_items = list(index.get("items") or [])
        old_by_folder = {str(row.get("source_folder")): row for row in old_items}

        # Root folders still need their first move even when the older index says
        # READY. REVIEW folders are retried on later batches until resolved.
        pending = [
            item for item in folders
            if item["folder"] not in old_by_folder
            or old_by_folder[item["folder"]].get("status") == "review"
            or item.get("source_prefix") == item["folder"]
        ][: max(1, limit)]

        records: list[dict[str, Any]] = []
        item_by_folder = {item["folder"]: item for item in pending}
        total = len(pending)
        print(f"[KafiaNet] Selected {total} pending film(s). Mode={'APPLY' if apply else 'DRY_RUN'}.", flush=True)
        for position, item in enumerate(pending, start=1):
            print(
                f"[KafiaNet] [{position}/{total}] START {item['folder']}",
                flush=True,
            )
            record = self._record_for_folder(item)
            records.append(record)
            tmdb_id = (record.get("tmdb") or {}).get("tmdb_id")
            print(
                f"[KafiaNet] [{position}/{total}] DONE status={record['status']} "
                f"tmdb_id={tmdb_id if tmdb_id is not None else 'none'}",
                flush=True,
            )

        projected = list(old_items)
        previous_by_folder = {str(row.get("source_folder")): row for row in projected}

        for record in records:
            previous = previous_by_folder.get(record["source"]["folder"])
            original_image_path = (
                (previous or {}).get("original_image_path")
                or (previous or {}).get("image_path")
                or record["source"]["image_path"]
            )
            projected = [
                row for row in projected
                if row.get("source_folder") != record["source"]["folder"]
            ]
            projected.append(
                {
                    "source_folder": record["source"]["folder"],
                    "image_path": record["source"]["image_path"],
                    "original_image_path": original_image_path,
                    "tmdb_id": (record.get("tmdb") or {}).get("tmdb_id"),
                    "canonical_folder_name": (record.get("correction") or {}).get("canonical_folder_name"),
                    "status": record["status"],
                    "output_path": None,
                }
            )

        by_id: dict[str, list[dict[str, Any]]] = {}
        for row in projected:
            tmdb_id = row.get("tmdb_id")
            if tmdb_id:
                by_id.setdefault(str(tmdb_id), []).append(row)

        files: dict[str, str] = {}
        moves: dict[str, dict[str, str]] = {}
        deletes: list[str] = []
        image_deletes: list[str] = []
        ready_count = review_count = duplicate_count = 0

        def json_filename(record: dict[str, Any], folder: str) -> str:
            status = record.get("status")
            correction = record.get("correction") or {}
            tmdb = record.get("tmdb") or {}
            if status in {"ready", "duplicate"}:
                title = correction.get("canonical_folder_name")
                if not title and tmdb.get("title"):
                    year = tmdb.get("year")
                    title = f"{tmdb['title']} ({year})" if year else tmdb["title"]
            else:
                title = folder
            return safe_segment(str(title or folder)) + ".json"

        # When a new record reveals a duplicate, update both its metadata and
        # its image folder without downloading/re-uploading the image bytes.
        for record in records:
            folder = record["source"]["folder"]
            tmdb_id = (record.get("tmdb") or {}).get("tmdb_id")
            group = by_id.get(str(tmdb_id), []) if tmdb_id else []
            if tmdb_id and len(group) > 1:
                record["status"] = "duplicate"
                record["duplicate_group"] = {
                    "tmdb_id": int(tmdb_id),
                    "source_folders": sorted(row["source_folder"] for row in group),
                }
                target_prefix = f"مكرر/{tmdb_id}/{safe_segment(folder)}"
                duplicate_count += 1
            elif record["status"] == "ready":
                target_prefix = f"جاهز/{safe_segment(folder)}"
                ready_count += 1
            else:
                target_prefix = f"مراجعة/{safe_segment(folder)}"
                review_count += 1

            item = item_by_folder[folder]
            poster_files = list(item.get("poster_files") or [])
            if not poster_files:
                raise RuntimeError(
                    f"Cannot move poster folder without image blobs: {item.get('source_prefix')}/{folder}"
                )

            target_images: list[str] = []
            target_image_paths: list[str] = []
            source_prefix = str(item.get("source_prefix") or folder).rstrip("/")
            for poster in poster_files:
                source_path = str(poster["path"])
                relative_path = str(poster.get("relative_path") or source_path[len(source_prefix) + 1:])
                target_path = f"{target_prefix}/{relative_path}"
                target_images.append(target_path)
                suffix = "." + source_path.rsplit(".", 1)[-1].lower() if "." in source_path else ""
                if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
                    target_image_paths.append(target_path)
                if source_path != target_path:
                    if not poster.get("sha"):
                        raise RuntimeError(f"Missing Git blob SHA; refusing image move: {source_path}")
                    moves[target_path] = {
                        "sha": str(poster["sha"]),
                        "mode": str(poster.get("mode") or "100644"),
                    }
                    image_deletes.append(source_path)

            # Keep the source folder label intact for traceability. The JSON
            # filename itself uses the verified TMDB title when one exists.
            old_source = record["source"].get("image_path")
            previous = previous_by_folder.get(folder) or {}
            record["source"]["original_image_path"] = (
                previous.get("original_image_path")
                or previous.get("image_path")
                or old_source
            )
            if not target_image_paths:
                raise RuntimeError(f"No supported poster image found for folder: {folder}")
            record["source"]["image_path"] = target_image_paths[0]
            record["source"]["image_url"] = self.github.raw_url(target_image_paths[0])

            out_path = f"{target_prefix}/{json_filename(record, folder)}"
            record["output_path"] = out_path
            files[out_path] = json.dumps(record, ensure_ascii=False, indent=2) + "\n"

            row = next(row for row in projected if row["source_folder"] == folder)
            row["status"] = record["status"]
            row["image_path"] = target_image_paths[0]
            row["original_image_path"] = record["source"]["original_image_path"]
            row["output_path"] = out_path
            row["tmdb_id"] = tmdb_id
            row["canonical_folder_name"] = (record.get("correction") or {}).get("canonical_folder_name")

        # Remove old JSON paths when a record is moved or its JSON filename changes.
        for record in records:
            previous = previous_by_folder.get(record["source"]["folder"])
            previous_path = previous.get("output_path") if previous else None
            if previous_path and previous_path != record["output_path"]:
                deletes.append(str(previous_path))

        # If a previously READY film becomes a duplicate after this batch, move
        # its current image files and rename its JSON in the same atomic commit.
        if apply:
            for tmdb_id, group in by_id.items():
                if len(group) < 2:
                    continue
                for row in group:
                    previous_path = row.get("output_path")
                    if not previous_path or not previous_path.startswith("جاهز/"):
                        continue
                    old_record = self.github.get_json_file(previous_path)
                    if not old_record:
                        raise RuntimeError(
                            f"Cannot safely move duplicate metadata; existing file is missing: {previous_path}"
                        )
                    source_folder = str(row["source_folder"])
                    old_prefix = f"جاهز/{safe_segment(source_folder)}"
                    new_prefix = f"مكرر/{tmdb_id}/{safe_segment(source_folder)}"
                    moved_images = self.github.list_folder_blobs(old_prefix)
                    for poster in moved_images:
                        source_path = poster["path"]
                        relative_path = source_path[len(old_prefix) + 1:]
                        target_path = f"{new_prefix}/{relative_path}"
                        moves[target_path] = {"sha": poster["sha"], "mode": poster["mode"]}
                        image_deletes.append(source_path)
                        if row.get("image_path") == source_path:
                            row["image_path"] = target_path
                            old_record["source"]["image_path"] = target_path
                            old_record["source"]["image_url"] = self.github.raw_url(target_path)

                    old_record["status"] = "duplicate"
                    old_record["duplicate_group"] = {
                        "tmdb_id": int(tmdb_id),
                        "source_folders": sorted(x["source_folder"] for x in group),
                    }
                    old_record["output_path"] = (
                        f"{new_prefix}/{json_filename(old_record, source_folder)}"
                    )
                    files[old_record["output_path"]] = json.dumps(
                        old_record, ensure_ascii=False, indent=2
                    ) + "\n"
                    deletes.append(previous_path)
                    row["status"] = "duplicate"
                    row["output_path"] = old_record["output_path"]

        # Reconcile all output paths and deduplicate delete operations.
        for record in records:
            row = next(
                row for row in projected
                if row["source_folder"] == record["source"]["folder"]
            )
            row["status"] = record["status"]
            row["output_path"] = record["output_path"]

        deletes = sorted(set(deletes + image_deletes))
        image_deletes = sorted(set(image_deletes))

        projected_by_folder = {
            str(row.get("source_folder")): row for row in projected
        }
        next_pending_estimate = 0
        for item in folders:
            row = projected_by_folder.get(item["folder"])
            if (
                row is None
                or row.get("status") == "review"
                or not str(row.get("image_path") or "").startswith(("جاهز/", "مراجعة/", "مكرر/"))
            ):
                next_pending_estimate += 1

        summary = {
            "mode": "APPLY" if apply else "DRY_RUN",
            "source_repo": self.settings.posters_repo,
            "branch": self.settings.posters_branch,
            "source_folders_found": len(folders),
            "already_indexed_before_batch": len(old_items),
            "batch_requested": limit,
            "batch_selected": len(records),
            "ready": ready_count,
            "review": review_count,
            "duplicates_in_this_batch": duplicate_count,
            "planned_output_files": sorted(files),
            "planned_image_moves": sorted(moves),
            "planned_source_image_deletes": image_deletes,
            "records": records,
            "next_pending_estimate": next_pending_estimate,
        }

        if apply and (files or moves):
            print(
                f"[KafiaNet] Preparing GitHub commit with {len(files) + 1} JSON file(s) "
                f"and {len(moves)} image move(s)...",
                flush=True,
            )
            index_doc = {
                "schema_version": 1,
                "updated_utc": datetime.now(timezone.utc).isoformat(),
                "source_repository": self.settings.posters_repo,
                "items": sorted(projected, key=lambda row: str(row.get("source_folder", "")).casefold()),
            }
            files["metadata/index.json"] = json.dumps(index_doc, ensure_ascii=False, indent=2) + "\n"
            commit_sha = self.github.commit_files(
                files,
                deletes=deletes,
                moves=moves,
                message=f"KafiaNet metadata and poster organization: {len(records)} films",
            )
            summary["commit_sha"] = commit_sha
            summary["written_files"] = len(files)
            summary["moved_image_files"] = len(moves)
            summary["deleted_old_files"] = len(deletes)
            print(f"[KafiaNet] GitHub commit completed: {commit_sha}", flush=True)

        return summary
