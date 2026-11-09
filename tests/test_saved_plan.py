from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from app.engine import MetadataEngine
from app.github_api import GitHubAPI


class SavedPlanIntegrityTests(unittest.TestCase):
    def test_plan_round_trip_and_integrity_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "batch-plan.json")
            plan = {
                "plan_schema_version": 1,
                "plan_id": "plan-test",
                "source_repo": "owner/posters",
                "branch": "main",
                "source_head_sha": "abc123",
                "pending": [{"folder": "Film (2024)", "poster_files": [{"path": "Film (2024)/poster.jpg", "sha": "image-sha"}]}],
                "records": [{"source": {"folder": "Film (2024)"}, "status": "ready"}],
                "old_items": [],
            }

            digest = MetadataEngine._write_plan_file(path, plan)
            loaded = MetadataEngine._load_plan_file(path)

            self.assertEqual(loaded["plan_id"], "plan-test")
            self.assertEqual(loaded["sha256"], digest)
            self.assertEqual(loaded["records"], plan["records"])

    def test_plan_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch-plan.json"
            plan = {
                "plan_schema_version": 1,
                "plan_id": "plan-test",
                "pending": [],
                "records": [],
            }
            MetadataEngine._write_plan_file(str(path), plan)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["records"].append({"status": "ready"})
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "integrity check failed"):
                MetadataEngine._load_plan_file(str(path))

    def test_existing_plan_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch-plan.json"
            path.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                MetadataEngine._write_plan_file(str(path), {"plan_schema_version": 1})
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")


class UnchangedPosterDecisionCacheTests(unittest.TestCase):
    def make_engine(self, previous_record):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.github = Mock()
        engine.github.raw_url.return_value = "https://raw.example/poster.jpg"
        engine.github.get_json_file.return_value = previous_record
        engine.reverse_image = Mock()
        engine._reverse_image_results = Mock(side_effect=AssertionError("live Google Lens call was not expected"))
        return engine

    def test_same_image_blob_reuses_saved_review_without_network_research(self):
        image_path = "مراجعة/Film (2024)/poster.jpg"
        previous = {
            "status": "review",
            "source": {"folder": "Film (2024)", "image_blob_sha": "same-image-sha"},
            "match": {"status": "review", "reason": "no official ID"},
        }
        engine = self.make_engine(previous)
        item = {
            "folder": "Film (2024)",
            "image_path": image_path,
            "poster_files": [{"path": image_path, "sha": "same-image-sha", "mode": "100644"}],
        }

        result = engine._record_for_folder(
            item,
            previous_output_path="مراجعة/Film (2024)/Film (2024).json",
        )

        self.assertIs(result, previous)
        engine._reverse_image_results.assert_not_called()
        engine.github.get_json_file.assert_called_once()

    def test_changed_image_or_explicit_refresh_triggers_new_search(self):
        image_path = "مراجعة/Film (2024)/poster.jpg"
        previous = {
            "status": "review",
            "source": {"folder": "Film (2024)", "image_blob_sha": "old-image-sha"},
        }

        changed = self.make_engine(previous)
        changed._reverse_image_results.side_effect = RuntimeError("search reached")
        item = {
            "folder": "Film (2024)",
            "image_path": image_path,
            "poster_files": [{"path": image_path, "sha": "new-image-sha"}],
        }
        with self.assertRaisesRegex(RuntimeError, "search reached"):
            changed._record_for_folder(item, previous_output_path="review.json")
        changed._reverse_image_results.assert_called_once()

        refreshed = self.make_engine(previous)
        refreshed._reverse_image_results.side_effect = RuntimeError("refresh reached")
        item["poster_files"][0]["sha"] = "old-image-sha"
        with self.assertRaisesRegex(RuntimeError, "refresh reached"):
            refreshed._record_for_folder(
                item,
                previous_output_path="review.json",
                refresh_evidence=True,
            )
        refreshed._reverse_image_results.assert_called_once()


class GitHubErrorDetailsTests(unittest.TestCase):
    def test_post_includes_github_validation_message(self):
        response = httpx.Response(
            422,
            json={"message": "Invalid tree", "errors": [{"resource": "Tree", "code": "invalid"}]},
            request=httpx.Request("POST", "https://api.github.com/repos/owner/repo/git/trees"),
        )
        api = GitHubAPI.__new__(GitHubAPI)
        api.client = Mock()
        api.client.post.return_value = response

        with self.assertRaisesRegex(RuntimeError, "Invalid tree"):
            api._post("/git/trees", {"base_tree": "abc", "tree": []})


if __name__ == "__main__":
    unittest.main()
