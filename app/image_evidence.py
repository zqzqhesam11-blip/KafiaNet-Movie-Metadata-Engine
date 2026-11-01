from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


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
    """Unwrap readable Google result URLs before validating official destinations.

    Google Lens may return /url wrappers or Google Images /imgres links. For
    /imgres, imgrefurl is the publisher page; imgurl is only the image asset.
    Opaque /goto tokens are resolved separately by the Google Lens client.
    """
    candidate = str(value or "").strip()
    for _ in range(4):
        try:
            parsed = urlparse(candidate)
        except ValueError:
            return ""
        host = (parsed.hostname or "").casefold().rstrip(".")
        is_google = host == "google.com" or host.endswith(".google.com")
        if not is_google:
            return candidate

        path = parsed.path.rstrip("/").casefold()
        if path not in {"/url", "/imgres"}:
            return candidate

        query = parse_qs(parsed.query)
        if path == "/imgres":
            keys = ("imgrefurl", "url", "q")
        else:
            keys = ("q", "url", "adurl", "imgrefurl")
        nested = next(
            (str(item).strip() for key in keys for item in query.get(key, []) if str(item).strip()),
            "",
        )
        if not nested or nested == candidate:
            return candidate
        try:
            nested_parsed = urlparse(nested)
        except ValueError:
            return candidate
        if nested_parsed.scheme not in {"http", "https"} or not nested_parsed.netloc:
            return candidate
        candidate = nested
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
    """Count official TMDB destinations and summarize result URL domains."""
    official_urls: set[str] = set()
    movie_ids: list[int] = []
    seen_ids: set[int] = set()
    result_hosts: dict[str, int] = {}
    result_url_count = 0
    google_wrapper_count = 0

    for result in results or []:
        if not isinstance(result, dict) or result.get("error"):
            continue
        raw_values = result.get("resolved_urls") or []
        if not raw_values:
            raw_values = [
                value for value in (
                    result.get("resolved_url"),
                    result.get("url"),
                    *(result.get("url_candidates") or []),
                )
                if isinstance(value, str) and value.strip()
            ]
        for raw_value in raw_values:
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            result_url_count += 1
            try:
                raw_parsed = urlparse(raw_value.strip())
            except ValueError:
                raw_parsed = None
            if raw_parsed:
                raw_host = (raw_parsed.hostname or "").casefold().rstrip(".")
                raw_path = raw_parsed.path.rstrip("/").casefold()
                if raw_host == "google.com" or raw_host.endswith(".google.com"):
                    if raw_path in {"/url", "/imgres"}:
                        google_wrapper_count += 1

        for destination in _resolved_destination_urls(result):
            try:
                host = (urlparse(destination).hostname or "").casefold().rstrip(".")
            except ValueError:
                host = ""
            if host:
                result_hosts[host] = result_hosts.get(host, 0) + 1
            movie_id = tmdb_movie_id_from_url(destination)
            if movie_id is None:
                continue
            official_urls.add(destination)
            if movie_id not in seen_ids:
                seen_ids.add(movie_id)
                movie_ids.append(movie_id)

    top_hosts = dict(
        sorted(result_hosts.items(), key=lambda item: (-item[1], item[0]))[:12]
    )
    return {
        "official_tmdb_url_count": len(official_urls),
        "official_tmdb_ids": movie_ids,
        "result_url_count": result_url_count,
        "google_wrapper_count": google_wrapper_count,
        "result_hosts": top_hosts,
    }
