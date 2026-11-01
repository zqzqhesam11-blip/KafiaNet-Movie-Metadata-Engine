from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.github_api import GitHubAPI
from app.image_evidence import extract_tmdb_movie_ids
from app.google_lens import BrightDataGoogleLensClient, BrightDataQuotaError
from app.matching import normalize_title, title_similarity
from app.tmdb import TMDBClient, WORLDWIDE_TITLE_POLICY_VERSION


SCHEMA_VERSION = 3
MAX_OFFICIAL_ID_LOOKUPS = 80


def safe_segment(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return (value or "untitled")[:140]


def _unique_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = " ".join(str(value or "").split()).strip()
        key = normalize_title(item)
        if item and key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _country_tokens(value: str) -> list[str]:
    return [
        part.strip(" .()")
        for part in re.split(r"[,;/|&]|\band\b", str(value or ""), flags=re.IGNORECASE)
        if part.strip(" .()")
    ]


def _phrase_in_lens_text(phrase: str, text: str) -> bool:
    """True only when a normalized TMDB fact is actually written in Lens evidence."""
    key = normalize_title(phrase)
    haystack = normalize_title(text)
    if not key or not haystack:
        return False
    return f" {key} " in f" {haystack} "


def _person_match(hint: str, candidates: list[str]) -> bool:
    hint_key = normalize_title(hint)
    if not hint_key:
        return False
    for candidate in candidates:
        candidate_key = normalize_title(candidate)
        if not candidate_key:
            continue
        if hint_key == candidate_key:
            return True
        if min(len(hint_key), len(candidate_key)) >= 6 and title_similarity(hint, candidate) >= 0.92:
            return True
    return False


def _candidate_metadata_profile(normalized: dict[str, Any]) -> dict[str, Any]:
    country_values: list[str] = []
    for item in normalized.get("production_countries") or []:
        if isinstance(item, dict):
            country_values.extend([
                str(item.get("name") or ""),
                str(item.get("iso_3166_1") or ""),
            ])
    for item in normalized.get("origin_country") or []:
        if isinstance(item, str):
            country_values.append(item)
        elif isinstance(item, dict):
            country_values.extend([str(item.get("name") or ""), str(item.get("iso_3166_1") or "")])

    directors = [
        str(person.get("name") or "")
        for person in (normalized.get("directors") or [])
        if isinstance(person, dict) and person.get("name")
    ]
    cast = [
        {
            "name": str(person.get("name") or ""),
            "character": str(person.get("character") or ""),
        }
        for person in (normalized.get("cast") or [])[:12]
        if isinstance(person, dict) and (person.get("name") or person.get("character"))
    ]
    return {
        "year": normalized.get("year"),
        "countries": _unique_text(country_values),
        "directors": _unique_text(directors),
        "cast": cast,
        "rating": normalized.get("vote_average"),
        "rating_votes": normalized.get("vote_count"),
    }


def _compare_lens_metadata(
    normalized: dict[str, Any],
    observations: list[dict[str, Any]],
    associated_lens_titles: list[str],
) -> dict[str, Any]:
    """Compare explicit facts in Lens result text with a candidate's TMDB facts.

    Unknown fields are neutral. A TMDB value is never treated as Lens evidence
    by itself; a clue must first be present in a Lens result title/snippet.
    """
    allowed_titles = {
        normalize_title(value) for value in associated_lens_titles if normalize_title(value)
    }
    relevant = [
        item for item in observations
        if isinstance(item, dict)
        and (
            normalize_title(item.get("lens_title") or "") in allowed_titles
            or any(
                _phrase_in_lens_text(query, str(item.get("lens_title") or ""))
                for query in allowed_titles
            )
        )
    ]
    years = list(dict.fromkeys(
        int(value)
        for item in relevant for value in (item.get("years") or [])
        if str(value).isdigit()
    ))
    countries = _unique_text([
        value for item in relevant for value in (item.get("countries") or [])
    ])
    directors = _unique_text([
        value for item in relevant for value in (item.get("directors") or [])
    ])
    cast_hints = _unique_text([
        value for item in relevant for value in (item.get("cast") or [])
    ])
    character_hints = _unique_text([
        value for item in relevant for value in (item.get("characters") or [])
    ])

    # Lens snippets are often prose, not structured "Director:"/"Cast:" fields.
    # Accept a TMDB candidate fact as Lens evidence only when that exact name
    # (or country name) occurs in the corresponding Lens result title/snippet.
    lens_texts = [
        " ".join((str(item.get("result_title") or ""), str(item.get("content") or "")))
        for item in relevant
    ]
    ratings: list[dict[str, Any]] = []
    for item in relevant:
        for rating in item.get("ratings") or []:
            if isinstance(rating, dict):
                try:
                    value = float(rating.get("value"))
                except (TypeError, ValueError):
                    continue
                ratings.append({
                    "source": str(rating.get("source") or "UNSPECIFIED").upper(),
                    "value": value,
                })

    profile = _candidate_metadata_profile(normalized)
    candidate_year = profile.get("year")
    candidate_countries = profile["countries"]
    candidate_directors = profile["directors"]
    candidate_cast = [item["name"] for item in profile["cast"] if item.get("name")]
    candidate_characters = [item["character"] for item in profile["cast"] if item.get("character")]

    snippet_countries = [
        value for value in candidate_countries
        if len(normalize_title(value)) >= 4
        and not re.fullmatch(r"[A-Z]{2}", value.strip(), flags=re.IGNORECASE)
        and any(_phrase_in_lens_text(value, text) for text in lens_texts)
    ]
    snippet_directors = [
        value for value in candidate_directors
        if any(_phrase_in_lens_text(value, text) for text in lens_texts)
    ]
    snippet_cast = [
        value for value in candidate_cast
        if any(_phrase_in_lens_text(value, text) for text in lens_texts)
    ]
    snippet_characters = [
        value for value in candidate_characters
        if any(_phrase_in_lens_text(value, text) for text in lens_texts)
    ]
    countries = _unique_text([*countries, *snippet_countries])
    directors = _unique_text([*directors, *snippet_directors])
    cast_hints = _unique_text([*cast_hints, *snippet_cast])
    character_hints = _unique_text([*character_hints, *snippet_characters])

    matches: dict[str, Any] = {}
    conflicts: dict[str, Any] = {}
    matched_fields: list[str] = []

    def record_field(name: str, clue_values: list[Any], candidate_values: list[Any], matched: bool) -> None:
        if not clue_values:
            return
        details = {
            "lens_values": clue_values[:12],
            "tmdb_values": candidate_values[:12],
            "matched": bool(matched),
        }
        if matched:
            matches[name] = details
            matched_fields.append(name)
        elif candidate_values:
            conflicts[name] = details

    if years and candidate_year is not None:
        record_field("year", years, [candidate_year], any(year == int(candidate_year) for year in years))

    country_parts = _unique_text([
        part for country in countries for part in _country_tokens(country)
    ])
    country_match = any(
        normalize_title(part) == normalize_title(candidate)
        for part in country_parts for candidate in candidate_countries
    )
    record_field("country", country_parts, candidate_countries, country_match)

    director_match = any(
        _person_match(hint, candidate_directors)
        for hint in directors
    )
    record_field("director", directors, candidate_directors, director_match)

    cast_match = any(
        _person_match(hint, candidate_cast)
        for hint in cast_hints
    )
    record_field("cast", cast_hints, candidate_cast, cast_match)

    character_match = any(
        _person_match(hint, candidate_characters)
        for hint in character_hints
    )
    record_field("main_character", character_hints, candidate_characters, character_match)

    rating = profile.get("rating")
    rating_match = False
    rating_details: dict[str, Any] | None = None
    if ratings and rating is not None:
        try:
            candidate_rating = float(rating)
        except (TypeError, ValueError):
            candidate_rating = None
        if candidate_rating is not None:
            deltas = [
                {
                    **item,
                    "delta": round(abs(float(item["value"]) - candidate_rating), 2),
                }
                for item in ratings
            ]
            best = min(deltas, key=lambda item: item["delta"])
            tolerance = 1.25 if best["source"] == "IMDB" else 0.9
            rating_match = best["delta"] <= tolerance
            rating_details = {
                "lens_values": ratings[:8],
                "tmdb_value": candidate_rating,
                "delta": best["delta"],
                "matched": rating_match,
                "note": "supporting signal only; ratings vary by provider and time",
            }
    if rating_details:
        (matches if rating_match else conflicts)["rating"] = rating_details

    # Count how those matching facts are distributed across the actual Lens
    # result pages. This prevents combining unrelated snippets that happened to
    # share a Lens title into one artificial "fingerprint".
    observation_support: list[dict[str, Any]] = []
    seen_observation_sources: set[str] = set()
    best_single_observation_match_count = 0
    for index, item in enumerate(relevant):
        item_years = [
            int(value) for value in (item.get("years") or []) if str(value).isdigit()
        ]
        item_countries = _unique_text([
            part for value in (item.get("countries") or [])
            for part in _country_tokens(str(value))
        ])
        item_directors = [str(value) for value in (item.get("directors") or [])]
        item_cast = [str(value) for value in (item.get("cast") or [])]
        item_characters = [str(value) for value in (item.get("characters") or [])]
        item_text = " ".join((
            str(item.get("result_title") or ""),
            str(item.get("content") or ""),
        ))
        item_countries = _unique_text([
            *item_countries,
            *[
                value for value in candidate_countries
                if len(normalize_title(value)) >= 4
                and not re.fullmatch(r"[A-Z]{2}", value.strip(), flags=re.IGNORECASE)
                and _phrase_in_lens_text(value, item_text)
            ],
        ])
        item_directors = _unique_text([
            *item_directors,
            *[value for value in candidate_directors if _phrase_in_lens_text(value, item_text)],
        ])
        item_cast = _unique_text([
            *item_cast,
            *[value for value in candidate_cast if _phrase_in_lens_text(value, item_text)],
        ])
        item_characters = _unique_text([
            *item_characters,
            *[value for value in candidate_characters if _phrase_in_lens_text(value, item_text)],
        ])
        item_fields: list[str] = []
        if (
            years and candidate_year is not None
            and any(year == int(candidate_year) for year in item_years)
        ):
            item_fields.append("year")
        if item_countries and any(
            normalize_title(value) == normalize_title(candidate)
            for value in item_countries for candidate in candidate_countries
        ):
            item_fields.append("country")
        if any(_person_match(hint, candidate_directors) for hint in item_directors):
            item_fields.append("director")
        if any(_person_match(hint, candidate_cast) for hint in item_cast):
            item_fields.append("cast")
        if any(_person_match(hint, candidate_characters) for hint in item_characters):
            item_fields.append("main_character")
        if not item_fields:
            continue
        source_url = str(item.get("result_url") or "").strip()
        source_key = source_url.casefold() or normalize_title(item.get("result_title") or "") or f"row-{index}"
        if source_key in seen_observation_sources:
            continue
        seen_observation_sources.add(source_key)
        best_single_observation_match_count = max(
            best_single_observation_match_count, len(item_fields)
        )
        observation_support.append({
            "result_title": str(item.get("result_title") or "")[:160],
            "result_url": source_url[:350],
            "matched_fields": item_fields,
        })

    # Rating is deliberately not a decisive identity field. At least two
    # independent hard metadata dimensions must agree for disambiguation.
    strong_matches = [name for name in matched_fields if name in {
        "year", "country", "director", "cast", "main_character"
    }]
    score_weights = {
        "year": 2.0, "country": 1.5, "director": 2.0,
        "cast": 1.5, "main_character": 2.0, "rating": 0.25,
    }
    score = sum(score_weights.get(name, 0.0) for name in matches)
    return {
        "lens_titles_used": _unique_text(associated_lens_titles),
        "lens_observations_used": len(relevant),
        "observed_clues": {
            "years": years,
            "countries": countries,
            "directors": directors,
            "cast": cast_hints,
            "main_characters": character_hints,
            "ratings": ratings,
        },
        "matches": matches,
        "conflicts": conflicts,
        "matched_fields": matched_fields,
        "strong_match_count": len(strong_matches),
        "supporting_observation_count": len(observation_support),
        "best_single_observation_match_count": best_single_observation_match_count,
        "supporting_observations": observation_support[:8],
        "evidence_qualified": bool(
            len(strong_matches) >= 2
            and (best_single_observation_match_count >= 2 or len(observation_support) >= 2)
        ),
        "score": round(score, 3),
        "rating_is_decisive": False,
    }


class MetadataEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._stop_batch = False
        self._provider_stop_status: int | None = None
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
        self.reverse_image = BrightDataGoogleLensClient(
            settings.brightdata_api_key,
            settings.brightdata_serp_zone,
            timeout=max(45, settings.http_timeout_seconds),
        )

    def close(self) -> None:
        self.github.close()
        self.tmdb.close()
        if self.reverse_image:
            self.reverse_image.close()

    def _reverse_image_results(self, image_url: str) -> list[dict[str, Any]]:
        if not self.reverse_image:
            return []
        try:
            return self.reverse_image.search_by_image_url(image_url)
        except BrightDataQuotaError as exc:
            # Free quota exhaustion and provider throttling stop the whole batch.
            self._stop_batch = True
            self._provider_stop_status = exc.status_code
            return [{
                "error": type(exc).__name__,
                "message": str(exc)[:300],
                "stop_batch": True,
            }]
        except Exception as exc:
            # A reverse-image provider failure must never invent or block TMDB identity.
            return [{"error": type(exc).__name__, "message": str(exc)[:300]}]

    def _find_movie(
        self,
        reverse_image_results: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        """Resolve the poster only from a TMDB movie ID returned by Google Lens.

        No Lens title, snippet, country, year, cast, IMDb link, image URL,
        poster filename or source-folder name is used to identify the movie.
        Lens must return an official themoviedb.org/movie/<numeric-id> URL.
        TMDB then supplies the canonical movie data and artwork.
        """
        reverse_image_failed = any(
            isinstance(item, dict) and item.get("error")
            for item in (reverse_image_results or [])
        )
        lens_tmdb_ids = extract_tmdb_movie_ids(reverse_image_results)
        title_search = {
            "status": "not_used_id_only",
            "queries": [],
            "result_count": 0,
            "qualified_candidate_ids": list(lens_tmdb_ids),
            "errors": [],
            "folder_name_used": False,
            "image_filename_used": False,
        }
        evidence: dict[str, Any] = {
            "strategy": "google_lens_tmdb_movie_id_only",
            "tmdb_ids": list(lens_tmdb_ids),
            "imdb_ids": [],
            "lens_title_queries": [],
            "metadata_observations": [],
            "backlinks": [],
            "tmdb_title_search": title_search,
            "id_resolution": {
                "strategy": "official_tmdb_movie_url_only",
                "tmdb_ids_seen": list(lens_tmdb_ids),
                "imdb_ids_seen": [],
                "unresolved_tmdb_ids": [],
                "unresolved_imdb_ids": [],
                "folder_name_used": False,
                "image_filename_used": False,
                "lens_title_hints_used_for_tmdb_search": False,
                "errors": [],
            },
            "candidate_identities": [],
        }

        if reverse_image_failed:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "Google Lens image lookup failed; no TMDB identity was guessed",
                "candidate_ids": [],
            }, evidence

        if not lens_tmdb_ids:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": (
                    "Google Lens did not return an official TMDB movie URL containing "
                    "a numeric movie ID; title and country searches are disabled"
                ),
                "candidate_ids": [],
            }, evidence

        resolved: dict[int, dict[str, Any]] = {}
        details_errors: list[dict[str, str]] = []
        for tmdb_id in lens_tmdb_ids:
            try:
                details = self._details_cache.get(int(tmdb_id))
                if details is None:
                    details = self.tmdb.movie_details(int(tmdb_id))
                    self._details_cache[int(tmdb_id)] = details
                if int(details.get("id") or 0) != int(tmdb_id):
                    raise ValueError("TMDB returned a movie record with a different ID")
                resolved[int(tmdb_id)] = details
            except Exception as exc:
                details_errors.append({
                    "tmdb_id": str(tmdb_id),
                    "error": type(exc).__name__,
                    "message": str(exc)[:250],
                })

        evidence["id_resolution"]["errors"] = details_errors
        evidence["id_resolution"]["unresolved_tmdb_ids"] = [
            int(row["tmdb_id"]) for row in details_errors
        ]
        if details_errors:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "One or more TMDB movie IDs returned by Lens could not be verified through TMDB",
                "candidate_ids": sorted(resolved),
                "details_errors": details_errors,
            }, evidence

        identities: list[dict[str, Any]] = []
        for tmdb_id, details in sorted(resolved.items()):
            normalized = TMDBClient.normalize_details(details)
            imdb_id = str(
                (normalized.get("external_ids") or {}).get("imdb_id") or ""
            ).strip().lower() or None
            profile = _candidate_metadata_profile(normalized)
            identity = {
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
                "title": normalized.get("title"),
                "original_title": normalized.get("original_title"),
                "year": normalized.get("year"),
                "metadata_profile": profile,
                "metadata_match": {
                    "strategy": "not_used_tmdb_id_only",
                    "matched_fields": [],
                    "conflicts": {},
                },
                "lens_title_queries": [],
                "lens_title_support_count": 0,
                "title_similarity": None,
                "cross_provider_match": False,
            }
            identities.append(identity)

        evidence["candidate_identities"] = identities

        # Multiple distinct official TMDB IDs are conflicting image results.
        # Fetch and show their TMDB profiles, but never choose one using text.
        if len(identities) != 1:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": (
                    "Google Lens returned multiple different official TMDB movie IDs; "
                    "the image identity is ambiguous"
                ),
                "candidate_ids": sorted(resolved),
                "candidate_comparisons": [
                    {
                        "tmdb_id": row["tmdb_id"],
                        "title": row["title"],
                        "imdb_id": row["imdb_id"],
                        "metadata_profile": row["metadata_profile"],
                    }
                    for row in identities
                ],
            }, evidence

        selected = identities[0]
        selected_id = int(selected["tmdb_id"])
        selected_candidate = resolved[selected_id]
        identity_source = "google_lens_official_tmdb_movie_id"
        evidence["resolved_identity"] = {
            "tmdb_id": selected_id,
            "imdb_id": selected["imdb_id"],
            "source": identity_source,
            "verification": "tmdb_movie_details_id_equals_lens_movie_id",
            "folder_name_used": False,
            "image_filename_used": False,
        }
        return selected_candidate, {
            "status": "ready",
            "confidence": 1.0,
            "identity_source": identity_source,
            "identity_method": "lens_tmdb_movie_id_lookup",
            "tmdb_id": selected_id,
            "imdb_id": selected["imdb_id"],
            "folder_name_used_for_matching": False,
            "reason": (
                "Google Lens returned one official TMDB movie ID; TMDB confirmed "
                "the same ID and supplied the movie record"
            ),
        }, evidence

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
        lens_diagnostics = (
            getattr(self.reverse_image, "last_lens_diagnostics", {}) or {}
        ) if self.reverse_image else {}

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
            saved_results = previous_reverse_image.get("results") or []
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



        candidate, match, reverse_image_evidence = self._find_movie(reverse_image_results)
        reverse_image_evidence["results_source"] = reverse_image_results_source
        reverse_image_evidence["lens_diagnostics"] = lens_diagnostics

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
            title_selection = self.tmdb.select_worldwide_title(normalized)

            external_ids = normalized.setdefault("external_ids", {})
            imdb_id = (
                str(match.get("imdb_id") or external_ids.get("imdb_id") or "")
                .strip()
                .lower()
                or None
            )
            if imdb_id:
                external_ids["imdb_id"] = imdb_id

            canonical_title = (
                title_selection.get("title")
                or normalized.get("title")
                or f"TMDB Movie {tmdb_id}"
            )
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
            record["tmdb_id"] = None
            record["imdb_id"] = None
            record["identity"] = None
            record["correction"] = None
            record["status"] = "review"

        return record

    def run_batch(self, limit: int, apply: bool) -> dict[str, Any]:
        self._stop_batch = False
        self._provider_stop_status = None
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
            unresolved_imdb = ",".join(str(value) for value in (unresolved_ids.get("unresolved_imdb_ids") or [])[:3]) or "none"
            unresolved_tmdb = ",".join(str(value) for value in (unresolved_ids.get("unresolved_tmdb_ids") or [])[:3]) or "none"
            lens_diag = (reverse_image_info.get("latest_query") or {}).get("diagnostics") or {}
            lens_error_items = (reverse_image_info.get("latest_query") or {}).get("errors") or []

            def safe_log_detail(value: Any, limit: int = 170) -> str:
                detail = " ".join(str(value or "").replace('"', "'").replace("\r", " ").replace("\n", " ").split())
                secret = str(getattr(self.settings, "brightdata_api_key", "") or "")
                if secret:
                    detail = detail.replace(secret, "[REDACTED]")
                return detail[:limit] or "none"

            lens_error = "none"
            if lens_error_items:
                first_error = lens_error_items[0]
                lens_error = safe_log_detail(
                    f"{first_error.get('type', 'ProviderError')}: {first_error.get('message') or first_error.get('error')}"
                )
            print(
                f"[KafiaNet] [{position}/{total}] DONE status={record['status']} "
                f"tmdb_id={tmdb_id if tmdb_id is not None else 'none'} "
                f"imdb_id={imdb_id if imdb_id is not None else 'none'} "
                f"reverse_image={latest_query_status} evidence={reverse_image_source} "
                f"lens_http={lens_diag.get('http_status', 'none')} "
                f"lens_attempts={lens_diag.get('attempts', 'none')} "
                f"lens_results={lens_diag.get('result_count', 'none')} "
                f"lens_error='{lens_error}' "
                f"tmdb_title_search={(identity_evidence.get('tmdb_title_search') or {}).get('status', 'unknown')} "
                f"tmdb_search_queries={len((identity_evidence.get('tmdb_title_search') or {}).get('queries') or [])} "
                f"tmdb_search_matches={len((identity_evidence.get('tmdb_title_search') or {}).get('qualified_candidate_ids') or [])} "
                f'reason="{reason}" candidates={candidate_text} '
                f"unresolved_imdb_ids={unresolved_imdb} unresolved_tmdb_ids={unresolved_tmdb}",
                flush=True,
            )

            title_search_report = identity_evidence.get("tmdb_title_search") or {}
            for query_report in (title_search_report.get("queries") or [])[:8]:
                print(
                    f"[KafiaNet] [{position}/{total}] LENS_TITLE "
                    f"query='{safe_log_detail(query_report.get('query'), 120)}' "
                    f"tmdb_candidate_ids={','.join(str(value) for value in (query_report.get('qualified_candidate_ids') or [])[:10]) or 'none'}",
                    flush=True,
                )

            for lens_result in (identity_evidence.get("backlinks") or [])[:12]:
                lens_metadata = lens_result.get("metadata_clues") or {}
                lens_page = (
                    lens_result.get("resolved_url")
                    or (lens_result.get("resolved_urls") or [None])[0]
                    or lens_result.get("url")
                )
                print(
                    f"[KafiaNet] [{position}/{total}] LENS_RESULT "
                    f"title='{safe_log_detail(lens_result.get('title'), 130)}' "
                    f"snippet='{safe_log_detail(lens_result.get('content'), 360)}' "
                    f"year_clues={','.join(str(value) for value in (lens_metadata.get('years') or [])) or 'none'} "
                    f"country_clues='{safe_log_detail(', '.join(lens_metadata.get('countries') or []), 90)}' "
                    f"director_clues='{safe_log_detail(', '.join(lens_metadata.get('directors') or []), 90)}' "
                    f"cast_clues='{safe_log_detail(', '.join(lens_metadata.get('cast') or []), 110)}' "
                    f"character_clues='{safe_log_detail(', '.join(lens_metadata.get('characters') or []), 90)}' "
                    f"rating_clues='{safe_log_detail(', '.join(str(value.get('value')) for value in (lens_metadata.get('ratings') or []) if isinstance(value, dict)), 60)}' "
                    f"url='{safe_log_detail(lens_page, 180)}' "
                    f"url_error='{safe_log_detail(lens_result.get('url_resolution_error'), 120)}'",
                    flush=True,
                )

            for candidate_report in (identity_evidence.get("candidate_identities") or [])[:12]:
                profile = candidate_report.get("metadata_profile") or {}
                metadata = candidate_report.get("metadata_match") or {}
                cast_names = [
                    str(person.get("name") or "")
                    for person in (profile.get("cast") or [])[:5]
                    if isinstance(person, dict) and person.get("name")
                ]
                character_names = [
                    str(person.get("character") or "")
                    for person in (profile.get("cast") or [])[:5]
                    if isinstance(person, dict) and person.get("character")
                ]
                matched_fields = ",".join(metadata.get("matched_fields") or []) or "none"
                conflict_fields = ",".join((metadata.get("conflicts") or {}).keys()) or "none"
                lens_clues = metadata.get("observed_clues") or {}
                print(
                    f"[KafiaNet] [{position}/{total}] TMDB_CANDIDATE "
                    f"id={candidate_report.get('tmdb_id')} "
                    f"title='{safe_log_detail(candidate_report.get('title'), 110)}' "
                    f"imdb_id={candidate_report.get('imdb_id') or 'none'} "
                    f"year={profile.get('year') or 'none'} "
                    f"countries='{safe_log_detail(', '.join(profile.get('countries') or []), 100)}' "
                    f"director='{safe_log_detail(', '.join(profile.get('directors') or []), 90)}' "
                    f"cast='{safe_log_detail(', '.join(cast_names), 110)}' "
                    f"characters='{safe_log_detail(', '.join(character_names), 90)}' "
                    f"rating={profile.get('rating') if profile.get('rating') is not None else 'none'} "
                    f"votes={profile.get('rating_votes') if profile.get('rating_votes') is not None else 'none'} "
                    f"lens_years={','.join(str(value) for value in (lens_clues.get('years') or [])) or 'none'} "
                    f"lens_countries='{safe_log_detail(', '.join(lens_clues.get('countries') or []), 90)}' "
                    f"lens_directors='{safe_log_detail(', '.join(lens_clues.get('directors') or []), 90)}' "
                    f"lens_cast='{safe_log_detail(', '.join(lens_clues.get('cast') or []), 100)}' "
                    f"lens_characters='{safe_log_detail(', '.join(lens_clues.get('main_characters') or []), 90)}' "
                    f"lens_ratings='{safe_log_detail(', '.join(str(value.get('value')) for value in (lens_clues.get('ratings') or []) if isinstance(value, dict)), 50)}' "
                    f"lens_support={candidate_report.get('lens_title_support_count', 0)} "
                    f"matched={matched_fields} conflicts={conflict_fields} "
                    f"strong_metadata_matches={metadata.get('strong_match_count', 0)} "
                    f"metadata_evidence_qualified={metadata.get('evidence_qualified', False)} "
                    f"supporting_lens_pages={metadata.get('supporting_observation_count', 0)}",
                    flush=True,
                )
                for support in (metadata.get("supporting_observations") or [])[:5]:
                    print(
                        f"[KafiaNet] [{position}/{total}] LENS_SUPPORT "
                        f"candidate={candidate_report.get('tmdb_id')} "
                        f"title='{safe_log_detail(support.get('result_title'), 100)}' "
                        f"url='{safe_log_detail(support.get('result_url'), 180)}' "
                        f"matched={','.join(support.get('matched_fields') or []) or 'none'}",
                        flush=True,
                    )
            if self._stop_batch:
                print(
                    f"[KafiaNet] STOP: Bright Data HTTP {self._provider_stop_status}; "
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
