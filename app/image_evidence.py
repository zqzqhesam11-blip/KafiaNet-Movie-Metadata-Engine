from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse


IMDB_ID_RE = re.compile(r"\b(tt\d{5,12})\b", re.IGNORECASE)
TMDB_MOVIE_RE = re.compile(r"^/movie/(\d+)(?:[-/]|$)", re.IGNORECASE)
# reverse-image result titles may be full article captions or snippets rather than
# image/movie titles. Keep hints bounded before they can reach the TMDB API.
MAX_TITLE_HINT_CHARS = 160
MAX_TITLE_HINT_WORDS = 22
MAX_BACKLINKS = 80
MAX_LENS_TITLE_QUERIES = 5
YEAR_HINT_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
COUNTRY_RE = re.compile(
    r"\b(?:country(?: of origin)?|production country|production countries)\s*[:=–-]\s*([^|;\n<>]{2,60})",
    re.IGNORECASE,
)
DIRECTOR_RE = re.compile(
    r"\b(?:(?:director|dir)\s*[:=–-]\s*|directed by\s+)([^|;\n<>]{2,80})",
    re.IGNORECASE,
)
CAST_RE = re.compile(
    r"\b(?:cast|starring|stars|featuring)\s*[:=–-]?\s*([^|;\n<>]{2,180})",
    re.IGNORECASE,
)
CHARACTER_RE = re.compile(
    r"\b(?:main character|lead character|character|role)\s*(?:[:=–-]|\bis\b)\s*([^|;\n<>]{2,80})",
    re.IGNORECASE,
)
AS_CHARACTER_RE = re.compile(
    r"([A-ZÀ-Þ][\w'’. -]{1,60}?)\s+as\s+([A-ZÀ-Þ][\w'’. -]{1,50})",
    re.IGNORECASE,
)
CHARACTER_ACTOR_RE = re.compile(
    r"([A-ZÀ-Þ][\w'’. -]{1,45}?)\s*\(\s*([A-ZÀ-Þ][\w'’. -]{1,60})\s*\)",
    re.IGNORECASE,
)
RATING_RE = re.compile(
    r"\b(?:(IMDb|TMDB)\s*(?:rating|score)?\s*[:=]?\s*|(?:rating|score)\s*[:=]\s*)(\d{1,2}(?:\.\d+)?)\s*(?:/\s*10)?",
    re.IGNORECASE,
)

