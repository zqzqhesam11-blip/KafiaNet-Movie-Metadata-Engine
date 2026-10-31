from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.engine import MetadataEngine


MATRIX = {
    "id": 603,
    "title": "The Matrix",
    "original_title": "The Matrix",
    "release_date": "1999-03-31",
}


class FakeTMDB:
    def __init__(self, folder_results=None, imdb_results=None):
        self.folder_results = folder_results or {}
        self.imdb_results = imdb_results or {}
        self.search_calls = []

    def search_movies(self, title, year=None):
        self.search_calls.append((title, year))
        return self.folder_results.get(title, [])

    def find_by_imdb_id(self, imdb_id):
        return self.imdb_results.get(imdb_id, [])

    def movie_details(self, tmdb_id):
        return dict(MATRIX)


class TinEyeTMDBMatchingTests(unittest.TestCase):
    def make_engine(self, tmdb):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.settings = SimpleNamespace(match_accept_score=0.94)
        engine.tmdb = tmdb
        engine._details_cache = {}
        return engine

    def test_tineye_image_filename_can_recover_wrong_folder_name(self):
        tmdb = FakeTMDB(folder_results={"The Matrix": [dict(MATRIX)]}, imdb_results={})
        engine = self.make_engine(tmdb)
        candidate, match, evidence = engine._find_movie(
            "bad folder label",
            None,
            [
                {
                    "title": "The.Matrix.1999.1080p.POSTER.jpg",
                    "url": "https://example.com/movies",
                    "source": "https://cdn.example.com/posters/matrix-cover.jpg",
                }
            ],
        )
        self.assertEqual(candidate["id"], 603)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["best_tineye_hint"], "The Matrix")
        self.assertIn(("The Matrix", None), tmdb.search_calls)
        self.assertIn("title_hints", evidence)

    def test_trusted_imdb_backlink_resolves_tmdb_identity(self):
        tmdb = FakeTMDB(imdb_results={"tt0133093": [dict(MATRIX)]})
        engine = self.make_engine(tmdb)
        candidate, match, evidence = engine._find_movie(
            "abbreviated unknown folder",
            None,
            [
                {
                    "title": "poster.jpg",
                    "url": "https://www.imdb.com/title/tt0133093/",
                    "source": "https://cdn.example.com/poster.jpg",
                }
            ],
        )
        self.assertEqual(candidate["id"], 603)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["confidence"], 1.0)
        self.assertEqual(evidence["imdb_ids"], ["tt0133093"])

    def test_high_confidence_folder_conflict_routes_to_review(self):
        gladiator = {
            "id": 98,
            "title": "Gladiator",
            "original_title": "Gladiator",
            "release_date": "2000-05-01",
        }
        tmdb = FakeTMDB(
            folder_results={"Gladiator": [gladiator]},
            imdb_results={"tt0133093": [dict(MATRIX)]},
        )
        engine = self.make_engine(tmdb)
        candidate, match, _ = engine._find_movie(
            "Gladiator",
            2000,
            [
                {
                    "title": "poster.jpg",
                    "url": "https://www.imdb.com/title/tt0133093/",
                    "source": "https://cdn.example.com/poster.jpg",
                }
            ],
        )
        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertIn("conflicts", match["reason"])

    def test_multiple_direct_movie_ids_route_to_review(self):
        second = {
            "id": 155,
            "title": "The Dark Knight",
            "original_title": "The Dark Knight",
            "release_date": "2008-07-18",
        }
        tmdb = FakeTMDB(
            imdb_results={
                "tt0133093": [dict(MATRIX)],
                "tt0468569": [second],
            }
        )
        engine = self.make_engine(tmdb)
        candidate, match, _ = engine._find_movie(
            "unknown",
            None,
            [
                {"title": "first.jpg", "url": "https://www.imdb.com/title/tt0133093/"},
                {"title": "second.jpg", "url": "https://www.imdb.com/title/tt0468569/"},
            ],
        )
        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(set(match["candidate_ids"]), {"603", "155"})

    def test_failed_reverse_image_search_forces_review(self):
        tmdb = FakeTMDB(folder_results={"The Matrix": [dict(MATRIX)]})
        engine = self.make_engine(tmdb)
        candidate, match, _ = engine._find_movie(
            "The Matrix",
            1999,
            [{"error": "Timeout", "message": "provider unavailable"}],
        )
        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertIn("TinEye reverse-image request failed", match["reason"])

    def test_no_tineye_matches_keeps_title_only_candidate_in_review(self):
        tmdb = FakeTMDB(folder_results={"The Matrix": [dict(MATRIX)]})
        engine = self.make_engine(tmdb)
        candidate, match, _ = engine._find_movie("The Matrix", 1999, [])
        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertIn("no reverse-image matches", match["reason"])

    def test_no_tmdb_match_is_review(self):
        engine = self.make_engine(FakeTMDB())
        candidate, match, _ = engine._find_movie("not a film", None, [])
        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")


if __name__ == "__main__":
    unittest.main()
