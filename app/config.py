from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    tmdb_bearer_token: str
    github_token: str
    posters_repo: str
    posters_branch: str
    tmdb_language: str
    searxng_url: str
    searxng_token: str
    batch_limit: int
    match_accept_score: float
    http_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            tmdb_bearer_token=os.getenv("TMDB_BEARER_TOKEN", "").strip(),
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            posters_repo=os.getenv(
                "POSTERS_REPO",
                "zqzqhesam11-blip/KafiaNet-Movie-Posters",
            ).strip(),
            posters_branch=os.getenv("POSTERS_BRANCH", "main").strip(),
            tmdb_language=os.getenv("TMDB_LANGUAGE", "en-US").strip(),
            searxng_url=os.getenv("SEARXNG_URL", "").strip().rstrip("/"),
            searxng_token=os.getenv("SEARXNG_TOKEN", "").strip(),
            batch_limit=max(1, int(os.getenv("BATCH_LIMIT", "5"))),
            match_accept_score=min(
                1.0, max(0.0, float(os.getenv("MATCH_ACCEPT_SCORE", "0.94")))
            ),
            http_timeout_seconds=max(
                5.0, float(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
            ),
        )

    def validate_for_processing(self, apply: bool) -> None:
        missing = []
        if not self.tmdb_bearer_token:
            missing.append("TMDB_BEARER_TOKEN")
        if apply and not self.github_token:
            missing.append("GITHUB_TOKEN")
        if missing:
            raise RuntimeError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )
