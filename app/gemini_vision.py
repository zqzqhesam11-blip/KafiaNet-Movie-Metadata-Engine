from __future__ import annotations

import base64
import json
import math
from typing import Any
from urllib.parse import urlparse

import httpx


GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
MAX_POSTER_BYTES = 12 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MIN_CANDIDATE_CONFIDENCE = 0.85

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_movie_poster": {"type": "boolean"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "year": {"type": "integer"},
                    "director": {"type": "string"},
                    "confidence": {"type": "number"},
                    "visible_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "title",
                    "year",
                    "director",
                    "confidence",
                    "visible_evidence",
                ],
                "additionalProperties": False,
            },
        },
        "poster_text": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["is_movie_poster", "candidates", "poster_text", "notes"],
    "additionalProperties": False,
}


class GeminiVisionError(RuntimeError):
    """A safe, credential-free Gemini vision request error."""


class GeminiPosterVision:
    """Identify likely movie titles from poster pixels using Gemini's vision API.

    The model output is only a candidate signal. Callers must independently
    verify title, year, and director against TMDB before accepting an identity.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.8-flash",
        timeout: float = 60.0,
        max_image_bytes: int = MAX_POSTER_BYTES,
    ):
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "gemini-3.8-flash").strip()
        if not self.api_key:
            raise ValueError("Gemini API key is required")
        if not self.model or len(self.model) > 100:
            raise ValueError("Invalid Gemini model name")
        self.timeout = max(10.0, float(timeout))
        self.max_image_bytes = max(1024, min(int(max_image_bytes), MAX_POSTER_BYTES))
        self.client = httpx.Client(timeout=self.timeout, follow_redirects=False)

    @staticmethod
    def validate_image_url(image_url: str) -> str:
        """Accept only the public raw GitHub host used for poster image blobs."""
        value = str(image_url or "").strip()
        try:
            parsed = urlparse(value)
        except ValueError as exc:
            raise ValueError("Invalid poster image URL") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "raw.githubusercontent.com"
            or parsed.username
            or parsed.password
            or not parsed.path
            or parsed.port not in (None, 443)
        ):
            raise ValueError(
                "Gemini vision accepts only HTTPS raw.githubusercontent.com poster URLs"
            )
        return value

    @staticmethod
    def _detect_mime(image_bytes: bytes, header_value: str | None) -> str:
        declared = str(header_value or "").split(";", 1)[0].strip().lower()
        if declared == "image/jpg":
            declared = "image/jpeg"
        detected = None
        if image_bytes.startswith(b"\xff\xd8\xff"):
            detected = "image/jpeg"
        elif image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            detected = "image/png"
        elif (
            len(image_bytes) >= 12
            and image_bytes[:4] == b"RIFF"
            and image_bytes[8:12] == b"WEBP"
        ):
            detected = "image/webp"
        if detected and declared in SUPPORTED_IMAGE_TYPES and detected != declared:
            raise ValueError("Poster image signature does not match its content type")
        mime_type = detected or declared
        if mime_type not in SUPPORTED_IMAGE_TYPES:
            raise ValueError("Poster must be a JPEG, PNG, or WEBP image")
        return mime_type

    def _download_poster(self, image_url: str) -> tuple[bytes, str]:
        url = self.validate_image_url(image_url)
        chunks: list[bytes] = []
        total = 0
        with self.client.stream("GET", url) as response:
            if response.status_code != 200:
                raise GeminiVisionError(
                    f"Poster image download failed (HTTP {response.status_code})"
                )
            declared_size = response.headers.get("content-length")
            if declared_size:
                try:
                    if int(declared_size) > self.max_image_bytes:
                        raise ValueError("Poster image exceeds the configured size limit")
                except ValueError as exc:
                    if "configured size limit" in str(exc):
                        raise
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.max_image_bytes:
                    raise ValueError("Poster image exceeds the configured size limit")
                chunks.append(chunk)
            mime_header = response.headers.get("content-type")
        image_bytes = b"".join(chunks)
        if not image_bytes:
            raise ValueError("Poster image download was empty")
        return image_bytes, self._detect_mime(image_bytes, mime_header)

    @staticmethod
    def _prompt() -> str:
        return (
            "Identify the movie shown by this poster image. Inspect the image itself, "
            "including readable title text, original-language title, credit block, "
            "director, actors, and release-year text. Do not use or infer any local "
            "filename, folder name, catalog label, or information outside this image. "
            "Return at most three plausible movie candidates, ordered by visual evidence. "
            "Only include a candidate when the poster gives strong evidence for its title. "
            "Use the film's first/original release year, not the poster re-release year. "
            "Set year to 0 and director to an empty string when they cannot be read or "
            "identified reliably. Confidence must reflect the evidence visible in this "
            "poster, not how famous or likely the movie seems. If the image is not a "
            "movie poster or the identity is uncertain, return no candidates. "
            "Never invent an IMDb ID, TMDB ID, URL, actor, director, or year. "
            "Keep visible_evidence limited to short details actually visible in the image."
        )

    @staticmethod
    def _extract_output_text(payload: dict[str, Any]) -> str:
        parts: list[str] = []
        for step in payload.get("steps") or []:
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            for part in step.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    value = str(part.get("text") or "").strip()
                    if value:
                        parts.append(value)
        if not parts:
            raise GeminiVisionError("Gemini returned no model text output")
        return "\n".join(parts)

    @staticmethod
    def _normalize_model_result(model_result: dict[str, Any]) -> dict[str, Any]:
        is_movie_poster = model_result.get("is_movie_poster") is True
        candidates: list[dict[str, Any]] = []
        for row in model_result.get("candidates") or []:
            if not isinstance(row, dict):
                continue
            title = " ".join(str(row.get("title") or "").split()).strip()[:180]
            if len(title) < 3:
                continue
            try:
                year = int(row.get("year") or 0)
                confidence = float(row.get("confidence"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(confidence):
                continue
            confidence = max(0.0, min(1.0, confidence))
            director = " ".join(str(row.get("director") or "").split()).strip()[:120]
            visible_evidence = [
                " ".join(str(value or "").split())[:180]
                for value in (row.get("visible_evidence") or [])
                if str(value or "").strip()
            ][:6]
            candidates.append({
                "title": title,
                "year": year if 1870 <= year <= 2100 else None,
                "director": director or None,
                "confidence": round(confidence, 4),
                "visible_evidence": visible_evidence,
            })
            if len(candidates) >= 3:
                break

        poster_text = [
            " ".join(str(value or "").split())[:180]
            for value in (model_result.get("poster_text") or [])
            if str(value or "").strip()
        ][:12]
        eligible = [
            row for row in candidates
            if row.get("year") is not None
            and row["confidence"] >= MIN_CANDIDATE_CONFIDENCE
        ]
        status = (
            "ok" if is_movie_poster and eligible
            else "no_confident_candidate"
        )
        return {
            "provider": "google_gemini_vision",
            "source": "poster_image_pixels",
            "status": status,
            "model": "",
            "is_movie_poster": is_movie_poster,
            "identity_evidence": False,
            "automatic_match_allowed": False,
            "minimum_confidence": MIN_CANDIDATE_CONFIDENCE,
            "candidates": candidates,
            "poster_text": poster_text,
            "notes": " ".join(str(model_result.get("notes") or "").split())[:400],
        }

    def identify_poster(self, image_url: str) -> dict[str, Any]:
        """Download one public poster and ask Gemini for structured visual candidates."""
        image_bytes, mime_type = self._download_poster(image_url)
        request_payload = {
            "model": self.model,
            "store": False,
            "input": [{
                "type": "user_input",
                "content": [
                    {"type": "text", "text": self._prompt()},
                    {
                        "type": "image",
                        "mime_type": mime_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                        "resolution": "medium",
                    },
                ],
            }],
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": _RESPONSE_SCHEMA,
            },
            "generation_config": {"max_output_tokens": 700},
        }
        response = self.client.post(
            GEMINI_INTERACTIONS_URL,
            headers={
                "x-goog-api-key": self.api_key,
                "content-type": "application/json",
            },
            json=request_payload,
        )
        if response.status_code == 429:
            raise GeminiVisionError("Gemini API request limit reached (HTTP 429)")
        if response.status_code >= 400:
            raise GeminiVisionError(
                f"Gemini API request failed (HTTP {response.status_code})"
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise GeminiVisionError("Gemini returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("status") not in {
            "completed",
            None,
        }:
            status = payload.get("status") if isinstance(payload, dict) else "invalid"
            raise GeminiVisionError(f"Gemini interaction did not complete (status={status})")
        model_text = self._extract_output_text(payload)
        try:
            model_result = json.loads(model_text)
        except json.JSONDecodeError as exc:
            raise GeminiVisionError("Gemini output did not match the required JSON format") from exc
        if not isinstance(model_result, dict):
            raise GeminiVisionError("Gemini output was not a JSON object")
        normalized = self._normalize_model_result(model_result)
        normalized["model"] = self.model
        usage = payload.get("usage") or {}
        if isinstance(usage, dict):
            normalized["usage"] = {
                "total_input_tokens": usage.get("total_input_tokens"),
                "total_output_tokens": usage.get("total_output_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        return normalized

    def close(self) -> None:
        self.client.close()
