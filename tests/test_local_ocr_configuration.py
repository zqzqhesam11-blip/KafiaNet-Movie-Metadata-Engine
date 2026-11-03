from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings


class LocalOCRConfigurationTests(unittest.TestCase):
    def test_local_ocr_is_disabled_when_environment_variable_is_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.local_ocr_enabled)

    def test_local_ocr_can_only_be_enabled_by_explicit_true_value(self):
        for value in ("1", "true", "YES", "on"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"LOCAL_OCR_ENABLED": value}, clear=True
            ):
                self.assertTrue(Settings.from_env().local_ocr_enabled)

        for value in ("", "0", "false", "no", "off", "anything"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"LOCAL_OCR_ENABLED": value}, clear=True
            ):
                self.assertFalse(Settings.from_env().local_ocr_enabled)


if __name__ == "__main__":
    unittest.main()
