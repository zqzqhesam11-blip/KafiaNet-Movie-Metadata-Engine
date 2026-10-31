from __future__ import annotations

import unittest
from wsgiref.util import setup_testing_defaults

from auth_middleware import BearerTokenMiddleware


class BearerTokenMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.forwarded = 0

        def upstream(environ, start_response):
            self.forwarded += 1
            body = b'{"results":[]}'
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]

        self.app = BearerTokenMiddleware(upstream, "test-secret-token")

    def request(self, path="/search", authorization=None, method="GET"):
        environ = {}
        setup_testing_defaults(environ)
        environ["PATH_INFO"] = path
        environ["REQUEST_METHOD"] = method
        if authorization is not None:
            environ["HTTP_AUTHORIZATION"] = authorization

        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = dict(headers)

        body = b"".join(self.app(environ, start_response))
        return captured, body

    def test_missing_token_is_rejected_without_forwarding(self):
        response, body = self.request()
        self.assertEqual(response["status"], "401 Unauthorized")
        self.assertIn("WWW-Authenticate", response["headers"])
        self.assertEqual(body, b"unauthorized\n")
        self.assertEqual(self.forwarded, 0)

    def test_wrong_token_is_rejected_without_forwarding(self):
        response, _ = self.request(authorization="Bearer wrong-token")
        self.assertEqual(response["status"], "401 Unauthorized")
        self.assertEqual(self.forwarded, 0)

    def test_correct_token_reaches_searxng(self):
        response, body = self.request(authorization="Bearer test-secret-token")
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(body, b'{"results":[]}')
        self.assertEqual(self.forwarded, 1)

    def test_health_is_available_without_token(self):
        response, body = self.request(path="/health")
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(body, b"ok\n")
        self.assertEqual(self.forwarded, 0)

    def test_health_head_has_no_body(self):
        response, body = self.request(path="/health", method="HEAD")
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(body, b"")
        self.assertEqual(response["headers"]["Content-Length"], "3")
        self.assertEqual(self.forwarded, 0)

    def test_empty_configured_token_is_rejected(self):
        with self.assertRaises(ValueError):
            BearerTokenMiddleware(lambda environ, start_response: [], "")


if __name__ == "__main__":
    unittest.main()
