from __future__ import annotations

from typing import Any
import httpx

from app.matching import normalize_title


WORLDWIDE_TITLE_POLICY_VERSION = 1
WORLDWIDE_TITLE_MIN_MARKETS = 3


class TMDBClient:
    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self, bearer_token: str, language: str = "en-US", timeout: float = 30):
        if not bearer_token:
            raise RuntimeError("TMDB_BEARER_TOKEN is required")
        self.language = language
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept": "application/json",
            },
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.get(f"{self.BASE_URL}{path}", params=params or {})
        response.raise_for_status()
        return response.json()

    def search_movies(self, title: str, year: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "query": title,
            "include_adult": "false",
            "language": self.language,
        }
        if year:
            params["year"] = year
        data = self._get("/search/movie", params)
        return list(data.get("results") or [])

    def find_by_imdb_id(self, imdb_id: str) -> list[dict[str, Any]]:
        """Resolve a trusted IMDb identifier to TMDB movie records."""
        imdb_id = imdb_id.strip().lower()
        if not imdb_id.startswith("tt") or not imdb_id[2:].isdigit():
            return []
        data = self._get(
            f"/find/{imdb_id}",
            {
                "external_source": "imdb_id",
                "language": self.language,
            },
        )
        return list(data.get("movie_results") or [])

    def movie_details(self, tmdb_id: int) -> dict[str, Any]:
        return self._get(
            f"/movie/{tmdb_id}",
            {
                "language": self.language,
                "append_to_response": (
                    "alternative_titles,credits,release_dates,external_ids,keywords"
                ),
            },
        )

    @staticmethod
    def select_worldwide_title(movie: dict[str, Any]) -> dict[str, Any]:
        """
        Select a conservatively supported public title without preferring a language.

        TMDB does not expose title popularity by audience. Its alternative-title
        records are country-scoped, so a title only overrides the configured-language
        primary title when the same normalized title is listed in at least three
        distinct country markets. Ties and weak evidence keep the primary title.
        """
        primary = str(movie.get("title") or movie.get("original_title") or "").strip()
        primary_key = normalize_title(primary)
        groups: dict[str, dict[str, Any]] = {}

        for item in movie.get("alternative_titles") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            country = str(item.get("country") or "").strip().upper()
            if not title or not country:
                continue

            title_type = str(item.get("type") or "").casefold()
            if any(marker in title_type for marker in ("working title", "internal title", "temporary title")):
                continue

            key = normalize_title(title)
            if not key or key == primary_key:
                continue

            group = groups.setdefault(
                key,
                {"countries": set(), "spellings": {}, "first_seen": len(groups)},
            )
            group["countries"].add(country)
            group["spellings"][title] = group["spellings"].get(title, 0) + 1

        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for key, group in groups.items():
            market_count = len(group["countries"])
            if market_count < WORLDWIDE_TITLE_MIN_MARKETS:
                continue
            spelling = sorted(
                group["spellings"].items(),
                key=lambda pair: (-pair[1], len(pair[0]), pair[0].casefold()),
            )[0][0]
            candidates.append((market_count, key, {**group, "title": spelling}))

        if not candidates:
            return {
                "title": primary,
                "source": "tmdb_primary_title",
                "market_count": 0,
                "markets": [],
                "policy_version": WORLDWIDE_TITLE_POLICY_VERSION,
            }

        best_count = max(row[0] for row in candidates)
        winners = [row for row in candidates if row[0] == best_count]
        if len(winners) != 1:
            return {
                "title": primary,
                "source": "tmdb_primary_title_ambiguous_alternatives",
                "market_count": 0,
                "markets": [],
                "policy_version": WORLDWIDE_TITLE_POLICY_VERSION,
            }

        market_count, _, group = winners[0]
        return {
            "title": group["title"],
            "source": "tmdb_alternative_title_multi_market",
            "market_count": market_count,
            "markets": sorted(group["countries"]),
            "policy_version": WORLDWIDE_TITLE_POLICY_VERSION,
        }

    @staticmethod
    def normalize_details(details: dict[str, Any]) -> dict[str, Any]:
        credits = details.get("credits") or {}
        cast = sorted(
            credits.get("cast") or [],
            key=lambda item: (item.get("order", 9999), item.get("id", 0)),
        )
        crew = credits.get("crew") or []

        def people_for(jobs: set[str]) -> list[dict[str, Any]]:
            result = []
            seen = set()
            for person in crew:
                job = str(person.get("job") or "")
                if job.casefold() in {item.casefold() for item in jobs}:
                    person_id = person.get("id")
                    if person_id not in seen:
                        seen.add(person_id)
                        result.append(
                            {
                                "id": person_id,
                                "name": person.get("name"),
                                "job": job,
                                "department": person.get("department"),
                            }
                        )
            return result

        release_dates: dict[str, list[dict[str, Any]]] = {}
        for country in (details.get("release_dates") or {}).get("results", []):
            certs = [
                {
                    "certification": item.get("certification"),
                    "release_date": item.get("release_date"),
                    "type": item.get("type"),
                }
                for item in country.get("release_dates", [])
                if item.get("certification") or item.get("release_date")
            ]
            if certs:
                release_dates[str(country.get("iso_3166_1") or "")] = certs

        alt_titles = [
            {
                "title": item.get("title"),
                "country": item.get("iso_3166_1"),
                "type": item.get("type"),
            }
            for item in (details.get("alternative_titles") or {}).get("titles", [])
            if item.get("title")
        ]

        collection = details.get("belongs_to_collection")
        keywords = (details.get("keywords") or {}).get("keywords") or []

        return {
            "tmdb_id": details.get("id"),
            "media_type": "movie",
            "title": details.get("title"),
            "original_title": details.get("original_title"),
            "release_date": details.get("release_date"),
            "year": (
                int(str(details.get("release_date"))[:4])
                if str(details.get("release_date") or "")[:4].isdigit()
                else None
            ),
            "original_language": details.get("original_language"),
            "origin_country": details.get("origin_country") or [],
            "production_countries": details.get("production_countries") or [],
            "production_companies": details.get("production_companies") or [],
            "genres": details.get("genres") or [],
            "runtime": details.get("runtime"),
            "status": details.get("status"),
            "overview": details.get("overview"),
            "tagline": details.get("tagline"),
            "homepage": details.get("homepage"),
            "poster_path": details.get("poster_path"),
            "backdrop_path": details.get("backdrop_path"),
            "popularity": details.get("popularity"),
            "vote_average": details.get("vote_average"),
            "vote_count": details.get("vote_count"),
            "collection": collection,
            "external_ids": details.get("external_ids") or {},
            "alternative_titles": alt_titles,
            "certifications_by_country": release_dates,
            "cast": [
                {
                    "id": person.get("id"),
                    "name": person.get("name"),
                    "character": person.get("character"),
                    "order": person.get("order"),
                }
                for person in cast
            ],
            "directors": people_for({"Director"}),
            "writers": people_for({"Writer", "Screenplay", "Story", "Teleplay"}),
            "keywords": keywords,
        }

    def close(self) -> None:
        self.client.close()
