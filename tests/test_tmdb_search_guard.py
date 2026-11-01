from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.tmdb import TMDBClient


class TMDBMovieDetailsArtworkTests(unittest.TestCase):
    def make_client(self):
        client = TMDBClient.__new__(TMDBClient)
        client.language = "en-US"
        client.client = Mock()
        return client

    def test_movie_details_requests_metadata_and_images_with_bearer_token(self):
        client = self.make_client()
        response = Mock()
        response.json.return_value = {"id": 123, "images": {"posters": []}}
        client.client.get.return_value = response

        details = client.movie_details(123)

        self.assertEqual(details["id"], 123)
        args, kwargs = client.client.get.call_args
        self.assertEqual(args[0], "https://api.themoviedb.org/3/movie/123")
        self.assertIn("images", kwargs["params"]["append_to_response"])
        self.assertEqual(kwargs["params"]["include_image_language"], "en,null")

    def test_builds_safe_tmdb_poster_urls(self):
        self.assertEqual(
            TMDBClient.image_url("/poster.jpg", "w500"),
            "https://image.tmdb.org/t/p/w500/poster.jpg",
        )
        self.assertEqual(
            TMDBClient.image_url("/poster.jpg", "original"),
            "https://image.tmdb.org/t/p/original/poster.jpg",
        )
        self.assertIsNone(TMDBClient.image_url("https://evil.example/poster.jpg"))
        self.assertIsNone(TMDBClient.image_url(""))

    def test_normalized_details_include_ranked_tmdb_poster_images(self):
        details = {
            "id": 123,
            "title": "Example Film",
            "original_title": "Example Film",
            "release_date": "2020-01-01",
            "original_language": "en",
            "poster_path": "/primary.jpg",
            "images": {
                "posters": [
                    {
                        "file_path": "/lower.jpg",
                        "iso_639_1": None,
                        "width": 500,
                        "height": 750,
                        "vote_average": 4.0,
                        "vote_count": 1,
                    },
                    {
                        "file_path": "/better.jpg",
                        "iso_639_1": "en",
                        "width": 1000,
                        "height": 1500,
                        "vote_average": 8.0,
                        "vote_count": 20,
                    },
                ]
            },
        }

        normalized = TMDBClient.normalize_details(details)

        self.assertEqual(normalized["poster_url"], "https://image.tmdb.org/t/p/w500/primary.jpg")
        self.assertEqual(normalized["posters"][0]["file_path"], "/better.jpg")
        self.assertEqual(
            normalized["posters"][0]["url"],
            "https://image.tmdb.org/t/p/w500/better.jpg",
        )
        self.assertEqual(
            normalized["posters"][0]["original_url"],
            "https://image.tmdb.org/t/p/original/better.jpg",
        )


if __name__ == "__main__":
    unittest.main()
