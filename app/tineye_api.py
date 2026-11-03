from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

import httpx


TINEYE_API_BASE_URL = "https://api.tineye.com/rest/"
HTTP_OK = 200
MAX_NORMALIZED_RESULTS = 300
MAX_BACKLINKS_PER_MATCH = 3
MAX_TITLE_HINTS = 5
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif")
GENERIC_FILENAME_WORDS = {
    "poster", "posters", "cover", "covers", "artwork", "art", "image",
    "images", "photo", "photos", "wallpaper", "fanart", "thumb",
    "thumbnail", "original", "small", "large", "front", "back",
}


class TinEyeAPIError(RuntimeError):
    """Safe, actionable error from the official TinEye API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        api_code: int | None = None,
    ):
        self.status_code = status_code
        self.api_code = api_code
        super().__init__(message)


class TinEyeAPIClient:
    """Official TinEye REST client; never scrapes the public TinEye website."""

    def __init__(self, api_key: str, timeout: float = 60.0):
        key = str(api_key or "").strip()
        if not key:
            raise RuntimeError("TINEYE_API_KEY is required for TinEye API access")
        self.client = httpx.Client(
            headers={"Accept": "application/json", "X-API-KEY": key},
            timeout=max(15.0, float(timeout)),
        )
        self.last_search_diagnostics: dict[str, Any] = {}
        # Compatibility names are consumed by the current engine diagnostics.
        self.last_lens_diagnostics: dict[str, Any] = {}
        self.last_official_search_diagnostics: dict[str, Any] = {
            "status": "not_run",
            "query_count": 0,
            "query_variants": [],
            "title_hints": [],
            "attempts": 0,
            "organic_result_count": 0,
            "official_url_count": 0,
        }

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
            402: "TinEye search bundle is exhausted; check the account's remaining search credits",
            403: "TinEye API access was denied; check account/API permissions",
            429: "TinEye API rate limit reached; stop the batch and retry later",
        }
        action = (
            "TinEye API service error; retry only after the provider recovers"
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
                f"TinEye API returned non-JSON data (HTTP {response.status_code})",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise TinEyeAPIError(
                "TinEye API returned an invalid response object",
                status_code=response.status_code,
            )

        try:
            api_code = int(payload.get("code")) if payload.get("code") is not None else None
        except (TypeError, ValueError):
            api_code = None

        if response.status_code != HTTP_OK or api_code != HTTP_OK:
            raise TinEyeAPIError(
                self._error_message(response.status_code, api_code, self._messages(payload)),
                status_code=response.status_code,
                api_code=api_code,
            )
        return payload

    @staticmethod
    def _image_name(image_url: str) -> str:
        path = unquote(urlparse(str(image_url or "")).path)
        return path.rsplit("/", 1)[-1][:180] if path else ""

    @staticmethod
    def _clean_external_image_name(value: str) -> tuple[str, int | None]:
        """Normalize only a TinEye-returned matched-image name, never a local filename."""
        text = unquote(str(value or "")).strip()
        if not text:
            return "", None
        if "://" in text:
            text = TinEyeAPIClient._image_name(text)
        text = text.rsplit("/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
        text = re.sub(r"(?i)\\.(?:jpe?g|png|webp|gif|bmp|tiff?|avif)$", "", text)
        text = re.sub(r"[_+]+", " ", text)
        text = re.sub(r"[-.]+", " ", text)
        text = re.sub(r"\\s+", " ", text).strip(" _-.,()[]")
        year_match = re.search(r"(?<!\\d)((?:18|19|20)\\d{2})(?!\\d)", text)
        year = int(year_match.group(1)) if year_match else None
        if year_match:
            text = text[:year_match.start()] + " " + text[year_match.end():]
        words = [word for word in text.split() if word.casefold() not in GENERIC_FILENAME_WORDS]
        title = " ".join(words).strip()
        if len(title) < 2 or not re.search(r"[A-Za-zÀ-ž]", title):
            return "", year
        if title.casefold() in {"movie", "film", "poster", "cover", "image"}:
            return "", year
        return title[:180], year

    @classmethod
    def title_hints_from_reverse_image_results(
        cls, results: list[dict[str, Any]]
    ) -> tuple[list[str], int | None]:
        """Extract review-only hints from TinEye's matched-image names."""
        output: list[str] = []
        seen: set[str] = set()
        years: set[int] = set()
        for row in results or []:
            if not isinstance(row, dict) or row.get("error"):
                continue
            title, year = cls._clean_external_image_name(str(row.get("title") or ""))
            if year:
                years.add(year)
            key = re.sub(r"\\W+", " ", title.casefold()).strip()
            if not title or not key or key in seen:
                continue
            seen.add(key)
            output.append(title)
            if len(output) >= MAX_TITLE_HINTS:
                break
        return output, (next(iter(years)) if len(years) == 1 else None)

    def resolve_result_urls(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Expose TinEye backlinks as direct destinations for strict host validation."""
        prepared = [dict(item) for item in (results or []) if isinstance(item, dict)]
        direct_count = 0
        for item in prepared:
            url = str(item.get("url") or "").strip()
            parsed = urlparse(url)
            direct_urls = (
                [url]
                if parsed.scheme in {"https", "http"} and parsed.netloc
                else []
            )
            item["url_candidates"] = direct_urls
            item["resolved_urls"] = direct_urls
            item["resolved_url"] = direct_urls[0] if direct_urls else ""
            direct_count += len(direct_urls)
        self.last_lens_diagnostics.update({
            "provider": "tineye_official_api",
            "direct_backlink_urls": direct_count,
        })
        return prepared

    def search_by_image_url(self, image_url: str) -> list[dict[str, Any]]:
        """Search a public HTTPS poster URL and normalize official API backlinks."""
        parsed = urlparse(str(image_url or "").strip())
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise TinEyeAPIError(
                "TinEye requires a publicly reachable HTTPS image URL without embedded credentials",
                status_code=400,
                api_code=400,
            )

        self.last_search_diagnostics = {
            "provider": "tineye_official_api",
            "status": "requesting",
            "http_status": None,
            "attempts": 1,
            "match_count": 0,
            "backlink_count": 0,
            "result_count": 0,
        }
        self.last_lens_diagnostics = dict(self.last_search_diagnostics)
        try:
            payload = self._get("search", {
                "image_url": image_url,
                "offset": 0,
                "limit": 100,
                "sort": "score",
                "order": "desc",
            })
        except TinEyeAPIError as exc:
            self.last_search_diagnostics.update({
                "status": "error",
                "http_status": exc.status_code,
                "api_code": exc.api_code,
                "error": str(exc)[:400],
            })
            self.last_lens_diagnostics = dict(self.last_search_diagnostics)
            raise

        data = payload.get("results") or {}
        if not isinstance(data, dict):
            raise TinEyeAPIError("TinEye API response field 'results' is not an object")
        matches = data.get("matches") or []
        if not isinstance(matches, list):
            raise TinEyeAPIError("TinEye API response field 'results.matches' is not a list")

        normalized: list[dict[str, Any]] = []
        backlink_count = 0
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
                image_name = str(
                    backlink.get("image_name") or self._image_name(source_url)
                ).strip()
                normalized.append({
                    "title": image_name,
                    "url": page_url or source_url,
                    "url_candidates": [page_url] if page_url else ([source_url] if source_url else []),
                    "resolved_url": page_url or "",
                    "resolved_urls": [page_url] if page_url else [],
                    "source": source_url,
                    "content": "",
                    "provider": "tineye_official_api",
                    "match_score": match.get("score"),
                    "match_domain": match.get("domain"),
                    "crawl_date": backlink.get("crawl_date"),
                })
                backlink_count += 1
                if len(normalized) >= MAX_NORMALIZED_RESULTS:
                    break
            if len(normalized) >= MAX_NORMALIZED_RESULTS:
                break

        self.last_search_diagnostics.update({
            "status": "ok" if normalized else "no_matches",
            "http_status": HTTP_OK,
            "match_count": len(matches),
            "backlink_count": backlink_count,
            "result_count": len(normalized),
        })
        self.last_lens_diagnostics = dict(self.last_search_diagnostics)
        return normalized

    def remaining_searches(self) -> dict[str, Any]:
        """Verify API access and available search bundles without searching an image."""
        payload = self._get("remaining_searches")
        result = payload.get("results") or {}
        if not isinstance(result, dict):
            raise TinEyeAPIError("TinEye API returned invalid remaining-searches data")
        return result
