from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.engine import MetadataEngine


class PreviousGoogleLensEvidenceTests(unittest.TestCase):
    def make_engine(self, latest_results, previous_results):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.settings = SimpleNamespace(
            posters_repo="owner/posters",
            posters_branch="main",
        )
        engine.reverse_image = object()
        engine.github = Mock()
        engine.github.raw_url.return_value = "https://raw.example/poster.jpg"
        engine.github.get_json_file.return_value = {
            "tineye": {
                "results": previous_results,
                "evidence": {"title_hints": ["Saved Poster Title"]},
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
        self.assertEqual(engine._find_movie.call_args.args[2], previous)

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


if __name__ == "__main__":
    unittest.main()
