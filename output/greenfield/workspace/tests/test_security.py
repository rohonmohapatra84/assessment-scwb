"""API-key authentication, response hardening, and per-client rate limits."""
import unittest

from shortener.app import create_app
from .wsgi_client import Client


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(":memory:")
        self.client = Client(self.app)

    def tearDown(self):
        self.app.close()

    def configure(self, **kwargs):
        self.app.close()
        self.app = create_app(":memory:", **kwargs)
        self.client = Client(self.app)

    def assert_error(self, response, status, code=None):
        self.assertEqual(response.status, status, response.body)
        self.assertTrue(response.headers.get("x-request-id"))
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(response.headers.get("content-type", "").split(";")[0], "application/json")
        payload = response.json()
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(set(payload["error"]), {"code", "message"})
        self.assertRegex(payload["error"]["code"], r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
        self.assertIsInstance(payload["error"]["message"], str)
        self.assertTrue(payload["error"]["message"])
        if code is not None:
            self.assertEqual(payload["error"]["code"], code)

    def create_request(self, **kwargs):
        return self.client.request("POST", "/api/v1/links", body={"url": "https://example.com/"}, **kwargs)

    def test_auth_disabled_by_default(self):
        response = self.create_request()
        self.assertEqual(response.status, 201, response.body)
        code = response.json()["code"]
        self.assertEqual(self.client.request("GET", "/api/v1/links/" + code).status, 200)
        self.assertEqual(self.client.request("GET", "/api/v1/links/" + code + "/stats").status, 200)
        self.assertEqual(self.client.request("DELETE", "/api/v1/links/" + code).status, 204)

    def test_missing_and_invalid_keys_protect_every_api_operation(self):
        self.configure(api_keys=("unittest-only-key-a", "unittest-only-key-b"))
        created = self.create_request(headers={"X-API-Key": "unittest-only-key-a"})
        self.assertEqual(created.status, 201, created.body)
        code = created.json()["code"]
        operations = (("POST", "/api/v1/links"), ("GET", "/api/v1/links/" + code), ("DELETE", "/api/v1/links/" + code), ("GET", "/api/v1/links/" + code + "/stats"))
        for headers in ({}, {"X-API-Key": ""}, {"X-API-Key": "incorrect"}, {"X-API-Key": "unittest-only-key-a-extra"}):
            for method, path in operations:
                with self.subTest(headers=headers, method=method, path=path):
                    response = self.client.request(method, path, body={"url": "https://example.com/"} if method == "POST" else None, headers=headers)
                    self.assert_error(response, 401)
        self.assertEqual(self.client.request("GET", "/" + code).status, 302)

    def test_each_configured_key_authorizes_all_api_operations(self):
        keys = ("unittest-only-key-a", "unittest-only-key-b")
        self.configure(api_keys=keys)
        for key in keys:
            with self.subTest(key=key):
                headers = {"X-API-Key": key}
                created = self.create_request(headers=headers)
                self.assertEqual(created.status, 201, created.body)
                path = "/api/v1/links/" + created.json()["code"]
                self.assertEqual(self.client.request("GET", path, headers=headers).status, 200)
                self.assertEqual(self.client.request("GET", path + "/stats", headers=headers).status, 200)
                self.assertEqual(self.client.request("DELETE", path, headers=headers).status, 204)

    def test_auth_does_not_protect_public_routes(self):
        self.configure(api_keys=("unittest-only-key",))
        created = self.create_request(headers={"X-API-Key": "unittest-only-key"})
        self.assertEqual(created.status, 201, created.body)
        for path, expected in (("/healthz", 200), ("/openapi.json", 200), ("/" + created.json()["code"], 302)):
            with self.subTest(path=path):
                response = self.client.request("GET", path)
                self.assertEqual(response.status, expected, response.body)
                self.assertTrue(response.headers.get("x-request-id"))
                self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")

    def test_ac8_rate_limit_two_requests_then_429(self):
        self.configure(rate_limit_per_minute=2)
        first = self.create_request()
        second = self.create_request()
        self.assertEqual(first.status, 201, first.body)
        self.assertEqual(second.status, 201, second.body)
        self.assert_error(self.create_request(), 429, "rate_limited")
        self.assert_error(self.create_request(), 429, "rate_limited")

    def test_rate_limit_is_isolated_by_remote_addr(self):
        self.configure(rate_limit_per_minute=2)
        for ip in ("203.0.113.10", "203.0.113.11"):
            with self.subTest(ip=ip):
                for _ in range(2):
                    response = self.create_request(ip=ip)
                    self.assertEqual(response.status, 201, response.body)
                self.assert_error(self.create_request(ip=ip), 429, "rate_limited")

    def test_x_forwarded_for_cannot_bypass_rate_limit(self):
        self.configure(rate_limit_per_minute=2)
        for forwarded in ("198.51.100.1", "198.51.100.2"):
            response = self.create_request(headers={"X-Forwarded-For": forwarded}, ip="203.0.113.20")
            self.assertEqual(response.status, 201, response.body)
        self.assert_error(self.create_request(headers={"X-Forwarded-For": "198.51.100.3"}, ip="203.0.113.20"), 429, "rate_limited")
        independent = self.create_request(headers={"X-Forwarded-For": "198.51.100.1"}, ip="203.0.113.21")
        self.assertEqual(independent.status, 201, independent.body)

    def test_rate_limit_applies_only_to_creation(self):
        self.configure(rate_limit_per_minute=2)
        first = self.create_request()
        self.assertEqual(first.status, 201, first.body)
        code = first.json()["code"]
        self.assertEqual(self.create_request().status, 201)
        self.assert_error(self.create_request(), 429, "rate_limited")
        for path, expected in (("/" + code, 302), ("/api/v1/links/" + code, 200), ("/api/v1/links/" + code + "/stats", 200), ("/healthz", 200), ("/openapi.json", 200)):
            with self.subTest(path=path):
                response = self.client.request("GET", path)
                self.assertEqual(response.status, expected, response.body)
        self.assertEqual(self.client.request("DELETE", "/api/v1/links/" + code).status, 204)


if __name__ == "__main__":
    unittest.main()
