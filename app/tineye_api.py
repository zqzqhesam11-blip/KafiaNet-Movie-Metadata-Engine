from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlparse

import httpx


TINEYE_API_BASE_URL = "https://api.tineye.com/rest/"
HTTP_OK = 200
MAX_NORMALIZED_RESULTS = 300
MAX_BACKLINKS_PER_MATCH = 3


class TinEyeAPIError(RuntimeError):
    """Safe, actionable error from the official TinEye API."""


class TinEyeAPIClient:
    """Client for TinEye's official REST API using the X-API-KEY header."""

    def __init__(self, api_key: str, timeout: float = 60.0):
        key = str(api_key or "").strip()
        if not key:
            raise RuntimeError("TINEYE_API_KEY is required for TinEye API access")
        self.client = httpx.Client(
            headers={"Accept": "application/json", "X-API-KEY": key},
            timeout=max(15.0, float(timeout)),
        )

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _messages(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        values = payload.get("messages") or []
        if isinstance(values, str):
            return values[:300]
        if isinstance(values, list):
            parts = [
                str(item.get("message") or item.get("description") or item)
                if isinstance(item, dict) else str(item)
                for item in values
            ]
            return "; ".join(part for part in parts if part)[:300]
        return str(values)[:300]

    @staticmethod
    def _error_message(http_status: int, api_code: int | None, detail: str) -> str:
        code = api_code or http_status
        actions = {
            400: "TinEye rejected the image URL; confirm it is public HTTPS and points directly to a supported image",
            401: "TinEye API authentication failed; check the TINEYE_API_KEY Hostless variable",
            402: "TinEye search bundle is exhausted; purchase or add search credits",
            403: "TinEye API access was denied; check account/API permissions",
            429: "TinEye API rate limit reached; reduce request frequency and retry later",
        }
        action = (
            "TinEye API service error; retry after the provider recovers"
            if code >= 500 else actions.get(code, f"TinEye API request failed with code {code}")
        )
        suffix = f": {detail}" if detail else ""
        return f"{action} (HTTP {http_status}, API {api_code}){suffix}"[:600]

    def _get(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{TINEYE_API_BASE_URL}{method.strip('/')}/"
        try:
            response = self.client.get(url, params=params or {})
        except httpx.HTTPError as exc:
            raise TinEyeAPIError(
                f"TinEye API network error ({type(exc).__name__}): {str(exc)[:250]}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise TinEyeAPIError(
                f"TinEye API returned non-JSON data (HTTP {response.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise TinEyeAPIError("TinEye API returned an invalid response object")
        try:
            api_code = int(payload.get("code")) if payload.get("code") is not None else None
        except (TypeError, ValueError):
            api_code = None
        if response.status_code != HTTP_OK or api_code != HTTP_OK:
            raise TinEyeAPIError(
                self._error_message(response.status_code, api_code, self._messages(payload))
            )
        return payload

    @staticmethod
    def _image_name(image_url: str) -> str:
        path = unquote(urlparse(image_url).path)
        return path.rsplit("/", 1)[-1][:180] if path else ""

    def search_by_image_url(self, image_url: str) -> list[dict[str, Any]]:
        """Search a publicly reachable poster URL and normalize API backlinks."""
        parsed = urlparse(str(image_url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise TinEyeAPIError("TinEye requires a publicly reachable HTTPS image URL")
        payload = self._get("search", {
            "image_url": image_url,
            "offset": 0,
            "limit": 100,
            "sort": "score",
            "order": "desc",
        })
        data = payload.get("results") or {}
        if not isinstance(data, dict):
            raise TinEyeAPIError("TinEye API response field 'results' is not an object")
        matches = data.get("matches") or []
        if not isinstance(matches, list):
            raise TinEyeAPIError("TinEye API response field 'results.matches' is not a list")
        normalized: list[dict[str, Any]] = []
        for match in matches:
            if not isinstance(match, dict):
                continue
            backlinks = match.get("backlinks") or []
            if not isinstance(backlinks, list):
                continue
            for backlink in backlinks[:MAX_BACKLINKS_PER_MATCH]:
                if not isinstance(backlink, dict):
                    continue
                source_url = str(backlink.get("url") or match.get("image_url") or "").strip()
                page_url = str(backlink.get("backlink") or "").strip()
                if not source_url and not page_url:
                    continue
                title = str(
                    backlink.get("image_name") or self._image_name(source_url)
                    or match.get("domain") or ""
                ).strip()
                normalized.append({
                    "title": title,
                    "url": page_url or source_url,
                    "source": source_url,
                    "content": "",
                    "match_score": match.get("score"),
                    "match_domain": match.get("domain"),
                })
                if len(normalized) >= MAX_NORMALIZED_RESULTS:
                    return normalized
        return normalized

    def remaining_searches(self) -> dict[str, Any]:
        """Check API credentials and available bundles without searching an image."""
        payload = self._get("remaining_searches")
        result = payload.get("results") or {}
        if not isinstance(result, dict):
            raise TinEyeAPIError("TinEye API returned invalid remaining-searches data")
        return result
