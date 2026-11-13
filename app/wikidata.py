from __future__ import annotations

import json
import re
from typing import Any

import httpx


WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
FILM_INSTANCE_IDS = {"Q11424", "Q24862", "Q506240"}


def _clean(value: Any, limit: int = 180) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _title_key(value: Any) -> str:
    return re.sub(r"[^\w]+", " ", _clean(value, 250).casefold(), flags=re.UNICODE).strip()


def _claim_values(claims: dict[str, Any], property_id: str) -> list[Any]:
    output = []
    for row in claims.get(property_id) or []:
        if not isinstance(row, dict):
            continue
        value = (
            (row.get("mainsnak") or {}).get("datavalue") or {}
        ).get("value")
        if value is not None:
            output.append(value)
    return output


def _entity_ids(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(
        str(value.get("id") or "")
        for value in values
        if isinstance(value, dict) and re.fullmatch(r"Q\d+", str(value.get("id") or ""))
    ))


def _year_from_claims(claims: dict[str, Any]) -> int | None:
    years = []
    for value in _claim_values(claims, "P577"):
        raw = str(value.get("time") or "") if isinstance(value, dict) else ""
        match = re.match(r"^[+-]?(\d{4})-", raw)
        if match:
            year = int(match.group(1))
            if 1870 <= year <= 2100:
                years.append(year)
    return min(years) if years else None


