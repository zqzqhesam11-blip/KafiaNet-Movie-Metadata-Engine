from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any


YEAR_RE = re.compile(r"\s*[\[(（](19\d{2}|20\d{2})[\])）]\s*$")
TITLE_ALIAS_RE = re.compile(r"[\[(（]([^()\[\]（）]+)[\])）]")


def split_title_year(folder_name: str) -> tuple[str, int | None]:
    value = folder_name.strip()
    match = YEAR_RE.search(value)
    if not match:
        return value, None
    return value[: match.start()].strip(), int(match.group(1))


def title_search_variants(title: str) -> list[str]:
    """Return the original title, its unbracketed form, and parenthetical aliases.

    Folder names are source identifiers and are never rewritten. This helper
    only broadens TMDB search so names such as "Breathless (À Bout de Souffle)"
    can be found by either public title.
    """
    value = str(title or "").strip()
    if not value:
        return []

    variants = [value]
    aliases = [
        match.group(1).strip()
        for match in TITLE_ALIAS_RE.finditer(value)
        if match.group(1).strip()
        and not re.fullmatch(r"(?:19|20)\d{2}", match.group(1).strip())
    ]
    base = TITLE_ALIAS_RE.sub(" ", value)
    base = " ".join(base.split()).strip()
    if base:
        variants.append(base)
    variants.extend(aliases)

    result: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        key = normalize_title(variant)
        if key and key not in seen:
            seen.add(key)
            result.append(variant)
    return result


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
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
