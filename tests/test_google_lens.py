from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

import httpx

from app.google_lens import (
    BRIGHTDATA_REQUEST_URL,
    BrightDataAPIError,
    BrightDataGoogleLensClient,
)


class BrightDataGoogleLensClientTests(unittest.TestCase):
    def make_client(self, payload=None, status=200, error=None, body=None):
        api = BrightDataGoogleLensClient.__new__(BrightDataGoogleLensClient)
        api.zone = "serp_api1"
        response = Mock()
        response.status_code = status
        response.text = body if body is not None else ""
        response.json.return_value = payload
        api.client = Mock()
        if error is not None:
            api.client.post.side_effect = error
        else:
            api.client.post.return_value = response
        return api

    def test_search_uses_brightdata_rest_and_google_lens_visual_matches(self):
        api = self.make_client({
            "visual_matches": [{
                "position": 1,
                "title": "The Matrix (1999) - IMDb",
                "link": "https://www.imdb.com/title/tt0133093/",
                "source": "https://cdn.example.com/matrix-poster.jpg",
                "thumbnail": "https://thumb.example.com/matrix.jpg",
                "description": "Movie poster",
            }]
        })
        result = api.search_by_image_url("https://raw.githubusercontent.com/owner/repo/main/poster.jpg")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "The Matrix (1999) - IMDb")
        self.assertEqual(result[0]["url"], "https://www.imdb.com/title/tt0133093/")
        self.assertEqual(result[0]["source"], "https://cdn.example.com/matrix-poster.jpg")
        call = api.client.post.call_args
        self.assertEqual(call.args[0], BRIGHTDATA_REQUEST_URL)
        self.assertEqual(call.kwargs["json"]["zone"], "serp_api1")
        target = call.kwargs["json"]["url"]
        self.assertIn("lens.google.com/uploadbyurl?", target)
        self.assertIn("brd_json=1", target)
        self.assertIn("brd_lens=visual_matches", target)
        self.assertIn("raw.githubusercontent.com", target)

    def test_accepts_brightdata_images_array_and_original_image(self):
        api = self.make_client({
            "images": [{
                "global_rank": 1,
                "title": "The Matrix (1999)",
                "link": "https://www.imdb.com/title/tt0133093/",
                "image": "https://thumb.example.com/matrix.jpg",
                "original_image": "https://cdn.example.com/matrix-original.jpg",
            }]
        })
        result = api.search_by_image_url("https://example.com/poster.jpg")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://www.imdb.com/title/tt0133093/")
        self.assertEqual(result[0]["source"], "https://cdn.example.com/matrix-original.jpg")
        self.assertEqual(result[0]["position"], 1)

    def test_unwraps_json_encoded_brightdata_body(self):
        nested = {
            "data": {
                "images": [{
                    "title": "Movie poster",
                    "link": "https://example.com/movie",
                    "original_image": "https://example.com/poster-full.jpg",
                }]
            }
        }
        api = self.make_client({"body": json.dumps(nested)})
        result = api.search_by_image_url("https://example.com/poster.jpg")
        self.assertEqual(result[0]["title"], "Movie poster")
        self.assertEqual(result[0]["source"], "https://example.com/poster-full.jpg")

    def test_unrecognized_json_includes_safe_shape_diagnostics(self):
        api = self.make_client({"general": {"query": "hidden"}, "metadata": {"id": "private"}})
        with self.assertRaisesRegex(BrightDataAPIError, r"general=object\[query\]"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_empty_visual_matches_returns_empty_list(self):
        api = self.make_client({"visual_matches": []})
        self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])

    def test_supports_nested_google_lens_results(self):
        api = self.make_client({"data": {"visual_matches": [{"title": "Movie", "link": "https://example.com/movie"}]}})
        self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg")[0]["title"], "Movie")

    def test_invalid_url_is_rejected_before_network_request(self):
        api = self.make_client({"visual_matches": []})
        with self.assertRaisesRegex(BrightDataAPIError, "public HTTPS"):
            api.search_by_image_url("http://localhost/poster.jpg")
        api.client.post.assert_not_called()

    def test_401_reports_invalid_api_key(self):
        api = self.make_client({"error": "unauthorized"}, status=401, body="unauthorized")
        with self.assertRaisesRegex(BrightDataAPIError, "BRIGHTDATA_API_KEY"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_402_reports_exhausted_free_credits(self):
        api = self.make_client({"error": "no credits"}, status=402, body="no credits")
        with self.assertRaisesRegex(BrightDataAPIError, "do not add funds"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_429_reports_request_limit(self):
        api = self.make_client({"error": "rate limit"}, status=429, body="rate limit")
        with self.assertRaisesRegex(BrightDataAPIError, "quota"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_network_error_is_reported_without_retry(self):
        api = self.make_client(error=httpx.ConnectError("connection refused"))
        with self.assertRaisesRegex(BrightDataAPIError, "network error"):
            api.search_by_image_url("https://example.com/poster.jpg")
        self.assertEqual(api.client.post.call_count, 1)

    def test_non_json_response_is_not_mistaken_for_no_matches(self):
        response = Mock()
        response.status_code = 200
        response.text = "<html>challenge</html>"
        response.json.side_effect = ValueError("not json")
        api = self.make_client()
        api.client.post.return_value = response
        with self.assertRaisesRegex(BrightDataAPIError, "did not return parsed JSON"):
            api.search_by_image_url("https://example.com/poster.jpg")


if __name__ == "__main__":
    unittest.main()
