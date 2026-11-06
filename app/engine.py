from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.github_api import GitHubAPI
from app.image_evidence import extract_imdb_ids, extract_tmdb_movie_ids, tmdb_movie_url_diagnostics
from app.local_ocr import LocalPosterOCR
from app.google_lens import BrightDataGoogleLensClient, BrightDataQuotaError
from app.gemini_vision import GeminiPosterVision, MIN_CANDIDATE_CONFIDENCE
from app.tmdb import TMDBClient
from app.wikipedia import WikipediaClient


SCHEMA_VERSION = 3
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
    allowed = title_match and year_match
    return {
        "status": "passed" if allowed else "conflict",
        "allowed": allowed,
        "reason": (
            "TMDB title alias and release year are consistent with the source-folder label"
            if allowed
            else "Resolved TMDB identity conflicts with the source-folder title and/or year; automatic match blocked"
        ),
        "folder_year": folder_year,
        "candidate_year": candidate_year,
        "title_match": title_match,
        "year_match": year_match,
        "matching_title_aliases": matching_aliases[:5],
        "folder_name_used_for_search": False,
        "folder_name_used_to_select_candidate": False,
        "folder_name_used_for_conflict_veto": True,
    }


class MetadataEngine:
    def __init__(self, settings: Settings):
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
        self.tmdb = TMDBClient(
            settings.tmdb_bearer_token,
            settings.tmdb_language,
            settings.http_timeout_seconds,
        )
        self.wikipedia = WikipediaClient(timeout=settings.http_timeout_seconds)
        self.reverse_image = BrightDataGoogleLensClient(
            settings.brightdata_api_key,
            settings.brightdata_serp_zone,
            timeout=max(45, settings.http_timeout_seconds),
        )
        gemini_api_key = str(getattr(settings, "gemini_api_key", "") or "").strip()
        self.gemini_vision = (
            GeminiPosterVision(
                gemini_api_key,
                model=str(getattr(settings, "gemini_model", "gemini-3.8-flash") or "gemini-3.8-flash"),
                timeout=settings.http_timeout_seconds,
            )
            if gemini_api_key
            else None
        )
        self.local_ocr = (
            LocalPosterOCR(timeout=settings.http_timeout_seconds)
            if bool(getattr(settings, "local_ocr_enabled", False))
            else None
        )

    def close(self) -> None:
        self.github.close()
        self.tmdb.close()
        if getattr(self, "wikipedia", None):
            self.wikipedia.close()
        if self.reverse_image:
            self.reverse_image.close()
        if getattr(self, "gemini_vision", None):
            self.gemini_vision.close()
        if getattr(self, "local_ocr", None):
            self.local_ocr.close()

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


    @staticmethod
    def _lens_title_corroborates_vision_candidate(
        title: str,
        year: int,
        lens_results: list[dict[str, Any]],
        lens_client: Any,
    ) -> tuple[bool, int | None, list[str]]:
        """Check whether independent Lens result titles support a vision title."""
        hints: list[str] = []
        lens_year: int | None = None
        extractor = getattr(lens_client, "title_hints_from_lens_results", None)
        if callable(extractor):
            try:
                raw_hints, lens_year = extractor(list(lens_results or []))
                hints = [
                    " ".join(str(value or "").split()).strip()
                    for value in (raw_hints or [])
                    if str(value or "").strip()
                ][:8]
            except Exception:
                hints = []
                lens_year = None
        if not hints:
            hints = [
                " ".join(str(row.get("title") or "").split()).strip()
                for row in (lens_results or [])
                if isinstance(row, dict) and not row.get("error") and row.get("title")
            ][:8]

        wanted = [
            token for token in _identity_title_tokens(title)
            if token not in {"a", "an", "the"}
        ]
        if len(wanted) < 3:
            return False, lens_year, hints
        corroborated = False
        for hint in hints:
            available = _identity_title_tokens(hint)
            cursor = 0
            matched = True
            for token in wanted:
                try:
                    cursor = available.index(token, cursor) + 1
                except ValueError:
                    matched = False
                    break
            if matched:
                corroborated = True
                break
        year_ok = lens_year is None or int(lens_year) == int(year)
        return corroborated and year_ok, lens_year, hints

    def _vision_tmdb_candidates(
        self,
        vision_result: dict[str, Any] | None,
    ) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
        """Search TMDB from high-confidence poster-vision titles; still unverified."""
        output: dict[str, Any] = {
            "provider": "google_gemini_vision",
            "status": "not_run",
            "identity_evidence": False,
            "automatic_match_allowed": False,
            "minimum_confidence": MIN_CANDIDATE_CONFIDENCE,
            "queries": [],
            "candidate_tmdb_ids": [],
            "verified_tmdb_ids": [],
            "validation": [],
        }
        by_tmdb_id: dict[int, list[dict[str, Any]]] = {}
        if not isinstance(vision_result, dict):
            output["status"] = "unavailable"
            return by_tmdb_id, output
        if vision_result.get("status") != "ok" or vision_result.get("is_movie_poster") is not True:
            output["status"] = str(vision_result.get("status") or "no_confident_candidate")
            return by_tmdb_id, output

        searcher = getattr(self.tmdb, "search_movies", None)
        if not callable(searcher):
            output["status"] = "tmdb_search_unavailable"
            return by_tmdb_id, output

        candidates = [
            row for row in (vision_result.get("candidates") or [])
            if isinstance(row, dict)
            and str(row.get("title") or "").strip()
            and isinstance(row.get("year"), int)
            and 1870 <= row["year"] <= 2100
            and float(row.get("confidence") or 0) >= MIN_CANDIDATE_CONFIDENCE
        ][:3]
        if not candidates:
            output["status"] = "no_confident_candidate"
            return by_tmdb_id, output

        for candidate in candidates:
            title = " ".join(str(candidate.get("title") or "").split()).strip()
            year = int(candidate["year"])
            query_row: dict[str, Any] = {
                "title": title[:180],
                "year": year,
                "confidence": candidate.get("confidence"),
                "results": [],
            }
            try:
                payload = searcher(title, year)
                rows = payload.get("results") or []
                for row in rows[:8]:
                    if not isinstance(row, dict) or not str(row.get("id") or "").isdigit():
                        continue
                    release_date = str(row.get("release_date") or "")
                    result_year = int(release_date[:4]) if release_date[:4].isdigit() else None
                    title_keys = {
                        _comparison_title_key(row.get("title")),
                        _comparison_title_key(row.get("original_title")),
                    }
                    exact_title = _comparison_title_key(title) in title_keys
                    if not exact_title or result_year != year:
                        continue
                    tmdb_id = int(row["id"])
                    if tmdb_id <= 0:
                        continue
                    by_tmdb_id.setdefault(tmdb_id, []).append(dict(candidate))
                    query_row["results"].append({
                        "tmdb_id": tmdb_id,
                        "title": row.get("title"),
                        "original_title": row.get("original_title"),
                        "year": result_year,
                    })
                    if len(query_row["results"]) >= 5:
                        break
            except Exception as exc:
                query_row["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
            output["queries"].append(query_row)

        output["candidate_tmdb_ids"] = sorted(by_tmdb_id)
        output["status"] = "tmdb_candidates_found" if by_tmdb_id else "no_exact_tmdb_title_year_match"
        return by_tmdb_id, output

    def _validate_vision_candidate_details(
        self,
        details: dict[str, Any],
        vision_candidate: dict[str, Any],
        lens_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Require exact TMDB title/year and a matching director or Lens corroboration."""
        normalized = TMDBClient.normalize_details(details or {})
        wanted_title = _comparison_title_key(vision_candidate.get("title"))
        titles = [
            normalized.get("title"),
            normalized.get("original_title"),
            *[
                row.get("title")
                for row in (normalized.get("alternative_titles") or [])
                if isinstance(row, dict)
            ],
        ]
        matching_titles = [
            str(value) for value in titles
            if value and _comparison_title_key(value) == wanted_title
        ]
        title_match = bool(matching_titles)
        try:
            wanted_year = int(vision_candidate.get("year"))
            tmdb_year = int(normalized.get("year"))
        except (TypeError, ValueError):
            wanted_year = None
            tmdb_year = None
        year_match = wanted_year is not None and tmdb_year == wanted_year

        vision_director = " ".join(str(vision_candidate.get("director") or "").split()).strip()
        tmdb_directors = [
            str(row.get("name") or "").strip()
            for row in (normalized.get("directors") or [])
            if isinstance(row, dict) and row.get("name")
        ]
        director_match = (
            bool(vision_director)
            and _comparison_title_key(vision_director) in {
                _comparison_title_key(value) for value in tmdb_directors
            }
        )
        lens_corroborated, lens_year, lens_hints = (
            self._lens_title_corroborates_vision_candidate(
                str(vision_candidate.get("title") or ""),
                wanted_year or 0,
                lens_results,
                getattr(self, "reverse_image", None),
            )
            if not vision_director and wanted_year is not None
            else (False, None, [])
        )
        corroboration_passed = director_match if vision_director else lens_corroborated
        allowed = title_match and year_match and corroboration_passed
        return {
            "title_match": title_match,
            "year_match": year_match,
            "matching_tmdb_titles": matching_titles[:5],
            "vision_year": wanted_year,
            "tmdb_year": tmdb_year,
            "vision_director": vision_director or None,
            "tmdb_directors": tmdb_directors[:8],
            "director_match": director_match,
            "lens_title_corroborated": lens_corroborated,
            "lens_year": lens_year,
            "lens_title_hints": lens_hints[:5],
            "corroboration_method": "tmdb_director" if vision_director else "lens_title",
            "allowed": allowed,
            "reason": (
                "Gemini poster title/year matched TMDB and the director was confirmed"
                if allowed and vision_director
                else "Gemini poster title/year matched TMDB and Lens independently corroborated the title"
                if allowed
                else "Gemini candidate failed exact TMDB title/year/director or independent Lens corroboration"
            ),
        }

    def _find_movie(
        self,
        reverse_image_results: list[dict[str, Any]],
        expected_folder: str | None = None,
        vision_result: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        """Resolve Lens official IDs or strict Gemini poster-title candidates.

        Lens direct official IDs are preferred. When Lens has no official ID,
        high-confidence Gemini visual title/year candidates may be searched in
        TMDB; each must match TMDB's title/year and either its director credits
        or independent Lens title evidence. If Gemini has no strong candidate,
        the existing Lens-derived official-page search remains available.
        Folder names and image filenames never discover or select candidates.
        The folder label remains a negative-only conflict veto after resolution.
        Every selected TMDB ID is verified through official movie details.
        """
        reverse_image_failed = any(
            isinstance(item, dict) and item.get("error")
            for item in (reverse_image_results or [])
        )
        lens_tmdb_ids = extract_tmdb_movie_ids(reverse_image_results)
        lens_imdb_ids = extract_imdb_ids(reverse_image_results)
        lens_tmdb_ids_from_lens = list(lens_tmdb_ids)
        lens_imdb_ids_from_lens = list(lens_imdb_ids)
        title_search_tmdb_ids: list[int] = []
        title_search_imdb_ids: list[str] = []
        title_search_diagnostics: dict[str, Any] = {
            "status": (
                "skipped_lens_error" if reverse_image_failed
                else "skipped_direct_official_id_present"
                if (lens_tmdb_ids or lens_imdb_ids)
                else "not_run"
            ),
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
            "error": None,
        }

        def title_search_evidence() -> dict[str, Any]:
            return {
                "source": "google_lens_result_titles_and_direct_play_store_urls",
                "status": title_search_diagnostics.get("status", "not_run"),
                "query_count": title_search_diagnostics.get("query_count", 0),
                "query_variants": list(title_search_diagnostics.get("query_variants") or []),
                "title_hints": list(title_search_diagnostics.get("title_hints") or [])[:5],
                "year_hint": title_search_diagnostics.get("year_hint"),
                "response_format": title_search_diagnostics.get("response_format", "json"),
                "payload_shape": title_search_diagnostics.get("payload_shape"),
                "http_status": title_search_diagnostics.get("http_status"),
                "attempts": title_search_diagnostics.get("attempts", 0),
                "organic_result_count": title_search_diagnostics.get("organic_result_count", 0),
                "candidate_url_count": title_search_diagnostics.get("candidate_url_count", 0),
                "google_redirect_count": title_search_diagnostics.get("google_redirect_count", 0),
                "google_redirect_unwrapped_count": title_search_diagnostics.get("google_redirect_unwrapped_count", 0),
                "candidate_domains": dict(title_search_diagnostics.get("candidate_domains") or {}),
                "official_url_count": title_search_diagnostics.get("official_url_count", 0),
                "error": title_search_diagnostics.get("error"),
                "tmdb_ids": list(title_search_tmdb_ids),
                "imdb_ids": list(title_search_imdb_ids),
            }

        tmdb_id_lookup = {
            "status": "resolving" if (lens_tmdb_ids or lens_imdb_ids) else "no_tmdb_id",
            "tmdb_ids": list(lens_tmdb_ids),
            "direct_tmdb_ids": list(lens_tmdb_ids),
            "imdb_ids": list(lens_imdb_ids),
            "imdb_find_results": {},
            "imdb_find_errors": [],
            "details_resolved": 0,
            "errors": [],
            "folder_name_used": False,
            "image_filename_used": False,
        }
        evidence: dict[str, Any] = {
            "strategy": "official_tmdb_or_imdb_url_identity_resolution",
            "tmdb_ids": list(lens_tmdb_ids),
            "imdb_ids": list(lens_imdb_ids),
            "tmdb_id_lookup": tmdb_id_lookup,
            "id_resolution": {
                "strategy": "official_tmdb_or_imdb_url_then_tmdb_find",
                "tmdb_ids_seen": list(lens_tmdb_ids),
                "imdb_ids_seen": list(lens_imdb_ids),
                "imdb_to_tmdb_ids": {},
                "unresolved_tmdb_ids": [],
                "folder_name_used": False,
                "image_filename_used": False,
                "errors": [],
            },
            "candidate_identities": [],
            "official_title_search": title_search_evidence(),
        }

        vision_can_recover_lens_failure = (
            isinstance(vision_result, dict)
            and vision_result.get("status") == "ok"
            and vision_result.get("is_movie_poster") is True
            and bool(vision_result.get("candidates"))
        )
        if reverse_image_failed and not vision_can_recover_lens_failure:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "Google Lens image lookup or URL resolution failed and Gemini supplied no strong visual candidate",
                "candidate_ids": [],
            }, evidence

        vision_candidates_by_id: dict[int, list[dict[str, Any]]] = {}
        vision_resolution: dict[str, Any] = {
            "provider": "google_gemini_vision",
            "status": "skipped_official_identity_present" if (lens_tmdb_ids or lens_imdb_ids) else "not_run",
            "identity_evidence": False,
            "automatic_match_allowed": False,
            "queries": [],
            "candidate_tmdb_ids": [],
            "verified_tmdb_ids": [],
            "validation": [],
        }
        if not lens_tmdb_ids and not lens_imdb_ids and isinstance(vision_result, dict):
            vision_candidates_by_id, vision_resolution = self._vision_tmdb_candidates(vision_result)
            if vision_candidates_by_id:
                title_search_diagnostics["status"] = "skipped_vision_tmdb_candidates_found"

        # If vision finds a strong title candidate, search TMDB directly rather
        # than spending another Bright Data Google Search request. Otherwise,
        # retain the existing Lens-derived official-page search fallback.
        if not lens_tmdb_ids and not lens_imdb_ids and not vision_candidates_by_id and not self._stop_batch:
            title_searcher = getattr(
                getattr(self, "reverse_image", None),
                "search_official_movie_pages_from_lens",
                None,
            )
            if callable(title_searcher):
                title_search_rows: list[dict[str, Any]] = []
                try:
                    # Pass a snapshot: appending returned official rows must not mutate
                    # the input evidence object supplied to the search client.
                    raw_title_search_rows = title_searcher(list(reverse_image_results))
                    if isinstance(raw_title_search_rows, list):
                        title_search_rows = [
                            row for row in raw_title_search_rows if isinstance(row, dict)
                        ]
                    else:
                        title_search_rows = []
                    if title_search_rows and isinstance(reverse_image_results, list):
                        reverse_image_results.extend(title_search_rows)
                    raw_title_search_diagnostics = getattr(
                        getattr(self, "reverse_image", None),
                        "last_official_search_diagnostics",
                        {},
                    )
                    if isinstance(raw_title_search_diagnostics, dict):
                        title_search_diagnostics = dict(raw_title_search_diagnostics)
                except BrightDataQuotaError as exc:
                    self._stop_batch = True
                    self._provider_stop_status = exc.status_code
                    title_search_diagnostics.update({
                        "status": "quota_error",
                        "error": str(exc)[:300],
                    })
                except Exception as exc:
                    provider_diagnostics = getattr(
                        getattr(self, "reverse_image", None),
                        "last_official_search_diagnostics",
                        {},
                    )
                    if isinstance(provider_diagnostics, dict):
                        title_search_diagnostics = dict(provider_diagnostics)
                    title_search_diagnostics.update({
                        "status": title_search_diagnostics.get("status") or "error",
                        "error": title_search_diagnostics.get("error")
                        or f"{type(exc).__name__}: {str(exc)[:300]}",
                    })
                    self._stop_for_provider_server_error(provider_diagnostics)

                title_search_tmdb_ids = extract_tmdb_movie_ids(title_search_rows)
                title_search_imdb_ids = extract_imdb_ids(title_search_rows)
                lens_tmdb_ids = list(dict.fromkeys([*lens_tmdb_ids, *title_search_tmdb_ids]))
                lens_imdb_ids = list(dict.fromkeys([*lens_imdb_ids, *title_search_imdb_ids]))

        evidence["vision_resolution"] = vision_resolution
        evidence["strategy"] = (
            "google_lens_official_id"
            if (lens_tmdb_ids_from_lens or lens_imdb_ids_from_lens)
            else "gemini_vision_tmdb_title_year_lookup"
            if vision_candidates_by_id
            else "lens_derived_google_official_title_search"
        )
        evidence["official_title_search"] = title_search_evidence()
        evidence["id_resolution"]["strategy"] = (
            "lens_official_id_or_lens_derived_google_official_title_search_then_tmdb_find"
        )
        evidence["id_resolution"]["tmdb_ids_seen"] = list(lens_tmdb_ids)
        evidence["id_resolution"]["imdb_ids_seen"] = list(lens_imdb_ids)
        evidence["id_resolution"]["google_title_search_tmdb_ids"] = list(title_search_tmdb_ids)
        evidence["id_resolution"]["google_title_search_imdb_ids"] = list(title_search_imdb_ids)
        evidence["id_resolution"]["vision_tmdb_ids"] = sorted(vision_candidates_by_id)
        tmdb_id_lookup.update({
            "status": "resolving" if (lens_tmdb_ids or lens_imdb_ids or vision_candidates_by_id) else "no_tmdb_id",
            "tmdb_ids": list(lens_tmdb_ids),
            "direct_tmdb_ids": list(lens_tmdb_ids_from_lens),
            "imdb_ids": list(lens_imdb_ids),
            "google_title_search_tmdb_ids": list(title_search_tmdb_ids),
            "google_title_search_imdb_ids": list(title_search_imdb_ids),
            "vision_tmdb_ids": sorted(vision_candidates_by_id),
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

        if not lens_tmdb_ids and not lens_imdb_ids and not vision_candidates_by_id:
            reason = (
                "Neither Lens nor Gemini poster vision produced a TMDB/IMDb identity candidate"
            )
            if title_search_diagnostics.get("error"):
                reason = (
                    "Lens returned no official IDs and the Lens-derived Google title search failed: "
                    + str(title_search_diagnostics.get("error"))[:220]
                )
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": reason,
                "candidate_ids": [],
            }, evidence

        if self._stop_batch:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "Bright Data quota or request limit reached during official title search; batch stopped",
                "candidate_ids": [],
            }, evidence

        # IMDb IDs are mapped only through TMDB's official external-ID endpoint.
        # This is an identifier lookup, not a title search.
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

        # A failed official-ID lookup is not silently ignored, even if another
        # result happened to contain a TMDB URL.
        if imdb_find_errors:
            tmdb_id_lookup["status"] = "imdb_id_unresolved"
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "One or more official IMDb IDs could not be resolved to a TMDB movie through /find",
                "candidate_ids": sorted(set(lens_tmdb_ids).union(
                    value for ids in imdb_to_tmdb_ids.values() for value in ids
                )),
                "imdb_find_errors": imdb_find_errors,
            }, evidence

        candidate_tmdb_ids = list(dict.fromkeys([
            *lens_tmdb_ids,
            *(value for ids in imdb_to_tmdb_ids.values() for value in ids),
            *sorted(vision_candidates_by_id),
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
                    f"Google Lens official links resolved to {len(candidate_tmdb_ids)} different "
                    f"TMDB movie IDs; the safe lookup limit is {MAX_LENS_TMDB_ID_CANDIDATES}"
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
                if int(tmdb_id) in vision_candidates_by_id:
                    checks = [
                        self._validate_vision_candidate_details(details, candidate, reverse_image_results)
                        for candidate in vision_candidates_by_id[int(tmdb_id)]
                    ]
                    allowed_checks = [row for row in checks if row.get("allowed")]
                    vision_resolution["validation"].append({
                        "tmdb_id": int(tmdb_id),
                        "checks": checks[:3],
                        "allowed": bool(allowed_checks),
                    })
                    if not allowed_checks:
                        continue
                    vision_resolution["verified_tmdb_ids"].append(int(tmdb_id))
                resolved[int(tmdb_id)] = details
            except Exception as exc:
                details_errors.append({
                    "tmdb_id": str(tmdb_id),
                    "error": type(exc).__name__,
                    "message": str(exc)[:250],
                })

        vision_resolution["verified_tmdb_ids"] = sorted(set(vision_resolution["verified_tmdb_ids"]))
        if vision_candidates_by_id:
            vision_resolution["status"] = (
                "verified" if vision_resolution["verified_tmdb_ids"] else "tmdb_details_validation_failed"
            )
            vision_resolution["identity_evidence"] = bool(vision_resolution["verified_tmdb_ids"])
            vision_resolution["automatic_match_allowed"] = len(vision_resolution["verified_tmdb_ids"]) == 1
        evidence["vision_resolution"] = vision_resolution
        evidence["id_resolution"]["errors"].extend(details_errors)
        evidence["id_resolution"]["unresolved_tmdb_ids"] = [
            int(row["tmdb_id"]) for row in details_errors
        ]
        tmdb_id_lookup["details_resolved"] = len(resolved)
        tmdb_id_lookup["errors"] = details_errors
        tmdb_id_lookup["status"] = "error" if details_errors else "resolved"
        if vision_candidates_by_id and not vision_resolution.get("verified_tmdb_ids"):
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "Gemini visual candidate did not pass exact TMDB title/year and director-or-Lens corroboration checks",
                "candidate_ids": sorted(vision_candidates_by_id),
            }, evidence
        if details_errors:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "One or more TMDB movie IDs could not be verified through official movie details",
                "candidate_ids": sorted(candidate_tmdb_ids),
                "details_errors": details_errors,
            }, evidence

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

        # Multiple different official TMDB movie IDs remain a conflict. Never
        # select one using Lens captions, snippets, folder names, or filenames.
        if len(identities) != 1:
            tmdb_id_lookup["status"] = "ambiguous_multiple_tmdb_ids"
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": (
                    "Google Lens official TMDB/IMDb links resolved to multiple different "
                    "TMDB movie IDs; the image identity is ambiguous"
                ),
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
            }, evidence

        selected = identities[0]
        selected_id = int(selected["tmdb_id"])
        selected_candidate = resolved[selected_id]
        identity_consistency = _folder_identity_consistency(
            selected_candidate,
            expected_folder,
        )
        evidence["identity_consistency"] = identity_consistency
        if not identity_consistency.get("allowed", False):
            tmdb_id_lookup["status"] = "identity_context_conflict"
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": identity_consistency.get("reason") or "Candidate identity conflicts with source context",
                "candidate_ids": [selected_id],
                "identity_consistency": identity_consistency,
            }, evidence
        has_direct_tmdb_id = selected_id in lens_tmdb_ids_from_lens
        has_title_search_tmdb_id = selected_id in title_search_tmdb_ids
        has_imdb_id = bool(expected_imdb_ids_by_tmdb.get(selected_id))
        has_title_search_imdb_id = any(
            value in title_search_imdb_ids
            for value in expected_imdb_ids_by_tmdb.get(selected_id, [])
        )
        has_vision_tmdb_id = selected_id in vision_candidates_by_id
        if has_vision_tmdb_id:
            identity_source = "gemini_vision_poster_pixels_verified_by_tmdb"
            identity_method = "gemini_vision_exact_title_year_director_tmdb_details"
            verification = "poster_image_visual_identification_plus_exact_tmdb_title_year_and_director_or_lens_corroboration"
        elif has_direct_tmdb_id and has_imdb_id:
            identity_source = "google_lens_official_tmdb_and_imdb_ids"
            identity_method = "lens_tmdb_and_imdb_id_lookup"
            verification = "official_tmdb_url_plus_tmdb_find_imdb_id_and_movie_details"
        elif has_title_search_tmdb_id and has_title_search_imdb_id:
            identity_source = "google_serp_official_tmdb_and_imdb_ids"
            identity_method = "lens_title_search_tmdb_and_imdb_id_lookup"
            verification = "official_tmdb_and_imdb_urls_from_lens_derived_search_then_tmdb_find_and_matching_movie_details"
        elif has_title_search_imdb_id:
            identity_source = "google_serp_official_imdb_id_via_tmdb_find"
            identity_method = "lens_title_search_imdb_id_tmdb_find"
            verification = "official_imdb_url_from_lens_derived_search_then_tmdb_find_and_matching_movie_details"
        elif has_imdb_id:
            identity_source = "google_lens_official_imdb_id_via_tmdb_find"
            identity_method = "lens_imdb_id_tmdb_find"
            verification = "official_imdb_url_then_tmdb_find_and_matching_movie_details_external_id"
        elif has_direct_tmdb_id:
            identity_source = "google_lens_official_tmdb_movie_id"
            identity_method = "lens_tmdb_movie_id_lookup"
            verification = "tmdb_movie_details_id_equals_lens_movie_id"
        elif has_title_search_tmdb_id:
            identity_source = "google_serp_official_tmdb_movie_id"
            identity_method = "lens_title_search_tmdb_movie_id_lookup"
            verification = "official_tmdb_url_from_lens_derived_search_and_matching_tmdb_movie_details_id"
        else:
            identity_source = "google_official_tmdb_movie_id"
            identity_method = "official_tmdb_movie_id_lookup"
            verification = "official_tmdb_movie_details_id_verification"

        evidence["resolved_identity"] = {
            "tmdb_id": selected_id,
            "imdb_id": selected["imdb_id"],
            "source": identity_source,
            "verification": verification,
            "official_tmdb_ids": list(lens_tmdb_ids),
            "official_imdb_ids": list(lens_imdb_ids),
            "google_title_search_tmdb_ids": list(title_search_tmdb_ids),
            "google_title_search_imdb_ids": list(title_search_imdb_ids),
            "vision_tmdb_ids": sorted(vision_candidates_by_id),
            "vision_validation": vision_resolution.get("validation") or [],
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
                "Gemini identified the poster and TMDB movie details confirmed its exact title/year plus director or Lens title corroboration"
                if has_vision_tmdb_id
                else "TMDB movie details confirmed official TMDB and IMDb links returned by the Lens-derived Google search"
                if has_title_search_tmdb_id and has_title_search_imdb_id
                else "TMDB confirmed the movie ID from an official TMDB link"
                if has_direct_tmdb_id and not has_imdb_id
                else "TMDB /find mapped an official IMDb title ID to a movie and movie details confirmed the same IMDb ID"
                if has_imdb_id and not has_direct_tmdb_id
                else "TMDB movie details confirmed the official TMDB URL and the IMDb ID resolved through TMDB /find"
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
        item: dict[str, str],
        previous_output_path: str | None = None,
    ) -> dict[str, Any]:
        folder = item["folder"]
        image_path = item["image_path"]
        image_url = self.github.raw_url(image_path)

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
            # Evidence from the former visual_matches tab is deliberately stale:
            # only results explicitly tagged as exact_matches may be reused.
            saved_results = (
                previous_reverse_image.get("results") or []
                if saved_query_diagnostics.get("lens_tab") == "exact_matches"
                else []
            )
            reusable_results = [
                result for result in saved_results
                if isinstance(result, dict)
                and not result.get("error")
                and any(result.get(key) for key in ("title", "url", "source"))
            ]
            if reusable_results:
                reverse_image_results = reusable_results
                reverse_image_results_source = "previous_saved"

        # Google /goto links hide the real page behind an opaque token. Resolve
        # all Lens result URLs before extracting official IMDb/TMDB identifiers.
        resolver = getattr(self.reverse_image, "resolve_result_urls", None)
        if callable(resolver):
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

        vision_result: dict[str, Any] = {
            "provider": "google_gemini_vision",
            "source": "poster_image_pixels",
            "status": (
                "skipped_official_identity_present"
                if has_lens_official_id
                else "disabled_by_configuration"
                if getattr(self, "gemini_vision", None) is None
                else "not_run"
            ),
            "model": str(getattr(self.settings, "gemini_model", "gemini-3.8-flash") or "gemini-3.8-flash"),
            "identity_evidence": False,
            "automatic_match_allowed": False,
            "candidates": [],
            "poster_text": [],
        }
        gemini_vision = getattr(self, "gemini_vision", None)
        if not has_lens_official_id and gemini_vision is not None and not self._stop_batch:
            try:
                vision_result = gemini_vision.identify_poster(image_url)
                if not isinstance(vision_result, dict):
                    raise ValueError("Gemini vision client returned an invalid result")
            except Exception as exc:
                provider_status = getattr(exc, "status_code", None)
                if provider_status == 429 or (
                    isinstance(provider_status, int) and provider_status >= 500
                ):
                    self._stop_batch = True
                    self._provider_stop_status = provider_status
                    self._provider_stop_reason = (
                        "gemini_quota_or_throttle"
                        if provider_status == 429
                        else "gemini_provider_server_error"
                    )
                vision_result = {
                    "provider": "google_gemini_vision",
                    "source": "poster_image_pixels",
                    "status": "error",
                    "model": str(getattr(self.settings, "gemini_model", "gemini-3.8-flash") or "gemini-3.8-flash"),
                    "identity_evidence": False,
                    "automatic_match_allowed": False,
                    "candidates": [],
                    "poster_text": [],
                    "error": f"{type(exc).__name__}: {str(exc)[:220]}",
                }

        candidate, match, reverse_image_evidence = self._find_movie(
            reverse_image_results, expected_folder=folder, vision_result=vision_result
        )
        vision_resolution = reverse_image_evidence.get("vision_resolution") or {}
        vision_result["tmdb_resolution"] = vision_resolution
        vision_result["identity_evidence"] = bool(vision_resolution.get("identity_evidence"))
        vision_result["automatic_match_allowed"] = bool(
            vision_resolution.get("automatic_match_allowed")
            and str(match.get("identity_source") or "").startswith("gemini_vision_")
        )
        wikipedia_tmdb_suggestions = (
            self._wikipedia_tmdb_suggestions_from_lens(reverse_image_results)
            if not candidate
            else {
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
            }
        )
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
        lens_diagnostics.update({
            "title_search_status": title_search_diag.get("status", "not_run"),
            "title_search_response_format": title_search_diag.get("response_format", "unknown"),
            "title_search_http_status": title_search_diag.get("http_status"),
            "title_search_payload_shape": title_search_diag.get("payload_shape"),
            "title_search_attempts": title_search_diag.get("attempts", 0),
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
        lens_diagnostics.update({
            "vision_status": vision_result.get("status", "not_run"),
            "vision_model": vision_result.get("model", "unknown"),
            "vision_candidates": len(vision_result.get("candidates") or []),
            "vision_tmdb_candidate_ids": list(vision_resolution.get("candidate_tmdb_ids") or []),
            "vision_verified_tmdb_ids": list(vision_resolution.get("verified_tmdb_ids") or []),
            "vision_resolution_status": vision_resolution.get("status", "not_run"),
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
            },
            "original": {
                "folder_name": folder,
                "folder_name_used_for_matching": False,
            },
            "match": match,
            "wikipedia": wikipedia_tmdb_suggestions,
            "vision": vision_result,
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

    def run_batch(self, limit: int, apply: bool) -> dict[str, Any]:
        self._stop_batch = False
        self._provider_stop_status = None
        self._provider_stop_reason = None
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
                # batch can retry it; one invalid Lens backlink or API response
                # must not abort the remaining films in the batch.
                folder = item["folder"]
                image_path = item["image_path"]
                image_url = self.github.raw_url(image_path)
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
                        "enabled": self.reverse_image is not None,
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
                    getattr(self.settings, "gemini_api_key", ""),
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
            tmdb_lookup = identity_evidence.get("tmdb_id_lookup") or {}
            print(
                f"[KafiaNet] [{position}/{total}] DONE status={record['status']} "
                f"tmdb_id={tmdb_id if tmdb_id is not None else 'none'} "
                f"imdb_id={imdb_id if imdb_id is not None else 'none'} "
                f"reverse_image={latest_query_status} evidence={reverse_image_source} "
                f"lens_tab={lens_diag.get('lens_tab', 'unknown')} "
                f"lens_data_format={lens_diag.get('data_format', 'unknown')} "
                f"lens_http={lens_diag.get('http_status', 'none')} "
                f"lens_attempts={lens_diag.get('attempts', 'none')} "
                f"lens_results={lens_diag.get('result_count', 'none')} "
                f"vision_status={lens_diag.get('vision_status', 'not_run')} "
                f"vision_model={lens_diag.get('vision_model', 'unknown')} "
                f"vision_candidates={lens_diag.get('vision_candidates', 0)} "
                f"vision_tmdb_ids={','.join(str(value) for value in (lens_diag.get('vision_tmdb_candidate_ids') or [])[:5]) or 'none'} "
                f"vision_verified_ids={','.join(str(value) for value in (lens_diag.get('vision_verified_tmdb_ids') or [])[:5]) or 'none'} "
                f"vision_resolution={lens_diag.get('vision_resolution_status', 'not_run')} "
                f"title_search_status={lens_diag.get('title_search_status', 'not_run')} "
                f"title_search_format={lens_diag.get('title_search_response_format', 'unknown')} "
                f"title_search_http={lens_diag.get('title_search_http_status', 'none')} "
                f"title_search_shape='{safe_log_detail(str(lens_diag.get('title_search_payload_shape') or 'none'), 180)}' "
                f"title_search_attempts={lens_diag.get('title_search_attempts', 0)} "
                f"title_search_results={lens_diag.get('title_search_results', 0)} "
                f"title_search_official_urls={lens_diag.get('title_search_official_urls', 0)} "
                f"title_search_query_count={lens_diag.get('title_search_query_count', 0)} "
                f"title_search_query_variants='{','.join(lens_diag.get('title_search_query_variants') or []) or 'none'}' "
                f"title_search_hints='{safe_log_detail(' || '.join(lens_diag.get('title_search_hints') or []), 170)}' "
                f"wikipedia_status={lens_diag.get('wikipedia_status', 'not_run')} "
                f"wikipedia_articles={lens_diag.get('wikipedia_article_count', 0)} "
                f"wikipedia_tmdb_results={lens_diag.get('wikipedia_tmdb_result_count', 0)} "
                f"wikipedia_exact_title_year_matches={lens_diag.get('wikipedia_tmdb_exact_title_year_matches', 0)} "
                f"title_search_candidate_urls={lens_diag.get('title_search_candidate_urls', 0)} "
                f"title_search_google_redirects={lens_diag.get('title_search_google_redirects', 0)} "
                f"title_search_unwrapped_redirects={lens_diag.get('title_search_unwrapped_redirects', 0)} "
                f"title_search_domains='{safe_log_detail(','.join(f'{host}:{count}' for host, count in (lens_diag.get('title_search_candidate_domains') or {}).items()), 220)}' "
                f"official_tmdb_urls={lens_diag.get('official_tmdb_url_count', 0)} "
                f"official_tmdb_ids={','.join(str(value) for value in (lens_diag.get('official_tmdb_ids') or [])[:10]) or 'none'} "
                f"official_imdb_ids={','.join(str(value) for value in (lens_diag.get('official_imdb_ids') or [])[:10]) or 'none'} "
                f"result_urls={lens_diag.get('result_url_count', 0)} "
                f"google_wrappers={lens_diag.get('google_wrapper_count', 0)} "
                f"google_goto={lens_diag.get('google_goto_candidates', 0)}/{lens_diag.get('google_goto_resolved', 0)}/{lens_diag.get('google_goto_failed', 0)}/{lens_diag.get('google_goto_skipped', 0)} "
                f"google_goto_direct_urls_kept={lens_diag.get('google_goto_direct_urls_kept', 0)} "
                f"google_goto_http='{safe_log_detail(','.join(f'{status}:{count}' for status, count in (lens_diag.get('google_goto_http_statuses') or {}).items()), 100)}' "
                f"google_goto_methods='{safe_log_detail(','.join(f'{method}:{count}' for method, count in (lens_diag.get('google_goto_methods') or {}).items()), 100)}' "
                f"google_goto_errors='{safe_log_detail(','.join(f'{kind}:{count}' for kind, count in (lens_diag.get('google_goto_error_kinds') or {}).items()), 140)}' "
                f"google_goto_samples='{safe_log_detail(' || '.join(lens_diag.get('google_goto_error_samples') or []), 260)}' "
                f"result_hosts='{safe_log_detail(','.join(f'{host}:{count}' for host, count in (lens_diag.get('result_hosts') or {}).items()), 220)}' "
                f"result_paths='{safe_log_detail(','.join(f'{path}:{count}' for path, count in (lens_diag.get('result_url_paths') or {}).items()), 260)}' "
                f"raw_fields='{safe_log_detail(','.join(lens_diag.get('raw_result_fields') or []), 240)}' "
                f"raw_field_types='{safe_log_detail(','.join(f'{name}:{types}' for name, types in (lens_diag.get('raw_result_field_types') or {}).items()), 200)}' "
                f"raw_link_paths='{safe_log_detail(','.join(f'{path}:{count}' for path, count in (lens_diag.get('raw_link_paths') or {}).items()), 260)}' "
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
