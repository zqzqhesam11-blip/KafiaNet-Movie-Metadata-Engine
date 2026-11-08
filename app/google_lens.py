from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlencode, urlparse

import httpx

from app.image_evidence import _resolved_destination_url


BRIGHTDATA_REQUEST_URL = "https://api.brightdata.com/request"
GOOGLE_LENS_URL = "https://lens.google.com/uploadbyurl"
GOOGLE_SEARCH_URL = "https://www.google.com/search"
LENS_TAB = "exact_matches"
LENS_FALLBACK_TAB = "visual_matches"
MAX_NORMALIZED_RESULTS = 80
MAX_TRANSIENT_RETRIES = 2
MAX_RATE_LIMIT_RETRIES = 1
MAX_RATE_LIMIT_WAIT_SECONDS = 30
TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}


class BrightDataAPIError(RuntimeError):
    """Safe, actionable error from Bright Data or Google Lens."""


class BrightDataQuotaError(BrightDataAPIError):
    """Provider quota exhaustion or throttling; callers must stop the current batch."""

    def __init__(self, status_code: int, message: str):
        self.status_code = int(status_code)
        super().__init__(message)


class BrightDataGoogleLensClient:
    """Google Lens reverse-image search through Bright Data's SERP API."""

    def __init__(self, api_key: str, zone: str, timeout: float = 60.0):
        key = str(api_key or "").strip()
        zone_name = str(zone or "").strip()
        if not key:
            raise RuntimeError("BRIGHTDATA_API_KEY is required")
        if not zone_name:
            raise RuntimeError("BRIGHTDATA_SERP_ZONE is required")
        self.zone = zone_name
        self.client = httpx.Client(
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=max(15.0, float(timeout)),
        )
        self.last_lens_diagnostics: dict[str, Any] = {}
        self.last_official_search_diagnostics: dict[str, Any] = {}

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _is_google_goto_url(value: str) -> bool:
        try:
            parsed = urlparse(str(value or "").strip())
            host = (parsed.hostname or "").casefold().rstrip(".")
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and (host == "google.com" or host.endswith(".google.com"))
            and parsed.path.rstrip("/") == "/goto"
        )

    def resolve_result_urls(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep direct page URLs and quarantine undocumented Google /goto wrappers.

        Bright Data documents parsed Lens results, but Google does not publish a
        /goto resolution API. In our live run every Lens /goto returned HTTP 404
        for both HEAD and GET, without Location. Do not repeat those requests or
        treat wrapper URLs as movie identity. Only direct HTTP(S) result URLs are
        eligible for official IMDb/TMDB identifier extraction.
        """
        if not isinstance(getattr(self, "last_lens_diagnostics", None), dict):
            self.last_lens_diagnostics = {}

        prepared = [dict(item) for item in (results or []) if isinstance(item, dict)]
        goto_skipped = 0
        direct_urls_kept = 0

        for item in prepared:
            candidates: list[str] = []
            for key in ("url_candidates", "resolved_urls"):
                values = item.get(key)
                if isinstance(values, list):
                    candidates.extend(
                        str(value).strip()
                        for value in values
                        if isinstance(value, str) and str(value).strip()
                    )
            for key in ("url", "resolved_url"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())

            unique_candidates = list(dict.fromkeys(candidates))
            direct_destinations: list[str] = []
            for candidate in unique_candidates:
                try:
                    parsed = urlparse(candidate)
                except ValueError:
                    continue
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    continue
                if self._is_google_goto_url(candidate):
                    goto_skipped += 1
                    continue
                direct_destinations.append(candidate)

            direct_destinations = list(dict.fromkeys(direct_destinations))
            direct_urls_kept += len(direct_destinations)
            item["url_candidates"] = unique_candidates
            item["resolved_urls"] = direct_destinations
            item["resolved_url"] = direct_destinations[0] if direct_destinations else ""
            if any(self._is_google_goto_url(value) for value in unique_candidates):
                item["url_resolution_status"] = (
                    "direct_urls_kept_goto_skipped"
                    if direct_destinations
                    else "google_goto_skipped_no_documented_destination"
                )
            else:
                item.pop("url_resolution_status", None)
            # Legacy error fields from earlier resolver versions must not survive.
            item.pop("url_resolution_error", None)
            item.pop("url_resolution_errors", None)

        self.last_lens_diagnostics.update({
            "google_goto_candidates": goto_skipped,
            "google_goto_resolved": 0,
            "google_goto_failed": 0,
            "google_goto_skipped": goto_skipped,
            "google_goto_direct_urls_kept": direct_urls_kept,
            "google_goto_http_statuses": {},
            "google_goto_methods": {},
            "google_goto_error_kinds": {},
            "google_goto_error_samples": [],
        })
        return prepared

    @staticmethod
    def _lens_request_url(image_url: str, tab: str = LENS_TAB) -> str:
        if tab not in {LENS_TAB, LENS_FALLBACK_TAB}:
            raise BrightDataAPIError(f"Unsupported Google Lens tab: {tab}")
        parsed = urlparse(str(image_url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise BrightDataAPIError(
                "Google Lens requires a public HTTPS poster URL without embedded credentials"
            )
        query = {
            "url": image_url,
            "hl": "en",
            "brd_json": "1",
            "brd_lens": tab,
        }
        return f"{GOOGLE_LENS_URL}?{urlencode(query)}"

    @staticmethod
    def _error_detail(payload: Any, body: str = "") -> str:
        if isinstance(payload, dict):
            for key in ("message", "error", "description", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:300]
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                return "; ".join(str(x) for x in errors)[:300]
        return str(body or "")[:300]

    @staticmethod
    def _payload_shape(payload: Any) -> str:
        """Describe JSON keys and value types without logging URLs or response contents."""
        if not isinstance(payload, dict):
            return f"root_type={type(payload).__name__}"
        parts: list[str] = []
        for key, value in list(payload.items())[:16]:
            if isinstance(value, dict):
                nested_keys = ",".join(str(item) for item in list(value.keys())[:8])
                parts.append(f"{key}=object[{nested_keys}]")
            elif isinstance(value, list):
                parts.append(f"{key}=array[{len(value)}]")
            else:
                parts.append(f"{key}={type(value).__name__}")
        return "top_level=" + ";".join(parts)

    @staticmethod
    def _raw_result_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Summarize provider field names and URL paths without exposing query tokens."""
        field_names: set[str] = set()
        field_types: dict[str, set[str]] = {}
        path_counts: dict[str, int] = {}
        url_fields = {
            "link", "url", "page_url", "website", "source_url", "landing_page",
            "result_url", "destination_url", "target_url", "href", "imgrefurl",
            "source", "original_image", "image_url", "image", "thumbnail",
            "thumbnail_url",
        }

        def collect_strings(value: Any):
            if isinstance(value, str):
                yield value
            elif isinstance(value, list):
                for child in value[:20]:
                    yield from collect_strings(child)
            elif isinstance(value, dict):
                for name in ("url", "link", "href", "src", "target", "original_url"):
                    if name in value:
                        yield from collect_strings(value[name])

        for row in rows[:MAX_NORMALIZED_RESULTS]:
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                name = str(key)
                field_names.add(name)
                value_type = (
                    "object" if isinstance(value, dict)
                    else "array" if isinstance(value, list)
                    else "null" if value is None
                    else type(value).__name__
                )
                field_types.setdefault(name, set()).add(value_type)
                if name.casefold() not in url_fields:
                    continue
                for candidate in collect_strings(value):
                    try:
                        parsed = urlparse(candidate.strip())
                    except ValueError:
                        continue
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        continue
                    host = (parsed.hostname or "").casefold().rstrip(".")
                    path = parsed.path.rstrip("/") or "/"
                    label = f"{host}{path}"
                    path_counts[label] = path_counts.get(label, 0) + 1

        top_paths = dict(
            sorted(path_counts.items(), key=lambda item: (-item[1], item[0]))[:12]
        )
        compact_types = {
            key: "/".join(sorted(types))
            for key, types in sorted(field_types.items())[:30]
        }
        return {
            "raw_result_fields": sorted(field_names)[:40],
            "raw_result_field_types": compact_types,
            "raw_link_paths": top_paths,
        }

    @staticmethod
    def _rows_from_payload(payload: Any, depth: int = 0) -> list[dict[str, Any]] | None:
        """Extract result rows from Bright Data's JSON response and known wrappers.

        SERP API responses can be returned directly or inside JSON-encoded
        body/data envelopes. Key casing and wrapper depth have varied between
        API response modes, so normalize keys before looking for organic rows.
        The final movie-identity filter remains separate and accepts only
        official TMDB/IMDb destination URLs.
        """
        if depth > 10:
            return None

        if isinstance(payload, str):
            text = payload.strip().lstrip("\ufeff")
            # Some upstream JSON responses carry Google's anti-XSSI prefix.
            if text.startswith(")]}'"):
                text = text.split("\n", 1)[1].lstrip() if "\n" in text else ""
            if text.startswith(("{", "[")):
                try:
                    return BrightDataGoogleLensClient._rows_from_payload(
                        json.loads(text), depth + 1
                    )
                except (TypeError, ValueError):
                    return None
            return None

        if isinstance(payload, list):
            if all(isinstance(item, dict) for item in payload):
                return payload
            return None

        if not isinstance(payload, dict):
            return None

        def normalized_key(value: Any) -> str:
            return re.sub(r"[^a-z0-9]", "", str(value).casefold())

        result_keys = {
            "organic", "organicresults", "organicsearchresults",
            "webresults", "searchresults", "exactmatches",
            "visualmatches", "lensresults", "similarimages", "images",
            "resultitems", "items", "matches", "results",
        }
        wrapper_keys = {
            "data", "result", "response", "body", "content", "raw",
            "output", "payload", "lens", "serp", "search",
            "searchresponse", "responsebody", "parsed", "parsedresponse",
            "results",
        }
        normalized_items = [
            (normalized_key(key), value)
            for key, value in payload.items()
        ]

        # Prefer explicit organic/Lens collections at any casing or nesting
        # level before considering generic wrappers.
        for key, value in normalized_items:
            if key in result_keys and isinstance(value, list):
                if all(isinstance(item, dict) for item in value):
                    return value

        # Bright Data format=json may return a SERP object directly, while
        # some integrations wrap it as status_code/body JSON.
        for key, value in normalized_items:
            if key in wrapper_keys:
                if isinstance(value, (dict, list, str)):
                    rows = BrightDataGoogleLensClient._rows_from_payload(
                        value, depth + 1
                    )
                    if rows is not None:
                        return rows

        # Last-resort compatibility for provider versions that rename the
        # collection: only accept arrays that look like search-result rows.
        url_fields = {
            "link", "url", "href", "pageurl", "resulturl",
            "destinationurl", "targeturl", "landingpage",
        }
        for _, value in normalized_items:
            if not isinstance(value, list) or not value:
                continue
            if not all(isinstance(item, dict) for item in value):
                continue
            if any(
                any(normalized_key(field) in url_fields for field in item)
                and any(normalized_key(field) in {"title", "name"} for field in item)
                for item in value
            ):
                return value

        # Some provider envelopes introduce unlabelled object/string layers.
        # Explore object/string children only; never treat metadata arrays as
        # movie search results.
        for _, value in normalized_items:
            if isinstance(value, (dict, str)):
                rows = BrightDataGoogleLensClient._rows_from_payload(
                    value, depth + 1
                )
                if rows is not None:
                    return rows
        return None

    @staticmethod
    def _first_url(item: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = item.get(key)
            if isinstance(value, dict):
                value = value.get("url") or value.get("link") or value.get("src")
            if isinstance(value, str):
                candidate = value.strip()
                parsed = urlparse(candidate)
                if parsed.scheme in {"http", "https"} and parsed.netloc:
                    return candidate[:1000]
        return ""

    @classmethod
    def _all_urls(cls, item: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
        """Collect all distinct page URLs exposed by a search result."""
        values: list[str] = []
        seen: set[str] = set()
        for key in keys:
            value = item.get(key)
            candidates = value if isinstance(value, list) else [value]
            for candidate_value in candidates:
                if isinstance(candidate_value, dict):
                    candidate_value = (
                        candidate_value.get("url")
                        or candidate_value.get("link")
                        or candidate_value.get("href")
                        or candidate_value.get("src")
                        or candidate_value.get("target")
                    )
                if not isinstance(candidate_value, str):
                    continue
                candidate = candidate_value.strip()
                parsed = urlparse(candidate)
                if parsed.scheme in {"http", "https"} and parsed.netloc and candidate not in seen:
                    seen.add(candidate)
                    values.append(candidate[:1000])
        return values

    @classmethod
    def _normalize_matches(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in rows[:MAX_NORMALIZED_RESULTS]:
            title = str(item.get("title") or item.get("name") or "").strip()[:300]
            page_urls = cls._all_urls(
                item, ("link", "url", "page_url", "website", "source_url", "landing_page", "result_url", "destination_url", "target_url", "href", "imgrefurl")
            )
            page_url = page_urls[0] if page_urls else ""
            # Bright Data's image schema uses original_image for the full-size
            # image and image for its preview; keep the full-size URL first.
            source_url = cls._first_url(item, ("source",))
            source_is_image = False
            if source_url:
                source_path = urlparse(source_url).path.casefold()
                image_suffixes = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif")
                source_is_image = (
                    source_path.endswith(image_suffixes)
                    or "/t/p/" in source_path
                )
            # Bright Data's "source" commonly points to the original full-size
            # image, so preserve its precedence over the preview/thumbnail.
            image_url = source_url if source_is_image else cls._first_url(
                item,
                ("original_image", "image_url", "original", "image", "thumbnail", "thumbnail_url"),
            )
            if source_url and not source_is_image and source_url not in page_urls:
                page_urls.append(source_url)
                page_url = page_urls[0] if page_urls else source_url
            if not page_url and image_url:
                page_url = image_url
            description = str(
                item.get("description") or item.get("snippet") or item.get("text") or item.get("content") or ""
            ).strip()[:700]
            if not title and not page_url and not image_url:
                continue
            normalized.append({
                "title": title,
                "url": page_url,
                "url_candidates": page_urls or ([page_url] if page_url else []),
                "source": image_url,
                "content": description,
                "provider": "google_lens_brightdata",
                "position": item.get("position") or item.get("global_rank") or item.get("rank"),
            })
        return normalized

    @staticmethod
    def _official_movie_url(value: str) -> bool:
        try:
            parsed = urlparse(str(value or "").strip())
            host = (parsed.hostname or "").casefold().rstrip(".")
            path = parsed.path.rstrip("/").casefold()
        except ValueError:
            return False
        if host == "imdb.com" or host.endswith(".imdb.com"):
            return bool(re.search(r"/title/tt\d{5,12}(?:/|$)", parsed.path, flags=re.I))
        if host == "themoviedb.org" or host.endswith(".themoviedb.org"):
            return bool(re.match(r"^/movie/\d+(?:[-/]|$)", parsed.path, flags=re.I))
        if host in {"aficatalog.afi.com", "catalog.afi.com"}:
            return bool(path and path not in {"/", "/faq", "/behind-the-veil"})
        if host == "collections-search.bfi.org.uk":
            return path.startswith("/web/details/")
        return False

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        """Decode JSON whether the HTTP client or a wrapped text body supplies it."""
        try:
            return response.json()
        except (ValueError, TypeError):
            raw_text = str(getattr(response, "text", "") or "").strip().lstrip("\ufeff")
            if raw_text.startswith(")]}'"):
                raw_text = raw_text.split("\n", 1)[1].lstrip() if "\n" in raw_text else ""
            if raw_text.startswith(("{", "[")):
                try:
                    return json.loads(raw_text)
                except (TypeError, ValueError):
                    pass
            return None

    @staticmethod
    def _rate_limit_retry_delay(response: httpx.Response, payload: Any) -> int | None:
        """Return a short, explicit provider cooldown; never retry generic 429s blindly."""
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("retry-after") if hasattr(headers, "get") else None
        delays: list[int] = []

        if retry_after:
            try:
                delays.append(max(1, int(float(str(retry_after).strip()))))
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(str(retry_after))
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delays.append(max(1, int((retry_at - datetime.now(timezone.utc)).total_seconds())))
                except (TypeError, ValueError, OverflowError):
                    pass

        detail = BrightDataGoogleLensClient._error_detail(
            payload, str(getattr(response, "text", "") or "")
        )
        if isinstance(payload, dict) and payload.get("body"):
            detail += " " + str(payload.get("body"))[:500]
        match = re.search(
            r"(?:minimum\s+of|at\s+least|after\s+(?:a\s+)?minimum\s+of|retry\s+after|try\s+again\s+after)\s*(\d{1,3})\s*(?:seconds?|secs?)",
            detail,
            flags=re.I,
        )
        if match:
            delays.append(int(match.group(1)))
        elif "recently failed" in detail.casefold():
            # Bright Data's explicit same-query cooldown message specifies 15 seconds.
            delays.append(15)

        if not delays:
            return None
        delay = max(delays)
        if delay > MAX_RATE_LIMIT_WAIT_SECONDS:
            return None
        return delay

    def _post_serp_with_retries(
        self,
        target_url: str,
        stage: str,
        *,
        parsed_light_header: bool = True,
        response_format: str = "raw",
    ) -> tuple[httpx.Response, Any, int, int]:
        """POST one SERP request with bounded transport, gateway, and explicit cooldown retries.

        Generic 429 responses are not retried. A retry is allowed only when Bright Data
        supplies Retry-After or its explicit "query recently failed" cooldown message.
        """
        if response_format not in {"raw", "json"}:
            raise BrightDataAPIError(
                f"Unsupported Bright Data response format for {stage}: {response_format}"
            )

        attempt = 0
        transient_retries = 0
        rate_limit_retries = 0
        wait_seconds = 0
        self.last_request_diagnostics = {
            "attempts": 0,
            "rate_limit_retries": 0,
            "rate_limit_wait_seconds": 0,
        }

        while True:
            attempt += 1
            try:
                request_headers = (
                    {"x-unblock-data-format": "parsed_light"}
                    if parsed_light_header else None
                )
                response = self.client.post(
                    BRIGHTDATA_REQUEST_URL,
                    headers=request_headers,
                    json={
                        "zone": self.zone,
                        "url": target_url,
                        "format": response_format,
                    },
                )
            except httpx.TransportError as exc:
                if transient_retries >= MAX_TRANSIENT_RETRIES:
                    self.last_request_diagnostics.update({
                        "attempts": attempt,
                        "rate_limit_retries": rate_limit_retries,
                        "rate_limit_wait_seconds": wait_seconds,
                    })
                    raise BrightDataAPIError(
                        f"Bright Data {stage} transport failure after {attempt} "
                        f"attempt(s) ({type(exc).__name__}): {str(exc)[:220]}"
                    ) from exc
                time.sleep(min(2 ** transient_retries, 2))
                transient_retries += 1
                continue

            payload = self._safe_json(response)
            try:
                effective_status = int(response.status_code)
            except (TypeError, ValueError):
                effective_status = 0

            if (
                200 <= effective_status < 300
                and isinstance(payload, dict)
                and "status_code" in payload
                and "body" in payload
            ):
                try:
                    inner_status = int(payload.get("status_code") or 0)
                except (TypeError, ValueError):
                    inner_status = 0
                if inner_status:
                    effective_status = inner_status

            empty_success_body = (
                200 <= effective_status < 300
                and (
                    payload is None
                    or (
                        isinstance(payload, dict)
                        and "status_code" in payload
                        and "body" in payload
                        and not str(payload.get("body") or "").strip()
                    )
                )
            )

            if (
                (effective_status in TRANSIENT_HTTP_STATUSES or empty_success_body)
                and transient_retries < MAX_TRANSIENT_RETRIES
            ):
                time.sleep(min(2 ** transient_retries, 2))
                transient_retries += 1
                continue

            if effective_status == 429 and rate_limit_retries < MAX_RATE_LIMIT_RETRIES:
                delay = self._rate_limit_retry_delay(response, payload)
                if delay is not None:
                    rate_limit_retries += 1
                    wait_seconds += delay
                    time.sleep(delay)
                    continue

            self.last_request_diagnostics.update({
                "attempts": attempt,
                "rate_limit_retries": rate_limit_retries,
                "rate_limit_wait_seconds": wait_seconds,
            })
            return response, payload, effective_status, attempt


    @staticmethod
    def _clean_title_hint(value: str) -> tuple[str, int | None]:
        """Clean a Lens title without treating publisher names as part of the film title."""
        text = unquote(str(value or "")).replace("_", " ").replace("+", " ")
        text = text.replace("∣", "|")
        text = re.sub(r"^[^:]{2,24}\s*:\s*(?=[A-ZÀ-ž])", "", text)
        text = re.sub(r"\s*\|\s*(?:watch\s+(?:the\s+)?film\s+online|watch\s+online|official\s+trailer|full\s+movie).*$", "", text, flags=re.I)
        text = re.sub(
            r"\s+(?:on|at)\s+(?:cinobo|plex|prime\s+video|netflix|amazon\s+prime|"
            r"youtube|google\s+play|letterboxd)\b.*$",
            "",
            text,
            flags=re.I,
        )
        text = re.sub(
            r"\s*[-–—|:]\s*(?:IMDb|TMDB|The Movie Database|Wikipedia|Vikipedi|"
            r"YouTube|Prime Video|Netflix|Amazon Prime Video|Rotten Tomatoes|"
            r"Moviepilot|OFDb|NTV Haber|TikTok|Essential Cinema|The Disapproving Swede|"
            r"Cinobo|Plex|Filmweb|Letterboxd)\b.*$",
            "",
            text,
            flags=re.I,
        )
        # Keep titles, not catalogue/editorial fragments appended after a separator.
        text = re.sub(r"\s*[|]\s*(?:film|kritik|trailer|reviews?|criticism)\b.*$", "", text, flags=re.I)
        text = re.sub(r"[^\w\s'’\-]", " ", text, flags=re.UNICODE)
        text = re.sub(r"\s+", " ", text).strip(" \t\r\n-–—|:;,.")
        year_match = re.search(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)", text)
        year = int(year_match.group(1)) if year_match else None
        text = re.sub(r"[\[(](?:18|19|20)\d{2}[\])]", " ", text)
        text = re.sub(r"\s+(?:18|19|20)\d{2}\s*$", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" \t\r\n-–—|:;,.")
        if len(text) < 5 or not re.search(r"[A-Za-zÀ-ž]", text):
            return "", year
        if text.casefold() in {
            "google play", "movie", "movies", "film", "films",
            "official site", "watch online", "full movie",
        }:
            return "", year
        return text[:180], year

    @classmethod
    def title_hints_from_lens_results(
        cls,
        results: list[dict[str, Any]],
    ) -> tuple[list[str], int | None]:
        """Rank repeated Lens title signals and retain direct Play Store aliases as suggestions.

        Opaque Google /goto URLs are never decoded or treated as identity evidence.
        Local filenames and source-folder labels are never accepted as title inputs.
        """
        lens_buckets: dict[str, dict[str, Any]] = {}
        play_hints: list[str] = []

        for item in results or []:
            if not isinstance(item, dict) or item.get("error"):
                continue
            title_value = item.get("title")
            if isinstance(title_value, str):
                title, year = cls._clean_title_hint(title_value)
                if title:
                    key = re.sub(r"\W+", " ", title.casefold()).strip()
                    bucket = lens_buckets.setdefault(key, {
                        "title": title,
                        "count": 0,
                        "years": set(),
                        "first_seen": len(lens_buckets),
                    })
                    bucket["count"] += 1
                    if year:
                        bucket["years"].add(year)

            urls: list[str] = []
            for key in ("url", "resolved_url", "url_candidates", "resolved_urls"):
                value = item.get(key)
                candidates = value if isinstance(value, list) else [value]
                urls.extend(
                    candidate.strip()
                    for candidate in candidates
                    if isinstance(candidate, str) and candidate.strip()
                )

            for candidate in dict.fromkeys(urls):
                try:
                    parsed = urlparse(candidate)
                except ValueError:
                    continue
                host = (parsed.hostname or "").casefold().rstrip(".")
                if host != "play.google.com":
                    continue
                prefix = "/store/movies/details/"
                if not parsed.path.casefold().startswith(prefix):
                    continue
                slug = parsed.path[len(prefix):].split("/", 1)[0]
                if slug and not slug.isdigit():
                    play_title, _ = cls._clean_title_hint(slug)
                    if play_title:
                        key = re.sub(r"\W+", " ", play_title.casefold()).strip()
                        if key and all(
                            re.sub(r"\W+", " ", old.casefold()).strip() != key
                            for old in play_hints
                        ):
                            play_hints.append(play_title)

        ranked_lens = sorted(
            lens_buckets.values(),
            key=lambda row: (-int(row["count"]), int(row["first_seen"])),
        )
        repeated_lens = [row for row in ranked_lens if int(row["count"]) >= 2]
        single_lens = [row for row in ranked_lens if int(row["count"]) < 2]

        ordered_hints: list[str] = []
        def append_unique(value: str) -> None:
            key = re.sub(r"\W+", " ", value.casefold()).strip()
            if key and all(
                re.sub(r"\W+", " ", old.casefold()).strip() != key
                for old in ordered_hints
            ):
                ordered_hints.append(value)

        for row in repeated_lens:
            append_unique(str(row["title"]))
        for value in play_hints:
            append_unique(value)
        for row in single_lens:
            append_unique(str(row["title"]))

        year: int | None = None
        if repeated_lens:
            top_years = repeated_lens[0]["years"]
            if len(top_years) == 1:
                year = next(iter(top_years))
        if year is None:
            all_years = {
                value
                for bucket in lens_buckets.values()
                for value in bucket["years"]
            }
            if len(all_years) == 1:
                year = next(iter(all_years))

        return ordered_hints[:5], year

    @staticmethod
    def _google_official_title_search_url(
        title_hints: list[str],
        year: int | None,
        *,
        broad: bool = False,
    ) -> str:
        """Build domain-scoped searches across official film and archive catalogs.

        IMDb and TMDB are searched for official identifiers; AFI and BFI are
        searched as independent catalog references for reviewer corroboration.
        The exact pass keeps phrases intact and applies the year when available.
        """
        domains = (
            "imdb.com/title",
            "themoviedb.org/movie",
            "aficatalog.afi.com",
            "catalog.afi.com",
            "collections-search.bfi.org.uk",
        )
        branches: list[str] = []
        for value in title_hints:
            title = re.sub(r"[^\w\s'’\-]", " ", str(value or ""), flags=re.UNICODE)
            title = re.sub(r"\s+", " ", title).strip()
            if not title:
                continue
            if broad:
                title_expression = f"({title})"
            else:
                title_expression = f'"{title.replace(chr(34), " ").strip()}"'
                if year:
                    title_expression += f" {int(year)}"
            for domain in domains:
                branches.append(f"(site:{domain} {title_expression})")

        query = " OR ".join(branches)
        return f"{GOOGLE_SEARCH_URL}?{urlencode({'q': query, 'hl': 'en', 'brd_json': '1'})}"

    def search_official_movie_pages_from_lens(
        self,
        lens_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Find official movie pages from Lens-derived titles with one bounded fallback."""
        title_hints, year = self.title_hints_from_lens_results(lens_results)
        self.last_official_search_diagnostics = {
            "status": "no_title_hints" if not title_hints else "requesting",
            "query_count": 0,
            "query_variants": [],
            "title_hints": title_hints,
            "year_hint": year,
            "response_format": "json",
            "payload_shape": None,
            "http_status": None,
            "attempts": 0,
            "organic_result_count": 0,
            "candidate_url_count": 0,
            "google_redirect_count": 0,
            "google_redirect_unwrapped_count": 0,
            "candidate_domains": {},
            "official_url_count": 0,
            "catalog_source_count": 0,
            "afi_catalog_result_count": 0,
            "bfi_archive_result_count": 0,
            "error": None,
        }
        if not title_hints:
            return []

        diagnostics = self.last_official_search_diagnostics
        official_by_url: dict[str, dict[str, Any]] = {}
        candidate_domains: dict[str, int] = {}
        total_organic = 0
        total_candidates = 0
        total_redirects = 0
        total_unwrapped = 0
        total_attempts = 0
        afi_catalog_count = 0
        bfi_archive_count = 0

        # The first query is precise; the second is used only when Google returns
        # a valid SERP but no official IMDb/TMDB movie URL.
        query_specs = (
            ("exact_site_scoped", False),
            ("broad_site_scoped", True),
        )
        for variant, broad in query_specs:
            target_url = self._google_official_title_search_url(
                title_hints, year, broad=broad
            )
            diagnostics["query_count"] += 1
            diagnostics["query_variants"].append(variant)

            try:
                response, payload, effective_status, attempts = self._post_serp_with_retries(
                    target_url,
                    "Google official movie title search",
                    parsed_light_header=False,
                    response_format="json",
                )
            except BrightDataAPIError as exc:
                diagnostics.update({
                    "status": "quota_error" if isinstance(exc, BrightDataQuotaError) else "error",
                    "attempts": total_attempts + MAX_TRANSIENT_RETRIES + 1,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
                raise

            total_attempts += attempts
            body = getattr(response, "text", "") or ""
            diagnostics.update({
                "status": "http_error" if effective_status < 200 or effective_status >= 300 else "parsing",
                "http_status": effective_status,
                "attempts": total_attempts,
                "response_format": "json",
                "payload_shape": self._payload_shape(payload) if payload is not None else None,
            })

            if effective_status < 200 or effective_status >= 300:
                error_payload = payload.get("body") if (
                    isinstance(payload, dict) and "status_code" in payload and "body" in payload
                ) else payload
                message = self._http_error(
                    effective_status,
                    self._error_detail(error_payload, str(error_payload or body)),
                )
                diagnostics["status"] = "quota_error" if effective_status in {402, 429} else "http_error"
                diagnostics["error"] = message
                if effective_status in {402, 429}:
                    raise BrightDataQuotaError(effective_status, message)
                raise BrightDataAPIError(message)

            if payload is None:
                headers = getattr(response, "headers", {}) or {}
                content_type = headers.get("content-type", "unknown") if hasattr(headers, "get") else "unknown"
                content_length = headers.get("content-length", "unknown") if hasattr(headers, "get") else "unknown"
                message = (
                    "Bright Data Google title search returned an empty or non-JSON response "
                    f"(HTTP {effective_status}, content-type={content_type}, "
                    f"content-length={content_length})"
                )
                diagnostics.update({"status": "invalid_response", "error": message})
                raise BrightDataAPIError(message)

            rows = self._rows_from_payload(payload)
            if rows is None:
                shape = self._payload_shape(payload)
                message = (
                    "Bright Data Google title search returned JSON without a recognized organic result list; "
                    + shape
                )
                diagnostics.update({"status": "unparsed_response", "error": message})
                raise BrightDataAPIError(message)

            total_organic += len(rows)
            normalized = self._normalize_matches(rows)
            for result in normalized:
                for value in (result.get("url_candidates") or []):
                    if not isinstance(value, str) or not value.strip():
                        continue
                    total_candidates += 1
                    raw_url = value.strip()
                    try:
                        raw_parsed = urlparse(raw_url)
                        raw_host = (raw_parsed.hostname or "").casefold().rstrip(".")
                        raw_path = raw_parsed.path.rstrip("/").casefold()
                    except ValueError:
                        raw_host = ""
                        raw_path = ""

                    if (
                        (raw_host == "google.com" or raw_host.endswith(".google.com"))
                        and raw_path in {"/url", "/imgres"}
                    ):
                        total_redirects += 1

                    # Only unwrap explicit Google destination parameters; the final
                    # allow-list below remains restricted to official movie pages.
                    destination_url = _resolved_destination_url(raw_url)
                    if destination_url != raw_url:
                        total_unwrapped += 1

                    try:
                        destination_host = (
                            urlparse(destination_url).hostname or ""
                        ).casefold().rstrip(".")
                    except ValueError:
                        destination_host = ""
                    if destination_host:
                        candidate_domains[destination_host] = (
                            candidate_domains.get(destination_host, 0) + 1
                        )

                    if not self._official_movie_url(destination_url):
                        continue
                    if destination_url in official_by_url:
                        continue
                    destination_path = ""
                    try:
                        destination_path = (urlparse(destination_url).hostname or "").casefold().rstrip(".")
                    except ValueError:
                        pass
                    if destination_path in {"aficatalog.afi.com", "catalog.afi.com"}:
                        catalog_source = "afi_catalog"
                        afi_catalog_count += 1
                    elif destination_path == "collections-search.bfi.org.uk":
                        catalog_source = "bfi_national_archive"
                        bfi_archive_count += 1
                    elif destination_path == "imdb.com" or destination_path.endswith(".imdb.com"):
                        catalog_source = "imdb_official_title_page"
                    elif destination_path == "themoviedb.org" or destination_path.endswith(".themoviedb.org"):
                        catalog_source = "tmdb_official_movie_page"
                    else:
                        catalog_source = "unknown_official_source"
                    official_by_url[destination_url] = {
                        **result,
                        "url": destination_url,
                        "url_candidates": [destination_url],
                        "resolved_urls": [destination_url],
                        "resolved_url": destination_url,
                        "provider": "google_serp_trusted_film_catalog_search",
                        "catalog_source": catalog_source,
                        "identity_evidence": False,
                        "automatic_match_allowed": False,
                        "search_source": "lens_result_title_or_direct_play_store_url",
                        "search_query_variant": variant,
                        "original_search_url": raw_url if raw_url != destination_url else None,
                    }

            # A catalog/archive reference is useful for review but cannot
            # replace an official TMDB/IMDb identifier. Continue to the broader
            # fallback unless an actual identity-bearing movie URL was found.
            if any(
                row.get("catalog_source") in {
                    "tmdb_official_movie_page",
                    "imdb_official_title_page",
                }
                for row in official_by_url.values()
            ):
                break

        diagnostics.update({
            "status": "ok" if official_by_url else "no_official_results",
            "attempts": total_attempts,
            "organic_result_count": total_organic,
            "candidate_url_count": total_candidates,
            "google_redirect_count": total_redirects,
            "google_redirect_unwrapped_count": total_unwrapped,
            "candidate_domains": dict(
                sorted(candidate_domains.items(), key=lambda item: (-item[1], item[0]))[:12]
            ),
            "official_url_count": len(official_by_url),
            "catalog_source_count": afi_catalog_count + bfi_archive_count,
            "afi_catalog_result_count": afi_catalog_count,
            "bfi_archive_result_count": bfi_archive_count,
            "error": None,
        })
        return list(official_by_url.values())

    def _request_lens_tab(
        self,
        image_url: str,
        tab: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Fetch and normalize one documented Google Lens tab."""
        target_url = self._lens_request_url(image_url, tab=tab)
        response, payload, effective_status, attempts = self._post_serp_with_retries(
            target_url,
            f"Google Lens {tab} image search",
        )
        body = getattr(response, "text", "") or ""
        if effective_status < 200 or effective_status >= 300:
            error_payload = payload.get("body") if (
                isinstance(payload, dict) and "status_code" in payload and "body" in payload
            ) else payload
            message = self._http_error(
                effective_status,
                self._error_detail(error_payload, str(error_payload or body)),
            )
            if effective_status in {402, 429}:
                raise BrightDataQuotaError(effective_status, message)
            raise BrightDataAPIError(message)

        if payload is None:
            headers = getattr(response, "headers", {}) or {}
            content_type = headers.get("content-type", "unknown") if hasattr(headers, "get") else "unknown"
            content_length = headers.get("content-length", "unknown") if hasattr(headers, "get") else "unknown"
            raise BrightDataAPIError(
                "Bright Data returned an empty or non-JSON response "
                f"(HTTP {effective_status}, content-type={content_type}, "
                f"content-length={content_length})"
            )

        if isinstance(payload, dict) and "status_code" in payload and "body" in payload:
            inner_body = payload.get("body")
            if not str(inner_body or "").strip():
                raise BrightDataAPIError(
                    "Bright Data API envelope was received but its body is empty "
                    f"(inner HTTP {effective_status}); no Google Lens results were delivered"
                )

        if isinstance(payload, dict) and any(payload.get(key) for key in ("error", "errors")):
            detail = self._error_detail(payload)
            raise BrightDataAPIError(
                f"Bright Data returned an API error: {detail or 'unknown provider error'}"
            )

        rows = self._rows_from_payload(payload)
        if rows is None:
            shape = self._payload_shape(payload)
            raise BrightDataAPIError(
                "Bright Data returned JSON, but no recognized Google Lens result list was present; "
                + shape
            )

        normalized = self._normalize_matches(rows)
        request_diagnostics = dict(getattr(self, "last_request_diagnostics", {}) or {})
        tab_diagnostics = {
            "status": "ok" if normalized else "no_matches",
            "http_status": effective_status,
            "attempts": attempts,
            "result_count": len(normalized),
            "raw_result_count": len(rows),
            **request_diagnostics,
            **self._raw_result_diagnostics(rows),
        }
        return normalized, rows, tab_diagnostics

    def search_by_image_url(self, image_url: str) -> list[dict[str, Any]]:
        """Search exact matches, then visual matches when exact results lack official IDs.

        Google began wrapping some exact-match destinations in opaque /goto
        URLs. A single visual_matches fallback is used to seek direct publisher
        links. Opaque wrappers remain quarantined and are never parsed as IDs.
        """
        self.last_official_search_diagnostics = {
            "status": "not_run",
            "query_count": 0,
            "query_variants": [],
            "title_hints": [],
            "year_hint": None,
            "response_format": "json",
            "payload_shape": None,
            "http_status": None,
            "attempts": 0,
            "organic_result_count": 0,
            "candidate_url_count": 0,
            "google_redirect_count": 0,
            "google_redirect_unwrapped_count": 0,
            "candidate_domains": {},
            "official_url_count": 0,
            "error": None,
        }
        self.last_lens_diagnostics = {
            "status": "requesting",
            "lens_tab": LENS_TAB,
            "lens_tabs_attempted": [],
            "lens_tab_diagnostics": {},
            "data_format": "parsed_light",
            "http_status": None,
            "attempts": 0,
            "result_count": 0,
            "error": None,
        }

        try:
            exact_results, exact_rows, exact_diag = self._request_lens_tab(image_url, LENS_TAB)
        except BrightDataAPIError as exc:
            request_diag = dict(getattr(self, "last_request_diagnostics", {}) or {})
            self.last_lens_diagnostics.update({
                "status": "quota_error" if isinstance(exc, BrightDataQuotaError) else "error",
                "lens_tabs_attempted": [LENS_TAB],
                "lens_tab_diagnostics": {
                    LENS_TAB: {
                        "status": "quota_error" if isinstance(exc, BrightDataQuotaError) else "error",
                        **request_diag,
                    }
                },
                "attempts": request_diag.get("attempts", 1),
                "rate_limit_retries": request_diag.get("rate_limit_retries", 0),
                "rate_limit_wait_seconds": request_diag.get("rate_limit_wait_seconds", 0),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
            raise

        tabs = {LENS_TAB: exact_diag}
        attempts = int(exact_diag.get("attempts") or 0)
        combined = list(exact_results)
        exact_ids = extract_tmdb_movie_ids(exact_results) or extract_imdb_ids(exact_results)
        visual_results: list[dict[str, Any]] = []

        # Use visual_matches only when exact_matches did not produce an official
        # IMDb/TMDB page. This is a bounded second request, not a batch retry loop.
        if not exact_ids:
            try:
                visual_results, visual_rows, visual_diag = self._request_lens_tab(
                    image_url, LENS_FALLBACK_TAB
                )
                tabs[LENS_FALLBACK_TAB] = visual_diag
                attempts += int(visual_diag.get("attempts") or 0)
                seen = {
                    (
                        re.sub(r"\W+", " ", str(row.get("title") or "").casefold()).strip(),
                        str(row.get("url") or ""),
                        str(row.get("source") or ""),
                    )
                    for row in combined
                }
                for row in visual_results:
                    key = (
                        re.sub(r"\W+", " ", str(row.get("title") or "").casefold()).strip(),
                        str(row.get("url") or ""),
                        str(row.get("source") or ""),
                    )
                    if key not in seen:
                        combined.append(row)
                        seen.add(key)
            except BrightDataQuotaError as exc:
                request_diag = dict(getattr(self, "last_request_diagnostics", {}) or {})
                tabs[LENS_FALLBACK_TAB] = {
                    "status": "quota_error",
                    **request_diag,
                    "error": str(exc)[:300],
                }
                self.last_lens_diagnostics.update({
                    "status": "quota_error",
                    "lens_tabs_attempted": [LENS_TAB, LENS_FALLBACK_TAB],
                    "lens_tab_diagnostics": tabs,
                    "attempts": attempts + int(request_diag.get("attempts") or 0),
                    "rate_limit_retries": sum(
                        int(item.get("rate_limit_retries") or 0) for item in tabs.values()
                    ),
                    "rate_limit_wait_seconds": sum(
                        int(item.get("rate_limit_wait_seconds") or 0) for item in tabs.values()
                    ),
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
                raise
            except BrightDataAPIError as exc:
                request_diag = dict(getattr(self, "last_request_diagnostics", {}) or {})
                tabs[LENS_FALLBACK_TAB] = {
                    "status": "error",
                    **request_diag,
                    "error": f"{type(exc).__name__}: {str(exc)[:250]}",
                }

        raw_paths: dict[str, int] = {}
        raw_fields: set[str] = set()
        for diag in tabs.values():
            raw_fields.update(diag.get("raw_result_fields") or [])
            for key, value in (diag.get("raw_link_paths") or {}).items():
                raw_paths[key] = raw_paths.get(key, 0) + int(value or 0)

        self.last_lens_diagnostics.update({
            "status": "ok" if combined else "no_matches",
            "lens_tab": "+".join(tabs),
            "lens_tabs_attempted": list(tabs),
            "lens_tab_diagnostics": tabs,
            "http_status": tabs.get(LENS_FALLBACK_TAB, tabs[LENS_TAB]).get("http_status"),
            "attempts": attempts,
            "rate_limit_retries": sum(
                int(item.get("rate_limit_retries") or 0) for item in tabs.values()
            ),
            "rate_limit_wait_seconds": sum(
                int(item.get("rate_limit_wait_seconds") or 0) for item in tabs.values()
            ),
            "raw_result_fields": sorted(raw_fields)[:40],
            "raw_link_paths": dict(
                sorted(raw_paths.items(), key=lambda item: (-item[1], item[0]))[:12]
            ),
            "exact_matches_result_count": len(exact_results),
            "visual_matches_result_count": len(visual_results),
            "raw_result_count": sum(
                int(item.get("raw_result_count") or 0) for item in tabs.values()
            ),
            "result_count": len(combined),
            "error": tabs.get(LENS_FALLBACK_TAB, {}).get("error"),
        })
        return combined

    @staticmethod
    def _http_error(status: int, detail: str) -> str:
        messages = {
            400: "Bright Data rejected the SERP request; verify the request URL and SERP zone",
            401: "Bright Data authentication failed; verify BRIGHTDATA_API_KEY in Hostless",
            402: "Bright Data reports no available free/API credits; do not add funds or enable a paid plan",
            403: "Bright Data denied this request; verify the SERP zone is active and permitted",
            429: "Bright Data returned HTTP 429 after any explicit cooldown retry; this may be temporary throttling or a SERP-zone throughput limit",
        }
        message = messages.get(status, "Bright Data service error; the image lookup did not return a usable response")
        suffix = f": {detail}" if detail else ""
        return f"{message} (HTTP {status}){suffix}"[:600]
