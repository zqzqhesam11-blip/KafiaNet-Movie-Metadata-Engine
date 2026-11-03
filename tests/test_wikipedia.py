from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.wikipedia import WikipediaClient


class WikipediaClientTests(unittest.TestCase):
    def make_client(self):
        client = WikipediaClient.__new__(WikipediaClient)
        client.language = "en"
        client.api_url = "https://en.wikipedia.org/w/api.php"
        client.client = Mock()
        return client

    def test_searches_lens_titles_and_extracts_movie_year_from_wikipedia_lead(self):
        client = self.make_client()
        search_response = Mock()
        search_response.json.return_value = {
            "query": {
                "search": [
                    {
                        "pageid": 123,
                        "title": "A Short Film About Killing",
                        "snippet": "1988 Polish crime drama film",
                    }
                ]
            }
        }
        page_response = Mock()
        page_response.json.return_value = {
            "query": {
                "pages": [
                    {
                        "pageid": 123,
                        "title": "A Short Film About Killing",
                        "extract": "A Short Film About Killing is a 1988 Polish crime drama film directed by Krzysztof Kieślowski.",
                        "categories": [{"title": "1988 films"}],
                    }
                ]
            }
        }
        client.client.get.side_effect = [search_response, page_response]

        result = client.search_articles(["A Short Film About Killing", "A Short Film About Killing"])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["query_count"], 1)
        self.assertEqual(result["article_count"], 1)
        article = result["articles"][0]
        self.assertEqual(article["pageid"], 123)
        self.assertEqual(article["year"], 1988)
        self.assertTrue(article["is_movie_article"])
        self.assertEqual(article["url"], "https://en.wikipedia.org/?curid=123")
        self.assertEqual(client.client.get.call_count, 2)
        self.assertIn("pageids", client.client.get.call_args.kwargs["params"])

    def test_ambiguous_years_in_lead_are_not_guessed(self):
        year, candidates = WikipediaClient._article_year(
            "Example Film",
            "Example Film is a 1988 film and a 2001 restored release.",
        )
        self.assertIsNone(year)
        self.assertEqual(candidates, [1988, 2001])

    def test_year_can_be_read_from_wikipedia_disambiguation_title(self):
        year, candidates = WikipediaClient._article_year(
            "Example Film (1988 film)",
            "Example Film is a Polish drama film.",
        )
        self.assertEqual(year, 1988)
        self.assertEqual(candidates, [1988])

    def test_wikipedia_errors_are_reported_without_raising(self):
        client = self.make_client()
        response = Mock()
        response.raise_for_status.side_effect = RuntimeError("upstream unavailable")
        client.client.get.return_value = response

        result = client.search_articles(["Example Film"])

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["article_count"], 0)
        self.assertEqual(result["errors"][0]["query"], "Example Film")



if __name__ == "__main__":
    unittest.main()
