from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.engine import MetadataEngine
from app.tmdb import TMDBClient


def movie_details(tmdb_id: int, imdb_id: str | None, title: str) -> dict:
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "release_date": "1988-03-11",
        "poster_path": "/primary-poster.jpg",
        "backdrop_path": "/backdrop.jpg",
        "original_language": "en",
        "production_countries": [{"iso_3166_1": "PL", "name": "Poland"}],
        "external_ids": {"imdb_id": imdb_id},
        "credits": {"cast": [], "crew": []},
        "alternative_titles": {"titles": []},
        "release_dates": {"results": []},
        "keywords": {"keywords": []},
        "images": {
            "posters": [
                {
                    "file_path": "/poster-a.jpg",
                    "iso_639_1": "en",
                    "width": 1000,
                    "height": 1500,
                    "vote_average": 7.5,
                    "vote_count": 8,
                },
                {
                    "file_path": "/poster-b.jpg",
                    "iso_639_1": None,
                    "width": 800,
                    "height": 1200,
                    "vote_average": 6.0,
                    "vote_count": 2,
                },
            ],
            "backdrops": [],
        },
    }


class FakeTMDB:
    def __init__(self):
        self.records = {
            10754: movie_details(10754, "tt0095468", "A Short Film About Killing"),
            31056: movie_details(31056, "tt0095467", "A Short Film About Love"),
        }
        self.details_calls: list[int] = []

    def movie_details(self, tmdb_id: int):
        self.details_calls.append(int(tmdb_id))
        return self.records[int(tmdb_id)]

    @staticmethod
    def normalize_details(details: dict):
        return TMDBClient.normalize_details(details)

    @staticmethod
    def select_worldwide_title(details: dict):
        return TMDBClient.select_worldwide_title(details)


class FakeGitHub:
    def raw_url(self, path: str) -> str:
        return f"https://raw.example.test/{path}"

    def get_json_file(self, _path: str):
        return None


class FakeResolver:
    last_lens_diagnostics = {"status": "ok", "result_count": 1}

    def resolve_result_urls(self, rows):
        return rows


class IdOnlyIdentityTests(unittest.TestCase):
    def make_engine(self):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine._details_cache = {}
        engine._stop_batch = False
        engine._provider_stop_status = None
        engine.tmdb = FakeTMDB()
        engine.settings = SimpleNamespace(
            posters_repo="owner/posters",
            posters_branch="main",
        )
        engine.github = FakeGitHub()
        engine.reverse_image = FakeResolver()
        return engine

    def test_one_official_tmdb_movie_id_resolves_through_tmdb_details(self):
        engine = self.make_engine()
        rows = [{
            "title": "Completely wrong title and country must be ignored",
            "url": "https://www.themoviedb.org/movie/10754-a-short-film-about-killing",
            "resolved_url": "https://www.themoviedb.org/movie/10754-a-short-film-about-killing",
            "content": "Wrong country: France; wrong year: 2020",
            "source": "https://cdn.example.test/WRONG-FILENAME-2020.jpg",
        }]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 10754)
        self.assertEqual(match["imdb_id"], "tt0095468")
        self.assertEqual(match["identity_method"], "lens_tmdb_movie_id_lookup")
        self.assertEqual(evidence["tmdb_ids"], [10754])
        self.assertEqual(evidence["tmdb_id_lookup"]["status"], "not_used_id_only")
        self.assertEqual(engine.tmdb.details_calls, [10754])
        self.assertFalse(evidence["id_resolution"]["folder_name_used"])
        self.assertFalse(evidence["id_resolution"]["image_filename_used"])

    def test_lens_title_without_tmdb_movie_id_stays_in_review(self):
        engine = self.make_engine()

        candidate, match, evidence = engine._find_movie([{
            "title": "A Short Film About Killing (1988) - IMDb",
            "url": "https://www.imdb.com/title/tt0095468/",
            "resolved_url": "https://www.imdb.com/title/tt0095468/",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertIn("numeric movie ID", match["reason"])
        self.assertEqual(evidence["tmdb_ids"], [])
        self.assertEqual(engine.tmdb.details_calls, [])

    def test_title_only_result_does_not_call_tmdb_text_search(self):
        engine = self.make_engine()

        candidate, match, _ = engine._find_movie([{
            "title": "Buladó (2020)",
            "url": "https://example.test/film/bulado",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(engine.tmdb.details_calls, [])

    def test_multiple_official_tmdb_movie_ids_remain_review(self):
        engine = self.make_engine()
        rows = [
            {
                "title": "Any text",
                "url": "https://www.themoviedb.org/movie/10754",
                "resolved_url": "https://www.themoviedb.org/movie/10754",
            },
            {
                "title": "Different text",
                "url": "https://www.themoviedb.org/movie/31056",
                "resolved_url": "https://www.themoviedb.org/movie/31056",
            },
        ]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(set(match["candidate_ids"]), {10754, 31056})
        self.assertEqual(set(engine.tmdb.details_calls), {10754, 31056})
        self.assertEqual(len(evidence["candidate_identities"]), 2)

    def test_non_tmdb_url_with_movie_path_is_not_accepted_as_tmdb_id(self):
        engine = self.make_engine()

        candidate, match, evidence = engine._find_movie([{
            "title": "Looks like a TMDB route",
            "url": "https://evil.example/themoviedb.org/movie/10754",
            "resolved_url": "https://evil.example/themoviedb.org/movie/10754",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(evidence["tmdb_ids"], [])
        self.assertEqual(engine.tmdb.details_calls, [])

    def test_lens_provider_error_does_not_fall_back_to_title_search(self):
        engine = self.make_engine()

        candidate, match, evidence = engine._find_movie([{
            "error": "BrightDataAPIError",
            "message": "Lens request failed",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(evidence["tmdb_id_lookup"]["queries"], [])
        self.assertEqual(engine.tmdb.details_calls, [])

    def test_record_saves_tmdb_details_and_tmdb_poster_urls(self):
        engine = self.make_engine()
        engine._reverse_image_results = lambda _image_url: [{
            "title": "Wrong Lens title must not affect the identity",
            "url": "https://www.themoviedb.org/movie/10754",
            "resolved_url": "https://www.themoviedb.org/movie/10754",
        }]

        record = engine._record_for_folder({
            "folder": "THIS FOLDER NAME IS INTENTIONALLY WRONG (1900)",
            "image_path": "Posters/poster.jpg",
        })

        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["tmdb_id"], 10754)
        self.assertEqual(record["imdb_id"], "tt0095468")
        self.assertEqual(record["tmdb"]["title"], "A Short Film About Killing")
        self.assertEqual(
            record["tmdb"]["poster_url"],
            "https://image.tmdb.org/t/p/w500/primary-poster.jpg",
        )
        self.assertEqual(
            record["tmdb"]["posters"][0]["url"],
            "https://image.tmdb.org/t/p/w500/poster-a.jpg",
        )
        self.assertEqual(
            record["tmdb"]["posters"][0]["original_url"],
            "https://image.tmdb.org/t/p/original/poster-a.jpg",
        )
        self.assertEqual(
            record["identity"]["source"],
            "google_lens_official_tmdb_movie_id",
        )
        self.assertFalse(record["original"]["folder_name_used_for_matching"])


if __name__ == "__main__":
    unittest.main()
