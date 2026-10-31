from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from app.config import Settings
from app.github_api import GitHubAPI
from app.matching import classify_match, score_candidate, split_title_year
from app.searxng import SearXNGClient
from app.tmdb import TMDBClient


SCHEMA_VERSION = 1


def safe_segment(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return (value or "untitled")[:140]


class MetadataEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
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

    def _find_movie(self, title: str, year: int | None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        results = self.tmdb.search_movies(title, year)
        scored = []
        for candidate in results:
            score = score_candidate(title, year, candidate)
            scored.append((score["confidence"], candidate, score))
        scored.sort(key=lambda row: row[0], reverse=True)

        if not scored:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "TMDB returned no search candidates",
            }

        top_score, top_candidate, top_detail = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if top_score - second_score < 0.025 and len(scored) > 1:
            return None, {
                "status": "review",
                **top_detail,
                "reason": "Ambiguous TMDB candidates have nearly equal scores",
                "candidate_ids": [scored[0][1].get("id"), scored[1][1].get("id")],
            }

        status = classify_match(top_detail, self.settings.match_accept_score)
        if status != "ready":
            return None, {
                "status": "review",
                **top_detail,
                "reason": "Title/year confidence is below the automatic-accept threshold",
                "candidate_id": top_candidate.get("id"),
                "candidate_title": top_candidate.get("title"),
            }
        return top_candidate, {"status": "ready", **top_detail, "reason": "High-confidence title/year match"}

    def _record_for_folder(self, item: dict[str, str]) -> dict[str, Any]:
        folder = item["folder"]
        image_path = item["image_path"]
        title_hint, year_hint = split_title_year(folder)
        image_url = self.github.raw_url(image_path)

        tineye_results = self._tineye_results(image_url)
        candidate, match = self._find_movie(title_hint, year_hint)

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
            },
        }

        if candidate:
            details = self.tmdb.movie_details(int(candidate["id"]))
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
