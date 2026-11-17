from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.engine import MetadataEngine
from app.tmdb import TMDBClient
from app.image_evidence import (
    extract_google_play_ids,
    extract_imdb_ids,
    extract_tmdb_movie_ids,
    extract_wikidata_ids,
    tmdb_movie_url_diagnostics,
)


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
        self.find_calls: list[str] = []
        self.find_results = {
            "tt0095468": {"movie_results": [{"id": 10754}]},
            "tt0095467": {"movie_results": [{"id": 31056}]},
        }
        self.wikidata_find_calls: list[str] = []
        self.search_calls: list[tuple[str, int]] = []
        self.search_pages: dict[tuple[str, int], dict] = {}
        self.wikidata_find_results = {
            "Q12345": {"movie_results": [{"id": 10754}], "tv_results": [{"id": 999}]},
        }

    def find_by_imdb_id(self, imdb_id: str):
        self.find_calls.append(imdb_id)
        return self.find_results.get(imdb_id, {"movie_results": []})

    def find_by_wikidata_id(self, wikidata_id: str):
        self.wikidata_find_calls.append(wikidata_id)
        return self.wikidata_find_results.get(wikidata_id, {"movie_results": []})

    def search_movies(self, query: str, year: int | None = None, page: int = 1):
        self.search_calls.append((query, page))
        return self.search_pages.get(
            (query, page),
            {"results": [], "total_pages": 1, "total_results": 0},
        )

    def movie_details(self, tmdb_id: int):
        self.details_calls.append(int(tmdb_id))
        return self.records[int(tmdb_id)]

    @staticmethod
    def normalize_details(details: dict):
        return TMDBClient.normalize_details(details)

class FakeWikidata:
    def __init__(self):
        self.google_play_calls: list[str] = []
        self.google_play_results = {
            "KuBCXRtFvO8": {
                "status": "resolved",
                "wikidata_ids": ["Q12345"],
                "result_count": 1,
            },
        }

    def find_films_by_google_play_id(self, google_play_id: str):
        self.google_play_calls.append(google_play_id)
        return self.google_play_results.get(google_play_id, {
            "status": "no_match",
            "wikidata_ids": [],
            "result_count": 0,
        })


class FakeGooglePlay:
    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.calls: list[str] = []

    def fetch_movie_metadata(self, movie_id: str):
        self.calls.append(movie_id)
        if movie_id not in self.pages:
            raise RuntimeError("No fake Google Play page configured")
        return self.pages[movie_id]

    def close(self):
        return None


