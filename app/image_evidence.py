from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse


IMDB_ID_RE = re.compile(r"\b(tt\d{5,12})\b", re.IGNORECASE)
TMDB_MOVIE_RE = re.compile(r"^/movie/(\d+)(?:[-/]|$)", re.IGNORECASE)
# reverse-image result titles may be full article captions or snippets rather than
# image/movie titles. Keep hints bounded before they can reach the TMDB API.
MAX_TITLE_HINT_CHARS = 160
MAX_TITLE_HINT_WORDS = 22
MAX_BACKLINKS = 12
YEAR_HINT_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
COUNTRY_RE = re.compile(
    r"\b(?:country(?: of origin)?|production country|production countries)\s*[:=–-]\s*([^|;\n<>]{2,60})",
    re.IGNORECASE,
)
DIRECTOR_RE = re.compile(
    r"\b(?:director|directed by)\s*[:=–-]\s*([^|;\n<>]{2,80})",
    re.IGNORECASE,
)

GENERIC_WORDS = {
    "poster", "posters", "movie", "movies", "film", "films", "cover",
    "covers", "image", "images", "thumbnail", "thumb", "download",
    "original", "large", "small", "medium", "full", "hd", "jpg", "jpeg",
    "png", "webp", "gif", "bmp", "tif", "tiff", "avif", "official",
    "artwork", "bluray", "brrip", "webrip", "webdl", "hdrip",
    "x264", "x265", "h264", "h265",
}


def _allowed_host(url: str, root: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return host == root or host.endswith("." + root)


def tmdb_movie_id_from_url(url: str) -> int | None:
    if not _allowed_host(url, "themoviedb.org"):
        return None
    match = TMDB_MOVIE_RE.match(urlparse(url).path)
    return int(match.group(1)) if match else None


def imdb_id_from_url(url: str) -> str | None:
    if not _allowed_host(url, "imdb.com"):
        return None
    match = IMDB_ID_RE.search(urlparse(url).path)
    return match.group(1).lower() if match else None


def _clean_hint(value: str) -> str | None:
    value = unquote(value or "").strip()
    if not value:
        return None

    value = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    value = re.sub(r"\.(?:jpe?g|png|webp|gif|bmp|tiff?|avif)$", "", value, flags=re.I)
    value = re.sub(r"[_+.-]+", " ", value)
    # Reject article/caption prose instead of turning it into a TMDB query.
    if len(value) > MAX_TITLE_HINT_CHARS or len(value.split()) > MAX_TITLE_HINT_WORDS:
        return None

    # Preserve an opening numeric title (e.g. "1917" or "2001: A Space Odyssey"),
    # while dropping a later release-year suffix from common poster filenames.
    parts = value.split()
    parts = [
        part
        for index, part in enumerate(parts)
        if index == 0 or not re.fullmatch(r"(?:19|20)\d{2}", part)
    ]
    value = " ".join(parts)
    value = re.sub(r"\b(?:480|720|1080|1440|2160)p\b|\b4k\b", " ", value, flags=re.I)
    value = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in value)

    words = [
        word
        for word in value.split()
        if word.casefold() not in GENERIC_WORDS
        and (len(word) > 1 or word.casefold() in {"a", "i"})
    ]
    has_letters = any(any(ch.isalpha() for ch in word) for word in words)
    has_short_numeric_title = any(re.fullmatch(r"\d{2,4}", word) for word in words)
    if not words or not (has_letters or has_short_numeric_title):
        return None

    hint = " ".join(words).strip()
    if re.fullmatch(r"(?:tt\d{5,12}|img\d+|image\d+|[0-9a-f]{16,})", hint, flags=re.I):
        return None
    return hint if len(hint) >= 3 else None


def extract_reverse_image_evidence(results: list[dict]) -> dict[str, list]:
    """Extract conservative title hints and trusted external movie IDs from reverse-image backlinks."""
    hints: list[str] = []
    imdb_ids: list[str] = []
    tmdb_ids: list[int] = []
    year_hints: list[int] = []
    country_hints: list[str] = []
    director_hints: list[str] = []
    backlinks: list[dict] = []

    for result in results or []:
        if not isinstance(result, dict) or result.get("error"):
            # Provider failures are diagnostics, not image matches or backlinks.
            continue

        title = _clean_hint(str(result.get("title") or ""))
        if title and title.casefold() not in {item.casefold() for item in hints}:
            hints.append(title)

        linked_imdb_ids: list[str] = []
        linked_tmdb_ids: list[int] = []
        for field in ("url", "source"):
            url = str(result.get(field) or "")
            imdb_id = imdb_id_from_url(url)
            if imdb_id:
                if imdb_id not in imdb_ids:
                    imdb_ids.append(imdb_id)
                if imdb_id not in linked_imdb_ids:
                    linked_imdb_ids.append(imdb_id)

            tmdb_id = tmdb_movie_id_from_url(url)
            if tmdb_id:
                if tmdb_id not in tmdb_ids:
                    tmdb_ids.append(tmdb_id)
                if tmdb_id not in linked_tmdb_ids:
                    linked_tmdb_ids.append(tmdb_id)

        source = str(result.get("source") or "")
        try:
            basename = PurePosixPath(urlparse(source).path).name
        except ValueError:
            basename = ""
        source_hint = _clean_hint(basename)
        if source_hint and source_hint.casefold() not in {item.casefold() for item in hints}:
            hints.append(source_hint)

        raw_title = str(result.get("title") or "")
        raw_content = str(result.get("content") or "")
        raw_text = " ".join((raw_title, raw_content, unquote(basename)))
        for year_match in YEAR_HINT_RE.findall(raw_text):
            year_value = int(year_match)
            if 1888 <= year_value <= 2035 and year_value not in year_hints:
                year_hints.append(year_value)
        for country_match in COUNTRY_RE.findall(raw_content):
            value = country_match.strip(" .,:")
            if value and value.casefold() not in {item.casefold() for item in country_hints}:
                country_hints.append(value)
        for director_match in DIRECTOR_RE.findall(raw_content):
            value = director_match.strip(" .,:")
            if value and value.casefold() not in {item.casefold() for item in director_hints}:
                director_hints.append(value)

        if len(backlinks) < MAX_BACKLINKS:
            backlinks.append({
                "title": raw_title[:180],
                "url": str(result.get("url") or "")[:500],
                "source": source[:500],
                "content": raw_content[:500],
                "imdb_ids": linked_imdb_ids,
                "tmdb_ids": linked_tmdb_ids,
            })

    return {
        "title_hints": hints[:8],
        "imdb_ids": imdb_ids[:8],
        "tmdb_ids": tmdb_ids[:8],
        "year_hints": year_hints[:12],
        "country_hints": country_hints[:8],
        "director_hints": director_hints[:8],
        "backlinks": backlinks,
    }
