from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

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

    def test_search_uses_brightdata_rest_and_google_lens_exact_matches(self):
        api = self.make_client({
            "exact_matches": [{
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
        self.assertEqual(call.kwargs["json"]["format"], "raw")
        target = call.kwargs["json"]["url"]
        self.assertIn("lens.google.com/uploadbyurl?", target)
        self.assertIn("brd_json=1", target)
        self.assertIn("brd_lens=exact_matches", target)
        self.assertIn("raw.githubusercontent.com", target)

    def test_lens_image_search_uses_documented_parameters_only(self):
        api = self.make_client({"exact_matches": []})

        rows = api.search_by_image_url("https://example.com/poster.jpg")

        self.assertEqual(rows, [])
        self.assertEqual(api.client.post.call_count, 1)
        request_url = api.client.post.call_args.kwargs["json"]["url"]
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(request_url).query)
        self.assertEqual(query["url"], ["https://example.com/poster.jpg"])
        self.assertEqual(query["brd_lens"], ["exact_matches"])
        self.assertEqual(query["hl"], ["en"])
        self.assertEqual(api.last_lens_diagnostics["lens_tab"], "exact_matches")
        self.assertNotIn("q", query)
        self.assertNotIn("query", query)

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

    def test_unwraps_brightdata_json_api_envelope(self):
        results = {
            "exact_matches": [{
                "title": "A Short Film About Killing (1988)",
                "link": "https://www.imdb.com/title/tt0095468/",
                "source": "https://images.example.com/poster.jpg",
            }]
        }
        api = self.make_client({
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(results),
        })
        result = api.search_by_image_url("https://example.com/poster.jpg")
        self.assertEqual(len(result), 1)
        self.assertIn("A Short Film About Killing", result[0]["title"])

    def test_reports_brightdata_inner_http_errors_from_json_envelope(self):
        api = self.make_client({
            "status_code": 401,
            "headers": {"content-type": "application/json"},
            "body": "unauthorized",
        })
        with self.assertRaisesRegex(BrightDataAPIError, "BRIGHTDATA_API_KEY"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_retries_empty_http_200_response_and_recovers(self):
        empty = Mock()
        empty.status_code = 200
        empty.text = ""
        empty.headers = {"content-type": "application/json", "content-length": "0"}
        empty.json.side_effect = ValueError("empty")

        recovered = Mock()
        recovered.status_code = 200
        recovered.text = '{"exact_matches":[]}'
        recovered.headers = {"content-type": "application/json", "content-length": "21"}
        recovered.json.return_value = {"exact_matches": []}

        api = self.make_client()
        api.client.post.side_effect = [empty, recovered]
        with patch("app.google_lens.time.sleep"):
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])

        self.assertEqual(api.client.post.call_count, 2)
        self.assertEqual(api.last_lens_diagnostics["attempts"], 2)
        self.assertEqual(api.last_lens_diagnostics["status"], "no_matches")

    def test_empty_json_response_retries_then_reports_http_metadata(self):
        response = Mock()
        response.status_code = 200
        response.text = ""
        response.headers = {"content-type": "application/json", "content-length": "0"}
        response.json.side_effect = ValueError("empty")
        api = self.make_client()
        api.client.post.return_value = response
        with patch("app.google_lens.time.sleep"):
            with self.assertRaisesRegex(BrightDataAPIError, "content-length=0"):
                api.search_by_image_url("https://example.com/poster.jpg")

        self.assertEqual(api.client.post.call_count, 3)
        self.assertEqual(api.last_lens_diagnostics["attempts"], 3)

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

    def test_empty_exact_matches_returns_empty_list(self):
        api = self.make_client({"exact_matches": []})
        self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])

    def test_supports_nested_google_lens_results(self):
        api = self.make_client({"data": {"exact_matches": [{"title": "Movie", "link": "https://example.com/movie"}]}})
        self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg")[0]["title"], "Movie")

    def test_invalid_url_is_rejected_before_network_request(self):
        api = self.make_client({"exact_matches": []})
        with self.assertRaisesRegex(BrightDataAPIError, "public HTTPS"):
            api.search_by_image_url("http://localhost/poster.jpg")
        api.client.post.assert_not_called()

    def test_401_reports_invalid_api_key(self):
        api = self.make_client({"error": "unauthorized"}, status=401, body="unauthorized")
        with self.assertRaisesRegex(BrightDataAPIError, "BRIGHTDATA_API_KEY"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_402_reports_exhausted_free_credits_without_retry(self):
        api = self.make_client({"error": "no credits"}, status=402, body="no credits")
        with self.assertRaisesRegex(BrightDataAPIError, "do not add funds"):
            api.search_by_image_url("https://example.com/poster.jpg")
        self.assertEqual(api.client.post.call_count, 1)

    def test_429_reports_request_limit(self):
        api = self.make_client({"error": "rate limit"}, status=429, body="rate limit")
        with self.assertRaisesRegex(BrightDataAPIError, "quota"):
            api.search_by_image_url("https://example.com/poster.jpg")

    def test_transient_502_is_retried_and_parsed(self):
        failed = Mock()
        failed.status_code = 502
        failed.text = "temporary gateway error"
        failed.json.side_effect = ValueError("not JSON")
        recovered = Mock()
        recovered.status_code = 200
        recovered.text = '{"exact_matches":[]}'
        recovered.json.return_value = {"exact_matches": []}
        api = self.make_client()
        api.client.post.side_effect = [failed, recovered]
        with patch("app.google_lens.time.sleep"):
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])
        self.assertEqual(api.client.post.call_count, 2)
        self.assertEqual(api.client.post.call_args.kwargs["json"]["format"], "raw")

    def test_transient_network_error_is_retried(self):
        recovered = Mock()
        recovered.status_code = 200
        recovered.text = '{"exact_matches":[]}'
        recovered.json.return_value = {"exact_matches": []}
        api = self.make_client()
        api.client.post.side_effect = [httpx.ConnectError("temporary failure"), recovered]
        with patch("app.google_lens.time.sleep"):
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])
        self.assertEqual(api.client.post.call_count, 2)

    def test_retries_inner_502_envelope_for_lens(self):
        failed = Mock()
        failed.status_code = 200
        failed.text = '{"status_code":502,"body":"upstream gateway"}'
        failed.json.return_value = {"status_code": 502, "body": "upstream gateway"}
        recovered = Mock()
        recovered.status_code = 200
        recovered.text = '{"exact_matches":[]}'
        recovered.json.return_value = {"exact_matches": []}
        api = self.make_client()
        api.client.post.side_effect = [failed, recovered]

        with patch("app.google_lens.time.sleep"):
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])

        self.assertEqual(api.client.post.call_count, 2)
        self.assertEqual(api.last_lens_diagnostics["attempts"], 2)
        self.assertEqual(api.last_lens_diagnostics["status"], "no_matches")

    def test_retries_remote_protocol_errors(self):
        recovered = Mock()
        recovered.status_code = 200
        recovered.text = '{"exact_matches":[]}'
        recovered.json.return_value = {"exact_matches": []}
        api = self.make_client()
        api.client.post.side_effect = [
            httpx.RemoteProtocolError("peer closed connection"),
            recovered,
        ]

        with patch("app.google_lens.time.sleep"):
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])
        self.assertEqual(api.client.post.call_count, 2)

    def test_non_json_response_is_not_mistaken_for_no_matches(self):
        response = Mock()
        response.status_code = 200
        response.text = "<html>challenge</html>"
        response.json.side_effect = ValueError("not json")
        api = self.make_client()
        api.client.post.return_value = response
        with self.assertRaisesRegex(BrightDataAPIError, "non-JSON response"):
            api.search_by_image_url("https://example.com/poster.jpg")


    def test_resolves_google_goto_using_location_without_following_target(self):
        api = self.make_client({"exact_matches": []})
        api.redirect_client = Mock()
        response = Mock()
        response.status_code = 302
        response.headers = {
            "location": "https://www.imdb.com/title/tt0095468/"
        }
        api.redirect_client.get.return_value = response

        goto_url = "https://lens.google.com/goto?url=CAESopaque-token"
        rows = api.resolve_result_urls([{
            "title": "Any unrelated caption",
            "url": goto_url,
        }])

        self.assertEqual(
            rows[0]["resolved_url"],
            "https://www.imdb.com/title/tt0095468/",
        )
        api.redirect_client.get.assert_called_once_with(goto_url)

    def test_direct_official_links_do_not_trigger_google_redirect_requests(self):
        api = self.make_client({"exact_matches": []})
        api.redirect_client = Mock()
        direct_url = "https://www.themoviedb.org/movie/10754-a-short-film-about-killing"

        rows = api.resolve_result_urls([{"url": direct_url}])

        self.assertEqual(rows[0]["resolved_url"], direct_url)
        self.assertEqual(rows[0]["resolved_urls"], [direct_url])
        api.redirect_client.get.assert_not_called()

    def test_uses_followed_destination_when_location_header_is_missing(self):
        api = self.make_client({"exact_matches": []})
        api.redirect_client = Mock()
        first = Mock()
        first.status_code = 404
        first.headers = {}
        followed = Mock()
        followed.url = "https://www.imdb.com/title/tt0095468/"
        api.redirect_client.get.side_effect = [first, followed]

        rows = api.resolve_result_urls([{
            "url": "https://lens.google.com/goto?url=CAESopaque",
        }])

        self.assertEqual(rows[0]["resolved_url"], "https://www.imdb.com/title/tt0095468/")
        self.assertEqual(api.redirect_client.get.call_count, 2)
        self.assertEqual(
            api.redirect_client.get.call_args_list[1].kwargs,
            {"follow_redirects": True},
        )

if __name__ == "__main__":
    unittest.main()
