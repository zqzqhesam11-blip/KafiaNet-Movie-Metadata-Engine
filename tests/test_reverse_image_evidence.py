from __future__ import annotations

import unittest

from app.image_evidence import (
    extract_reverse_image_evidence,
    extract_tmdb_movie_ids,
    imdb_id_from_url,
    tmdb_movie_id_from_url,
)


class ReverseImageEvidenceTests(unittest.TestCase):
    def test_extracts_movie_title_from_image_filename(self):
        evidence = extract_reverse_image_evidence([
            {
                "title": "The.Matrix.1999.1080p.POSTER.jpg",
                "url": "https://example.com/gallery",
                "source": "https://cdn.example.com/art/the-matrix-cover.jpg",
            }
        ])
        self.assertIn("the matrix", [x.casefold() for x in evidence["title_hints"]])
        self.assertEqual(evidence["imdb_ids"], [])
        self.assertEqual(evidence["tmdb_ids"], [])

    def test_skips_provider_error_records_without_empty_backlinks(self):
        evidence = extract_reverse_image_evidence([
            {"error": "BrightDataAPIError", "message": "response not understood"},
        ])
        self.assertEqual(evidence["backlinks"], [])
        self.assertEqual(evidence["title_hints"], [])
        self.assertEqual(evidence["imdb_ids"], [])

    def test_preserves_numeric_title_and_connector_words(self):
        evidence = extract_reverse_image_evidence([
            {
                "title": "2001.A.Space.Odyssey.1968.POSTER.jpg",
                "url": "https://example.com/movie",
                "source": "",
            },
            {
                "title": "The.Lord.of.the.Rings.The.Fellowship.of.the.Ring.jpg",
                "url": "https://example.com/movie",
                "source": "",
            },
        ])
        self.assertEqual(
            evidence["title_hints"],
            [
                "2001 A Space Odyssey",
                "The Lord of the Rings The Fellowship of the Ring",
            ],
        )

    def test_preserves_short_numeric_movie_title(self):
        evidence = extract_reverse_image_evidence([
            {"title": "1917.POSTER.jpg", "url": "https://example.com/movie", "source": ""}
        ])
        self.assertEqual(evidence["title_hints"], ["1917"])

    def test_only_lens_result_title_is_used_for_tmdb_search(self):
        evidence = extract_reverse_image_evidence([{
            "title": "A Short Film About Killing (1988) - IMDb",
            "url": "https://example.com/result",
            "source": "https://cdn.example.com/UNRELATED-IMAGE-NAME-poster.jpg",
        }])

        self.assertEqual(
            evidence["lens_title_queries"],
            ["A Short Film About Killing"],
        )
        self.assertNotIn(
            "unrelated image name",
            [item.casefold() for item in evidence["lens_title_queries"]],
        )

    def test_repeated_lens_title_phrases_remove_dvd_retailer_and_person_noise(self):
        titles = [
            "Breathless Import Amazon fr DVD et Blu ray",
            "bout de souffle Breathless DVD import Dvd",
            "Tallenge Breathless A Bout De Souffle Jean Luc Godard",
            "A BOUT DE SOUFFLE JEAN LUC GODARD JEAN SEBERG JEAN PAUL",
            "Classics bout de souffle Jean Luc Godard France",
        ]
        evidence = extract_reverse_image_evidence([
            {"title": title, "url": f"https://example.com/result/{index}"}
            for index, title in enumerate(titles)
        ])

        queries = [value.casefold() for value in evidence["lens_title_queries"]]
        self.assertIn("bout de souffle", queries)
        self.assertIn("a bout de souffle", queries)
        self.assertNotIn("amazon", " ".join(queries))
        self.assertNotIn("dvd", " ".join(queries))
        self.assertLessEqual(len(queries), 5)

    def test_image_filename_does_not_become_a_lens_title_query(self):
        evidence = extract_reverse_image_evidence([{
            "title": "",
            "url": "https://example.com/result",
            "source": "https://cdn.example.com/A-Short-Film-About-Killing-poster.jpg",
        }])
        self.assertEqual(evidence["lens_title_queries"], [])

    def test_ignores_generic_image_names(self):
        evidence = extract_reverse_image_evidence([
            {"title": "poster.jpg", "url": "https://example.com", "source": ""}
        ])
        self.assertEqual(evidence["title_hints"], [])

    def test_rejects_long_news_caption_as_a_movie_title_hint(self):
        caption = (
            "caption corrects spelling of surname to cecchettin director and actress "
            "paola cortellesi attends an interview with the associated press in rome "
            "tuesday nov 21 paola cortellesi directed ancora domani theres still "
            "tomorrow as the country was transfixed by developments in giulia "
            "cecchettins murder a about a woman who endures beatings and belittling "
            "by her overbearing husband was topping the box office "
        ) * 3
        evidence = extract_reverse_image_evidence([
            {"title": caption, "url": "https://example.com/photo", "source": ""}
        ])
        self.assertEqual(evidence["title_hints"], [])

    def test_extracts_year_country_director_and_keeps_backlink_association(self):
        evidence = extract_reverse_image_evidence([{
            "title": "State-of-Emergency-2010.jpg",
            "url": "https://www.imdb.com/title/tt1705134/",
            "source": "https://cdn.example.com/poster.jpg",
            "content": "Director: Turner Clay | Country of origin: United States | Release year: 2011",
        }])
        self.assertEqual(evidence["imdb_ids"], ["tt1705134"])
        self.assertIn(2010, evidence["year_hints"])
        self.assertIn(2011, evidence["year_hints"])
        self.assertEqual(evidence["country_hints"], ["United States"])
        self.assertEqual(evidence["director_hints"], ["Turner Clay"])
        self.assertEqual(evidence["backlinks"][0]["imdb_ids"], ["tt1705134"])

    def test_extracts_lens_metadata_fingerprint_clues(self):
        evidence = extract_reverse_image_evidence([{
            "title": "Buladó (2020)",
            "url": "https://example.com/film/bulado",
            "source": "https://cdn.example.com/unrelated-poster-1999.jpg",
            "content": (
                "Country of origin: Curaçao, Netherlands | Director: Eché Janga | "
                "Cast: Tiara Richards, Everon Jackson Hooi, Felix de Rooy | "
                "Main character: Kenza | IMDb rating: 6.5/10"
            ),
        }])

        observation = evidence["metadata_observations"][0]
        self.assertEqual(observation["lens_title"], "Buladó")
        self.assertEqual(observation["years"], [2020])
        self.assertEqual(observation["countries"], ["Curaçao, Netherlands"])
        self.assertEqual(observation["directors"], ["Eché Janga"])
        self.assertEqual(observation["cast"], [
            "Tiara Richards", "Everon Jackson Hooi", "Felix de Rooy"
        ])
        self.assertIn("Kenza", observation["characters"])
        self.assertEqual(observation["ratings"], [{"source": "IMDB", "value": 6.5}])
        # The image URL filename is not treated as a year clue.
        self.assertNotIn(1999, evidence["year_hints"])

    def test_image_filename_alone_does_not_create_metadata_year(self):
        evidence = extract_reverse_image_evidence([{
            "title": "",
            "url": "https://example.com/result",
            "source": "https://cdn.example.com/Bulado-2020-poster.jpg",
        }])
        self.assertEqual(evidence["year_hints"], [])
        self.assertEqual(evidence["metadata_observations"][0]["years"], [])

    def test_extracts_imdb_id_only_from_imdb_host(self):
        self.assertEqual(
            imdb_id_from_url("https://www.imdb.com/title/tt0133093/"),
            "tt0133093",
        )
        self.assertIsNone(imdb_id_from_url("https://evil.example/tt0133093"))

    def test_current_identity_path_extracts_only_official_tmdb_movie_ids(self):
        ids = extract_tmdb_movie_ids([
            {
                "title": "Wrong film title (1900)",
                "url": "https://lens.google.com/goto?url=CAESopaque",
                "resolved_url": "https://www.themoviedb.org/movie/10754-a-film",
                "source": "https://cdn.example/another-film-poster.jpg",
                "content": "Country: France; Director: Wrong Person",
            },
            {
                "title": "IMDb result",
                "resolved_url": "https://www.imdb.com/title/tt0095468/",
            },
            {
                "title": "Spoofed TMDB path",
                "resolved_url": "https://evil.example/movie/99999",
            },
            {
                "title": "Duplicate result",
                "resolved_url": "https://www.themoviedb.org/movie/10754",
            },
        ])

        self.assertEqual(ids, [10754])

    def test_extracts_tmdb_id_only_from_tmdb_host(self):
        self.assertEqual(
            tmdb_movie_id_from_url("https://www.themoviedb.org/movie/603-the-matrix"),
            603,
        )
        self.assertIsNone(tmdb_movie_id_from_url("https://evil.example/movie/603"))

    def test_rejects_tmdb_tv_routes(self):
        self.assertIsNone(
            tmdb_movie_id_from_url("https://www.themoviedb.org/tv/603-a-show")
        )


    def test_extracts_ids_from_resolved_destinations_not_captions(self):
        evidence = extract_reverse_image_evidence([
            {
                "title": "Completely unrelated caption 2001",
                "url": "https://lens.google.com/goto?url=CAESopaque",
                "resolved_url": "https://www.imdb.com/title/tt0095468/",
            },
            {
                "title": "Another unrelated title 1990",
                "url": "https://lens.google.com/goto?url=CAESopaque2",
                "resolved_url": "https://www.themoviedb.org/movie/10754-a-short-film-about-killing",
            },
        ])

        self.assertEqual(evidence["imdb_ids"], ["tt0095468"])
        self.assertEqual(evidence["tmdb_ids"], [10754])
        self.assertEqual(evidence["backlinks"][0]["resolved_url"], "https://www.imdb.com/title/tt0095468/")

    def test_opaque_goto_token_is_not_guessed_or_decoded_as_an_id(self):
        evidence = extract_reverse_image_evidence([{
            "title": "A Short Film About Killing",
            "url": "https://lens.google.com/goto?url=CAESopaque-token",
            "source": "https://cdn.example.com/tt0095468-poster.jpg",
        }])

        self.assertEqual(evidence["imdb_ids"], [])
        self.assertEqual(evidence["tmdb_ids"], [])

    def test_extracts_ids_from_all_resolved_result_urls(self):
        evidence = extract_reverse_image_evidence([{
            "title": "Misleading Lens caption",
            "url": "https://lens.google.com/goto?url=CAESopaque-token",
            "resolved_urls": [
                "https://www.imdb.com/title/tt0095468/",
                "https://www.themoviedb.org/movie/10754",
            ],
        }])
        self.assertEqual(evidence["imdb_ids"], ["tt0095468"])
        self.assertEqual(evidence["tmdb_ids"], [10754])
        self.assertEqual(
            evidence["backlinks"][0]["resolved_urls"],
            [
                "https://www.imdb.com/title/tt0095468/",
                "https://www.themoviedb.org/movie/10754",
            ],
        )

    def test_extracts_all_official_ids_across_many_backlinks(self):
        results = []
        for index in range(1, 13):
            results.append({
                "title": f"Result {index}",
                "url": f"https://www.imdb.com/title/tt{9000000 + index}/",
            })
        evidence = extract_reverse_image_evidence(results)

        self.assertEqual(len(evidence["imdb_ids"]), 12)
        self.assertEqual(evidence["imdb_ids"][0], "tt9000001")
        self.assertEqual(evidence["imdb_ids"][-1], "tt9000012")


if __name__ == "__main__":
    unittest.main()
