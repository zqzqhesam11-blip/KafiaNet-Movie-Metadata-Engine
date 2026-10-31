from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.searxng import SearXNGClient


class SearXNGClientTests(unittest.TestCase):
    def make_client(self, payload):
        client = SearXNGClient.__new__(SearXNGClient)
        response = Mock()
        response.json.return_value = payload
        response.raise_for_status = Mock()
        client.client = Mock()
        client.client.get.return_value = response
        client.base_url = "https://searx.example"
        return client

    def test_surfaces_tineye_suspension_reason(self):
        client = self.make_client({
            "results": [],
            "unresponsive_engines": [["tineye", "Suspended: too many requests"]],
        })

        with self.assertRaisesRegex(RuntimeError, "Suspended: too many requests"):
            client.tineye_by_image_url("https://example.com/poster.jpg")

    def test_returns_empty_when_tineye_responded_without_matches(self):
        client = self.make_client({
            "results": [],
            "unresponsive_engines": [],
        })

        self.assertEqual(
            client.tineye_by_image_url("https://example.com/poster.jpg"),
            [],
        )

    def test_returns_result_rows_when_engine_succeeds(self):
        client = self.make_client({
            "results": [{
                "title": "Movie Poster",
                "url": "https://example.com/movie",
                "source": "https://cdn.example.com/poster.jpg",
                "content": "Poster",
            }],
            "unresponsive_engines": [],
        })

        result = client.tineye_by_image_url("https://example.com/poster.jpg")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Movie Poster")


if __name__ == "__main__":
    unittest.main()
