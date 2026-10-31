from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from app.engine import MetadataEngine
from app.github_api import GitHubAPI


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

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


class FakeEngineGitHub:
    def __init__(self, folders, index):
        self.folders = folders
        self.index = index
        self.committed = None

    def list_film_folders(self):
        return self.folders

    def get_json_file(self, path):
        if path == "metadata/index.json":
            return self.index
        return None

    def raw_url(self, path):
        return "https://raw.example/" + path

    def list_folder_blobs(self, prefix):
        return []

    def commit_files(self, files, deletes=None, message="", moves=None):
        self.committed = {
            "files": dict(files),
            "deletes": list(deletes or []),
            "moves": dict(moves or {}),
            "message": message,
        }
        return "test-commit"


class PosterOrganizationTests(unittest.TestCase):
    def make_engine(self, folders, index, records):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.settings = SimpleNamespace(
            validate_for_processing=lambda apply: None,
            posters_repo="owner/posters",
            posters_branch="main",
        )
        engine.github = FakeEngineGitHub(folders, index)
        engine._record_for_folder = lambda item, previous_output_path=None: dict(records[item["folder"]])
        return engine

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
        api.branch = "main"
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

    def test_ready_film_moves_folder_and_uses_canonical_json_filename(self):
        item = {
            "folder": "Original Folder (2020)",
            "source_prefix": "Original Folder (2020)",
            "image_path": "Original Folder (2020)/poster.jpg",
            "poster_files": [
                {
                    "path": "Original Folder (2020)/poster.jpg",
                    "relative_path": "poster.jpg",
                    "sha": "poster-sha",
                    "mode": "100644",
                }
            ],
        }
        record = {
            "source": {
                "folder": item["folder"],
                "image_path": item["image_path"],
                "image_url": "https://raw.example/original.jpg",
            },
            "tmdb": {"tmdb_id": 42, "title": "Correct Film", "year": 2020},
            "correction": {"canonical_folder_name": "Correct Film (2020)"},
            "status": "ready",
        }
        engine = self.make_engine([item], {"items": []}, {item["folder"]: record})

        result = engine.run_batch(limit=1, apply=True)
        committed = engine.github.committed
        json_path = "جاهز/Original Folder (2020)/Correct Film (2020).json"

        self.assertEqual(result["commit_sha"], "test-commit")
        self.assertIn(json_path, committed["files"])
        saved = json.loads(committed["files"][json_path])
        self.assertEqual(saved["source"]["image_path"], "جاهز/Original Folder (2020)/poster.jpg")
        self.assertEqual(saved["source"]["original_image_path"], item["image_path"])
        self.assertEqual(
            committed["moves"]["جاهز/Original Folder (2020)/poster.jpg"]["sha"],
            "poster-sha",
        )
        self.assertIn(item["image_path"], committed["deletes"])

    def test_review_folder_can_be_promoted_to_ready(self):
        item = {
            "folder": "Unresolved Film (2021)",
            "source_prefix": "مراجعة/Unresolved Film (2021)",
            "image_path": "مراجعة/Unresolved Film (2021)/poster.jpg",
            "poster_files": [
                {
                    "path": "مراجعة/Unresolved Film (2021)/poster.jpg",
                    "relative_path": "poster.jpg",
                    "sha": "review-poster-sha",
                    "mode": "100644",
                }
            ],
        }
        old_json = "مراجعة/Unresolved Film (2021)/Unresolved Film (2021).json"
        index = {
            "items": [
                {
                    "source_folder": item["folder"],
                    "image_path": item["image_path"],
                    "original_image_path": "Unresolved Film (2021)/poster.jpg",
                    "status": "review",
                    "output_path": old_json,
                }
            ]
        }
        record = {
            "source": {
                "folder": item["folder"],
                "image_path": item["image_path"],
                "image_url": "https://raw.example/review.jpg",
            },
            "tmdb": {"tmdb_id": 99, "title": "Resolved Film", "year": 2021},
            "correction": {"canonical_folder_name": "Resolved Film (2021)"},
            "status": "ready",
        }
        engine = self.make_engine([item], index, {item["folder"]: record})

        engine.run_batch(limit=1, apply=True)
        committed = engine.github.committed
        new_image = "جاهز/Unresolved Film (2021)/poster.jpg"
        new_json = "جاهز/Unresolved Film (2021)/Resolved Film (2021).json"

        self.assertIn(new_image, committed["moves"])
        self.assertIn(item["image_path"], committed["deletes"])
        self.assertIn(old_json, committed["deletes"])
        self.assertIn(new_json, committed["files"])
        saved = json.loads(committed["files"][new_json])
        self.assertEqual(
            saved["source"]["original_image_path"],
            "Unresolved Film (2021)/poster.jpg",
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
