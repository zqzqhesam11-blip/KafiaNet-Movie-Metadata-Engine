from __future__ import annotations

import unittest

import httpx

from app.google_play import (
    GooglePlayMetadataClient,
    GooglePlayMetadataError,
    evaluate_google_play_tmdb_candidate,
    parse_google_play_movie_html,
    parse_runtime_minutes,
)


class GooglePlayMetadataTests(unittest.TestCase):
    def test_parses_open_graph_and_json_ld_movie_fields(self):
        body = """
        <html><head>
          <meta property="og:title" content="Breathless - Movies on Google Play">
          <meta property="og:description" content="A small time crook, Michel Poiccard, is chased by police and flees to Italy.">
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Movie",
            "name": "Breathless",
            "description": "A small time crook, Michel Poiccard, is chased by police and flees to Italy.",
            "datePublished": "1961-02-07",
            "duration": "PT1H30M",
            "director": {"@type": "Person", "name": "Jean-Luc Godard"},
            "actor": [
              {"@type": "Person", "name": "Jean-Paul Belmondo"},
              {"@type": "Person", "name": "Jean Seberg"}
            ],
            "genre": ["Drama", "Crime"]
          }
          </script>
        </head><body></body></html>
        """

        result = parse_google_play_movie_html(
            "eDqHf0j8GUc.P",
            body,
            page_url="https://play.google.com/store/movies/details?id=eDqHf0j8GUc.P",
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["title"], "Breathless")
        self.assertIn("Michel Poiccard", result["description"])
        self.assertEqual(result["year"], 1961)
        self.assertEqual(result["runtime_minutes"], 90)
        self.assertEqual(result["directors"], ["Jean-Luc Godard"])
        self.assertEqual(result["cast"], ["Jean-Paul Belmondo", "Jean Seberg"])
        self.assertEqual(result["genres"], ["Drama", "Crime"])
        self.assertFalse(result["automatic_match_allowed"])

    def test_parses_visible_year_and_duration_when_schema_fields_are_absent(self):
        body = """
        <html><head>
          <meta property="og:title" content="Breathless - Movies on Google Play">
          <meta property="og:description" content="A small time crook, Michel Poiccard, flees to Italy.">
        </head><body><div>1960 • 90 minutes</div></body></html>
        """
        result = parse_google_play_movie_html("D1_Pzd4ayj0.P", body)
        self.assertEqual(result["year"], 1960)
        self.assertEqual(result["runtime_minutes"], 90)

    def test_parses_runtime_formats(self):
        self.assertEqual(parse_runtime_minutes("PT1H30M"), 90)
        self.assertEqual(parse_runtime_minutes("90 minutes"), 90)
        self.assertEqual(parse_runtime_minutes("1 hr 30 min"), 90)
        self.assertEqual(parse_runtime_minutes("1h30m"), 90)
        self.assertEqual(parse_runtime_minutes(105), 105)
        self.assertIsNone(parse_runtime_minutes("unknown"))

    def test_rejects_invalid_store_id_and_does_not_infer_slug(self):
        with self.assertRaises(ValueError):
            parse_google_play_movie_html(
                "Breathless",
                '<meta property="og:title" content="Breathless">',
            )

    def test_client_rejects_redirect_outside_official_google_play_page(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "https://evil.example/store/movies/details?id=eDqHf0j8GUc.P"},
                request=request,
            )

        client = GooglePlayMetadataClient.__new__(GooglePlayMetadataClient)
        client.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            with self.assertRaisesRegex(GooglePlayMetadataError, "outside its official"):
                client.fetch_movie_metadata("eDqHf0j8GUc.P")
        finally:
            client.close()

        self.assertEqual(len(requests), 1)

    def test_client_fetches_page_without_following_external_redirects(self):
        body = """
        <html><head>
          <meta property="og:title" content="Breathless - Movies on Google Play">
          <meta property="og:description" content="A small time crook, Michel Poiccard, flees to Italy.">
        </head><body></body></html>
        """

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "play.google.com")
            return httpx.Response(200, text=body, request=request)

        client = GooglePlayMetadataClient.__new__(GooglePlayMetadataClient)
        client.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            result = client.fetch_movie_metadata("D1_Pzd4ayj0.P")
        finally:
            client.close()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["title"], "Breathless")
        self.assertEqual(result["google_play_id"], "D1_Pzd4ayj0.P")

    def test_tmdb_candidate_requires_exact_title_and_multiple_independent_signals(self):
        metadata = {
            "title": "Breathless",
            "description": (
                "A small time crook, Michel Poiccard, chased by the police after "
                "stealing a car, shoots one of them and flees. Back in Paris he "
                "finds an American girlfriend and convinces her to go to Italy."
            ),
            "year": 1961,
            "runtime_minutes": 90,
            "directors": [],
            "cast": [],
        }
        details = {
            "tmdb_id": 269,
            "title": "Breathless",
            "original_title": "À bout de souffle",
            "alternative_titles": [],
            "year": 1960,
            "runtime": 90,
            "overview": (
                "A small-time thief steals a car and impulsively murders a motorcycle "
                "policeman. Wanted by the authorities, he attempts to persuade a girl "
                "to run away to Italy with him."
            ),
            "directors": [{"name": "Jean-Luc Godard"}],
            "cast": [
                {"name": "Jean-Paul Belmondo", "character": "Michel Poiccard"},
                {"name": "Jean Seberg", "character": "Patricia Franchini"},
            ],
        }

        result = evaluate_google_play_tmdb_candidate(metadata, details)

        self.assertTrue(result["accepted"])
        self.assertTrue(result["title_match"])
        self.assertTrue(result["character_match"])
        self.assertTrue(result["year_match"])
        self.assertTrue(result["runtime_match"])
        self.assertTrue(result["signals"]["description_match"])

    def test_tmdb_candidate_with_runtime_conflict_is_rejected(self):
        metadata = {
            "title": "Breathless",
            "description": "A small time crook, Michel Poiccard, steals a car and flees to Italy.",
            "year": 1961,
            "runtime_minutes": 90,
        }
        details = {
            "tmdb_id": 999,
            "title": "Breathless",
            "original_title": "Breathless",
            "alternative_titles": [],
            "year": 1961,
            "runtime": 130,
            "overview": "A small time crook, Michel Poiccard, steals a car and flees to Italy.",
            "directors": [],
            "cast": [{"name": "Actor", "character": "Michel Poiccard"}],
        }

        result = evaluate_google_play_tmdb_candidate(metadata, details)

        self.assertFalse(result["accepted"])
        self.assertIn("runtime_difference_over_8_minutes", result["conflicts"])

    def test_tmdb_candidate_with_title_only_is_not_accepted(self):
        metadata = {
            "title": "Breathless",
            "description": "A story of love and crime in Paris.",
            "year": None,
            "runtime_minutes": None,
        }
        details = {
            "tmdb_id": 1,
            "title": "Breathless",
            "original_title": "Breathless",
            "alternative_titles": [],
            "year": None,
            "runtime": None,
            "overview": "A different story about love and crime in Paris.",
            "directors": [],
            "cast": [],
        }

        result = evaluate_google_play_tmdb_candidate(metadata, details)

        self.assertFalse(result["accepted"])


if __name__ == "__main__":
    unittest.main()
