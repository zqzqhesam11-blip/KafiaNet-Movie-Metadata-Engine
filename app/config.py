from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    tmdb_bearer_token: str
    tineye_api_key: str
    github_token: str
    posters_repo: str
    posters_branch: str
    tmdb_language: str
    batch_limit: int
    match_accept_score: float
    http_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            tmdb_bearer_token=os.getenv("TMDB_BEARER_TOKEN", "").strip(),
            tineye_api_key=os.getenv("TINEYE_API_KEY", "").strip(),
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            posters_repo=os.getenv("POSTERS_REPO", "zqzqhesam11-blip/KafiaNet-Movie-Posters").strip(),
            posters_branch=os.getenv("POSTERS_BRANCH", "main").strip(),
            tmdb_language=os.getenv("TMDB_LANGUAGE", "en-US").strip(),
            batch_limit=max(1, int(os.getenv("BATCH_LIMIT", "5"))),
            match_accept_score=min(1.0, max(0.0, float(os.getenv("MATCH_ACCEPT_SCORE", "0.94")))),
            http_timeout_seconds=max(5.0, float(os.getenv("HTTP_TIMEOUT_SECONDS", "60"))),
        )

    def validate_tineye(self) -> None:
        if not self.tineye_api_key:
            raise RuntimeError(
                "Missing required environment variable: TINEYE_API_KEY. "
                "Configure the official TinEye API key in Hostless; do not paste it into chat."
            )

    def validate_for_processing(self, apply: bool) -> None:
        missing = []
        if not self.tmdb_bearer_token:
            missing.append("TMDB_BEARER_TOKEN")
        if not self.tineye_api_key:
            missing.append("TINEYE_API_KEY")
        if apply and not self.github_token:
            missing.append("GITHUB_TOKEN")
        if missing:
            raise RuntimeError("Missing required environment variable(s): " + ", ".join(missing))
