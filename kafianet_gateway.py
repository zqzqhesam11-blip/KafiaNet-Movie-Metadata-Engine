from __future__ import annotations

import os

from auth_middleware import BearerTokenMiddleware

token = os.environ.get("SEARXNG_TOKEN", "").strip()
if not token:
    raise RuntimeError("SEARXNG_TOKEN must be configured in Hostless")
if not os.environ.get("SEARXNG_SECRET", "").strip():
    raise RuntimeError("SEARXNG_SECRET must be configured in Hostless")

# Import SearXNG only after validating both required secrets.
from searx.webapp import app as searxng_app  # noqa: E402

app = BearerTokenMiddleware(searxng_app, token)
