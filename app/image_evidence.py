from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse


TMDB_MOVIE_RE = re.compile(r"^/movie/(\d+)(?:[-/]|$)", re.IGNORECASE)


def _allowed_host(url: str, root: str) -> bool:
    try:
        host = (urlparse(str(url or "")).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return host == root or host.endswith("." + root)


def tmdb_movie_id_from_url(url: str) -> int | None:
    """Accept a movie ID only from an official TMDB movie URL."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not _allowed_host(url, "themoviedb.org"):
        return None
    match = TMDB_MOVIE_RE.match(parsed.path)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _resolved_destination_url(value: str) -> str:
    """Unwrap Google's readable /url wrapper; opaque /goto tokens remain opaque."""
    candidate = str(value or "").strip()
    for _ in range(2):
        try:
            parsed = urlparse(candidate)
        except ValueError:
            return ""
        host = (parsed.hostname or "").casefold().rstrip(".")
        if (host == "google.com" or host.endswith(".google.com")) and parsed.path.rstrip("/") == "/url":
            query = parse_qs(parsed.query)
            nested = next(iter(query.get("q") or query.get("url") or []), "")
            if nested:
                candidate = unquote(nested).strip()
                continue
        return candidate
    return candidate


def _resolved_destination_urls(result: dict) -> list[str]:
    """Use resolved destinations first; otherwise inspect only explicit result URLs."""
    resolved_values = result.get("resolved_urls") or []
    if not resolved_values:
        resolved_values = [
            value for value in (result.get("resolved_url"), result.get("url"))
            if isinstance(value, str) and value.strip()
        ]
    destinations: list[str] = []
    seen: set[str] = set()
    for value in resolved_values:
        if not isinstance(value, str) or not value.strip():
            continue
        destination = _resolved_destination_url(value)
        if destination and destination not in seen:
            seen.add(destination)
            destinations.append(destination)
    return destinations


def extract_tmdb_movie_ids(results: list[dict]) -> list[int]:
    """Extract unique numeric IDs only from official TMDB movie destinations.

    Lens titles, text snippets, IMDb links, country names, image URLs, filenames,
    and source-folder names are never read as movie identity inputs.
    """
    movie_ids: list[int] = []
    seen: set[int] = set()
    for result in results or []:
        if not isinstance(result, dict) or result.get("error"):
            continue
        for destination in _resolved_destination_urls(result):
            movie_id = tmdb_movie_id_from_url(destination)
            if movie_id is not None and movie_id not in seen:
                seen.add(movie_id)
                movie_ids.append(movie_id)
    return movie_ids

def tmdb_movie_url_diagnostics(results: list[dict]) -> dict[str, object]:
    """Count distinct official TMDB movie URLs and IDs in resolved Lens results."""
    official_urls: set[str] = set()
    movie_ids: list[int] = []
    seen_ids: set[int] = set()
    for result in results or []:
        if not isinstance(result, dict) or result.get("error"):
            continue
        for destination in _resolved_destination_urls(result):
            movie_id = tmdb_movie_id_from_url(destination)
            if movie_id is None:
                continue
            official_urls.add(destination)
            if movie_id not in seen_ids:
                seen_ids.add(movie_id)
                movie_ids.append(movie_id)
    return {
        "official_tmdb_url_count": len(official_urls),
        "official_tmdb_ids": movie_ids,
    }
