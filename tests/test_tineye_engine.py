from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.engine import MetadataEngine
from app.tineye_api import TinEyeAPIError


class TinEyeEngineIntegrationTests(unittest.TestCase):
    def make_engine(self, status_code=None, api_code=None):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.reverse_image = Mock()
        engine.reverse_image.last_lens_diagnostics = {
            "status": "error",
            "http_status": status_code,
            "api_code": api_code,
        }
        engine._stop_batch = False
        engine._provider_stop_status = None
        engine._provider_stop_reason = None
        return engine

    def test_auth_quota_and_rate_limit_errors_stop_batch(self):
        for code in (401, 402, 403, 429):
            with self.subTest(code=code):
                engine = self.make_engine(code, code)
                engine.reverse_image.search_by_image_url.side_effect = TinEyeAPIError(
                    "TinEye API rejected request",
                    status_code=code,
                    api_code=code,
                )

                result = engine._reverse_image_results("https://example.com/poster.jpg")

                self.assertTrue(engine._stop_batch)
                self.assertEqual(engine._provider_stop_status, code)
                self.assertEqual(engine._provider_stop_reason, "tineye_auth_quota_or_throttle")
                self.assertTrue(result[0]["stop_batch"])
                self.assertEqual(result[0]["provider"], "tineye_official_api")

    def test_server_failure_stops_batch(self):
        engine = self.make_engine(503, 503)
        engine.reverse_image.search_by_image_url.side_effect = TinEyeAPIError(
            "TinEye upstream unavailable",
            status_code=503,
            api_code=503,
        )

        result = engine._reverse_image_results("https://example.com/poster.jpg")

        self.assertTrue(engine._stop_batch)
        self.assertEqual(engine._provider_stop_status, 503)
        self.assertEqual(engine._provider_stop_reason, "provider_server_error")
        self.assertTrue(result[0]["stop_batch"])

    def test_network_or_unparsed_success_response_stops_batch(self):
        for status_code, api_code in ((None, None), (200, None)):
            with self.subTest(status_code=status_code):
                engine = self.make_engine(status_code, api_code)
                engine.reverse_image.search_by_image_url.side_effect = TinEyeAPIError(
                    "TinEye did not return a usable response",
                    status_code=status_code,
                    api_code=api_code,
                )

                result = engine._reverse_image_results("https://example.com/poster.jpg")

                self.assertTrue(engine._stop_batch)
                self.assertTrue(result[0]["stop_batch"])

    def test_invalid_single_image_request_does_not_stop_other_posters(self):
        engine = self.make_engine(400, 400)
        engine.reverse_image.search_by_image_url.side_effect = TinEyeAPIError(
            "Invalid poster URL",
            status_code=400,
            api_code=400,
        )

        result = engine._reverse_image_results("https://example.com/poster.jpg")

        self.assertFalse(engine._stop_batch)
        self.assertNotIn("stop_batch", result[0])

    def test_matched_image_name_alone_cannot_create_movie_identity(self):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine._details_cache = {}
        engine.tmdb = Mock()
        engine.tmdb.search_movies.return_value = {"results": []}

        candidate, match, evidence = engine._find_movie([{
            "title": "The.Matrix.1999.poster.jpg",
            "url": "https://images.example/The.Matrix.1999.poster.jpg",
            "source": "https://cdn.example/The.Matrix.1999.poster.jpg",
            "content": "Movie title and year in a matching result",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertFalse(evidence["id_resolution"]["folder_name_used"])
        self.assertFalse(evidence["id_resolution"]["image_filename_used"])
        engine.tmdb.search_movies.assert_not_called()
        engine.tmdb.find_by_imdb_id.assert_not_called()
        engine.tmdb.movie_details.assert_not_called()


if __name__ == "__main__":
    unittest.main()
