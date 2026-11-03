from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from app.engine import MetadataEngine
from app.google_vision import GoogleVisionClient, GoogleVisionQuotaError


POSTER_URL = "https://raw.githubusercontent.com/example/posters/main/poster.jpg"
TMDB_URL = "https://www.themoviedb.org/movie/10754-a-short-film-about-killing"


class GoogleVisionClientTests(unittest.TestCase):
    def test_only_full_matching_pages_are_returned_as_identity_evidence(self):
        rows = GoogleVisionClient._exact_page_matches({
            "pagesWithMatchingImages": [
                {
                    "pageTitle": "Official film page",
                    "url": TMDB_URL,
                    "fullMatchingImages": [
                        {"url": "https://image.example/poster-full.jpg"}
                    ],
                    "partialMatchingImages": [
                        {"url": "https://image.example/poster-crop.jpg"}
                    ],
                },
                {
                    "pageTitle": "Similar or partial page",
                    "url": "https://www.imdb.com/title/tt0095468/",
                    "partialMatchingImages": [
                        {"url": "https://image.example/poster-partial.jpg"}
                    ],
                },
                {
                    "pageTitle": "Malformed page",
                    "url": "https://[invalid",
                    "fullMatchingImages": [
                        {"url": "https://image.example/bad.jpg"}
                    ],
                },
            ]
        })

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], TMDB_URL)
        self.assertEqual(rows[0]["match_type"], "full_image_match")
        self.assertEqual(rows[0]["provider"], "google_cloud_vision_web_detection")

    def test_analyze_image_url_extracts_full_match_and_ocr_without_using_partial_as_proof(self):
        captured = []

        payload = {
            "responses": [{
                "webDetection": {
                    "pagesWithMatchingImages": [
                        {
                            "pageTitle": "Official TMDB film page",
                            "url": TMDB_URL,
                            "fullMatchingImages": [
                                {"url": "https://image.example/full.jpg"}
                            ],
                        },
                        {
                            "pageTitle": "Partial page",
                            "url": "https://www.imdb.com/title/tt0095468/",
                            "partialMatchingImages": [
                                {"url": "https://image.example/partial.jpg"}
                            ],
                        },
                    ],
                    "fullMatchingImages": [{"url": "https://image.example/full.jpg"}],
                    "partialMatchingImages": [{"url": "https://image.example/partial.jpg"}],
                    "visuallySimilarImages": [{"url": "https://image.example/similar.jpg"}],
                },
                "fullTextAnnotation": {
                    "text": "Buladó\nDirected by Someone\n2023",
                    "pages": [{
                        "property": {
                            "detectedLanguages": [
                                {"languageCode": "nl"},
                                {"languageCode": "en"},
                            ]
                        }
                    }],
                },
            }]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=payload)

        client = GoogleVisionClient(
            "test-api-key",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = client.analyze_image_url(POSTER_URL)
        finally:
            client.close()

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].url.params.get("key"), "test-api-key")
        body = json.loads(captured[0].content)
        feature_types = {feature["type"] for feature in body["requests"][0]["features"]}
        self.assertEqual(feature_types, {"WEB_DETECTION", "TEXT_DETECTION"})

        self.assertEqual(len(result["official_page_matches"]), 1)
        self.assertEqual(result["official_page_matches"][0]["url"], TMDB_URL)
        self.assertEqual(result["ocr_text"], "Buladó\nDirected by Someone\n2023")
        self.assertEqual(result["ocr_title_hints"], ["Buladó"])
        self.assertEqual(result["ocr_language_codes"], ["nl", "en"])
        self.assertEqual(len(result["partial_matching_image_urls"]), 1)
        self.assertEqual(len(result["visually_similar_image_urls"]), 1)

    def test_rejects_non_github_public_image_urls_before_network_request(self):
        client = GoogleVisionClient("test-api-key", transport=httpx.MockTransport(
            lambda request: httpx.Response(500)
        ))
        try:
            with self.assertRaises(ValueError):
                client.analyze_image_url("https://evil.example/poster.jpg")
        finally:
            client.close()

    def test_http_429_raises_a_quota_error_without_retrying(self):
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})

        client = GoogleVisionClient("test-api-key", transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(GoogleVisionQuotaError):
                client.analyze_image_url(POSTER_URL)
            self.assertEqual(len(captured), 1)
            self.assertEqual(client.last_diagnostics["status"], "quota_or_throttle")
        finally:
            client.close()


class GoogleVisionIdentityGateTests(unittest.TestCase):
    @staticmethod
    def make_engine():
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.settings = SimpleNamespace(
            google_vision_enabled=True,
            google_vision_require_confirmation=True,
        )
        engine._details_cache = {}
        engine._stop_batch = False
        engine._provider_stop_status = None
        engine._provider_stop_reason = None
        engine._provider_stop_provider = None
        engine.reverse_image = SimpleNamespace(last_official_search_diagnostics={})
        engine.tmdb = Mock()
        engine.tmdb.movie_details.return_value = {
            "id": 10754,
            "title": "A Short Film About Killing",
            "original_title": "Krótki film o zabijaniu",
            "release_date": "1988-03-11",
            "external_ids": {},
            "production_countries": [],
            "credits": {"cast": [], "crew": []},
            "release_dates": {"results": []},
            "alternative_titles": {"titles": []},
            "keywords": {"keywords": []},
            "images": {"posters": []},
        }
        return engine

    def test_lens_id_without_vision_full_match_stays_review(self):
        engine = self.make_engine()
        lens_rows = [{
            "title": "Lens result",
            "url": TMDB_URL,
            "resolved_urls": [TMDB_URL],
            "resolved_url": TMDB_URL,
        }]

        candidate, match, evidence = engine._find_movie(
            lens_rows,
            google_vision_results=[],
        )

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertTrue(match["vision_confirmation_required"])
        self.assertEqual(match["candidate_ids"], [10754])
        self.assertEqual(engine.tmdb.movie_details.call_count, 1)
        self.assertFalse(evidence["resolved_identity"] if "resolved_identity" in evidence else False)

    def test_vision_full_match_official_tmdb_url_can_be_verified(self):
        engine = self.make_engine()
        vision_rows = [{
            "title": "Full-image match",
            "url": TMDB_URL,
            "resolved_urls": [TMDB_URL],
            "resolved_url": TMDB_URL,
            "provider": "google_cloud_vision_web_detection",
            "match_type": "full_image_match",
        }]

        candidate, match, evidence = engine._find_movie(
            [],
            google_vision_results=vision_rows,
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["identity_source"], "google_cloud_vision_full_match_tmdb_page")
        self.assertEqual(evidence["resolved_identity"]["tmdb_id"], 10754)
        self.assertEqual(evidence["resolved_identity"]["google_vision_tmdb_ids"], [10754])
        self.assertFalse(evidence["resolved_identity"]["folder_name_used"])
        self.assertFalse(evidence["resolved_identity"]["image_filename_used"])

    def test_partial_match_cannot_supply_an_identity_even_with_an_official_url(self):
        engine = self.make_engine()
        partial_rows = [{
            "title": "Partial match",
            "url": TMDB_URL,
            "resolved_urls": [TMDB_URL],
            "provider": "google_cloud_vision_web_detection",
            "match_type": "partial_image_match",
        }]

        candidate, match, evidence = engine._find_movie(
            [],
            google_vision_results=partial_rows,
        )

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(match["candidate_ids"], [])
        self.assertEqual(evidence["google_vision_tmdb_ids"], [])
        engine.tmdb.movie_details.assert_not_called()

    def test_ocr_tmdb_results_are_unverified_suggestions_only(self):
        engine = self.make_engine()
        engine.tmdb.search_movie.return_value = {
            "results": [{
                "id": 737756,
                "title": "Buladó",
                "original_title": "Buladó",
                "release_date": "2020-10-01",
                "overview": "A family story.",
                "poster_path": "/poster.jpg",
            }]
        }

        result = engine._search_ocr_title_candidates({
            "ocr_title_hints": ["Buladó"],
        })

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates"][0]["tmdb_id"], 737756)
        self.assertFalse(result["identity_verified"])
        self.assertIn("cannot set READY", result["warning"])
        engine.tmdb.search_movie.assert_called_once_with("Buladó")


if __name__ == "__main__":
    unittest.main()