class FakeGitHub:
    def raw_url(self, path: str):
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
        engine.wikidata = FakeWikidata()
        engine.google_play = FakeGooglePlay()
        engine.settings = SimpleNamespace(
            posters_repo="owner/posters",
            posters_branch="main",
            review_research_enabled=False,
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
        self.assertEqual(evidence["tmdb_id_lookup"]["status"], "resolved")
        self.assertEqual(engine.tmdb.details_calls, [10754])
        self.assertFalse(evidence["id_resolution"]["folder_name_used"])
        self.assertFalse(evidence["id_resolution"]["image_filename_used"])

    def test_tmdb_id_is_ready_even_when_tmdb_has_no_imdb_external_id(self):
        engine = self.make_engine()
        engine.tmdb.records[10754]["external_ids"] = {}

        candidate, match, _ = engine._find_movie([{
            "title": "Ignore title text",
            "resolved_url": "https://www.themoviedb.org/movie/10754",
        }])

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 10754)
        self.assertIsNone(match["imdb_id"])

    def test_extracts_wikidata_qid_only_from_typed_metadata_or_entity_url(self):
        rows = [
            {"lens_identifiers": {"wikidata_ids": ["Q12345"]}},
            {"url": "https://www.wikidata.org/wiki/Q67890"},
            {"url": "https://www.wikidata.org/wiki/Special:EntityPage/Q00000"},
            {"title": "Q55555 is just caption text"},
        ]

        self.assertEqual(extract_wikidata_ids(rows), ["Q12345", "Q67890"])

    def test_lens_wikidata_qid_resolves_only_one_tmdb_movie_result(self):
        engine = self.make_engine()
        engine.tmdb.records[10754]["external_ids"]["wikidata_id"] = "Q12345"
        rows = [{
            "title": "A deliberately misleading Lens caption",
            "lens_identifiers": {
                "wikidata_ids": ["Q12345"],
                "asin_ids": ["B003PHJLQY"],
                "google_play_ids": ["com.example.breathless"],
            },
        }]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 10754)
        self.assertEqual(match["identity_source"], "google_lens_wikidata_id_via_tmdb_find")
        self.assertEqual(match["identity_method"], "lens_wikidata_id_tmdb_find")
        self.assertEqual(engine.tmdb.wikidata_find_calls, ["Q12345"])
        self.assertEqual(engine.tmdb.details_calls, [10754])
        self.assertEqual(evidence["wikidata_resolution"]["status"], "resolved")
        self.assertEqual(evidence["resolved_identity"]["wikidata_ids"], ["Q12345"])
        self.assertEqual(evidence["resolved_identity"]["wikidata_tmdb_ids"], [10754])

    def test_wikidata_details_must_confirm_the_same_qid_returned_by_find(self):
        engine = self.make_engine()
        engine.tmdb.records[10754]["external_ids"]["wikidata_id"] = "Q99999"

        candidate, match, evidence = engine._find_movie([{
            "lens_identifiers": {"wikidata_ids": ["Q12345"]},
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(engine.tmdb.wikidata_find_calls, ["Q12345"])
        self.assertEqual(engine.tmdb.details_calls, [10754])
        self.assertEqual(
            evidence["wikidata_resolution"]["status"],
            "details_verification_failed",
        )
        self.assertEqual(
            evidence["tmdb_id_lookup"]["errors"][0]["message"],
            "TMDB movie details did not confirm the Wikidata ID returned by /find",
        )

    def test_google_play_id_maps_through_wikidata_p6562_then_tmdb(self):
        engine = self.make_engine()
        engine.tmdb.records[10754]["external_ids"]["wikidata_id"] = "Q12345"

        candidate, match, evidence = engine._find_movie([{
            "title": "A deliberately misleading Lens caption",
            "lens_identifiers": {
                "google_play_ids": ["KuBCXRtFvO8"],
            },
        }])

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(
            match["identity_source"],
            "google_lens_google_play_id_via_wikidata_tmdb_find",
        )
        self.assertEqual(
            match["identity_method"],
            "lens_google_play_id_wikidata_tmdb_find",
        )
        self.assertEqual(engine.wikidata.google_play_calls, ["KuBCXRtFvO8"])
        self.assertEqual(engine.tmdb.wikidata_find_calls, ["Q12345"])
        self.assertEqual(engine.tmdb.details_calls, [10754])
        self.assertEqual(
            evidence["google_play_resolution"]["status"],
            "resolved",
        )
        self.assertEqual(
            evidence["resolved_identity"]["google_play_ids"],
            ["KuBCXRtFvO8"],
        )
        self.assertEqual(
            evidence["resolved_identity"]["wikidata_ids"],
            ["Q12345"],
        )

    def test_google_play_id_mapping_to_multiple_wikidata_films_stays_review(self):
        engine = self.make_engine()
        engine.wikidata.google_play_results["KuBCXRtFvO8"] = {
            "status": "ambiguous",
            "wikidata_ids": ["Q12345", "Q99999"],
            "result_count": 2,
        }

        candidate, match, evidence = engine._find_movie([{
            "lens_identifiers": {
                "google_play_ids": ["KuBCXRtFvO8"],
            },
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(
            evidence["google_play_resolution"]["status"],
            "ambiguous",
        )
        self.assertEqual(match["candidate_ids"], ["Q12345", "Q99999"])
        self.assertEqual(engine.tmdb.wikidata_find_calls, [])
        self.assertEqual(engine.tmdb.details_calls, [])

    def test_multiple_google_play_ids_are_checked_until_one_unique_qid_is_found(self):
        engine = self.make_engine()
        engine.tmdb.records[10754]["external_ids"]["wikidata_id"] = "Q12345"
        engine.wikidata.google_play_results["D1_Pzd4ayj0.P"] = {
            "status": "no_match",
            "wikidata_ids": [],
            "result_count": 0,
        }
        engine.wikidata.google_play_results["eDqHf0j8GUc.P"] = {
            "status": "resolved",
            "wikidata_ids": ["Q12345"],
            "result_count": 1,
        }

        candidate, match, evidence = engine._find_movie([
            {"url": "https://play.google.com/store/movies/details?id=D1_Pzd4ayj0.P"},
            {"url": "https://play.google.com/store/movies/details/Breathless?id=eDqHf0j8GUc.P"},
        ])

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(
            engine.wikidata.google_play_calls,
            ["D1_Pzd4ayj0.P", "eDqHf0j8GUc.P"],
        )
        self.assertEqual(
            evidence["google_play_resolution"]["status"],
            "resolved",
        )
        self.assertEqual(
            evidence["google_play_resolution"]["selected_google_play_id"],
            "eDqHf0j8GUc.P",
        )

    def test_different_google_play_ids_mapping_to_different_qids_stays_review(self):
        engine = self.make_engine()
        engine.wikidata.google_play_results["D1_Pzd4ayj0.P"] = {
            "status": "resolved",
            "wikidata_ids": ["Q12345"],
            "result_count": 1,
        }
        engine.wikidata.google_play_results["eDqHf0j8GUc.P"] = {
            "status": "resolved",
            "wikidata_ids": ["Q99999"],
            "result_count": 1,
        }

        candidate, match, evidence = engine._find_movie([
            {"url": "https://play.google.com/store/movies/details?id=D1_Pzd4ayj0.P"},
            {"url": "https://play.google.com/store/movies/details?id=eDqHf0j8GUc.P"},
        ])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(
            evidence["google_play_resolution"]["status"],
            "ambiguous",
        )
        self.assertEqual(
            evidence["google_play_resolution"]["wikidata_ids"],
            ["Q12345", "Q99999"],
        )
        self.assertEqual(engine.tmdb.wikidata_find_calls, [])

    def test_google_play_page_metadata_resolves_only_after_tmdb_detail_corroboration(self):
        engine = self.make_engine()
        google_play_ids = ["eDqHf0j8GUc.P", "D1_Pzd4ayj0.P"]
        page_description = (
            "A small time crook, Michel Poiccard, chased by the police after "
            "stealing a car, shoots one of them and flees. Back in Paris he "
            "finds an American girlfriend and convinces her to go to Italy."
        )
        for movie_id in google_play_ids:
            engine.wikidata.google_play_results[movie_id] = {
                "status": "no_match",
                "wikidata_ids": [],
                "result_count": 0,
            }
            engine.google_play.pages[movie_id] = {
                "provider": "google_play_movie_page",
                "status": "ok",
                "google_play_id": movie_id,
                "title": "Breathless",
                "description": page_description,
                "year": 1961,
                "runtime_minutes": 90,
                "directors": [],
                "cast": [],
            }

        details = movie_details(269, "tt0053472", "Breathless")
        details["original_title"] = "À bout de souffle"
        details["release_date"] = "1960-03-16"
        details["runtime"] = 90
        details["overview"] = (
            "A small-time thief steals a car and impulsively murders a motorcycle "
            "policeman. Wanted by the authorities, he attempts to persuade a girl "
            "to run away to Italy with him."
        )
        details["credits"]["cast"] = [
            {"id": 1, "name": "Jean-Paul Belmondo", "character": "Michel Poiccard", "order": 0},
            {"id": 2, "name": "Jean Seberg", "character": "Patricia Franchini", "order": 1},
        ]
        details["credits"]["crew"] = [
            {"id": 3, "name": "Jean-Luc Godard", "job": "Director", "department": "Directing"}
        ]
        engine.tmdb.records[269] = details
        engine.tmdb.search_pages[("Breathless", 1)] = {
            "results": [{
                "id": 269,
                "title": "Breathless",
                "original_title": "À bout de souffle",
                "release_date": "1960-03-16",
            }],
            "total_pages": 1,
            "total_results": 1,
        }

        candidate, match, evidence = engine._find_movie([
            {"url": "https://play.google.com/store/movies/details?id=eDqHf0j8GUc.P"},
            {"url": "https://play.google.com/store/movies/details?id=D1_Pzd4ayj0.P"},
        ])

        self.assertEqual(candidate["id"], 269)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 269)
        self.assertEqual(
            match["identity_source"],
            "google_lens_google_play_page_metadata_via_tmdb_details",
        )
        self.assertEqual(
            match["identity_method"],
            "lens_google_play_page_metadata_tmdb_match",
        )
        self.assertEqual(engine.google_play.calls, google_play_ids)
        self.assertEqual(engine.tmdb.search_calls, [("Breathless", 1)])
        self.assertEqual(engine.tmdb.details_calls, [269])
        self.assertEqual(
            evidence["google_play_metadata_resolution"]["status"],
            "resolved",
        )
        self.assertEqual(
            evidence["google_play_metadata_resolution"]["selected_tmdb_id"],
            269,
        )
        self.assertTrue(
            evidence["google_play_metadata_resolution"]["candidate_matches"][0]["accepted"]
        )

    def test_google_play_page_year_prefilter_keeps_correct_candidate_under_details_limit(self):
        engine = self.make_engine()
        movie_id = "eDqHf0j8GUc.P"
        page_description = (
            "A small time crook, Michel Poiccard, chased by the police after "
            "stealing a car, shoots one of them and flees. Back in Paris he "
            "finds an American girlfriend and convinces her to go to Italy."
        )
        engine.google_play.pages[movie_id] = {
            "provider": "google_play_movie_page",
            "status": "ok",
            "google_play_id": movie_id,
            "title": "Breathless",
            "description": page_description,
            "year": 1961,
            "runtime_minutes": 90,
            "directors": [],
            "cast": [],
        }

        details = movie_details(269, "tt0053472", "Breathless")
        details["original_title"] = "À bout de souffle"
        details["release_date"] = "1960-03-16"
        details["runtime"] = 90
        details["overview"] = (
            "A small-time thief steals a car and impulsively murders a motorcycle "
            "policeman. Wanted by the authorities, he attempts to persuade a girl "
            "to run away to Italy with him."
        )
        details["credits"]["cast"] = [
            {"id": 1, "name": "Jean-Paul Belmondo", "character": "Michel Poiccard", "order": 0},
            {"id": 2, "name": "Jean Seberg", "character": "Patricia Franchini", "order": 1},
        ]
        engine.tmdb.records[269] = details

        # Simulate 18 unrelated exact-title matches from different decades.
        # Their known years conflict with the official Play page by more than
        # the evaluator's hard two-year threshold, so details calls are unnecessary.
        unrelated = [
            {
                "id": 1000 + offset,
                "title": "Breathless",
                "original_title": "Breathless",
                "release_date": f"{1980 + offset}-01-01",
            }
            for offset in range(18)
        ]
        correct = {
            "id": 269,
            "title": "Breathless",
            "original_title": "À bout de souffle",
            "release_date": "1960-03-16",
        }
        search_rows = unrelated + [correct]
        engine.tmdb.search_pages[("Breathless", 1)] = {
            "results": search_rows,
            "total_pages": 1,
            "total_results": len(search_rows),
        }

        candidate, match, evidence = engine._find_movie([
            {"url": f"https://play.google.com/store/movies/details?id={movie_id}"},
        ])

        resolution = evidence["google_play_metadata_resolution"]
        self.assertEqual(candidate["id"], 269)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 269)
        self.assertEqual(match["identity_method"], "lens_google_play_page_metadata_tmdb_match")
        self.assertEqual(resolution["candidate_count"], 19)
        self.assertEqual(resolution["candidate_count_after_prefilter"], 1)
        self.assertEqual(resolution["year_conflict_pruned_count"], 18)
        self.assertEqual(resolution["candidate_details_count"], 1)
        self.assertEqual(resolution["year_conflict_pruned_ids"], [
            1000 + offset for offset in range(18)
        ])
        self.assertEqual(engine.tmdb.details_calls, [269])

    def test_google_play_page_runtime_conflict_keeps_film_in_review(self):
        engine = self.make_engine()
        movie_id = "eDqHf0j8GUc.P"
        engine.wikidata.google_play_results[movie_id] = {
            "status": "no_match",
            "wikidata_ids": [],
            "result_count": 0,
        }
        engine.google_play.pages[movie_id] = {
            "provider": "google_play_movie_page",
            "status": "ok",
            "google_play_id": movie_id,
            "title": "Breathless",
            "description": (
                "A small time crook, Michel Poiccard, chased by the police after "
                "stealing a car and fleeing to Italy."
            ),
            "year": 1961,
            "runtime_minutes": 90,
            "directors": [],
            "cast": [],
        }
        details = movie_details(269, "tt0053472", "Breathless")
        details["release_date"] = "1960-03-16"
        details["runtime"] = 130
        details["overview"] = (
            "A small time thief, Michel Poiccard, steals a car and flees to Italy."
        )
        details["credits"]["cast"] = [
            {"id": 1, "name": "Jean-Paul Belmondo", "character": "Michel Poiccard", "order": 0}
        ]
        engine.tmdb.records[269] = details
        engine.tmdb.search_pages[("Breathless", 1)] = {
            "results": [{"id": 269, "title": "Breathless", "original_title": "Breathless"}],
            "total_pages": 1,
            "total_results": 1,
        }

        candidate, match, evidence = engine._find_movie([
            {"url": f"https://play.google.com/store/movies/details?id={movie_id}"},
        ])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(
            evidence["google_play_metadata_resolution"]["status"],
            "no_verified_tmdb_metadata_match",
        )
        self.assertIn(
            "runtime_difference_over_8_minutes",
            evidence["google_play_metadata_resolution"]["candidate_matches"][0]["page_comparisons"][0]["conflicts"],
        )


    def test_google_play_title_slug_does_not_trigger_wikidata_lookup(self):
        engine = self.make_engine()

        candidate, match, evidence = engine._find_movie([{
            "url": "https://play.google.com/store/movies/details/Breathless",
            "title": "Breathless (1960)",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(engine.wikidata.google_play_calls, [])
        self.assertEqual(
            evidence["google_play_resolution"]["status"],
            "no_google_play_id",
        )

    def test_extract_google_play_ids_ignores_path_slugs_and_accepts_id_parameter(self):
        rows = [
            {"url": "https://play.google.com/store/movies/details/Breathless"},
            {
                "url": (
                    "https://play.google.com/store/movies/details/"
                    "Breathless?id=KuBCXRtFvO8&hl=en"
                )
            },
            {"lens_identifiers": {"google_play_ids": ["KuBCXRtFvO8"]}},
            {"title": "KuBCXRtFvO8"},
        ]

        self.assertEqual(extract_google_play_ids(rows), ["KuBCXRtFvO8"])

    def test_extract_google_play_ids_accepts_period_p_suffix_but_not_path_slug(self):
        rows = [
            {"url": "https://play.google.com/store/movies/details/Breathless"},
            {"url": "https://play.google.com/store/movies/details?id=D1_Pzd4ayj0.P"},
            {"url": "https://play.google.com/store/movies/details?id=eDqHf0j8GUc.P"},
            {"title": "D1_Pzd4ayj0.P"},
        ]

        self.assertEqual(
            extract_google_play_ids(rows),
            ["D1_Pzd4ayj0.P", "eDqHf0j8GUc.P"],
        )

    def test_wikidata_qid_with_multiple_tmdb_movie_results_stays_review(self):
        engine = self.make_engine()
        engine.tmdb.wikidata_find_results["Q12345"] = {
            "movie_results": [{"id": 10754}, {"id": 31056}],
            "tv_results": [],
        }

        candidate, match, evidence = engine._find_movie([{
            "lens_identifiers": {"wikidata_ids": ["Q12345"]},
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(
            evidence["wikidata_resolution"]["status"],
            "ambiguous_tmdb_movie_results",
        )
        self.assertEqual(match["candidate_ids"], [10754, 31056])
        self.assertEqual(engine.tmdb.details_calls, [])

    def test_asin_google_play_and_google_knowledge_graph_mid_never_create_identity(self):
        engine = self.make_engine()

        candidate, match, evidence = engine._find_movie([{
            "title": "Breathless (1960)",
            "lens_identifiers": {
                "asin_ids": ["B003PHJLQY"],
                "google_play_ids": ["com.example.breathless"],
                "google_knowledge_graph_ids": ["kg:/m/0dl567"],
            },
            "url": "https://www.amazon.co.uk/dp/B003PHJLQY",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(engine.tmdb.wikidata_find_calls, [])
        self.assertEqual(engine.tmdb.find_calls, [])
        self.assertEqual(engine.tmdb.details_calls, [])
        self.assertEqual(evidence["wikidata_resolution"]["status"], "no_wikidata_id")

    def test_tmdb_client_wikidata_find_uses_external_source_and_validates_qid(self):
        client = TMDBClient.__new__(TMDBClient)
        client.language = "en-US"
        client._get = Mock(return_value={"movie_results": [{"id": 10754}]})

        result = client.find_by_wikidata_id("q12345")

        self.assertEqual(result["movie_results"][0]["id"], 10754)
        client._get.assert_called_once_with(
            "/find/Q12345",
            {"external_source": "wikidata_id", "language": "en-US"},
        )
        with self.assertRaises(ValueError):
            client.find_by_wikidata_id("B003PHJLQY")

    def test_official_imdb_title_id_resolves_via_tmdb_find_without_title_search(self):
        engine = self.make_engine()

        candidate, match, evidence = engine._find_movie([{
            "title": "Wrong Lens title and year must be ignored",
            "url": "https://www.imdb.com/title/tt0095468/",
            "resolved_url": "https://www.imdb.com/title/tt0095468/",
        }])

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 10754)
        self.assertEqual(match["imdb_id"], "tt0095468")
        self.assertEqual(match["identity_method"], "lens_imdb_id_tmdb_find")
        self.assertEqual(evidence["tmdb_ids"], [10754])
        self.assertEqual(evidence["imdb_ids"], ["tt0095468"])
        self.assertEqual(evidence["id_resolution"]["imdb_to_tmdb_ids"], {"tt0095468": [10754]})
        self.assertEqual(engine.tmdb.find_calls, ["tt0095468"])
        self.assertEqual(engine.tmdb.details_calls, [10754])

    def test_extracts_imdb_id_from_official_media_viewer_url(self):
        url = "https://www.imdb.com/title/tt0053472/mediaviewer/rm567149056/"
        self.assertEqual(extract_imdb_ids([{"url": url}]), ["tt0053472"])

    def test_imdb_news_subpages_are_not_accepted_as_title_identity(self):
        url = "https://www.imdb.com/title/tt0053472/news/"
        self.assertEqual(extract_imdb_ids([{"url": url}]), [])

    def test_official_imdb_id_without_tmdb_movie_match_stays_in_review(self):
        engine = self.make_engine()
        engine.tmdb.find_results["tt0000000"] = {"movie_results": []}

        candidate, match, evidence = engine._find_movie([{
            "url": "https://www.imdb.com/title/tt0000000/",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertIn("could not be resolved", match["reason"])
        self.assertEqual(evidence["tmdb_ids"], [])
        self.assertEqual(engine.tmdb.find_calls, ["tt0000000"])
        self.assertEqual(engine.tmdb.details_calls, [])


    def test_direct_lens_tmdb_id_stays_first_priority_when_imdb_is_also_present(self):
        engine = self.make_engine()
        engine.tmdb.find_results["tt0095468"] = {"movie_results": [{"id": 31056}]}

        candidate, match, evidence = engine._find_movie([
            {"url": "https://www.themoviedb.org/movie/10754"},
            {"url": "https://www.imdb.com/title/tt0095468/"},
        ])

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 10754)
        self.assertEqual(engine.tmdb.details_calls, [10754])
        self.assertEqual(engine.tmdb.find_calls, ["tt0095468"])
        self.assertEqual(evidence["id_resolution"]["ignored_secondary_lens_imdb_ids"], [])

    def test_lens_imdb_id_fallback_is_used_when_direct_tmdb_id_cannot_be_verified(self):
        engine = self.make_engine()

        candidate, match, evidence = engine._find_movie([
            {"url": "https://www.themoviedb.org/movie/99999"},
            {"url": "https://www.imdb.com/title/tt0095468/"},
        ])

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 10754)
        self.assertEqual(match["imdb_id"], "tt0095468")
        self.assertEqual(match["identity_source"], "google_lens_official_imdb_id_via_tmdb_find")
        self.assertEqual(engine.tmdb.find_calls, ["tt0095468"])
        self.assertEqual(engine.tmdb.details_calls, [99999, 10754])
        self.assertEqual(evidence["tmdb_id_lookup"]["status"], "resolved_via_imdb_fallback")

    def test_title_only_result_does_not_call_tmdb_text_search(self):
        engine = self.make_engine()

        candidate, match, _ = engine._find_movie([{
            "title": "Buladó (2020)",
            "url": "https://example.test/film/bulado",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(engine.tmdb.details_calls, [])


    def test_first_ranked_lens_tmdb_id_is_used_and_secondary_ids_are_recorded(self):
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

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 10754)
        self.assertEqual(engine.tmdb.details_calls, [10754])
        self.assertEqual(evidence["id_resolution"]["observed_lens_tmdb_ids"], [10754, 31056])
        self.assertEqual(evidence["id_resolution"]["ignored_secondary_lens_tmdb_ids"], [31056])


    def test_lens_caption_without_movie_identifier_stays_review_without_search(self):
        engine = self.make_engine()
        searcher = Mock(return_value=[
            {"url": "https://www.imdb.com/title/tt0095468/"},
        ])
        engine.reverse_image = SimpleNamespace(
            resolve_result_urls=lambda rows: rows,
            search_official_movie_pages_from_lens=searcher,
            last_official_search_diagnostics={"status": "not_run", "query_count": 0},
        )

        candidate, match, evidence = engine._find_movie([{
            "title": "A Short Film About Killing (1988)",
            "url": "https://play.google.com/store/movies/details/a_short_film_about_killing",
        }])

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(evidence["lens_title_resolution"]["status"], "disabled_id_only_mode")
        self.assertEqual(evidence["official_title_search"]["status"], "disabled_id_only_mode")
        searcher.assert_not_called()
        self.assertEqual(engine.tmdb.details_calls, [])


    def test_only_first_official_imdb_id_from_lens_is_resolved_by_tmdb(self):
        engine = self.make_engine()
        apollo = movie_details(
            664996,
            "tt7978758",
            "Apollo 10½: A Space Age Childhood",
        )
        apollo["release_date"] = "2022-04-01"
        engine.tmdb.records[664996] = apollo
        engine.tmdb.find_results["tt7978758"] = {
            "movie_results": [{"id": 664996}]
        }

        candidate, match, evidence = engine._find_movie([
            {"url": "https://www.imdb.com/title/tt7978758/"},
            {"url": "https://www.imdb.com/title/tt28422574/"},
        ])

        self.assertEqual(candidate["id"], 664996)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["imdb_id"], "tt7978758")
        self.assertEqual(engine.tmdb.find_calls, ["tt7978758"])
        self.assertEqual(evidence["id_resolution"]["observed_lens_imdb_ids"], [
            "tt7978758", "tt28422574"
        ])


    def test_source_folder_label_is_not_part_of_lens_id_resolution(self):
        engine = self.make_engine()

        candidate, match, evidence = engine._find_movie(
            [{"url": "https://www.themoviedb.org/movie/10754"}],
            expected_folder="عنوان محلي مختلف (1988)",
        )

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        consistency = evidence["identity_consistency"]
        self.assertEqual(consistency["status"], "not_applied_id_only_mode")
        self.assertTrue(consistency["allowed"])
        self.assertFalse(consistency["folder_name_used_for_conflict_veto"])

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
        self.assertEqual(evidence["tmdb_id_lookup"]["status"], "no_tmdb_id")
        self.assertNotIn("queries", evidence["tmdb_id_lookup"])
        self.assertEqual(engine.tmdb.details_calls, [])


    def test_record_saves_tmdb_details_and_tmdb_poster_urls(self):
        engine = self.make_engine()
        engine._reverse_image_results = lambda _image_url: [{
            "title": "Wrong Lens title must not affect the identity",
            "url": "https://www.themoviedb.org/movie/10754",
            "resolved_url": "https://www.themoviedb.org/movie/10754",
        }]

        record = engine._record_for_folder({
            "folder": "A Short Film About Killing (1988)",
            "image_path": "Posters/poster.jpg",
        })

        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["tmdb_id"], 10754)
        self.assertEqual(record["imdb_id"], "tt0095468")
        self.assertEqual(record["tmdb"]["title"], "A Short Film About Killing")
        self.assertEqual(record["tmdb"]["poster_url"], "https://image.tmdb.org/t/p/w500/primary-poster.jpg")
        self.assertEqual(record["tmdb"]["posters"][0]["url"], "https://image.tmdb.org/t/p/w500/poster-a.jpg")
        self.assertEqual(record["tmdb"]["posters"][0]["original_url"], "https://image.tmdb.org/t/p/original/poster-a.jpg")
        self.assertEqual(record["identity"]["source"], "google_lens_official_tmdb_movie_id")
        self.assertFalse(record["original"]["folder_name_used_for_matching"])
        consistency = record["reverse_image"]["evidence"]["identity_consistency"]
        self.assertEqual(consistency["status"], "not_applied_id_only_mode")
        self.assertFalse(consistency["folder_name_used_for_search"])
        self.assertFalse(consistency["folder_name_used_for_conflict_veto"])


    def test_wrong_folder_label_does_not_override_lens_imdb_id_verified_by_tmdb(self):
        engine = self.make_engine()
        wrong_movie = movie_details(634, "tt0243155", "Bridget Jones's Diary")
        wrong_movie["release_date"] = "2001-04-13"
        engine.tmdb.records[634] = wrong_movie
        engine.tmdb.find_results["tt0243155"] = {"movie_results": [{"id": 634}]}

        candidate, match, evidence = engine._find_movie(
            [{"url": "https://www.imdb.com/title/tt0243155/"}],
            expected_folder="Buladó (2020)",
        )

        self.assertEqual(candidate["id"], 634)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(match["tmdb_id"], 634)
        self.assertEqual(evidence["identity_consistency"]["status"], "not_applied_id_only_mode")


    def test_localized_folder_label_is_not_used_to_veto_lens_id(self):
        engine = self.make_engine()
        apollo = movie_details(664996, "tt7978758", "Apollo 10½: A Space Age Childhood")
        apollo["release_date"] = "2022-04-01"
        engine.tmdb.records[664996] = apollo
        engine.tmdb.find_results["tt7978758"] = {"movie_results": [{"id": 664996}]}

        candidate, match, evidence = engine._find_movie(
            [{"url": "https://www.imdb.com/title/tt7978758/"}],
            expected_folder="Apollo 101⁄2 A Space Age Childhood (2022)",
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["id"], 664996)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(evidence["identity_consistency"]["status"], "not_applied_id_only_mode")

    def test_reviewer_only_research_is_skipped_by_default_to_keep_batch_fast(self):
        engine = self.make_engine()
        engine._reverse_image_results = lambda _image_url: [{
            "title": "Some useful Lens title",
            "url": "https://play.google.com/store/movies/details/some_useful_lens_title",
        }]
        engine._wikipedia_tmdb_suggestions_from_lens = Mock(
            side_effect=AssertionError("reviewer-only sources must be opt-in")
        )

        record = engine._record_for_folder({
            "folder": "Some useful Lens title",
            "image_path": "Posters/example.jpg",
        })

        self.assertEqual(record["status"], "review")
        engine._wikipedia_tmdb_suggestions_from_lens.assert_not_called()
        self.assertEqual(
            record["reverse_image"]["latest_query"]["diagnostics"]["wikipedia_status"],
            "disabled_by_configuration",
        )
        self.assertEqual(
            record["reverse_image"]["latest_query"]["diagnostics"]["wikidata_status"],
            "disabled_by_configuration",
        )


    def test_lens_failure_keeps_google_search_disabled(self):
        engine = self.make_engine()
        engine.reverse_image.last_lens_diagnostics = {
            "status": "error",
            "result_count": 0,
        }
        engine.reverse_image.last_official_search_diagnostics = {
            "status": "ok",
            "query_count": 1,
            "title_hints": ["Biz Size Döneriz"],
            "candidate_domains": {"themoviedb.org": 2},
            "official_url_count": 2,
        }
        engine._reverse_image_results = lambda _image_url: [{
            "error": "BrightDataAPIError",
            "message": "Lens returned an empty response",
        }]

        record = engine._record_for_folder({
            "folder": "Breathless (1960)",
            "image_path": "Posters/breathless.jpg",
        })

        diagnostics = record["reverse_image"]["latest_query"]["diagnostics"]
        self.assertEqual(diagnostics["title_search_status"], "disabled_id_only_mode")
        self.assertEqual(diagnostics["title_search_query_count"], 0)
        self.assertEqual(diagnostics["title_search_hints"], [])
        self.assertEqual(diagnostics["title_search_candidate_domains"], {})

    def test_tmdb_url_diagnostics_count_only_official_movie_destinations(self):
        diagnostics = tmdb_movie_url_diagnostics([
            {
                "url": "https://www.themoviedb.org/movie/10754",
                "resolved_urls": [
                    "https://www.themoviedb.org/movie/10754",
                    "https://www.themoviedb.org/movie/10754-a-short-film",
                    "https://www.imdb.com/title/tt0095468/",
                ],
            },
            {
                "url": "https://evil.example/movie/31056",
                "resolved_urls": ["https://evil.example/movie/31056"],
            },
            {
                "error": "provider failure",
                "url": "https://www.themoviedb.org/movie/99999",
            },
        ])

        self.assertEqual(diagnostics["official_tmdb_url_count"], 2)
        self.assertEqual(diagnostics["official_tmdb_ids"], [10754])


    def test_extracts_tmdb_id_from_google_imgres_imgrefurl(self):
        results = [{
            "url": (
                "https://www.google.com/imgres?"
                "imgurl=https%3A%2F%2Fimages.example%2Fposter.jpg&"
                "imgrefurl=https%3A%2F%2Fwww.themoviedb.org%2Fmovie%2F10754"
            )
        }]
        self.assertEqual(extract_tmdb_movie_ids(results), [10754])

    def test_does_not_trust_google_imgres_image_asset_as_page_identity(self):
        results = [{
            "url": (
                "https://www.google.com/imgres?"
                "imgurl=https%3A%2F%2Fwww.themoviedb.org%2Fmovie%2F10754%2Fposter.jpg&"
                "imgrefurl=https%3A%2F%2Fevil.example%2Fmovie%2F123"
            )
        }]
        self.assertEqual(extract_tmdb_movie_ids(results), [])

    def test_unwraps_nested_google_url_and_imgres_wrappers(self):
        results = [{
            "resolved_urls": [
                "https://www.google.com/url?q=https%3A%2F%2Fwww.google.com%2Fimgres%3Fimgrefurl%3Dhttps%253A%252F%252Fwww.themoviedb.org%252Fmovie%252F10754"
            ]
        }]
        self.assertEqual(extract_tmdb_movie_ids(results), [10754])

    def test_visual_matches_official_imdb_id_is_ignored_for_movie_identity(self):
        engine = self.make_engine()
        rows = [{
            "lens_tab": "visual_matches",
            "title": "A visually similar but unrelated movie",
            "resolved_url": "https://www.imdb.com/title/tt0095468/",
        }]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertIsNone(candidate)
        self.assertEqual(match["status"], "review")
        self.assertEqual(engine.tmdb.find_calls, [])
        self.assertEqual(engine.tmdb.details_calls, [])
        self.assertEqual(
            evidence["id_resolution"]["visual_matches_identifiers_ignored"]["imdb_ids"],
            ["tt0095468"],
        )
        self.assertEqual(
            evidence["id_resolution"]["identity_identifier_source"],
            "exact_matches_only",
        )

    def test_exact_google_play_id_is_not_conflicted_by_visual_match_id(self):
        engine = self.make_engine()
        engine.tmdb.records[10754]["external_ids"]["wikidata_id"] = "Q12345"
        rows = [
            {
                "lens_tab": "exact_matches",
                "url": "https://play.google.com/store/movies/details?id=KuBCXRtFvO8",
            },
            {
                "lens_tab": "visual_matches",
                "url": "https://play.google.com/store/movies/details?id=D1_Pzd4ayj0.P",
            },
        ]

        candidate, match, evidence = engine._find_movie(rows)

        self.assertEqual(candidate["id"], 10754)
        self.assertEqual(match["status"], "ready")
        self.assertEqual(engine.wikidata.google_play_calls, ["KuBCXRtFvO8"])
        self.assertEqual(
            evidence["google_play_resolution"]["google_play_ids_seen"],
            ["KuBCXRtFvO8"],
        )
        self.assertEqual(
            evidence["id_resolution"]["visual_matches_identifiers_ignored"]["google_play_ids"],
            ["D1_Pzd4ayj0.P"],
        )

    def test_persistent_reject_element_after_geo_retry_stops_batch(self):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine._stop_batch = False
        engine._provider_stop_status = None
        engine._provider_stop_reason = None
        engine.reverse_image = SimpleNamespace(
            last_lens_diagnostics={
                "http_status": 200,
                "provider_error_code": "reject_element",
                "provider_error_retries": 1,
                "provider_retry_country": "us",
            },
            search_by_image_url=Mock(
                side_effect=RuntimeError(
                    "Bright Data rejected Google Lens extraction after one us geo retry"
                )
            ),
        )

        rows = engine._reverse_image_results("https://example.test/poster.jpg")

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["stop_batch"])
        self.assertTrue(engine._stop_batch)
        self.assertEqual(engine._provider_stop_status, 200)
        self.assertEqual(
            engine._provider_stop_reason,
            "brightdata_reject_element_after_geo_retry",
        )

    def test_reject_element_before_geo_retry_does_not_trip_provider_circuit_breaker(self):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine._stop_batch = False
        engine._provider_stop_status = None
        engine._provider_stop_reason = None
        engine.reverse_image = SimpleNamespace(
            last_lens_diagnostics={
                "http_status": 200,
                "provider_error_code": "reject_element",
                "provider_error_retries": 0,
            },
            search_by_image_url=Mock(
                side_effect=RuntimeError("initial reject element")
            ),
        )

        rows = engine._reverse_image_results("https://example.test/poster.jpg")

        self.assertFalse(rows[0].get("stop_batch", False))
        self.assertFalse(engine._stop_batch)
        self.assertIsNone(engine._provider_stop_reason)


if __name__ == "__main__":
    unittest.main()
