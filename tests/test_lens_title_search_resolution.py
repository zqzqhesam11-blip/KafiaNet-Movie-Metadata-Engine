from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.engine import MetadataEngine


class LensDerivedOfficialTitleResolutionTests(unittest.TestCase):
    def test_ai_mode_and_navigation_captions_are_not_movie_title_hints(self):
        from app.google_lens import BrightDataGoogleLensClient

        for caption in ("AI Mode", "Google AI Overview", "โหมด AI", "Google Search"):
            with self.subTest(caption=caption):
                self.assertEqual(
                    BrightDataGoogleLensClient._clean_title_hint(caption)[0],
                    "",
                )

        hints, year = BrightDataGoogleLensClient.title_hints_from_lens_results([
            {"title": "AI Mode"},
            {"title": "Google Search"},
        ])
        self.assertEqual(hints, [])
        self.assertIsNone(year)

    def test_official_title_search_5xx_stops_batch(self):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine._details_cache = {}
        engine._stop_batch = False
        engine._provider_stop_status = None
        searcher = Mock(side_effect=RuntimeError("Bright Data upstream HTTP 502"))
        engine.reverse_image = SimpleNamespace(
            search_official_movie_pages_from_lens=searcher,
            last_official_search_diagnostics={
                "status": "http_error",
                "http_status": 502,
                "query_count": 1,
                "attempts": 3,
                "error": "Bright Data service error HTTP 502",
            },
        )
        engine.tmdb = Mock()

        candidate, match, evidence = engine._find_movie([
            {"title": "Movie title from Lens"}
        ])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertTrue(engine._stop_batch)
        self.assertEqual(engine._provider_stop_status, 502)
        self.assertEqual(
            evidence["official_title_search"]["status"],
            "http_error",
        )
        self.assertEqual(
            evidence["official_title_search"]["error"],
            "Bright Data service error HTTP 502",
        )
        engine.tmdb.find_by_imdb_id.assert_not_called()

    def test_official_search_urls_are_resolved_and_verified_through_tmdb(self):
        tmdb_id = 10754
        imdb_id = "tt0095468"
        tmdb_url = "https://www.themoviedb.org/movie/10754-a-short-film-about-killing"
        imdb_url = "https://www.imdb.com/title/tt0095468/"

        engine = MetadataEngine.__new__(MetadataEngine)
        engine._details_cache = {}
        engine._stop_batch = False
        engine._provider_stop_status = None

        searcher = Mock(return_value=[
            {
                "title": "A Short Film About Killing (1988) — The Movie Database",
                "url": tmdb_url,
                "url_candidates": [tmdb_url],
                "resolved_urls": [tmdb_url],
                "resolved_url": tmdb_url,
                "provider": "google_serp_official_movie_title_search",
            },
            {
                "title": "A Short Film About Killing (1988) - IMDb",
                "url": imdb_url,
                "url_candidates": [imdb_url],
                "resolved_urls": [imdb_url],
                "resolved_url": imdb_url,
                "provider": "google_serp_official_movie_title_search",
            },
        ])
        engine.reverse_image = SimpleNamespace(
            search_official_movie_pages_from_lens=searcher,
            last_official_search_diagnostics={
                "status": "ok",
                "query_count": 1,
                "title_hints": ["A Short Film About Killing"],
                "year_hint": 1988,
                "organic_result_count": 10,
                "official_url_count": 2,
            },
        )

        details = {
            "id": tmdb_id,
            "title": "A Short Film About Killing",
            "original_title": "Krótki film o zabijaniu",
            "release_date": "1988-03-11",
            "original_language": "pl",
            "external_ids": {"imdb_id": imdb_id},
            "production_countries": [{"name": "Poland", "iso_3166_1": "PL"}],
            "credits": {
                "cast": [
                    {"id": 1, "name": "Mirosław Baka", "character": "Jacek", "order": 0}
                ],
                "crew": [],
            },
            "release_dates": {"results": []},
            "alternative_titles": {"titles": []},
            "keywords": {"keywords": []},
            "images": {"posters": []},
        }
        engine.tmdb = Mock()
        engine.tmdb.find_by_imdb_id.return_value = {
            "movie_results": [{"id": tmdb_id}]
        }
        engine.tmdb.movie_details.return_value = details

        # The only input title evidence is from Google Lens/Play results.
        # No source-folder name or local poster filename is passed to the resolver.
        lens_rows = [{
            "title": "A Short Film About Killing (1988)",
            "url": "https://play.google.com/store/movies/details/a_short_film_about_killing",
            "url_candidates": [
                "https://play.google.com/store/movies/details/a_short_film_about_killing"
            ],
        }]

        candidate, match, evidence = engine._find_movie(lens_rows)

        self.assertEqual(searcher.call_count, 1)
        search_input = searcher.call_args.args[0]
        self.assertEqual(len(search_input), 1)
        self.assertTrue(search_input[0]["url"].startswith("https://play.google.com/"))
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["id"], tmdb_id)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], tmdb_id)
        self.assertEqual(match["imdb_id"], imdb_id)
        self.assertEqual(engine.tmdb.find_by_imdb_id.call_args.args[0], imdb_id)
        self.assertEqual(engine.tmdb.movie_details.call_args.args[0], tmdb_id)
        self.assertEqual(evidence["official_title_search"]["tmdb_ids"], [tmdb_id])
        self.assertEqual(evidence["official_title_search"]["imdb_ids"], [imdb_id])
        self.assertFalse(evidence["resolved_identity"]["folder_name_used"])
        self.assertFalse(evidence["resolved_identity"]["image_filename_used"])
        self.assertIn(tmdb_url, [row.get("url") for row in lens_rows])
        self.assertIn(imdb_url, [row.get("url") for row in lens_rows])


if __name__ == "__main__":
    unittest.main()
