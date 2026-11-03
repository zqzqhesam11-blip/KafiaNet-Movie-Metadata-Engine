from __future__ import annotations

import unittest
from unittest.mock import Mock

import httpx

from app.image_evidence import extract_imdb_ids, extract_tmdb_movie_ids
from app.tineye_api import (
    TINEYE_API_BASE_URL,
    TinEyeAPIClient,
    TinEyeAPIError,
)


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
        api.last_search_diagnostics = {}
        api.last_lens_diagnostics = {}
        api.last_official_search_diagnostics = {
            "status": "not_run",
            "query_count": 0,
            "attempts": 0,
            "organic_result_count": 0,
            "official_url_count": 0,
        }
        return api

    def test_search_uses_official_api_and_preserves_direct_imdb_backlink(self):
        api = self.make_client({
            "code": 200,
            "results": {"matches": [{
                "image_url": "https://images.example/The.Matrix.1999.poster.jpg",
                "image_name": "The.Matrix.1999.poster.jpg",
                "domain": "images.example",
                "score": 98,
                "backlinks": [{
                    "url": "https://images.example/The.Matrix.1999.poster.jpg",
                    "backlink": "https://www.imdb.com/title/tt0133093/",
                    "crawl_date": "2026-10-01",
                }],
            }]},
        })

        result = api.search_by_image_url(
            "https://raw.githubusercontent.com/owner/repo/main/poster.jpg"
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provider"], "tineye_official_api")
        self.assertEqual(result[0]["url"], "https://www.imdb.com/title/tt0133093/")
        self.assertEqual(extract_imdb_ids(api.resolve_result_urls(result)), ["tt0133093"])
        self.assertEqual(api.client.get.call_args.args[0], f"{TINEYE_API_BASE_URL}search/")
        self.assertEqual(
            api.client.get.call_args.kwargs["params"]["image_url"],
            "https://raw.githubusercontent.com/owner/repo/main/poster.jpg",
        )
        self.assertEqual(api.last_search_diagnostics["status"], "ok")
        self.assertEqual(api.last_search_diagnostics["result_count"], 1)

    def test_official_tmdb_backlink_is_the_only_source_of_tmdb_id(self):
        api = self.make_client({
            "code": 200,
            "results": {"matches": [{
                "image_url": "https://images.example/poster.jpg",
                "backlinks": [{
                    "url": "https://images.example/poster.jpg",
                    "backlink": "https://www.themoviedb.org/movie/10754",
                }],
            }]},
        })
        rows = api.search_by_image_url("https://example.com/poster.jpg")
        self.assertEqual(extract_tmdb_movie_ids(api.resolve_result_urls(rows)), [10754])

    def test_empty_matches_return_empty_list(self):
        api = self.make_client({"code": 200, "results": {"matches": []}})
        self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])
        self.assertEqual(api.last_search_diagnostics["status"], "no_matches")

    def test_invalid_or_non_https_url_is_rejected_before_network_request(self):
        api = self.make_client({"code": 200, "results": {"matches": []}})
        for url in ("http://example.com/poster.jpg", "file:///poster.jpg", "https://user:pass@example.com/poster.jpg"):
            with self.subTest(url=url):
                with self.assertRaises(TinEyeAPIError):
                    api.search_by_image_url(url)
        api.client.get.assert_not_called()

    def test_auth_quota_and_rate_limit_errors_are_structured(self):
        for code, message in (
            (401, "TINEYE_API_KEY"),
            (402, "bundle is exhausted"),
            (403, "access was denied"),
            (429, "rate limit"),
        ):
            with self.subTest(code=code):
                api = self.make_client(
                    {"code": code, "messages": ["provider rejected the request"]},
                    status=code,
                )
                with self.assertRaises(TinEyeAPIError) as caught:
                    api.search_by_image_url("https://example.com/poster.jpg")
                self.assertEqual(caught.exception.api_code, code)
                self.assertEqual(caught.exception.status_code, code)
                self.assertIn(message, str(caught.exception))
                self.assertEqual(api.client.get.call_count, 1)
                self.assertEqual(api.last_search_diagnostics["status"], "error")

    def test_network_error_is_reported_without_retry(self):
        api = self.make_client(error=httpx.ConnectError("connection refused"))
        with self.assertRaisesRegex(TinEyeAPIError, "network error"):
            api.search_by_image_url("https://example.com/poster.jpg")
        self.assertEqual(api.client.get.call_count, 1)

    def test_title_hints_use_only_tineye_matched_image_names(self):
        hints, year = TinEyeAPIClient.title_hints_from_reverse_image_results([
            {
                "title": "The.Matrix.1999.poster.jpg",
                "source": "https://cdn.example/Unrelated.Film.2020.jpg",
                "url": "https://www.imdb.com/title/tt0000000/",
                "content": "Another unrelated title",
            },
            {"title": "The Matrix 1999 poster.jpg"},
            {"title": "poster.jpg"},
        ])
        self.assertEqual(hints, ["The Matrix"])
        self.assertEqual(year, 1999)

    def test_remaining_searches_does_not_submit_an_image(self):
        api = self.make_client({
            "code": 200,
            "results": {"total_remaining_searches": 42, "bundles": []},
        })
        account = api.remaining_searches()
        self.assertEqual(account["total_remaining_searches"], 42)
        self.assertEqual(api.client.get.call_args.args[0], f"{TINEYE_API_BASE_URL}remaining_searches/")
        self.assertEqual(api.client.get.call_args.kwargs["params"], {})

    def test_bad_response_shape_is_not_a_successful_no_match(self):
        api = self.make_client({"code": 200, "results": []})
        with self.assertRaisesRegex(TinEyeAPIError, "is not an object"):
            api.search_by_image_url("https://example.com/poster.jpg")


    def test_match_without_backlinks_is_hint_only_and_never_an_identity_url(self):
        api = self.make_client({
            "code": 200,
            "results": {"matches": [{
                "image_url": "https://images.example/The.Matrix.1999.poster.jpg",
                "domain": "images.example",
                "score": 94,
                "backlinks": [],
            }]},
        })

        rows = api.search_by_image_url("https://example.com/poster.jpg")
        resolved = api.resolve_result_urls(rows)
        hints, year = api.title_hints_from_reverse_image_results(resolved)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "The.Matrix.1999.poster.jpg")
        self.assertEqual(rows[0]["source"], "https://images.example/The.Matrix.1999.poster.jpg")
        self.assertEqual(rows[0]["url"], "")
        self.assertEqual(rows[0]["resolved_urls"], [])
        self.assertEqual(extract_tmdb_movie_ids(resolved), [])
        self.assertEqual(extract_imdb_ids(resolved), [])
        self.assertEqual(hints, ["The Matrix"])
        self.assertEqual(year, 1999)

if __name__ == "__main__":
    unittest.main()
