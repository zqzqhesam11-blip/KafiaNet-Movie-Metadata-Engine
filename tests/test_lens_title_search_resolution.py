from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.engine import MetadataEngine
from app.google_lens import BrightDataGoogleLensClient


def movie_details(movie_id: int, title: str, year: int, imdb_id: str | None = None) -> dict:
    return {
        "id": movie_id,
        "title": title,
        "original_title": title,
        "release_date": f"{year}-01-01",
        "original_language": "en",
        "external_ids": {"imdb_id": imdb_id},
        "production_countries": [],
        "credits": {"cast": [], "crew": []},
        "release_dates": {"results": []},
        "alternative_titles": {"titles": []},
        "keywords": {"keywords": []},
        "images": {"posters": []},
    }


class LensFirstResolutionTests(unittest.TestCase):
    def make_engine(self):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine._details_cache = {}
        engine._stop_batch = False
        engine._provider_stop_status = None
        engine._provider_stop_reason = None
        engine.reverse_image = SimpleNamespace()
        engine.tmdb = Mock()
        return engine

    def test_lens_title_uses_one_tmdb_search_and_no_second_google_search(self):
        engine = self.make_engine()
        details = movie_details(10754, "A Short Film About Killing", 1988, "tt0095468")
        engine.tmdb.search_movies.return_value = {
            "results": [{
                "id": 10754,
                "title": "A Short Film About Killing",
                "original_title": "Krótki film o zabijaniu",
                "release_date": "1988-03-11",
            }]
        }
        engine.tmdb.movie_details.return_value = details
        lens_rows = [{
            "title": "A Short Film About Killing (1988)",
            "url": "https://play.google.com/store/movies/details/a_short_film_about_killing",
        }]

        candidate, match, evidence = engine._find_movie(lens_rows)

        self.assertEqual(match["status"], "ready")
        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["tmdb_id"], 10754)
        self.assertEqual(match["imdb_id"], "tt0095468")
        engine.tmdb.search_movies.assert_called_once_with("A Short Film About Killing", year=1988)
        engine.tmdb.find_by_imdb_id.assert_not_called()
        engine.tmdb.movie_details.assert_called_once_with(10754)
        self.assertEqual(evidence["official_title_search"]["tmdb_ids"], [10754])
        self.assertEqual(
            evidence["official_title_search"]["source"],
            "tmdb_search_from_google_lens_image_match",
        )
        self.assertFalse(evidence["resolved_identity"]["folder_name_used"])

    def test_no_exact_tmdb_title_match_stays_review_after_one_query(self):
        engine = self.make_engine()
        engine.tmdb.search_movies.return_value = {
            "results": [{
                "id": 1,
                "title": "A different film",
                "original_title": "A different film",
                "release_date": "1988-01-01",
            }]
        }

        candidate, match, evidence = engine._find_movie([
            {"title": "A Short Film About Killing (1988)"}
        ])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(
            evidence["official_title_search"]["status"],
            "no_exact_title_year_match",
        )
        engine.tmdb.search_movies.assert_called_once()
        engine.tmdb.movie_details.assert_not_called()
        engine.tmdb.find_by_imdb_id.assert_not_called()

    def test_lens_title_and_year_choose_between_competing_direct_image_matches(self):
        engine = self.make_engine()
        first = movie_details(10754, "A Short Film About Killing", 1988)
        second = movie_details(99999, "A Short Film About Killing", 2005)
        engine.tmdb.movie_details.side_effect = [first, second]
        lens_rows = [
            {
                "title": "A Short Film About Killing (1988)",
                "url": "https://www.themoviedb.org/movie/10754-a-short-film-about-killing",
            },
            {
                "title": "A Short Film About Killing (1988)",
                "url": "https://www.themoviedb.org/movie/99999-other-result",
            },
        ]

        candidate, match, evidence = engine._find_movie(lens_rows)

        self.assertEqual(match["status"], "ready")
        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(
            evidence["identity_disambiguation"]["selected_tmdb_id"],
            10754,
        )
        engine.tmdb.search_movies.assert_not_called()
        engine.tmdb.find_by_imdb_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
