import unittest

from app.matching import classify_match, normalize_title, score_candidate, split_title_year, title_search_variants


class MatchingTests(unittest.TestCase):
    def test_split_year(self):
        self.assertEqual(split_title_year("Movie Name (2024)"), ("Movie Name", 2024))

    def test_parenthetical_public_title_aliases_are_searched_separately(self):
        self.assertEqual(
            title_search_variants("Breathless (À Bout de Souffle)"),
            ["Breathless (À Bout de Souffle)", "Breathless", "À Bout de Souffle"],
        )

    def test_normalize_title(self):
        self.assertEqual(normalize_title("À Bout de Souffle"), "a bout de souffle")

    def test_exact_title_and_year_is_ready(self):
        score = score_candidate(
            "The Matrix",
            1999,
            {"title": "The Matrix", "original_title": "The Matrix", "release_date": "1999-03-30"},
        )
        self.assertEqual(classify_match(score, 0.94), "ready")

    def test_missing_tmdb_year_cannot_be_auto_accepted_when_folder_has_year(self):
        score = score_candidate(
            "The Matrix",
            1999,
            {"title": "The Matrix", "original_title": "The Matrix"},
        )
        self.assertEqual(classify_match(score, 0.94), "review")

    def test_year_conflict_goes_to_review(self):
        score = score_candidate(
            "The Matrix",
            1999,
            {"title": "The Matrix", "original_title": "The Matrix", "release_date": "2003-05-15"},
        )
        self.assertEqual(classify_match(score, 0.94), "review")


if __name__ == "__main__":
    unittest.main()
