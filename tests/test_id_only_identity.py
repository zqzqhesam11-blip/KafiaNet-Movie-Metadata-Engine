from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.engine import MetadataEngine
from app.tmdb import TMDBClient
from app.image_evidence import extract_imdb_ids, extract_tmdb_movie_ids, tmdb_movie_url_diagnostics


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

    def find_by_imdb_id(self, imdb_id: str):
        self.find_calls.append(imdb_id)
        return self.find_results.get(imdb_id, {"movie_results": []})

    def movie_details(self, tmdb_id: int):
        self.details_calls.append(int(tmdb_id))
        return self.records[int(tmdb_id)]

    @staticmethod
    def normalize_details(details: dict):
        return TMDBClient.normalize_details(details)

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

if __name__ == "__main__":
    unittest.main()
