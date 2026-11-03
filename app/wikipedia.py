from __future__ import annotations

import re
from typing import Any

import httpx


class WikipediaClient:
    """Small, bounded MediaWiki search client used only for reviewer suggestions."""

    def __init__(self, language: str = "en", timeout: float = 20):
        language_code = re.sub(r"[^a-z-]", "", str(language or "en").casefold())[:12] or "en"
        self.language = language_code
        self.api_url = f"https://{language_code}.wikipedia.org/w/api.php"
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "KafiaNet-Movie-Metadata-Engine/1.0 (Wikipedia metadata lookup)",
            },
        )

    @staticmethod
    def _normalize_hints(title_hints: list[str], limit: int) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in title_hints or []:
            title = " ".join(str(value or "").split()).strip()
            key = re.sub(r"\W+", " ", title.casefold()).strip()
            if len(title) < 2 or not key or key in seen:
                continue
            seen.add(key)
            output.append(title[:180])
            if len(output) >= max(1, limit):
                break
        return output

    @staticmethod
    def _page_rows(payload: Any) -> list[dict[str, Any]]:
        pages = ((payload or {}).get("query") or {}).get("pages") or []
        if isinstance(pages, dict):
            pages = list(pages.values())
        return [row for row in pages if isinstance(row, dict)]

    @staticmethod
    def _article_year(title: str, extract: str) -> tuple[int | None, list[int]]:
        # Prefer the article's lead sentence; later paragraphs can mention
        # unrelated dates, restorations, awards, or re-releases.
        lead = re.split(r"(?<=[.!?])\s+", str(extract or "").strip(), maxsplit=1)[0]
        years = list(dict.fromkeys(
            int(value) for value in re.findall(r"\b(?:18|19|20)\d{2}\b", lead)
        ))
        if not years:
            disambiguator = re.search(
                r"\b((?:18|19|20)\d{2})\s+film\b",
                str(title or ""),
                flags=re.I,
            )
            if disambiguator:
                years = [int(disambiguator.group(1))]
        return (years[0] if len(years) == 1 else None), years

    def search_articles(
        self,
        title_hints: list[str],
        *,
        max_queries: int = 5,
        results_per_query: int = 3,
        max_articles: int = 10,
    ) -> dict[str, Any]:
        queries = self._normalize_hints(title_hints, max_queries)
        output: dict[str, Any] = {
            "provider": "wikipedia_mediawiki_api",
            "language": self.language,
            "status": "no_title_hints" if not queries else "searching",
            "query_count": len(queries),
            "queries": queries,
            "search_result_count": 0,
            "article_count": 0,
            "articles": [],
            "errors": [],
        }
        if not queries:
            return output

        found: dict[int, dict[str, Any]] = {}
        successful_queries = 0
        for query in queries:
            try:
                response = self.client.get(
                    self.api_url,
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": query,
                        "srnamespace": 0,
                        "srlimit": max(1, min(int(results_per_query), 5)),
                        "format": "json",
                        "formatversion": 2,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                rows = ((payload or {}).get("query") or {}).get("search") or []
                if not isinstance(rows, list):
                    raise ValueError("Wikipedia returned an invalid search result list")
                successful_queries += 1
                output["search_result_count"] += len(rows)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    raw_page_id = row.get("pageid")
                    if not str(raw_page_id or "").isdigit():
                        continue
                    page_id = int(raw_page_id)
                    found.setdefault(page_id, {
                        "pageid": page_id,
                        "title": str(row.get("title") or "").strip()[:250],
                        "snippet": re.sub(r"<[^>]+>", " ", str(row.get("snippet") or ""))[:500],
                        "matched_query": query,
                    })
                    if len(found) >= max(1, max_articles):
                        break
                if len(found) >= max(1, max_articles):
                    break
            except Exception as exc:
                output["errors"].append({
                    "query": query,
                    "type": type(exc).__name__,
                    "message": str(exc).replace("\n", " ")[:220],
                })

        if not found:
            output["status"] = (
                "error" if queries and successful_queries == 0
                else "no_articles"
            )
            return output

        page_ids = list(found)[:max(1, max_articles)]
        try:
            response = self.client.get(
                self.api_url,
                params={
                    "action": "query",
                    "pageids": "|".join(str(value) for value in page_ids),
                    "prop": "extracts|categories",
                    "exintro": 1,
                    "explaintext": 1,
                    "exchars": 1200,
                    "cllimit": 20,
                    "clshow": "!hidden",
                    "format": "json",
                    "formatversion": 2,
                },
            )
            response.raise_for_status()
            payload = response.json()
            page_rows = self._page_rows(payload)
            pages_by_id = {
                int(row["pageid"]): row
                for row in page_rows
                if str(row.get("pageid") or "").isdigit()
            }
        except Exception as exc:
            output["errors"].append({
                "stage": "fetch_article_intros",
                "type": type(exc).__name__,
                "message": str(exc).replace("\n", " ")[:220],
            })
            output["status"] = "error"
            return output

        articles: list[dict[str, Any]] = []
        for page_id in page_ids:
            search_row = found[page_id]
            page = pages_by_id.get(page_id) or {}
            title = str(page.get("title") or search_row.get("title") or "").strip()
            extract = " ".join(str(page.get("extract") or "").split())
            categories = [
                str(item.get("title") or "")
                for item in (page.get("categories") or [])
                if isinstance(item, dict) and item.get("title")
            ]
            lead = extract[:800]
            is_movie_article = bool(
                re.search(r"\b(?:film|motion picture)\b", lead, flags=re.I)
                or any("film" in category.casefold() for category in categories)
            )
            year, year_candidates = self._article_year(title, extract)
            articles.append({
                "pageid": page_id,
                "title": title[:250],
                "url": f"https://{self.language}.wikipedia.org/?curid={page_id}",
                "snippet": search_row.get("snippet"),
                "extract": extract[:1200],
                "categories": categories[:20],
                "is_movie_article": is_movie_article,
                "year": year,
                "year_candidates": year_candidates,
                "matched_query": search_row.get("matched_query"),
            })

        output["articles"] = articles
        output["article_count"] = len(articles)
        output["status"] = "ok" if articles else "no_articles"
        return output

    def close(self) -> None:
        self.client.close()
