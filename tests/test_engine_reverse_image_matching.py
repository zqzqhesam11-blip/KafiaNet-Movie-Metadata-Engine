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
    def __init__(self, folder_results=None, imdb_results=None, details_by_id=None):
        self.folder_results = folder_results or {}
        self.imdb_results = imdb_results or {}
        self.details_by_id = details_by_id or {}
        self.search_calls = []

    def search_movies(self, title, year=None):
        self.search_calls.append((title, year))
        return self.folder_results.get(title, [])

    def find_by_imdb_id(self, imdb_id):
        return self.imdb_results.get(imdb_id, [])

    def movie_details(self, tmdb_id):
        return dict(self.details_by_id.get(tmdb_id, MATRIX))


class GoogleLensTMDBMatchingTests(unittest.TestCase):
    def make_engine(self, tmdb):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.settings = SimpleNamespace(match_accept_score=0.94)
        engine.tmdb = tmdb
        engine._details_cache = {}
        return engine

    def test_reverse_image_image_filename_can_recover_wrong_folder_name(self):
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
        self.assertEqual(match["best_reverse_image_hint"], "The Matrix")
        self.assertIn(("The Matrix", None), tmdb.search_calls)
        self.assertIn("title_hints", evidence)

    def test_trusted_imdb_id_without_any_matching_title_stays_in_review(self):
        tmdb = FakeTMDB(imdb_results={"tt0133093": [dict(MATRIX)]})
        engine = self.make_engine(tmdb)
        candidate, match, evidence = engine._find_movie(
            "abbreviated unknown folder",
            None,
            [{
                "title": "poster.jpg",
                "url": "https://www.imdb.com/title/tt0133093/",
                "source": "https://cdn.example.com/poster.jpg",
            }],
        )
        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertIn("mandatory title match", match["reason"])
        self.assertEqual(evidence["imdb_ids"], ["tt0133093"])
        self.assertEqual(evidence["rejected_external_ids"][0]["candidate_id"], "603")

    def test_trusted_imdb_backlink_requires_and_accepts_matching_poster_title(self):
        tmdb = FakeTMDB(imdb_results={"tt0133093": [dict(MATRIX)]})
        engine = self.make_engine(tmdb)
        candidate, match, evidence = engine._find_movie(
            "abbreviated unknown folder",
            None,
            [{
                "title": "The.Matrix.1999.poster.jpg",
                "url": "https://www.imdb.com/title/tt0133093/",
                "source": "https://cdn.example.com/poster.jpg",
            }],
        )
        self.assertEqual(candidate["id"], 603)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["confidence"], 1.0)
        self.assertEqual(match["title_match_source"], "reverse_image_poster_title")
        self.assertEqual(match["matched_source_title"], "The Matrix")
        self.assertEqual(evidence["imdb_ids"], ["tt0133093"])

    def test_folder_candidate_without_title_match_is_not_auto_accepted_even_without_collision(self):
        tmdb = FakeTMDB(folder_results={"Unknown Folder": [{
            "id": 777,
            "title": "Completely Different Movie",
            "original_title": "Completely Different Movie",
            "release_date": "2020-01-01",
        }]})
        engine = self.make_engine(tmdb)
        candidate, match, _ = engine._find_movie(
            "Unknown Folder",
            2020,
            [{"title": "poster.jpg", "url": "https://example.com/gallery"}],
        )
        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertIn("mandatory title match", match["reason"])

    def test_unrelated_reverse_image_id_is_rejected_when_folder_title_matches(self):
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
        candidate, match, evidence = engine._find_movie(
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
        self.assertEqual(candidate["id"], 98)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["best_folder_title_variant"], "Gladiator")
        self.assertEqual(evidence["rejected_external_ids"][0]["candidate_id"], "603")

    def test_wrong_year_and_unrelated_imdb_backlink_do_not_override_folder_match(self):
        bulado = {
            "id": 737756,
            "title": "Buladó",
            "original_title": "Buladó",
            "release_date": "2020-09-24",
        }
        unrelated = {
            "id": 396810,
            "title": "The Last Word",
            "original_title": "The Last Word",
            "release_date": "2016-09-09",
        }
        tmdb = FakeTMDB(
            folder_results={"Buladó": [bulado]},
            imdb_results={"tt5023260": [unrelated]},
        )
        engine = self.make_engine(tmdb)
        candidate, match, evidence = engine._find_movie(
            "Buladó",
            2020,
            [
                {
                    "title": "Bulado poster 2020.jpg",
                    "url": "https://www.imdb.com/title/tt5023260/",
                }
            ],
        )
        self.assertEqual(candidate["id"], 737756)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(evidence["rejected_external_ids"][0]["candidate_id"], "396810")

    def test_wrong_folder_year_is_corrected_from_exact_title_and_imdb_id(self):
        corrected = {
            "id": 91487,
            "title": "State of Siege",
            "original_title": "État de siège",
            "release_date": "1972-05-30",
        }
        tmdb = FakeTMDB(
            folder_results={"State of Siege": [corrected]},
            imdb_results={"tt0070959": [corrected]},
        )
        engine = self.make_engine(tmdb)
        candidate, match, evidence = engine._find_movie(
            "State of Siege",
            1982,
            [
                {
                    "title": "State.of.Siege.1972.poster.jpg",
                    "url": "https://www.imdb.com/title/tt0070959/",
                }
            ],
        )
        self.assertEqual(candidate["id"], 91487)
        self.assertEqual(match["status"], "ready")
        self.assertTrue(match["source_year_corrected"])
        self.assertEqual(match["tmdb_year"], 1972)
        self.assertIn(("State of Siege", None), tmdb.search_calls)
        self.assertEqual(evidence["rejected_external_ids"], [])

    def test_wrong_folder_year_can_be_corrected_by_exact_reverse_image_title_hint(self):
        corrected = {
            "id": 91487,
            "title": "State of Siege",
            "original_title": "État de siège",
            "release_date": "1972-05-30",
        }
        tmdb = FakeTMDB(folder_results={"State of Siege": [corrected]})
        engine = self.make_engine(tmdb)
        candidate, match, _ = engine._find_movie(
            "State of Siege",
            1982,
            [
                {
                    "title": "State.of.Siege.1972.poster.jpg",
                    "url": "https://example.com/poster",
                }
            ],
        )
        self.assertEqual(candidate["id"], 91487)
        self.assertEqual(match["status"], "ready")
        self.assertTrue(match["source_year_corrected"])
        self.assertEqual(match["tmdb_year"], 1972)

    def test_parenthetical_alias_finds_alternative_original_title(self):
        breathless = {
            "id": 269,
            "title": "Breathless",
            "original_title": "À bout de souffle",
            "release_date": "1960-03-16",
        }
        tmdb = FakeTMDB(folder_results={"À Bout de Souffle": [breathless]})
        engine = self.make_engine(tmdb)
        candidate, match, _ = engine._find_movie(
            "Breathless (À Bout de Souffle)",
            1960,
            [{"title": "Breathless.1960.poster.jpg", "url": "https://example.com/poster"}],
        )
        self.assertEqual(candidate["id"], 269)
        self.assertEqual(match["status"], "ready")
        self.assertIn(("À Bout de Souffle", 1960), tmdb.search_calls)

    def test_trusted_imdb_link_resolves_same_title_year_collision_using_country_and_director(self):
        us_film = {
            "id": 136476,
            "title": "State of Emergency",
            "original_title": "State of Emergency",
            "release_date": "2012-09-17",
        }
        russian_film = {
            "id": 520975,
            "title": "State of Emergency",
            "original_title": "Аварийное состояние",
            "release_date": "2012-06-01",
        }
        details = {
            136476: {
                "id": 136476,
                "title": "State of Emergency",
                "original_title": "State of Emergency",
                "release_date": "2012-09-17",
                "runtime": 90,
                "production_countries": [{"iso_3166_1": "US", "name": "United States of America"}],
                "credits": {"crew": [{"job": "Director", "name": "Turner Clay"}], "cast": []},
            },
            520975: {
                "id": 520975,
                "title": "State of Emergency",
                "original_title": "Аварийное состояние",
                "release_date": "2012-06-01",
                "runtime": 100,
                "production_countries": [{"iso_3166_1": "RU", "name": "Russia"}],
                "credits": {"crew": [{"job": "Director", "name": "Vsevolod Benigsen"}], "cast": []},
            },
        }
        tmdb = FakeTMDB(
            folder_results={"State of Emergency": [us_film, russian_film]},
            imdb_results={"tt1705134": [us_film]},
            details_by_id=details,
        )
        engine = self.make_engine(tmdb)

        candidate, match, evidence = engine._find_movie(
            "State of Emergency",
            2012,
            [{
                "title": "State-of-Emergency-2010.jpg",
                "url": "https://www.imdb.com/title/tt1705134/",
                "source": "https://cdn.example.com/state-of-emergency.jpg",
                "content": "",
            }],
        )

        self.assertEqual(candidate["id"], 136476)
        self.assertEqual(match["status"], "ready")
        self.assertTrue(match["same_title_year_collision_resolved"])
        self.assertIn("same-title/year collision", match["reason"])
        self.assertEqual(
            evidence["selected_identity_profile"]["production_countries"],
            ["United States of America"],
        )
        self.assertEqual(
            evidence["selected_identity_profile"]["directors"],
            ["Turner Clay"],
        )
        self.assertEqual(evidence["same_title_year_candidates"][0]["candidate_id"], "520975")
        self.assertEqual(
            evidence["same_title_year_candidates"][0]["directors"],
            ["Vsevolod Benigsen"],
        )
        self.assertEqual(
            evidence["same_title_year_candidates"][0]["production_countries"],
            ["Russia"],
        )

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
                {"title": "The.Matrix.1999.poster.jpg", "url": "https://www.imdb.com/title/tt0133093/"},
                {"title": "The.Dark.Knight.2008.poster.jpg", "url": "https://www.imdb.com/title/tt0468569/"},
            ],
        )
        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(set(match["candidate_ids"]), {"603", "155"})

    def test_folder_conflict_scoring_uses_source_title_and_year(self):
        matrix_reloaded = {
            "id": 604,
            "title": "The Matrix Reloaded",
            "original_title": "The Matrix Reloaded",
            "release_date": "2003-05-15",
        }
        tmdb = FakeTMDB(
            folder_results={
                "The Matrix": [dict(MATRIX)],
                "The Matrix Reloaded": [matrix_reloaded],
            }
        )
        engine = self.make_engine(tmdb)
        candidate, match, _ = engine._find_movie(
            "The Matrix",
            1999,
            [
                {
                    "title": "The.Matrix.Reloaded.2003.poster.jpg",
                    "url": "https://example.com/posters",
                    "source": "https://cdn.example.com/matrix-reloaded.jpg",
                }
            ],
        )
        self.assertEqual(candidate["id"], 603)
        self.assertEqual(match["status"], "ready")

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
        self.assertIn("Google Lens reverse-image request failed", match["reason"])

    def test_no_reverse_image_matches_keeps_title_only_candidate_in_review(self):
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
