import os
import unittest
from unittest import mock

import route_test_ui as ui


class FakeHandler:
    def __init__(self, address="127.0.0.1"):
        self.client_address = (address, 12345)


class HttpSecurityTests(unittest.TestCase):
    def setUp(self):
        with ui.LOGIN_RATE_LOCK:
            ui.LOGIN_FAILURES.clear()

    def tearDown(self):
        with ui.LOGIN_RATE_LOCK:
            ui.LOGIN_FAILURES.clear()

    def test_session_cookie_has_http_only_and_strict_same_site(self):
        headers = ui._session_headers("token-value")
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Max-Age=604800", cookie)
        self.assertNotIn("; Secure", cookie)

    def test_secure_cookie_can_be_enabled_for_https_deployment(self):
        with mock.patch.dict(os.environ, {"ROUTE_TEST_SECURE_COOKIE": "1"}):
            cookie = ui._session_headers("token-value")["Set-Cookie"]
        self.assertIn("; Secure", cookie)

    def test_clearing_cookie_preserves_security_attributes(self):
        cookie = ui._session_headers(clear=True)["Set-Cookie"]
        self.assertIn("Max-Age=0", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_security_headers_block_common_browser_attack_classes(self):
        handler = ui.Handler.__new__(ui.Handler)
        headers = handler._security_headers()
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("object-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

    def test_login_failures_are_rate_limited_per_client(self):
        handler = FakeHandler()
        now = 1000.0
        for _ in range(ui.LOGIN_RATE_LIMIT):
            ui._record_login_failure(handler, now=now)
        retry_after = ui._login_rate_status(handler, now=now)
        self.assertIsNotNone(retry_after)
        self.assertGreaterEqual(retry_after, 1)
        self.assertEqual(ui._login_rate_status(FakeHandler("127.0.0.2"), now=now), None)

    def test_login_rate_limit_expires_and_success_clears_failures(self):
        handler = FakeHandler()
        now = 1000.0
        for _ in range(ui.LOGIN_RATE_LIMIT):
            ui._record_login_failure(handler, now=now)
        self.assertIsNotNone(ui._login_rate_status(handler, now=now))
        self.assertIsNone(ui._login_rate_status(handler, now=now + ui.LOGIN_RATE_WINDOW_SECONDS + 1))
        for _ in range(ui.LOGIN_RATE_LIMIT):
            ui._record_login_failure(handler, now=now + 200)
        ui._clear_login_failures(handler)
        self.assertIsNone(ui._login_rate_status(handler, now=now + 200))


if __name__ == "__main__":
    unittest.main()
