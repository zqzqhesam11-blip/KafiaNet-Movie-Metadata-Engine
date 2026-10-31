from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any


YEAR_RE = re.compile(r"\s*[\[(（](19\d{2}|20\d{2})[\])）]\s*$")


def split_title_year(folder_name: str) -> tuple[str, int | None]:
    value = folder_name.strip()
    match = YEAR_RE.search(value)
    if not match:
        return value, None
    return value[: match.start()].strip(), int(match.group(1))


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = value.replace("&", " and ")
    value = "".join(ch if ch.isalnum() else " " for ch in value)
    return " ".join(value.split())


def title_similarity(left: str, right: str) -> float:
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def score_candidate(
    source_title: str,
    source_year: int | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    names = [
        str(candidate.get("title") or ""),
        str(candidate.get("original_title") or ""),
    ]
    similarity = max((title_similarity(source_title, name) for name in names), default=0.0)

    date = str(candidate.get("release_date") or "")
    candidate_year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
    year_delta = (
        abs(source_year - candidate_year)
        if source_year is not None and candidate_year is not None
        else None
    )

    # A year disagreement is a review signal, not permission to overwrite the year.
    score = similarity
    if source_year is not None and candidate_year is not None:
        score = similarity * 0.90 + (0.10 if year_delta == 0 else 0.0)
        if year_delta is not None and year_delta > 1:
            score *= 0.82

    return {
        "confidence": round(score, 4),
        "title_similarity": round(similarity, 4),
        "source_year": source_year,
        "tmdb_year": candidate_year,
        "year_delta": year_delta,
    }


def classify_match(score: dict[str, Any], threshold: float) -> str:
    if score["confidence"] < threshold:
        return "review"
    if score["year_delta"] is not None and score["year_delta"] > 1:
        return "review"
    return "ready"
