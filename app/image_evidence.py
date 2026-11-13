from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


TMDB_MOVIE_RE = re.compile(r"^/movie/(\d+)(?:[-/]|$)", re.IGNORECASE)
IMDB_TITLE_RE = re.compile(r"^/title/(tt\d+)(?:/mediaviewer(?:/rm\d+)?)?/?$", re.IGNORECASE)
WIKIDATA_ENTITY_RE = re.compile(r"^/(?:wiki|entity)/(Q[1-9]\d{0,15})(?:/|$)", re.IGNORECASE)


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


def imdb_id_from_url(url: str) -> str | None:
    """Accept official IMDb title pages and their media-viewer URLs, but not other subpages."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not _allowed_host(url, "imdb.com"):
        return None
    match = IMDB_TITLE_RE.match(parsed.path)
    return match.group(1).lower() if match else None


def wikidata_id_from_url(url: str) -> str | None:
    """Accept QIDs only from canonical Wikidata entity URLs."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not _allowed_host(url, "wikidata.org"):
        return None
    match = WIKIDATA_ENTITY_RE.match(parsed.path)
    return match.group(1).upper() if match else None


def _typed_lens_ids(result: dict, field: str, pattern: re.Pattern) -> list[str]:
    identifiers = result.get("lens_identifiers") or {}
    if not isinstance(identifiers, dict):
        return []
    values = identifiers.get(field) or []
    if not isinstance(values, list):
        return []
    output = []
    seen = set()
    for value in values:
        candidate = str(value or "").strip()
        if pattern.fullmatch(candidate) and candidate not in seen:
            seen.add(candidate)
            output.append(candidate)
    return output


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
    """Extract unique IDs from official TMDB URLs or explicitly typed Lens metadata.

    Titles, snippets, arbitrary ID keys, commerce IDs, image URLs, filenames and
    source-folder names are never read as movie identity inputs.
    """
    movie_ids: list[int] = []
    seen: set[int] = set()
    for result in results or []:
        if not isinstance(result, dict) or result.get("error"):
            continue
        url_ids = []
        for destination in _resolved_destination_urls(result):
            movie_id = tmdb_movie_id_from_url(destination)
            if movie_id is not None and movie_id not in url_ids:
                url_ids.append(movie_id)
        typed_ids = list(dict.fromkeys(
            int(raw_id) for raw_id in _typed_lens_ids(
                result, "tmdb_ids", re.compile(r"^[1-9]\d{0,11}$")
            )
        ))
        # Preserve the existing Lens URL-candidate order. Typed metadata is
        # accepted only when it is singular and does not conflict with URL IDs.
        if len(typed_ids) > 1 or (
            typed_ids and url_ids and typed_ids[0] not in url_ids
        ):
            continue
        row_ids = url_ids or typed_ids
        for movie_id in row_ids:
            if movie_id not in seen:
                seen.add(movie_id)
                movie_ids.append(movie_id)
    return movie_ids

def extract_imdb_ids(results: list[dict]) -> list[str]:
    """Extract unique IDs from official IMDb title URLs or typed Lens metadata."""
    imdb_ids: list[str] = []
    seen: set[str] = set()
    for result in results or []:
        if not isinstance(result, dict) or result.get("error"):
            continue
        url_ids = []
        for destination in _resolved_destination_urls(result):
            imdb_id = imdb_id_from_url(destination)
            if imdb_id and imdb_id not in url_ids:
                url_ids.append(imdb_id)
        typed_ids = list(dict.fromkeys(
            value.lower() for value in _typed_lens_ids(
                result, "imdb_ids", re.compile(r"^tt\d{5,12}$", re.I)
            )
        ))
        if len(typed_ids) > 1 or (
            typed_ids and url_ids and typed_ids[0] not in url_ids
        ):
            continue
        row_ids = url_ids or typed_ids
        for imdb_id in row_ids:
            if imdb_id not in seen:
                seen.add(imdb_id)
                imdb_ids.append(imdb_id)
    return imdb_ids


def extract_google_play_ids(results: list[dict]) -> list[str]:
    """Extract Google Play Movies identifiers only from typed metadata or id= URLs.

    Path slugs such as /store/movies/details/Breathless are display text, not
    stable external identifiers, and are intentionally ignored.
    """
    output: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(r"^(?=.*\d)[A-Za-z0-9_-]{8,64}$")
    for result in results or []:
        if not isinstance(result, dict) or result.get("error"):
            continue
        for value in _typed_lens_ids(result, "google_play_ids", pattern):
            if value not in seen:
                seen.add(value)
                output.append(value)
        for destination in _resolved_destination_urls(result):
            try:
                parsed = urlparse(destination)
            except ValueError:
                continue
            if parsed.scheme not in {"https", "http"} or not _allowed_host(destination, "play.google.com"):
                continue
            value = (parse_qs(parsed.query).get("id") or [""])[0].strip()
            if pattern.fullmatch(value) and value not in seen:
                seen.add(value)
                output.append(value)
    return output


