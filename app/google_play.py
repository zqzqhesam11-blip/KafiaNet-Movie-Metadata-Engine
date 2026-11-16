from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from app.image_evidence import GOOGLE_PLAY_MOVIES_ID_RE


GOOGLE_PLAY_MOVIE_URL = "https://play.google.com/store/movies/details"
MAX_GOOGLE_PLAY_PAGE_BYTES = 2_000_000
MAX_GOOGLE_PLAY_REDIRECTS = 3
DEFAULT_GOOGLE_PLAY_TIMEOUT = 20.0

_DESCRIPTION_STOP_WORDS = {
    "about", "after", "again", "against", "also", "among", "and", "are",
    "around", "back", "because", "been", "before", "being", "between",
    "but", "can", "could", "did", "does", "down", "during", "each", "even",
    "ever", "every", "for", "from", "further", "had", "has", "have", "her",
    "here", "him", "his", "how", "into", "its", "just", "like", "made",
    "make", "many", "more", "most", "much", "must", "near", "not", "now",
    "off", "once", "only", "other", "our", "out", "over", "own", "same",
    "she", "should", "since", "some", "such", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "through",
    "too", "under", "until", "very", "was", "were", "what", "when", "where",
    "which", "while", "who", "will", "with", "would", "you", "your",
    "movie", "film", "films", "movies",
}


class GooglePlayMetadataError(RuntimeError):
    """Safe, bounded error while reading an official Google Play movie page."""


class _GooglePlayHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.itemprops: dict[str, list[str]] = {}
        self.json_ld: list[str] = []
        self.title_parts: list[str] = []
        self.visible_text: list[str] = []
        self._in_title = False
        self._in_json_ld = False
        self._script_parts: list[str] = []
        self._itemprop_stack: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        tag_name = tag.casefold()
        if tag_name == "meta":
            content = values.get("content", "").strip()
            if content:
                for key in ("property", "name", "itemprop"):
                    name = values.get(key, "").strip().casefold()
                    if name:
                        self.meta.setdefault(name, []).append(content)
                        if key == "itemprop":
                            self.itemprops.setdefault(name, []).append(content)
        if tag_name == "title":
            self._in_title = True
        if tag_name == "script":
            self._in_json_ld = "ld+json" in values.get("type", "").casefold()
            self._script_parts = []
        itemprop = values.get("itemprop", "").strip().casefold()
        if itemprop and tag_name not in {"meta", "script"}:
            self._itemprop_stack.append((tag_name, itemprop))
            value = values.get("content") or values.get("datetime") or values.get("value")
            if value:
                self.itemprops.setdefault(itemprop, []).append(value.strip())

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.casefold()
        if tag_name == "title":
            self._in_title = False
        if tag_name == "script":
            if self._in_json_ld:
                value = "".join(self._script_parts).strip()
                if value:
                    self.json_ld.append(value)
            self._in_json_ld = False
            self._script_parts = []
        for index in range(len(self._itemprop_stack) - 1, -1, -1):
            if self._itemprop_stack[index][0] == tag_name:
                del self._itemprop_stack[index:]
                break

    def handle_data(self, data: str) -> None:
        value = str(data or "").strip()
        if not value:
            return
        if not self._in_json_ld:
            self.visible_text.append(value)
        if self._in_title:
            self.title_parts.append(value)
        if self._in_json_ld:
            self._script_parts.append(data)
        for _tag, itemprop in self._itemprop_stack:
            self.itemprops.setdefault(itemprop, []).append(value)


def _clean_text(value: Any, limit: int = 3000) -> str:
    text = unescape(" ".join(str(value or "").split())).strip()
    return text[:limit]


def _iter_dicts(value: Any, depth: int = 0):
    if depth > 10:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:100]:
            yield from _iter_dicts(child, depth + 1)


def _named_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, str):
        text = _clean_text(value, 180)
        if text:
            values.append(text)
    elif isinstance(value, dict):
        name = _clean_text(value.get("name"), 180)
        if name:
            values.append(name)
    elif isinstance(value, list):
        for row in value[:30]:
            values.extend(_named_values(row))
    return list(dict.fromkeys(values))


