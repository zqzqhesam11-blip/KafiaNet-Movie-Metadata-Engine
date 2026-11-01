from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.engine import MetadataEngine
from app.tmdb import TMDBClient


def movie_details(tmdb_id: int, imdb_id: str, title: str) -> dict:
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "release_date": "1988-03-11",
        "external_ids": {"imdb_id": imdb_id},
        "credits": {"cast": [], "crew": []},
        "alternative_titles": {"titles": []},
        "release_dates": {"results": []},
        "keywords": {"keywords": []},
    }


class FakeTMDB:
    def __init__(self):
        self.records = {
            10754: movie_details(10754, "tt0095468", "A Short Film About Killing"),
            31056: movie_details(31056, "tt0095467", "A Short Film About Love"),
        }
        self.imdb_map = {
            "tt0095468": [{"id": 10754, "title": "Deliberately wrong cached title"}],
            "tt0095467": [{"id": 31056, "title": "Another wrong cached title"}],
        }
        self.search_movies_calls = 0

    def find_by_imdb_id(self, imdb_id: str):
        return self.imdb_map.get(imdb_id, [])

    def movie_details(self, tmdb_id: int):
        return self.records[int(tmdb_id)]

    @staticmethod
    def normalize_details(details: dict):
        return TMDBClient.normalize_details(details)

    @staticmethod
    def select_worldwide_title(details: dict):
        return TMDBClient.select_worldwide_title(details)

    def search_movies(self, *args, **kwargs):
        self.search_movies_calls += 1
        raise AssertionError("TMDB title search must never be used")


class FakeGitHub:
    def raw_url(self, path: str) -> str:
        return f"https://raw.example.test/{path}"


class FakeResolver:
    def __init__(self):
        self.search_rows = []
        self.search_input = []

    def resolve_result_urls(self, rows):
        return rows

    def search_official_id_pages(self, rows):
        self.search_input = list(rows)
        return list(self.search_rows)


