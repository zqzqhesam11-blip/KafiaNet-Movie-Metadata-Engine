from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse


IMDB_ID_RE = re.compile(r"\b(tt\d{5,12})\b", re.IGNORECASE)
TMDB_MOVIE_RE = re.compile(r"^/movie/(\d+)(?:[-/]|$)", re.IGNORECASE)
GENERIC_WORDS = {
    "poster", "posters", "movie", "movies", "film", "films", "cover",
    "covers", "image", "images", "thumbnail", "thumb", "download",
    "original", "large", "small", "medium", "full", "hd", "jpg", "jpeg",
    "png", "webp", "gif", "bmp", "tif", "tiff", "avif", "the", "and",
    "a", "an", "of", "for", "from", "official", "artwork", "bluray",
    "brrip", "webrip", "webdl", "hdrip", "x264", "x265", "h264", "h265",
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
    value = re.sub(r"\b(?:19|20)\d{2}\b", " ", value)
    value = re.sub(r"\b(?:480|720|1080|1440|2160)p\b|\b4k\b", " ", value, flags=re.I)
    value = re.sub(r"[_+.-]+", " ", value)
    value = re.sub(r"[^0-9A-Za-zÀ-ÖØ-öø-ÿ\u0400-\u04FF\u0600-\u06FF ]+", " ", value)
    words = [
        word.casefold()
        for word in value.split()
        if word.casefold() not in GENERIC_WORDS and len(word) > 1
    ]
    if not words or not any(any(ch.isalpha() for ch in word) for word in words):
        return None
    hint = " ".join(words).strip()
    return hint if len(hint) >= 3 else None


def extract_tineye_evidence(results: list[dict]) -> dict[str, list]:
    """Extract conservative title hints and trusted external movie IDs from TinEye backlinks."""
    hints: list[str] = []
    imdb_ids: list[str] = []
    tmdb_ids: list[int] = []

    for result in results or []:
        if not isinstance(result, dict):
            continue

        title = _clean_hint(str(result.get("title") or ""))
        if title and title.casefold() not in {item.casefold() for item in hints}:
            hints.append(title)

        for field in ("url", "source"):
            url = str(result.get(field) or "")
            imdb_id = imdb_id_from_url(url)
            if imdb_id and imdb_id not in imdb_ids:
                imdb_ids.append(imdb_id)

            tmdb_id = tmdb_movie_id_from_url(url)
            if tmdb_id and tmdb_id not in tmdb_ids:
                tmdb_ids.append(tmdb_id)

        source = str(result.get("source") or "")
        try:
            basename = PurePosixPath(urlparse(source).path).name
        except ValueError:
            basename = ""
        source_hint = _clean_hint(basename)
        if source_hint and source_hint.casefold() not in {item.casefold() for item in hints}:
            hints.append(source_hint)

    return {
        "title_hints": hints[:8],
        "imdb_ids": imdb_ids[:8],
        "tmdb_ids": tmdb_ids[:8],
    }