def _first_value(objects: list[dict[str, Any]], *keys: str) -> Any:
    for obj in objects:
        for key in keys:
            value = obj.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _parse_year(value: Any) -> int | None:
    if isinstance(value, int) and 1870 <= value <= 2100:
        return value
    match = re.search(r"(?<!\d)((?:18|19|20)\d{2})(?:-\d{2}(?:-\d{2})?)?(?!\d)", str(value or ""))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1870 <= year <= 2100 else None


def parse_runtime_minutes(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = int(value)
        return number if 1 <= number <= 600 else None
    text = str(value or "").strip()
    if not text:
        return None
    iso = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text, flags=re.I)
    if iso:
        hours = int(iso.group(1) or 0)
        minutes = int(iso.group(2) or 0)
        seconds = int(iso.group(3) or 0)
        total = hours * 60 + minutes + (1 if seconds >= 30 else 0)
        return total if 1 <= total <= 600 else None
    compact_hours = re.fullmatch(r"\s*(\d+)\s*h\s*(?:(\d+)\s*m)?\s*", text, flags=re.I)
    if compact_hours:
        total = int(compact_hours.group(1)) * 60 + int(compact_hours.group(2) or 0)
        return total if 1 <= total <= 600 else None
    hours = re.search(r"(\d+)\s*(?:h|hr|hrs|hour|hours)\b", text, flags=re.I)
    minutes = re.search(r"(\d+)\s*(?:m|min|mins|minute|minutes)\b", text, flags=re.I)
    total = (int(hours.group(1)) * 60 if hours else 0) + (
        int(minutes.group(1)) if minutes else 0
    )
    if total:
        return total if 1 <= total <= 600 else None
    if re.fullmatch(r"\d{1,3}", text):
        total = int(text)
        return total if 1 <= total <= 600 else None
    return None


