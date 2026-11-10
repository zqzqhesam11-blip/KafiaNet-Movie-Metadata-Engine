from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlparse, parse_qs

import httpx

from app.image_evidence import (
    extract_imdb_ids,
    extract_tmdb_movie_ids,
    imdb_id_from_url,
    tmdb_movie_id_from_url,
    _resolved_destination_url,
)


BRIGHTDATA_REQUEST_URL = "https://api.brightdata.com/request"
GOOGLE_LENS_URL = "https://lens.google.com/uploadbyurl"
GOOGLE_SEARCH_URL = "https://www.google.com/search"
LENS_TAB = "exact_matches"
LENS_FALLBACK_TAB = "visual_matches"
MAX_NORMALIZED_RESULTS = 80
MAX_GOOGLE_GOTO_RESOLUTIONS = 3
GOOGLE_GOTO_TIMEOUT_SECONDS = 8.0
MAX_TRANSIENT_RETRIES = 2
RATE_LIMIT_BACKOFF_SECONDS = (5, 15, 30)
MAX_RATE_LIMIT_RETRIES = len(RATE_LIMIT_BACKOFF_SECONDS)
MAX_RATE_LIMIT_WAIT_SECONDS = 60
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
        # Use a separate unauthenticated client for Google redirects. Never send
        # the Bright Data bearer token to Google or follow redirects automatically.
        self.redirect_client = httpx.Client(
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
            },
            timeout=GOOGLE_GOTO_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        self.last_result_url_diagnostics: dict[str, Any] = {}
        self.last_lens_diagnostics: dict[str, Any] = {}
        self.last_official_search_diagnostics: dict[str, Any] = {}

    def close(self) -> None:
        self.client.close()
        redirect_client = getattr(self, "redirect_client", None)
        if redirect_client is not None:
            redirect_client.close()

    @staticmethod
    def _is_google_goto_url(value: str) -> bool:
        """Recognize only HTTPS Google /goto URLs carrying an opaque url token."""
        try:
            parsed = urlparse(str(value or "").strip())
            host = (parsed.hostname or "").casefold().rstrip(".")
            port = parsed.port
            query = parse_qs(parsed.query)
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and host in {"google.com", "www.google.com", "lens.google.com"}
            and port in {None, 443}
            and parsed.path.rstrip("/") == "/goto"
            and bool(str((query.get("url") or [""])[0]).strip())
        )

    @staticmethod
    def _is_official_movie_destination(value: str) -> bool:
        """Only retain resolved Google redirects that identify an official movie page."""
        return bool(
            tmdb_movie_id_from_url(value) is not None
            or imdb_id_from_url(value) is not None
        )

    def _resolve_one_google_goto(self, value: str) -> tuple[str | None, int | None, str]:
        """Resolve one opaque Google token from its HTTP Location header only.

        No protobuf/base64 decoding, HTML scraping, JavaScript execution, or
        automatic redirect following is used. An untrusted final host is rejected.
        """
        if not self._is_google_goto_url(value):
            return None, None, "not_google_goto"
        client = getattr(self, "redirect_client", None)
        if client is None or not callable(getattr(client, "get", None)):
            return None, None, "resolver_unavailable"
        try:
            response = client.get(
                value,
                follow_redirects=False,
                timeout=GOOGLE_GOTO_TIMEOUT_SECONDS,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            headers = getattr(response, "headers", {}) or {}
            location = headers.get("location") if hasattr(headers, "get") else None
            if status in {301, 302, 303, 307, 308} and location:
                destination = urljoin(value, str(location).strip())
                # Google sometimes places a plain /url wrapper in Location.
                destination = _resolved_destination_url(destination)
                try:
                    parsed = urlparse(destination)
                except ValueError:
                    return None, status, "invalid_location"
                if parsed.scheme != "https" or not parsed.netloc:
                    return None, status, "non_https_location"
                if not self._is_official_movie_destination(destination):
                    return None, status, "non_official_destination"
                return destination, status, "resolved"
            if status in {401, 403, 429}:
                return None, status, "google_rate_limited_or_blocked"
            if status == 404:
                return None, status, "google_goto_not_found"
            if 200 <= status < 300:
                return None, status, "no_redirect_interstitial_or_page"
            return None, status, "unexpected_http_status"
        except (httpx.RequestError, ValueError, TypeError) as exc:
            return None, None, f"request_error_{type(exc).__name__}"
        except Exception as exc:
            # Keep the batch alive for one bad redirect; diagnostics expose only
            # the exception class, never the signed Google token or response body.
            return None, None, f"request_error_{type(exc).__name__}"

    def resolve_result_urls(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Resolve at most three Lens /goto links per tab, keeping only safe destinations.

        Direct result URLs are preserved. Opaque /goto tokens are requested with
        GET and follow_redirects=False; only an HTTPS IMDb/TMDB Location is added.
        Requests are sequential and stop on a block/interstitial response to avoid
        triggering Google's per-results-page redirect throttling.
        """
        if not isinstance(getattr(self, "last_lens_diagnostics", None), dict):
            self.last_lens_diagnostics = {}

        prepared = [dict(item) for item in (results or []) if isinstance(item, dict)]
        stats: dict[str, Any] = {
            "google_goto_candidates": 0,
            "google_goto_resolved": 0,
            "google_goto_failed": 0,
            "google_goto_skipped": 0,
            "google_goto_direct_urls_kept": 0,
            "google_goto_http_statuses": {},
            "google_goto_methods": {},
            "google_goto_error_kinds": {},
            "google_goto_error_samples": [],
            "google_goto_resolution_limit": MAX_GOOGLE_GOTO_RESOLUTIONS,
        }
        attempted = 0
        stop_for_block = False

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
            safe_destinations: list[str] = []
            direct_url_count = 0
            saw_goto = False
            row_status = None

            for candidate in unique_candidates:
                try:
                    parsed = urlparse(candidate)
                except ValueError:
                    continue
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    continue

                if self._is_google_goto_url(candidate):
                    saw_goto = True
                    stats["google_goto_candidates"] += 1
                    if attempted >= MAX_GOOGLE_GOTO_RESOLUTIONS or stop_for_block:
                        stats["google_goto_skipped"] += 1
                        row_status = "google_goto_resolution_limit_or_block"
                        continue
                    attempted += 1
                    stats["google_goto_methods"]["GET"] = (
                        stats["google_goto_methods"].get("GET", 0) + 1
                    )
                    destination, status, outcome = self._resolve_one_google_goto(candidate)
                    if status is not None:
                        key = str(status)
                        stats["google_goto_http_statuses"][key] = (
                            stats["google_goto_http_statuses"].get(key, 0) + 1
                        )
                    if destination:
                        if destination not in safe_destinations:
                            safe_destinations.append(destination)
                        stats["google_goto_resolved"] += 1
                        row_status = "google_goto_resolved"
                    else:
                        stats["google_goto_failed"] += 1
                        row_status = outcome
                        stats["google_goto_error_kinds"][outcome] = (
                            stats["google_goto_error_kinds"].get(outcome, 0) + 1
                        )
                        # A 200 response without a Location is commonly a
                        # verification/interstitial page; do not hammer this SERP.
                        if (
                            outcome in {
                                "google_rate_limited_or_blocked",
                                "no_redirect_interstitial_or_page",
                                "resolver_unavailable",
                            }
                            or outcome.startswith("request_error_")
                            or (status is not None and status >= 500)
                        ):
                            stop_for_block = True
                    continue

                if candidate not in safe_destinations:
                    safe_destinations.append(candidate)
                    direct_url_count += 1

            safe_destinations = list(dict.fromkeys(safe_destinations))
            stats["google_goto_direct_urls_kept"] += direct_url_count
            item["url_candidates"] = [
                value for value in safe_destinations
                if isinstance(value, str) and value.strip()
            ]
            item["resolved_urls"] = safe_destinations
            item["resolved_url"] = safe_destinations[0] if safe_destinations else ""
            # Remove the opaque token from persisted Lens evidence. Only the
            # verified destination (or an empty URL) is retained.
            item["url"] = safe_destinations[0] if safe_destinations else ""
            if saw_goto:
                item["url_resolution_status"] = row_status or "google_goto_unresolved"
            else:
                item.pop("url_resolution_status", None)
            item.pop("url_resolution_error", None)
            item.pop("url_resolution_errors", None)

        self.last_result_url_diagnostics = stats
        self.last_lens_diagnostics.update(stats)
        return prepared

    @staticmethod
    def _lens_request_url(
        image_url: str,
        tab: str = LENS_TAB,
        response_mode: str = "parsed_light",
    ) -> str:
        """Build one Lens URL with exactly one Bright Data JSON selector.

        Bright Data documents two independent ways to request parsed results:
        the x-unblock-data-format header (preferred for Lens parsed_light), or
        the brd_json URL parameter. Do not send both selectors in one request.
        """
        if tab not in {LENS_TAB, LENS_FALLBACK_TAB}:
            raise BrightDataAPIError(f"Unsupported Google Lens tab: {tab}")
        if response_mode not in {"parsed_light", "brd_json"}:
            raise BrightDataAPIError(
                f"Unsupported Google Lens response mode: {response_mode}"
            )
        parsed = urlparse(str(image_url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise BrightDataAPIError(
                "Google Lens requires a public HTTPS poster URL without embedded credentials"
            )
        query = {
            "url": image_url,
            "hl": "en",
            "brd_lens": tab,
        }
        if response_mode == "brd_json":
            query["brd_json"] = "1"
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
        path_counts_by_field: dict[str, dict[str, int]] = {}
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
                    field_counts = path_counts_by_field.setdefault(name.casefold(), {})
                    field_counts[label] = field_counts.get(label, 0) + 1

        top_paths = dict(
            sorted(path_counts.items(), key=lambda item: (-item[1], item[0]))[:12]
        )
        top_paths_by_field = {
            field: dict(
                sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:8]
            )
            for field, counts in sorted(path_counts_by_field.items())
        }
        compact_types = {
            key: "/".join(sorted(types))
            for key, types in sorted(field_types.items())[:30]
        }
        return {
            "raw_result_fields": sorted(field_names)[:40],
            "raw_result_field_types": compact_types,
            "raw_link_paths": top_paths,
            "raw_link_paths_by_field": top_paths_by_field,
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

        # Prefer non-empty explicit Lens collections at any casing or nesting
        # level. Some providers include an empty exact_matches array beside a
        # populated visual_matches collection in the same response.
        empty_result_list: list[dict[str, Any]] | None = None
        for key, value in normalized_items:
            if key in result_keys and isinstance(value, list):
                if all(isinstance(item, dict) for item in value):
                    if value:
                        return value
                    if empty_result_list is None:
                        empty_result_list = value

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
                if rows is not None and rows:
                    return rows
        return empty_result_list

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
        """Collect page URLs, including nested Bright Data link objects.

        Bright Data's Lens fields are primarily scalar, but integrations may
        wrap a link in one or more objects/lists. Walk only the designated page
        URL fields, bound traversal, and explicitly ignore image/thumbnail
        branches so image CDN links are never mistaken for movie-page links.
        """
        values: list[str] = []
        seen: set[str] = set()
        image_keys = {
            "image", "imageurl", "originalimage", "originalimageurl",
            "thumbnail", "thumbnailurl", "sourceicon", "logo",
            "imagebase64", "base64",
        }
        max_depth = 6
        max_nodes = 256
        nodes_seen = 0

        def collect(value: Any, depth: int = 0) -> None:
            nonlocal nodes_seen
            if depth > max_depth or nodes_seen >= max_nodes:
                return
            nodes_seen += 1
            if isinstance(value, str):
                candidate = value.strip()
                if not candidate or len(candidate) > 4096:
                    return
                try:
                    parsed = urlparse(candidate)
                except ValueError:
                    return
                if (
                    parsed.scheme in {"http", "https"}
                    and parsed.netloc
                    and candidate not in seen
                ):
                    seen.add(candidate)
                    values.append(candidate[:1000])
                return
            if isinstance(value, list):
                for child in value[:40]:
                    collect(child, depth + 1)
                return
            if isinstance(value, dict):
                for name, child in value.items():
                    normalized_name = re.sub(r"[^a-z0-9]", "", str(name).casefold())
                    if normalized_name in image_keys:
                        continue
                    if isinstance(child, (dict, list)) or normalized_name in {
                        "url", "link", "href", "src", "target", "destination",
                        "destinationurl", "pageurl", "resulturl", "targeturl",
                        "landingpage", "website", "canonicalurl", "value",
                    }:
                        collect(child, depth + 1)

        for key in keys:
            if key in item:
                collect(item.get(key))
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
        return max(delays)

    def _post_serp_with_retries(
        self,
        target_url: str,
        stage: str,
        *,
        parsed_light_header: bool = True,
        response_format: str = "raw",
        retry_empty_success: bool = True,
    ) -> tuple[httpx.Response, Any, int, int]:
        """POST one SERP request with bounded transport and HTTP 429 backoff.

        Empty HTTP-200 bodies can be retried for ordinary requests. Google Lens
        disables identical empty-body retries and instead makes one controlled
        request using Bright Data's alternative documented JSON selector.
        Explicit Retry-After/cooldown values are honored up to 60 seconds.
        HTTP 402 credit exhaustion is never retried.
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
                (
                    effective_status in TRANSIENT_HTTP_STATUSES
                    or (empty_success_body and retry_empty_success)
                )
                and transient_retries < MAX_TRANSIENT_RETRIES
            ):
                time.sleep(min(2 ** transient_retries, 2))
                transient_retries += 1
                continue

            if effective_status == 429 and rate_limit_retries < MAX_RATE_LIMIT_RETRIES:
                delay = self._rate_limit_retry_delay(response, payload)
                if delay is None:
                    delay = RATE_LIMIT_BACKOFF_SECONDS[rate_limit_retries]
                # Never override a provider-supplied long Retry-After with a shorter
                # local delay. Fall back only when the provider gave no cooldown.
                if delay <= MAX_RATE_LIMIT_WAIT_SECONDS:
                    rate_limit_retries += 1
                    wait_seconds += delay
                    time.sleep(delay)
                    continue

            headers = getattr(response, "headers", {}) or {}
            safe_header_names = (
                "content-type",
                "content-length",
                "x-brd-error-code",
                "x-brd-error",
                "x-brd-err-code",
                "x-brd-err-msg",
                "x-brd-rate-limit",
                "x-brd-rate-limit-period-ms",
                "x-response-id",
            )
            safe_response_headers = {}
            for name in safe_header_names:
                try:
                    value = headers.get(name) if hasattr(headers, "get") else None
                except Exception:
                    value = None
                if value is not None and str(value).strip():
                    safe_response_headers[name] = str(value).strip()[:180]

            response_text = str(getattr(response, "text", "") or "")
            self.last_request_diagnostics.update({
                "attempts": attempt,
                "rate_limit_retries": rate_limit_retries,
                "rate_limit_wait_seconds": wait_seconds,
                "http_status": effective_status,
                "response_headers": safe_response_headers,
                "response_body_empty": bool(empty_success_body or not response_text.strip()),
                "response_body_bytes": len(response_text.encode("utf-8", errors="replace")),
            })
            return response, payload, effective_status, attempt


    @staticmethod
    def _clean_title_hint(value: str) -> tuple[str, int | None]:
        """Clean a Lens title without treating publisher names as part of the film title."""
        text = unquote(str(value or "")).replace("_", " ").replace("+", " ")
        text = text.replace("∣", "|")
        text = re.sub(
            r"^(?:Prime Video|Netflix|Amazon Prime Video|Google Play|YouTube|Apple TV)\s*:\s*",
            "",
            text,
            flags=re.I,
        )
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
        text = re.sub(
            r"\s+(?:english\s+)?(?:subtitled|subtitles?|dubbed)(?:\s+version)?$",
            "",
            text,
            flags=re.I,
        )
        text = re.sub(r"[^\w\s'’\-]", " ", text, flags=re.UNICODE)
        text = re.sub(r"\s+", " ", text).strip(" \t\r\n-–—|:;,.")
        year_match = re.search(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)", text)
        year = int(year_match.group(1)) if year_match else None
        text = re.sub(r"[\[(](?:18|19|20)\d{2}[\])]", " ", text)
        text = re.sub(r"\s+(?:18|19|20)\d{2}\s*$", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" \t\r\n-–—|:;,.")
        if len(text) < 5 or not re.search(r"[A-Za-zÀ-ž]", text):
            return "", year
        if re.fullmatch(
            r"(?:google\s+)?(?:ai\s+mode|ai\s+overview|search|overview|gemini|ask\s+google)",
            text,
            flags=re.I,
        ) or re.fullmatch(r"(?:โหมด|mode)\s*ai", text, flags=re.I):
            return "", year
        # Lens can expose the publisher/platform name as the result title
        # (for example, dozens of rows labelled only "YouTube"). Such labels
        # are not film-title evidence and must never outrank a direct movie
        # detail URL from Google Play or another result.
        generic_key = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
        if generic_key in {
            "google play", "google play movies", "play store",
            "youtube", "youtube com", "youtube movies",
            "prime video", "amazon prime video", "netflix", "apple tv",
            "imdb", "tmdb", "the movie database", "wikipedia",
            "letterboxd", "rotten tomatoes",
            "movie", "movies", "film", "films",
            "official site", "watch online", "full movie", "movie trailer",
            "ai", "ai mode", "ai overview", "google ai mode",
            "google search", "search", "overview", "gemini", "ask google",
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
        lens_title_keys = set(lens_buckets)
        for value in play_hints:
            key = re.sub(r"\W+", " ", value.casefold()).strip()
            if key not in lens_title_keys:
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
        """Build one fast, domain-scoped search across official movie catalogs.

        Keep the search broad enough to tolerate local-language and alternate
        poster titles. Candidate IDs are still verified by TMDB before READY;
        the Lens year hint remains available for candidate disambiguation.
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
                if year:
                    title_expression += f" {int(year)}"
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
        candidate_title_hints, year = self.title_hints_from_lens_results(lens_results)
        # Lens often returns useful movie titles alongside unrelated editorial,
        # retail, and platform captions. Combining all of them into one large
        # Google OR query dilutes the search and can surface mostly Google
        # navigation results. Search only the highest-ranked Lens/Play title;
        # keep the other candidates for diagnostics, not query expansion.
        title_hints = candidate_title_hints[:1]
        self.last_official_search_diagnostics = {
            "status": "no_title_hints" if not title_hints else "requesting",
            "query_count": 0,
            "query_variants": [],
            "title_hints": title_hints,
            "candidate_title_hints": candidate_title_hints[:5],
            "year_hint": year,
            "response_format": "json",
            "payload_shape": None,
            "http_status": None,
            "attempts": 0,
            "rate_limit_retries": 0,
            "rate_limit_wait_seconds": 0,
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

        # Use a single broad site-scoped request. The previous exact-then-broad
        # sequence doubled lookup latency for every poster without an official
        # ID. TMDB /find and movie details remain mandatory before READY.
        query_specs = (
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
                request_diagnostics = dict(getattr(self, "last_request_diagnostics", {}) or {})
                total_attempts += int(request_diagnostics.get("attempts") or 0)
                diagnostics.update({
                    "status": "quota_error" if isinstance(exc, BrightDataQuotaError) else "error",
                    "attempts": total_attempts,
                    "rate_limit_retries": int(diagnostics.get("rate_limit_retries") or 0)
                    + int(request_diagnostics.get("rate_limit_retries") or 0),
                    "rate_limit_wait_seconds": int(diagnostics.get("rate_limit_wait_seconds") or 0)
                    + int(request_diagnostics.get("rate_limit_wait_seconds") or 0),
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                })
                raise

            total_attempts += attempts
            body = getattr(response, "text", "") or ""
            request_diagnostics = dict(getattr(self, "last_request_diagnostics", {}) or {})
            diagnostics.update({
                "status": "http_error" if effective_status < 200 or effective_status >= 300 else "parsing",
                "http_status": effective_status,
                "attempts": total_attempts,
                "rate_limit_retries": int(diagnostics.get("rate_limit_retries") or 0)
                + int(request_diagnostics.get("rate_limit_retries") or 0),
                "rate_limit_wait_seconds": int(diagnostics.get("rate_limit_wait_seconds") or 0)
                + int(request_diagnostics.get("rate_limit_wait_seconds") or 0),
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
        """Fetch one Lens tab, with a single format fallback for empty/unparsed 200s.

        Primary: Bright Data's documented Lens header
            x-unblock-data-format: parsed_light
        Fallback: Bright Data's documented brd_json=1 URL parameter.
        Each request uses exactly one selector. We do not spend two extra
        requests repeating the same empty request format.
        """
        response_modes: list[str] = []
        request_diagnostics: list[dict[str, Any]] = []
        total_attempts = 0

        def request_mode(mode: str):
            nonlocal total_attempts
            target_url = self._lens_request_url(
                image_url, tab=tab, response_mode=mode
            )
            response, payload, effective_status, attempts = self._post_serp_with_retries(
                target_url,
                f"Google Lens {tab} image search ({mode})",
                parsed_light_header=(mode == "parsed_light"),
                retry_empty_success=False,
            )
            total_attempts += int(attempts or 0)
            response_modes.append(mode)
            request_diagnostics.append(
                dict(getattr(self, "last_request_diagnostics", {}) or {})
            )
            return response, payload, effective_status

        def raise_for_http_status(
            effective_status: int,
            payload: Any,
            response: httpx.Response,
        ) -> None:
            if 200 <= effective_status < 300:
                return
            body = getattr(response, "text", "") or ""
            error_payload = payload.get("body") if (
                isinstance(payload, dict)
                and "status_code" in payload
                and "body" in payload
            ) else payload
            message = self._http_error(
                effective_status,
                self._error_detail(error_payload, str(error_payload or body)),
            )
            if effective_status in {402, 429}:
                raise BrightDataQuotaError(effective_status, message)
            raise BrightDataAPIError(message)

        def reject_provider_error(payload: Any) -> None:
            if isinstance(payload, dict) and any(
                payload.get(key) for key in ("error", "errors")
            ):
                detail = self._error_detail(payload)
                raise BrightDataAPIError(
                    f"Bright Data returned an API error: {detail or 'unknown provider error'}"
                )

        def parse_rows(payload: Any) -> list[dict[str, Any]] | None:
            if payload is None:
                return None
            if (
                isinstance(payload, dict)
                and "status_code" in payload
                and "body" in payload
                and not str(payload.get("body") or "").strip()
            ):
                return None
            return self._rows_from_payload(payload)

        def aggregate_request_diagnostics(
            selected_mode: str,
            effective_status: int,
        ) -> dict[str, Any]:
            last_request = request_diagnostics[-1] if request_diagnostics else {}
            aggregated = {
                "attempts": total_attempts,
                "rate_limit_retries": sum(
                    int(item.get("rate_limit_retries") or 0)
                    for item in request_diagnostics
                ),
                "rate_limit_wait_seconds": sum(
                    int(item.get("rate_limit_wait_seconds") or 0)
                    for item in request_diagnostics
                ),
                "http_status": last_request.get("http_status", effective_status),
                "response_headers": dict(last_request.get("response_headers") or {}),
                "response_body_empty": last_request.get("response_body_empty"),
                "response_body_bytes": last_request.get("response_body_bytes"),
                "response_modes_attempted": list(response_modes),
                "data_format": "parsed_light" if selected_mode == "parsed_light" else "json",
            }
            self.last_request_diagnostics = aggregated
            return aggregated

        # Attempt 1: the Lens-specific parsed_light response documented by
        # Bright Data. Do not also append brd_json=1 to this URL.
        response, payload, effective_status = request_mode("parsed_light")
        aggregate_diagnostics = aggregate_request_diagnostics(
            "parsed_light", effective_status
        )
        raise_for_http_status(effective_status, payload, response)
        reject_provider_error(payload)
        rows = parse_rows(payload)
        selected_mode = "parsed_light"

        # An empty or unrecognized HTTP-200 body is not a successful empty
        # search. Try one alternate, documented full-JSON selector exactly once.
        if rows is None:
            response, payload, effective_status = request_mode("brd_json")
            selected_mode = "brd_json"
            aggregate_diagnostics = aggregate_request_diagnostics(
                selected_mode, effective_status
            )
            raise_for_http_status(effective_status, payload, response)
            reject_provider_error(payload)
            rows = parse_rows(payload)

        if rows is None:
            if payload is None or (
                isinstance(payload, dict)
                and "status_code" in payload
                and "body" in payload
                and not str(payload.get("body") or "").strip()
            ):
                response_headers = aggregate_diagnostics.get("response_headers") or {}
                header_summary = ", ".join(
                    f"{key}={value}" for key, value in sorted(response_headers.items())
                ) or "no response metadata headers"
                details = (
                    f"HTTP {aggregate_diagnostics.get('http_status')}, "
                    f"{header_summary}, "
                    f"body_bytes={aggregate_diagnostics.get('response_body_bytes')}"
                )
                message = (
                    "Bright Data returned an empty or non-JSON response for Google Lens "
                    f"after trying parsed_light and brd_json once each ({details})"
                )
            else:
                message = (
                    "Bright Data returned JSON, but no recognized Google Lens result list "
                    "was present after trying parsed_light and brd_json; "
                    + self._payload_shape(payload)
                )
            raise BrightDataAPIError(message)

        normalized = self._normalize_matches(rows)
        tab_diagnostics = {
            "status": "ok" if normalized else "no_matches",
            "http_status": aggregate_diagnostics["http_status"],
            "attempts": total_attempts,
            "result_count": len(normalized),
            "raw_result_count": len(rows),
            "rate_limit_retries": aggregate_diagnostics["rate_limit_retries"],
            "rate_limit_wait_seconds": aggregate_diagnostics["rate_limit_wait_seconds"],
            "data_format": aggregate_diagnostics["data_format"],
            "response_modes_attempted": list(response_modes),
            "response_headers": aggregate_diagnostics["response_headers"],
            **self._raw_result_diagnostics(rows),
        }
        return normalized, rows, tab_diagnostics

    def search_by_image_url(self, image_url: str) -> list[dict[str, Any]]:
        """Search Lens exact matches, then visual matches if no verified ID is exposed.

        Captions never create identifiers. Opaque /goto tokens are resolved only
        through a bounded, unauthenticated GET redirect check; final destinations
        must be HTTPS IMDb/TMDB movie pages before TMDB verification.
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

        self.last_result_url_diagnostics = {}
        try:
            exact_results, exact_rows, exact_diag = self._request_lens_tab(image_url, LENS_TAB)
            exact_results = [{**row, "lens_tab": LENS_TAB} for row in exact_results]
            exact_results = self.resolve_result_urls(exact_results)
            exact_url_diag = dict(self.last_result_url_diagnostics)
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
                "http_status": request_diag.get("http_status"),
                "data_format": request_diag.get("data_format", "parsed_light"),
                "response_modes_attempted": request_diag.get("response_modes_attempted", []),
                "response_headers": request_diag.get("response_headers", {}),
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
        visual_url_diag: dict[str, Any] = {}
        visual_search_error: BrightDataAPIError | None = None
        visual_fallback_skipped_reason = (
            "exact_matches_has_official_id" if exact_ids else None
        )

        # If exact_matches yields no official identifier, also inspect the
        # visual_matches tab. Titles/captions do not suppress this lookup.
        if not exact_ids:
            try:
                visual_results, visual_rows, visual_diag = self._request_lens_tab(
                    image_url, LENS_FALLBACK_TAB
                )
                visual_results = [{**row, "lens_tab": LENS_FALLBACK_TAB} for row in visual_results]
                visual_results = self.resolve_result_urls(visual_results)
                visual_url_diag = dict(self.last_result_url_diagnostics)
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
                visual_url_diag = {}
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
                attempts += int(request_diag.get("attempts") or 0)
                visual_url_diag = {}
                visual_search_error = exc
                tabs[LENS_FALLBACK_TAB] = {
                    "status": "error",
                    **request_diag,
                    "error": f"{type(exc).__name__}: {str(exc)[:250]}",
                }

        raw_paths: dict[str, int] = {}
        raw_paths_by_field: dict[str, dict[str, int]] = {}
        raw_fields: set[str] = set()
        raw_field_types: dict[str, set[str]] = {}
        for diag in tabs.values():
            raw_fields.update(diag.get("raw_result_fields") or [])
            for name, types in (diag.get("raw_result_field_types") or {}).items():
                raw_field_types.setdefault(name, set()).update(
                    str(value) for value in str(types).split("/")
                    if value
                )
            for key, value in (diag.get("raw_link_paths") or {}).items():
                raw_paths[key] = raw_paths.get(key, 0) + int(value or 0)
            for field, paths in (diag.get("raw_link_paths_by_field") or {}).items():
                field_paths = raw_paths_by_field.setdefault(field, {})
                for key, value in paths.items():
                    field_paths[key] = field_paths.get(key, 0) + int(value or 0)

        goto_diagnostics = {}
        for name in (
            "google_goto_candidates", "google_goto_resolved", "google_goto_failed",
            "google_goto_skipped", "google_goto_direct_urls_kept",
            "google_goto_resolution_limit",
        ):
            values = [exact_url_diag.get(name, 0), visual_url_diag.get(name, 0)]
            goto_diagnostics[name] = max(values) if name == "google_goto_resolution_limit" else sum(values)
        for name in ("google_goto_http_statuses", "google_goto_methods", "google_goto_error_kinds"):
            merged = {}
            for source in (exact_url_diag, visual_url_diag):
                for key, value in (source.get(name) or {}).items():
                    merged[key] = merged.get(key, 0) + int(value or 0)
            goto_diagnostics[name] = merged
        goto_diagnostics["google_goto_error_samples"] = []

        self.last_lens_diagnostics.update({
            **goto_diagnostics,
            "status": (
                "partial_error" if visual_search_error and combined
                else "error" if visual_search_error
                else "ok" if combined
                else "no_matches"
            ),
            "lens_tab": "+".join(tabs),
            "lens_tabs_attempted": list(tabs),
            "lens_tab_diagnostics": tabs,
            "http_status": tabs.get(LENS_FALLBACK_TAB, tabs[LENS_TAB]).get("http_status"),
            "data_format": tabs.get(LENS_FALLBACK_TAB, tabs[LENS_TAB]).get("data_format", "parsed_light"),
            "response_modes_attempted": {
                name: list(diag.get("response_modes_attempted") or [])
                for name, diag in tabs.items()
            },
            "response_headers": dict(
                tabs.get(LENS_FALLBACK_TAB, tabs[LENS_TAB]).get("response_headers") or {}
            ),
            "attempts": attempts,
            "rate_limit_retries": sum(
                int(item.get("rate_limit_retries") or 0) for item in tabs.values()
            ),
            "rate_limit_wait_seconds": sum(
                int(item.get("rate_limit_wait_seconds") or 0) for item in tabs.values()
            ),
            "raw_result_fields": sorted(raw_fields)[:40],
            "raw_result_field_types": {
                name: "/".join(sorted(types))
                for name, types in sorted(raw_field_types.items())[:30]
            },
            "raw_link_paths": dict(
                sorted(raw_paths.items(), key=lambda item: (-item[1], item[0]))[:12]
            ),
            "raw_link_paths_by_field": {
                field: dict(
                    sorted(paths.items(), key=lambda item: (-item[1], item[0]))[:8]
                )
                for field, paths in sorted(raw_paths_by_field.items())
            },
            "exact_matches_result_count": len(exact_results),
            "visual_matches_result_count": len(visual_results),
            "visual_fallback_skipped_reason": visual_fallback_skipped_reason,
            "raw_result_count": sum(
                int(item.get("raw_result_count") or 0) for item in tabs.values()
            ),
            "result_count": len(combined),
            "error": tabs.get(LENS_FALLBACK_TAB, {}).get("error"),
        })
        # A failed visual fallback means the combined Lens lookup is incomplete,
        # even when exact_matches returned title-only rows. Surface the provider
        # failure so the engine can preserve previously saved exact ID evidence.
        if visual_search_error is not None:
            raise visual_search_error
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
