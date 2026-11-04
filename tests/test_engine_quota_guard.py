from __future__ import annotations

import unittest

from app.engine import MetadataEngine
from app.google_lens import BrightDataQuotaError


class ReverseImageQuotaStub:
    api_request_attempts = 3
    last_lens_diagnostics = {}

    def search_by_image_url(self, image_url: str):
        raise BrightDataQuotaError(
            402,
            "Bright Data reports no available free/API credits; do not add funds or enable a paid plan",
        )


class EngineBrightDataQuotaGuardTests(unittest.TestCase):
    def make_engine(self) -> MetadataEngine:
        engine = MetadataEngine.__new__(MetadataEngine)
        engine.reverse_image = ReverseImageQuotaStub()
        engine._stop_batch = False
        engine._provider_stop_status = None
        engine._provider_stop_reason = None
        return engine

    def test_quota_exhaustion_stops_batch_and_preserves_http_status(self):
        engine = self.make_engine()

        rows = engine._reverse_image_results("https://example.com/poster.jpg")

        self.assertTrue(engine._stop_batch)
        self.assertEqual(engine._provider_stop_status, 402)
        self.assertEqual(engine._provider_stop_reason, "provider_quota_or_throttle")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["stop_batch"])
        self.assertEqual(engine._brightdata_api_request_attempts(), 3)

    def test_auth_and_zone_access_errors_stop_the_batch(self):
        for status in (401, 403):
            with self.subTest(status=status):
                engine = self.make_engine()
                stopped = engine._stop_for_provider_server_error({"http_status": status})

                self.assertTrue(stopped)
                self.assertTrue(engine._stop_batch)
                self.assertEqual(engine._provider_stop_status, status)
                self.assertEqual(engine._provider_stop_reason, "provider_auth_or_access")

    def test_exhausted_server_error_still_stops_the_batch(self):
        engine = self.make_engine()

        stopped = engine._stop_for_provider_server_error({"http_status": 503})

        self.assertTrue(stopped)
        self.assertTrue(engine._stop_batch)
        self.assertEqual(engine._provider_stop_status, 503)
        self.assertEqual(engine._provider_stop_reason, "provider_server_error")


if __name__ == "__main__":
    unittest.main()
