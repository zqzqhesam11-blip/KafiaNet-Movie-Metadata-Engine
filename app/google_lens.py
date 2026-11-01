from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx


BRIGHTDATA_REQUEST_URL = "https://api.brightdata.com/request"
GOOGLE_LENS_URL = "https://lens.google.com/uploadbyurl"
MAX_NORMALIZED_RESULTS = 80
MAX_GOTO_RESOLUTION_WORKERS = 8
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
        # Google Lens /goto links are opaque tokens. Resolve them with an
        # unauthenticated client; never send the Bright Data API key to Google.
        # First inspect Location, then use one bounded redirect-following fallback.
        self.redirect_client = httpx.Client(
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; KafiaNetMetadata/1.0)",
                "Referer": "https://lens.google.com/",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=12.0,
            follow_redirects=False,
        )
        self.last_lens_diagnostics: dict[str, Any] = {}
        self.last_official_search_diagnostics: dict[str, Any] = {}

    def close(self) -> None:
        self.client.close()
        self.redirect_client.close()

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

    def _resolve_goto_target(self, value: str) -> tuple[str, str | None]:
        if not self._is_google_goto_url(value):
            return value, None
        try:
            response = self.redirect_client.get(value)
            location = str(response.headers.get("location") or "").strip()
            if location:
                destination = urljoin(value, location)
                parsed = urlparse(destination)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    return "", "Google /goto returned an invalid destination URL"
                return destination, None

            # A bounded fallback for Google edges that omit Location but still
            # expose the final page when redirects are followed.
            followed = self.redirect_client.get(value, follow_redirects=True)
            final_url = str(getattr(followed, "url", "") or "").strip()
            parsed = urlparse(final_url)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.netloc
                and not self._is_google_goto_url(final_url)
            ):
                return final_url, None
            return "", (
                f"Google /goto returned HTTP {response.status_code} without a usable "
                "Location or followed destination"
            )
        except httpx.HTTPError as exc:
            return "", f"Google /goto resolution failed ({type(exc).__name__})"

    def resolve_result_urls(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Resolve all URL candidates while preserving directly available official links."""
        prepared = [dict(item) for item in (results or []) if isinstance(item, dict)]
        work: list[tuple[int, str]] = []
        for index, item in enumerate(prepared):
            candidates = item.get("url_candidates")
            if not isinstance(candidates, list):
                candidates = []
            original_url = str(item.get("url") or "").strip()
            if original_url and original_url not in candidates:
                candidates = [original_url, *candidates]
            candidates = list(dict.fromkeys(
                str(value).strip()
                for value in candidates
                if isinstance(value, str) and str(value).strip()
            ))
            item["url_candidates"] = candidates
            if str(item.get("resolved_url") or "").strip() and not item.get("resolved_urls"):
                item["resolved_urls"] = [str(item["resolved_url"]).strip()]
            if item.get("resolved_urls"):
                continue
            for candidate in candidates:
                work.append((index, candidate))

        if not work:
            return prepared

        with ThreadPoolExecutor(max_workers=MAX_GOTO_RESOLUTION_WORKERS) as executor:
            resolved = list(executor.map(
                lambda pair: self._resolve_goto_target(pair[1]),
                work,
            ))

        destinations: dict[int, list[str]] = {}
        errors: dict[int, list[str]] = {}
        for (index, _original), (destination, error) in zip(work, resolved):
            if destination:
                destinations.setdefault(index, []).append(destination)
            if error:
                errors.setdefault(index, []).append(error)

        for index, item in enumerate(prepared):
            unique_destinations = list(dict.fromkeys(destinations.get(index, [])))
            item["resolved_urls"] = unique_destinations
            item["resolved_url"] = unique_destinations[0] if unique_destinations else ""
            item_errors = errors.get(index, [])
            if item_errors:
                item["url_resolution_error"] = item_errors[0]
                item["url_resolution_errors"] = item_errors[:10]
            else:
                item.pop("url_resolution_error", None)
                item.pop("url_resolution_errors", None)
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
            "brd_lens": "visual_matches",
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
    def _rows_from_payload(payload: Any, depth: int = 0) -> list[dict[str, Any]] | None:
        """Handle Lens arrays and Bright Data's occasional JSON/body wrappers."""
        if depth > 6:
            return None
        if isinstance(payload, str):
            text = payload.strip()
            if text.startswith(("{", "[")):
                try:
                    return BrightDataGoogleLensClient._rows_from_payload(json.loads(text), depth + 1)
                except (TypeError, ValueError):
                    return None
            return None
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)] if all(
                isinstance(item, dict) for item in payload
            ) else None
        if not isinstance(payload, dict):
            return None

        # Google Lens normally returns visual_matches. Bright Data's parsed image
        # response can also expose the equivalent ranked items under images.
        for key in (
            "visual_matches", "exact_matches", "lens_results", "images",
            "similar_images", "organic", "organic_results", "web_results",
            "search_results",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)] if all(
                    isinstance(item, dict) for item in value
                ) else None

        # Handle structured wrappers and JSON-encoded body fields. results is
        # a fallback for provider variants whose result list is generic.
        for key in (
            "data", "result", "results", "response", "body", "content",
            "raw", "output", "payload", "lens",
        ):
            value = payload.get(key)
            if isinstance(value, (dict, list, str)):
                rows = BrightDataGoogleLensClient._rows_from_payload(value, depth + 1)
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
                        or candidate_value.get("src")
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
                item, ("link", "url", "page_url", "website", "source_url", "landing_page")
            )
            page_url = page_urls[0] if page_urls else ""
            # Bright Data's image schema uses original_image for the full-size
            # image and image for its preview; keep the full-size URL first.
            image_url = cls._first_url(
                item,
                ("original_image", "image_url", "original", "source", "image", "thumbnail", "thumbnail_url"),
            )
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
        try:
            return response.json()
        except (ValueError, TypeError):
            return None

    def _post_serp_with_retries(
        self,
        target_url: str,
        stage: str,
    ) -> tuple[httpx.Response, Any, int, int]:
        """POST one SERP request and retry transport or outer/inner transient failures.

        Bright Data can return an HTTP-200 API envelope whose body carries the
        actual upstream HTTP status. Retry that status too; otherwise a wrapped
        Google 502 is incorrectly treated as a permanent per-film failure.
        """
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            try:
                response = self.client.post(
                    BRIGHTDATA_REQUEST_URL,
                    json={"zone": self.zone, "url": target_url, "format": "raw"},
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

            if effective_status in TRANSIENT_HTTP_STATUSES and attempt < MAX_TRANSIENT_RETRIES:
                time.sleep(min(2 ** attempt, 2))
                continue

            return response, payload, effective_status, attempt + 1

        raise BrightDataAPIError(
            f"Bright Data {stage} ended without a response after {MAX_TRANSIENT_RETRIES + 1} attempts"
        )

    def search_by_image_url(self, image_url: str) -> list[dict[str, Any]]:
        """Search one public poster URL with bounded retries for transient provider failures."""
        target_url = self._lens_request_url(image_url)
        self.last_lens_diagnostics = {
            "status": "requesting",
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
                f"content-length={content_length}); request uses format=raw and brd_json=1"
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
