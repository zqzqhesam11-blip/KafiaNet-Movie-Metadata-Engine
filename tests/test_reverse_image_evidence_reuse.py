from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.engine import MetadataEngine
from app.google_lens import BrightDataQuotaError


class PreviousGoogleLensEvidenceTests(unittest.TestCase):
    def make_engine(self, latest_results, previous_results, saved_lens_tab="exact_matches"):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.settings = SimpleNamespace(
            posters_repo="owner/posters",
            posters_branch="main",
        )
        engine.reverse_image = object()
        engine.github = Mock()
        engine.github.raw_url.return_value = "https://raw.example/poster.jpg"
        engine.github.get_json_file.return_value = {
            "reverse_image": {
                "results": previous_results,
                "evidence": {"title_hints": ["Saved Poster Title"]},
                "latest_query": {
                    "diagnostics": {"lens_tab": saved_lens_tab}
                } if saved_lens_tab else {},
            }
        }
        engine.tmdb = Mock()
        engine._details_cache = {}
        engine._reverse_image_results = Mock(return_value=latest_results)
        engine._find_movie = Mock(return_value=(
            None,
            {"status": "review", "reason": "test"},
            {"title_hints": []},
        ))
        return engine

    def test_reuses_saved_results_when_live_search_returns_empty(self):
        previous = [{
            "title": "Saved Poster Title",
            "url": "https://example.com/movie",
            "source": "https://cdn.example.com/poster.jpg",
        }]
        engine = self.make_engine([], previous)

        record = engine._record_for_folder(
            {"folder": "Example Movie (2000)", "image_path": "مراجعة/Example Movie (2000)/poster.jpg"},
            previous_output_path="مراجعة/Example Movie (2000)/Example Movie (2000).json",
        )

        self.assertEqual(record["reverse_image"]["results"], previous)
        self.assertEqual(record["reverse_image"]["results_source"], "previous_saved")
        self.assertEqual(record["reverse_image"]["latest_query"]["status"], "no_matches")
        self.assertEqual(engine._find_movie.call_args.args[0], previous)

    def test_reuses_saved_results_when_live_search_is_unresponsive(self):
        previous = [{
            "title": "Saved Poster Title",
            "url": "https://example.com/movie",
            "source": "https://cdn.example.com/poster.jpg",
        }]
        latest_error = [{
            "error": "RuntimeError",
            "message": "Google Lens API rate limit or service failure",
        }]
        engine = self.make_engine(latest_error, previous)

        record = engine._record_for_folder(
            {"folder": "Example Movie (2000)", "image_path": "مراجعة/Example Movie (2000)/poster.jpg"},
            previous_output_path="مراجعة/Example Movie (2000)/Example Movie (2000).json",
        )

        self.assertEqual(record["reverse_image"]["results"], previous)
        self.assertEqual(record["reverse_image"]["results_source"], "previous_saved")
        self.assertEqual(record["reverse_image"]["latest_query"]["status"], "error")
        self.assertEqual(record["reverse_image"]["latest_query"]["errors"], latest_error)


    def test_does_not_reuse_saved_visual_matches_as_exact_match_evidence(self):
        previous = [{
            "title": "Saved Poster Title",
            "url": "https://www.themoviedb.org/movie/10754",
            "source": "https://cdn.example.com/poster.jpg",
        }]
        engine = self.make_engine([], previous, saved_lens_tab="visual_matches")

        record = engine._record_for_folder(
            {"folder": "Example Movie (2000)", "image_path": "مراجعة/Example Movie (2000)/poster.jpg"},
            previous_output_path="مراجعة/Example Movie (2000)/Example Movie (2000).json",
        )

        self.assertEqual(record["reverse_image"]["results"], [])
        self.assertEqual(record["reverse_image"]["results_source"], "live")
        self.assertEqual(engine._find_movie.call_args.args[0], [])

    def test_does_not_reuse_saved_results_without_exact_matches_tab_marker(self):
        previous = [{
            "title": "Old saved result",
            "url": "https://www.themoviedb.org/movie/10754",
        }]
        engine = self.make_engine([], previous, saved_lens_tab=None)

        record = engine._record_for_folder(
            {"folder": "Example Movie (2000)", "image_path": "مراجعة/Example Movie (2000)/poster.jpg"},
            previous_output_path="مراجعة/Example Movie (2000)/Example Movie (2000).json",
        )

        self.assertEqual(record["reverse_image"]["results"], [])
        self.assertEqual(record["reverse_image"]["results_source"], "live")

    def test_does_not_reuse_previous_error_rows_as_evidence(self):
        previous_errors = [{"error": "RuntimeError", "message": "old failure"}]
        latest_error = [{"error": "RuntimeError", "message": "new failure"}]
        engine = self.make_engine(latest_error, previous_errors)

        record = engine._record_for_folder(
            {"folder": "Example Movie (2000)", "image_path": "مراجعة/Example Movie (2000)/poster.jpg"},
            previous_output_path="مراجعة/Example Movie (2000)/Example Movie (2000).json",
        )

        self.assertEqual(record["reverse_image"]["results"], latest_error)
        self.assertEqual(record["reverse_image"]["results_source"], "live")
        self.assertEqual(record["reverse_image"]["latest_query"]["status"], "error")


    def test_provider_quota_stop_flag_prevents_batch_continuation(self):
        for status_code in (402, 429):
            with self.subTest(status_code=status_code):
                engine = MetadataEngine.__new__(MetadataEngine)
                engine.reverse_image = Mock()
                engine.reverse_image.search_by_image_url.side_effect = BrightDataQuotaError(
                    status_code, f"Bright Data HTTP {status_code}"
                )
                engine._stop_batch = False
                engine._provider_stop_status = None

                result = engine._reverse_image_results("https://example.com/poster.jpg")

                self.assertTrue(engine._stop_batch)
                self.assertEqual(engine._provider_stop_status, status_code)
                self.assertTrue(result[0]["stop_batch"])
                self.assertEqual(engine.reverse_image.search_by_image_url.call_count, 1)


if __name__ == "__main__":
    unittest.main()
