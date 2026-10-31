from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.github_api import GitHubAPI
from app.matching import classify_match, normalize_title, score_candidate, split_title_year, title_search_variants, title_similarity
from app.tineye_evidence import extract_tineye_evidence
from app.searxng import SearXNGClient
from app.tmdb import TMDBClient, WORLDWIDE_TITLE_POLICY_VERSION


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

    def _tmdb_identity_profile(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Read production country/director fields when title/year collide."""
        tmdb_id = int(candidate["id"])
        details = self._details_cache.get(tmdb_id)
        error = None
        if details is None:
            try:
                details = self.tmdb.movie_details(tmdb_id)
                self._details_cache[tmdb_id] = details
            except Exception as exc:
                details = {}
                error = type(exc).__name__

        normalized = TMDBClient.normalize_details(details) if details else {}
        countries = sorted({
            str(item.get("name") or item.get("iso_3166_1") or "").strip()
            for item in (normalized.get("production_countries") or [])
            if isinstance(item, dict) and (item.get("name") or item.get("iso_3166_1"))
        })
        directors = sorted({
            str(item.get("name") or "").strip()
            for item in (normalized.get("directors") or [])
            if isinstance(item, dict) and item.get("name")
        })
        return {
            "tmdb_id": tmdb_id,
            "title": normalized.get("title") or candidate.get("title"),
            "original_title": normalized.get("original_title") or candidate.get("original_title"),
            "year": normalized.get("year"),
            "production_countries": countries,
            "directors": directors,
            "runtime": normalized.get("runtime"),
            "metadata_lookup_error": error,
        }

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

        # Keep the exact source folder text as the identity, but search its
        # parenthetical aliases separately (e.g. Breathless / À Bout de Souffle).
        # If the stored year is wrong, retry a title-only TMDB query only when
        # the year-scoped query did not return an exact-year title candidate.
        folder_title_variants = title_search_variants(title) or [title]
        evidence["folder_title_variants"] = folder_title_variants
        for folder_variant in folder_title_variants:
            folder_results = self.tmdb.search_movies(folder_variant, year)
            for candidate in folder_results:
                add_candidate(candidate, "folder_search")
            has_exact_year = any(
                score_candidate(folder_variant, year, candidate)["year_delta"] == 0
                for candidate in folder_results
            ) if year is not None else True
            if year is not None and not has_exact_year:
                for candidate in self.tmdb.search_movies(folder_variant, None):
                    add_candidate(candidate, "folder_search")

        # TinEye image filenames can carry a useful film title even when the
        # local folder name is abbreviated or misspelled. Keep fan-out bounded.
        seen_hints = {normalize_title(value) for value in folder_title_variants}
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
        evidence["rejected_external_ids"] = []

        if not candidates:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "TMDB returned no candidates from folder or TinEye evidence",
            }, evidence

        def best_folder_match(candidate: dict[str, Any]) -> tuple[dict[str, Any], str]:
            scored_variants = [
                (score_candidate(variant, year, candidate), variant)
                for variant in folder_title_variants
            ] or [(score_candidate(title, year, candidate), title)]
            return max(scored_variants, key=lambda row: row[0]["confidence"])

        # TinEye links can point to an unrelated page that happened to reuse
        # the poster. Validate direct IMDb/TMDB IDs against the source title
        # and release year before they can be authoritative. A wrong folder
        # year remains correctable when an unscoped TMDB title search
        # independently returns the same exact-title movie.
        rejected_external_ids: list[dict[str, Any]] = []
        direct_sources = {"tineye_imdb", "tineye_tmdb"}
        for candidate_id in list(candidates):
            candidate_sources = sources.get(candidate_id, set())
            if not (candidate_sources & direct_sources) or year is None:
                continue

            candidate = candidates[candidate_id]
            folder_detail, best_folder_variant = best_folder_match(candidate)
            folder_similarity = float(folder_detail["title_similarity"])
            hint_similarity = max(
                [
                    title_similarity(hint, str(candidate.get("title") or ""))
                    for hint in evidence["title_hints"][:3]
                ] + [
                    title_similarity(hint, str(candidate.get("original_title") or ""))
                    for hint in evidence["title_hints"][:3]
                ],
                default=0.0,
            )
            title_support = max(folder_similarity, hint_similarity)
            rejection_reason = None
            if folder_detail["tmdb_year"] is None:
                rejection_reason = "TMDB candidate has no release year while the source folder has one"
            elif title_support < 0.88:
                rejection_reason = "TinEye external ID title does not agree with the source folder or title hints"
            elif folder_detail["year_delta"] is not None and folder_detail["year_delta"] > 1 and (
                folder_similarity < 0.94 or "folder_search" not in candidate_sources
            ):
                rejection_reason = (
                    "TinEye external ID year differs by more than one year without an independent exact-title search match"
                )

            if rejection_reason:
                rejected_external_ids.append({
                    "candidate_id": candidate_id,
                    "candidate_title": candidate.get("title"),
                    "candidate_year": folder_detail["tmdb_year"],
                    "source_year": year,
                    "year_delta": folder_detail["year_delta"],
                    "best_folder_title_variant": best_folder_variant,
                    "title_support": round(title_support, 4),
                    "sources": sorted(candidate_sources & direct_sources),
                    "reason": rejection_reason,
                })
                # Preserve candidates also found by an independent title search;
                # remove candidates supported only by the rejected backlink.
                if not (candidate_sources - direct_sources):
                    candidates.pop(candidate_id, None)

        evidence["rejected_external_ids"] = rejected_external_ids

        if not candidates:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": (
                    "TinEye external IDs failed source-title/year validation; "
                    "no independent TMDB title candidate remained"
                ),
                "candidate_ids": [
                    item["candidate_id"] for item in rejected_external_ids
                ],
            }, evidence

        def candidate_score(candidate: dict[str, Any]) -> dict[str, Any]:
            folder_detail, best_folder_variant = best_folder_match(candidate)
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
                "best_folder_title_variant": best_folder_variant,
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

        rejected_ids = {
            str(item["candidate_id"]) for item in rejected_external_ids
        }
        direct_ids = {
            candidate_id
            for candidate_id, candidate_sources in sources.items()
            if candidate_id in candidates
            and candidate_id not in rejected_ids
            and ({"tineye_imdb", "tineye_tmdb"} & candidate_sources)
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

            # A folder search may return multiple films with the exact same
            # title and year. Treat that as a title collision, not a conflict
            # with a trusted IMDb/TMDB backlink. Compare TMDB identity metadata
            # and retain the competing records in the output JSON.
            folder_scored = [
                (best_folder_match(candidate)[0]["confidence"], candidate_id, candidate)
                for candidate_id, candidate in folder_candidates.items()
                if candidate_id != direct_id
            ]
            folder_scored.sort(key=lambda row: row[0], reverse=True)
            same_title_year_candidates: list[dict[str, Any]] = []
            unresolved_folder_conflicts: list[dict[str, Any]] = []
            direct_profile = None
            for folder_score, folder_id, folder_candidate in folder_scored:
                if folder_score < self.settings.match_accept_score:
                    continue
                folder_detail, _ = best_folder_match(folder_candidate)
                direct_names = [
                    str(direct_candidate.get("title") or ""),
                    str(direct_candidate.get("original_title") or ""),
                ]
                folder_names = [
                    str(folder_candidate.get("title") or ""),
                    str(folder_candidate.get("original_title") or ""),
                ]
                same_title = max(
                    (title_similarity(left, right) for left in direct_names for right in folder_names),
                    default=0.0,
                ) >= 0.96
                same_year = (
                    direct_detail["tmdb_year"] is not None
                    and direct_detail["tmdb_year"] == folder_detail["tmdb_year"]
                )
                if same_title and same_year:
                    if direct_profile is None:
                        direct_profile = self._tmdb_identity_profile(direct_candidate)
                    other_profile = self._tmdb_identity_profile(folder_candidate)
                    same_title_year_candidates.append({
                        "candidate_id": folder_id,
                        "title": folder_candidate.get("title"),
                        "year": folder_detail["tmdb_year"],
                        "production_countries": other_profile["production_countries"],
                        "directors": other_profile["directors"],
                        "runtime": other_profile["runtime"],
                        "identity_differs_from_selected": (
                            other_profile["production_countries"] != direct_profile["production_countries"]
                            or other_profile["directors"] != direct_profile["directors"]
                        ),
                    })
                else:
                    unresolved_folder_conflicts.append({
                        "candidate_id": folder_id,
                        "title": folder_candidate.get("title"),
                        "year": folder_detail["tmdb_year"],
                        "confidence": folder_score,
                    })

            if unresolved_folder_conflicts:
                return None, {
                    "status": "review",
                    **direct_detail,
                    "reason": "TinEye external ID conflicts with a different high-confidence folder-title candidate",
                    "candidate_ids": [
                        direct_id,
                        *[item["candidate_id"] for item in unresolved_folder_conflicts[:3]],
                    ],
                    "folder_conflicts": unresolved_folder_conflicts[:5],
                }, evidence

            if same_title_year_candidates:
                if direct_profile is None:
                    direct_profile = self._tmdb_identity_profile(direct_candidate)
                evidence["same_title_year_candidates"] = same_title_year_candidates
                evidence["selected_identity_profile"] = direct_profile

            source_year_corrected = bool(
                direct_detail["year_delta"] is not None
                and direct_detail["year_delta"] > 1
                and direct_detail["folder_title_similarity"] >= 0.94
                and "folder_search" in sources.get(direct_id, set())
            )
            if direct_detail["year_delta"] is not None and direct_detail["year_delta"] > 1 and not source_year_corrected:
                return None, {
                    "status": "review",
                    **direct_detail,
                    "reason": "TinEye external ID conflicts with the source folder year and lacks independent exact-title confirmation",
                    "candidate_id": direct_id,
                    "candidate_title": direct_candidate.get("title"),
                }, evidence

            return direct_candidate, {
                "status": "ready",
                **direct_detail,
                "confidence": 1.0,
                "source_year_corrected": source_year_corrected,
                "same_title_year_collision_resolved": bool(same_title_year_candidates),
                "reason": (
                    "TinEye IMDb/TMDB backlink resolved a same-title/year collision using the linked movie ID and TMDB identity metadata"
                    if same_title_year_candidates
                    else (
                        "TinEye ID and independent exact folder-title search agree; "
                        "TMDB release year will be used for the JSON filename"
                        if source_year_corrected
                        else "TinEye backlink resolved to a single title/year-validated IMDb/TMDB movie ID"
                    )
                ),
            }, evidence

        top_score, top_id, top_candidate, top_detail = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        # If folder-name matching and TinEye-title matching strongly disagree,
        # preserve both candidates for review rather than blending the signals.
        folder_ranked = sorted(
            (
                (best_folder_match(candidate)[0]["confidence"], candidate_id)
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

        source_year_corrected = bool(
            top_detail["year_delta"] is not None
            and top_detail["year_delta"] > 1
            and top_detail["folder_title_similarity"] >= 0.98
            and top_detail["tineye_title_similarity"] >= 0.94
            and "folder_search" in sources.get(top_id, set())
        )
        if source_year_corrected:
            return top_candidate, {
                "status": "ready",
                **top_detail,
                "confidence": 1.0,
                "source_year_corrected": True,
                "reason": (
                    "Exact source-folder title, independent TinEye title hint, "
                    "and TMDB search agree; TMDB release year will be used for the JSON filename"
                ),
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

    def _record_for_folder(
        self,
        item: dict[str, str],
        previous_output_path: str | None = None,
    ) -> dict[str, Any]:
        folder = item["folder"]
        image_path = item["image_path"]
        title_hint, year_hint = split_title_year(folder)
        image_url = self.github.raw_url(image_path)

        latest_tineye_results = self._tineye_results(image_url)
        latest_errors = [
            item for item in latest_tineye_results
            if isinstance(item, dict) and item.get("error")
        ]
        latest_query_status = (
            "error" if latest_errors
            else "ok" if latest_tineye_results
            else "no_matches"
        )

        # A temporary empty/unresponsive TinEye response must not erase useful
        # reverse-image evidence already stored for this exact poster.
        tineye_results = latest_tineye_results
        tineye_results_source = "live"
        if not latest_tineye_results or latest_errors:
            previous_record = (
                self.github.get_json_file(previous_output_path)
                if previous_output_path
                else None
            )
            previous_tineye = (previous_record or {}).get("tineye") or {}
            saved_results = previous_tineye.get("results") or []
            reusable_results = [
                result for result in saved_results
                if isinstance(result, dict)
                and not result.get("error")
                and any(result.get(key) for key in ("title", "url", "source"))
            ]
            if reusable_results:
                tineye_results = reusable_results
                tineye_results_source = "previous_saved"

        candidate, match, tineye_evidence = self._find_movie(title_hint, year_hint, tineye_results)
        tineye_evidence["results_source"] = tineye_results_source

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
                "results_source": tineye_results_source,
                "latest_query": {
                    "status": latest_query_status,
                    "errors": latest_errors,
                },
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
            title_selection = self.tmdb.select_worldwide_title(normalized)
            canonical_title = title_selection.get("title") or normalized.get("title") or candidate.get("title") or title_hint
            canonical_year = normalized.get("year")
            canonical_folder_name = (
                f"{canonical_title} ({canonical_year})" if canonical_year else canonical_title
            )
            normalized["worldwide_title"] = canonical_title
            normalized["worldwide_title_selection"] = title_selection
            record["tmdb"] = normalized
            record["title_policy_version"] = WORLDWIDE_TITLE_POLICY_VERSION
            record["correction"] = {
                # Legacy key retained for catalog-reader compatibility; this
                # is a JSON basename, never a physical folder rename.
                "canonical_folder_name": canonical_folder_name,
                "canonical_json_name": canonical_folder_name,
                "worldwide_title": canonical_title,
                "title_selection": title_selection,
                "json_rename_required": safe_segment(folder).casefold()
                != safe_segment(canonical_folder_name).casefold(),
                "rename_required": False,
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
            previous_row = old_by_folder.get(item["folder"]) or {}
            try:
                record = self._record_for_folder(
                    item,
                    previous_output_path=previous_row.get("output_path"),
                )
            except Exception as exc:
                # Isolate failures to this film. Persist it as REVIEW so a later
                # batch can retry it; one malformed TinEye title or API response
                # must not abort the remaining films in the batch.
                folder = item["folder"]
                image_path = item["image_path"]
                title_hint, year_hint = split_title_year(folder)
                image_url = self.github.raw_url(image_path)
                previous_record = None
                if previous_row.get("output_path"):
                    try:
                        previous_record = self.github.get_json_file(previous_row["output_path"])
                    except Exception:
                        # Preserve the original processing error; Git history still
                        # retains the previously committed metadata if this read fails.
                        previous_record = None
                previous_tineye = (previous_record or {}).get("tineye") or {}
                previous_tineye_results = [
                    result for result in (previous_tineye.get("results") or [])
                    if isinstance(result, dict)
                    and not result.get("error")
                    and any(result.get(key) for key in ("title", "url", "source"))
                ]
                record = {
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
                    "match": {
                        "status": "review",
                        "confidence": 0.0,
                        "reason": "Processing failed before a verified movie match; item is eligible for retry",
                    },
                    "tineye": {
                        "enabled": self.searxng is not None,
                        "results": previous_tineye_results,
                        "results_source": "previous_saved" if previous_tineye_results else "none",
                        "latest_query": {
                            "status": "processing_error",
                            "errors": [{
                                "type": type(exc).__name__,
                                "message": str(exc).replace("\\n", " ")[:300],
                            }],
                        },
                        "evidence": previous_tineye.get("evidence") or {},
                    },
                    "tmdb": None,
                    "correction": None,
                    "status": "review",
                    "processing_error": {
                        "type": type(exc).__name__,
                        "message": str(exc).replace("\n", " ")[:500],
                    },
                }
                print(
                    f"[KafiaNet] [{position}/{total}] ERROR "
                    f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:220]}",
                    flush=True,
                )
            records.append(record)
            tmdb_id = (record.get("tmdb") or {}).get("tmdb_id")
            tineye_info = record.get("tineye") or {}
            latest_query_status = (tineye_info.get("latest_query") or {}).get("status", "unknown")
            tineye_source = tineye_info.get("results_source", "live")
            match_info = record.get("match") or {}
            reason = str(match_info.get("reason") or "unknown")
            reason = " ".join(reason.replace('"', "'").replace("\\r", " ").replace("\\n", " ").split())[:150]
            candidate_ids = match_info.get("candidate_ids") or []
            if not candidate_ids and match_info.get("candidate_id") is not None:
                candidate_ids = [match_info["candidate_id"]]
            candidate_text = ",".join(str(value) for value in candidate_ids[:4]) or "none"
            rejected_ids = [
                str(value.get("candidate_id"))
                for value in (tineye_info.get("evidence") or {}).get("rejected_external_ids", [])
                if isinstance(value, dict) and value.get("candidate_id") is not None
            ]
            rejected_text = ",".join(rejected_ids[:4]) or "none"
            print(
                f"[KafiaNet] [{position}/{total}] DONE status={record['status']} "
                f"tmdb_id={tmdb_id if tmdb_id is not None else 'none'} "
                f"tineye={latest_query_status} evidence={tineye_source} "
                f'reason="{reason}" candidates={candidate_text} '
                f"rejected_external_ids={rejected_text}",
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
                    "canonical_json_name": (record.get("correction") or {}).get("canonical_json_name"),
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
                title = correction.get("canonical_json_name") or correction.get("canonical_folder_name")
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
                target_prefix = f"مكرر/{tmdb_id}/{folder}"
                duplicate_count += 1
            elif record["status"] == "ready":
                target_prefix = f"جاهز/{folder}"
                ready_count += 1
            else:
                target_prefix = f"مراجعة/{folder}"
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
                    old_prefix = f"جاهز/{source_folder}"
                    new_prefix = f"مكرر/{tmdb_id}/{source_folder}"
                    original_image_path = (
                        row.get("original_image_path") or row.get("image_path")
                    )
                    moved_images = self.github.list_folder_blobs(old_prefix)
                    image_source_prefix = old_prefix
                    # During the first migration, older READY JSON files may
                    # exist while their images are still in the original root.
                    if not moved_images:
                        moved_images = self.github.list_folder_blobs(source_folder)
                        image_source_prefix = source_folder
                    for poster in moved_images:
                        source_path = poster["path"]
                        relative_path = source_path[len(image_source_prefix) + 1:]
                        target_path = f"{new_prefix}/{relative_path}"
                        moves[target_path] = {"sha": poster["sha"], "mode": poster["mode"]}
                        image_deletes.append(source_path)
                        if row.get("image_path") == source_path:
                            row["image_path"] = target_path
                            old_record["source"]["image_path"] = target_path
                            old_record["source"]["image_url"] = self.github.raw_url(target_path)
                    old_record["source"].setdefault("original_image_path", original_image_path)
                    row["original_image_path"] = original_image_path

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
            "tineye_unresponsive": sum(
                1 for record in records
                if (record.get("tineye") or {}).get("latest_query", {}).get("status")
                in {"error", "processing_error"}
            ),
            "tineye_no_matches": sum(
                1 for record in records
                if (record.get("tineye") or {}).get("latest_query", {}).get("status")
                == "no_matches"
            ),
            "tineye_previous_evidence_reused": sum(
                1 for record in records
                if (record.get("tineye") or {}).get("results_source") == "previous_saved"
            ),
            "tineye_rejected_external_ids": sum(
                len(
                    ((record.get("tineye") or {}).get("evidence") or {}).get(
                        "rejected_external_ids", []
                    )
                )
                for record in records
            ),
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
