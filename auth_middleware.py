from __future__ import annotations

import hmac
from typing import Callable, Iterable


StartResponse = Callable[..., object]
WSGIApp = Callable[[dict, StartResponse], Iterable[bytes]]


class BearerTokenMiddleware:
    """Require one constant-time-checked Bearer token for every non-health route."""

    def __init__(self, app: WSGIApp, token: str):
        token = token.strip()
        if not token:
            raise ValueError("SEARXNG_TOKEN is required")
        self.app = app
        self.expected_authorization = f"Bearer {token}"

    @staticmethod
    def _response(
        start_response: StartResponse,
        status: str,
        body: bytes,
        *,
        head_only: bool = False,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> list[bytes]:
        payload = b"" if head_only else body
        headers = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        start_response(status, headers)
        return [payload]

    def __call__(self, environ: dict, start_response: StartResponse) -> Iterable[bytes]:
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET").upper()

        # Hostless health probes must work without exposing the search endpoint.
        if path == "/health" and method in {"GET", "HEAD"}:
            return self._response(
                start_response,
                "200 OK",
                b"ok\n",
                head_only=(method == "HEAD"),
            )

        supplied = environ.get("HTTP_AUTHORIZATION", "")
        if not hmac.compare_digest(supplied, self.expected_authorization):
            return self._response(
                start_response,
                "401 Unauthorized",
                b"unauthorized\n",
                head_only=(method == "HEAD"),
                extra_headers=[("WWW-Authenticate", 'Bearer realm="KafiaNet SearXNG"')],
            )

        return self.app(environ, start_response)
