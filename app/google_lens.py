from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx


BRIGHTDATA_REQUEST_URL = "https://api.brightdata.com/request"
GOOGLE_LENS_URL = "https://lens.google.com/uploadbyurl"
MAX_NORMALIZED_RESULTS = 80


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

    def close(self) -> None:
        self.client.close()

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
        for key in ("visual_matches", "exact_matches", "lens_results", "images", "similar_images"):
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
    def _normalize_matches(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in rows[:MAX_NORMALIZED_RESULTS]:
            title = str(item.get("title") or item.get("name") or "").strip()[:300]
            page_url = cls._first_url(item, ("link", "url", "page_url", "website", "source_url"))
            # Bright Data's image schema uses original_image for the full-size
            # image and image for its preview; keep the full-size URL first.
            image_url = cls._first_url(
                item,
                ("original_image", "image_url", "original", "image", "thumbnail", "thumbnail_url"),
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
                "source": image_url,
                "content": description,
                "provider": "google_lens_brightdata",
                "position": item.get("position") or item.get("global_rank") or item.get("rank"),
            })
        return normalized

    def search_by_image_url(self, image_url: str) -> list[dict[str, Any]]:
        """Search a public poster URL; one SERP API request per poster, no automatic retry."""
        target_url = self._lens_request_url(image_url)
        try:
            response = self.client.post(
                BRIGHTDATA_REQUEST_URL,
                json={"zone": self.zone, "url": target_url, "format": "raw"},
            )
        except httpx.HTTPError as exc:
            raise BrightDataAPIError(
                f"Bright Data network error ({type(exc).__name__}): {str(exc)[:250]}"
            ) from exc

        body = response.text or ""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code < 200 or response.status_code >= 300:
            message = self._http_error(response.status_code, self._error_detail(payload, body))
            if response.status_code in {402, 429}:
                raise BrightDataQuotaError(response.status_code, message)
            raise BrightDataAPIError(message)
        if payload is None:
            raise BrightDataAPIError(
                "Bright Data did not return parsed JSON for Google Lens (brd_json=1); "
                f"response began with: {body[:180]!r}"
            )
        if isinstance(payload, dict) and any(payload.get(key) for key in ("error", "errors")):
            detail = self._error_detail(payload)
            raise BrightDataAPIError(f"Bright Data returned an API error: {detail or 'unknown provider error'}")

        rows = self._rows_from_payload(payload)
        if rows is None:
            raise BrightDataAPIError(
                "Bright Data returned JSON, but no recognized Google Lens result list was present; "
                + self._payload_shape(payload)
            )
        return self._normalize_matches(rows)

    @staticmethod
    def _http_error(status: int, detail: str) -> str:
        messages = {
            400: "Bright Data rejected the Google Lens request; verify the poster URL and SERP zone",
            401: "Bright Data authentication failed; verify BRIGHTDATA_API_KEY in Hostless",
            402: "Bright Data reports no available free/API credits; do not add funds or enable a paid plan",
            403: "Bright Data denied this request; verify the SERP zone is active and permitted",
            429: "Bright Data request limit reached; stop processing and check the dashboard quota",
        }
        message = messages.get(status, "Bright Data service error; the image lookup did not return a usable response")
        suffix = f": {detail}" if detail else ""
        return f"{message} (HTTP {status}){suffix}"[:600]
