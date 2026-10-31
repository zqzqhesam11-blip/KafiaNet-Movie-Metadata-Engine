from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from app.config import Settings
from app.github_api import GitHubAPI
from app.matching import classify_match, normalize_title, score_candidate, split_title_year, title_similarity
from app.tineye_evidence import extract_tineye_evidence
from app.searxng import SearXNGClient
from app.tmdb import TMDBClient


SCHEMA_VERSION = 1


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
                (score_candidate(candidate)["confidence"], candidate_id)
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
        if apply:
            self.settings.validate_for_processing(apply=True)
        elif not self.settings.tmdb_bearer_token:
            raise RuntimeError("TMDB_BEARER_TOKEN is required even for DRY RUN")

        folders = self.github.list_film_folders()
        index = self.github.get_json_file("metadata/index.json") or {"schema_version": 1, "items": []}
        old_items = list(index.get("items") or [])
        old_by_folder = {str(row.get("source_folder")): row for row in old_items}

        pending = [
            item for item in folders
            if item["folder"] not in old_by_folder
            or old_by_folder[item["folder"]].get("status") == "review"
        ][: max(1, limit)]

        records = [self._record_for_folder(item) for item in pending]
        projected = list(old_items)

        previous_by_folder = {
            str(row.get("source_folder")): row
            for row in projected
        }
        stale_output_paths: list[str] = []

        for record in records:
            previous = previous_by_folder.get(record["source"]["folder"])
            if previous and previous.get("output_path"):
                stale_output_paths.append(str(previous["output_path"]))
            projected = [
                row for row in projected
                if row.get("source_folder") != record["source"]["folder"]
            ]
            projected.append(
                {
                    "source_folder": record["source"]["folder"],
                    "image_path": record["source"]["image_path"],
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
        deletes: list[str] = []
        ready_count = review_count = duplicate_count = 0

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
                out_path = f"مكرر/{tmdb_id}/{safe_segment(folder)}/movie.json"
                duplicate_count += 1
            elif record["status"] == "ready":
                out_path = f"جاهز/{safe_segment(folder)}/movie.json"
                ready_count += 1
            else:
                out_path = f"مراجعة/{safe_segment(folder)}/movie.json"
                review_count += 1

            record["output_path"] = out_path
            files[out_path] = json.dumps(record, ensure_ascii=False, indent=2) + "\n"

        # Remove a previous output only when this batch changes its destination.
        for record in records:
            previous = previous_by_folder.get(record["source"]["folder"])
            previous_path = previous.get("output_path") if previous else None
            if previous_path and previous_path != record["output_path"]:
                deletes.append(str(previous_path))

        # When a newly found ID makes an earlier READY entry a duplicate, copy its
        # existing metadata JSON into the duplicate group and delete its old READY path.
        if apply:
            for tmdb_id, group in by_id.items():
                if len(group) < 2:
                    continue
                for row in group:
                    previous_path = row.get("output_path")
                    if previous_path and previous_path.startswith("جاهز/"):
                        old_record = self.github.get_json_file(previous_path)
                        if not old_record:
                            raise RuntimeError(
                                f"Cannot safely move duplicate metadata; existing file is missing: {previous_path}"
                            )
                        source_folder = str(row["source_folder"])
                        new_path = f"مكرر/{tmdb_id}/{safe_segment(source_folder)}/movie.json"
                        old_record["status"] = "duplicate"
                        old_record["duplicate_group"] = {
                            "tmdb_id": int(tmdb_id),
                            "source_folders": sorted(x["source_folder"] for x in group),
                        }
                        old_record["output_path"] = new_path
                        files[new_path] = json.dumps(old_record, ensure_ascii=False, indent=2) + "\n"
                        deletes.append(previous_path)
                        row["status"] = "duplicate"
                        row["output_path"] = new_path

        # Set output paths/status for newly processed rows after duplicate grouping.
        for record in records:
            row = next(
                row for row in projected
                if row["source_folder"] == record["source"]["folder"]
            )
            row["status"] = record["status"]
            row["output_path"] = record["output_path"]

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
            "records": records,
            "next_pending_estimate": sum(
                1 for item in folders
                if item["folder"] not in {
                    str(row.get("source_folder"))
                    for row in projected
                    if row.get("status") in {"ready", "duplicate"}
                }
            ),
        }

        if apply and files:
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
                message=f"KafiaNet metadata batch: {len(records)} films",
            )
            summary["commit_sha"] = commit_sha
            summary["written_files"] = len(files)
            summary["deleted_old_ready_files"] = len(deletes)

        return summary
