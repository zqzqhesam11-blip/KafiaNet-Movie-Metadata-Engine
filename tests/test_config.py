from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings


class ReviewResearchConfigurationTests(unittest.TestCase):
    def test_reviewer_only_research_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertFalse(settings.review_research_enabled)

    def test_reviewer_only_research_can_be_enabled_explicitly(self):
        with patch.dict(os.environ, {"REVIEW_RESEARCH_ENABLED": "true"}, clear=True):
            settings = Settings.from_env()

        self.assertTrue(settings.review_research_enabled)


if __name__ == "__main__":
    unittest.main()
