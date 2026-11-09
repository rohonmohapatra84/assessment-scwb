"""Input validation tests using only the public HTTP interface."""
import unittest

from shortener.app import create_app
from .wsgi_client import Client


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(":memory:")
        self.client = Client(self.app)

    def tearDown(self):
        self.app.close()

    def assert_bad_request(self, response):
        self.assertEqual(response.status, 400, response.body)
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertTrue(response.headers.get("x-request-id"))
        self.assertEqual(response.headers.get("content-type", "").split(";")[0], "application/json")
        payload = response.json()
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(set(payload["error"]), {"code", "message"})
        self.assertRegex(payload["error"]["code"], r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
        self.assertIsInstance(payload["error"]["message"], str)
        self.assertTrue(payload["error"]["message"])

    def post(self, body):
        return self.client.request("POST", "/api/v1/links", body=body)

    def test_ac5_reject_non_http_schemes(self):
        for url in ("ftp://example.com/file", "file:///etc/passwd", "javascript:alert(1)", "data:text/plain,hello", "mailto:test@example.com", "//example.com/path", "example.com", "https://", ""):
            with self.subTest(url=url):
                self.assert_bad_request(self.post({"url": url}))

    def test_private_loopback_link_local_and_reserved_hosts(self):
        urls = (
            "http://localhost/", "http://LOCALHOST/", "http://127.0.0.1/", "http://127.9.8.7/",
            "http://10.1.2.3/", "http://172.16.0.1/", "http://172.31.255.254/", "http://192.168.1.1/",
            "http://169.254.169.254/", "http://0.0.0.0/", "http://240.0.0.1/", "http://255.255.255.255/",
            "http://[::1]/", "http://[::]/", "http://[fc00::1]/", "http://[fd12::1]/", "http://[fe80::1]/",
            "http://[::ffff:127.0.0.1]/",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assert_bad_request(self.post({"url": url}))

    def test_reject_url_credentials(self):
        for url in ("https://user@example.com/", "https://user:password@example.com/", "https://:password@example.com/", "https://user%40name@example.com/", "https://@example.com/"):
            with self.subTest(url=url):
                self.assert_bad_request(self.post({"url": url}))

    def test_reject_url_whitespace_and_controls(self):
        for url in (" https://example.com/", "https://example.com/ ", "https://example.com/a b", "https://example.com/a\tb", "https://example.com/a\nb", "https://example.com/\x00", "https://example.com/\x1f", "https://example.com/\x7f", "https://example.com/\r\nLocation: https://example.org/"):
            with self.subTest(url=repr(url)):
                self.assert_bad_request(self.post({"url": url}))

    def test_url_length_boundary(self):
        prefix = "https://example.com/"
        valid = prefix + "a" * (2048 - len(prefix))
        response = self.post({"url": valid})
        self.assertEqual(response.status, 201, response.body)
        self.assertEqual(response.json()["long_url"], valid)
        self.assert_bad_request(self.post({"url": valid + "a"}))

    def test_ac4_invalid_aliases_return_400(self):
        for alias in ("", "a", "ab", "a" * 33, "has space", "a.b", "a/b", "a+b", "a%b", "a?b", "a#b", "éab", "a\nb"):
            with self.subTest(alias=alias):
                self.assert_bad_request(self.post({"url": "https://example.com/", "custom_alias": alias}))

    def test_reserved_aliases_return_400(self):
        for alias in ("api", "healthz", "openapi.json", "admin"):
            with self.subTest(alias=alias):
                self.assert_bad_request(self.post({"url": "https://example.com/", "custom_alias": alias}))

    def test_body_must_be_json_object(self):
        for raw in (b"[]", b'["https://example.com/"]', b'"https://example.com/"', b"123", b"true", b"null"):
            with self.subTest(raw=raw):
                self.assert_bad_request(self.client.request("POST", "/api/v1/links", raw_body=raw))

    def test_missing_url_is_rejected(self):
        for body in ({}, {"custom_alias": "only_alias"}):
            with self.subTest(body=body):
                self.assert_bad_request(self.post(body))

    def test_url_must_be_string(self):
        for value in (None, 42, True, [], {}, ["https://example.com/"]):
            with self.subTest(value=value):
                self.assert_bad_request(self.post({"url": value}))

    def test_alias_must_be_string(self):
        for value in (None, 123, False, [], {}):
            with self.subTest(value=value):
                self.assert_bad_request(self.post({"url": "https://example.com/", "custom_alias": value}))

    def test_malformed_json_and_invalid_utf8_return_400(self):
        for raw in (b"", b"{", b'{"url":}', b'{"url":"https://example.com/",}', b'{"url":"https://example.com/"} trailing', b'{"url":"\xff"}'):
            with self.subTest(raw=raw):
                self.assert_bad_request(self.client.request("POST", "/api/v1/links", raw_body=raw))

    def test_body_larger_than_16_kib_is_rejected(self):
        raw = b'{"url":"https://example.com/"}' + b" " * (16 * 1024)
        self.assertGreater(len(raw), 16 * 1024)
        self.assert_bad_request(self.client.request("POST", "/api/v1/links", raw_body=raw))

    def test_http_and_https_public_urls_are_accepted(self):
        for url in ("http://example.com/path", "https://example.org:8443/path?x=1&y=2#part", "https://8.8.8.8/path", "https://[2606:4700:4700::1111]/path"):
            with self.subTest(url=url):
                response = self.post({"url": url})
                self.assertEqual(response.status, 201, response.body)
                self.assertEqual(response.json()["long_url"], url)


if __name__ == "__main__":
    unittest.main()