def extract_wikidata_ids(results: list[dict]) -> list[str]:
    """Extract QIDs only from typed Lens metadata or canonical Wikidata URLs."""
    output: list[str] = []
    seen: set[str] = set()
    for result in results or []:
        if not isinstance(result, dict) or result.get("error"):
            continue
        row_ids = []
        for destination in _resolved_destination_urls(result):
            qid = wikidata_id_from_url(destination)
            if qid and qid not in row_ids:
                row_ids.append(qid)
        for qid in _typed_lens_ids(
            result, "wikidata_ids", re.compile(r"^Q[1-9]\d{0,15}$", re.I)
        ):
            qid = qid.upper()
            if qid not in row_ids:
                row_ids.append(qid)
        # A single Lens row must resolve to one unambiguous Wikidata entity.
        if len(row_ids) != 1:
            continue
        qid = row_ids[0]
        if qid not in seen:
            seen.add(qid)
            output.append(qid)
    return output


def tmdb_movie_url_diagnostics(results: list[dict]) -> dict[str, object]:
    """Count official TMDB destinations and summarize result URL domains."""
    official_urls: set[str] = set()
    official_imdb_urls: set[str] = set()
    movie_ids: list[int] = []
    imdb_ids: list[str] = []
    seen_imdb_ids: set[str] = set()
    seen_ids: set[int] = set()
    result_hosts: dict[str, int] = {}
    result_url_count = 0
    google_wrapper_count = 0
    google_goto_url_count = 0
    result_url_paths: dict[str, int] = {}

    for result in results or []:
        if not isinstance(result, dict) or result.get("error"):
            continue
        raw_values = []
        raw_values.extend(
            value for value in (result.get("resolved_urls") or [])
            if isinstance(value, str) and value.strip()
        )
        raw_values.extend(
            value for value in (
                result.get("resolved_url"),
                result.get("url"),
                *(result.get("url_candidates") or []),
            )
            if isinstance(value, str) and value.strip()
        )
        # Count each distinct URL once even when the same link appears in both
        # the primary url field and url_candidates.
        raw_values = list(dict.fromkeys(raw_values))
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
                raw_path = raw_parsed.path.rstrip("/").casefold() or "/"
                path_label = f"{raw_host}{raw_path}"
                result_url_paths[path_label] = result_url_paths.get(path_label, 0) + 1
                if raw_host == "google.com" or raw_host.endswith(".google.com"):
                    if raw_path in {"/url", "/imgres", "/goto"}:
                        google_wrapper_count += 1
                    if raw_path == "/goto":
                        google_goto_url_count += 1

        for destination in _resolved_destination_urls(result):
            try:
                host = (urlparse(destination).hostname or "").casefold().rstrip(".")
            except ValueError:
                host = ""
            if host:
                result_hosts[host] = result_hosts.get(host, 0) + 1
            movie_id = tmdb_movie_id_from_url(destination)
            imdb_id = imdb_id_from_url(destination)
            if movie_id is not None:
                official_urls.add(destination)
                if movie_id not in seen_ids:
                    seen_ids.add(movie_id)
                    movie_ids.append(movie_id)
            if imdb_id is not None:
                official_imdb_urls.add(destination)
                if imdb_id not in seen_imdb_ids:
                    seen_imdb_ids.add(imdb_id)
                    imdb_ids.append(imdb_id)

    top_hosts = dict(
        sorted(result_hosts.items(), key=lambda item: (-item[1], item[0]))[:12]
    )
    observed_tmdb_ids = extract_tmdb_movie_ids(results)
    observed_imdb_ids = extract_imdb_ids(results)
    return {
        "official_tmdb_url_count": len(official_urls),
        "official_tmdb_ids": observed_tmdb_ids or movie_ids,
        "official_imdb_url_count": len(official_imdb_urls),
        "official_imdb_ids": observed_imdb_ids or imdb_ids,
        "lens_wikidata_ids": extract_wikidata_ids(results),
        "lens_google_play_ids": extract_google_play_ids(results),
        "lens_structured_tmdb_ids": [
            value for result in (results or []) if isinstance(result, dict)
            for value in _typed_lens_ids(result, "tmdb_ids", re.compile(r"^[1-9]\d{0,11}$"))
        ],
        "lens_structured_imdb_ids": [
            value.lower() for result in (results or []) if isinstance(result, dict)
            for value in _typed_lens_ids(result, "imdb_ids", re.compile(r"^tt\d{5,12}$", re.I))
        ],
        "result_url_count": result_url_count,
        "google_wrapper_count": google_wrapper_count,
        "google_goto_url_count": google_goto_url_count,
        "result_url_paths": dict(
            sorted(result_url_paths.items(), key=lambda item: (-item[1], item[0]))[:12]
        ),
        "result_hosts": top_hosts,
    }
