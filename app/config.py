from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    tmdb_bearer_token: str
    brightdata_api_key: str
    brightdata_serp_zone: str
    github_token: str
    posters_repo: str
    posters_branch: str
    tmdb_language: str
    batch_limit: int
    http_timeout_seconds: float
    local_ocr_enabled: bool = False
    # Official TinEye REST API key. Kept separate from Bright Data's legacy
    # fields so older configuration readers remain source-compatible.
    tineye_api_key: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            tmdb_bearer_token=os.getenv("TMDB_BEARER_TOKEN", "").strip(),
            brightdata_api_key=os.getenv("BRIGHTDATA_API_KEY", "").strip(),
            brightdata_serp_zone=os.getenv("BRIGHTDATA_SERP_ZONE", "").strip(),
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            posters_repo=os.getenv("POSTERS_REPO", "zqzqhesam11-blip/KafiaNet-Movie-Posters").strip(),
            posters_branch=os.getenv("POSTERS_BRANCH", "main").strip(),
            tmdb_language=os.getenv("TMDB_LANGUAGE", "en-US").strip(),
            batch_limit=max(1, int(os.getenv("BATCH_LIMIT", "5"))),
            http_timeout_seconds=max(5.0, float(os.getenv("HTTP_TIMEOUT_SECONDS", "60"))),
            local_ocr_enabled=os.getenv("LOCAL_OCR_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"},
            tineye_api_key=os.getenv("TINEYE_API_KEY", "").strip(),
        )

    def validate_tineye(self) -> None:
        if not self.tineye_api_key:
            raise RuntimeError(
                "Missing required environment variable: TINEYE_API_KEY. "
                "Configure the official TinEye API key in Hostless; do not paste it into chat."
            )

    def validate_brightdata(self) -> None:
        # Retained only for backwards compatibility with the old diagnostic command.
        missing = []
        if not self.brightdata_api_key:
            missing.append("BRIGHTDATA_API_KEY")
        if not self.brightdata_serp_zone:
            missing.append("BRIGHTDATA_SERP_ZONE")
        if missing:
            raise RuntimeError(
                "Missing Bright Data configuration: " + ", ".join(missing) +
                ". This legacy provider is not used for reverse-image searches."
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
