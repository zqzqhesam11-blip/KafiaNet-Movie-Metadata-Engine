from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import unquote, urlencode, urlparse

import httpx

from app.image_evidence import _resolved_destination_url


BRIGHTDATA_REQUEST_URL = "https://api.brightdata.com/request"
GOOGLE_LENS_URL = "https://lens.google.com/uploadbyurl"
GOOGLE_SEARCH_URL = "https://www.google.com/search"
LENS_TAB = "exact_matches"
MAX_NORMALIZED_RESULTS = 80
MAX_TRANSIENT_RETRIES = 2
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
    def _lens_request_url(image_url: str) -> str:
        parsed = urlparse(str(image_url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise BrightDataAPIError(
                "Google Lens requires a public HTTPS poster URL without embedded credentials"
            )
        query = {
            "url": image_url,
            "hl": "en",
            "brd_json": "1",
            "brd_lens": LENS_TAB,
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
        except ValueError:
            return False
        if host == "imdb.com" or host.endswith(".imdb.com"):
            return bool(re.search(r"/title/tt\d{5,12}(?:/|$)", parsed.path, flags=re.I))
        if host == "themoviedb.org" or host.endswith(".themoviedb.org"):
            return bool(re.match(r"^/movie/\d+(?:[-/]|$)", parsed.path, flags=re.I))
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

    def _post_serp_with_retries(
        self,
        target_url: str,
        stage: str,
        *,
        parsed_light_header: bool = True,
        response_format: str = "raw",
    ) -> tuple[httpx.Response, Any, int, int]:
        """POST one SERP request and retry transport or outer/inner transient failures.

        Bright Data can return an HTTP-200 API envelope whose body carries the
        actual upstream HTTP status. Retry that status too; otherwise a wrapped
        Google 502 is incorrectly treated as a permanent per-film failure.
        """
        if response_format not in {"raw", "json"}:
            raise BrightDataAPIError(
                f"Unsupported Bright Data response format for {stage}: {response_format}"
            )

        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
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
                if attempt >= MAX_TRANSIENT_RETRIES:
                    raise BrightDataAPIError(
                        f"Bright Data {stage} transport failure after {attempt + 1} "
                        f"attempt(s) ({type(exc).__name__}): {str(exc)[:220]}"
                    ) from exc
                time.sleep(min(2 ** attempt, 2))
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

            # Bright Data has occasionally returned HTTP 200 with no body or
            # a JSON envelope whose inner body is empty. That is a failed delivery,
            # not a valid empty Lens result. Retry it with the same bounded backoff
            # used for transient gateway failures before classifying the film REVIEW.
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
                and attempt < MAX_TRANSIENT_RETRIES
            ):
                time.sleep(min(2 ** attempt, 2))
                continue

            return response, payload, effective_status, attempt + 1

        raise BrightDataAPIError(
            f"Bright Data {stage} ended without a response after {MAX_TRANSIENT_RETRIES + 1} attempts"
        )


    @staticmethod
    def _clean_title_hint(value: str) -> tuple[str, int | None]:
        """Clean a title obtained from Lens metadata or a direct Play Store result URL."""
        text = unquote(str(value or "")).replace("_", " ").replace("+", " ")
        # Result titles are untrusted data: keep them as plain quoted words,
        # never allow them to inject extra Google search operators.
        text = re.sub(r"[^\w\s'’\-]", " ", text, flags=re.UNICODE)
        text = re.sub(r"\s+", " ", text).strip(" \t\r\n-–—|:;,.")
        year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)
        year = int(year_match.group(1)) if year_match else None
        text = re.sub(r"[\[(](?:19|20)\d{2}[\])]", " ", text)
        text = re.sub(r"\s+(?:19|20)\d{2}\s*$", " ", text)
        text = re.sub(
            r"\s+(?:[-–—|:]\s*)?(?:google play|tmdb|the movie database|imdb|"
            r"wikipedia|youtube|official trailer|watch online)\s*$",
            "",
            text,
            flags=re.I,
        )
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
        """Collect search phrases only from Lens result titles and direct Play Store URLs.

        Source-folder labels and local image filenames are not accepted by this API.
        Google /goto wrappers are deliberately excluded because their destinations
        are not known.
        """
        hints: list[str] = []
        years: set[int] = set()

        def add(value: str, *, include_year: bool = True) -> None:
            title, year = cls._clean_title_hint(value)
            if include_year and year:
                years.add(year)
            if not title:
                return
            key = re.sub(r"\W+", " ", title.casefold()).strip()
            if not key or any(
                re.sub(r"\W+", " ", old.casefold()).strip() == key
                for old in hints
            ):
                return
            hints.append(title)

        for item in results or []:
            if not isinstance(item, dict) or item.get("error"):
                continue
            title_value = item.get("title")
            if isinstance(title_value, str):
                add(title_value)

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
                    add(slug, include_year=False)
                if len(hints) >= 3:
                    break
            if len(hints) >= 3:
                break

        # Only use a year explicitly present in the Lens result title text.
        # A year embedded in a Play Store slug is not considered a release year.
        return hints[:3], next(iter(years)) if len(years) == 1 else None

    @staticmethod
    def _google_official_title_search_url(
        title_hints: list[str],
        year: int | None,
    ) -> str:
        quoted = [f'"{value.replace(chr(34), " ").strip()}"' for value in title_hints if value.strip()]
        title_clause = " OR ".join(quoted)
        query = (
            f"({title_clause}) "
            "(site:themoviedb.org/movie OR site:imdb.com/title)"
        )
        if year:
            query += f" {int(year)}"
        # Use Bright Data's native structured SERP response format instead
        # of asking Google for JSON inside a raw response body.
        return f"{GOOGLE_SEARCH_URL}?{urlencode({'q': query, 'hl': 'en'})}"

    def search_official_movie_pages_from_lens(
        self,
        lens_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Search Google once for official TMDB/IMDb pages using Lens-derived titles only."""
        title_hints, year = self.title_hints_from_lens_results(lens_results)
        self.last_official_search_diagnostics = {
            "status": "no_title_hints" if not title_hints else "requesting",
            "query_count": 0,
            "title_hints": title_hints,
            "year_hint": year,
            "response_format": "json",
            "payload_shape": None,
            "http_status": None,
            "attempts": 0,
            "organic_result_count": 0,
            "official_url_count": 0,
            "error": None,
        }
        if not title_hints:
            return []

        target_url = self._google_official_title_search_url(title_hints, year)
        try:
            response, payload, effective_status, attempts = self._post_serp_with_retries(
                target_url,
                "Google official movie title search",
                parsed_light_header=False,
                response_format="json",
            )
        except BrightDataAPIError as exc:
            self.last_official_search_diagnostics.update({
                "status": "error",
                "query_count": 1,
                "attempts": MAX_TRANSIENT_RETRIES + 1,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
            raise

        body = getattr(response, "text", "") or ""
        self.last_official_search_diagnostics.update({
            "status": "http_error" if effective_status < 200 or effective_status >= 300 else "parsing",
            "query_count": 1,
            "http_status": effective_status,
            "attempts": attempts,
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
            self.last_official_search_diagnostics["error"] = message
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
            self.last_official_search_diagnostics.update({
                "status": "invalid_response",
                "error": message,
            })
            raise BrightDataAPIError(message)

        rows = self._rows_from_payload(payload)
        if rows is None:
            shape = self._payload_shape(payload)
            message = (
                "Bright Data Google title search returned JSON without a recognized organic result list; "
                + shape
            )
            self.last_official_search_diagnostics.update({
                "status": "unparsed_response",
                "error": message,
            })
            raise BrightDataAPIError(message)

        normalized = self._normalize_matches(rows)
        official_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidate_url_count = 0
        google_redirect_count = 0
        google_redirect_unwrapped_count = 0
        candidate_domains: dict[str, int] = {}

        for result in normalized:
            for value in (result.get("url_candidates") or []):
                if not isinstance(value, str) or not value.strip():
                    continue
                candidate_url_count += 1
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
                    google_redirect_count += 1

                # Google SERP may expose a click-tracking /url wrapper instead
                # of the publisher URL. Unwrap only its explicit q/url/adurl/
                # imgrefurl destination, then apply the same strict official
                # TMDB movie / IMDb title URL allow-list.
                destination_url = _resolved_destination_url(raw_url)
                if destination_url != raw_url:
                    google_redirect_unwrapped_count += 1

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
                if destination_url in seen:
                    continue
                seen.add(destination_url)
                official_rows.append({
                    **result,
                    "url": destination_url,
                    "url_candidates": [destination_url],
                    "resolved_urls": [destination_url],
                    "resolved_url": destination_url,
                    "provider": "google_serp_official_movie_title_search",
                    "search_source": "lens_result_title_or_direct_play_store_url",
                    "original_search_url": raw_url if raw_url != destination_url else None,
                })

        top_candidate_domains = dict(
            sorted(candidate_domains.items(), key=lambda item: (-item[1], item[0]))[:12]
        )
        self.last_official_search_diagnostics.update({
            "status": "ok" if official_rows else "no_official_results",
            "organic_result_count": len(rows),
            "candidate_url_count": candidate_url_count,
            "google_redirect_count": google_redirect_count,
            "google_redirect_unwrapped_count": google_redirect_unwrapped_count,
            "candidate_domains": top_candidate_domains,
            "official_url_count": len(official_rows),
            "error": None,
        })
        return official_rows

    def search_by_image_url(self, image_url: str) -> list[dict[str, Any]]:
        """Search one public poster URL with bounded retries for transient provider failures."""
        target_url = self._lens_request_url(image_url)
        self.last_lens_diagnostics = {
            "status": "requesting",
            "lens_tab": LENS_TAB,
            "data_format": "parsed_light",
            "http_status": None,
            "attempts": 0,
            "result_count": 0,
            "error": None,
        }
        try:
            response, payload, effective_status, attempts = self._post_serp_with_retries(
                target_url,
                "Google Lens image search",
            )
        except BrightDataAPIError as exc:
            self.last_lens_diagnostics.update({
                "status": "error",
                "attempts": MAX_TRANSIENT_RETRIES + 1,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
            raise

        body = getattr(response, "text", "") or ""
        self.last_lens_diagnostics.update({
            "http_status": effective_status,
            "attempts": attempts,
        })
        if effective_status < 200 or effective_status >= 300:
            error_payload = payload.get("body") if (
                isinstance(payload, dict) and "status_code" in payload and "body" in payload
            ) else payload
            message = self._http_error(
                effective_status,
                self._error_detail(error_payload, str(error_payload or body)),
            )
            self.last_lens_diagnostics.update({
                "status": "quota_error" if effective_status in {402, 429} else "http_error",
                "error": message,
            })
            if effective_status in {402, 429}:
                raise BrightDataQuotaError(effective_status, message)
            raise BrightDataAPIError(message)

        if payload is None:
            headers = getattr(response, "headers", {}) or {}
            content_type = headers.get("content-type", "unknown") if hasattr(headers, "get") else "unknown"
            content_length = headers.get("content-length", "unknown") if hasattr(headers, "get") else "unknown"
            message = (
                "Bright Data returned an empty or non-JSON response "
                f"(HTTP {effective_status}, content-type={content_type}, "
                f"content-length={content_length}); request uses format=raw, brd_json=1, and x-unblock-data-format=parsed_light"
            )
            self.last_lens_diagnostics.update({"status": "invalid_response", "error": message})
            raise BrightDataAPIError(message)

        if isinstance(payload, dict) and "status_code" in payload and "body" in payload:
            inner_body = payload.get("body")
            if not str(inner_body or "").strip():
                message = (
                    "Bright Data API envelope was received but its body is empty "
                    f"(inner HTTP {effective_status}); no Google Lens results were delivered"
                )
                self.last_lens_diagnostics.update({"status": "empty_body", "error": message})
                raise BrightDataAPIError(message)

        if isinstance(payload, dict) and any(payload.get(key) for key in ("error", "errors")):
            detail = self._error_detail(payload)
            message = f"Bright Data returned an API error: {detail or 'unknown provider error'}"
            self.last_lens_diagnostics.update({"status": "provider_error", "error": message})
            raise BrightDataAPIError(message)

        rows = self._rows_from_payload(payload)
        if rows is None:
            shape = self._payload_shape(payload)
            message = (
                "Bright Data returned JSON, but no recognized Google Lens result list was present; "
                + shape
            )
            self.last_lens_diagnostics.update({
                "status": "unparsed_response",
                "payload_shape": shape,
                "error": message,
            })
            raise BrightDataAPIError(message)

        self.last_lens_diagnostics.update(self._raw_result_diagnostics(rows))
        normalized = self._normalize_matches(rows)
        self.last_lens_diagnostics.update({
            "status": "ok" if normalized else "no_matches",
            "raw_result_count": len(rows),
            "result_count": len(normalized),
            "error": None,
        })
        return normalized

    @staticmethod
    def _http_error(status: int, detail: str) -> str:
        messages = {
            400: "Bright Data rejected the SERP request; verify the request URL and SERP zone",
            401: "Bright Data authentication failed; verify BRIGHTDATA_API_KEY in Hostless",
            402: "Bright Data reports no available free/API credits; do not add funds or enable a paid plan",
            403: "Bright Data denied this request; verify the SERP zone is active and permitted",
            429: "Bright Data request limit reached; stop processing and check the dashboard quota",
        }
        message = messages.get(status, "Bright Data service error; the image lookup did not return a usable response")
        suffix = f": {detail}" if detail else ""
        return f"{message} (HTTP {status}){suffix}"[:600]
