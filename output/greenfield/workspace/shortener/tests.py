"""Run deterministic tests with python -m unittest shortener.tests -v."""

import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

from .app import create_app
from .contract import openapi_document
from .ratelimit import TokenBucketLimiter


def request(app, method: str, path: str, body: object = None, *, raw: bytes | None = None,
            ip: str = "198.51.100.10", headers: dict | None = None):
    environ = {}
    setup_testing_defaults(environ)
    route, _, query = path.partition("?")
    payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    environ.update(REQUEST_METHOD=method, PATH_INFO=route, QUERY_STRING=query, REMOTE_ADDR=ip,
                   CONTENT_LENGTH=str(len(payload)), CONTENT_TYPE="application/json")
    environ["wsgi.input"] = io.BytesIO(payload)
    environ.update(headers or {})
    captured = {}
    def start(status, response_headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(response_headers)
    iterable = app(environ, start)
    try:
        response = b"".join(iterable)
    finally:
        if hasattr(iterable, "close"):
            iterable.close()
    return captured["status"], captured["headers"], json.loads(response) if response else None


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.addCleanup(self.app.close)
        # No test depends on external DNS, but validation still checks addresses.
        self.dns = patch("shortener.validation.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 80))])
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def create(self, alias=None):
        body = {"url": "https://example.com/a?x=1"}
        if alias is not None:
            body["custom_alias"] = alias
        status, headers, result = request(self.app, "POST", "/api/v1/links", body)
        self.assertEqual(status, 201)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(headers["X-Request-Id"])
        return result

    def test_contract_routes_and_document(self) -> None:
        expected = {(path, method.upper(), definition["operationId"])
                    for path, methods in openapi_document()["paths"].items()
                    for method, definition in methods.items()}
        self.assertEqual(expected, {(r.path, r.method, r.operation) for r in self.app.routes})
        self.assertEqual(request(self.app, "GET", "/openapi.json")[2], openapi_document())
        self.assertEqual(request(self.app, "GET", "/healthz")[0], 200)
        self.assertEqual(request(self.app, "PUT", "/api/v1/links")[0], 405)
        self.assertEqual(request(self.app, "GET", "/unknown/path")[0], 404)

    def test_create_redirect_metadata_delete_and_reserved_codes(self) -> None:
        link = self.create("my-link")
        status, headers, _ = request(self.app, "GET", "/my-link")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], link["long_url"])
        self.assertEqual(request(self.app, "GET", "/api/v1/links/my-link")[2], link)
        self.assertEqual(request(self.app, "DELETE", "/api/v1/links/my-link")[0], 204)
        self.assertEqual(request(self.app, "GET", "/my-link")[0], 404)
        self.assertEqual(request(self.app, "DELETE", "/api/v1/links/my-link")[0], 404)
        self.assertEqual(request(self.app, "POST", "/api/v1/links",
                                 {"url": "https://example.com", "custom_alias": "my-link"})[0], 409)

    def test_validation_and_body_limits(self) -> None:
        invalid = ["ftp://example.com", "http://localhost", "http://127.0.0.1", "http://127.1",
                   "http://0x7f000001", "http://10.1.2.3", "http://169.254.1.1", "http://[::1]",
                   "http://[::ffff:127.0.0.1]", "https://user:pass@example.com", "https://example.com/a b",
                   "https://example.com/\n", "https://example.com/" + "x" * 2048, None, 42]
        for url in invalid:
            with self.subTest(url=url):
                self.assertEqual(request(self.app, "POST", "/api/v1/links", {"url": url})[0], 400)
        for alias in ("ab", "a/b", "api", "admin", "healthz", "x" * 33, None):
            self.assertEqual(request(self.app, "POST", "/api/v1/links",
                                     {"url": "https://example.com", "custom_alias": alias})[0], 400)
        for raw in (b"[]", b"null", b"{", b'{"url":NaN}', b"x" * 16385):
            status, headers, body = request(self.app, "POST", "/api/v1/links", raw=raw)
            self.assertEqual(status, 400)
            self.assertIn("error", body)
            self.assertIn("X-Request-Id", headers)
        with patch("shortener.validation.socket.getaddrinfo", return_value=[
                (2, 1, 6, "", ("192.168.1.1", 80))]):
            self.assertEqual(request(self.app, "POST", "/api/v1/links", {"url": "https://example.com"})[0], 400)

    def test_auth_and_per_client_rate_limit(self) -> None:
        app = create_app(api_keys=("test-only-key",), rate_limit_per_minute=1)
        self.addCleanup(app.close)
        body = {"url": "https://example.com"}
        self.assertEqual(request(app, "POST", "/api/v1/links", body)[0], 401)
        headers = {"HTTP_X_API_KEY": "test-only-key", "HTTP_X_FORWARDED_FOR": "8.8.8.8"}
        self.assertEqual(request(app, "POST", "/api/v1/links", body, headers=headers)[0], 201)
        headers["HTTP_X_FORWARDED_FOR"] = "1.1.1.1"
        self.assertEqual(request(app, "POST", "/api/v1/links", body, headers=headers)[0], 429)
        self.assertEqual(request(app, "POST", "/api/v1/links", body, headers=headers, ip="198.51.100.11")[0], 201)
        self.assertEqual(request(app, "GET", "/healthz")[0], 200)
        now = [0.0]
        limiter = TokenBucketLimiter(1, clock=lambda: now[0])
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        now[0] = 60.0
        self.assertTrue(limiter.allow("a"))

    def test_collision_retry_is_bounded(self) -> None:
        self.create("AAAAAAA")
        with patch.object(self.app.service, "generate_code", side_effect=["AAAAAAA", "BBBBBBB"]):
            self.assertEqual(self.create()["code"], "BBBBBBB")
        with patch.object(self.app.service, "generate_code", return_value="AAAAAAA") as generate:
            self.assertEqual(request(self.app, "POST", "/api/v1/links", {"url": "https://example.com"})[0], 503)
            self.assertEqual(generate.call_count, 5)
        self.assertRegex(self.app.service.generate_code(), r"^[A-Za-z0-9]{7}$")

    def test_analytics_and_days(self) -> None:
        self.create("stats-link")
        for ua in ("Mozilla/5.0 iPhone Mobile", "Mozilla/5.0 iPhone Mobile", "Googlebot"):
            self.assertEqual(request(self.app, "GET", "/stats-link", headers={
                "HTTP_USER_AGENT": ua, "HTTP_REFERER": "https://example.org/"})[0], 302)
        self.app.flush_analytics()
        stats = request(self.app, "GET", "/api/v1/links/stats-link/stats")[2]
        self.assertEqual(stats["total_clicks"], 3)
        self.assertEqual(stats["unique_visitors"], 2)
        self.assertEqual(stats["window_days"], 7)
        self.assertEqual(sum(row["clicks"] for row in stats["clicks_by_day"]), 3)
        self.assertEqual({row["device"] for row in stats["devices"]}, {"mobile", "bot"})
        for days in ("0", "91", "", "abc", "1.2", "7&days=8"):
            self.assertEqual(request(self.app, "GET", "/api/v1/links/stats-link/stats?days=" + days)[0], 400)

    def test_flush_waits_for_drained_background_batch(self) -> None:
        self.create("flush-link")
        analytics = self.app.analytics
        analytics._stop.set()
        analytics._thread.join()
        request(self.app, "GET", "/flush-link")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        original = self.app.repository.write_clicks
        def blocked(events):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test timeout")
            original(events)
        with patch.object(self.app.repository, "write_clicks", side_effect=blocked):
            background = threading.Thread(target=analytics.flush)
            background.start()
            self.assertTrue(entered.wait(2))
            def foreground():
                self.app.flush_analytics()
                completed.set()
            waiter = threading.Thread(target=foreground)
            waiter.start()
            try:
                self.assertFalse(completed.wait(0.05))
            finally:
                release.set()
                background.join(5)
                waiter.join(5)
            self.assertTrue(completed.is_set())
        self.assertEqual(self.app.repository.stats("flush-link", 7)["total_clicks"], 1)

    def test_queue_drop_retry_and_generic_errors(self) -> None:
        self.create("drop-link")
        analytics = self.app.analytics
        analytics._stop.set()
        analytics._thread.join()
        for _ in range(analytics._queue.maxsize + 1):
            analytics.record("drop-link", "198.51.100.10", "", None)
        self.assertEqual(analytics.dropped_events, 1)
        with patch.object(self.app.repository, "write_clicks", side_effect=RuntimeError("private detail")):
            with self.assertRaises(RuntimeError):
                analytics.flush()
        analytics.flush()
        self.assertEqual(self.app.repository.stats("drop-link", 7)["total_clicks"], analytics._queue.maxsize)
        with patch.object(self.app.service, "metadata", side_effect=RuntimeError("private detail")):
            status, _, body = request(self.app, "GET", "/api/v1/links/drop-link")
            self.assertEqual(status, 500)
            self.assertNotIn("private detail", json.dumps(body))

    def test_persistence_schema_and_salt_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "links.db")
            app = create_app(path)
            try:
                status, _, link = request(app, "POST", "/api/v1/links", {"url": "https://example.com"})
                self.assertEqual(status, 201)
                request(app, "GET", "/" + link["code"], headers={"HTTP_USER_AGENT": "Mozilla"})
            finally:
                app.close()
            reopened = create_app(path)
            try:
                request(reopened, "GET", "/" + link["code"], headers={"HTTP_USER_AGENT": "Mozilla"})
                reopened.flush_analytics()
                stats = request(reopened, "GET", "/api/v1/links/" + link["code"] + "/stats")[2]
                self.assertEqual(stats["total_clicks"], 2)
                self.assertEqual(stats["unique_visitors"], 1)
            finally:
                reopened.close()
            with sqlite3.connect(path) as db:
                expected = {"links": ["code", "long_url", "created_at", "deleted_at"],
                            "click_events": ["id", "code", "ts", "visitor_hash", "referrer", "device"],
                            "click_daily": ["code", "day", "clicks"]}
                for table, columns in expected.items():
                    self.assertEqual([row[1] for row in db.execute("PRAGMA table_info(" + table + ")")], columns)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)
                event = db.execute("SELECT visitor_hash FROM click_events LIMIT 1").fetchone()[0]
                self.assertRegex(event, r"^[a-f0-9]{64}$")
                self.assertNotIn("198.51.100.10", " ".join(str(row) for row in db.execute("SELECT * FROM click_events")))


if __name__ == "__main__":
    unittest.main()