def parse_google_play_movie_html(
    movie_id: str,
    body: str,
    *,
    page_url: str | None = None,
) -> dict[str, Any]:
    """Extract movie metadata from Google Play's structured page data and meta tags.

    The external ID is supplied by the caller and is never inferred from a title,
    path slug, or arbitrary value embedded in the HTML.
    """
    external_id = str(movie_id or "").strip()
    if not GOOGLE_PLAY_MOVIES_ID_RE.fullmatch(external_id):
        raise ValueError("A valid Google Play Movies ID is required")
    raw_html = str(body or "")
    if len(raw_html.encode("utf-8", errors="ignore")) > MAX_GOOGLE_PLAY_PAGE_BYTES:
        raise GooglePlayMetadataError("Google Play movie page exceeded the size limit")

    parser = _GooglePlayHTMLParser()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception as exc:
        raise GooglePlayMetadataError(
            f"Google Play movie page could not be parsed ({type(exc).__name__})"
        ) from exc

    structured_objects: list[dict[str, Any]] = []
    for script in parser.json_ld:
        try:
            payload = json.loads(script)
        except (TypeError, ValueError):
            continue
        for obj in _iter_dicts(payload):
            raw_type = obj.get("@type") or obj.get("type") or ""
            types = [raw_type] if isinstance(raw_type, str) else raw_type if isinstance(raw_type, list) else []
            if any(
                str(value).rstrip("/").rsplit("/", 1)[-1].casefold()
                in {"movie", "creativework", "videoobject"}
                for value in types
            ):
                structured_objects.append(obj)
    if not structured_objects:
        for script in parser.json_ld:
            try:
                payload = json.loads(script)
            except (TypeError, ValueError):
                continue
            structured_objects.extend(
                obj for obj in _iter_dicts(payload)
                if obj.get("name") and obj.get("description")
            )

    title_raw = (
        _first_value(structured_objects, "name", "headline")
        or next(iter(parser.meta.get("og:title", [])), "")
        or next(iter(parser.itemprops.get("name", [])), "")
        or " ".join(parser.title_parts)
    )
    title = _clean_text(title_raw, 240)
    title = re.sub(
        r"\s*[-|]\s*(?:movies?\s+on\s+)?google\s+play\s*$",
        "",
        title,
        flags=re.I,
    ).strip()

    description_raw = (
        _first_value(structured_objects, "description", "abstract")
        or next(iter(parser.meta.get("og:description", [])), "")
        or next(iter(parser.meta.get("description", [])), "")
        or next(iter(parser.itemprops.get("description", [])), "")
    )
    description = _clean_text(description_raw, 3000)

    year_raw = _first_value(
        structured_objects,
        "datePublished", "releaseDate", "dateCreated", "publicationDate", "year",
    )
    if year_raw is None:
        year_raw = next(iter(
            parser.itemprops.get("datepublished", [])
            or parser.itemprops.get("releasedate", [])
            or parser.meta.get("datepublished", [])
        ), None)
    if year_raw is None:
        for key in ("datePublished", "releaseDate", "publishedDate", "releaseYear"):
            match = re.search(
                rf'"{re.escape(key)}"\s*:\s*["\']?((?:18|19|20)\d{{2}}(?:-\d{{2}}(?:-\d{{2}})?)?)',
                raw_html,
                flags=re.I,
            )
            if match:
                year_raw = match.group(1)
                break
    year = _parse_year(year_raw)

    visible_text = " ".join(parser.visible_text)
    visible_card = re.search(
        r"(?<!\d)((?:18|19|20)\d{2})\s*[•·|–—-]\s*(\d{1,3})\s*(?:minutes?|mins?)\b",
        visible_text,
        flags=re.I,
    )
    if visible_card:
        year = year or _parse_year(visible_card.group(1))

    runtime_raw = _first_value(
        structured_objects,
        "duration", "runtime", "durationMinutes", "runtimeMinutes",
    )
    if runtime_raw is None:
        runtime_raw = next(iter(
            parser.itemprops.get("duration", [])
            or parser.itemprops.get("runtime", [])
        ), None)
    if runtime_raw is None:
        for key in ("duration", "runtime", "durationMinutes", "runtimeMinutes"):
            match = re.search(
                rf'"{re.escape(key)}"\s*:\s*"?(PT[\dHMS]+|\d{{1,3}}(?:\s*(?:min|minutes|m))?)',
                raw_html,
                flags=re.I,
            )
            if match:
                runtime_raw = match.group(1)
                break
    runtime_minutes = parse_runtime_minutes(runtime_raw)
    if runtime_minutes is None and visible_card:
        runtime_minutes = parse_runtime_minutes(visible_card.group(2))

    director_values: list[str] = []
    cast_values: list[str] = []
    genre_values: list[str] = []
    for obj in structured_objects:
        director_values.extend(_named_values(obj.get("director")))
        cast_values.extend(_named_values(obj.get("actor") or obj.get("actors")))
        genre_values.extend(_named_values(obj.get("genre")))
    director_values.extend(parser.itemprops.get("director", []))
    cast_values.extend(parser.itemprops.get("actor", []))
    genre_values.extend(parser.itemprops.get("genre", []))

    return {
        "provider": "google_play_movie_page",
        "status": "ok" if title and description else "incomplete",
        "google_play_id": external_id,
        "title": title or None,
        "description": description or None,
        "year": year,
        "runtime_minutes": runtime_minutes,
        "directors": list(dict.fromkeys(_clean_text(value, 180) for value in director_values if _clean_text(value, 180)))[:8],
        "cast": list(dict.fromkeys(_clean_text(value, 180) for value in cast_values if _clean_text(value, 180)))[:16],
        "genres": list(dict.fromkeys(_clean_text(value, 100) for value in genre_values if _clean_text(value, 100)))[:12],
        "page_url": page_url or None,
        "identity_evidence": False,
        "automatic_match_allowed": False,
    }


