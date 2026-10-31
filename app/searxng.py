from __future__ import annotations

from typing import Any
import httpx


class SearXNGClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 45):
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("SEARXNG_URL must be an absolute HTTP(S) URL")
        if not token.strip():
            raise RuntimeError("SEARXNG_TOKEN is required for the protected SearXNG service")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token.strip()}",
        }
        self.client = httpx.Client(headers=headers, timeout=timeout)

    def tineye_by_image_url(self, image_url: str) -> list[dict[str, Any]]:
        # SearXNG online_url_search engines receive the URL as the search query.
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
        results = data.get("results") or []
        return [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "source": item.get("source"),
                "content": item.get("content"),
            }
            for item in results
        ]

    def close(self) -> None:
        self.client.close()
