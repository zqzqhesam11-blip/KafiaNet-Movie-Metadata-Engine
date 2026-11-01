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
        self.search_movies_queries = []
        self.search_overrides = {}

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

    def search_movies(self, title: str, year=None):
        self.search_movies_calls += 1
        self.search_movies_queries.append(title)
        if title in self.search_overrides:
            return self.search_overrides[title]
        key = title.casefold()
        if key == "a short film about killing":
            return [{"id": 10754, "title": "A Short Film About Killing", "original_title": "Krótki film o zabijaniu"}]
        if key == "a short film about love":
            return [{"id": 31056, "title": "A Short Film About Love", "original_title": "Krótki film o miłości"}]
        return []


class FakeGitHub:
    def raw_url(self, path: str) -> str:
        return f"https://raw.example.test/{path}"


class FakeResolver:
    def resolve_result_urls(self, rows):
        return rows


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

    def test_lens_result_title_searches_tmdb_without_using_folder_or_image_filename(self):
        engine = self.make_engine()
        rows = [{
            "title": "A Short Film About Killing (1988) - IMDb",
            "url": "https://example.com/a-short-film-about-killing",
            "source": "https://example.com/UNRELATED-IMAGE-NAME-poster.jpg",
        }]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(match["candidate_ids"], [10754])
        self.assertIn("at least two independent matching metadata facts", match["reason"])
        self.assertEqual(match["candidate_comparisons"][0]["metadata_match"]["matched_fields"], ["year"])
        self.assertEqual(engine.tmdb.search_movies_queries, ["A Short Film About Killing"])
        self.assertEqual(evidence["tmdb_title_search"]["status"], "matched")
        self.assertFalse(evidence["tmdb_title_search"]["folder_name_used"])
        self.assertFalse(evidence["tmdb_title_search"]["image_filename_used"])

    def test_unique_lens_title_candidate_requires_two_matching_metadata_facts(self):
        engine = self.make_engine()
        engine.tmdb.records[10754]["credits"]["crew"] = [{
            "id": 20,
            "name": "Krzysztof Kieslowski",
            "job": "Director",
            "department": "Directing",
        }]

        candidate, match, evidence = engine._find_movie([{
            "title": "A Short Film About Killing (1988) - IMDb",
            "url": "https://example.com/a-short-film-about-killing",
            "content": "Directed by Krzysztof Kieslowski.",
        }])

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        metadata = evidence["resolved_identity"]["metadata_match"]
        self.assertIn("year", metadata["matched_fields"])
        self.assertIn("director", metadata["matched_fields"])
        self.assertTrue(metadata["evidence_qualified"])

    def test_multiple_close_tmdb_candidates_remain_in_review(self):
        engine = self.make_engine()
        engine.tmdb.records[5001] = movie_details(5001, "tt0005001", "The Thing")
        engine.tmdb.records[5002] = movie_details(5002, "tt0005002", "The Thing")
        engine.tmdb.search_overrides["The Thing"] = [
            {"id": 5001, "title": "The Thing", "original_title": "The Thing"},
            {"id": 5002, "title": "The Thing", "original_title": "The Thing"},
        ]

        candidate, match, evidence = engine._find_movie([{
            "title": "The Thing - IMDb",
            "url": "https://example.com/lens-result",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(set(match["candidate_ids"]), {5001, 5002})
        self.assertEqual(evidence["tmdb_title_search"]["status"], "matched")
        self.assertEqual(len(evidence["tmdb_title_search"]["qualified_candidate_ids"]), 2)
        self.assertEqual(len(match["candidate_comparisons"]), 2)

    def test_repeated_lens_phrase_connects_multiple_results_to_one_metadata_fingerprint(self):
        engine = self.make_engine()
        details = movie_details(5003, "tt0057345", "Breathless")
        details["original_title"] = "À bout de souffle"
        details["release_date"] = "1960-03-16"
        details["production_countries"] = [
            {"iso_3166_1": "FR", "name": "France"}
        ]
        details["origin_country"] = ["FR"]
        details["credits"] = {
            "cast": [],
            "crew": [{
                "id": 21,
                "name": "Jean-Luc Godard",
                "job": "Director",
                "department": "Directing",
            }],
        }
        engine.tmdb.records[5003] = details
        engine.tmdb.search_overrides["A Bout De Souffle"] = [{
            "id": 5003,
            "title": "Breathless",
            "original_title": "À bout de souffle",
        }]

        candidate, match, evidence = engine._find_movie([
            {
                "title": "A Bout De Souffle (1960) Jean Luc Godard",
                "url": "https://example.com/lens/result-one",
                "content": "Director: Jean-Luc Godard.",
            },
            {
                "title": "A Bout De Souffle Jean Seberg",
                "url": "https://example.com/lens/result-two",
                "content": "Country of origin: France.",
            },
        ])

        self.assertEqual(candidate["id"], 5003)
        self.assertEqual(match["status"], "ready")
        metadata = evidence["resolved_identity"]["metadata_match"]
        self.assertIn("year", metadata["matched_fields"])
        self.assertIn("country", metadata["matched_fields"])
        self.assertIn("director", metadata["matched_fields"])
        self.assertTrue(metadata["evidence_qualified"])

    def test_lens_metadata_fingerprint_selects_candidate_when_titles_are_ambiguous(self):
        engine = self.make_engine()
        matching = movie_details(5001, "tt0005001", "The Thing")
        matching["release_date"] = "2020-06-01"
        matching["production_countries"] = [
            {"iso_3166_1": "NL", "name": "Netherlands"}
        ]
        matching["origin_country"] = ["NL"]
        matching["vote_average"] = 6.4
        matching["vote_count"] = 100
        matching["credits"] = {
            "cast": [{
                "id": 1, "name": "Tiara Richards", "character": "Kenza", "order": 0
            }],
            "crew": [{
                "id": 2, "name": "Eché Janga", "job": "Director", "department": "Directing"
            }],
        }

        competing = movie_details(5002, "tt0005002", "The Thing")
        competing["release_date"] = "1997-01-01"
        competing["production_countries"] = [
            {"iso_3166_1": "US", "name": "United States"}
        ]
        competing["origin_country"] = ["US"]
        competing["vote_average"] = 6.5
        competing["vote_count"] = 200
        competing["credits"] = {
            "cast": [{
                "id": 3, "name": "Another Actor", "character": "Other Role", "order": 0
            }],
            "crew": [{
                "id": 4, "name": "Different Director", "job": "Director", "department": "Directing"
            }],
        }
        engine.tmdb.records[5001] = matching
        engine.tmdb.records[5002] = competing
        engine.tmdb.search_overrides["The Thing"] = [
            {"id": 5001, "title": "The Thing", "original_title": "The Thing"},
            {"id": 5002, "title": "The Thing", "original_title": "The Thing"},
        ]

        candidate, match, evidence = engine._find_movie([{
            "title": "The Thing (2020)",
            "url": "https://example.com/lens-result",
            "content": (
                "Country of origin: Netherlands | Director: Eché Janga | "
                "Cast: Tiara Richards | Main character: Kenza | IMDb rating: 6.5/10"
            ),
        }])

        self.assertEqual(candidate["id"], 5001)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["identity_method"], "lens_metadata_fingerprint_unique_candidate")
        selected = next(
            item for item in evidence["candidate_identities"] if item["tmdb_id"] == 5001
        )
        self.assertGreaterEqual(selected["metadata_match"]["strong_match_count"], 4)
        self.assertTrue(selected["metadata_match"]["evidence_qualified"])
        self.assertEqual(selected["metadata_match"]["best_single_observation_match_count"], 5)
        self.assertIn("year", selected["metadata_match"]["matched_fields"])
        self.assertIn("country", selected["metadata_match"]["matched_fields"])
        self.assertIn("director", selected["metadata_match"]["matched_fields"])
        self.assertIn("main_character", selected["metadata_match"]["matched_fields"])

    def test_unlabelled_lens_snippet_names_match_tmdb_metadata(self):
        engine = self.make_engine()
        matching = movie_details(5001, "tt0005001", "The Thing")
        matching["release_date"] = "2020-06-01"
        matching["production_countries"] = [
            {"iso_3166_1": "NL", "name": "Netherlands"}
        ]
        matching["origin_country"] = ["NL"]
        matching["credits"] = {
            "cast": [{
                "id": 1, "name": "Tiara Richards", "character": "Kenza", "order": 0
            }],
            "crew": [{
                "id": 2, "name": "Eché Janga", "job": "Director", "department": "Directing"
            }],
        }
        competing = movie_details(5002, "tt0005002", "The Thing")
        competing["release_date"] = "1997-01-01"
        competing["production_countries"] = [
            {"iso_3166_1": "US", "name": "United States"}
        ]
        competing["origin_country"] = ["US"]
        competing["credits"] = {
            "cast": [{
                "id": 3, "name": "Another Actor", "character": "Other Role", "order": 0
            }],
            "crew": [{
                "id": 4, "name": "Different Director", "job": "Director", "department": "Directing"
            }],
        }
        engine.tmdb.records[5001] = matching
        engine.tmdb.records[5002] = competing
        engine.tmdb.search_overrides["The Thing"] = [
            {"id": 5001, "title": "The Thing", "original_title": "The Thing"},
            {"id": 5002, "title": "The Thing", "original_title": "The Thing"},
        ]

        candidate, match, evidence = engine._find_movie([{
            "title": "The Thing (2020)",
            "url": "https://example.com/lens-result",
            "content": (
                "The 2020 film from the Netherlands is directed by Eché Janga. "
                "Kenza is played by Tiara Richards."
            ),
        }])

        self.assertEqual(candidate["id"], 5001)
        self.assertEqual(match["status"], "ready")
        selected = next(
            item for item in evidence["candidate_identities"] if item["tmdb_id"] == 5001
        )
        metadata = selected["metadata_match"]
        self.assertTrue(metadata["evidence_qualified"])
        self.assertIn("country", metadata["matched_fields"])
        self.assertIn("director", metadata["matched_fields"])
        self.assertIn("cast", metadata["matched_fields"])
        self.assertIn("main_character", metadata["matched_fields"])
        self.assertEqual(metadata["best_single_observation_match_count"], 5)

    def test_rating_alone_cannot_disambiguate_tmdb_candidates(self):
        engine = self.make_engine()
        for tmdb_id, imdb_id, rating in (
            (5001, "tt0005001", 6.5),
            (5002, "tt0005002", 6.5),
        ):
            details = movie_details(tmdb_id, imdb_id, "The Thing")
            details["vote_average"] = rating
            details["vote_count"] = 100
            engine.tmdb.records[tmdb_id] = details
        engine.tmdb.search_overrides["The Thing"] = [
            {"id": 5001, "title": "The Thing", "original_title": "The Thing"},
            {"id": 5002, "title": "The Thing", "original_title": "The Thing"},
        ]

        candidate, match, _ = engine._find_movie([{
            "title": "The Thing",
            "url": "https://example.com/lens-result",
            "content": "IMDb rating: 6.5/10",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(match["candidate_ids"], [5001, 5002])

    def test_ambiguous_title_without_lens_metadata_clues_reports_candidate_profiles(self):
        engine = self.make_engine()
        engine.tmdb.records[5001] = movie_details(5001, "tt0005001", "The Thing")
        engine.tmdb.records[5002] = movie_details(5002, "tt0005002", "The Thing")
        engine.tmdb.search_overrides["The Thing"] = [
            {"id": 5001, "title": "The Thing", "original_title": "The Thing"},
            {"id": 5002, "title": "The Thing", "original_title": "The Thing"},
        ]

        candidate, match, evidence = engine._find_movie([{
            "title": "The Thing",
            "url": "https://example.com/lens-result",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(len(match["candidate_comparisons"]), 2)
        self.assertIn("metadata_profile", match["candidate_comparisons"][0])

    def test_image_filename_alone_never_becomes_a_tmdb_search_query(self):
        engine = self.make_engine()
        rows = [{
            "title": "",
            "url": "https://example.com/unrelated-page",
            "source": "https://cdn.example.com/A-Short-Film-About-Killing-poster.jpg",
        }]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(engine.tmdb.search_movies_calls, 0)
        self.assertEqual(evidence["tmdb_title_search"]["queries"], [])

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

    def test_lens_provider_error_is_preserved_and_tmdb_search_is_skipped(self):
        engine = self.make_engine()
        engine._reverse_image_results = lambda _image_url: [{
            "error": "BrightDataAPIError",
            "message": "Bright Data service error (HTTP 502): upstream timeout",
        }]

        record = engine._record_for_folder({
            "folder": "DO NOT USE THIS FOLDER",
            "image_path": "Posters/poster.jpg",
        })

        self.assertEqual(record["status"], "review")
        latest = record["reverse_image"]["latest_query"]
        self.assertEqual(latest["status"], "error")
        self.assertIn("HTTP 502", latest["errors"][0]["message"])
        self.assertEqual(
            record["reverse_image"]["evidence"]["tmdb_title_search"]["status"],
            "skipped_lens_error",
        )
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
        self.assertEqual(
            record["reverse_image"]["evidence"]["tmdb_title_search"]["status"],
            "skipped_official_ids_present",
        )
        self.assertEqual(engine.tmdb.search_movies_calls, 0)

if __name__ == "__main__":
    unittest.main()
