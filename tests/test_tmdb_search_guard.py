from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.tmdb import TMDBClient


class TMDBSearchQueryGuardTests(unittest.TestCase):
    def make_client(self):
        client = TMDBClient.__new__(TMDBClient)
        client.language = "en-US"
        client.client = Mock()
        return client

    def test_rejects_long_caption_without_calling_tmdb(self):
        client = self.make_client()
        long_caption = (
            "caption corrects spelling of surname to cecchettin director and actress "
            "paola cortellesi attends an interview with the associated press in rome "
        ) * 8

        self.assertEqual(client.search_movies(long_caption, 2023), [])
        client.client.get.assert_not_called()

    def test_collapses_whitespace_for_normal_movie_title(self):
        client = self.make_client()
        response = Mock()
        response.json.return_value = {"results": [{"id": 123}]}
        client.client.get.return_value = response

        result = client.search_movies("  There’s   Still\nTomorrow  ", 2023)

        self.assertEqual(result, [{"id": 123}])
        kwargs = client.client.get.call_args.kwargs
        self.assertEqual(kwargs["params"]["query"], "There’s Still Tomorrow")
        self.assertEqual(kwargs["params"]["year"], 2023)


if __name__ == "__main__":
    unittest.main()
