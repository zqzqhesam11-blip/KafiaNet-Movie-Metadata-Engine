from __future__ import annotations

from typing import Any
import httpx


class SearXNGClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 45):
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith("https://"):
            raise ValueError("SEARXNG_URL must be an absolute HTTPS URL")
        if not token.strip():
            raise RuntimeError("SEARXNG_TOKEN is required for the protected SearXNG service")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token.strip()}",
        }
        self.client = httpx.Client(headers=headers, timeout=timeout)

    @staticmethod
    def _tineye_failure(data: dict[str, Any]) -> str | None:
        """Read SearXNG's per-engine failure metadata instead of hiding it."""
        if data.get("error"):
            return str(data["error"])[:300]

        for entry in data.get("unresponsive_engines") or []:
            name = ""
            reason = "engine unresponsive"
            if isinstance(entry, dict):
                name = str(entry.get("name") or entry.get("engine") or "")
                reason = str(entry.get("reason") or entry.get("error") or reason)
            elif isinstance(entry, (list, tuple)):
                if entry:
                    name = str(entry[0] or "")
                if len(entry) > 1 and entry[1]:
                    reason = str(entry[1])
            else:
                name = str(entry or "")

            if name.strip().casefold() == "tineye":
                return f"TinEye engine unresponsive: {reason}"[:300]
        return None

    def tineye_by_image_url(self, image_url: str) -> list[dict[str, Any]]:
        # SearXNG's online_url_search processor interprets the image URL query.
        response = self.client.get(
            f"{self.base_url}/search",
            params={
                "q": image_url,
                "engines": "tineye",
                "format": "json",
            },
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("SearXNG returned an invalid JSON response")

        failure = self._tineye_failure(data)
        if failure:
            raise RuntimeError(failure)

        results = data.get("results") or []
        if not isinstance(results, list):
            raise RuntimeError("SearXNG response field 'results' is not a list")

        return [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "source": item.get("source"),
                "content": item.get("content"),
            }
            for item in results
            if isinstance(item, dict)
        ]

    def close(self) -> None:
        self.client.close()
