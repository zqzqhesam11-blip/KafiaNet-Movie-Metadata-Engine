from __future__ import annotations

import unittest

from app.tmdb import TMDBClient


class WorldwideTitleSelectionTests(unittest.TestCase):
    def test_keeps_tmdb_primary_when_no_title_has_wide_market_coverage(self):
        result = TMDBClient.select_worldwide_title(
            {
                "title": "Dìdi (弟弟)",
                "original_title": "Dìdi (弟弟)",
                "alternative_titles": [
                    {"title": "Little Brother", "country": "US", "type": "English translation of Chinese characters"},
                    {"title": "Didi", "country": "US", "type": ""},
                    {"title": "Dìdi", "country": "US", "type": ""},
                ],
            }
        )
        self.assertEqual(result["title"], "Dìdi (弟弟)")
        self.assertEqual(result["source"], "tmdb_primary_title")

    def test_selects_a_non_english_title_reused_across_multiple_markets(self):
        result = TMDBClient.select_worldwide_title(
            {
                "title": "Life Is Beautiful",
                "original_title": "La vita è bella",
                "alternative_titles": [
                    {"title": "La vida es bella", "country": "ES", "type": ""},
                    {"title": "La vida es bella", "country": "MX", "type": ""},
                    {"title": "La vida es bella", "country": "AR", "type": ""},
                ],
            }
        )
        self.assertEqual(result["title"], "La vida es bella")
        self.assertEqual(result["source"], "tmdb_alternative_title_multi_market")
        self.assertEqual(result["market_count"], 3)
        self.assertEqual(result["markets"], ["AR", "ES", "MX"])

    def test_counts_distinct_markets_not_duplicate_rows(self):
        result = TMDBClient.select_worldwide_title(
            {
                "title": "Primary Title",
                "alternative_titles": [
                    {"title": "Common Alternative", "country": "US", "type": ""},
                    {"title": "Common Alternative", "country": "US", "type": "subtitle"},
                    {"title": "Common Alternative", "country": "GB", "type": ""},
                ],
            }
        )
        self.assertEqual(result["title"], "Primary Title")
        self.assertEqual(result["source"], "tmdb_primary_title")

    def test_keeps_primary_title_when_two_alternatives_have_equal_spread(self):
        result = TMDBClient.select_worldwide_title(
            {
                "title": "Primary Title",
                "alternative_titles": [
                    {"title": "Alternative A", "country": "US", "type": ""},
                    {"title": "Alternative A", "country": "GB", "type": ""},
                    {"title": "Alternative A", "country": "CA", "type": ""},
                    {"title": "Alternative B", "country": "FR", "type": ""},
                    {"title": "Alternative B", "country": "DE", "type": ""},
                    {"title": "Alternative B", "country": "ES", "type": ""},
                ],
            }
        )
        self.assertEqual(result["title"], "Primary Title")
        self.assertEqual(result["source"], "tmdb_primary_title_ambiguous_alternatives")

    def test_ignores_internal_working_titles(self):
        result = TMDBClient.select_worldwide_title(
            {
                "title": "Public Title",
                "alternative_titles": [
                    {"title": "Internal Code Name", "country": "US", "type": "Working Title"},
                    {"title": "Internal Code Name", "country": "GB", "type": "Working Title"},
                    {"title": "Internal Code Name", "country": "CA", "type": "Working Title"},
                ],
            }
        )
        self.assertEqual(result["title"], "Public Title")

    def test_groups_diacritic_variants_but_preserves_the_common_display_spelling(self):
        result = TMDBClient.select_worldwide_title(
            {
                "title": "Le Fabuleux Destin d'Amélie Poulain",
                "alternative_titles": [
                    {"title": "Amelie", "country": "US", "type": ""},
                    {"title": "Amélie", "country": "GB", "type": ""},
                    {"title": "Amélie", "country": "CA", "type": ""},
                ],
            }
        )
        self.assertEqual(result["title"], "Amélie")
        self.assertEqual(result["market_count"], 3)


if __name__ == "__main__":
    unittest.main()
