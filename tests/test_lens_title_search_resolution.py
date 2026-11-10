from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.engine import MetadataEngine
from app.google_lens import BrightDataGoogleLensClient
from app.image_evidence import extract_imdb_ids


class LensIdentifierOnlyResolutionTests(unittest.TestCase):
    @staticmethod
    def make_engine(tmdb):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine._details_cache = {}
        engine._stop_batch = False
        engine._provider_stop_status = None
        engine.tmdb = tmdb
        engine.reverse_image = SimpleNamespace()
        return engine

    @staticmethod
    def movie_details(movie_id=10754, imdb_id="tt0095468"):
        return {
            "id": movie_id,
            "title": "A Short Film About Killing",
            "original_title": "Krótki film o zabijaniu",
            "release_date": "1988-03-11",
            "external_ids": {"imdb_id": imdb_id},
            "production_countries": [{"name": "Poland", "iso_3166_1": "PL"}],
            "credits": {"cast": [], "crew": []},
            "release_dates": {"results": []},
            "alternative_titles": {"titles": []},
            "keywords": {"keywords": []},
            "images": {"posters": []},
        }

    def test_lens_tmdb_movie_id_is_used_directly_with_tmdb_details(self):
        tmdb = Mock()
        tmdb.movie_details.return_value = self.movie_details()
        engine = self.make_engine(tmdb)

        candidate, match, evidence = engine._find_movie([{
            "title": "Unrelated caption is not used for identity",
            "url": "https://www.themoviedb.org/movie/10754-a-short-film-about-killing",
        }])

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 10754)
        tmdb.movie_details.assert_called_once_with(10754)
        tmdb.find_by_imdb_id.assert_not_called()
        tmdb.search_movies.assert_not_called()
        self.assertEqual(evidence["strategy"], "google_lens_official_id")
        self.assertEqual(evidence["tmdb_id_lookup"]["tmdb_ids"], [10754])

    def test_lens_imdb_id_is_resolved_through_tmdb_find_then_details(self):
        tmdb = Mock()
        tmdb.find_by_imdb_id.return_value = {"movie_results": [{"id": 10754}]}
        tmdb.movie_details.return_value = self.movie_details()
        engine = self.make_engine(tmdb)

        candidate, match, evidence = engine._find_movie([{
            "url": "https://www.imdb.com/title/tt0095468/",
        }])

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        tmdb.find_by_imdb_id.assert_called_once_with("tt0095468")
        tmdb.movie_details.assert_called_once_with(10754)
        tmdb.search_movies.assert_not_called()
        self.assertEqual(evidence["id_resolution"]["imdb_to_tmdb_ids"], {
            "tt0095468": [10754]
        })

    def test_lens_caption_without_official_identifier_does_not_trigger_search(self):
        tmdb = Mock()
        engine = self.make_engine(tmdb)
        google_search = Mock(return_value=[{
            "url": "https://www.themoviedb.org/movie/10754"
        }])
        engine.reverse_image.search_official_movie_pages_from_lens = google_search

        candidate, match, evidence = engine._find_movie([{
            "title": "A Short Film About Killing (1988)",
            "url": "https://play.google.com/store/movies/details/a_short_film_about_killing",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(evidence["lens_title_resolution"]["status"], "disabled_id_only_mode")
        self.assertEqual(evidence["official_title_search"]["status"], "disabled_id_only_mode")
        google_search.assert_not_called()
        tmdb.search_movies.assert_not_called()
        tmdb.find_by_imdb_id.assert_not_called()
        tmdb.movie_details.assert_not_called()

    def test_exact_matches_without_identifier_also_fetches_visual_matches(self):
        client = BrightDataGoogleLensClient.__new__(BrightDataGoogleLensClient)
        exact_rows = [{
            "title": "Film title caption",
            "url": "https://play.google.com/store/movies/details/film_title",
            "source": "https://encrypted-tbn0.gstatic.com/images/exact.jpg",
        }]
        visual_rows = [{
            "title": "A Short Film About Killing",
            "url": "https://www.themoviedb.org/movie/10754",
            "source": "https://encrypted-tbn0.gstatic.com/images/visual.jpg",
        }]
        diagnostics = {
            "attempts": 1,
            "raw_result_count": 1,
            "http_status": 200,
            "raw_result_fields": [],
            "raw_link_paths": {},
        }
        client._request_lens_tab = Mock(side_effect=[
            (exact_rows, exact_rows, diagnostics),
            (visual_rows, visual_rows, diagnostics),
        ])

        results = client.search_by_image_url("https://images.example.test/poster.jpg")

        self.assertEqual(
            [call.args[1] for call in client._request_lens_tab.call_args_list],
            ["exact_matches", "visual_matches"],
        )
        self.assertEqual(client.last_lens_diagnostics["lens_tabs_attempted"], [
            "exact_matches", "visual_matches"
        ])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1]["lens_tab"], "visual_matches")

    def test_exact_matches_identifier_skips_second_lens_tab(self):
        client = BrightDataGoogleLensClient.__new__(BrightDataGoogleLensClient)
        exact_rows = [{
            "title": "A Short Film About Killing",
            "url": "https://www.themoviedb.org/movie/10754",
        }]
        diagnostics = {
            "attempts": 1,
            "raw_result_count": 1,
            "http_status": 200,
            "raw_result_fields": [],
            "raw_link_paths": {},
        }
        client._request_lens_tab = Mock(return_value=(exact_rows, exact_rows, diagnostics))

        results = client.search_by_image_url("https://images.example.test/poster.jpg")

        client._request_lens_tab.assert_called_once()
        self.assertEqual(client.last_lens_diagnostics["lens_tabs_attempted"], [
            "exact_matches"
        ])
        self.assertEqual(results[0]["lens_tab"], "exact_matches")

    def test_imdb_news_subpage_is_not_accepted_as_a_movie_identifier(self):
        self.assertEqual(extract_imdb_ids([{
            "url": "https://www.imdb.com/title/tt0190106/news"
        }]), [])
        self.assertEqual(extract_imdb_ids([{
            "url": "https://www.imdb.com/title/tt0095468/"
        }]), ["tt0095468"])


if __name__ == "__main__":
    unittest.main()
