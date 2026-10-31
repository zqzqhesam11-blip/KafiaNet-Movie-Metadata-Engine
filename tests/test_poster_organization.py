from __future__ import annotations

import unittest

from app.github_api import GitHubAPI


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeTreeClient:
    def __init__(self):
        self.calls = []
        self.patched = None

    def post(self, path, json):
        self.calls.append((path, json))
        if path == "/git/blobs":
            return FakeResponse({"sha": "json-blob"})
        if path == "/git/trees":
            return FakeResponse({"sha": "new-tree"})
        if path == "/git/commits":
            return FakeResponse({"sha": "new-commit"})
        raise AssertionError(f"Unexpected POST path: {path}")

    def patch(self, path, json):
        self.patched = (path, json)
        return FakeResponse({"object": {"sha": "new-commit"}})


class PosterOrganizationTests(unittest.TestCase):
    def make_api(self, tree):
        api = GitHubAPI.__new__(GitHubAPI)
        api.recursive_tree = lambda: tree
        return api

    def test_lists_root_and_review_images_but_not_ready_images(self):
        tree = [
            {
                "type": "blob",
                "path": "Film A/poster.jpg",
                "sha": "root-jpg",
                "mode": "100644",
            },
            {
                "type": "blob",
                "path": "Film A/alternate.png",
                "sha": "root-png",
                "mode": "100644",
            },
            {
                "type": "blob",
                "path": "Film A/notes.txt",
                "sha": "root-notes",
                "mode": "100644",
            },
            {
                "type": "blob",
                "path": "مراجعة/Film B/poster.webp",
                "sha": "review-webp",
                "mode": "100644",
            },
            {
                "type": "blob",
                "path": "مراجعة/Film B/Film B.json",
                "sha": "review-json",
                "mode": "100644",
            },
            {
                "type": "blob",
                "path": "جاهز/Film C/poster.jpg",
                "sha": "ready-jpg",
                "mode": "100644",
            },
        ]
        folders = self.make_api(tree).list_film_folders()
        by_name = {row["folder"]: row for row in folders}

        self.assertEqual(set(by_name), {"Film A", "Film B"})
        self.assertEqual(by_name["Film A"]["source_prefix"], "Film A")
        self.assertEqual(len(by_name["Film A"]["poster_files"]), 3)
        self.assertEqual(
            by_name["Film A"]["image_path"],
            "Film A/alternate.png",
        )
        self.assertEqual(by_name["Film B"]["source_prefix"], "مراجعة/Film B")
        self.assertEqual(
            by_name["Film B"]["poster_files"][0]["sha"],
            "review-webp",
        )

    def test_commit_moves_existing_image_blob_without_reuploading_it(self):
        api = GitHubAPI.__new__(GitHubAPI)
        api._branch_commit = lambda: ("old-head", "base-tree")
        api.client = FakeTreeClient()

        result = api.commit_files(
            files={"جاهز/Film A/Film A (2020).json": "{}"},
            deletes=["Film A/poster.jpg"],
            moves={
                "جاهز/Film A/poster.jpg": {
                    "sha": "original-image-blob",
                    "mode": "100644",
                }
            },
        )

        self.assertEqual(result, "new-commit")
        tree_call = next(call for call in api.client.calls if call[0] == "/git/trees")
        entries = tree_call[1]["tree"]
        image_entry = next(
            entry for entry in entries
            if entry["path"] == "جاهز/Film A/poster.jpg"
        )
        delete_entry = next(
            entry for entry in entries
            if entry["path"] == "Film A/poster.jpg"
        )
        self.assertEqual(image_entry["sha"], "original-image-blob")
        self.assertEqual(image_entry["type"], "blob")
        self.assertIsNone(delete_entry["sha"])
        self.assertEqual(
            len([call for call in api.client.calls if call[0] == "/git/blobs"]),
            1,
        )

    def test_rejects_a_move_that_conflicts_with_a_json_file(self):
        api = GitHubAPI.__new__(GitHubAPI)
        api._branch_commit = lambda: ("old-head", "base-tree")
        api.client = FakeTreeClient()

        with self.assertRaisesRegex(ValueError, "Conflicting Git tree operation"):
            api.commit_files(
                files={"same/path.json": "{}"},
                moves={"same/path.json": {"sha": "blob"}},
            )


if __name__ == "__main__":
    unittest.main()
