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

    def test_each_image_search_resets_previous_official_title_search_diagnostics(self):
        api = self.make_client({"exact_matches": []})
        api.last_official_search_diagnostics = {
            "status": "ok",
            "query_count": 2,
            "title_hints": ["Biz Size Döneriz"],
            "candidate_domains": {"themoviedb.org": 2},
        }

        api.search_by_image_url("https://example.com/breathless-poster.jpg")

        self.assertEqual(api.last_official_search_diagnostics["status"], "not_run")
        self.assertEqual(api.last_official_search_diagnostics["query_count"], 0)
        self.assertEqual(api.last_official_search_diagnostics["title_hints"], [])
        self.assertEqual(api.last_official_search_diagnostics["candidate_domains"], {})

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
        self.assertNotIn("data_format", call.kwargs["json"])
        self.assertEqual(call.kwargs["headers"]["x-unblock-data-format"], "parsed_light")
        self.assertEqual(api.last_lens_diagnostics["data_format"], "parsed_light")
        target = call.kwargs["json"]["url"]
        self.assertIn("lens.google.com/uploadbyurl?", target)
        self.assertIn("brd_json=1", target)
        self.assertIn("brd_lens=exact_matches", target)
        self.assertIn("raw.githubusercontent.com", target)

    def test_captures_raw_result_fields_and_redacted_link_paths(self):
        api = self.make_client({
            "exact_matches": [{
                "title": "A Short Film About Killing",
                "link": "https://lens.google.com/goto?url=CAES_PRIVATE_TOKEN",
                "source": "Play Store",
                "thumbnail": "https://images.example.test/poster.jpg",
            }]
        })

        api.search_by_image_url("https://example.com/poster.jpg")

        diagnostics = api.last_lens_diagnostics
        self.assertIn("link", diagnostics["raw_result_fields"])
        self.assertIn("source", diagnostics["raw_result_fields"])
        self.assertIn("lens.google.com/goto", diagnostics["raw_link_paths"])
        self.assertNotIn("CAES_PRIVATE_TOKEN", repr(diagnostics))

    def test_lens_image_search_uses_documented_parameters_only(self):
        api = self.make_client({"exact_matches": []})

        rows = api.search_by_image_url("https://example.com/poster.jpg")

        self.assertEqual(rows, [])
        self.assertEqual(api.client.post.call_count, 2)
        from urllib.parse import parse_qs, urlparse
        exact_query = parse_qs(urlparse(
            api.client.post.call_args_list[0].kwargs["json"]["url"]
        ).query)
        visual_query = parse_qs(urlparse(
            api.client.post.call_args_list[1].kwargs["json"]["url"]
        ).query)
        self.assertEqual(exact_query["url"], ["https://example.com/poster.jpg"])
        self.assertEqual(exact_query["brd_lens"], ["exact_matches"])
        self.assertEqual(visual_query["brd_lens"], ["visual_matches"])
        self.assertEqual(exact_query["hl"], ["en"])
        self.assertEqual(
            api.last_lens_diagnostics["lens_tabs_attempted"],
            ["exact_matches", "visual_matches"],
        )
        self.assertNotIn("q", exact_query)
        self.assertNotIn("query", exact_query)

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
        api.client.post.side_effect = [empty, recovered, recovered]
        with patch("app.google_lens.time.sleep"):
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])

        self.assertEqual(api.client.post.call_count, 3)
        self.assertEqual(api.last_lens_diagnostics["attempts"], 3)
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

    def test_empty_exact_matches_returns_empty_list_after_visual_fallback(self):
        api = self.make_client({"exact_matches": []})
        self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])
        self.assertEqual(api.client.post.call_count, 2)
        self.assertEqual(
            api.last_lens_diagnostics["lens_tabs_attempted"],
            ["exact_matches", "visual_matches"],
        )

    def test_prefers_populated_visual_matches_over_empty_exact_matches(self):
        rows = BrightDataGoogleLensClient._rows_from_payload({
            "exact_matches": [],
            "visual_matches": [{
                "title": "A Short Film About Killing",
                "link": "https://www.imdb.com/title/tt0095468/",
            }],
        })
        self.assertEqual(len(rows or []), 1)
        self.assertEqual(rows[0]["title"], "A Short Film About Killing")

    def test_supports_nested_generic_results_wrapper(self):
        rows = BrightDataGoogleLensClient._rows_from_payload({
            "results": {
                "organic": [{
                    "title": "A Short Film About Killing",
                    "link": "https://www.imdb.com/title/tt0095468/",
                }]
            }
        })
        self.assertEqual(len(rows or []), 1)
        self.assertEqual(
            rows[0]["link"] if rows else None,
            "https://www.imdb.com/title/tt0095468/",
        )

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

    def test_429_without_retry_after_or_cooldown_is_not_retried(self):
        api = self.make_client({"error": "rate limit"}, status=429, body="rate limit")
        with self.assertRaisesRegex(BrightDataAPIError, "HTTP 429"):
            api.search_by_image_url("https://example.com/poster.jpg")
        self.assertEqual(api.client.post.call_count, 1)

    def test_retries_explicit_brightdata_same_query_cooldown(self):
        cooldown = Mock()
        cooldown.status_code = 429
        cooldown.text = (
            "This query recently failed and cannot be attempted at this time. "
            "Please try again later, after a minimum of 15 seconds."
        )
        cooldown.json.return_value = {"error": cooldown.text}
        cooldown.headers = {}

        recovered = Mock()
        recovered.status_code = 200
        recovered.text = '{"exact_matches":[{"title":"A Short Film About Killing","link":"https://www.imdb.com/title/tt0095468/"}]}'
        recovered.json.return_value = {
            "exact_matches": [{
                "title": "A Short Film About Killing",
                "link": "https://www.imdb.com/title/tt0095468/",
            }]
        }
        api = self.make_client()
        api.client.post.side_effect = [cooldown, recovered]

        with patch("app.google_lens.time.sleep") as sleep:
            rows = api.search_by_image_url("https://example.com/poster.jpg")

        self.assertEqual(len(rows), 1)
        self.assertEqual(api.client.post.call_count, 2)
        sleep.assert_called_once_with(15)
        self.assertEqual(api.last_lens_diagnostics["rate_limit_retries"], 1)
        self.assertEqual(api.last_lens_diagnostics["rate_limit_wait_seconds"], 15)
        self.assertEqual(api.last_lens_diagnostics["attempts"], 2)

    def test_retries_rate_limit_from_retry_after_header(self):
        throttled = Mock()
        throttled.status_code = 429
        throttled.text = "Too Many Requests"
        throttled.json.return_value = {"error": "temporary throttling"}
        throttled.headers = {"retry-after": "7"}

        recovered = Mock()
        recovered.status_code = 200
        recovered.text = '{"exact_matches":[]}'
        recovered.json.return_value = {"exact_matches": []}

        api = self.make_client()
        api.client.post.side_effect = [throttled, recovered, recovered]
        with patch("app.google_lens.time.sleep") as sleep:
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])

        sleep.assert_called_once_with(7)
        self.assertEqual(api.last_lens_diagnostics["rate_limit_retries"], 1)
        self.assertEqual(api.last_lens_diagnostics["rate_limit_wait_seconds"], 7)

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
        api.client.post.side_effect = [failed, recovered, recovered]
        with patch("app.google_lens.time.sleep"):
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])
        self.assertEqual(api.client.post.call_count, 3)
        self.assertEqual(api.client.post.call_args.kwargs["json"]["format"], "raw")

    def test_transient_network_error_is_retried(self):
        recovered = Mock()
        recovered.status_code = 200
        recovered.text = '{"exact_matches":[]}'
        recovered.json.return_value = {"exact_matches": []}
        api = self.make_client()
        api.client.post.side_effect = [
            httpx.ConnectError("temporary failure"),
            recovered,
            recovered,
        ]
        with patch("app.google_lens.time.sleep"):
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])
        self.assertEqual(api.client.post.call_count, 3)

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
        api.client.post.side_effect = [failed, recovered, recovered]

        with patch("app.google_lens.time.sleep"):
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])

        self.assertEqual(api.client.post.call_count, 3)
        self.assertEqual(api.last_lens_diagnostics["attempts"], 3)
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
            recovered,
        ]

        with patch("app.google_lens.time.sleep"):
            self.assertEqual(api.search_by_image_url("https://example.com/poster.jpg"), [])
        self.assertEqual(api.client.post.call_count, 3)

    def test_non_json_response_is_not_mistaken_for_no_matches(self):
        response = Mock()
        response.status_code = 200
        response.text = "<html>challenge</html>"
        response.json.side_effect = ValueError("not json")
        api = self.make_client()
        api.client.post.return_value = response
        with self.assertRaisesRegex(BrightDataAPIError, "non-JSON response"):
            api.search_by_image_url("https://example.com/poster.jpg")


    def test_google_lens_goto_is_quarantined_without_external_http_requests(self):
        api = self.make_client({"exact_matches": []})
        goto_url = "https://lens.google.com/goto?url=CAES_PRIVATE_TOKEN"

        rows = api.resolve_result_urls([{
            "title": "Any unrelated caption",
            "url": goto_url,
        }])

        self.assertEqual(rows[0]["resolved_urls"], [])
        self.assertEqual(rows[0]["resolved_url"], "")
        self.assertEqual(
            rows[0]["url_resolution_status"],
            "google_goto_skipped_no_documented_destination",
        )
        self.assertEqual(api.last_lens_diagnostics["google_goto_candidates"], 1)
        self.assertEqual(api.last_lens_diagnostics["google_goto_skipped"], 1)
        self.assertEqual(api.last_lens_diagnostics["google_goto_resolved"], 0)
        self.assertEqual(api.last_lens_diagnostics["google_goto_failed"], 0)
        self.assertEqual(api.last_lens_diagnostics["google_goto_http_statuses"], {})
        # No redirect_client exists: the resolver no longer calls Google /goto.
        self.assertFalse(hasattr(api, "redirect_client"))

    def test_visual_matches_is_skipped_when_exact_matches_has_usable_title(self):
        exact = Mock()
        exact.status_code = 200
        exact.text = ""
        exact.json.return_value = {
            "exact_matches": [{
                "title": "A Short Film About Killing (1988)",
                "link": "https://lens.google.com/goto?url=CAES_PRIVATE_TOKEN",
                "image_url": "https://images.example.test/exact-poster.jpg",
            }]
        }
        api = self.make_client()
        api.client.post.side_effect = [exact]

        rows = api.search_by_image_url("https://example.com/poster.jpg")

        self.assertEqual(api.client.post.call_count, 1)
        self.assertEqual(api.last_lens_diagnostics["lens_tabs_attempted"], ["exact_matches"])
        self.assertEqual(api.last_lens_diagnostics["visual_matches_result_count"], 0)
        self.assertEqual(
            api.last_lens_diagnostics["visual_fallback_skipped_reason"],
            "exact_matches_has_usable_title_hint",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "A Short Film About Killing (1988)")

    def test_visual_matches_fallback_runs_when_exact_matches_has_no_usable_title(self):
        exact = Mock()
        exact.status_code = 200
        exact.text = ""
        exact.json.return_value = {
            "exact_matches": [{
                "title": "Movies",
                "link": "https://lens.google.com/goto?url=CAES_PRIVATE_TOKEN",
                "image_url": "https://images.example.test/exact-poster.jpg",
            }]
        }
        visual = Mock()
        visual.status_code = 200
        visual.text = ""
        visual.json.return_value = {
            "visual_matches": [{
                "title": "A Short Film About Killing (1988)",
                "link": "https://www.imdb.com/title/tt0095468/",
                "image": "https://images.example.test/visual-poster.jpg",
            }]
        }
        api = self.make_client()
        api.client.post.side_effect = [exact, visual]

        rows = api.search_by_image_url("https://example.com/poster.jpg")

        self.assertIn(
            "https://www.imdb.com/title/tt0095468/",
            [row["url"] for row in rows],
        )
        self.assertEqual(api.client.post.call_count, 2)
        self.assertEqual(
            [call.kwargs["json"]["url"].split("brd_lens=")[-1] for call in api.client.post.call_args_list],
            ["exact_matches", "visual_matches"],
        )
        self.assertEqual(api.last_lens_diagnostics["exact_matches_result_count"], 1)
        self.assertEqual(api.last_lens_diagnostics["visual_matches_result_count"], 1)
        self.assertEqual(api.last_lens_diagnostics["status"], "ok")

    def test_direct_official_links_are_kept_while_google_goto_is_skipped(self):
        api = self.make_client({"exact_matches": []})
        official_url = "https://www.imdb.com/title/tt0095468/"
        goto_url = "https://lens.google.com/goto?url=CAES_PRIVATE_TOKEN"

        rows = api.resolve_result_urls([{
            "url": goto_url,
            "url_candidates": [goto_url, official_url],
        }])

        self.assertEqual(rows[0]["resolved_url"], official_url)
        self.assertEqual(rows[0]["resolved_urls"], [official_url])
        self.assertEqual(rows[0]["url_resolution_status"], "direct_urls_kept_goto_skipped")
        self.assertEqual(api.last_lens_diagnostics["google_goto_skipped"], 1)
        self.assertEqual(api.last_lens_diagnostics["google_goto_direct_urls_kept"], 1)

    def test_direct_official_tmdb_link_is_kept_without_google_requests(self):
        api = self.make_client({"exact_matches": []})
        direct_url = "https://www.themoviedb.org/movie/10754-a-short-film-about-killing"

        rows = api.resolve_result_urls([{"url": direct_url}])

        self.assertEqual(rows[0]["resolved_url"], direct_url)
        self.assertEqual(rows[0]["resolved_urls"], [direct_url])
        self.assertEqual(api.last_lens_diagnostics["google_goto_skipped"], 0)

    def test_normalizes_source_page_url_when_source_is_a_publisher_link(self):
        api = self.make_client({
            "exact_matches": [{
                "title": "A Short Film About Killing",
                "source": "https://www.themoviedb.org/movie/10754",
                "image": "https://images.example.test/poster.jpg",
            }]
        })
        result = api.search_by_image_url("https://example.com/poster.jpg")
        self.assertEqual(result[0]["url"], "https://www.themoviedb.org/movie/10754")
        self.assertIn("https://www.themoviedb.org/movie/10754", result[0]["url_candidates"])
        self.assertEqual(result[0]["source"], "https://images.example.test/poster.jpg")

    def test_normalizes_href_as_a_source_page_url(self):
        api = self.make_client({
            "exact_matches": [{
                "title": "A Short Film About Killing",
                "link": {"href": "https://www.themoviedb.org/movie/10754"},
                "image": "https://images.example.test/poster.jpg",
            }]
        })
        result = api.search_by_image_url("https://example.com/poster.jpg")
        self.assertEqual(result[0]["url"], "https://www.themoviedb.org/movie/10754")

    def test_title_hints_come_only_from_lens_titles_and_direct_play_urls(self):
        hints, year = BrightDataGoogleLensClient.title_hints_from_lens_results([
            {
                "title": "A Short Film About Killing (1988)",
                "url": "https://play.google.com/store/movies/details/a_short_film_about_killing",
                "url_candidates": [
                    "https://play.google.com/store/movies/details/a_short_film_about_killing",
                    "https://lens.google.com/goto?url=PRIVATE",
                ],
            },
            {
                "url": "https://play.google.com/store/movies/details/kr%C3%B3tki_film_o_zabijaniu",
            },
            {
                "url": "https://play.google.com/store/movies/details/%C3%96ld%C3%BCrme_%C3%9Czerine_K%C4%B1sa_Bir_Film",
            },
        ])

        self.assertEqual(year, 1988)
        # A distinct direct Play Store title is prioritized as a useful alias.
        self.assertEqual(hints[0].casefold(), "krótki film o zabijaniu")
        self.assertIn("a short film about killing", [value.casefold() for value in hints])
        self.assertIn("krótki film o zabijaniu", [value.casefold() for value in hints])
        self.assertIn("öldürme üzerine kısa bir film", [value.casefold() for value in hints])
        self.assertNotIn("PRIVATE", " ".join(hints))

    def test_late_play_store_title_is_not_lost_after_early_lens_hints(self):
        lens_rows = [
            {"title": "Unrelated poster caption number one"},
            {"title": "Unrelated poster caption number two"},
            {"title": "Unrelated poster caption number three"},
            {"title": "Unrelated poster caption number four"},
            {"title": "Google Play"},
            {
                "title": "Google Play",
                "url": (
                    "https://play.google.com/store/movies/details/"
                    "Apollo_10_e_meio_aventura_na_era_espacial"
                ),
            },
        ]

        hints, year = BrightDataGoogleLensClient.title_hints_from_lens_results(lens_rows)

        self.assertEqual(hints[0], "Apollo 10 e meio aventura na era espacial")
        self.assertIn("Unrelated poster caption number one", hints)
        self.assertIsNone(year)

    def test_repeated_lens_title_ranks_above_conflicting_play_store_aliases(self):
        hints, year = BrightDataGoogleLensClient.title_hints_from_lens_results([
            {"title": "Unrelated poster blog headline about Polish films"},
            {"title": "A Short Film About Killing (1988) - IMDb"},
            {"title": "A Short Film About Killing on Cinobo | Watch the film online"},
            {"title": "A Short Film About Killing | The Disapproving Swede"},
            {
                "url": "https://play.google.com/store/movies/details/kr%C3%B3tki_film_o_zabijaniu"
            },
        ])

        self.assertEqual(hints[0], "A Short Film About Killing")
        self.assertEqual(year, 1988)
        self.assertIn("krótki film o zabijaniu", [value.casefold() for value in hints])

    def test_title_hint_year_is_not_guessed_from_play_store_slug(self):
        hints, year = BrightDataGoogleLensClient.title_hints_from_lens_results([
            {
                "url": "https://play.google.com/store/movies/details/a_short_film_about_killing_1988",
            }
        ])

        self.assertIsNone(year)
        self.assertEqual(hints, ["a short film about killing"])

    def test_official_title_search_uses_only_the_primary_lens_title(self):
        api = self.make_client({"organic": []})
        lens_rows = [
            {"title": "A Short Film About Killing (1988) - IMDb"},
            {"title": "A Short Film About Killing (1988) - The Movie Database"},
            {"title": "A Short Film About Killing on Cinobo | Watch the film online"},
            {"title": "Most Adum would love this film Adum hasn't seen r YMS"},
            {"title": "Amazon com A Short Film About Killing DVD Documentary, Horror"},
            {"title": "YouTube"},
            {"title": "A Short Film About Killing Alternative Film Movie Print Wall"},
        ]

        api.search_official_movie_pages_from_lens(lens_rows)

        from urllib.parse import parse_qs, urlparse
        query = parse_qs(
            urlparse(api.client.post.call_args_list[0].kwargs["json"]["url"]).query
        )["q"][0]
        self.assertIn('"A Short Film About Killing" 1988', query)
        self.assertNotIn("Adum", query)
        self.assertNotIn("Amazon", query)
        self.assertNotIn("YouTube", query)
        self.assertNotIn("Movie Print", query)
        self.assertEqual(
            api.last_official_search_diagnostics["title_hints"],
            ["A Short Film About Killing"],
        )
        self.assertGreaterEqual(
            len(api.last_official_search_diagnostics["candidate_title_hints"]), 2
        )
        self.assertEqual(api.last_official_search_diagnostics["query_count"], 2)

    def test_searches_only_official_tmdb_and_imdb_pages_from_lens_titles(self):
        api = self.make_client({
            "organic": [
                {
                    "rank": 1,
                    "title": "A Short Film About Killing (1988) — The Movie Database",
                    "link": "https://www.themoviedb.org/movie/10754-a-short-film-about-killing",
                    "description": "Crime, drama",
                },
                {
                    "rank": 2,
                    "title": "A Short Film About Killing (1988) - IMDb",
                    "link": "https://www.imdb.com/title/tt0095468/",
                    "description": "Official IMDb title page",
                },
                {
                    "rank": 3,
                    "title": "A Short Film About Killing - Wikipedia",
                    "link": "https://en.wikipedia.org/wiki/A_Short_Film_About_Killing",
                },
            ]
        })
        lens_rows = [{
            "title": "A Short Film About Killing (1988)",
            "url": "https://play.google.com/store/movies/details/a_short_film_about_killing",
        }]

        rows = api.search_official_movie_pages_from_lens(lens_rows)

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["url"] for row in rows},
            {
                "https://www.themoviedb.org/movie/10754-a-short-film-about-killing",
                "https://www.imdb.com/title/tt0095468/",
            },
        )
        call = api.client.post.call_args
        self.assertEqual(call.args[0], BRIGHTDATA_REQUEST_URL)
        self.assertIsNone(call.kwargs["headers"])
        self.assertEqual(call.kwargs["json"]["format"], "json")
        self.assertNotIn("data_format", call.kwargs["json"])
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(call.kwargs["json"]["url"]).query)
        self.assertEqual(query["brd_json"], ["1"])
        self.assertEqual(query["hl"], ["en"])
        self.assertEqual(
            api.last_official_search_diagnostics["response_format"], "json"
        )
        self.assertIn('(site:imdb.com/title "A Short Film About Killing" 1988)', query["q"][0])
        self.assertIn('(site:themoviedb.org/movie "A Short Film About Killing" 1988)', query["q"][0])
        self.assertIn("1988", query["q"][0])
        self.assertEqual(api.last_official_search_diagnostics["status"], "ok")
        self.assertEqual(api.last_official_search_diagnostics["query_count"], 1)
        self.assertEqual(api.last_official_search_diagnostics["query_variants"], ["exact_site_scoped"])
        self.assertEqual(api.last_official_search_diagnostics["official_url_count"], 2)
        self.assertEqual(
            api.last_official_search_diagnostics["title_hints"],
            ["A Short Film About Killing"],
        )

    def test_title_search_uses_broader_site_scoped_query_only_when_exact_query_has_no_official_page(self):
        first = Mock()
        first.status_code = 200
        first.text = ""
        first.json.return_value = {
            "organic": [{
                "title": "Untrusted publisher result",
                "link": "https://example.com/review/a-short-film",
            }]
        }
        second = Mock()
        second.status_code = 200
        second.text = ""
        second.json.return_value = {
            "organic": [{
                "title": "A Short Film About Killing (1988) - IMDb",
                "link": "https://www.imdb.com/title/tt0095468/",
            }]
        }
        api = self.make_client()
        api.client.post.side_effect = [first, second]

        rows = api.search_official_movie_pages_from_lens([
            {"title": "A Short Film About Killing (1988)"}
        ])

        self.assertEqual([row["url"] for row in rows], [
            "https://www.imdb.com/title/tt0095468/"
        ])
        self.assertEqual(rows[0]["search_query_variant"], "broad_site_scoped")
        self.assertEqual(api.client.post.call_count, 2)
        self.assertEqual(api.last_official_search_diagnostics["query_count"], 2)
        self.assertEqual(
            api.last_official_search_diagnostics["query_variants"],
            ["exact_site_scoped", "broad_site_scoped"],
        )
        from urllib.parse import parse_qs, urlparse
        broad_query = parse_qs(
            urlparse(api.client.post.call_args_list[1].kwargs["json"]["url"]).query
        )["q"][0]
        self.assertIn("(site:imdb.com/title (A Short Film About Killing))", broad_query)
        self.assertIn("(site:themoviedb.org/movie (A Short Film About Killing))", broad_query)
        self.assertNotIn("1988", broad_query)

    def test_title_search_parses_documented_brightdata_body_organic_shape(self):
        body = {
            "general": {
                "search_engine": "google",
                "query": '"A Short Film About Killing" site:imdb.com/title',
            },
            "organic": [{
                "link": "https://www.imdb.com/title/tt0095468/",
                "title": "A Short Film About Killing (1988) - IMDb",
                "rank": 1,
            }],
        }
        api = self.make_client({
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(body),
        })

        rows = api.search_official_movie_pages_from_lens([
            {"title": "A Short Film About Killing (1988)"}
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://www.imdb.com/title/tt0095468/")
        self.assertEqual(api.last_official_search_diagnostics["status"], "ok")
        self.assertEqual(api.last_official_search_diagnostics["organic_result_count"], 1)
        call = api.client.post.call_args
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(call.kwargs["json"]["url"]).query)
        self.assertEqual(call.kwargs["json"]["format"], "json")
        self.assertEqual(query["brd_json"], ["1"])

    def test_official_title_search_retries_explicit_brightdata_cooldown(self):
        cooldown = Mock()
        cooldown.status_code = 429
        cooldown.text = (
            "This query recently failed and cannot be attempted at this time. "
            "Please try again later, after a minimum of 15 seconds."
        )
        cooldown.json.return_value = {"error": cooldown.text}
        cooldown.headers = {}

        recovered = Mock()
        recovered.status_code = 200
        recovered.text = ""
        recovered.json.return_value = {
            "organic": [{
                "title": "A Short Film About Killing (1988) - IMDb",
                "link": "https://www.imdb.com/title/tt0095468/",
            }]
        }
        api = self.make_client()
        api.client.post.side_effect = [cooldown, recovered]

        with patch("app.google_lens.time.sleep") as sleep:
            rows = api.search_official_movie_pages_from_lens([
                {"title": "A Short Film About Killing (1988)"}
            ])

        self.assertEqual([row["url"] for row in rows], [
            "https://www.imdb.com/title/tt0095468/"
        ])
        self.assertEqual(api.client.post.call_count, 2)
        sleep.assert_called_once_with(15)
        diagnostics = api.last_official_search_diagnostics
        self.assertEqual(diagnostics["query_count"], 1)
        self.assertEqual(diagnostics["attempts"], 2)
        self.assertEqual(diagnostics["rate_limit_retries"], 1)
        self.assertEqual(diagnostics["rate_limit_wait_seconds"], 15)

    def test_title_search_parses_structured_json_inside_brightdata_body_envelope(self):
        organic = [{
            "title": "Apollo 10½: A Space Age Childhood - IMDb",
            "link": "https://www.imdb.com/title/tt7978758/",
        }]
        envelope = {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"Data": {"Organic_Results": organic}}),
        }
        api = self.make_client(envelope)

        rows = api.search_official_movie_pages_from_lens([
            {"title": "Apollo 10½ A Space Age Childhood (2022)"}
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://www.imdb.com/title/tt7978758/")
        self.assertEqual(api.last_official_search_diagnostics["status"], "ok")
        self.assertEqual(api.last_official_search_diagnostics["organic_result_count"], 1)
        self.assertEqual(api.last_official_search_diagnostics["response_format"], "json")
        self.assertIn("status_code=int", api.last_official_search_diagnostics["payload_shape"])

    def test_title_search_falls_back_to_json_text_when_response_json_parser_fails(self):
        response = Mock()
        response.status_code = 200
        response.text = json.dumps({
            "organic": [{
                "title": "A Short Film About Killing - IMDb",
                "link": "https://www.imdb.com/title/tt0095468/",
            }]
        })
        response.json.side_effect = ValueError("provider returned JSON as text")
        response.headers = {"content-type": "application/json"}

        api = self.make_client()
        api.client.post.return_value = response

        rows = api.search_official_movie_pages_from_lens([
            {"title": "A Short Film About Killing (1988)"}
        ])

        self.assertEqual([row["url"] for row in rows], [
            "https://www.imdb.com/title/tt0095468/"
        ])
        self.assertEqual(api.last_official_search_diagnostics["status"], "ok")

    def test_title_search_supports_renamed_result_collection_without_trusting_metadata_arrays(self):
        api = self.make_client({
            "provider_payload": {
                "items_v2": [{
                    "title": "Breathless - TMDB",
                    "destination_url": "https://www.imdb.com/title/tt0053472/",
                }]
            },
            "metadata": [{
                "title": "Not a result",
                "source": "https://www.imdb.com/title/tt0000001/",
            }],
        })
        rows = api.search_official_movie_pages_from_lens([
            {"title": "Breathless (1960)"}
        ])

        # Unknown provider collection names are supported, but identity still
        # requires an official IMDb/TMDB movie URL.
        self.assertEqual([row["url"] for row in rows], [
            "https://www.imdb.com/title/tt0053472/"
        ])
        self.assertEqual(api.last_official_search_diagnostics["status"], "ok")

    def test_title_search_diagnostics_report_unrecognized_structured_payload_shape(self):
        api = self.make_client({
            "general": {"query": "redacted"},
            "provider_metadata": {"version": 2},
        })

        with self.assertRaisesRegex(
            BrightDataAPIError, "without a recognized organic result list"
        ):
            api.search_official_movie_pages_from_lens([
                {"title": "Breathless (1960)"}
            ])

        diagnostics = api.last_official_search_diagnostics
        self.assertEqual(diagnostics["status"], "unparsed_response")
        self.assertEqual(diagnostics["response_format"], "json")
        self.assertIn("general=object[query]", diagnostics["payload_shape"])

    def test_official_title_search_does_not_return_untrusted_domains(self):
        api = self.make_client({
            "organic": [
                {
                    "title": "Wrong site",
                    "link": "https://example.com/movie/10754",
                },
                {
                    "title": "Unrelated TMDB route",
                    "link": "https://www.themoviedb.org/tv/10754",
                },
            ]
        })
        rows = api.search_official_movie_pages_from_lens([
            {"title": "A Short Film About Killing (1988)"}
        ])
        self.assertEqual(rows, [])
        self.assertEqual(api.last_official_search_diagnostics["status"], "no_official_results")

    def test_no_lens_title_hints_means_no_google_search_request(self):
        api = self.make_client({"organic": []})
        rows = api.search_official_movie_pages_from_lens([
            {"url": "https://lens.google.com/goto?url=opaque"},
            {"title": "Google Play"},
        ])
        self.assertEqual(rows, [])
        api.client.post.assert_not_called()
        self.assertEqual(api.last_official_search_diagnostics["status"], "no_title_hints")


    def test_unwraps_google_serp_redirects_before_official_domain_allowlist(self):
        api = self.make_client({
            "organic": [
                {
                    "rank": 1,
                    "title": "A Short Film About Killing (1988) — TMDB",
                    "link": (
                        "https://www.google.com/url?q="
                        "https%3A%2F%2Fwww.themoviedb.org%2Fmovie%2F10754-a-short-film-about-killing"
                        "&sa=U"
                    ),
                },
                {
                    "rank": 2,
                    "title": "A Short Film About Killing (1988) - IMDb",
                    "link": (
                        "https://www.google.com/url?url="
                        "https%3A%2F%2Fwww.imdb.com%2Ftitle%2Ftt0095468%2F"
                    ),
                },
                {
                    "rank": 3,
                    "title": "Untrusted result",
                    "link": (
                        "https://www.google.com/url?q="
                        "https%3A%2F%2Fexample.com%2Fmovie%2F10754"
                    ),
                },
            ]
        })

        rows = api.search_official_movie_pages_from_lens([
            {"title": "A Short Film About Killing (1988)"}
        ])

        self.assertEqual(
            {row["url"] for row in rows},
            {
                "https://www.themoviedb.org/movie/10754-a-short-film-about-killing",
                "https://www.imdb.com/title/tt0095468/",
            },
        )
        self.assertEqual(api.last_official_search_diagnostics["organic_result_count"], 3)
        self.assertEqual(api.last_official_search_diagnostics["candidate_url_count"], 3)
        self.assertEqual(api.last_official_search_diagnostics["google_redirect_count"], 3)
        self.assertEqual(api.last_official_search_diagnostics["google_redirect_unwrapped_count"], 3)
        self.assertEqual(api.last_official_search_diagnostics["official_url_count"], 2)
        self.assertEqual(api.last_official_search_diagnostics["status"], "ok")

    def test_google_redirect_to_non_movie_tmdb_page_is_rejected(self):
        api = self.make_client({
            "organic": [
                {
                    "title": "Wrong TMDB media type",
                    "link": (
                        "https://www.google.com/url?q="
                        "https%3A%2F%2Fwww.themoviedb.org%2Ftv%2F10754"
                    ),
                },
                {
                    "title": "Untrusted external site",
                    "link": "https://example.com/movie/10754",
                },
            ]
        })

        rows = api.search_official_movie_pages_from_lens([
            {"title": "A Short Film About Killing (1988)"}
        ])

        self.assertEqual(rows, [])
        self.assertEqual(api.last_official_search_diagnostics["status"], "no_official_results")
        # Both bounded title-search passes see the same Google tracking result.
        self.assertEqual(api.last_official_search_diagnostics["google_redirect_unwrapped_count"], 2)
        self.assertEqual(api.last_official_search_diagnostics["query_count"], 2)


    def test_trusted_film_archives_are_saved_as_reviewer_sources_only(self):
        afi_url = "https://aficatalog.afi.com/Film/2269"
        bfi_url = "https://collections-search.bfi.org.uk/web/Details/ChoiceFilmWorks/150010110"
        self.assertTrue(BrightDataGoogleLensClient._official_movie_url(afi_url))
        self.assertTrue(BrightDataGoogleLensClient._official_movie_url(bfi_url))
        self.assertFalse(BrightDataGoogleLensClient._official_movie_url("https://example.com/Film/2269"))

        api = self.make_client({
            "organic": [
                {"title": "AFI film record", "link": afi_url, "description": "Film record"},
                {"title": "BFI archive record", "link": bfi_url, "description": "Archive record"},
            ]
        })
        rows = api.search_official_movie_pages_from_lens([
            {"title": "Example American film"}
        ])

        self.assertEqual({row["catalog_source"] for row in rows}, {
            "afi_catalog",
            "bfi_national_archive",
        })
        self.assertTrue(all(row["identity_evidence"] is False for row in rows))
        self.assertTrue(all(row["automatic_match_allowed"] is False for row in rows))
        self.assertEqual(api.last_official_search_diagnostics["catalog_source_count"], 2)
        # Archive references alone do not suppress the broad pass for official IDs.
        self.assertEqual(api.last_official_search_diagnostics["query_count"], 2)


if __name__ == "__main__":
    unittest.main()
