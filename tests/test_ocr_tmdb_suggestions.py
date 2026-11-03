from __future__ import annotations

import unittest

from app.engine import MetadataEngine


class FakeTMDB:
    def __init__(self):
        self.queries = []

    def search_movies(self, query):
        self.queries.append(query)
        return {
            "results": [
                {
                    "id": 10754,
                    "title": "The Example Movie",
                    "original_title": "The Example Movie",
                    "release_date": "2001-04-12",
                    "vote_average": 7.1,
                    "poster_path": "/example.jpg",
                },
                {
                    "id": 22,
                    "title": "Another Candidate",
                    "original_title": "Another Candidate",
                    "release_date": "1998-01-01",
                },
            ]
        }


class OCRTMDBSuggestionTests(unittest.TestCase):
    def make_engine(self):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.tmdb = FakeTMDB()
        return engine

    def test_ocr_search_results_are_explicitly_suggestions_only(self):
        engine = self.make_engine()
        result = engine._tmdb_suggestions_from_ocr({
            "status": "ok",
            "title_hints": [
                {"text": "The Example Movie", "confidence": 0.97},
                {"text": "Another OCR Line", "confidence": 0.84},
            ],
        })

        self.assertEqual(result["status"], "suggestions_only")
        self.assertFalse(result["identity_evidence"])
        self.assertFalse(result["automatic_match_allowed"])
        self.assertEqual(result["query_count"], 2)
        self.assertEqual(engine.tmdb.queries, ["The Example Movie", "Another OCR Line"])
        self.assertEqual(result["queries"][0]["results"][0]["tmdb_id"], 10754)
        self.assertNotIn("identity", result)
        self.assertNotIn("match", result)

    def test_ocr_query_count_is_bounded_and_duplicate_hints_are_removed(self):
        engine = self.make_engine()
        result = engine._tmdb_suggestions_from_ocr({
            "status": "ok",
            "title_hints": [
                {"text": "The Example Movie"},
                {"text": "the example movie"},
                {"text": "Second Candidate"},
                {"text": "Third Candidate"},
                {"text": "Fourth Candidate"},
            ],
        })

        self.assertEqual(result["query_count"], 3)
        self.assertEqual(
            engine.tmdb.queries,
            ["The Example Movie", "Second Candidate", "Third Candidate"],
        )

    def test_non_ok_ocr_does_not_trigger_tmdb_search(self):
        engine = self.make_engine()
        result = engine._tmdb_suggestions_from_ocr({
            "status": "error",
            "title_hints": [{"text": "Ignored"}],
        })

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["query_count"], 0)
        self.assertEqual(engine.tmdb.queries, [])


if __name__ == "__main__":
    unittest.main()