class IdOnlyIdentityTests(unittest.TestCase):
    def make_engine(self):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine._details_cache = {}
        engine.tmdb = FakeTMDB()
        engine.settings = SimpleNamespace(
            posters_repo="owner/posters",
            posters_branch="main",
        )
        engine.github = FakeGitHub()
        engine.reverse_image = FakeResolver()
        return engine

    def test_tmdb_and_imdb_links_select_same_movie_without_title_or_folder(self):
        engine = self.make_engine()
        rows = [
            {
                "title": "Unrelated caption for a different film",
                "url": "https://www.imdb.com/title/tt0095468/",
                "resolved_url": "https://www.imdb.com/title/tt0095468/",
            },
            {
                "title": "Random page headline with a conflicting year",
                "url": "https://www.themoviedb.org/movie/10754",
                "resolved_url": "https://www.themoviedb.org/movie/10754",
            },
        ]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 10754)
        self.assertEqual(match["imdb_id"], "tt0095468")
        self.assertTrue(match["folder_name_used_for_matching"] is False)
        self.assertTrue(evidence["resolved_identity"]["cross_provider_match"])
        self.assertEqual(engine.tmdb.search_movies_calls, 0)

    def test_matching_pair_wins_over_another_single_provider_candidate(self):
        engine = self.make_engine()
        rows = [
            {
                "title": "Caption says A Short Film About Love",
                "url": "https://www.imdb.com/title/tt0095468/",
                "resolved_url": "https://www.imdb.com/title/tt0095468/",
            },
            {
                "title": "Unrelated title string",
                "url": "https://www.themoviedb.org/movie/10754",
                "resolved_url": "https://www.themoviedb.org/movie/10754",
            },
            {
                "title": "Another result",
                "url": "https://www.themoviedb.org/movie/31056",
                "resolved_url": "https://www.themoviedb.org/movie/31056",
            },
        ]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["imdb_id"], "tt0095468")
        self.assertEqual(match["identity_method"], "matching_tmdb_and_imdb_backlinks")
        self.assertIn(31056, [row["tmdb_id"] for row in evidence["candidate_identities"]])
        self.assertEqual(engine.tmdb.search_movies_calls, 0)

    def test_distinct_imdb_candidates_without_cross_provider_match_go_to_review(self):
        engine = self.make_engine()
        rows = [
            {
                "title": "A Short Film About Killing",
                "url": "https://www.imdb.com/title/tt0095468/",
                "resolved_url": "https://www.imdb.com/title/tt0095468/",
            },
            {
                "title": "A Short Film About Love",
                "url": "https://www.imdb.com/title/tt0095467/",
                "resolved_url": "https://www.imdb.com/title/tt0095467/",
            },
        ]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(set(match["candidate_ids"]), {10754, 31056})
        self.assertEqual(engine.tmdb.search_movies_calls, 0)

    def test_no_official_ids_means_review_even_when_folder_and_caption_match(self):
        engine = self.make_engine()
        rows = [{
            "title": "A Short Film About Killing",
            "url": "https://example.com/a-short-film-about-killing",
            "source": "https://example.com/tt0095468-poster.jpg",
        }]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertIn("No official IMDb/TMDB movie URL", match["reason"])
        self.assertEqual(engine.tmdb.search_movies_calls, 0)

    def test_single_tmdb_candidate_without_verified_imdb_id_stays_in_review(self):
        engine = self.make_engine()
        engine.tmdb.records[10754] = movie_details(
            10754, "", "A Short Film About Killing"
        )
        engine.tmdb.records[10754]["external_ids"] = {}

        candidate, match, _ = engine._find_movie([{
            "title": "Caption is not an identity signal",
            "url": "https://www.themoviedb.org/movie/10754",
            "resolved_url": "https://www.themoviedb.org/movie/10754",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertIn("both IDs are required", match["reason"])
        self.assertEqual(engine.tmdb.search_movies_calls, 0)

    def test_record_persists_both_ids_as_explicit_fields(self):
        engine = self.make_engine()
        rows = [
            {
                "title": "Wrong caption",
                "url": "https://www.imdb.com/title/tt0095468/",
                "resolved_url": "https://www.imdb.com/title/tt0095468/",
            },
            {
                "title": "Wrong title",
                "url": "https://www.themoviedb.org/movie/10754",
                "resolved_url": "https://www.themoviedb.org/movie/10754",
            },
        ]
        engine._reverse_image_results = lambda _image_url: rows

        record = engine._record_for_folder({
            "folder": "THIS FOLDER NAME IS INTENTIONALLY WRONG (1900)",
            "image_path": "Posters/poster.jpg",
        })

        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["tmdb_id"], 10754)
        self.assertEqual(record["imdb_id"], "tt0095468")
        self.assertEqual(record["tmdb"]["tmdb_id"], 10754)
        self.assertEqual(record["tmdb"]["external_ids"]["imdb_id"], "tt0095468")
        self.assertEqual(record["identity"]["tmdb_id"], 10754)
        self.assertEqual(record["identity"]["imdb_id"], "tt0095468")
        self.assertFalse(record["original"]["folder_name_used_for_matching"])
        self.assertEqual(engine.tmdb.search_movies_calls, 0)

    def test_google_official_id_search_results_can_resolve_image_match_without_folder_name(self):
        engine = self.make_engine()
        engine.reverse_image.search_rows = [
            {
                "title": "Official IMDb result",
                "url": "https://www.imdb.com/title/tt0095468/",
                "resolved_url": "https://www.imdb.com/title/tt0095468/",
                "resolved_urls": ["https://www.imdb.com/title/tt0095468/"],
                "provider": "google_official_id_search",
            },
            {
                "title": "Official TMDB result",
                "url": "https://www.themoviedb.org/movie/10754",
                "resolved_url": "https://www.themoviedb.org/movie/10754",
                "resolved_urls": ["https://www.themoviedb.org/movie/10754"],
                "provider": "google_official_id_search",
            },
        ]
        engine._reverse_image_results = lambda _image_url: [{
            "title": "Lens title hint from the poster image",
            "url": "https://example.com/non-official-result",
        }]

        record = engine._record_for_folder({
            "folder": "DO NOT USE THIS FOLDER NAME FOR SEARCH",
            "image_path": "Posters/poster.jpg",
        })

        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["tmdb_id"], 10754)
        self.assertEqual(record["imdb_id"], "tt0095468")
        self.assertEqual(
            engine.reverse_image.search_input[0]["title"],
            "Lens title hint from the poster image",
        )
        self.assertEqual(engine.tmdb.search_movies_calls, 0)
        self.assertEqual(
            record["reverse_image"]["evidence"]["official_id_search"]["status"],
            "ok",
        )


if __name__ == "__main__":
    unittest.main()
