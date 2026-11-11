from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.github_api import GitHubAPI
from app.image_evidence import (
    extract_imdb_ids,
    extract_tmdb_movie_ids,
    extract_wikidata_ids,
    tmdb_movie_url_diagnostics,
)
from app.local_ocr import LocalPosterOCR
from app.google_lens import BrightDataGoogleLensClient, BrightDataQuotaError
from app.tmdb import TMDBClient
from app.wikipedia import WikipediaClient
from app.wikidata import WikidataClient


SCHEMA_VERSION = 3
PLAN_SCHEMA_VERSION = 1
MAX_LENS_TMDB_ID_CANDIDATES = 10
MAX_OCR_TMDB_QUERIES = 3
MAX_OCR_TMDB_RESULTS_PER_QUERY = 5
MAX_WIKIPEDIA_TITLE_HINTS = 5
MAX_WIKIPEDIA_ARTICLES = 8
MAX_WIKIPEDIA_TMDB_RESULTS_PER_ARTICLE = 5
TRANSIENT_PROVIDER_HTTP_STATUSES = {500, 502, 503, 504}


def safe_segment(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return (value or "untitled")[:140]


def _comparison_title_key(value: str | None) -> str:
    """Normalize a display title for conservative Wikipedia/TMDB comparison."""
    title = " ".join(str(value or "").split()).strip()
    title = re.sub(
        r"\s*\(\s*(?:(?:18|19|20)\d{2}\s+)?film\s*\)\s*$",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s*\(\s*(?:18|19|20)\d{2}\s*\)\s*$", "", title)
    title = unicodedata.normalize("NFKD", title)
    title = "".join(char for char in title if not unicodedata.combining(char))
    return re.sub(r"[^\w]+", " ", title.casefold(), flags=re.UNICODE).strip()

def _tmdb_details_match_lens_title(
    details: dict[str, Any],
    title_hint: str,
    year_hint: int | str | None,
) -> bool:
    """Match a Lens-derived primary title/year against an already-resolved TMDB record.

    This helper may disambiguate official IDs returned by the title-scoped search;
    it never creates an ID or authorizes a title-only identity.
    """
    hint_key = _comparison_title_key(title_hint)
    if not hint_key:
        return False

    normalized = TMDBClient.normalize_details(details or {})
    aliases = [
        normalized.get("title"),
        normalized.get("original_title"),
    ]
    aliases.extend(
        row.get("title")
        for row in (normalized.get("alternative_titles") or [])
        if isinstance(row, dict)
    )
    compact_hint_key = "".join(hint_key.split())
    title_matches = any(
        _comparison_title_key(value) == hint_key
        or "".join(_comparison_title_key(value).split()) == compact_hint_key
        for value in aliases
        if str(value or "").strip()
    )
    if not title_matches:
        return False

    try:
        expected_year = int(year_hint) if year_hint is not None else None
    except (TypeError, ValueError):
        expected_year = None
    if expected_year is None:
        return True

    actual_year = normalized.get("year")
    try:
        actual_year = int(actual_year) if actual_year is not None else None
    except (TypeError, ValueError):
        actual_year = None
    return actual_year == expected_year


def _identity_title_tokens(value: str | None) -> list[str]:
    """Normalize a title to Unicode-aware word tokens for exact phrase matching."""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return re.findall(r"[^\W_]+", normalized.casefold(), flags=re.UNICODE)


def _contains_title_tokens(folder_tokens: list[str], title_tokens: list[str]) -> bool:
    if not title_tokens:
        return False
    # Very short one-word names are too ambiguous to act as a safe veto key.
    if len(title_tokens) == 1 and len(title_tokens[0]) < 5:
        return False
    width = len(title_tokens)
    return any(
        folder_tokens[start:start + width] == title_tokens
        for start in range(len(folder_tokens) - width + 1)
    )


def _folder_identity_consistency(
    details: dict[str, Any],
    expected_folder: str | None,
) -> dict[str, Any]:
    """Reject an official ID whose resolved title/year conflicts with its source label.

    The folder label is a negative-only consistency veto: it is never sent to a
    search provider, used to discover an ID, or used to choose between candidates.
    """
    if not str(expected_folder or "").strip():
        return {
            "status": "not_applied",
            "allowed": True,
            "reason": "No source-folder label was supplied for the consistency veto",
            "folder_name_used_for_search": False,
            "folder_name_used_to_select_candidate": False,
            "folder_name_used_for_conflict_veto": False,
        }

    normalized = TMDBClient.normalize_details(details or {})
    folder = str(expected_folder).strip()
    folder_year_match = re.search(r"\(((?:18|19|20)\d{2})\)\s*$", folder)
    folder_year = int(folder_year_match.group(1)) if folder_year_match else None
    folder_title = folder[:folder_year_match.start()].strip() if folder_year_match else folder
    folder_tokens = _identity_title_tokens(folder_title)

    aliases: list[str] = []
    for value in (normalized.get("title"), normalized.get("original_title")):
        title = str(value or "").strip()
        if title and title not in aliases:
            aliases.append(title)
    for row in normalized.get("alternative_titles") or []:
        if isinstance(row, dict):
            title = str(row.get("title") or "").strip()
            if title and title not in aliases:
                aliases.append(title)

    matching_aliases = [
        title for title in aliases
        if _contains_title_tokens(
            folder_tokens,
            _identity_title_tokens(title),
        )
    ]
    candidate_year = normalized.get("year")
    try:
        candidate_year = int(candidate_year) if candidate_year is not None else None
    except (TypeError, ValueError):
        candidate_year = None

    title_match = bool(matching_aliases)
    year_match = (
        folder_year is None
        or (candidate_year is not None and candidate_year == folder_year)
    )
    # Folder labels often contain local-language names, transliterations, or
    # incomplete years. Treat them as a veto only when BOTH fields explicitly
    # contradict the already-verified official TMDB identity.
    clear_title_and_year_conflict = (
        not title_match
        and folder_year is not None
        and candidate_year is not None
        and candidate_year != folder_year
    )
    allowed = not clear_title_and_year_conflict
    return {
        "status": "conflict" if clear_title_and_year_conflict else "passed",
        "allowed": allowed,
        "reason": (
            "Resolved TMDB identity conflicts with both the source-folder title and release year; automatic match blocked"
            if clear_title_and_year_conflict
            else "No confirmed title-and-year contradiction with the source-folder label"
        ),
        "folder_year": folder_year,
        "candidate_year": candidate_year,
        "title_match": title_match,
        "year_match": year_match,
        "clear_title_and_year_conflict": clear_title_and_year_conflict,
        "matching_title_aliases": matching_aliases[:5],
        "folder_name_used_for_search": False,
        "folder_name_used_to_select_candidate": False,
        "folder_name_used_for_conflict_veto": True,
    }


def _canonical_plan_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class MetadataEngine:
    def __init__(self, settings: Settings, *, research_enabled: bool = True):
        self.settings = settings
        self._stop_batch = False
        self._provider_stop_status: int | None = None
        self._provider_stop_reason: str | None = None
        self._details_cache: dict[int, dict[str, Any]] = {}
        self.github = GitHubAPI(
            settings.posters_repo,
            settings.posters_branch,
            token=settings.github_token,
            timeout=settings.http_timeout_seconds,
        )
        self.tmdb = (
            TMDBClient(
                settings.tmdb_bearer_token,
                settings.tmdb_language,
                settings.http_timeout_seconds,
            )
            if research_enabled else None
        )
        self.wikipedia = (
            WikipediaClient(timeout=settings.http_timeout_seconds)
            if research_enabled else None
        )
        self.reverse_image = (
            BrightDataGoogleLensClient(
                settings.brightdata_api_key,
                settings.brightdata_serp_zone,
                timeout=max(45, settings.http_timeout_seconds),
            )
            if research_enabled else None
        )
        self.wikidata = (
            WikidataClient(timeout=settings.http_timeout_seconds)
            if research_enabled else None
        )
        self.local_ocr = (
            LocalPosterOCR(timeout=settings.http_timeout_seconds)
            if research_enabled and bool(getattr(settings, "local_ocr_enabled", False))
            else None
        )

    def close(self) -> None:
        self.github.close()
        if getattr(self, "tmdb", None):
            self.tmdb.close()
        if getattr(self, "wikipedia", None):
            self.wikipedia.close()
        if self.reverse_image:
            self.reverse_image.close()
        if getattr(self, "wikidata", None):
            self.wikidata.close()
        if getattr(self, "local_ocr", None):
            self.local_ocr.close()

    @staticmethod
    def _load_plan_file(path: str) -> dict[str, Any]:
        plan_path = Path(path).expanduser()
        try:
            document = json.loads(plan_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RuntimeError(f"Saved DRY_RUN plan does not exist: {plan_path}") from None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot read saved DRY_RUN plan {plan_path}: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise RuntimeError("Saved DRY_RUN plan is invalid: expected a JSON object")
        expected_digest = str(document.get("sha256") or "")
        digest_payload = dict(document)
        digest_payload.pop("sha256", None)
        actual_digest = hashlib.sha256(
            _canonical_plan_json(digest_payload).encode("utf-8")
        ).hexdigest()
        if not expected_digest or expected_digest != actual_digest:
            raise RuntimeError("Saved DRY_RUN plan integrity check failed; refusing to apply it")
        if int(document.get("plan_schema_version") or 0) != PLAN_SCHEMA_VERSION:
            raise RuntimeError("Saved DRY_RUN plan version is unsupported; create a new DRY_RUN plan")
        if not isinstance(document.get("pending"), list) or not isinstance(document.get("records"), list):
            raise RuntimeError("Saved DRY_RUN plan is incomplete: pending items or records are missing")
        return document

    @staticmethod
    def _write_plan_file(path: str, document: dict[str, Any]) -> str:
        plan_path = Path(path).expanduser()
        if plan_path.exists():
            raise FileExistsError(
                f"Refusing to overwrite an existing DRY_RUN plan: {plan_path}"
            )
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(document)
        payload.pop("sha256", None)
        payload["sha256"] = hashlib.sha256(
            _canonical_plan_json(payload).encode("utf-8")
        ).hexdigest()
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        temporary_path = plan_path.with_name(plan_path.name + ".tmp")
        try:
            with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
            temporary_path.replace(plan_path)
        except Exception:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return payload["sha256"]

    def _stop_for_provider_server_error(self, diagnostics: Any) -> bool:
        """Stop the remaining batch after an exhausted Bright Data 5xx response."""
        if not isinstance(diagnostics, dict):
            return False
        raw_status = diagnostics.get("http_status")
        try:
            status_code = int(raw_status)
        except (TypeError, ValueError):
            return False
        if status_code not in TRANSIENT_PROVIDER_HTTP_STATUSES:
            return False
        self._stop_batch = True
        self._provider_stop_status = status_code
        self._provider_stop_reason = "provider_server_error"
        return True

    def _reverse_image_results(self, image_url: str) -> list[dict[str, Any]]:
        if not self.reverse_image:
            return []
        try:
            return self.reverse_image.search_by_image_url(image_url)
        except BrightDataQuotaError as exc:
            # Free quota exhaustion and provider throttling stop the whole batch.
            self._stop_batch = True
            self._provider_stop_status = exc.status_code
            self._provider_stop_reason = "provider_quota_or_throttle"
            return [{
                "error": type(exc).__name__,
                "message": str(exc)[:300],
                "stop_batch": True,
            }]
        except Exception as exc:
            # Exhausted upstream 5xx retries indicate a provider-side failure.
            # Stop before querying every remaining poster and burning more quota.
            diagnostics = getattr(self.reverse_image, "last_lens_diagnostics", {})
            stop_batch = self._stop_for_provider_server_error(diagnostics)
            result = {"error": type(exc).__name__, "message": str(exc)[:300]}
            if stop_batch:
                result["stop_batch"] = True
            return [result]


    def _resolve_unique_exact_lens_tmdb_title(
        self,
        reverse_image_results: list[dict[str, Any]],
    ) -> tuple[list[int], dict[str, Any]]:
        """Resolve a title caption from Lens exact_matches only when TMDB has one exact result.

        This is a quota-saving fast path, not fuzzy title matching: folder names,
        filenames, visual_matches captions, Play Store slugs, and generic Google
        captions are excluded. A partial or ambiguous TMDB result falls back to
        the existing official IMDb/TMDB page search and remains REVIEW otherwise.
        """
        diagnostics: dict[str, Any] = {
            "status": "not_run",
            "provider": "tmdb_search_movie",
            "query_count": 0,
            "title_hint": None,
            "year_hint": None,
            "exact_candidate_count": 0,
            "candidate_tmdb_ids": [],
            "selected_tmdb_id": None,
            "details_verified": False,
            "reason": None,
        }
        lens_client = getattr(self, "reverse_image", None)
        hint_extractor = getattr(lens_client, "title_hints_from_lens_results", None)
        if not callable(hint_extractor):
            diagnostics.update({
                "status": "skipped_title_extractor_unavailable",
                "reason": "Google Lens title extractor is unavailable",
            })
            return [], diagnostics

        exact_rows = [
            row for row in (reverse_image_results or [])
            if isinstance(row, dict)
            and not row.get("error")
            and row.get("lens_tab") == "exact_matches"
        ]

        # Backward compatibility for providers/tests that return only the exact
        # tab and expose its tab diagnostics, but did not tag individual rows.
        lens_diagnostics = getattr(lens_client, "last_lens_diagnostics", {}) or {}
        attempted_tabs = list(lens_diagnostics.get("lens_tabs_attempted") or [])
        if (
            not exact_rows
            and attempted_tabs == ["exact_matches"]
            and lens_diagnostics.get("lens_tab") == "exact_matches"
        ):
            exact_rows = [
                row for row in (reverse_image_results or [])
                if isinstance(row, dict)
                and not row.get("error")
                and "lens_tab" not in row
            ]

        if not exact_rows:
            diagnostics.update({
                "status": "skipped_no_exact_lens_rows",
                "reason": "No title-bearing rows were confirmed as Google Lens exact_matches",
            })
            return [], diagnostics

        # Pass captions only. This deliberately excludes Play Store slugs and
        # wrapped Google destinations, which can describe unrelated content.
        caption_rows = [
            {"title": str(row.get("title") or "")}
            for row in exact_rows
            if str(row.get("title") or "").strip()
        ]
        try:
            title_hints, year_hint = hint_extractor(caption_rows)
        except Exception as exc:
            diagnostics.update({
                "status": "title_extraction_error",
                "reason": f"{type(exc).__name__}: {str(exc)[:220]}",
            })
            return [], diagnostics

        title_hint = next(
            (str(value).strip() for value in (title_hints or []) if str(value or "").strip()),
            "",
        )
        diagnostics["title_hint"] = title_hint or None
        diagnostics["year_hint"] = year_hint
        if not title_hint:
            diagnostics.update({
                "status": "skipped_no_usable_exact_title",
                "reason": "Lens exact_matches returned no usable movie title caption",
            })
            return [], diagnostics

        tmdb = getattr(self, "tmdb", None)
        search_movies = getattr(tmdb, "search_movies", None)
        if not callable(search_movies):
            diagnostics.update({
                "status": "skipped_tmdb_search_unavailable",
                "reason": "TMDB title search is unavailable",
            })
            return [], diagnostics

        diagnostics["query_count"] = 1
        try:
            payload = search_movies(title_hint, year=year_hint)
        except Exception as exc:
            diagnostics.update({
                "status": "tmdb_search_error",
                "reason": f"{type(exc).__name__}: {str(exc)[:220]}",
            })
            return [], diagnostics

        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            diagnostics.update({
                "status": "invalid_tmdb_search_response",
                "reason": "TMDB title search returned an invalid result payload",
            })
            return [], diagnostics

        search_rows = [row for row in payload.get("results") or [] if isinstance(row, dict)]
        raw_total = payload.get("total_results")
        try:
            total_results = int(raw_total)
        except (TypeError, ValueError):
            diagnostics.update({
                "status": "incomplete_tmdb_search_response",
                "reason": "TMDB did not return total_results; uniqueness cannot be proven",
            })
            return [], diagnostics

        if total_results > len(search_rows):
            diagnostics.update({
                "status": "incomplete_tmdb_search_response",
                "reason": (
                    f"TMDB returned {len(search_rows)} of {total_results} results; "
                    "uniqueness cannot be proven from a partial page"
                ),
                "tmdb_total_results": total_results,
                "tmdb_returned_results": len(search_rows),
            })
            return [], diagnostics

        expected_title = _comparison_title_key(title_hint)
        exact_by_id: dict[int, dict[str, Any]] = {}
        for row in search_rows:
            title_matches = expected_title in {
                _comparison_title_key(str(row.get("title") or "")),
                _comparison_title_key(str(row.get("original_title") or "")),
            }
            if not title_matches:
                continue
            if year_hint is not None:
                release_date = str(row.get("release_date") or "")
                release_year = int(release_date[:4]) if release_date[:4].isdigit() else None
                if release_year != int(year_hint):
                    continue
            raw_id = row.get("id")
            if str(raw_id or "").isdigit():
                exact_by_id.setdefault(int(raw_id), row)

        candidate_ids = sorted(exact_by_id)
        diagnostics.update({
            "tmdb_total_results": total_results,
            "tmdb_returned_results": len(search_rows),
            "exact_candidate_count": len(candidate_ids),
            "candidate_tmdb_ids": candidate_ids,
        })
        if len(candidate_ids) != 1:
            diagnostics.update({
                "status": (
                    "ambiguous_exact_tmdb_title"
                    if len(candidate_ids) > 1
                    else "no_exact_tmdb_title"
                ),
                "reason": (
                    "More than one TMDB movie exactly matches the Lens title; refusing an automatic choice"
                    if len(candidate_ids) > 1
                    else "No TMDB movie exactly matches the Lens title and available Lens year"
                ),
            })
            return [], diagnostics

        selected_id = candidate_ids[0]
        try:
            details = tmdb.movie_details(selected_id)
            if not isinstance(details, dict) or int(details.get("id") or 0) != selected_id:
                raise ValueError("TMDB details returned a different movie ID")
            if not _tmdb_details_match_lens_title(details, title_hint, year_hint):
                raise ValueError(
                    "TMDB movie details did not confirm the exact Lens title and available Lens year"
                )
        except Exception as exc:
            diagnostics.update({
                "status": "tmdb_details_verification_failed",
                "reason": f"{type(exc).__name__}: {str(exc)[:220]}",
            })
            return [], diagnostics

        if not isinstance(getattr(self, "_details_cache", None), dict):
            self._details_cache = {}
        self._details_cache[selected_id] = details
        diagnostics.update({
            "status": "resolved",
            "selected_tmdb_id": selected_id,
            "details_verified": True,
            "reason": "One exact Lens title matched one complete TMDB result and official movie details confirmed it",
        })
        return [selected_id], diagnostics

    def _find_movie(
        self,
        reverse_image_results: list[dict[str, Any]],
        expected_folder: str = "",
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        """Resolve typed movie identifiers and entity IDs returned by Google Lens.

        A Lens Wikidata QID may resolve through TMDB /find only when exactly one
        movie_result is returned. All accepted IDs still require TMDB movie-details
        verification before a READY identity can be emitted.
        """
        reverse_image_failed = any(
            isinstance(item, dict) and item.get("error")
            for item in (reverse_image_results or [])
        )
        observed_lens_tmdb_ids = extract_tmdb_movie_ids(reverse_image_results)
        observed_lens_imdb_ids = extract_imdb_ids(reverse_image_results)
        observed_lens_wikidata_ids = extract_wikidata_ids(reverse_image_results)
        # Prefer identifiers carried by official movie URLs or explicitly typed
        # Lens metadata. Captions, product IDs, store IDs, filenames and folder
        # labels never create or select a movie ID.
        direct_tmdb_ids = observed_lens_tmdb_ids[:1]
        direct_imdb_ids = observed_lens_imdb_ids[:1]
        wikidata_resolution: dict[str, Any] = {
            "status": (
                "skipped_official_lens_id_present"
                if (direct_tmdb_ids or direct_imdb_ids)
                else "no_wikidata_id"
                if not observed_lens_wikidata_ids
                else "pending"
            ),
            "wikidata_ids_seen": list(observed_lens_wikidata_ids),
            "selected_wikidata_id": None,
            "tmdb_movie_ids": [],
            "movie_result_count": 0,
            "errors": [],
        }
        wikidata_tmdb_ids: list[int] = []
        # A QID is only a typed entity identifier from Lens metadata or a
        # canonical Wikidata entity URL. Resolve it through TMDB /find; only a
        # single movie_result can continue to the normal TMDB details check.
        if not (direct_tmdb_ids or direct_imdb_ids) and observed_lens_wikidata_ids:
            wikidata_id = observed_lens_wikidata_ids[0]
            wikidata_resolution["selected_wikidata_id"] = wikidata_id
            try:
                wikidata_find = self.tmdb.find_by_wikidata_id(wikidata_id)
                movie_rows = wikidata_find.get("movie_results") or []
                mapped_ids = sorted({
                    int(row["id"])
                    for row in movie_rows
                    if isinstance(row, dict)
                    and str(row.get("id") or "").isdigit()
                    and int(row.get("id") or 0) > 0
                })
                wikidata_resolution.update({
                    "status": (
                        "resolved" if len(mapped_ids) == 1
                        else "ambiguous_tmdb_movie_results" if len(mapped_ids) > 1
                        else "no_tmdb_movie_match"
                    ),
                    "tmdb_movie_ids": mapped_ids,
                    "movie_result_count": len(mapped_ids),
                })
                if len(mapped_ids) == 1:
                    wikidata_tmdb_ids = mapped_ids
            except Exception as exc:
                wikidata_resolution["status"] = "lookup_error"
                wikidata_resolution["errors"] = [{
                    "type": type(exc).__name__,
                    "message": str(exc)[:220],
                }]
        lens_tmdb_ids = list(direct_tmdb_ids or wikidata_tmdb_ids)
        lens_imdb_ids = list(direct_imdb_ids)
        title_search_tmdb_ids: list[int] = []
        title_search_imdb_ids: list[str] = []
        title_search_diagnostics: dict[str, Any] = {
            "status": "disabled_id_only_mode",
            "query_count": 0,
            "query_variants": [],
            "title_hints": [],
            "year_hint": None,
            "response_format": "json",
            "payload_shape": None,
            "http_status": None,
            "attempts": 0,
            "organic_result_count": 0,
            "candidate_url_count": 0,
            "google_redirect_count": 0,
            "google_redirect_unwrapped_count": 0,
            "candidate_domains": {},
            "official_url_count": 0,
            "catalog_source_count": 0,
            "afi_catalog_result_count": 0,
            "bfi_archive_result_count": 0,
            "error": None,
        }

        def title_search_evidence() -> dict[str, Any]:
            return {
                "source": "disabled_google_search_id_only_mode",
                "status": title_search_diagnostics.get("status", "not_run"),
                "query_count": title_search_diagnostics.get("query_count", 0),
                "query_variants": list(title_search_diagnostics.get("query_variants") or []),
                "title_hints": list(title_search_diagnostics.get("title_hints") or [])[:5],
                "year_hint": title_search_diagnostics.get("year_hint"),
                "response_format": title_search_diagnostics.get("response_format", "json"),
                "payload_shape": title_search_diagnostics.get("payload_shape"),
                "http_status": title_search_diagnostics.get("http_status"),
                "attempts": title_search_diagnostics.get("attempts", 0),
                "rate_limit_retries": title_search_diagnostics.get("rate_limit_retries", 0),
                "rate_limit_wait_seconds": title_search_diagnostics.get("rate_limit_wait_seconds", 0),
                "organic_result_count": title_search_diagnostics.get("organic_result_count", 0),
                "candidate_url_count": title_search_diagnostics.get("candidate_url_count", 0),
                "google_redirect_count": title_search_diagnostics.get("google_redirect_count", 0),
                "google_redirect_unwrapped_count": title_search_diagnostics.get("google_redirect_unwrapped_count", 0),
                "candidate_domains": dict(title_search_diagnostics.get("candidate_domains") or {}),
                "official_url_count": title_search_diagnostics.get("official_url_count", 0),
                "catalog_source_count": title_search_diagnostics.get("catalog_source_count", 0),
                "afi_catalog_result_count": title_search_diagnostics.get("afi_catalog_result_count", 0),
                "bfi_archive_result_count": title_search_diagnostics.get("bfi_archive_result_count", 0),
                "error": title_search_diagnostics.get("error"),
                "tmdb_ids": list(title_search_tmdb_ids),
                "imdb_ids": list(title_search_imdb_ids),
            }

        tmdb_id_lookup: dict[str, Any] = {
            "status": "resolving" if (lens_tmdb_ids or lens_imdb_ids) else "no_tmdb_id",
            "tmdb_ids": list(lens_tmdb_ids),
            "direct_tmdb_ids": list(direct_tmdb_ids),
            "wikidata_tmdb_ids": list(wikidata_tmdb_ids),
            "wikidata_resolution": wikidata_resolution,
            "imdb_ids": list(lens_imdb_ids),
            "imdb_find_results": {},
            "imdb_find_errors": [],
            "details_resolved": 0,
            "errors": [],
            "folder_name_used": False,
            "image_filename_used": False,
        }
        evidence: dict[str, Any] = {
            "strategy": "official_tmdb_or_imdb_id_only",
            "tmdb_ids": list(lens_tmdb_ids),
            "imdb_ids": list(lens_imdb_ids),
            "tmdb_id_lookup": tmdb_id_lookup,
            "id_resolution": {
                "strategy": "google_lens_typed_identifier_or_entity_url_then_tmdb_find",
                "tmdb_ids_seen": list(lens_tmdb_ids),
                "imdb_ids_seen": list(lens_imdb_ids),
                "imdb_to_tmdb_ids": {},
                "wikidata_ids_seen": list(observed_lens_wikidata_ids),
                "wikidata_to_tmdb_ids": (
                    {wikidata_resolution.get("selected_wikidata_id"): list(wikidata_tmdb_ids)}
                    if wikidata_resolution.get("selected_wikidata_id") else {}
                ),
                "unresolved_tmdb_ids": [],
                "folder_name_used": False,
                "image_filename_used": False,
                "errors": [],
            },
            "candidate_identities": [],
            "wikidata_resolution": wikidata_resolution,
            "official_title_search": title_search_evidence(),
        }

        lens_title_tmdb_ids: list[int] = []
        lens_title_resolution: dict[str, Any] = {
            "status": "disabled_id_only_mode",
            "query_count": 0,
            "title_hint": None,
            "year_hint": None,
            "exact_candidate_count": 0,
            "candidate_tmdb_ids": [],
            "selected_tmdb_id": None,
            "details_verified": False,
            "reason": "Identity is obtained from a movie ID returned by Google Lens, then verified by TMDB",
        }
        evidence["lens_title_resolution"] = lens_title_resolution
        evidence["id_resolution"]["lens_title_tmdb_ids"] = []
        evidence["id_resolution"]["observed_lens_tmdb_ids"] = observed_lens_tmdb_ids
        evidence["id_resolution"]["observed_lens_imdb_ids"] = observed_lens_imdb_ids
        evidence["id_resolution"]["observed_lens_wikidata_ids"] = observed_lens_wikidata_ids
        evidence["id_resolution"]["ignored_secondary_lens_tmdb_ids"] = observed_lens_tmdb_ids[1:]
        evidence["id_resolution"]["ignored_secondary_lens_imdb_ids"] = observed_lens_imdb_ids[1:]
        tmdb_id_lookup["lens_title_tmdb_ids"] = []
        tmdb_id_lookup["observed_lens_tmdb_ids"] = observed_lens_tmdb_ids
        tmdb_id_lookup["observed_lens_imdb_ids"] = observed_lens_imdb_ids
        tmdb_id_lookup["observed_lens_wikidata_ids"] = observed_lens_wikidata_ids

        if wikidata_resolution.get("status") == "ambiguous_tmdb_movie_results":
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "One Lens Wikidata entity ID resolved to multiple TMDB movie records; refusing to choose between them",
                "candidate_ids": list(wikidata_resolution.get("tmdb_movie_ids") or []),
            }, evidence

        if reverse_image_failed and not (lens_tmdb_ids or lens_imdb_ids):
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "Google Lens image lookup or URL resolution failed; no official movie ID was available",
                "candidate_ids": [],
            }, evidence

        # ID-only pipeline: Google Lens is the sole discovery provider.
        # Do not invoke Google Search or search TMDB by title/caption.
        evidence["official_title_search"] = title_search_evidence()
        evidence["strategy"] = (
            "google_lens_official_id"
            if (direct_tmdb_ids or direct_imdb_ids)
            else "google_lens_wikidata_id_via_tmdb_find"
            if wikidata_tmdb_ids
            else "google_lens_id_only_no_identifier"
        )
        evidence["id_resolution"]["tmdb_ids_seen"] = list(lens_tmdb_ids)
        evidence["id_resolution"]["lens_title_tmdb_ids"] = list(lens_title_tmdb_ids)
        evidence["id_resolution"]["imdb_ids_seen"] = list(lens_imdb_ids)
        evidence["id_resolution"]["google_title_search_tmdb_ids"] = list(title_search_tmdb_ids)
        evidence["id_resolution"]["google_title_search_imdb_ids"] = list(title_search_imdb_ids)
        tmdb_id_lookup.update({
            "status": "resolving" if (lens_tmdb_ids or lens_imdb_ids) else "no_tmdb_id",
            "tmdb_ids": list(lens_tmdb_ids),
            "direct_tmdb_ids": list(direct_tmdb_ids),
            "imdb_ids": list(lens_imdb_ids),
            "google_title_search_tmdb_ids": list(title_search_tmdb_ids),
            "google_title_search_imdb_ids": list(title_search_imdb_ids),
            "lens_title_tmdb_ids": list(lens_title_tmdb_ids),
        })
        evidence["tmdb_ids"] = list(lens_tmdb_ids)
        evidence["imdb_ids"] = list(lens_imdb_ids)

        if self._stop_batch:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "A provider limit or service error was reached; this batch is stopping without catalog writes",
                "candidate_ids": [],
            }, evidence

        if not lens_tmdb_ids and not lens_imdb_ids:
            reason = "Google Lens did not return a verified TMDB movie ID, IMDb title ID, or resolvable Wikidata film ID for this image"
            if wikidata_resolution.get("status") == "no_tmdb_movie_match":
                reason += "; the Lens Wikidata entity had no TMDB movie_result"
            elif wikidata_resolution.get("status") == "lookup_error":
                reason += "; Wikidata ID lookup failed"
            if title_search_diagnostics.get("error"):
                reason += ": " + str(title_search_diagnostics.get("error"))[:180]
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": reason,
                "candidate_ids": [],
            }, evidence

        imdb_to_tmdb_ids: dict[str, list[int]] = {}
        imdb_find_errors: list[dict[str, str]] = []
        for imdb_id in lens_imdb_ids:
            try:
                find_result = self.tmdb.find_by_imdb_id(imdb_id)
                movie_rows = find_result.get("movie_results") or []
                mapped_ids = sorted({
                    int(row["id"])
                    for row in movie_rows
                    if isinstance(row, dict) and str(row.get("id") or "").isdigit()
                })
                imdb_to_tmdb_ids[imdb_id] = mapped_ids
                if not mapped_ids:
                    imdb_find_errors.append({
                        "imdb_id": imdb_id,
                        "error": "NoTMDBMovieMatch",
                        "message": "TMDB /find returned no movie_results for this IMDb ID",
                    })
            except Exception as exc:
                imdb_find_errors.append({
                    "imdb_id": imdb_id,
                    "error": type(exc).__name__,
                    "message": str(exc)[:250],
                })

        evidence["id_resolution"]["imdb_to_tmdb_ids"] = imdb_to_tmdb_ids
        evidence["id_resolution"]["errors"] = imdb_find_errors
        tmdb_id_lookup["imdb_find_results"] = imdb_to_tmdb_ids
        tmdb_id_lookup["imdb_find_errors"] = imdb_find_errors

        direct_imdb_set = set(direct_imdb_ids)

        blocking_imdb_find_errors = [
            row for row in imdb_find_errors
            if str(row.get("imdb_id") or "") in direct_imdb_set
        ]
        ignored_secondary_imdb_errors = [
            row for row in imdb_find_errors
            if str(row.get("imdb_id") or "") not in direct_imdb_set
        ]
        evidence["id_resolution"]["ignored_secondary_imdb_errors"] = ignored_secondary_imdb_errors
        if blocking_imdb_find_errors and not direct_tmdb_ids:
            tmdb_id_lookup["status"] = "direct_imdb_id_unresolved"
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "An official IMDb ID returned directly by Google Lens could not be resolved through TMDB /find",
                "candidate_ids": sorted(set(lens_tmdb_ids).union(
                    value for ids in imdb_to_tmdb_ids.values() for value in ids
                )),
                "imdb_find_errors": blocking_imdb_find_errors,
            }, evidence

        # Preserve priority: Lens' direct TMDB ID first, then the TMDB ID(s)
        # resolved from Lens' first official IMDb ID. Keep fallback candidates
        # available so a failed direct TMDB details lookup does not end review.
        candidate_tmdb_ids = list(dict.fromkeys([
            *lens_tmdb_ids,
            *(value for ids in imdb_to_tmdb_ids.values() for value in ids),
        ]))
        tmdb_id_lookup["tmdb_ids"] = candidate_tmdb_ids
        evidence["tmdb_ids"] = candidate_tmdb_ids
        if not candidate_tmdb_ids:
            tmdb_id_lookup["status"] = "no_tmdb_movie_match"
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "The official IMDb ID did not resolve to a TMDB movie ID",
                "candidate_ids": [],
            }, evidence
        if len(candidate_tmdb_ids) > MAX_LENS_TMDB_ID_CANDIDATES:
            tmdb_id_lookup["status"] = "too_many_candidates"
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": (
                    f"Google Lens official IDs and IMDb /find resolved to {len(candidate_tmdb_ids)} "
                    f"different TMDB movie IDs; the safe lookup limit is {MAX_LENS_TMDB_ID_CANDIDATES}"
                ),
                "candidate_ids": candidate_tmdb_ids,
            }, evidence

        expected_imdb_ids_by_tmdb: dict[int, list[str]] = {}
        for imdb_id, mapped_ids in imdb_to_tmdb_ids.items():
            for tmdb_id in mapped_ids:
                expected_imdb_ids_by_tmdb.setdefault(tmdb_id, []).append(imdb_id)

        resolved: dict[int, dict[str, Any]] = {}
        details_errors: list[dict[str, str]] = []
        for tmdb_id in candidate_tmdb_ids:
            try:
                details = self._details_cache.get(int(tmdb_id))
                if details is None:
                    details = self.tmdb.movie_details(int(tmdb_id))
                    self._details_cache[int(tmdb_id)] = details
                if int(details.get("id") or 0) != int(tmdb_id):
                    raise ValueError("TMDB returned a movie record with a different ID")
                expected_imdb_ids = sorted(set(expected_imdb_ids_by_tmdb.get(tmdb_id, [])))
                returned_imdb_id = str(
                    (details.get("external_ids") or {}).get("imdb_id") or ""
                ).strip().lower()
                if expected_imdb_ids and returned_imdb_id not in expected_imdb_ids:
                    raise ValueError(
                        "TMDB movie details did not confirm the IMDb ID returned by /find"
                    )
                resolved[int(tmdb_id)] = details
                # A verified direct TMDB result wins immediately. Only continue
                # to the IMDb-derived candidate when the higher-priority ID fails.
                break
            except Exception as exc:
                details_errors.append({
                    "tmdb_id": str(tmdb_id),
                    "error": type(exc).__name__,
                    "message": str(exc)[:250],
                })

        evidence["id_resolution"]["errors"].extend(details_errors)
        evidence["id_resolution"]["unresolved_tmdb_ids"] = [
            int(row["tmdb_id"]) for row in details_errors
        ]
        tmdb_id_lookup["details_resolved"] = len(resolved)
        tmdb_id_lookup["errors"] = details_errors
        # Failed higher-priority candidates are diagnostic when a later IMDb
        # fallback resolves successfully; otherwise the empty-resolved path below
        # returns REVIEW with the recorded errors.
        ignored_secondary_detail_errors = list(details_errors)
        evidence["id_resolution"]["ignored_secondary_detail_errors"] = ignored_secondary_detail_errors

        identities: list[dict[str, Any]] = []
        for tmdb_id, details in sorted(resolved.items()):
            normalized = TMDBClient.normalize_details(details)
            imdb_id = str(
                (normalized.get("external_ids") or {}).get("imdb_id") or ""
            ).strip().lower() or None
            countries = [
                str(item.get("name") or item.get("iso_3166_1") or "")
                for item in (normalized.get("production_countries") or [])
                if isinstance(item, dict) and (item.get("name") or item.get("iso_3166_1"))
            ]
            directors = [
                str(item.get("name") or "")
                for item in (normalized.get("directors") or [])
                if isinstance(item, dict) and item.get("name")
            ]
            cast = [
                {
                    "name": str(item.get("name") or ""),
                    "character": str(item.get("character") or ""),
                }
                for item in (normalized.get("cast") or [])[:8]
                if isinstance(item, dict)
            ]
            tmdb_summary = {
                "year": normalized.get("year"),
                "countries": countries,
                "directors": directors,
                "cast": cast,
                "rating": normalized.get("vote_average"),
                "rating_votes": normalized.get("vote_count"),
                "poster_count": len(normalized.get("posters") or []),
            }
            identities.append({
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
                "title": normalized.get("title"),
                "original_title": normalized.get("original_title"),
                "year": normalized.get("year"),
                "tmdb_data": tmdb_summary,
            })

        evidence["candidate_identities"] = identities
        selected: dict[str, Any] | None = None
        identity_disambiguation: dict[str, Any] | None = None
        secondary_search_noise = bool(
            ignored_secondary_imdb_errors or ignored_secondary_detail_errors
        )

        if identities:
            # Lens result order is relevance-ranked. TMDB verifies the first
            # identifier; lower-ranked Lens IDs do not veto a verified match.
            selected = identities[0]

        if selected is None:
            tmdb_id_lookup["status"] = (
                "no_verified_tmdb_candidates" if not identities
                else "ambiguous_multiple_tmdb_ids"
            )
            reason = (
                "No official candidate could be resolved and verified by TMDB"
                if not identities
                else "Multiple official movie candidates remain after exact Lens title/year disambiguation"
            )
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": reason,
                "candidate_ids": sorted(resolved),
                "candidate_comparisons": [
                    {
                        "tmdb_id": row["tmdb_id"],
                        "title": row["title"],
                        "imdb_id": row["imdb_id"],
                        "tmdb_data": row["tmdb_data"],
                    }
                    for row in identities
                ],
                "imdb_find_errors": ignored_secondary_imdb_errors,
                "details_errors": ignored_secondary_detail_errors,
            }, evidence

        selected_id = int(selected["tmdb_id"])
        selected_uses_imdb = bool(expected_imdb_ids_by_tmdb.get(selected_id))
        tmdb_id_lookup["status"] = (
            "resolved_after_lens_title_disambiguation"
            if identity_disambiguation
            else "resolved_via_imdb_fallback"
            if direct_tmdb_ids and selected_id not in direct_tmdb_ids and selected_uses_imdb
            else "resolved"
        )
        if identity_disambiguation:
            evidence["identity_disambiguation"] = identity_disambiguation
        selected_candidate = resolved[selected_id]
        identity_consistency = {
            "status": "not_applied_id_only_mode",
            "allowed": True,
            "reason": "Source-folder names are not used to search for or veto a Lens-derived movie ID",
            "folder_name_used_for_search": False,
            "folder_name_used_to_select_candidate": False,
            "folder_name_used_for_conflict_veto": False,
        }
        evidence["identity_consistency"] = identity_consistency

        has_direct_tmdb_id = selected_id in direct_tmdb_ids
        has_imdb_id = bool(expected_imdb_ids_by_tmdb.get(selected_id))
        if has_direct_tmdb_id:
            identity_source = "google_lens_official_tmdb_movie_id"
            identity_method = "lens_tmdb_movie_id_lookup"
            verification = "TMDB movie details ID equals the first official TMDB movie ID returned by Google Lens"
        elif has_imdb_id:
            identity_source = "google_lens_official_imdb_id_via_tmdb_find"
            identity_method = "lens_imdb_id_tmdb_find"
            verification = "First official IMDb title ID returned by Google Lens resolved through TMDB /find and verified by TMDB movie details"
        elif selected_id in wikidata_tmdb_ids:
            identity_source = "google_lens_wikidata_id_via_tmdb_find"
            identity_method = "lens_wikidata_id_tmdb_find"
            verification = "The explicit Wikidata entity ID returned by Google Lens resolved to one TMDB movie_result, and TMDB movie details verified that movie ID"
        else:
            identity_source = "google_lens_id_via_tmdb"
            identity_method = "lens_identifier_tmdb_lookup"
            verification = "Lens identifier resolved and verified through TMDB"

        evidence["resolved_identity"] = {
            "tmdb_id": selected_id,
            "imdb_id": selected["imdb_id"],
            "source": identity_source,
            "verification": verification,
            "official_tmdb_ids": list(direct_tmdb_ids),
            "official_imdb_ids": list(direct_imdb_ids),
            "wikidata_ids": list(observed_lens_wikidata_ids),
            "wikidata_tmdb_ids": list(wikidata_tmdb_ids),
            "wikidata_resolution": wikidata_resolution,
            "lens_title_tmdb_ids": list(lens_title_tmdb_ids),
            "google_title_search_tmdb_ids": list(title_search_tmdb_ids),
            "google_title_search_imdb_ids": list(title_search_imdb_ids),
            "title_disambiguation": identity_disambiguation,
            "folder_name_used": False,
            "image_filename_used": False,
            "identity_consistency": identity_consistency,
        }
        return selected_candidate, {
            "status": "ready",
            "confidence": 1.0,
            "identity_source": identity_source,
            "identity_method": identity_method,
            "tmdb_id": selected_id,
            "imdb_id": selected["imdb_id"],
            "folder_name_used_for_matching": False,
            "reason": (
                "TMDB movie details verified the official movie ID returned by Google Lens"
                if has_direct_tmdb_id
                else "TMDB /find resolved the IMDb title ID returned by Google Lens and movie details verified the same IMDb ID"
                if has_imdb_id
                else "TMDB /find resolved the Wikidata entity ID returned by Google Lens and TMDB movie details verified the returned movie record"
                if selected_id in wikidata_tmdb_ids
                else "TMDB movie details verified the Lens-derived movie identifier"
            ),
        }, evidence

    def _tmdb_suggestions_from_ocr(
        self,
        ocr_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Search TMDB from poster OCR text for reviewer suggestions only.

        These results are deliberately isolated from _find_movie and can never
        supply an identity, candidate ID, READY status, or catalog filename.
        """
        output: dict[str, Any] = {
            "provider": "tmdb_search_movie",
            "status": "not_run",
            "identity_evidence": False,
            "automatic_match_allowed": False,
            "query_count": 0,
            "queries": [],
        }
        if not isinstance(ocr_result, dict) or ocr_result.get("status") != "ok":
            output["status"] = str((ocr_result or {}).get("status") or "unavailable")
            return output

        hints = ocr_result.get("title_hints") or []
        queries: list[str] = []
        seen: set[str] = set()
        for hint in hints:
            if not isinstance(hint, dict):
                continue
            query = " ".join(str(hint.get("text") or "").split())
            key = query.casefold()
            if len(query) < 2 or key in seen:
                continue
            seen.add(key)
            queries.append(query)
            if len(queries) >= MAX_OCR_TMDB_QUERIES:
                break

        if not queries:
            output["status"] = "no_title_hints"
            return output

        search_rows: list[dict[str, Any]] = []
        failed_queries = 0
        for query in queries:
            hint = next(
                (
                    row for row in hints
                    if isinstance(row, dict)
                    and " ".join(str(row.get("text") or "").split()).casefold() == query.casefold()
                ),
                {},
            )
            try:
                payload = self.tmdb.search_movies(query)
                results = payload.get("results") or []
                compact_results: list[dict[str, Any]] = []
                for row in results:
                    if not isinstance(row, dict):
                        continue
                    raw_id = row.get("id")
                    if not str(raw_id or "").isdigit():
                        continue
                    release_date = str(row.get("release_date") or "")
                    compact_results.append({
                        "tmdb_id": int(raw_id),
                        "title": row.get("title"),
                        "original_title": row.get("original_title"),
                        "release_date": release_date or None,
                        "year": int(release_date[:4]) if release_date[:4].isdigit() else None,
                        "vote_average": row.get("vote_average"),
                        "poster_path": row.get("poster_path"),
                    })
                    if len(compact_results) >= MAX_OCR_TMDB_RESULTS_PER_QUERY:
                        break
                search_rows.append({
                    "query": query,
                    "ocr_confidence": hint.get("confidence"),
                    "results": compact_results,
                })
            except Exception as exc:
                failed_queries += 1
                search_rows.append({
                    "query": query,
                    "ocr_confidence": hint.get("confidence"),
                    "results": [],
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                })

        output.update({
            "status": (
                "suggestions_only"
                if any(row.get("results") for row in search_rows)
                else "error" if failed_queries == len(queries)
                else "no_candidates"
            ),
            "query_count": len(queries),
            "queries": search_rows,
        })
        return output

    def _wikipedia_tmdb_suggestions_from_lens(
        self,
        lens_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Use Lens titles -> Wikipedia lead/year -> TMDB search, for review only.

        This pathway deliberately cannot return a candidate identity or set READY.
        The folder name and local image filename are never accepted as inputs.
        """
        output: dict[str, Any] = {
            "provider": "wikipedia_mediawiki_api",
            "tmdb_provider": "tmdb_search_movie",
            "status": "not_run",
            "identity_evidence": False,
            "automatic_match_allowed": False,
            "title_hints": [],
            "wikipedia_query_count": 0,
            "wikipedia_article_count": 0,
            "tmdb_query_count": 0,
            "tmdb_result_count": 0,
            "exact_title_year_match_count": 0,
            "wikipedia": {},
            "wikidata": {
                "provider": "wikidata_action_api",
                "status": "not_run",
                "identity_evidence": False,
                "automatic_match_allowed": False,
                "candidate_count": 0,
                "candidates": [],
                "errors": [],
            },
            "queries": [],
            "errors": [],
        }

        if any(
            isinstance(row, dict) and row.get("error")
            for row in (lens_results or [])
        ):
            output["status"] = "skipped_lens_error"
            return output

        lens_client = getattr(self, "reverse_image", None)
        hint_extractor = getattr(lens_client, "title_hints_from_lens_results", None)
        if not callable(hint_extractor):
            output["status"] = "title_hints_unavailable"
            return output
        try:
            raw_hints, _lens_year_hint = hint_extractor(list(lens_results or []))
            title_hints: list[str] = []
            seen_hints: set[str] = set()
            for value in raw_hints or []:
                title = " ".join(str(value or "").split()).strip()
                key = _comparison_title_key(title)
                if not key or key in seen_hints:
                    continue
                seen_hints.add(key)
                title_hints.append(title[:180])
                if len(title_hints) >= MAX_WIKIPEDIA_TITLE_HINTS:
                    break
            output["title_hints"] = title_hints
        except Exception as exc:
            output["status"] = "title_hint_error"
            output["errors"].append({
                "stage": "lens_title_hints",
                "type": type(exc).__name__,
                "message": str(exc).replace("\n", " ")[:220],
            })
            return output

        if not title_hints:
            output["status"] = "no_title_hints"
            return output

        wikidata = getattr(self, "wikidata", None)
        if wikidata is not None:
            try:
                wikidata_result = wikidata.search_films(
                    title_hints,
                    max_queries=MAX_WIKIPEDIA_TITLE_HINTS,
                    results_per_query=4,
                    max_candidates=12,
                )
                if not isinstance(wikidata_result, dict):
                    raise ValueError("Wikidata client returned an invalid result")
                output["wikidata"] = wikidata_result
            except Exception as exc:
                output["wikidata"] = {
                    "provider": "wikidata_action_api",
                    "status": "error",
                    "identity_evidence": False,
                    "automatic_match_allowed": False,
                    "candidate_count": 0,
                    "candidates": [],
                    "errors": [{
                        "type": type(exc).__name__,
                        "message": str(exc).replace("\n", " ")[:220],
                    }],
                }

        wikipedia = getattr(self, "wikipedia", None)
        if wikipedia is None:
            output["status"] = "wikipedia_unavailable"
            return output

        try:
            wiki_result = wikipedia.search_articles(
                title_hints,
                max_queries=MAX_WIKIPEDIA_TITLE_HINTS,
                results_per_query=3,
                max_articles=MAX_WIKIPEDIA_ARTICLES,
            )
            if not isinstance(wiki_result, dict):
                raise ValueError("Wikipedia client returned an invalid result")
        except Exception as exc:
            output["status"] = "wikipedia_error"
            output["errors"].append({
                "stage": "wikipedia_search",
                "type": type(exc).__name__,
                "message": str(exc).replace("\n", " ")[:220],
            })
            return output

        raw_articles = wiki_result.get("articles") or []
        articles = [
            row for row in raw_articles
            if isinstance(row, dict) and row.get("is_movie_article")
        ][:MAX_WIKIPEDIA_ARTICLES]
        output["wikipedia_query_count"] = int(wiki_result.get("query_count") or 0)
        output["wikipedia_article_count"] = len(articles)
        output["wikipedia"] = {
            "provider": wiki_result.get("provider", "wikipedia_mediawiki_api"),
            "language": wiki_result.get("language", "en"),
            "status": wiki_result.get("status", "unknown"),
            "query_count": output["wikipedia_query_count"],
            "search_result_count": int(wiki_result.get("search_result_count") or 0),
            "articles": [
                {
                    "pageid": row.get("pageid"),
                    "title": row.get("title"),
                    "url": row.get("url"),
                    "year": row.get("year"),
                    "year_candidates": row.get("year_candidates") or [],
                    "is_movie_article": bool(row.get("is_movie_article")),
                    "matched_query": row.get("matched_query"),
                    "snippet": row.get("snippet"),
                    "extract": str(row.get("extract") or "")[:700],
                }
                for row in articles
            ],
            "errors": list(wiki_result.get("errors") or [])[:8],
        }

        if not articles:
            output["status"] = (
                "wikipedia_error"
                if wiki_result.get("status") == "error"
                else "no_movie_articles"
            )
            return output

        successful_tmdb_queries = 0
        failed_tmdb_queries = 0
        for article in articles:
            wiki_title = " ".join(str(article.get("title") or "").split()).strip()
            raw_year = article.get("year")
            year = int(raw_year) if str(raw_year or "").isdigit() else None
            query_row: dict[str, Any] = {
                "wikipedia_title": wiki_title,
                "wikipedia_url": article.get("url"),
                "wikipedia_year": year,
                "query": wiki_title,
                "year_filter": year,
                "status": "skipped_missing_unambiguous_wikipedia_year",
                "results": [],
                "exact_title_year_match_count": 0,
            }
            if not wiki_title or year is None or not 1870 <= year <= 2100:
                output["queries"].append(query_row)
                continue

            try:
                payload = self.tmdb.search_movies(wiki_title, year=year)
                successful_tmdb_queries += 1
                raw_rows = payload.get("results") or []
                compact_results: list[dict[str, Any]] = []
                for row in raw_rows:
                    if not isinstance(row, dict):
                        continue
                    raw_id = row.get("id")
                    if not str(raw_id or "").isdigit():
                        continue
                    release_date = str(row.get("release_date") or "")
                    tmdb_year = (
                        int(release_date[:4])
                        if release_date[:4].isdigit()
                        else None
                    )
                    comparison_key = _comparison_title_key(wiki_title)
                    title_match = comparison_key in {
                        _comparison_title_key(row.get("title")),
                        _comparison_title_key(row.get("original_title")),
                    }
                    year_match = tmdb_year == year
                    exact_match = bool(title_match and year_match)
                    compact_results.append({
                        "tmdb_id": int(raw_id),
                        "title": row.get("title"),
                        "original_title": row.get("original_title"),
                        "release_date": release_date or None,
                        "year": tmdb_year,
                        "title_match": title_match,
                        "year_match": year_match,
                        "exact_title_year_match": exact_match,
                        "vote_average": row.get("vote_average"),
                        "poster_path": row.get("poster_path"),
                    })
                    if exact_match:
                        query_row["exact_title_year_match_count"] += 1
                    if len(compact_results) >= MAX_WIKIPEDIA_TMDB_RESULTS_PER_ARTICLE:
                        break
                query_row["results"] = compact_results
                query_row["status"] = "suggestions_only" if compact_results else "no_candidates"
                output["tmdb_result_count"] += len(compact_results)
                output["exact_title_year_match_count"] += query_row["exact_title_year_match_count"]
            except Exception as exc:
                failed_tmdb_queries += 1
                query_row["status"] = "error"
                query_row["error"] = f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:220]}"
                output["errors"].append({
                    "stage": "tmdb_search",
                    "wikipedia_title": wiki_title,
                    "wikipedia_year": year,
                    "type": type(exc).__name__,
                    "message": str(exc).replace("\n", " ")[:220],
                })
            output["queries"].append(query_row)

        output["tmdb_query_count"] = successful_tmdb_queries + failed_tmdb_queries
        if output["tmdb_result_count"]:
            output["status"] = "suggestions_only"
        elif output["tmdb_query_count"] and failed_tmdb_queries == output["tmdb_query_count"]:
            output["status"] = "tmdb_error"
        elif any(row.get("status") == "skipped_missing_unambiguous_wikipedia_year" for row in output["queries"]):
            output["status"] = "no_reliable_wikipedia_year"
        else:
            output["status"] = "no_tmdb_candidates"
        return output

    def _record_for_folder(
        self,
        item: dict[str, Any],
        previous_output_path: str | None = None,
        refresh_evidence: bool = False,
    ) -> dict[str, Any]:
        folder = item["folder"]
        image_path = item["image_path"]
        image_url = self.github.raw_url(image_path)
        image_blob_sha = next(
            (
                str(row.get("sha") or "")
                for row in (item.get("poster_files") or [])
                if str(row.get("path") or "") == image_path
            ),
            "",
        )

        # Freeze the decision for an unchanged poster. A second batch must not
        # replace a previously accepted Lens identity with a different live result.
        # A new lookup is permitted only when --refresh-evidence is explicit.
        if previous_output_path and image_blob_sha and not refresh_evidence:
            previous_record = self.github.get_json_file(previous_output_path)
            previous_source = (previous_record or {}).get("source") or {}
            previous_status = str((previous_record or {}).get("status") or "")
            if (
                previous_record
                and previous_status in {"ready", "review", "duplicate"}
                and str(previous_source.get("image_blob_sha") or "") == image_blob_sha
            ):
                print(
                    f"[KafiaNet] CACHED {folder}: poster SHA unchanged; "
                    f"keeping saved {previous_status.upper()} decision. "
                    "Use --refresh-evidence to search again.",
                    flush=True,
                )
                return previous_record

        latest_reverse_image_results = self._reverse_image_results(image_url)
        latest_errors = [
            item for item in latest_reverse_image_results
            if isinstance(item, dict) and item.get("error")
        ]
        latest_query_status = (
            "error" if latest_errors
            else "ok" if latest_reverse_image_results
            else "no_matches"
        )
        lens_diagnostics = dict(
            (getattr(self.reverse_image, "last_lens_diagnostics", {}) or {})
            if self.reverse_image else {}
        )

        # A temporary empty/unresponsive Google Lens response must not erase useful
        # reverse-image evidence already stored for this exact poster.
        reverse_image_results = latest_reverse_image_results
        reverse_image_results_source = "live"
        if not latest_reverse_image_results or latest_errors:
            previous_record = (
                self.github.get_json_file(previous_output_path)
                if previous_output_path
                else None
            )
            previous_reverse_image = (
                (previous_record or {}).get("reverse_image")
                or (previous_record or {}).get("tineye")
                or {}
            )
            saved_query_diagnostics = (
                (previous_reverse_image.get("latest_query") or {}).get("diagnostics")
                or (previous_reverse_image.get("evidence") or {}).get("lens_diagnostics")
                or {}
            )
            # Reuse only exact_matches evidence. Newer records tag each row with
            # its source tab, so exact rows remain distinguishable even when the
            # aggregate query also attempted visual_matches and that tab failed.
            saved_results = previous_reverse_image.get("results") or []
            saved_lens_tab = str(saved_query_diagnostics.get("lens_tab") or "")
            if saved_lens_tab == "exact_matches":
                # Legacy single-tab records may predate per-row lens_tab tags.
                exact_saved_results = saved_results
            else:
                exact_saved_results = [
                    result for result in saved_results
                    if isinstance(result, dict)
                    and result.get("lens_tab") == "exact_matches"
                ]
            reusable_results = [
                result for result in exact_saved_results
                if isinstance(result, dict)
                and not result.get("error")
                and any(result.get(key) for key in ("title", "url", "source"))
            ]
            if reusable_results:
                reverse_image_results = reusable_results
                reverse_image_results_source = "previous_saved"

        # BrightDataGoogleLensClient.search_by_image_url resolves each live
        # Lens tab before returning it and merges those per-tab diagnostics.
        # Running the resolver again here would erase the real attempt counters
        # because unresolved tokens have already been removed from live rows.
        # Older saved evidence may predate that normalization, so resolve only
        # when falling back to a previously stored result set.
        resolver = getattr(self.reverse_image, "resolve_result_urls", None)
        if callable(resolver) and reverse_image_results_source == "previous_saved":
            reverse_image_results = resolver(reverse_image_results)
            resolver_diagnostics = (
                getattr(self.reverse_image, "last_lens_diagnostics", {}) or {}
            )
            for key in (
                "google_goto_candidates",
                "google_goto_resolved",
                "google_goto_failed",
                "google_goto_skipped",
                "google_goto_direct_urls_kept",
                "google_goto_http_statuses",
                "google_goto_methods",
                "google_goto_error_kinds",
                "google_goto_error_samples",
                "google_goto_location_paths",
                "google_goto_destination_paths",
                "raw_result_fields",
                "raw_result_field_types",
                "raw_link_paths",
            ):
                if key in resolver_diagnostics:
                    lens_diagnostics[key] = resolver_diagnostics[key]

        # OCR receives only the public poster image URL generated from the GitHub image path.
        # Folder names and local filenames are never passed to OCR or title search.
        local_ocr = getattr(self, "local_ocr", None)
        ocr_result: dict[str, Any] = {
            "provider": "rapidocr_onnxruntime_cpu",
            "source": "poster_image_pixels",
            "identity_evidence": False,
            "status": (
                "disabled_by_configuration"
                if local_ocr is None
                else "skipped_official_id_present"
            ),
            "text_lines": [],
            "title_hints": [],
        }
        has_lens_official_id = bool(
            extract_tmdb_movie_ids(reverse_image_results)
            or extract_imdb_ids(reverse_image_results)
        )
        if not has_lens_official_id and local_ocr is not None:
            try:
                ocr_result = local_ocr.extract_from_url(image_url)
                if not isinstance(ocr_result, dict):
                    ocr_result = {
                        "provider": "rapidocr_onnxruntime_cpu",
                        "source": "poster_image_pixels",
                        "identity_evidence": False,
                        "status": "invalid_result",
                        "text_lines": [],
                        "title_hints": [],
                    }
            except Exception as exc:
                ocr_result = {
                    "provider": "rapidocr_onnxruntime_cpu",
                    "source": "poster_image_pixels",
                    "identity_evidence": False,
                    "status": "error",
                    "text_lines": [],
                    "title_hints": [],
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                }

        candidate, match, reverse_image_evidence = self._find_movie(
            reverse_image_results, expected_folder=folder
        )
        review_research_enabled = bool(
            getattr(getattr(self, "settings", None), "review_research_enabled", False)
        )
        if candidate:
            wikipedia_tmdb_suggestions = {
                "provider": "wikipedia_mediawiki_api",
                "tmdb_provider": "tmdb_search_movie",
                "status": "skipped_official_identity_resolved",
                "identity_evidence": False,
                "automatic_match_allowed": False,
                "title_hints": [],
                "wikipedia_query_count": 0,
                "wikipedia_article_count": 0,
                "tmdb_query_count": 0,
                "tmdb_result_count": 0,
                "exact_title_year_match_count": 0,
                "wikipedia": {},
                "queries": [],
                "errors": [],
                "wikidata": {
                    "provider": "wikidata_action_api",
                    "status": "skipped_official_identity_resolved",
                    "identity_evidence": False,
                    "automatic_match_allowed": False,
                    "candidate_count": 0,
                    "candidates": [],
                    "errors": [],
                },
            }
        elif review_research_enabled:
            wikipedia_tmdb_suggestions = self._wikipedia_tmdb_suggestions_from_lens(
                reverse_image_results
            )
        else:
            wikipedia_tmdb_suggestions = {
                "provider": "wikipedia_mediawiki_api",
                "tmdb_provider": "tmdb_search_movie",
                "status": "disabled_by_configuration",
                "identity_evidence": False,
                "automatic_match_allowed": False,
                "title_hints": [],
                "wikipedia_query_count": 0,
                "wikipedia_article_count": 0,
                "tmdb_query_count": 0,
                "tmdb_result_count": 0,
                "exact_title_year_match_count": 0,
                "wikipedia": {},
                "queries": [],
                "errors": [],
                "wikidata": {
                    "provider": "wikidata_action_api",
                    "status": "disabled_by_configuration",
                    "identity_evidence": False,
                    "automatic_match_allowed": False,
                    "candidate_count": 0,
                    "candidates": [],
                    "errors": [],
                },
            }
        if not candidate and wikipedia_tmdb_suggestions.get("status") == "suggestions_only":
            match["wikipedia_tmdb_suggestion_count"] = wikipedia_tmdb_suggestions.get("tmdb_result_count", 0)
            match["wikipedia_tmdb_exact_title_year_matches"] = wikipedia_tmdb_suggestions.get(
                "exact_title_year_match_count", 0
            )
            match["review_note"] = (
                "Wikipedia title/year and TMDB text-search results are reviewer suggestions only; "
                "they do not prove poster identity or authorize READY status."
            )
        ocr_tmdb_suggestions = (
            self._tmdb_suggestions_from_ocr(ocr_result)
            if not candidate
            else {
                "provider": "tmdb_search_movie",
                "status": "skipped_official_identity_resolved",
                "identity_evidence": False,
                "automatic_match_allowed": False,
                "query_count": 0,
                "queries": [],
            }
        )
        # Recompute after _find_movie: its official title-search fallback may
        # append direct TMDB/IMDb URLs found from Lens-derived title hints.
        lens_diagnostics.update(tmdb_movie_url_diagnostics(reverse_image_results))
        title_search_diag = reverse_image_evidence.get("official_title_search") or {}
        if not isinstance(title_search_diag, dict):
            title_search_diag = {}
        lens_title_resolution = reverse_image_evidence.get("lens_title_resolution") or {}
        lens_diagnostics.update({
            "lens_tmdb_title_resolution_status": lens_title_resolution.get("status", "not_run"),
            "lens_tmdb_title_query_count": lens_title_resolution.get("query_count", 0),
            "lens_tmdb_title_hint": lens_title_resolution.get("title_hint"),
            "lens_tmdb_title_selected_id": lens_title_resolution.get("selected_tmdb_id"),
            "title_search_status": title_search_diag.get("status", "not_run"),
            "title_search_response_format": title_search_diag.get("response_format", "unknown"),
            "title_search_http_status": title_search_diag.get("http_status"),
            "title_search_payload_shape": title_search_diag.get("payload_shape"),
            "title_search_attempts": title_search_diag.get("attempts", 0),
            "title_search_rate_limit_retries": title_search_diag.get("rate_limit_retries", 0),
            "title_search_rate_limit_wait_seconds": title_search_diag.get("rate_limit_wait_seconds", 0),
            "title_search_results": title_search_diag.get("organic_result_count", 0),
            "title_search_official_urls": title_search_diag.get("official_url_count", 0),
            "title_search_query_count": title_search_diag.get("query_count", 0),
            "title_search_query_variants": list(title_search_diag.get("query_variants") or []),
            "title_search_hints": list(title_search_diag.get("title_hints") or [])[:5],
            "title_search_candidate_urls": title_search_diag.get("candidate_url_count", 0),
            "title_search_google_redirects": title_search_diag.get("google_redirect_count", 0),
            "title_search_unwrapped_redirects": title_search_diag.get("google_redirect_unwrapped_count", 0),
            "title_search_candidate_domains": dict(title_search_diag.get("candidate_domains") or {}),
        })
        lens_diagnostics.update({
            "wikipedia_status": wikipedia_tmdb_suggestions.get("status", "not_run"),
            "wikipedia_query_count": wikipedia_tmdb_suggestions.get("wikipedia_query_count", 0),
            "wikipedia_article_count": wikipedia_tmdb_suggestions.get("wikipedia_article_count", 0),
            "wikipedia_tmdb_query_count": wikipedia_tmdb_suggestions.get("tmdb_query_count", 0),
            "wikipedia_tmdb_result_count": wikipedia_tmdb_suggestions.get("tmdb_result_count", 0),
            "wikipedia_tmdb_exact_title_year_matches": wikipedia_tmdb_suggestions.get(
                "exact_title_year_match_count", 0
            ),
        })
        wikidata_suggestions = wikipedia_tmdb_suggestions.get("wikidata") or {
            "provider": "wikidata_action_api",
            "status": "not_run",
            "identity_evidence": False,
            "automatic_match_allowed": False,
            "candidate_count": 0,
            "candidates": [],
            "errors": [],
        }
        catalog_rows = [
            row for row in reverse_image_results
            if isinstance(row, dict)
            and row.get("catalog_source") in {"afi_catalog", "bfi_national_archive"}
            and row.get("url")
        ]
        def compact_catalog_rows(source_name: str) -> list[dict[str, Any]]:
            output_rows = []
            seen_urls = set()
            for row in catalog_rows:
                if row.get("catalog_source") != source_name:
                    continue
                url = str(row.get("url") or "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                output_rows.append({
                    "title": str(row.get("title") or "")[:240],
                    "url": url,
                    "snippet": str(row.get("content") or row.get("snippet") or "")[:500],
                    "search_query_variant": row.get("search_query_variant"),
                    "source": source_name,
                    "identity_evidence": False,
                    "automatic_match_allowed": False,
                })
            return output_rows[:8]
        catalog_sources = {
            "status": "found" if catalog_rows else "no_results",
            "identity_evidence": False,
            "automatic_match_allowed": False,
            "afi_catalog": compact_catalog_rows("afi_catalog"),
            "bfi_national_archive": compact_catalog_rows("bfi_national_archive"),
        }
        reverse_image_evidence["official_catalog_sources"] = catalog_sources
        reverse_image_evidence["wikidata"] = {
            "status": wikidata_suggestions.get("status"),
            "candidate_count": wikidata_suggestions.get("candidate_count", 0),
            "identity_evidence": False,
            "automatic_match_allowed": False,
        }
        lens_diagnostics.update({
            "wikidata_status": wikidata_suggestions.get("status", "not_run"),
            "wikidata_candidates": wikidata_suggestions.get("candidate_count", 0),
            "afi_catalog_results": len(catalog_sources.get("afi_catalog") or []),
            "bfi_archive_results": len(catalog_sources.get("bfi_national_archive") or []),
        })
        reverse_image_evidence["results_source"] = reverse_image_results_source
        reverse_image_evidence["lens_diagnostics"] = lens_diagnostics
        reverse_image_evidence["wikipedia_tmdb_suggestions"] = {
            "status": wikipedia_tmdb_suggestions.get("status"),
            "identity_evidence": False,
            "automatic_match_allowed": False,
            "exact_title_year_match_count": wikipedia_tmdb_suggestions.get(
                "exact_title_year_match_count", 0
            ),
        }

        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "processed_utc": datetime.now(timezone.utc).isoformat(),
            "source": {
                "repository": self.settings.posters_repo,
                "branch": self.settings.posters_branch,
                "folder": folder,
                "image_path": image_path,
                "image_url": image_url,
                "image_blob_sha": image_blob_sha or None,
            },
            "original": {
                "folder_name": folder,
                "folder_name_used_for_matching": False,
            },
            "match": match,
            "wikipedia": wikipedia_tmdb_suggestions,
            "wikidata": wikidata_suggestions,
            "catalog_sources": catalog_sources,
            "ocr": {
                **ocr_result,
                "tmdb_suggestions": ocr_tmdb_suggestions,
            },
            "reverse_image": {
                "enabled": self.reverse_image is not None,
                "provider": "google_lens_brightdata",
                "results": reverse_image_results,
                "results_source": reverse_image_results_source,
                "latest_query": {
                    "status": latest_query_status,
                    "errors": latest_errors,
                    "diagnostics": lens_diagnostics,
                },
                "evidence": reverse_image_evidence,
            },
        }

        if candidate:
            tmdb_id = int(candidate["id"])
            details = self._details_cache.get(tmdb_id)
            if details is None:
                details = self.tmdb.movie_details(tmdb_id)
                self._details_cache[tmdb_id] = details
            normalized = self.tmdb.normalize_details(details)
            # Use the title returned by the verified TMDB movie record as-is.
            # Do not infer or select a title from Lens text, countries or market counts.
            canonical_title = (
                str(normalized.get("title") or normalized.get("original_title") or "").strip()
                or f"TMDB Movie {tmdb_id}"
            )
            title_selection = {
                "title": canonical_title,
                "source": "tmdb_movie_details",
            }

            external_ids = normalized.setdefault("external_ids", {})
            imdb_id = (
                str(match.get("imdb_id") or external_ids.get("imdb_id") or "")
                .strip()
                .lower()
                or None
            )
            if imdb_id:
                external_ids["imdb_id"] = imdb_id

            canonical_year = normalized.get("year")
            canonical_folder_name = (
                f"{canonical_title} ({canonical_year})" if canonical_year else canonical_title
            )
            normalized["worldwide_title"] = canonical_title
            normalized["worldwide_title_selection"] = title_selection

            record["tmdb"] = normalized
            record["tmdb_id"] = tmdb_id
            record["imdb_id"] = imdb_id
            record["identity"] = {
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
                "source": match.get("identity_source"),
                "verification": match.get("identity_method"),
                "folder_name_used": False,
            }
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
            record["tmdb_id"] = None
            record["imdb_id"] = None
            record["identity"] = None
            record["correction"] = None
            record["status"] = "review"

        return record

    def run_batch(
        self,
        limit: int,
        apply: bool,
        *,
        plan_out: str | None = None,
        apply_plan_path: str | None = None,
        refresh_evidence: bool = False,
    ) -> dict[str, Any]:
        if plan_out and (apply or apply_plan_path):
            raise ValueError("--plan-out is DRY_RUN-only and cannot be combined with APPLY")
        if apply_plan_path and not apply:
            raise ValueError("--apply-plan requires --apply")
        if apply_plan_path and refresh_evidence:
            raise ValueError("--refresh-evidence cannot be combined with --apply-plan")
        self._stop_batch = False
        self._provider_stop_status = None
        self._provider_stop_reason = None
        if apply_plan_path:
            self.settings.validate_for_processing(apply=apply, require_research=False)
        else:
            # Keep the original one-argument call compatible with lightweight
            # settings doubles used by the repository's run_batch unit tests.
            self.settings.validate_for_processing(apply=apply)

        print("[KafiaNet] Scanning poster repository...", flush=True)
        folders = self.github.list_film_folders()
        snapshot_head_sha = getattr(self.github, "last_snapshot_head_sha", None)
        print(f"[KafiaNet] Found {len(folders)} source/review folders.", flush=True)
        index = self.github.get_json_file("metadata/index.json") or {"schema_version": 1, "items": []}
        old_items = list(index.get("items") or [])
        old_by_folder = {str(row.get("source_folder")): row for row in old_items}
        if snapshot_head_sha and self.github.current_head_sha() != snapshot_head_sha:
            raise RuntimeError(
                "Poster repository changed while the batch snapshot was being read; "
                "no files were processed or written. Restart from a fresh DRY_RUN."
            )

        plan_document: dict[str, Any] | None = None
        plan_records_by_folder: dict[str, dict[str, Any]] | None = None
        if apply_plan_path:
            plan_document = self._load_plan_file(apply_plan_path)
            if plan_document.get("source_repo") != self.settings.posters_repo:
                raise RuntimeError("Saved plan belongs to a different poster repository")
            if plan_document.get("branch") != self.settings.posters_branch:
                raise RuntimeError("Saved plan belongs to a different poster branch")
            if not snapshot_head_sha or plan_document.get("source_head_sha") != snapshot_head_sha:
                raise RuntimeError(
                    "Poster repository changed after this DRY_RUN plan was created; "
                    "refusing to apply stale image paths or metadata. Create a new DRY_RUN."
                )
            if _canonical_plan_json(plan_document.get("old_items") or []) != _canonical_plan_json(old_items):
                raise RuntimeError("Poster metadata index changed after DRY_RUN; refusing stale plan")
            current_by_folder = {str(row.get("folder")): row for row in folders}
            planned_items = plan_document["pending"]
            planned_records = plan_document["records"]
            if len(planned_items) != len(planned_records):
                raise RuntimeError("Saved plan is incomplete: item/record count mismatch")
            plan_records_by_folder = {}
            for planned_item, planned_record in zip(planned_items, planned_records):
                folder_name = str(planned_item.get("folder") or "")
                current_item = current_by_folder.get(folder_name)
                if not folder_name or current_item is None:
                    raise RuntimeError(f"Saved plan source folder is missing: {folder_name or '(blank)'}")
                if _canonical_plan_json(current_item) != _canonical_plan_json(planned_item):
                    raise RuntimeError(
                        f"Poster files changed after DRY_RUN for folder {folder_name}; refusing to apply"
                    )
                record_folder = str(((planned_record.get("source") or {}).get("folder")) or "")
                if record_folder != folder_name or folder_name in plan_records_by_folder:
                    raise RuntimeError("Saved plan has duplicate or mismatched film records")
                plan_records_by_folder[folder_name] = planned_record
            pending = list(planned_items)
            print(
                f"[KafiaNet] Loaded immutable plan {plan_document.get('plan_id')} "
                f"for {len(pending)} film(s); Lens, Google Search, and TMDB will NOT be called.",
                flush=True,
            )
        else:
            # Root folders need their first move. Unchanged REVIEW posters reuse
            # their last saved decision and do not spend research quota again.
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
                if plan_records_by_folder is not None:
                    # Reuse the exact saved DRY_RUN decision and evidence.
                    # This branch performs no Lens, Google Search, or TMDB requests.
                    record = plan_records_by_folder[item["folder"]]
                else:
                    if refresh_evidence:
                        record = self._record_for_folder(
                            item,
                            previous_output_path=previous_row.get("output_path"),
                            refresh_evidence=True,
                        )
                    else:
                        record = self._record_for_folder(
                            item,
                            previous_output_path=previous_row.get("output_path"),
                        )
            except Exception as exc:
                # Isolate failures to this film. Persist it as REVIEW so a later
                # batch can retry it; one invalid Lens backlink or API response
                # must not abort the remaining films in the batch.
                folder = item["folder"]
                image_path = item["image_path"]
                image_url = self.github.raw_url(image_path)
                image_blob_sha = next(
                    (
                        str(row.get("sha") or "")
                        for row in (item.get("poster_files") or [])
                        if str(row.get("path") or "") == image_path
                    ),
                    "",
                )
                previous_record = None
                if previous_row.get("output_path"):
                    try:
                        previous_record = self.github.get_json_file(previous_row["output_path"])
                    except Exception:
                        # Preserve the original processing error; Git history still
                        # retains the previously committed metadata if this read fails.
                        previous_record = None
                previous_reverse_image = (previous_record or {}).get("reverse_image") or (previous_record or {}).get("tineye") or {}
                previous_reverse_image_results = [
                    result for result in (previous_reverse_image.get("results") or [])
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
                        "image_blob_sha": image_blob_sha or None,
                    },
                    "original": {
                        "folder_name": folder,
                        "folder_name_used_for_matching": False,
                    },
                    "match": {
                        "status": "review",
                        "confidence": 0.0,
                        "reason": "Processing failed before a verified movie match; item is eligible for retry",
                    },
                    "reverse_image": {
                        "enabled": getattr(self, "reverse_image", None) is not None,
                "provider": "google_lens_brightdata",
                        "results": previous_reverse_image_results,
                        "results_source": "previous_saved" if previous_reverse_image_results else "none",
                        "latest_query": {
                            "status": "processing_error",
                            "errors": [{
                                "type": type(exc).__name__,
                                "message": str(exc).replace("\\n", " ")[:300],
                            }],
                        },
                        "evidence": previous_reverse_image.get("evidence") or {},
                    },
                    "tmdb": None,
                    "tmdb_id": None,
                    "imdb_id": None,
                    "identity": None,
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
            tmdb_id = record.get("tmdb_id")
            imdb_id = record.get("imdb_id")
            reverse_image_info = record.get("reverse_image") or {}
            latest_query_status = (reverse_image_info.get("latest_query") or {}).get("status", "unknown")
            reverse_image_source = reverse_image_info.get("results_source", "live")
            match_info = record.get("match") or {}
            reason = str(match_info.get("reason") or "unknown")
            reason = " ".join(reason.replace('"', "'").replace("\\r", " ").replace("\\n", " ").split())[:150]
            candidate_ids = match_info.get("candidate_ids") or []
            if not candidate_ids and match_info.get("candidate_id") is not None:
                candidate_ids = [match_info["candidate_id"]]
            candidate_text = ",".join(str(value) for value in candidate_ids[:4]) or "none"
            identity_evidence = reverse_image_info.get("evidence") or {}
            unresolved_ids = identity_evidence.get("id_resolution") or {}
            unresolved_tmdb = ",".join(
                str(value) for value in (unresolved_ids.get("unresolved_tmdb_ids") or [])[:10]
            ) or "none"
            lens_diag = (reverse_image_info.get("latest_query") or {}).get("diagnostics") or {}
            lens_error_items = (reverse_image_info.get("latest_query") or {}).get("errors") or []

            def safe_log_detail(value: Any, limit: int = 170) -> str:
                detail = " ".join(str(value or "").replace('"', "'").replace("\r", " ").replace("\n", " ").split())
                for secret in (
                    getattr(self.settings, "brightdata_api_key", ""),
                ):
                    secret = str(secret or "")
                    if secret:
                        detail = detail.replace(secret, "[REDACTED]")
                return detail[:limit] or "none"

            lens_error = "none"
            if lens_error_items:
                first_error = lens_error_items[0]
                lens_error = safe_log_detail(
                    f"{first_error.get('type', 'ProviderError')}: {first_error.get('message') or first_error.get('error')}"
                )
            response_modes = lens_diag.get("response_modes_attempted") or {}
            if isinstance(response_modes, dict):
                response_modes_text = ",".join(
                    f"{tab}:{'+'.join(str(mode) for mode in (modes or []))}"
                    for tab, modes in response_modes.items()
                ) or "none"
            elif isinstance(response_modes, (list, tuple)):
                response_modes_text = "+".join(str(mode) for mode in response_modes) or "none"
            else:
                response_modes_text = str(response_modes)
            response_headers = lens_diag.get("response_headers") or {}
            safe_header_keys = (
                "content-type",
                "content-length",
                "x-brd-error-code",
                "x-brd-error",
                "x-brd-err-code",
                "x-brd-err-msg",
                "x-response-id",
                "x-brd-rate-limit",
                "x-brd-rate-limit-period-ms",
            )
            response_headers_text = ",".join(
                f"{key}={response_headers[key]}"
                for key in safe_header_keys
                if isinstance(response_headers, dict) and response_headers.get(key)
            ) or "none"
            raw_paths_by_field = lens_diag.get("raw_link_paths_by_field") or {}
            raw_paths_by_field_text = ",".join(
                f"{field}:" + "|".join(
                    f"{path}={count}" for path, count in paths.items()
                )
                for field, paths in raw_paths_by_field.items()
                if isinstance(paths, dict)
            ) or "none"
            raw_field_types_text = ",".join(
                f"{field}:{value}"
                for field, value in (lens_diag.get("raw_result_field_types") or {}).items()
            ) or "none"
            tmdb_lookup = identity_evidence.get("tmdb_id_lookup") or {}
            structured_identifier_counts = lens_diag.get("structured_identifier_counts") or {}
            structured_identifier_text = ",".join(
                f"{name.removesuffix('_ids')}:{count}"
                for name, count in structured_identifier_counts.items()
            ) or "none"
            wikidata_resolution = identity_evidence.get("wikidata_resolution") or {}
            wikidata_resolution_text = (
                f"{wikidata_resolution.get('status', 'not_run')}:"
                f"{wikidata_resolution.get('selected_wikidata_id') or 'none'}:"
                f"{','.join(str(value) for value in (wikidata_resolution.get('tmdb_movie_ids') or [])[:5]) or 'none'}"
            )
            print(
                f"[KafiaNet] [{position}/{total}] DONE status={record['status']} "
                f"tmdb_id={tmdb_id if tmdb_id is not None else 'none'} "
                f"imdb_id={imdb_id if imdb_id is not None else 'none'} "
                f"reverse_image={latest_query_status} evidence={reverse_image_source} "
                f"lens_tab={lens_diag.get('lens_tab', 'unknown')} "
                f"lens_data_format={lens_diag.get('data_format', 'unknown')} "
                f"lens_modes='{safe_log_detail(response_modes_text, 150)}' "
                f"lens_headers='{safe_log_detail(response_headers_text, 420)}' "
                f"lens_http={lens_diag.get('http_status', 'none')} "
                f"lens_attempts={lens_diag.get('attempts', 'none')} "
                f"lens_results={lens_diag.get('result_count', 'none')} "






                f"title_search_status={lens_diag.get('title_search_status', 'not_run')} "
                f"title_search_format={lens_diag.get('title_search_response_format', 'unknown')} "
                f"title_search_http={lens_diag.get('title_search_http_status', 'none')} "
                f"title_search_shape='{safe_log_detail(str(lens_diag.get('title_search_payload_shape') or 'none'), 180)}' "
                f"title_search_attempts={lens_diag.get('title_search_attempts', 0)} "
                f"title_search_rate_limit_retries={lens_diag.get('title_search_rate_limit_retries', 0)} "
                f"title_search_rate_limit_wait_seconds={lens_diag.get('title_search_rate_limit_wait_seconds', 0)} "
                f"title_search_results={lens_diag.get('title_search_results', 0)} "
                f"title_search_official_urls={lens_diag.get('title_search_official_urls', 0)} "
                f"title_search_query_count={lens_diag.get('title_search_query_count', 0)} "
                f"title_search_query_variants='{','.join(lens_diag.get('title_search_query_variants') or []) or 'none'}' "
                f"title_search_hints='{safe_log_detail(' || '.join(lens_diag.get('title_search_hints') or []), 170)}' "
                f"wikipedia_status={lens_diag.get('wikipedia_status', 'not_run')} "
                f"wikipedia_articles={lens_diag.get('wikipedia_article_count', 0)} "
                f"wikipedia_tmdb_results={lens_diag.get('wikipedia_tmdb_result_count', 0)} "
                f"wikipedia_exact_title_year_matches={lens_diag.get('wikipedia_tmdb_exact_title_year_matches', 0)} "
                f"wikidata_status={lens_diag.get('wikidata_status', 'not_run')} "
                f"wikidata_candidates={lens_diag.get('wikidata_candidates', 0)} "
                f"afi_catalog_results={lens_diag.get('afi_catalog_results', 0)} "
                f"bfi_archive_results={lens_diag.get('bfi_archive_results', 0)} "
                f"title_search_candidate_urls={lens_diag.get('title_search_candidate_urls', 0)} "
                f"title_search_google_redirects={lens_diag.get('title_search_google_redirects', 0)} "
                f"title_search_unwrapped_redirects={lens_diag.get('title_search_unwrapped_redirects', 0)} "
                f"title_search_domains='{safe_log_detail(','.join(f'{host}:{count}' for host, count in (lens_diag.get('title_search_candidate_domains') or {}).items()), 220)}' "
                f"official_tmdb_urls={lens_diag.get('official_tmdb_url_count', 0)} "
                f"official_tmdb_ids={','.join(str(value) for value in (lens_diag.get('official_tmdb_ids') or [])[:10]) or 'none'} "
                f"official_imdb_ids={','.join(str(value) for value in (lens_diag.get('official_imdb_ids') or [])[:10]) or 'none'} "
                f"lens_wikidata_ids={','.join(lens_diag.get('lens_wikidata_ids') or []) or 'none'} "
                f"lens_structured_ids='{safe_log_detail(structured_identifier_text, 180)}' "
                f"wikidata_id_lookup='{safe_log_detail(wikidata_resolution_text, 180)}' "
                f"result_urls={lens_diag.get('result_url_count', 0)} "
                f"google_wrappers={lens_diag.get('google_wrapper_count', 0)} "
                f"google_goto={lens_diag.get('google_goto_candidates', 0)}/{lens_diag.get('google_goto_resolved', 0)}/{lens_diag.get('google_goto_failed', 0)}/{lens_diag.get('google_goto_skipped', 0)} "
                f"google_goto_direct_urls_kept={lens_diag.get('google_goto_direct_urls_kept', 0)} "
                f"google_goto_http='{safe_log_detail(','.join(f'{status}:{count}' for status, count in (lens_diag.get('google_goto_http_statuses') or {}).items()), 100)}' "
                f"google_goto_methods='{safe_log_detail(','.join(f'{method}:{count}' for method, count in (lens_diag.get('google_goto_methods') or {}).items()), 100)}' "
                f"google_goto_errors='{safe_log_detail(','.join(f'{kind}:{count}' for kind, count in (lens_diag.get('google_goto_error_kinds') or {}).items()), 140)}' "
                f"google_goto_locations='{safe_log_detail(','.join(f'{path}:{count}' for path, count in (lens_diag.get('google_goto_location_paths') or {}).items()), 220)}' "
                f"google_goto_destinations='{safe_log_detail(','.join(f'{path}:{count}' for path, count in (lens_diag.get('google_goto_destination_paths') or {}).items()), 220)}' "
                f"google_goto_samples='{safe_log_detail(' || '.join(lens_diag.get('google_goto_error_samples') or []), 260)}' "
                f"result_hosts='{safe_log_detail(','.join(f'{host}:{count}' for host, count in (lens_diag.get('result_hosts') or {}).items()), 220)}' "
                f"result_paths='{safe_log_detail(','.join(f'{path}:{count}' for path, count in (lens_diag.get('result_url_paths') or {}).items()), 260)}' "
                f"raw_fields='{safe_log_detail(','.join(lens_diag.get('raw_result_fields') or []), 240)}' "
                f"raw_field_types='{safe_log_detail(raw_field_types_text, 220)}' "
                f"raw_link_paths='{safe_log_detail(','.join(f'{path}:{count}' for path, count in (lens_diag.get('raw_link_paths') or {}).items()), 260)}' "
                f"raw_link_fields='{safe_log_detail(raw_paths_by_field_text, 400)}' "
                f"lens_error='{lens_error}' "
                f"tmdb_id_lookup={tmdb_lookup.get('status', 'unknown')} "
                f"tmdb_ids={','.join(str(value) for value in (tmdb_lookup.get('tmdb_ids') or [])[:10]) or 'none'} "
                f"tmdb_details_resolved={tmdb_lookup.get('details_resolved', 0)} "
                f'tmdb_details_errors={len(tmdb_lookup.get("errors") or [])} '
                f'reason="{reason}" candidates={candidate_text} '
                f"unresolved_tmdb_ids={unresolved_tmdb}",
                flush=True,
            )

            for candidate_report in (identity_evidence.get("candidate_identities") or [])[:10]:
                tmdb_data = candidate_report.get("tmdb_data") or {}
                cast_names = [
                    str(person.get("name") or "")
                    for person in (tmdb_data.get("cast") or [])[:5]
                ]
                print(
                    f"[KafiaNet] [{position}/{total}] TMDB_ID_RESULT "
                    f"id={candidate_report.get('tmdb_id')} "
                    f"title='{safe_log_detail(candidate_report.get('title'), 120)}' "
                    f"imdb_id={candidate_report.get('imdb_id') or 'none'} "
                    f"year={tmdb_data.get('year') or 'none'} "
                    f"countries='{safe_log_detail(', '.join(tmdb_data.get('countries') or []), 100)}' "
                    f"directors='{safe_log_detail(', '.join(tmdb_data.get('directors') or []), 100)}' "
                    f"cast='{safe_log_detail(', '.join(cast_names), 120)}' "
                    f"rating={tmdb_data.get('rating') if tmdb_data.get('rating') is not None else 'none'} "
                    f"posters={tmdb_data.get('poster_count', 0)}",
                    flush=True,
                )
            if self._stop_batch:
                print(
                    f"[KafiaNet] STOP: provider HTTP {self._provider_stop_status} "
                    f"({self._provider_stop_reason or 'provider_limit'}); "
                    "remaining posters were not queried and this batch will not write catalog changes.",
                    flush=True,
                )
                break

        if self._stop_batch:
            return {
                "mode": "STOPPED_PROVIDER_LIMIT",
                "source_repo": self.settings.posters_repo,
                "branch": self.settings.posters_branch,
                "source_folders_found": len(folders),
                "already_indexed_before_batch": len(old_items),
                "batch_requested": limit,
                "batch_selected": len(records),
                "ready": sum(1 for item in records if item.get("status") == "ready"),
                "review": sum(1 for item in records if item.get("status") == "review"),
                "duplicates_in_this_batch": sum(1 for item in records if item.get("status") == "duplicate"),
                "reverse_image_unresponsive": sum(
                    1 for item in records
                    if (item.get("reverse_image") or {}).get("latest_query", {}).get("status")
                    in {"error", "processing_error"}
                ),
                "reverse_image_no_matches": sum(
                    1 for item in records
                    if (item.get("reverse_image") or {}).get("latest_query", {}).get("status") == "no_matches"
                ),
                "reverse_image_previous_evidence_reused": sum(
                    1 for item in records
                    if (item.get("reverse_image") or {}).get("results_source") == "previous_saved"
                ),
                "reverse_image_official_id_backlinks": sum(
                    len(((item.get("reverse_image") or {}).get("evidence") or {}).get("imdb_ids", []))
                    + len(((item.get("reverse_image") or {}).get("evidence") or {}).get("tmdb_ids", []))
                    for item in records
                ),
                "reverse_image_resolved_identities": sum(
                    1 for item in records if item.get("identity")
                ),
                "reverse_image_identity_conflicts": sum(
                    1 for item in records
                    if "multiple distinct official IMDb/TMDB movie identities"
                    in str((item.get("match") or {}).get("reason") or "")
                ),
                "planned_output_files": [],
                "planned_image_moves": [],
                "planned_source_image_deletes": [],
                "written_files": 0,
                "moved_image_files": 0,
                "deleted_old_files": 0,
                "provider_stop_status": self._provider_stop_status,
                "provider_stop_reason": self._provider_stop_reason,
                "records": records,
                "next_pending_estimate": max(0, len(pending) - len(records)),
            }

        # Snapshot actual decisions before output-path/duplicate reconciliation
        # mutates the in-memory record objects below.
        plan_records_snapshot = json.loads(json.dumps(records, ensure_ascii=False))
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
                    "tmdb_id": record.get("tmdb_id"),
                    "imdb_id": record.get("imdb_id"),
                    "identity_source": (record.get("identity") or {}).get("source"),
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
            "reverse_image_unresponsive": sum(
                1 for record in records
                if (record.get("reverse_image") or {}).get("latest_query", {}).get("status")
                in {"error", "processing_error"}
            ),
            "reverse_image_no_matches": sum(
                1 for record in records
                if (record.get("reverse_image") or {}).get("latest_query", {}).get("status")
                == "no_matches"
            ),
            "reverse_image_previous_evidence_reused": sum(
                1 for record in records
                if (record.get("reverse_image") or {}).get("results_source") == "previous_saved"
            ),
            "reverse_image_official_id_backlinks": sum(
                len(((record.get("reverse_image") or {}).get("evidence") or {}).get("imdb_ids", []))
                + len(((record.get("reverse_image") or {}).get("evidence") or {}).get("tmdb_ids", []))
                for record in records
            ),
            "reverse_image_resolved_identities": sum(
                1 for record in records if record.get("identity")
            ),
            "reverse_image_identity_conflicts": sum(
                1 for record in records
                if "multiple distinct official IMDb/TMDB movie identities"
                in str((record.get("match") or {}).get("reason") or "")
            ),
            "planned_output_files": sorted(files),
            "planned_image_moves": sorted(moves),
            "planned_source_image_deletes": image_deletes,
            "records": records,
            "next_pending_estimate": next_pending_estimate,
        }

        if plan_out:
            if not snapshot_head_sha:
                raise RuntimeError("Cannot create a DRY_RUN plan without a verified GitHub snapshot SHA")
            if self.github.current_head_sha() != snapshot_head_sha:
                raise RuntimeError(
                    "Poster repository changed during DRY_RUN; no plan was saved. "
                    "Run DRY_RUN again against the current repository."
                )
            plan_document = {
                "plan_schema_version": PLAN_SCHEMA_VERSION,
                "plan_id": str(uuid.uuid4()),
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "source_repo": self.settings.posters_repo,
                "branch": self.settings.posters_branch,
                "source_head_sha": snapshot_head_sha,
                "limit": int(limit),
                "old_items": old_items,
                "pending": pending,
                "records": plan_records_snapshot,
            }
            plan_sha256 = self._write_plan_file(plan_out, plan_document)
            summary["plan_file"] = str(Path(plan_out).expanduser())
            summary["plan_id"] = plan_document["plan_id"]
            summary["plan_sha256"] = plan_sha256
            print(
                f"[KafiaNet] DRY_RUN plan saved: {summary['plan_file']} "
                f"(plan_id={summary['plan_id']}, sha256={plan_sha256})",
                flush=True,
            )

        if apply and (files or moves):
            print(
                f"[KafiaNet] Preparing GitHub commit with {len(files) + 1} JSON file(s) "
                f"and {len(moves)} image move(s)...",
                flush=True,
            )
            index_doc = {
                "schema_version": 2,
                "updated_utc": datetime.now(timezone.utc).isoformat(),
                "source_repository": self.settings.posters_repo,
                "items": sorted(projected, key=lambda row: str(row.get("source_folder", "")).casefold()),
            }
            files["metadata/index.json"] = json.dumps(index_doc, ensure_ascii=False, indent=2) + "\n"
            commit_arguments = {
                "deletes": deletes,
                "moves": moves,
                "message": f"KafiaNet metadata and poster organization: {len(records)} films",
            }
            if snapshot_head_sha:
                # Production commits are bound to the exact preflight snapshot.
                commit_arguments["expected_head_sha"] = snapshot_head_sha
            commit_sha = self.github.commit_files(files, **commit_arguments)
            summary["commit_sha"] = commit_sha
            summary["written_files"] = len(files)
            summary["moved_image_files"] = len(moves)
            summary["deleted_old_files"] = len(deletes)
            print(f"[KafiaNet] GitHub commit completed: {commit_sha}", flush=True)
            if apply_plan_path:
                Path(apply_plan_path).expanduser().unlink(missing_ok=True)
                summary["plan_consumed"] = True
                print("[KafiaNet] Applied plan removed after successful commit.", flush=True)

        return summary
