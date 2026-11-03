from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx


VISION_ANNOTATE_URL = "https://vision.googleapis.com/v1/images:annotate"
TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}
MAX_RETRIES = 2
MAX_TEXT_CHARS = 5000


class GoogleVisionAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_code: str | None = None,
    ):
        self.status_code = status_code
        self.provider_code = provider_code
        super().__init__(message)


class GoogleVisionQuotaError(GoogleVisionAPIError):
    pass


class GoogleVisionClient:
    """Optional OCR and reverse-image web evidence through Google Cloud Vision.

    The client deliberately uses only full-image page matches as identity
    evidence. Partial matches, visually similar images, web entities and OCR
    text are retained as discovery clues, never as proof of a movie identity.
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = 45.0,
        transport: httpx.BaseTransport | None = None,
    ):
        key = str(api_key or "").strip()
        if not key:
            raise RuntimeError("GOOGLE_VISION_API_KEY is required when Google Vision is enabled")
        self.api_key = key
        self.client = httpx.Client(
            timeout=max(15.0, float(timeout)),
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        self.last_diagnostics: dict[str, Any] = {}

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _validate_image_url(image_url: str) -> str:
        value = str(image_url or "").strip()
        try:
            parsed = urlparse(value)
        except ValueError as exc:
            raise ValueError("Invalid poster URL") from exc
        host = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme != "https"
            or host != "raw.githubusercontent.com"
            or not parsed.path
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "Google Vision accepts only the engine's public raw.githubusercontent.com poster URLs"
            )
        return value

    @staticmethod
    def _extract_text(annotation: dict[str, Any]) -> str:
        full_text = annotation.get("fullTextAnnotation")
        if isinstance(full_text, dict) and isinstance(full_text.get("text"), str):
            return full_text["text"][:MAX_TEXT_CHARS]
        rows = annotation.get("textAnnotations") or []
        if rows and isinstance(rows[0], dict):
            return str(rows[0].get("description") or "")[:MAX_TEXT_CHARS]
        return ""

    @staticmethod
    def _language_codes(annotation: dict[str, Any]) -> list[str]:
        found: list[str] = []
        full_text = annotation.get("fullTextAnnotation") or {}
        for page in full_text.get("pages") or []:
            prop = page.get("property") or {}
            for language in prop.get("detectedLanguages") or []:
                code = str(language.get("languageCode") or "").strip()
                if code and code not in found:
                    found.append(code)
        return found[:12]

    @staticmethod
    def _title_hints(text: str) -> list[str]:
        """Return short OCR lines as search hints only; never assign identity."""
        ignored = (
            "directed by", "written by", "produced by", "starring",
            "executive producer", "coming soon", "in cinemas",
            "a film by", "based on the", "official trailer",
        )
        hints: list[str] = []
        for raw_line in str(text or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip(" \t-–—|:;,.")
            folded = line.casefold()
            words = re.findall(r"[^\W_]+", line, flags=re.UNICODE)
            if not (4 <= len(line) <= 80 and 1 <= len(words) <= 9):
                continue
            if any(term in folded for term in ignored):
                continue
            if not any(char.isalpha() for char in line):
                continue
            if line.casefold() not in {item.casefold() for item in hints}:
                hints.append(line)
            if len(hints) >= 6:
                break
        return hints

    @staticmethod
    def _exact_page_matches(web_detection: dict[str, Any]) -> list[dict[str, Any]]:
        """Keep only pages explicitly carrying a full (not partial) image match."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in web_detection.get("pagesWithMatchingImages") or []:
            if not isinstance(page, dict):
                continue
            full_matches = [
                item for item in (page.get("fullMatchingImages") or [])
                if isinstance(item, dict) and str(item.get("url") or "").startswith("https://")
            ]
            page_url = str(page.get("url") or "").strip()
            parsed = urlparse(page_url)
            if not full_matches or parsed.scheme != "https" or not parsed.netloc or page_url in seen:
                continue
            seen.add(page_url)
            rows.append({
                "title": str(page.get("pageTitle") or "")[:300],
                "url": page_url[:1500],
                "resolved_url": page_url[:1500],
                "resolved_urls": [page_url[:1500]],
                "provider": "google_cloud_vision_web_detection",
                "match_type": "full_image_match",
                "full_matching_image_urls": [
                    str(item["url"])[:1500] for item in full_matches[:5]
                ],
            })
            if len(rows) >= 20:
                break
        return rows

    def analyze_image_url(self, image_url: str) -> dict[str, Any]:
        image_uri = self._validate_image_url(image_url)
        request_body = {
            "requests": [{
                "image": {"source": {"imageUri": image_uri}},
                "features": [
                    {"type": "WEB_DETECTION", "maxResults": 10},
                    {"type": "TEXT_DETECTION"},
                ],
            }]
        }

        response: httpx.Response | None = None
        attempts = 0
        for attempt in range(MAX_RETRIES + 1):
            attempts = attempt + 1
            try:
                response = self.client.post(
                    VISION_ANNOTATE_URL,
                    params={"key": self.api_key},
                    json=request_body,
                )
            except httpx.HTTPError as exc:
                self.last_diagnostics = {
                    "status": "transport_error",
                    "http_status": None,
                    "attempts": attempts,
                    "error_type": type(exc).__name__,
                }
                if attempt < MAX_RETRIES:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise GoogleVisionAPIError(
                    f"Google Cloud Vision transport failure ({type(exc).__name__})"
                ) from None

            status = response.status_code
            if status == 429:
                self.last_diagnostics = {
                    "status": "quota_or_throttle",
                    "http_status": status,
                    "attempts": attempts,
                }
                raise GoogleVisionQuotaError(
                    "Google Cloud Vision quota or rate limit reached",
                    status_code=status,
                )
            if status in TRANSIENT_HTTP_STATUSES and attempt < MAX_RETRIES:
                time.sleep(0.4 * (attempt + 1))
                continue
            if status >= 400:
                self.last_diagnostics = {
                    "status": "http_error",
                    "http_status": status,
                    "attempts": attempts,
                }
                raise GoogleVisionAPIError(
                    f"Google Cloud Vision returned HTTP {status}",
                    status_code=status,
                )
            break

        if response is None:
            raise GoogleVisionAPIError("Google Cloud Vision returned no response")

        try:
            payload = response.json()
        except ValueError:
            self.last_diagnostics = {
                "status": "invalid_json",
                "http_status": response.status_code,
                "attempts": attempts,
            }
            raise GoogleVisionAPIError("Google Cloud Vision returned invalid JSON") from None

        responses = payload.get("responses") if isinstance(payload, dict) else None
        annotation = responses[0] if isinstance(responses, list) and responses else {}
        if not isinstance(annotation, dict):
            annotation = {}

        api_error = annotation.get("error")
        if isinstance(api_error, dict):
            provider_code = str(api_error.get("status") or "API_ERROR")
            if provider_code in {"RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"}:
                self.last_diagnostics = {
                    "status": "quota_or_throttle",
                    "http_status": response.status_code,
                    "provider_code": provider_code,
                    "attempts": attempts,
                }
                raise GoogleVisionQuotaError(
                    "Google Cloud Vision quota or rate limit reached",
                    status_code=response.status_code,
                    provider_code=provider_code,
                )
            self.last_diagnostics = {
                "status": "api_error",
                "http_status": response.status_code,
                "provider_code": provider_code,
                "attempts": attempts,
            }
            raise GoogleVisionAPIError(
                f"Google Cloud Vision API error ({provider_code})",
                status_code=response.status_code,
                provider_code=provider_code,
            )

        web_detection = annotation.get("webDetection") or {}
        text = self._extract_text(annotation)
        exact_page_matches = self._exact_page_matches(web_detection)
        full_matches = [
            str(item.get("url") or "")[:1500]
            for item in (web_detection.get("fullMatchingImages") or [])
            if isinstance(item, dict) and str(item.get("url") or "").startswith("https://")
        ]
        partial_matches = [
            str(item.get("url") or "")[:1500]
            for item in (web_detection.get("partialMatchingImages") or [])
            if isinstance(item, dict) and str(item.get("url") or "").startswith("https://")
        ]
        similar_images = [
            str(item.get("url") or "")[:1500]
            for item in (web_detection.get("visuallySimilarImages") or [])
            if isinstance(item, dict) and str(item.get("url") or "").startswith("https://")
        ]
        best_guess_labels = [
            str(item.get("label") or "")[:120]
            for item in (web_detection.get("bestGuessLabels") or [])
            if isinstance(item, dict) and item.get("label")
        ][:10]
        web_entities = [
            {
                "description": str(item.get("description") or "")[:160],
                "score": item.get("score"),
            }
            for item in (web_detection.get("webEntities") or [])
            if isinstance(item, dict) and item.get("description")
        ][:20]

        self.last_diagnostics = {
            "status": "ok",
            "http_status": response.status_code,
            "attempts": attempts,
            "official_exact_page_matches": len(exact_page_matches),
            "full_matching_images": len(full_matches),
            "partial_matching_images": len(partial_matches),
            "visually_similar_images": len(similar_images),
            "ocr_chars": len(text),
            "ocr_language_codes": self._language_codes(annotation),
        }
        return {
            "status": "ok",
            "official_page_matches": exact_page_matches,
            "ocr_text": text,
            "ocr_title_hints": self._title_hints(text),
            "ocr_language_codes": self._language_codes(annotation),
            "best_guess_labels": best_guess_labels,
            "web_entities": web_entities,
            "full_matching_image_urls": full_matches[:20],
            "partial_matching_image_urls": partial_matches[:20],
            "visually_similar_image_urls": similar_images[:20],
            "diagnostics": dict(self.last_diagnostics),
        }
