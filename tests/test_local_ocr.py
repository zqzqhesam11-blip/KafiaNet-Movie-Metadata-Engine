from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.local_ocr import LocalPosterOCR


class LocalPosterOCRTests(unittest.TestCase):
    def test_only_raw_github_https_image_urls_are_allowed(self):
        accepted = "https://raw.githubusercontent.com/owner/repo/main/poster.jpg"
        self.assertEqual(LocalPosterOCR.validate_image_url(accepted), accepted)

        for rejected in (
            "http://raw.githubusercontent.com/owner/repo/main/poster.jpg",
            "https://example.com/poster.jpg",
            "https://user:pass@raw.githubusercontent.com/owner/repo/main/poster.jpg",
            "file:///tmp/poster.jpg",
        ):
            with self.subTest(url=rejected):
                with self.assertRaises(ValueError):
                    LocalPosterOCR.validate_image_url(rejected)

    def test_filters_low_confidence_and_numeric_noise(self):
        result = SimpleNamespace(
            txts=("The Last Samurai", "2024", "X", "A Better Tomorrow"),
            scores=(0.96, 0.99, 0.99, 0.35),
            boxes=(
                ((0, 0), (150, 0), (150, 24), (0, 24)),
                ((0, 30), (50, 30), (50, 42), (0, 42)),
                ((0, 45), (10, 45), (10, 50), (0, 50)),
                ((0, 55), (120, 55), (120, 70), (0, 70)),
            ),
        )

        normalized = LocalPosterOCR._normalize_result(result, image_height=100)

        self.assertEqual(normalized["status"], "ok")
        self.assertEqual(
            [row["text"] for row in normalized["text_lines"]],
            ["The Last Samurai"],
        )
        self.assertEqual(
            [row["text"] for row in normalized["title_hints"]],
            ["The Last Samurai"],
        )

    def test_empty_result_is_not_promoted_to_a_title(self):
        result = SimpleNamespace(txts=("2022", "A"), scores=(0.99, 0.99), boxes=())
        normalized = LocalPosterOCR._normalize_result(result, image_height=100)

        self.assertEqual(normalized["status"], "no_text")
        self.assertEqual(normalized["title_hints"], [])


if __name__ == "__main__":
    unittest.main()
