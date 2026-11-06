from __future__ import annotations

import json
import unittest
from unittest.mock import call, patch

import httpx

from app.engine import MetadataEngine
from app.gemini_vision import GeminiPosterVision, GeminiVisionError, MIN_CANDIDATE_CONFIDENCE
from app.tmdb import TMDBClient


class GeminiVisionClientTests(unittest.TestCase):
    def test_accepts_only_public_raw_github_image_urls(self):
        accepted = GeminiPosterVision.validate_image_url(
            "https://raw.githubusercontent.com/owner/repo/main/poster.jpg"
        )
        self.assertEqual(accepted, "https://raw.githubusercontent.com/owner/repo/main/poster.jpg")
        for url in (
            "http://raw.githubusercontent.com/owner/repo/main/poster.jpg",
            "https://github.com/owner/repo/poster.jpg",
            "https://raw.githubusercontent.com.evil.example/owner/poster.jpg",
            "https://user:pass@raw.githubusercontent.com/owner/repo/poster.jpg",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    GeminiPosterVision.validate_image_url(url)

    def test_structured_image_request_and_response_are_normalized(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "raw.githubusercontent.com":
                return httpx.Response(
                    200,
                    headers={"content-type": "image/jpeg"},
                    content=b"\xff\xd8\xff" + b"poster-pixels",
                )
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "steps": [{
                        "type": "model_output",
                        "content": [{
                            "type": "text",
                            "text": json.dumps({
                                "is_movie_poster": True,
                                "candidates": [{
                                    "title": "A Short Film About Killing",
                                    "year": 1988,
                                    "director": "Krzysztof Kieslowski",
                                    "confidence": 0.97,
                                    "visible_evidence": ["A Short Film About Killing", "Krzysztof Kieslowski"],
                                }],
                                "poster_text": ["A Short Film About Killing"],
                                "notes": "The title and director are readable.",
                            }),
                        }],
                    }],
                    "usage": {
                        "total_input_tokens": 300,
                        "total_output_tokens": 50,
                        "total_tokens": 350,
                    },
                },
            )

        vision = GeminiPosterVision("test-secret", model="gemini-3.8-flash")
        vision.client.close()
        vision.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            result = vision.identify_poster(
                "https://raw.githubusercontent.com/owner/repo/main/poster.jpg"
            )
        finally:
            vision.close()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider"], "google_gemini_vision")
        self.assertEqual(result["candidates"][0]["title"], "A Short Film About Killing")
        self.assertEqual(result["candidates"][0]["year"], 1988)
        self.assertEqual(result["candidates"][0]["director"], "Krzysztof Kieslowski")
        self.assertEqual(result["model"], "gemini-3.8-flash")
        self.assertFalse(result["identity_evidence"])
        self.assertFalse(result["automatic_match_allowed"])
        self.assertEqual(captured["url"], "https://generativelanguage.googleapis.com/v1beta/interactions")
        self.assertFalse(captured["body"]["store"])
        self.assertEqual(captured["body"]["input"][0]["type"], "user_input")
        self.assertEqual(captured["body"]["input"][0]["content"][1]["type"], "image")
        self.assertEqual(captured["body"]["input"][0]["content"][1]["mime_type"], "image/jpeg")
        self.assertEqual(captured["body"]["input"][0]["content"][1]["resolution"], "medium")
        self.assertEqual(captured["body"]["response_format"]["mime_type"], "application/json")


    def test_gemini_http_503_retries_with_exponential_backoff(self):
        api_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls
            if request.url.host == "raw.githubusercontent.com":
                return httpx.Response(
                    200,
                    headers={"content-type": "image/jpeg"},
                    content=b"\\xff\\xd8\\xffposter-pixels",
                )
            api_calls += 1
            if api_calls < 3:
                return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "steps": [{
                        "type": "model_output",
                        "content": [{
                            "type": "text",
                            "text": json.dumps({
                                "is_movie_poster": True,
                                "candidates": [{
                                    "title": "A Short Film About Killing",
                                    "year": 1988,
                                    "director": "Krzysztof Kieslowski",
                                    "confidence": 0.97,
                                    "visible_evidence": ["Readable title"],
                                }],
                                "poster_text": ["A Short Film About Killing"],
                                "notes": "",
                            }),
                        }],
                    }],
                },
            )

        vision = GeminiPosterVision("test-secret")
        vision.client.close()
        vision.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            with patch("app.gemini_vision.time.sleep") as sleep:
                result = vision.identify_poster(
                    "https://raw.githubusercontent.com/owner/repo/main/poster.jpg"
                )
        finally:
            vision.close()

        self.assertEqual(api_calls, 3)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])

    def test_gemini_persistent_http_503_stops_after_three_attempts(self):
        api_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal api_calls
            if request.url.host == "raw.githubusercontent.com":
                return httpx.Response(
                    200,
                    headers={"content-type": "image/jpeg"},
                    content=b"\\xff\\xd8\\xffposter-pixels",
                )
            api_calls += 1
            return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})

        vision = GeminiPosterVision("test-secret")
        vision.client.close()
        vision.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            with patch("app.gemini_vision.time.sleep"):
                with self.assertRaises(GeminiVisionError) as caught:
                    vision.identify_poster(
                        "https://raw.githubusercontent.com/owner/repo/main/poster.jpg"
                    )
        finally:
            vision.close()

        self.assertEqual(api_calls, 3)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("after 3 attempts", str(caught.exception))

    def test_gemini_http_429_is_exposed_for_batch_stop(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "raw.githubusercontent.com":
                return httpx.Response(
                    200,
                    headers={"content-type": "image/jpeg"},
                    content=b"\xff\xd8\xffposter-pixels",
                )
            return httpx.Response(429, json={"error": {"message": "quota exhausted"}})

        vision = GeminiPosterVision("test-secret")
        vision.client.close()
        vision.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            with self.assertRaises(Exception) as caught:
                vision.identify_poster(
                    "https://raw.githubusercontent.com/owner/repo/main/poster.jpg"
                )
        finally:
            vision.close()

        self.assertEqual(getattr(caught.exception, "status_code", None), 429)
        self.assertIn("request limit", str(caught.exception).lower())

    def test_low_confidence_or_missing_year_never_becomes_eligible(self):
        result = GeminiPosterVision._normalize_model_result({
            "is_movie_poster": True,
            "candidates": [
                {
                    "title": "Uncertain Film",
                    "year": 1988,
                    "director": "Unknown Director",
                    "confidence": MIN_CANDIDATE_CONFIDENCE - 0.01,
                    "visible_evidence": [],
                },
                {
                    "title": "No Year Film",
                    "year": 0,
                    "director": "Unknown Director",
                    "confidence": 0.99,
                    "visible_evidence": [],
                },
            ],
            "poster_text": [],
            "notes": "",
        })
        self.assertEqual(result["status"], "no_confident_candidate")
        self.assertFalse(result["automatic_match_allowed"])


def movie_details() -> dict:
    return {
        "id": 10754,
        "title": "A Short Film About Killing",
        "original_title": "Krótki film o zabijaniu",
        "release_date": "1988-03-11",
        "poster_path": "/short-film-killing.jpg",
        "backdrop_path": "/backdrop.jpg",
        "original_language": "pl",
        "production_countries": [{"iso_3166_1": "PL", "name": "Poland"}],
        "external_ids": {"imdb_id": "tt0095468"},
        "credits": {
            "cast": [],
            "crew": [{"job": "Director", "name": "Krzysztof Kieslowski"}],
        },
        "alternative_titles": {"titles": []},
        "release_dates": {"results": []},
        "keywords": {"keywords": []},
        "images": {"posters": [], "backdrops": []},
    }


class VisionTMDB:
    def __init__(self):
        self.details_calls = []
        self.search_calls = []
        self.record = movie_details()

    def search_movies(self, query: str, year: int | None = None):
        self.search_calls.append((query, year))
        return {
            "results": [{
                "id": 10754,
                "title": "A Short Film About Killing",
                "original_title": "Krótki film o zabijaniu",
                "release_date": "1988-03-11",
            }]
        }

    def movie_details(self, tmdb_id: int):
        self.details_calls.append(tmdb_id)
        return self.record

    @staticmethod
    def normalize_details(details):
        return TMDBClient.normalize_details(details)


class VisionLens:
    def __init__(self, hints=None, year=1988):
        self.hints = hints or ["Krzysztof Kieslowski introduces A Short Film About Killing"]
        self.year = year

    def title_hints_from_lens_results(self, _rows):
        return self.hints, self.year


class GeminiToTMDBIdentityTests(unittest.TestCase):
    def make_engine(self, lens=None):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine._details_cache = {}
        engine._stop_batch = False
        engine._provider_stop_status = None
        engine.tmdb = VisionTMDB()
        engine.reverse_image = lens or VisionLens()
        return engine

    @staticmethod
    def vision_result(director="Krzysztof Kieslowski", year=1988, confidence=0.97):
        return {
            "provider": "google_gemini_vision",
            "source": "poster_image_pixels",
            "status": "ok",
            "is_movie_poster": True,
            "candidates": [{
                "title": "A Short Film About Killing",
                "year": year,
                "director": director,
                "confidence": confidence,
                "visible_evidence": ["The title is visible on the poster."],
            }],
        }

    def test_gemini_title_year_and_tmdb_director_match_can_resolve_identity(self):
        engine = self.make_engine()
        candidate, match, evidence = engine._find_movie(
            [],
            expected_folder="A Short Film About Killing (Krótki film o zabijaniu) (1988)",
            vision_result=self.vision_result(),
        )

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["identity_source"], "gemini_vision_poster_pixels_verified_by_tmdb")
        self.assertEqual(match["identity_method"], "gemini_vision_exact_title_year_director_tmdb_details")
        self.assertEqual(engine.tmdb.search_calls, [("A Short Film About Killing", 1988)])
        self.assertEqual(engine.tmdb.details_calls, [10754])
        self.assertEqual(evidence["vision_resolution"]["status"], "verified")
        self.assertEqual(evidence["vision_resolution"]["verified_tmdb_ids"], [10754])
        self.assertTrue(evidence["vision_resolution"]["automatic_match_allowed"])

    def test_wrong_director_keeps_candidate_in_review(self):
        engine = self.make_engine()
        candidate, match, evidence = engine._find_movie(
            [],
            expected_folder="A Short Film About Killing (1988)",
            vision_result=self.vision_result(director="A different director"),
        )

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(evidence["vision_resolution"]["status"], "tmdb_details_validation_failed")
        self.assertFalse(evidence["vision_resolution"]["automatic_match_allowed"])

    def test_missing_ai_director_requires_independent_lens_title_support(self):
        engine = self.make_engine(lens=VisionLens())
        candidate, match, evidence = engine._find_movie(
            [{"title": "Krzysztof Kieslowski introduces A Short Film About Killing"}],
            expected_folder="A Short Film About Killing (1988)",
            vision_result=self.vision_result(director=""),
        )
        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        check = evidence["vision_resolution"]["validation"][0]["checks"][0]
        self.assertTrue(check["lens_title_corroborated"])
        self.assertEqual(check["corroboration_method"], "lens_title")

    def test_missing_ai_director_without_lens_support_stays_review(self):
        engine = self.make_engine(lens=VisionLens(hints=["An unrelated movie title"], year=1988))
        candidate, match, evidence = engine._find_movie(
            [{"title": "An unrelated movie title"}],
            expected_folder="A Short Film About Killing (1988)",
            vision_result=self.vision_result(director=""),
        )
        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertFalse(evidence["vision_resolution"]["automatic_match_allowed"])

    def test_low_confidence_candidate_does_not_query_tmdb(self):
        engine = self.make_engine()
        candidate, match, evidence = engine._find_movie(
            [],
            expected_folder="A Short Film About Killing (1988)",
            vision_result=self.vision_result(confidence=0.80),
        )
        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(engine.tmdb.search_calls, [])
        self.assertEqual(evidence["vision_resolution"]["status"], "no_confident_candidate")


if __name__ == "__main__":
    unittest.main()