class GooglePlayMetadataClient:
    """Fetch metadata from official Google Play movie pages with bounded redirects."""

    def __init__(self, timeout: float = DEFAULT_GOOGLE_PLAY_TIMEOUT):
        self.client = httpx.Client(
            timeout=max(5.0, float(timeout)),
            follow_redirects=False,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
            },
        )

    @staticmethod
    def _is_allowed_page_url(value: str, expected_id: str) -> bool:
        try:
            parsed = urlparse(str(value or "").strip())
            port = parsed.port
        except ValueError:
            return False
        host = (parsed.hostname or "").casefold().rstrip(".")
        query = parse_qs(parsed.query)
        page_id = str((query.get("id") or [""])[0]).strip()
        return (
            parsed.scheme == "https"
            and host == "play.google.com"
            and port in {None, 443}
            and not parsed.username
            and not parsed.password
            and parsed.path.rstrip("/") == "/store/movies/details"
            and page_id == expected_id
        )

    def fetch_movie_metadata(self, movie_id: str) -> dict[str, Any]:
        external_id = str(movie_id or "").strip()
        if not GOOGLE_PLAY_MOVIES_ID_RE.fullmatch(external_id):
            raise ValueError("A valid Google Play Movies ID is required")

        current_url = f"{GOOGLE_PLAY_MOVIE_URL}?{urlencode({'id': external_id, 'hl': 'en_US', 'gl': 'US'})}"
        response = None
        for redirect_count in range(MAX_GOOGLE_PLAY_REDIRECTS + 1):
            try:
                response = self.client.get(current_url, follow_redirects=False)
            except httpx.RequestError as exc:
                raise GooglePlayMetadataError(
                    f"Google Play page request failed ({type(exc).__name__})"
                ) from exc

            status = int(getattr(response, "status_code", 0) or 0)
            if status in {301, 302, 303, 307, 308}:
                location = (getattr(response, "headers", {}) or {}).get("location")
                if not location or redirect_count >= MAX_GOOGLE_PLAY_REDIRECTS:
                    raise GooglePlayMetadataError("Google Play page redirect limit reached")
                next_url = urljoin(current_url, str(location).strip())
                if not self._is_allowed_page_url(next_url, external_id):
                    raise GooglePlayMetadataError("Google Play page redirected outside its official movie URL")
                current_url = next_url
                continue
            if status != 200:
                raise GooglePlayMetadataError(f"Google Play page returned HTTP {status}")
            break

        if response is None or int(getattr(response, "status_code", 0) or 0) != 200:
            raise GooglePlayMetadataError("Google Play page request did not complete")
        if not self._is_allowed_page_url(str(getattr(response, "url", current_url)), external_id):
            raise GooglePlayMetadataError("Google Play page response URL did not match the requested listing")
        content = bytes(getattr(response, "content", b"") or b"")
        if len(content) > MAX_GOOGLE_PLAY_PAGE_BYTES:
            raise GooglePlayMetadataError("Google Play movie page exceeded the size limit")
        body = str(getattr(response, "text", "") or "")
        if not body and content:
            body = content.decode("utf-8", errors="replace")
        return parse_google_play_movie_html(
            external_id,
            body,
            page_url=str(getattr(response, "url", current_url)),
        )

    def close(self) -> None:
        self.client.close()


def _normalized_text(value: Any) -> str:
    import unicodedata

    text = unescape(" ".join(str(value or "").split())).casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def _title_tokens(value: Any) -> set[str]:
    return {
        token for token in re.findall(r"[^\W_]+", _normalized_text(value), flags=re.UNICODE)
        if len(token) > 2 and token not in _DESCRIPTION_STOP_WORDS
    }


def _phrase_in_text(phrase: Any, text: Any) -> bool:
    key = _normalized_text(phrase)
    haystack = _normalized_text(text)
    if len(key) < 5 or not haystack:
        return False
    return f" {key} " in f" {haystack} "


