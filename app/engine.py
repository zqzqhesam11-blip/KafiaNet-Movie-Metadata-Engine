from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.github_api import GitHubAPI
from app.image_evidence import extract_reverse_image_evidence
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
        and normalize_title(item.get("lens_title") or "") in allowed_titles
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
        record_field("year", years, [candidate_year], any(abs(year - int(candidate_year)) <= 1 for year in years))

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
        """Identify a movie from Lens image matches, then resolve it through TMDB.

        Official IMDb/TMDB links in Lens are the strongest evidence. If Lens
        returns no official ID links, bounded Lens result titles may be searched
        in TMDB. Folder names, folder years, image filenames and descriptions
        are never sent to TMDB and never identify a movie.
        """
        evidence = extract_reverse_image_evidence(reverse_image_results)
        reverse_image_failed = any(
            isinstance(item, dict) and item.get("error")
            for item in reverse_image_results
        )
        candidates: dict[str, dict[str, Any]] = {}
        source_ids: dict[str, dict[str, set[str]]] = {}
        title_search_scores: dict[str, float] = {}
        resolution_errors: list[dict[str, str]] = []
        unresolved_imdb_ids: list[str] = []
        unresolved_tmdb_ids: list[int] = []

        def add_candidate(candidate: dict[str, Any], provider: str, source_id: str) -> None:
            candidate_id = candidate.get("id")
            try:
                key = str(int(candidate_id))
            except (TypeError, ValueError):
                return
            candidates.setdefault(key, candidate)
            links = source_ids.setdefault(
                key,
                {
                    "imdb_ids": set(),
                    "tmdb_ids": set(),
                    "lens_title_queries": set(),
                    "lens_result_titles": set(),
                },
            )
            if provider == "imdb":
                links["imdb_ids"].add(str(source_id))
            elif provider == "tmdb":
                links["tmdb_ids"].add(str(source_id))
            elif provider == "lens_title":
                links["lens_title_queries"].add(str(source_id))

        # Resolve official IMDb URLs before considering title-based fallback.
        for imdb_id in list(dict.fromkeys(evidence.get("imdb_ids") or []))[:MAX_OFFICIAL_ID_LOOKUPS]:
            try:
                found = self.tmdb.find_by_imdb_id(imdb_id)
                if not found:
                    unresolved_imdb_ids.append(imdb_id)
                for candidate in found:
                    add_candidate(candidate, "imdb", imdb_id)
            except Exception as exc:
                resolution_errors.append({
                    "provider": "imdb", "id": imdb_id,
                    "error": type(exc).__name__, "message": str(exc)[:250],
                })

        # Resolve official TMDB movie URLs to their complete canonical records.
        for tmdb_id in list(dict.fromkeys(evidence.get("tmdb_ids") or []))[:MAX_OFFICIAL_ID_LOOKUPS]:
            try:
                details = self.tmdb.movie_details(int(tmdb_id))
                self._details_cache[int(tmdb_id)] = details
                add_candidate(details, "tmdb", str(int(tmdb_id)))
            except Exception as exc:
                unresolved_tmdb_ids.append(int(tmdb_id))
                resolution_errors.append({
                    "provider": "tmdb", "id": str(tmdb_id),
                    "error": type(exc).__name__, "message": str(exc)[:250],
                })

        # Preserve which Lens result title carried each official ID backlink.
        # This lets the same metadata fingerprint validate ID-based candidates too.
        for candidate_id, links in source_ids.items():
            for backlink in evidence.get("backlinks") or []:
                backlink_tmdb_ids = {
                    str(value) for value in (backlink.get("tmdb_ids") or [])
                }
                backlink_imdb_ids = {
                    str(value).lower() for value in (backlink.get("imdb_ids") or [])
                }
                if (
                    str(candidate_id) in backlink_tmdb_ids
                    or bool(backlink_imdb_ids & {str(value).lower() for value in links["imdb_ids"]})
                ):
                    result_title = str(backlink.get("lens_title") or "").strip()
                    if result_title:
                        links["lens_result_titles"].add(result_title)

        # If Lens did not expose an official ID URL, search TMDB using only the
        # Lens result titles. This is not a second Google search and never uses
        # the catalog folder name, the poster filename, the image URL or snippets.
        initial_title_search_status = (
            "skipped_official_ids_present"
            if evidence.get("imdb_ids") or evidence.get("tmdb_ids")
            else "skipped_lens_error"
            if reverse_image_failed
            else "skipped_no_usable_lens_titles"
        )
        tmdb_title_search: dict[str, Any] = {
            "status": initial_title_search_status,
            "queries": [],
            "result_count": 0,
            "qualified_candidate_ids": [],
            "match_threshold": float(getattr(self.settings, "match_accept_score", 0.94)),
            "errors": [],
            "folder_name_used": False,
            "image_filename_used": False,
        }
        if not evidence.get("imdb_ids") and not evidence.get("tmdb_ids") and not reverse_image_failed:
            lens_queries = list(dict.fromkeys(evidence.get("lens_title_queries") or []))[:5]
            tmdb_title_search["status"] = "skipped_no_usable_lens_titles"
            if lens_queries:
                threshold = float(getattr(self.settings, "match_accept_score", 0.94))
                tmdb_title_search["status"] = "searching"
                query_reports: list[dict[str, Any]] = []
                for lens_title in lens_queries:
                    try:
                        search_results = self.tmdb.search_movies(lens_title)
                    except Exception as exc:
                        error = {
                            "query": lens_title[:160],
                            "type": type(exc).__name__,
                            "message": str(exc)[:250],
                        }
                        tmdb_title_search["errors"].append(error)
                        query_reports.append({
                            "query": lens_title[:160], "result_count": 0,
                            "qualified_candidate_ids": [], "error": error["type"],
                        })
                        continue

                    qualified_ids: list[int] = []
                    tmdb_title_search["result_count"] += len(search_results or [])
                    for search_candidate in (search_results or [])[:40]:
                        names = (
                            str(search_candidate.get("title") or ""),
                            str(search_candidate.get("original_title") or ""),
                        )
                        similarity = max(
                            (title_similarity(lens_title, name) for name in names),
                            default=0.0,
                        )
                        if similarity < threshold:
                            continue
                        try:
                            candidate_id = int(search_candidate.get("id"))
                        except (TypeError, ValueError):
                            continue
                        add_candidate(search_candidate, "lens_title", lens_title)
                        title_search_scores[str(candidate_id)] = max(
                            title_search_scores.get(str(candidate_id), 0.0),
                            similarity,
                        )
                        if candidate_id not in qualified_ids:
                            qualified_ids.append(candidate_id)
                    query_reports.append({
                        "query": lens_title[:160],
                        "result_count": len(search_results or []),
                        "qualified_candidate_ids": qualified_ids,
                        "error": None,
                    })

                tmdb_title_search["queries"] = query_reports
                tmdb_title_search["qualified_candidate_ids"] = sorted(
                    int(value) for value in title_search_scores
                )
                if tmdb_title_search["qualified_candidate_ids"]:
                    tmdb_title_search["status"] = "matched"
                elif tmdb_title_search["errors"]:
                    tmdb_title_search["status"] = "error"
                else:
                    tmdb_title_search["status"] = "no_confident_match"

        evidence["tmdb_title_search"] = tmdb_title_search
        evidence["resolved_sources"] = {
            candidate_id: {
                "imdb_ids": sorted(links["imdb_ids"]),
                "tmdb_ids": sorted(links["tmdb_ids"], key=int),
                "lens_title_queries": sorted(links["lens_title_queries"], key=str.casefold),
                "lens_result_titles": sorted(links["lens_result_titles"], key=str.casefold),
            }
            for candidate_id, links in source_ids.items()
        }
        evidence["id_resolution"] = {
            "strategy": (
                "official_imdb_tmdb_urls"
                if evidence.get("imdb_ids") or evidence.get("tmdb_ids")
                else "lens_title_to_tmdb_search"
            ),
            "folder_name_used": False,
            "image_filename_used": False,
            "lens_title_hints_used_for_tmdb_search": bool(tmdb_title_search["queries"]),
            "imdb_ids_seen": list(dict.fromkeys(evidence.get("imdb_ids") or [])),
            "tmdb_ids_seen": list(dict.fromkeys(evidence.get("tmdb_ids") or [])),
            "unresolved_imdb_ids": unresolved_imdb_ids,
            "unresolved_tmdb_ids": unresolved_tmdb_ids,
            "errors": resolution_errors,
        }

        if reverse_image_failed:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "Google Lens image search failed; TMDB search was not attempted without Lens evidence",
            }, evidence

        if not evidence.get("imdb_ids") and not evidence.get("tmdb_ids") and not candidates:
            reason = (
                "Lens returned no usable movie title for TMDB lookup"
                if not evidence.get("lens_title_queries")
                else "No Lens title produced a sufficiently close TMDB movie-title match"
            )
            if tmdb_title_search["errors"]:
                reason += "; TMDB title search returned provider errors"
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": reason + "; folder names and image filenames were not used",
                "candidate_ids": [],
            }, evidence

        if resolution_errors:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "At least one official IMDb/TMDB identifier could not be verified; refusing an incomplete identity decision",
                "candidate_ids": sorted(candidates, key=int),
                "resolution_errors": resolution_errors,
            }, evidence

        if not candidates:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "Official IMDb/TMDB links were found, but none resolved to a TMDB movie record",
                "candidate_ids": [],
            }, evidence

        identities: dict[str, dict[str, Any]] = {}
        identity_mismatches: list[dict[str, Any]] = []
        details_errors: list[dict[str, str]] = []
        for candidate_id in sorted(candidates, key=int):
            details = self._details_cache.get(int(candidate_id))
            if details is None:
                try:
                    details = self.tmdb.movie_details(int(candidate_id))
                    self._details_cache[int(candidate_id)] = details
                except Exception as exc:
                    details_errors.append({
                        "tmdb_id": candidate_id,
                        "error": type(exc).__name__,
                        "message": str(exc)[:250],
                    })
                    continue

            normalized = TMDBClient.normalize_details(details)
            external_ids = normalized.get("external_ids") or {}
            tmdb_imdb_id = str(external_ids.get("imdb_id") or "").strip().lower() or None
            linked_imdb_ids = sorted(source_ids.get(candidate_id, {}).get("imdb_ids", set()))
            linked_tmdb_ids = sorted(
                source_ids.get(candidate_id, {}).get("tmdb_ids", set()), key=int
            )
            linked_lens_titles = sorted(
                source_ids.get(candidate_id, {}).get("lens_title_queries", set()),
                key=str.casefold,
            )
            linked_lens_result_titles = sorted(
                source_ids.get(candidate_id, {}).get("lens_result_titles", set()),
                key=str.casefold,
            )
            associated_lens_titles = _unique_text(
                [*linked_lens_titles, *linked_lens_result_titles]
            )
            alternative_names = [
                str(item.get("title") or "")
                for item in (normalized.get("alternative_titles") or [])
                if isinstance(item, dict) and item.get("title")
            ]
            candidate_names = [
                str(normalized.get("title") or ""),
                str(normalized.get("original_title") or ""),
                *alternative_names,
            ]
            details_title_similarity = max(
                (
                    title_similarity(lens_title, candidate_name)
                    for lens_title in linked_lens_titles
                    for candidate_name in candidate_names
                ),
                default=0.0,
            )
            title_similarity_score = max(
                title_search_scores.get(candidate_id, 0.0),
                details_title_similarity,
            )
            metadata_profile = _candidate_metadata_profile(normalized)
            metadata_match = _compare_lens_metadata(
                normalized,
                evidence.get("metadata_observations") or [],
                associated_lens_titles,
            )

            if tmdb_imdb_id and linked_imdb_ids and (
                tmdb_imdb_id not in linked_imdb_ids or len(linked_imdb_ids) != 1
            ):
                identity_mismatches.append({
                    "tmdb_id": int(candidate_id),
                    "tmdb_imdb_id": tmdb_imdb_id,
                    "linked_imdb_ids": linked_imdb_ids,
                    "reason": "TMDB external_ids.imdb_id does not uniquely match the IMDb backlink IDs",
                })
                continue

            resolved_imdb_id = tmdb_imdb_id or (
                linked_imdb_ids[0] if len(linked_imdb_ids) == 1 else None
            )
            identities[candidate_id] = {
                "tmdb_id": int(candidate_id),
                "imdb_id": resolved_imdb_id,
                "title": normalized.get("title") or candidates[candidate_id].get("title"),
                "original_title": normalized.get("original_title") or candidates[candidate_id].get("original_title"),
                "year": normalized.get("year"),
                "imdb_ids_from_lens": linked_imdb_ids,
                "tmdb_ids_from_lens": linked_tmdb_ids,
                "lens_title_queries": linked_lens_titles,
                "lens_result_titles": linked_lens_result_titles,
                "lens_title_support_count": len(linked_lens_titles),
                "title_similarity": round(title_similarity_score, 4),
                "metadata_profile": metadata_profile,
                "metadata_match": metadata_match,
                "cross_provider_match": bool(
                    int(candidate_id) in [int(value) for value in linked_tmdb_ids]
                    and resolved_imdb_id and resolved_imdb_id in linked_imdb_ids
                ),
            }

        evidence["candidate_identities"] = list(identities.values())
        if details_errors or identity_mismatches:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "TMDB details could not fully validate all official IMDb/TMDB backlinks or title-search candidates",
                "candidate_ids": sorted(candidates, key=int),
                "details_errors": details_errors,
                "identity_mismatches": identity_mismatches,
            }, evidence

        if not identities:
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "No candidate passed TMDB metadata and IMDb-ID validation",
                "candidate_ids": sorted(candidates, key=int),
            }, evidence

        cross_provider = [
            candidate_id for candidate_id, identity in identities.items()
            if identity["cross_provider_match"]
        ]
        metadata_selected = False
        if len(identities) == 1:
            selected_id = next(iter(identities))
            sole_identity = identities[selected_id]
            if not sole_identity.get("imdb_id"):
                return None, {
                    "status": "review", "confidence": 0.0,
                    "reason": "The TMDB result has no verified IMDb external ID; both IDs are required for READY",
                    "candidate_ids": [int(selected_id)],
                }, evidence
            if unresolved_imdb_ids and not sole_identity["cross_provider_match"]:
                return None, {
                    "status": "review", "confidence": 0.0,
                    "reason": "Some official IMDb links remain unresolved and the candidate lacks matching IMDb+TMDB backlink corroboration",
                    "candidate_ids": [int(selected_id)],
                }, evidence
            if not (evidence.get("imdb_ids") or evidence.get("tmdb_ids")):
                score = sole_identity["title_similarity"]
                if score < float(getattr(self.settings, "match_accept_score", 0.94)):
                    return None, {
                        "status": "review", "confidence": score,
                        "reason": "The TMDB result did not meet the configured Lens-title similarity threshold",
                        "candidate_ids": [int(selected_id)],
                    }, evidence
            metadata = sole_identity.get("metadata_match") or {}
            if len(metadata.get("conflicts") or {}) >= 2 and not metadata.get("strong_match_count"):
                return None, {
                    "status": "review", "confidence": 0.0,
                    "reason": "The only title candidate conflicts with multiple explicit metadata clues from its Lens result",
                    "candidate_ids": [int(selected_id)],
                    "metadata_conflicts": metadata.get("conflicts") or {},
                }, evidence
        elif len(cross_provider) == 1:
            selected_id = cross_provider[0]
        else:
            ranked = sorted(
                identities.items(),
                key=lambda pair: (
                    int((pair[1].get("metadata_match") or {}).get("strong_match_count") or 0),
                    float((pair[1].get("metadata_match") or {}).get("score") or 0.0),
                    int(pair[1].get("lens_title_support_count") or 0),
                    float(pair[1].get("title_similarity") or 0.0),
                ),
                reverse=True,
            )
            best_id, best_identity = ranked[0]
            best_metadata = best_identity.get("metadata_match") or {}
            second_metadata = ranked[1][1].get("metadata_match") or {}
            best_strong_count = int(best_metadata.get("strong_match_count") or 0)
            best_score = float(best_metadata.get("score") or 0.0)
            second_score = float(second_metadata.get("score") or 0.0)

            # A title-only tie is not resolved by TMDB's metadata profile.
            # Require at least two independent Lens-observed dimensions, and
            # a clear score margin over every competing candidate.
            if (
                best_strong_count >= 2
                and (
                    int(second_metadata.get("strong_match_count") or 0) < 2
                    or best_score - second_score >= 1.5
                )
            ):
                selected_id = best_id
                metadata_selected = True
            else:
                return None, {
                    "status": "review",
                    "confidence": 0.0,
                    "reason": (
                        "Multiple TMDB candidates remain; Lens evidence did not provide "
                        "two or more matching metadata dimensions with a unique winner"
                    ),
                    "candidate_ids": sorted(int(value) for value in identities),
                    "cross_provider_candidate_ids": sorted(int(value) for value in cross_provider),
                    "candidate_comparisons": [
                        {
                            "tmdb_id": int(candidate_id),
                            "title": identity.get("title"),
                            "imdb_id": identity.get("imdb_id"),
                            "title_similarity": identity.get("title_similarity"),
                            "lens_title_support_count": identity.get("lens_title_support_count"),
                            "metadata_profile": identity.get("metadata_profile"),
                            "metadata_match": identity.get("metadata_match"),
                        }
                        for candidate_id, identity in ranked
                    ],
                }, evidence

        if not identities[selected_id].get("imdb_id"):
            return None, {
                "status": "review",
                "confidence": 0.0,
                "reason": "The selected TMDB candidate has no verified IMDb external ID",
                "candidate_ids": [int(selected_id)],
            }, evidence

        selected_identity = identities[selected_id]
        selected_candidate = self._details_cache[int(selected_id)]
        selected_imdb_id = selected_identity["imdb_id"]
        used_title_search = bool(selected_identity["lens_title_queries"])
        has_official_lens_ids = bool(
            selected_identity["imdb_ids_from_lens"] or selected_identity["tmdb_ids_from_lens"]
        )
        if metadata_selected:
            identity_source = (
                "google_lens_metadata_fingerprint_official_ids"
                if has_official_lens_ids
                else "google_lens_metadata_fingerprint_tmdb_search"
            )
        else:
            identity_source = (
                "google_lens_title_to_tmdb_search"
                if used_title_search and not has_official_lens_ids
                else "google_lens_official_imdb_tmdb_urls"
            )
        evidence["resolved_identity"] = {
            "tmdb_id": selected_identity["tmdb_id"],
            "imdb_id": selected_imdb_id,
            "source": identity_source,
            "imdb_ids_from_lens": selected_identity["imdb_ids_from_lens"],
            "tmdb_ids_from_lens": selected_identity["tmdb_ids_from_lens"],
            "lens_title_queries": selected_identity["lens_title_queries"],
            "lens_result_titles": selected_identity["lens_result_titles"],
            "title_similarity": selected_identity["title_similarity"],
            "metadata_profile": selected_identity["metadata_profile"],
            "metadata_match": selected_identity["metadata_match"],
            "cross_provider_match": selected_identity["cross_provider_match"],
            "folder_name_used": False,
            "image_filename_used": False,
        }

        return selected_candidate, {
            "status": "ready",
            "confidence": (
                0.9
                if metadata_selected
                else selected_identity["title_similarity"]
                if used_title_search and not has_official_lens_ids
                else 1.0
            ),
            "identity_source": identity_source,
            "identity_method": (
                "lens_metadata_fingerprint_unique_candidate"
                if metadata_selected
                else "matching_tmdb_and_imdb_backlinks"
                if selected_identity["cross_provider_match"]
                else "lens_title_tmdb_search_unique"
                if used_title_search
                else "single_unambiguous_official_movie_id"
            ),
            "tmdb_id": selected_identity["tmdb_id"],
            "imdb_id": selected_imdb_id,
            "title_match_required": used_title_search,
            "folder_name_used_for_matching": False,
            "reason": (
                "Lens title plus at least two independent Lens-observed metadata fields uniquely identified this TMDB candidate; full details and IMDb external ID were verified"
                if metadata_selected
                else "Google Lens image result title uniquely matched a TMDB movie; complete TMDB details and its IMDb external ID were verified"
                if used_title_search
                else "Official IMDb/TMDB movie URL resolved through TMDB and the IMDb identifier was verified"
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
                    f"lens_support={candidate_report.get('lens_title_support_count', 0)} "
                    f"matched={matched_fields} conflicts={conflict_fields} "
                    f"strong_metadata_matches={metadata.get('strong_match_count', 0)}",
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
