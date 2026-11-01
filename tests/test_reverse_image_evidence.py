from __future__ import annotations

import unittest

from app.image_evidence import extract_tmdb_movie_ids, tmdb_movie_id_from_url


class TMDBMovieIdExtractionTests(unittest.TestCase):
    def test_accepts_only_official_tmdb_movie_routes(self):
        self.assertEqual(
            tmdb_movie_id_from_url("https://www.themoviedb.org/movie/603-the-matrix"),
            603,
        )
        self.assertEqual(
            tmdb_movie_id_from_url("https://themoviedb.org/movie/10754"),
            10754,
        )
        self.assertIsNone(tmdb_movie_id_from_url("https://evil.example/movie/603"))
        self.assertIsNone(tmdb_movie_id_from_url("https://www.themoviedb.org/tv/603-a-show"))
        self.assertIsNone(tmdb_movie_id_from_url("https://www.themoviedb.org/person/603"))
        self.assertIsNone(tmdb_movie_id_from_url("javascript:alert(1)"))

    def test_extracts_unique_ids_from_resolved_lens_destinations(self):
        ids = extract_tmdb_movie_ids([
            {
                "title": "Wrong title and year (1900)",
                "url": "https://lens.google.com/goto?url=CAESopaque",
                "resolved_url": "https://www.themoviedb.org/movie/10754-a-film",
                "source": "https://cdn.example/another-film-poster.jpg",
                "content": "Country: France; Director: Wrong Person",
            },
            {
                "title": "IMDb result",
                "resolved_url": "https://www.imdb.com/title/tt0095468/",
            },
            {
                "title": "Spoofed TMDB path",
                "resolved_url": "https://evil.example/movie/99999",
            },
            {
                "title": "Duplicate TMDB result",
                "resolved_url": "https://www.themoviedb.org/movie/10754",
            },
            {
                "title": "Another movie",
                "resolved_urls": [
                    "https://www.themoviedb.org/movie/31056",
                    "https://www.themoviedb.org/movie/10754",
                ],
            },
        ])

        self.assertEqual(ids, [10754, 31056])

    def test_title_snippet_country_and_image_filename_never_create_id(self):
        ids = extract_tmdb_movie_ids([{
            "title": "Buladó (2020)",
            "url": "https://example.test/Bulado-2020-poster.jpg",
            "source": "https://cdn.example/Bulado-2020-poster.jpg",
            "content": "TMDB ID 737756; Country Curaçao",
        }])

        self.assertEqual(ids, [])

    def test_provider_errors_and_opaque_google_goto_tokens_are_ignored(self):
        ids = extract_tmdb_movie_ids([
            {
                "error": "BrightDataAPIError",
                "message": "Lens request failed",
                "resolved_url": "https://www.themoviedb.org/movie/10754",
            },
            {
                "title": "Opaque Google route",
                "url": "https://www.google.com/goto?url=CAESopaque",
            },
        ])

        self.assertEqual(ids, [])

    def test_google_readable_url_wrapper_is_unwrapped(self):
        ids = extract_tmdb_movie_ids([{
            "url": "https://www.google.com/url?q=https%3A%2F%2Fwww.themoviedb.org%2Fmovie%2F603",
        }])

        self.assertEqual(ids, [603])


if __name__ == "__main__":
    unittest.main()