def evaluate_google_play_tmdb_candidate(
    metadata: dict[str, Any],
    tmdb_details: dict[str, Any],
) -> dict[str, Any]:
    """Require exact title agreement plus independent page/detail corroboration."""
    title = str(metadata.get("title") or "").strip()
    description = str(metadata.get("description") or "").strip()
    aliases = [
        tmdb_details.get("title"),
        tmdb_details.get("original_title"),
    ]
    aliases.extend(
        row.get("title")
        for row in (tmdb_details.get("alternative_titles") or [])
        if isinstance(row, dict)
    )
    metadata_title_key = _normalized_text(title)
    title_matches = bool(metadata_title_key) and any(
        _normalized_text(alias) == metadata_title_key
        for alias in aliases if str(alias or "").strip()
    )

    tmdb_overview = str(tmdb_details.get("overview") or "").strip()
    page_terms = _title_tokens(description)
    tmdb_terms = _title_tokens(tmdb_overview)
    shared_terms = sorted(page_terms & tmdb_terms)
    denominator = len(page_terms) + len(tmdb_terms)
    description_dice = (2 * len(shared_terms) / denominator) if denominator else 0.0
    description_match = bool(
        len(shared_terms) >= 4
        and description_dice >= 0.16
    )

    cast_rows = [
        row for row in (tmdb_details.get("cast") or [])
        if isinstance(row, dict)
    ]
    role_matches = sorted({
        str(row.get("character") or "").strip()
        for row in cast_rows
        if _phrase_in_text(row.get("character"), description)
    })
    cast_name_matches = sorted({
        str(row.get("name") or "").strip()
        for row in cast_rows
        if _phrase_in_text(row.get("name"), description)
    })
    character_match = bool(role_matches)
    cast_description_match = bool(cast_name_matches)

    page_directors = {_normalized_text(value) for value in (metadata.get("directors") or []) if value}
    tmdb_directors = {
        _normalized_text(row.get("name"))
        for row in (tmdb_details.get("directors") or [])
        if isinstance(row, dict) and row.get("name")
    }
    director_match = bool(page_directors & tmdb_directors)

    page_cast = {_normalized_text(value) for value in (metadata.get("cast") or []) if value}
    tmdb_cast = {
        _normalized_text(row.get("name"))
        for row in cast_rows if row.get("name")
    }
    cast_match = bool(page_cast & tmdb_cast)

    raw_page_year = metadata.get("year")
    raw_tmdb_year = tmdb_details.get("year")
    try:
        page_year = int(raw_page_year) if raw_page_year is not None else None
        tmdb_year = int(raw_tmdb_year) if raw_tmdb_year is not None else None
    except (TypeError, ValueError):
        page_year = tmdb_year = None
    year_delta = abs(page_year - tmdb_year) if page_year and tmdb_year else None
    year_match = year_delta is not None and year_delta <= 1

    try:
        page_runtime = int(metadata.get("runtime_minutes")) if metadata.get("runtime_minutes") is not None else None
        tmdb_runtime = int(tmdb_details.get("runtime")) if tmdb_details.get("runtime") is not None else None
    except (TypeError, ValueError):
        page_runtime = tmdb_runtime = None
    runtime_delta = abs(page_runtime - tmdb_runtime) if page_runtime and tmdb_runtime else None
    runtime_match = runtime_delta is not None and runtime_delta <= 5

    conflicts = []
    if year_delta is not None and year_delta > 2:
        conflicts.append("release_year_difference_over_2")
    if runtime_delta is not None and runtime_delta > 8:
        conflicts.append("runtime_difference_over_8_minutes")

    signals = {
        "description_match": description_match,
        "character_match": character_match,
        "cast_description_match": cast_description_match,
        "director_match": director_match,
        "cast_match": cast_match,
        "year_match": year_match,
        "runtime_match": runtime_match,
    }
    signal_count = sum(1 for value in signals.values() if value)
    narrative_match = description_match or character_match
    accepted = bool(
        title_matches
        and not conflicts
        and narrative_match
        and signal_count >= 2
    )

    return {
        "tmdb_id": tmdb_details.get("tmdb_id") or tmdb_details.get("id"),
        "title": tmdb_details.get("title"),
        "original_title": tmdb_details.get("original_title"),
        "year": tmdb_details.get("year"),
        "runtime": tmdb_details.get("runtime"),
        "title_match": title_matches,
        "description_match": description_match,
        "character_match": character_match,
        "cast_description_match": cast_description_match,
        "director_match": director_match,
        "cast_match": cast_match,
        "year_match": year_match,
        "runtime_match": runtime_match,
        "description_overlap": round(description_dice, 4),
        "description_shared_terms": shared_terms[:30],
        "matched_character_roles": role_matches[:8],
        "matched_cast_names": cast_name_matches[:8],
        "director_match": director_match,
        "cast_match": cast_match,
        "year_delta": year_delta,
        "runtime_delta": runtime_delta,
        "signals": signals,
        "signal_count": signal_count,
        "conflicts": conflicts,
        "accepted": accepted,
    }
