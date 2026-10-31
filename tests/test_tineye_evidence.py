from __future__ import annotations

import unittest

from app.tineye_evidence import (
    extract_tineye_evidence,
    imdb_id_from_url,
    tmdb_movie_id_from_url,
)


class TinEyeEvidenceTests(unittest.TestCase):
    def test_extracts_movie_title_from_image_filename(self):
        evidence = extract_tineye_evidence([
            {
                "title": "The.Matrix.1999.1080p.POSTER.jpg",
                "url": "https://example.com/gallery",
                "source": "https://cdn.example.com/art/the-matrix-cover.jpg",
            }
        ])
        self.assertIn("the matrix 1080p", [x.casefold() for x in evidence["title_hints"]])
        self.assertEqual(evidence["imdb_ids"], [])
        self.assertEqual(evidence["tmdb_ids"], [])

    def test_ignores_generic_image_names(self):
        evidence = extract_tineye_evidence([
            {"title": "poster.jpg", "url": "https://example.com", "source": ""}
        ])
        self.assertEqual(evidence["title_hints"], [])

    def test_extracts_imdb_id_only_from_imdb_host(self):
        self.assertEqual(
            imdb_id_from_url("https://www.imdb.com/title/tt0133093/"),
            "tt0133093",
        )
        self.assertIsNone(imdb_id_from_url("https://evil.example/tt0133093"))

    def test_extracts_tmdb_id_only_from_tmdb_host(self):
        self.assertEqual(
            tmdb_movie_id_from_url("https://www.themoviedb.org/movie/603-the-matrix"),
            603,
        )
        self.assertIsNone(tmdb_movie_id_from_url("https://evil.example/movie/603"))

    def test_rejects_tmdb_tv_routes(self):
        self.assertIsNone(
            tmdb_movie_id_from_url("https://www.themoviedb.org/tv/603-a-show")
        )


if __name__ == "__main__":
    unittest.main()