GENERIC_WORDS = {
    "poster", "posters", "cover",
    "covers", "image", "images", "thumbnail", "thumb", "download",
    "original", "large", "small", "medium", "full", "hd", "jpg", "jpeg",
    "png", "webp", "gif", "bmp", "tif", "tiff", "avif", "official",
    "artwork", "bluray", "brrip", "webrip", "webdl", "hdrip",
    "x264", "x265", "h264", "h265", "imdb", "tmdb",
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

    # Remove bracketed release-year labels before punctuation is normalized,
    # while preserving numeric movie titles such as "1917" and "2001".
    value = re.sub(
        r"(?<!\w)[\[(（](?:19|20)\d{2}[\])）](?!\w)",
        " ",
        value,
    )
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


def _resolved_destination_url(result: dict) -> str:
    """Return the final destination exposed by the redirect resolver.

    Google /goto tokens are opaque. They must be resolved over HTTP before
    this parser runs; this helper only unwraps Google's older readable /url?q=
    wrapper when a provider or redirect returns one.
    """
    candidate = str(result.get("resolved_url") or result.get("url") or "").strip()
    for _ in range(2):
        try:
            parsed = urlparse(candidate)
        except ValueError:
            return ""
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host == "google.com" or host.endswith(".google.com"):
            if parsed.path.rstrip("/") == "/url":
                query = parse_qs(parsed.query)
                nested = next(iter(query.get("q") or query.get("url") or []), "")
                if nested:
                    candidate = unquote(nested).strip()
                    continue
        return candidate
    return candidate


def _resolved_destination_urls(result: dict) -> list[str]:
    """Return resolved destinations, falling back to raw fields only for legacy records."""
    resolved_values = result.get("resolved_urls") or []
    if resolved_values:
        candidates = [
            value.strip()
            for value in resolved_values
            if isinstance(value, str) and value.strip()
        ]
    else:
        candidates = [
            value.strip()
            for value in (result.get("resolved_url"), result.get("url"))
            if isinstance(value, str) and value.strip()
        ]

    destinations: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        destination = _resolved_destination_url({**result, "resolved_url": value})
        if destination and destination not in seen:
            seen.add(destination)
            destinations.append(destination)
    return destinations


LENS_QUERY_NOISE = {
    "amazon", "import", "dvd", "blu", "ray", "youtube", "dailymotion",
    "video", "facebook", "photos", "tallenge", "classics", "prime",
    "watch", "et", "fr", "best", "similar", "movies", "like",
}


def _clean_lens_query_title(value: str) -> str | None:
    """Remove obvious Lens retailer/media noise without changing saved evidence titles."""
    words = re.findall(r"[\wÀ-ÿ]+", str(value or ""), flags=re.UNICODE)
    words = [word for word in words if word.casefold() not in LENS_QUERY_NOISE]
    return _clean_hint(" ".join(words))


def _unique_titles(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = " ".join(str(value or "").casefold().split())
        if key and key not in seen:
            seen.add(key)
            result.append(str(value))
    return result


def extract_reverse_image_evidence(results: list[dict]) -> dict[str, list]:
    """Extract official IMDb/TMDB IDs from resolved Lens result destination URLs.

    Official IMDb/TMDB IDs are extracted only from destination URLs. Bounded
    Lens result titles are exposed separately as TMDB search hints; folder
    names and image filenames are never used for TMDB searching.
    """
    hints: list[str] = []
    lens_title_queries: list[str] = []
    imdb_ids: list[str] = []
    tmdb_ids: list[int] = []
    year_hints: list[int] = []
    country_hints: list[str] = []
    director_hints: list[str] = []
    backlinks: list[dict] = []
    lens_title_rows: list[str] = []
    lens_query_rows: list[str] = []

    for result in results or []:
        if not isinstance(result, dict) or result.get("error"):
            # Provider failures are diagnostics, not image matches or backlinks.
            continue

        raw_result_title = str(result.get("title") or "")
        title = _clean_hint(raw_result_title)
        if title:
            lens_title_rows.append(title)
            query_title = _clean_lens_query_title(raw_result_title)
            if query_title:
                lens_query_rows.append(query_title)
            if title.casefold() not in {item.casefold() for item in hints}:
                hints.append(title)
        # Only Lens result titles seed TMDB queries. We derive repeated short
        # phrases from Lens titles to remove retailer/person-name suffix noise;
        # folders, poster filenames, image URLs and snippets remain excluded.

        destination_urls = _resolved_destination_urls(result)
        linked_imdb_ids: list[str] = []
        linked_tmdb_ids: list[int] = []
        for destination_url in destination_urls:
            imdb_id = imdb_id_from_url(destination_url)
            if imdb_id:
                if imdb_id not in imdb_ids:
                    imdb_ids.append(imdb_id)
                if imdb_id not in linked_imdb_ids:
                    linked_imdb_ids.append(imdb_id)
            tmdb_id = tmdb_movie_id_from_url(destination_url)
            if tmdb_id:
                if tmdb_id not in tmdb_ids:
                    tmdb_ids.append(tmdb_id)
                if tmdb_id not in linked_tmdb_ids:
                    linked_tmdb_ids.append(tmdb_id)
        destination_url = destination_urls[0] if destination_urls else ""

        source = str(result.get("source") or "")

        raw_title = str(result.get("title") or "")
        raw_content = str(result.get("content") or "")
        # Metadata clues must come only from the Lens result title/content.
        # Never treat the uploaded poster filename or image URL as film metadata.
        raw_text = " ".join((raw_title, raw_content))
        result_years: list[int] = []
        result_countries: list[str] = []
        result_directors: list[str] = []
        result_cast: list[str] = []
        result_characters: list[str] = []
        result_ratings: list[dict] = []

        # A year in the Lens result title is a clue. In result content, accept
        # only explicitly labelled film/release years to avoid article publish dates.
        content_years = re.findall(
            r"\b(?:release year|film year|year released|release date)\s*[:=–-]?\s*((?:19|20)\d{2})",
            raw_content,
            flags=re.IGNORECASE,
        )
        for year_match in [*YEAR_HINT_RE.findall(raw_title), *content_years]:
            year_value = int(year_match)
            if 1888 <= year_value <= 2035 and year_value not in result_years:
                result_years.append(year_value)
            if 1888 <= year_value <= 2035 and year_value not in year_hints:
                year_hints.append(year_value)
        for country_match in COUNTRY_RE.findall(raw_content):
            value = country_match.strip(" .,")
            if value and value.casefold() not in {item.casefold() for item in result_countries}:
                result_countries.append(value)
            if value and value.casefold() not in {item.casefold() for item in country_hints}:
                country_hints.append(value)
        for director_match in DIRECTOR_RE.findall(raw_content):
            value = director_match.strip(" .,")
            if value and value.casefold() not in {item.casefold() for item in result_directors}:
                result_directors.append(value)
            if value and value.casefold() not in {item.casefold() for item in director_hints}:
                director_hints.append(value)
        for cast_match in CAST_RE.findall(raw_content):
            for person in re.split(r"\s*(?:,|;|\band\b|&)\s*", cast_match):
                person = person.strip(" .")
                if person and person.casefold() not in {item.casefold() for item in result_cast}:
                    result_cast.append(person)
        for character_match in CHARACTER_RE.findall(raw_content):
            value = character_match.strip(" .,")
            if value and value.casefold() not in {item.casefold() for item in result_characters}:
                result_characters.append(value)
        for actor_name, character_name in AS_CHARACTER_RE.findall(raw_content):
            actor_name = actor_name.strip(" .,")
            character_name = character_name.strip(" .,")
            if actor_name and actor_name.casefold() not in {item.casefold() for item in result_cast}:
                result_cast.append(actor_name)
            if character_name and character_name.casefold() not in {item.casefold() for item in result_characters}:
                result_characters.append(character_name)
        for character_name, actor_name in CHARACTER_ACTOR_RE.findall(raw_content):
            character_name = character_name.strip(" .,")
            actor_name = actor_name.strip(" .,")
            if character_name and character_name.casefold() not in {item.casefold() for item in result_characters}:
                result_characters.append(character_name)
            if actor_name and actor_name.casefold() not in {item.casefold() for item in result_cast}:
                result_cast.append(actor_name)

        for rating_source, rating_value in RATING_RE.findall(raw_content):
            try:
                numeric_rating = float(rating_value)
            except (TypeError, ValueError):
                continue
            if 0 <= numeric_rating <= 10:
                result_ratings.append({
                    "source": (rating_source or "unspecified").upper(),
                    "value": numeric_rating,
                })

        observation = {
            "lens_title": title or "",
            "result_title": raw_title[:180],
            "result_url": (destination_urls[0] if destination_urls else str(result.get("resolved_url") or result.get("url") or ""))[:500],
            "content": raw_content[:700],
            "years": result_years[:8],
            "countries": result_countries[:8],
            "directors": result_directors[:8],
            "cast": result_cast[:12],
            "characters": result_characters[:12],
            "ratings": result_ratings[:5],
        }

        if len(backlinks) < MAX_BACKLINKS:
            backlinks.append({
                "title": raw_title[:180],
                "lens_title": title or "",
                "metadata_clues": observation,
                "url": str(result.get("url") or "")[:500],
                "resolved_url": destination_url[:1000],
                "resolved_urls": destination_urls[:10],
                "url_candidates": [
                    str(value)[:500]
                    for value in (result.get("url_candidates") or [])[:10]
                    if isinstance(value, str)
                ],
                "provider": str(result.get("provider") or "")[:80],
                "search_query": str(result.get("search_query") or "")[:700],
                "url_resolution_error": str(result.get("url_resolution_error") or "")[:200],
                "source": source[:500],
                "content": raw_content[:500],
                "imdb_ids": linked_imdb_ids,
                "tmdb_ids": linked_tmdb_ids,
            })

    # Lens often decorates a real movie title with retailer, disc-format,
    # director or cast names. Repeated contiguous phrases across independent
    # visual-match titles are stronger query hints than one long noisy caption.
    phrase_rows: dict[str, dict[str, object]] = {}
    for row in lens_query_rows:
        words = row.split()
        seen_in_row: set[str] = set()
        # Two-word fragments are too generic ("de souffle", "bout de");
        # keep repeated 3-4 word phrases so the canonical title survives the cap.
        for width in range(3, min(4, len(words)) + 1):
            for start in range(0, len(words) - width + 1):
                phrase = " ".join(words[start:start + width]).strip()
                key = _clean_hint(phrase)
                if not key:
                    continue
                normalized_key = " ".join(key.casefold().split())
                if normalized_key in seen_in_row:
                    continue
                seen_in_row.add(normalized_key)
                entry = phrase_rows.setdefault(
                    normalized_key,
                    {"count": 0, "prefix_count": 0, "phrase": key},
                )
                entry["count"] = int(entry["count"]) + 1
                if start == 0:
                    entry["prefix_count"] = int(entry["prefix_count"]) + 1

    repeated_phrases = [
        entry for entry in phrase_rows.values()
        if int(entry["count"]) >= 2 and int(entry["prefix_count"]) >= 1
    ]
    repeated_phrases.sort(
        key=lambda entry: (
            -int(entry["prefix_count"]),
            -int(entry["count"]),
            -len(str(entry["phrase"]).split()),
            -len(str(entry["phrase"])),
        )
    )
    query_candidates = [
        str(entry["phrase"]) for entry in repeated_phrases
    ]
    # Keep a clean short result title as a fallback, but don't send long
    # captions containing director/cast/retailer names as if they were titles.
    query_candidates.extend(
        title for title in _unique_titles(lens_query_rows)
        if len(title.split()) <= 5
    )
    seen_queries: set[str] = set()
    for candidate_title in query_candidates:
        query_key = " ".join(candidate_title.casefold().split())
        if query_key and query_key not in seen_queries:
            seen_queries.add(query_key)
            lens_title_queries.append(candidate_title)
        if len(lens_title_queries) >= MAX_LENS_TITLE_QUERIES:
            break

    return {
        "title_hints": hints[:80],
        "lens_title_queries": lens_title_queries[:MAX_LENS_TITLE_QUERIES],
        "imdb_ids": imdb_ids[:80],
        "tmdb_ids": tmdb_ids[:80],
        "year_hints": year_hints[:80],
        "country_hints": country_hints[:80],
        "director_hints": director_hints[:80],
        "metadata_observations": [
            item.get("metadata_clues")
            for item in backlinks
            if isinstance(item.get("metadata_clues"), dict)
        ],
        "backlinks": backlinks,
    }
