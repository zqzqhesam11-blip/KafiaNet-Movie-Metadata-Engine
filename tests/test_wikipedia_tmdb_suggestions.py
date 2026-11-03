from __future__ import annotations

import unittest

from app.engine import MetadataEngine


class FakeLens:
    @staticmethod
    def title_hints_from_lens_results(rows):
        return ["A Short Film About Killing"], 1988


class FakeWikipedia:
    def __init__(self, articles):
        self.articles = articles
        self.calls = []

    def search_articles(self, title_hints, **kwargs):
        self.calls.append((list(title_hints), kwargs))
        return {
            "provider": "wikipedia_mediawiki_api",
            "language": "en",
            "status": "ok",
            "query_count": len(title_hints),
            "search_result_count": len(self.articles),
            "articles": self.articles,
            "errors": [],
        }


class FakeTMDB:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def search_movies(self, query, year=None):
        self.calls.append((query, year))
        return {"results": self.results}


class WikipediaTMDBSuggestionTests(unittest.TestCase):
    def make_engine(self, articles, results):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.reverse_image = FakeLens()
        engine.wikipedia = FakeWikipedia(articles)
        engine.tmdb = FakeTMDB(results)
        return engine

    def test_wikipedia_title_and_year_are_searched_in_tmdb_but_never_auto_match(self):
        article = {
            "pageid": 123,
            "title": "A Short Film About Killing",
            "url": "https://en.wikipedia.org/?curid=123",
            "year": 1988,
            "year_candidates": [1988],
            "is_movie_article": True,
            "matched_query": "A Short Film About Killing",
            "snippet": "1988 Polish drama film",
            "extract": "A Short Film About Killing is a 1988 Polish drama film.",
        }
        results = [
            {
                "id": 10754,
                "title": "A Short Film About Killing",
                "original_title": "Krótki film o zabijaniu",
                "release_date": "1988-03-11",
                "vote_average": 7.8,
            },
            {
                "id": 31056,
                "title": "A Short Film About Love",
                "original_title": "Krótki film o miłości",
                "release_date": "1988-02-01",
            },
        ]
        engine = self.make_engine([article], results)

        output = engine._wikipedia_tmdb_suggestions_from_lens([{"title": "A Short Film About Killing"}])

        self.assertEqual(output["status"], "suggestions_only")
        self.assertFalse(output["identity_evidence"])
        self.assertFalse(output["automatic_match_allowed"])
        self.assertEqual(engine.wikipedia.calls[0][0], ["A Short Film About Killing"])
        self.assertEqual(engine.tmdb.calls, [("A Short Film About Killing", 1988)])
        suggestions = output["queries"][0]["results"]
        self.assertTrue(suggestions[0]["exact_title_year_match"])
        self.assertFalse(suggestions[1]["exact_title_year_match"])
        self.assertEqual(output["exact_title_year_match_count"], 1)
        self.assertNotIn("tmdb_id", output)
        self.assertNotIn("identity", output)

    def test_ambiguous_wikipedia_year_does_not_query_tmdb(self):
        article = {
            "pageid": 456,
            "title": "Example Film",
            "url": "https://en.wikipedia.org/?curid=456",
            "year": None,
            "year_candidates": [1988, 2001],
            "is_movie_article": True,
        }
        engine = self.make_engine([article], [])

        output = engine._wikipedia_tmdb_suggestions_from_lens([{"title": "Example Film"}])

        self.assertEqual(output["status"], "no_reliable_wikipedia_year")
        self.assertEqual(engine.tmdb.calls, [])
        self.assertEqual(
            output["queries"][0]["status"],
            "skipped_missing_unambiguous_wikipedia_year",
        )

    def test_lens_failure_does_not_trigger_wikipedia_or_tmdb(self):
        engine = self.make_engine([], [])

        output = engine._wikipedia_tmdb_suggestions_from_lens([{
            "error": "BrightDataAPIError",
            "message": "Lens unavailable",
        }])

        self.assertEqual(output["status"], "skipped_lens_error")
        self.assertEqual(engine.wikipedia.calls, [])
        self.assertEqual(engine.tmdb.calls, [])

    def test_no_lens_title_hints_does_not_query_wikipedia_or_tmdb(self):
        engine = self.make_engine([], [])
        engine.reverse_image.title_hints_from_lens_results = lambda _rows: ([], None)

        output = engine._wikipedia_tmdb_suggestions_from_lens([{"title": "generic result"}])

        self.assertEqual(output["status"], "no_title_hints")
        self.assertEqual(engine.wikipedia.calls, [])
        self.assertEqual(engine.tmdb.calls, [])


if __name__ == "__main__":
    unittest.main()