class WikidataClient:
    """Bounded Wikidata film lookup; results are reviewer evidence, never identity proof."""

    def __init__(self, timeout: float = 20):
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "KafiaNet-Movie-Metadata-Engine/1.0 (film metadata cross-check)",
            },
        )

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self.client.get(WIKIDATA_API_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Wikidata returned an invalid response")
        if payload.get("error"):
            raise ValueError("Wikidata API returned an error")
        return payload

    def find_films_by_google_play_id(self, google_play_id: str) -> dict[str, Any]:
        """Resolve an exact Google Play Movies ID through Wikidata P6562.

        This is an identifier lookup, not a title search. The returned QIDs are
        still only candidates: the caller must resolve one QID through TMDB
        /find and confirm the same Wikidata ID in TMDB movie details.
        """
        external_id = str(google_play_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", external_id):
            raise ValueError("A valid Google Play Movies external ID is required")

        literal = json.dumps(external_id, ensure_ascii=True)
        query = (
            "SELECT DISTINCT ?item WHERE { "
            f"?item wdt:P6562 {literal} . "
            "?item wdt:P31/wdt:P279* ?filmClass . "
            "VALUES ?filmClass { wd:Q11424 wd:Q24862 wd:Q506240 } "
            "} LIMIT 4"
        )
        response = self.client.post(
            WIKIDATA_SPARQL_URL,
            data={"query": query, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Wikidata Query Service returned an invalid response")
        bindings = ((payload.get("results") or {}).get("bindings") or [])
        if not isinstance(bindings, list):
            raise ValueError("Wikidata Query Service returned an invalid bindings list")

        qids: list[str] = []
        for row in bindings:
            if not isinstance(row, dict):
                continue
            item = row.get("item") or {}
            uri = str(item.get("value") or "") if isinstance(item, dict) else ""
            match = re.fullmatch(r"https?://www\.wikidata\.org/entity/(Q[1-9]\d{0,15})", uri)
            if match and match.group(1) not in qids:
                qids.append(match.group(1))

        return {
            "provider": "wikidata_sparql_exact_external_id",
            "property": "P6562",
            "google_play_id": external_id,
            "status": (
                "resolved" if len(qids) == 1
                else "ambiguous" if len(qids) > 1
                else "no_match"
            ),
            "wikidata_ids": qids,
            "result_count": len(qids),
            "identity_evidence": bool(len(qids) == 1),
            "automatic_match_allowed": False,
        }

    @staticmethod
    def _normalize_hints(title_hints: list[str], limit: int) -> list[str]:
        output = []
        seen = set()
        for value in title_hints or []:
            title = _clean(value)
            key = _title_key(title)
            if len(key) < 3 or key in seen:
                continue
            seen.add(key)
            output.append(title)
            if len(output) >= max(1, min(int(limit), 5)):
                break
        return output

    def search_films(
        self,
        title_hints: list[str],
        *,
        max_queries: int = 5,
        results_per_query: int = 4,
        max_candidates: int = 12,
    ) -> dict[str, Any]:
        queries = self._normalize_hints(title_hints, max_queries)
        output: dict[str, Any] = {
            "provider": "wikidata_action_api",
            "status": "no_title_hints" if not queries else "searching",
            "identity_evidence": False,
            "automatic_match_allowed": False,
            "query_count": len(queries),
            "queries": queries,
            "search_result_count": 0,
            "candidate_count": 0,
            "candidates": [],
            "errors": [],
        }
        if not queries:
            return output

        found: dict[str, dict[str, Any]] = {}
        successful_queries = 0
        for query in queries:
            try:
                payload = self._get({
                    "action": "wbsearchentities",
                    "search": query,
                    "language": "en",
                    "uselang": "en",
                    "type": "item",
                    "limit": max(1, min(int(results_per_query), 5)),
                    "format": "json",
                })
                rows = payload.get("search") or []
                if not isinstance(rows, list):
                    raise ValueError("Wikidata search returned an invalid result list")
                successful_queries += 1
                output["search_result_count"] += len(rows)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    entity_id = str(row.get("id") or "")
                    if not re.fullmatch(r"Q\d+", entity_id):
                        continue
                    found.setdefault(entity_id, {
                        "id": entity_id,
                        "search_label": _clean(row.get("label"), 250),
                        "search_description": _clean(row.get("description"), 300),
                        "matched_query": query,
                    })
                    if len(found) >= max(1, min(int(max_candidates), 20)):
                        break
            except Exception as exc:
                output["errors"].append({
                    "query": query,
                    "type": type(exc).__name__,
                    "message": str(exc).replace("\n", " ")[:220],
                })
            if len(found) >= max(1, min(int(max_candidates), 20)):
                break

        if not found:
            output["status"] = "error" if successful_queries == 0 else "no_candidates"
            return output

        try:
            payload = self._get({
                "action": "wbgetentities",
                "ids": "|".join(found),
                "props": "labels|descriptions|aliases|claims|sitelinks",
                "languages": "en",
                "languagefallback": 1,
                "format": "json",
            })
            entities = payload.get("entities") or {}
            if not isinstance(entities, dict):
                raise ValueError("Wikidata entity payload was invalid")
        except Exception as exc:
            output["errors"].append({
                "stage": "fetch_entities",
                "type": type(exc).__name__,
                "message": str(exc).replace("\n", " ")[:220],
            })
            output["status"] = "error"
            return output

        director_ids: list[str] = []
        for entity in entities.values():
            if isinstance(entity, dict):
                director_ids.extend(_entity_ids(_claim_values(entity.get("claims") or {}, "P57")))
        director_labels: dict[str, str] = {}
        director_ids = list(dict.fromkeys(director_ids))
        if director_ids:
            try:
                people_payload = self._get({
                    "action": "wbgetentities",
                    "ids": "|".join(director_ids[:50]),
                    "props": "labels",
                    "languages": "en",
                    "languagefallback": 1,
                    "format": "json",
                })
                people = people_payload.get("entities") or {}
                director_labels = {
                    entity_id: _clean(
                        ((entity or {}).get("labels") or {}).get("en", {}).get("value"),
                        160,
                    )
                    for entity_id, entity in people.items()
                    if isinstance(entity, dict)
                }
            except Exception as exc:
                output["errors"].append({
                    "stage": "fetch_director_labels",
                    "type": type(exc).__name__,
                    "message": str(exc).replace("\n", " ")[:220],
                })

        candidates = []
        for entity_id, search_row in found.items():
            entity = entities.get(entity_id) or {}
            if not isinstance(entity, dict) or entity.get("missing") is not None:
                continue
            claims = entity.get("claims") or {}
            instance_ids = _entity_ids(_claim_values(claims, "P31"))
            description = _clean(
                ((entity.get("descriptions") or {}).get("en") or {}).get("value")
                or search_row.get("search_description"),
                300,
            )
            imdb_ids = [
                _clean(value, 30).lower()
                for value in _claim_values(claims, "P345")
                if re.fullmatch(r"tt\d{5,12}", str(value or "").strip(), flags=re.I)
            ]
            tmdb_ids = [
                _clean(value, 24)
                for value in _claim_values(claims, "P4947")
                if str(value or "").strip().isdigit()
            ]
            is_film = bool(
                set(instance_ids) & FILM_INSTANCE_IDS
                or (imdb_ids or tmdb_ids)
                and re.search(r"\b(film|movie|motion picture)\b", description, flags=re.I)
            )
            if not is_film:
                continue

            label = _clean(
                ((entity.get("labels") or {}).get("en") or {}).get("value")
                or search_row.get("search_label"),
                250,
            )
            aliases = [
                _clean(row.get("value"), 180)
                for row in ((entity.get("aliases") or {}).get("en") or [])
                if isinstance(row, dict) and row.get("value")
            ]
            enwiki_title = _clean(
                ((entity.get("sitelinks") or {}).get("enwiki") or {}).get("title"),
                250,
            )
            title_values = list(dict.fromkeys(
                value for value in [label, enwiki_title, *aliases] if value
            ))
            matched_query = str(search_row.get("matched_query") or "")
            exact_title_match = _title_key(matched_query) in {
                _title_key(value) for value in title_values
            }
            directors = [
                {"wikidata_id": qid, "name": director_labels.get(qid) or None}
                for qid in _entity_ids(_claim_values(claims, "P57"))
            ]
            candidates.append({
                "wikidata_id": entity_id,
                "title": label or search_row.get("search_label"),
                "title_variants": title_values[:8],
                "description": description,
                "year": _year_from_claims(claims),
                "directors": directors[:5],
                "imdb_ids": list(dict.fromkeys(imdb_ids))[:5],
                "tmdb_ids": list(dict.fromkeys(tmdb_ids))[:5],
                "matched_query": matched_query,
                "exact_title_match": exact_title_match,
                "url": f"https://www.wikidata.org/wiki/{entity_id}",
                "instance_of": instance_ids[:8],
            })

        output["candidates"] = candidates[:max(1, min(int(max_candidates), 20))]
        output["candidate_count"] = len(output["candidates"])
        if output["candidate_count"]:
            output["status"] = "suggestions_only"
        else:
            output["status"] = "no_film_entities"
        return output

    def close(self) -> None:
        self.client.close()
