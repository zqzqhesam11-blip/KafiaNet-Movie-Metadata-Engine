from __future__ import annotations

import unittest
from unittest.mock import Mock

import httpx

from app.tineye_api import TinEyeAPIClient, TinEyeAPIError, TINEYE_API_BASE_URL


class TinEyeAPIClientTests(unittest.TestCase):
    def make_client(self, payload=None, status=200, error=None):
        api = TinEyeAPIClient.__new__(TinEyeAPIClient)
        response = Mock()
        response.status_code = status
        response.json.return_value = payload
        api.client = Mock()
        if error is not None:
            api.client.get.side_effect = error
        else:
            api.client.get.return_value = response
        return api

    def test_search_uses_official_endpoint_and_image_url_parameter(self):
        api = self.make_client({
            "code": 200,
            "results": {"matches": [{
                "image_url": "https://matched.example/matrix.jpg",
                "domain": "matched.example",
                "score": 98,
                "backlinks": [{
                    "url": "https://matched.example/images/The.Matrix.1999.poster.jpg",
                    "backlink": "https://www.imdb.com/title/tt0133093/",
                    "crawl_date": "2026-10-01",
                }],
            }]},
        })
        result = api.search_by_image_url("https://raw.githubusercontent.com/owner/repo/main/poster.jpg")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "The.Matrix.1999.poster.jpg")
        self.assertEqual(result[0]["url"], "https://www.imdb.com/title/tt0133093/")
        self.assertEqual(api.client.get.call_args.args[0], f"{TINEYE_API_BASE_URL}search/")
        self.assertEqual(api.client.get.call_args.kwargs["params"]["image_url"],
                         "https://raw.githubusercontent.com/owner/repo/main/poster.jpg")

    def test_empty_matches_return_empty_list(self):
        api = self.make_client({"code": 200, "results": {"matches": []}})
        self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])

    def test_invalid_url_is_rejected_before_network_request(self):
        api = self.make_client({"code": 200, "results": {"matches": []}})
        with self.assertRaisesRegex(TinEyeAPIError, "publicly reachable HTTPS"):
            api.search_by_image_url("http://localhost/poster.jpg")
        api.client.get.assert_not_called()

    def test_401_reports_invalid_api_key(self):
        api = self.make_client({"code": 401, "messages": ["Please provide a valid API key."]}, status=401)
        with self.assertRaisesRegex(TinEyeAPIError, "TINEYE_API_KEY"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_402_reports_exhausted_bundle(self):
        api = self.make_client({"code": 402, "messages": ["No more searches available."]}, status=402)
        with self.assertRaisesRegex(TinEyeAPIError, "bundle is exhausted"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_429_reports_rate_limit(self):
        api = self.make_client({"code": 429, "messages": ["Too many requests."]}, status=429)
        with self.assertRaisesRegex(TinEyeAPIError, "rate limit"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_network_error_is_reported(self):
        api = self.make_client(error=httpx.ConnectError("connection refused"))
        with self.assertRaisesRegex(TinEyeAPIError, "network error"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_remaining_searches_checks_account_without_image_search(self):
        api = self.make_client({"code": 200, "results": {"total_remaining_searches": 42, "bundles": []}})
        self.assertEqual(api.remaining_searches()["total_remaining_searches"], 42)
        self.assertEqual(api.client.get.call_args.args[0], f"{TINEYE_API_BASE_URL}remaining_searches/")
        self.assertEqual(api.client.get.call_args.kwargs["params"], {})


if __name__ == "__main__":
    unittest.main()
