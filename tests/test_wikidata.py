from __future__ import annotations

import unittest

import httpx

from app.wikidata import WikidataClient


class WikidataClientTests(unittest.TestCase):
    def make_client(self, handler):
        client = WikidataClient.__new__(WikidataClient)
        client.client = httpx.Client(
            transport=httpx.MockTransport(handler),
            headers={"User-Agent": "KafiaNet-test"},
        )
        return client

    def test_searches_film_entities_and_extracts_cross_references(self):
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if params.get("action") == "wbsearchentities":
                return httpx.Response(200, json={
                    "search": [{
                        "id": "Q123",
                        "label": "A Short Film About Killing",
                        "description": "1988 Polish film directed by Krzysztof Kieślowski",
                    }]
                })
            if params.get("action") == "wbgetentities" and params.get("props") == "labels":
                return httpx.Response(200, json={
                    "entities": {
                        "Q456": {"labels": {"en": {"value": "Krzysztof Kieślowski"}}}
                    }
                })
            if params.get("action") == "wbgetentities":
                return httpx.Response(200, json={
                    "entities": {
                        "Q123": {
                            "labels": {"en": {"value": "A Short Film About Killing"}},
                            "descriptions": {"en": {"value": "1988 Polish film"}},
                            "aliases": {"en": [{"value": "Krótki film o zabijaniu"}]},
                            "sitelinks": {
                                "enwiki": {"title": "A Short Film About Killing"}
                            },
                            "claims": {
                                "P31": [{
                                    "mainsnak": {"datavalue": {"value": {"id": "Q11424"}}}
                                }],
                                "P577": [{
                                    "mainsnak": {"datavalue": {"value": {"time": "+1988-03-11T00:00:00Z"}}}
                                }],
                                "P57": [{
                                    "mainsnak": {"datavalue": {"value": {"id": "Q456"}}}
                                }],
                                "P345": [{
                                    "mainsnak": {"datavalue": {"value": "tt0095468"}}
                                }],
                                "P4947": [{
                                    "mainsnak": {"datavalue": {"value": "10754"}}
                                }],
                            },
                        }
                    }
                })
            return httpx.Response(400, json={"error": "unexpected request"})

        client = self.make_client(handler)
        try:
            output = client.search_films(["A Short Film About Killing"])
        finally:
            client.close()

        self.assertEqual(output["status"], "suggestions_only")
        self.assertEqual(output["candidate_count"], 1)
        candidate = output["candidates"][0]
        self.assertEqual(candidate["wikidata_id"], "Q123")
        self.assertEqual(candidate["year"], 1988)
        self.assertEqual(candidate["imdb_ids"], ["tt0095468"])
        self.assertEqual(candidate["tmdb_ids"], ["10754"])
        self.assertEqual(candidate["directors"][0]["name"], "Krzysztof Kieślowski")
        self.assertFalse(output["identity_evidence"])
        self.assertFalse(output["automatic_match_allowed"])

    def test_ignores_non_film_entities_and_handles_empty_hints(self):
        client = self.make_client(
            lambda request: httpx.Response(200, json={
                "search": [{
                    "id": "Q123",
                    "label": "Example",
                    "description": "example concept",
                }]
            }) if dict(request.url.params).get("action") == "wbsearchentities"
            else httpx.Response(200, json={
                "entities": {
                    "Q123": {
                        "labels": {"en": {"value": "Example"}},
                        "descriptions": {"en": {"value": "example concept"}},
                        "claims": {"P31": [{
                            "mainsnak": {"datavalue": {"value": {"id": "Q5"}}}
                        }]},
                    }
                }
            })
        )
        try:
            empty = client.search_films(["", "  "])
            result = client.search_films(["Example"])
        finally:
            client.close()

        self.assertEqual(empty["status"], "no_title_hints")
        self.assertEqual(result["status"], "no_film_entities")
        self.assertEqual(result["candidate_count"], 0)


if __name__ == "__main__":
    unittest.main()
