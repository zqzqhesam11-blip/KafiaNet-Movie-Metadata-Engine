from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlparse

import httpx


MAX_POSTER_BYTES = 12 * 1024 * 1024
MIN_TEXT_CONFIDENCE = 0.55
MAX_TEXT_LINES = 30
MAX_TITLE_HINTS = 5


class LocalPosterOCR:
    """Run poster OCR in-process with RapidOCR + ONNX Runtime CPU."""

    def __init__(self, timeout: float = 30.0, max_image_bytes: int = MAX_POSTER_BYTES):
        self.max_image_bytes = max(1024, int(max_image_bytes))
        self.client = httpx.Client(timeout=max(5.0, float(timeout)))
        self._engine: Any | None = None

    @staticmethod
    def validate_image_url(image_url: str) -> str:
        """Accept only the public raw GitHub image host used by GitHubAPI.raw_url."""
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
        ):
            raise ValueError("Local OCR accepts only HTTPS raw.githubusercontent.com poster URLs")
        return value

    def _download_image(self, image_url: str) -> bytes:
        url = self.validate_image_url(image_url)
        chunks: list[bytes] = []
        total = 0
        with self.client.stream("GET", url) as response:
            response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].lower()
            if content_type and not content_type.startswith("image/"):
                raise ValueError(f"Poster URL returned a non-image content type: {content_type}")
            declared_size = response.headers.get("content-length")
            if declared_size:
                try:
                    if int(declared_size) > self.max_image_bytes:
                        raise ValueError("Poster image exceeds the configured download size limit")
                except ValueError as exc:
                    if "configured download size limit" in str(exc):
                        raise
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.max_image_bytes:
                    raise ValueError("Poster image exceeds the configured download size limit")
                chunks.append(chunk)
        image_bytes = b"".join(chunks)
        if not image_bytes:
            raise ValueError("Poster image download was empty")
        return image_bytes

    def _get_engine(self):
        if self._engine is None:
            # The pinned RapidOCR wheel includes its small ONNX model files.
            # ONNX Runtime is installed separately and explicitly in requirements.txt.
            from rapidocr import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    @staticmethod
    def _normalize_result(
        result: Any,
        image_height: int = 0,
        min_confidence: float = MIN_TEXT_CONFIDENCE,
    ) -> dict[str, Any]:
        raw_texts = getattr(result, "txts", None)
        raw_scores = getattr(result, "scores", None)
        raw_boxes = getattr(result, "boxes", None)
        texts = list(raw_texts) if raw_texts is not None else []
        scores = list(raw_scores) if raw_scores is not None else []
        boxes = list(raw_boxes) if raw_boxes is not None else []
        lines: list[dict[str, Any]] = []

        for index, raw_text in enumerate(texts):
            text = " ".join(str(raw_text or "").split())
            if len(text) < 2 or sum(1 for char in text if char.isalpha()) < 2:
                continue
            try:
                confidence = float(scores[index]) if index < len(scores) else 0.0
            except (TypeError, ValueError):
                continue
            if not math.isfinite(confidence) or confidence < min_confidence:
                continue

            box_value: Any = boxes[index] if index < len(boxes) else None
            if hasattr(box_value, "tolist"):
                box_value = box_value.tolist()
            box: list[list[float]] = []
            try:
                for point in list(box_value or [])[:4]:
                    if hasattr(point, "tolist"):
                        point = point.tolist()
                    coordinates = list(point)
                    if len(coordinates) >= 2:
                        box.append([float(coordinates[0]), float(coordinates[1])])
            except (TypeError, ValueError):
                box = []

            line_height = 0.0
            if box:
                ys = [point[1] for point in box]
                line_height = max(ys) - min(ys)

            lines.append({
                "text": text,
                "confidence": round(max(0.0, min(1.0, confidence)), 4),
                "box": box,
                "_line_height": line_height,
                "_index": index,
            })

        # Keep the full OCR reading order for diagnostics.
        lines.sort(key=lambda row: int(row["_index"]))
        public_lines = [
            {
                "text": row["text"],
                "confidence": row["confidence"],
                "box": row["box"],
            }
            for row in lines[:MAX_TEXT_LINES]
        ]

        # Larger, high-confidence text is more useful as a search suggestion.
        ranked = sorted(
            lines,
            key=lambda row: (
                -(
                    float(row["confidence"]) * 0.8
                    + min(
                        float(row["_line_height"]) / max(float(image_height), 1.0) * 2.0,
                        1.0,
                    ) * 0.2
                ),
                int(row["_index"]),
            ),
        )
        hints: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in ranked:
            key = str(row["text"]).casefold()
            if key in seen:
                continue
            seen.add(key)
            hints.append({
                "text": row["text"],
                "confidence": row["confidence"],
                "box": row["box"],
            })
            if len(hints) >= MAX_TITLE_HINTS:
                break

        return {
            "status": "ok" if public_lines else "no_text",
            "text_lines": public_lines,
            "title_hints": hints,
        }

    def extract_from_url(self, image_url: str) -> dict[str, Any]:
        """Download the poster bytes and OCR the image itself; never inspect its path/name."""
        base = {
            "provider": "rapidocr_onnxruntime_cpu",
            "source": "poster_image_pixels",
            "identity_evidence": False,
            "status": "error",
            "text_lines": [],
            "title_hints": [],
        }
        try:
            image_bytes = self._download_image(image_url)
            import cv2
            import numpy as np

            image = cv2.imdecode(
                np.frombuffer(image_bytes, dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            if image is None:
                raise ValueError("Downloaded bytes are not a supported image")
            result = self._get_engine()(image)
            normalized = self._normalize_result(
                result,
                image_height=int(image.shape[0]),
            )
            base.update(normalized)
            return base
        except Exception as exc:
            base["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
            return base

    def close(self) -> None:
        self.client.close()
