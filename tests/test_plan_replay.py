from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.engine import MetadataEngine


class PlanGitHub:
    def __init__(self):
        self.head = "poster-head-1"
        self.committed = None

    def _branch_commit(self):
        return self.head, "poster-tree"

    def list_film_folders(self):
        return [{
            "folder": "Film Example (2020)",
            "source_prefix": "Film Example (2020)",
            "image_path": "Film Example (2020)/poster.jpg",
            "poster_files": [{
                "path": "Film Example (2020)/poster.jpg",
                "relative_path": "poster.jpg",
                "sha": "poster-blob-sha",
                "mode": "100644",
            }],
        }]

    def get_json_file(self, path):
        return {"items": []} if path == "metadata/index.json" else None

    def raw_url(self, path):
        return "https://raw.example/" + path

    def commit_files(self, files, deletes=None, message="", moves=None, expected_head_sha=None):
        if expected_head_sha and expected_head_sha != self.head:
            raise RuntimeError("stale plan")
        self.committed = {
            "files": dict(files),
            "deletes": list(deletes or []),
            "moves": dict(moves or {}),
            "message": message,
            "expected_head_sha": expected_head_sha,
        }
        return "immutable-plan-commit"


class DryRunPlanReplayTests(unittest.TestCase):
    def make_engine(self):
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.settings = SimpleNamespace(
            posters_repo="owner/posters",
            posters_branch="main",
            validate_for_processing=lambda apply: None,
        )
        engine.github = PlanGitHub()
        record = {
            "source": {
                "folder": "Film Example (2020)",
                "image_path": "Film Example (2020)/poster.jpg",
                "image_url": "https://raw.example/poster.jpg",
            },
            "match": {"status": "ready", "reason": "test"},
            "reverse_image": {
                "latest_query": {"status": "ok", "diagnostics": {}},
                "results_source": "live",
                "evidence": {},
            },
            "tmdb": {"tmdb_id": 42, "title": "Film Example", "year": 2020},
            "tmdb_id": 42,
            "imdb_id": "tt1234567",
            "identity": {"source": "test"},
            "correction": {
                "canonical_folder_name": "Film Example (2020)",
                "canonical_json_name": "Film Example (2020)",
            },
            "status": "ready",
        }
        engine._record_for_folder = Mock(return_value=record)
        return engine

    def test_second_dry_run_reuses_plan_and_apply_performs_no_search_again(self):
        engine = self.make_engine()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dry-run-plan.json"
            first = engine.run_batch(limit=1, apply=False, plan_path=path)
            self.assertEqual(first["ready"], 1)
            self.assertFalse(first["plan_reused"])
            self.assertTrue(path.is_file())
            plan = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(plan["base_head_sha"], "poster-head-1")
            self.assertIn("metadata/index.json", plan["files"])

            second = engine.run_batch(limit=1, apply=False, plan_path=path)
            self.assertTrue(second["plan_reused"])
            self.assertEqual(second["ready"], 1)
            self.assertEqual(engine._record_for_folder.call_count, 1)

            engine.settings.validate_for_processing = lambda apply: None
            applied = engine.apply_saved_plan(path)
            self.assertEqual(applied["commit_sha"], "immutable-plan-commit")
            self.assertTrue(applied["replayed_saved_plan"])
            self.assertFalse(path.exists())
            self.assertEqual(engine._record_for_folder.call_count, 1)
            self.assertEqual(
                engine.github.committed["expected_head_sha"],
                "poster-head-1",
            )
            self.assertIn("جاهز/Film Example (2020)/Film Example (2020).json",
                          engine.github.committed["files"])

    def test_apply_rejects_plan_after_poster_branch_changed_and_keeps_plan(self):
        engine = self.make_engine()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dry-run-plan.json"
            engine.run_batch(limit=1, apply=False, plan_path=path)
            engine.github.head = "poster-head-2"

            with self.assertRaisesRegex(RuntimeError, "changed after DRY_RUN"):
                engine.apply_saved_plan(path)

            self.assertTrue(path.is_file())
            self.assertIsNone(engine.github.committed)

    def test_dry_run_refresh_replaces_old_plan_only_after_new_scan(self):
        engine = self.make_engine()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dry-run-plan.json"
            engine.run_batch(limit=1, apply=False, plan_path=path)
            first_created = json.loads(path.read_text(encoding="utf-8"))["created_utc"]
            engine._record_for_folder.return_value["status"] = "review"
            second = engine.run_batch(limit=1, apply=False, plan_path=path, refresh_plan=True)
            self.assertFalse(second["plan_reused"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(saved["created_utc"], first_created)
            self.assertEqual(saved["summary"]["review"], 1)


if __name__ == "__main__":
    unittest.main()
