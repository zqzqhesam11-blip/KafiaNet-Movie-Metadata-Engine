from __future__ import annotations

import unittest

from app.cli import format_summary


class CliSummaryTests(unittest.TestCase):
    def test_apply_summary_omits_full_record_payloads(self):
        result = {
            "mode": "APPLY",
            "batch_selected": 200,
            "batch_requested": 200,
            "ready": 178,
            "review": 20,
            "duplicates_in_this_batch": 2,
            "written_files": 201,
            "moved_image_files": 200,
            "deleted_old_files": 400,
            "commit_sha": "abc123",
            "next_pending_estimate": 700,
            "tineye_unresponsive": 12,
            "tineye_no_matches": 5,
            "tineye_previous_evidence_reused": 8,
            "records": [{"large": "must not appear"}],
            "planned_output_files": ["very/long/path.json"],
            "planned_image_moves": ["poster.jpg"],
        }

        output = format_summary(result)

        self.assertIn("selected=200/200", output)
        self.assertIn("ready=178 review=20 duplicates=2", output)
        self.assertIn("commit_sha=abc123", output)
        self.assertIn(
            "tineye_unresponsive=12 no_matches=5 reused_saved_evidence=8",
            output,
        )
        self.assertNotIn("must not appear", output)
        self.assertNotIn("very/long/path.json", output)
        self.assertNotIn("poster.jpg", output)

    def test_dry_run_summary_reports_counts_only(self):
        output = format_summary({
            "mode": "DRY_RUN",
            "batch_selected": 5,
            "batch_requested": 5,
            "planned_output_files": ["a.json", "b.json"],
            "planned_image_moves": ["a.jpg", "b.jpg", "c.jpg"],
            "planned_source_image_deletes": ["old/a.jpg"],
        })

        self.assertIn("planned_json_files=2", output)
        self.assertIn("planned_image_moves=3", output)
        self.assertIn("planned_deletes=1", output)
        self.assertNotIn("a.json", output)


if __name__ == "__main__":
    unittest.main()
