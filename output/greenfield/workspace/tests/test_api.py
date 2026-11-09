"""Public WSGI interface and link lifecycle tests."""
import concurrent.futures
import datetime
import threading
import unittest

from shortener.app import create_app
from .wsgi_client import Client


class LinkAPITests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(":memory:")
        self.client = Client(self.app)

    def tearDown(self):
        self.app.close()

    def assert_response(self, response, status):
        self.assertEqual(response.status, status, response.body)
        self.assertTrue(response.headers.get("x-request-id"))
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        if status >= 400:
            self.assertEqual(response.headers.get("content-type", "").split(";")[0], "application/json")
            payload = response.json()
            self.assertEqual(set(payload), {"error"})
            self.assertEqual(set(payload["error"]), {"code", "message"})
            self.assertRegex(payload["error"]["code"], r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
            self.assertIsInstance(payload["error"]["message"], str)
            self.assertTrue(payload["error"]["message"])
        return response

    def create_link(self, alias=None, url="https://example.com/path?x=1#fragment"):
        body = {"url": url}
        if alias is not None:
            body["custom_alias"] = alias
        response = self.client.request("POST", "/api/v1/links", body=body)
        self.assert_response(response, 201)
        return response.json()

    def test_ac1_create_https_link(self):
        link = self.create_link()
        self.assertRegex(link["code"], r"^[A-Za-z0-9]{7}$")
        self.assertEqual(link["short_url"], "http://localhost:8080/" + link["code"])
        self.assertEqual(link["long_url"], "https://example.com/path?x=1#fragment")
        self.assertIsInstance(link["created_at"], str)
        datetime.datetime.fromisoformat(link["created_at"].replace("Z", "+00:00"))

    def test_ac2_redirects_with_302(self):
        link = self.create_link()
        response = self.client.request("GET", "/" + link["code"])
        self.assert_response(response, 302)
        self.assertEqual(response.headers.get("location"), link["long_url"])

    def test_ac3_unknown_code_returns_json_404(self):
        self.assert_response(self.client.request("GET", "/MissingCode987"), 404)

    def test_ac4_taken_alias_returns_409(self):
        self.create_link("chosen_alias")
        response = self.client.request("POST", "/api/v1/links", body={"url": "https://example.org/other", "custom_alias": "chosen_alias"})
        self.assert_response(response, 409)
        redirect = self.client.request("GET", "/chosen_alias")
        self.assert_response(redirect, 302)
        self.assertEqual(redirect.headers["location"], "https://example.com/path?x=1#fragment")

    def test_valid_alias_boundaries_and_characters(self):
        for alias in ("a_B", "Z" * 32, "Az09_-mixed"):
            with self.subTest(alias=alias):
                link = self.create_link(alias)
                self.assertEqual(link["code"], alias)
                self.assertEqual(link["short_url"], "http://localhost:8080/" + alias)

    def test_get_link_metadata_matches_creation(self):
        link = self.create_link()
        response = self.client.request("GET", "/api/v1/links/" + link["code"])
        self.assert_response(response, 200)
        metadata = response.json()
        for field in ("code", "short_url", "long_url", "created_at"):
            self.assertEqual(metadata[field], link[field])

    def test_delete_then_redirect_and_metadata_are_404(self):
        link = self.create_link("deleted_alias")
        path = "/api/v1/links/" + link["code"]
        deleted = self.client.request("DELETE", path)
        self.assert_response(deleted, 204)
        self.assertEqual(deleted.body, b"")
        self.assert_response(self.client.request("GET", "/" + link["code"]), 404)
        self.assert_response(self.client.request("GET", path), 404)

    def test_deleted_alias_remains_reserved(self):
        self.create_link("reserved_after_delete")
        self.assert_response(self.client.request("DELETE", "/api/v1/links/reserved_after_delete"), 204)
        response = self.client.request("POST", "/api/v1/links", body={"url": "https://example.org/", "custom_alias": "reserved_after_delete"})
        self.assert_response(response, 409)

    def test_missing_metadata_delete_and_stats(self):
        for method, path in (("GET", "/api/v1/links/AbsentCode"), ("DELETE", "/api/v1/links/AbsentCode"), ("GET", "/api/v1/links/AbsentCode/stats")):
            with self.subTest(method=method, path=path):
                self.assert_response(self.client.request(method, path), 404)

    def test_healthz_is_available(self):
        self.assert_response(self.client.request("GET", "/healthz"), 200)

    def test_unsupported_methods_return_json_405(self):
        link = self.create_link()
        for method, path in (("GET", "/api/v1/links"), ("PUT", "/api/v1/links/" + link["code"]), ("POST", "/api/v1/links/" + link["code"] + "/stats"), ("POST", "/healthz"), ("POST", "/openapi.json"), ("POST", "/" + link["code"])):
            with self.subTest(method=method, path=path):
                self.assert_response(self.client.request(method, path), 405)

    def test_custom_base_url_and_redirect_status(self):
        self.app.close()
        self.app = create_app(":memory:", base_url="https://short.example.org", redirect_status=307)
        self.client = Client(self.app)
        link = self.create_link()
        self.assertEqual(link["short_url"], "https://short.example.org/" + link["code"])
        response = self.client.request("GET", "/" + link["code"])
        self.assert_response(response, 307)
        self.assertEqual(response.headers["location"], link["long_url"])

    def test_generated_codes_are_distinct_and_base62(self):
        codes = [self.create_link()["code"] for _ in range(20)]
        self.assertEqual(len(set(codes)), len(codes))
        for code in codes:
            self.assertRegex(code, r"^[A-Za-z0-9]{7}$")

    def test_concurrent_alias_creation_has_exactly_one_winner(self):
        barrier = threading.Barrier(6)

        def create_once(index):
            barrier.wait(timeout=10)
            return Client(self.app).request("POST", "/api/v1/links", body={"url": "https://example.com/" + str(index), "custom_alias": "race_alias"})

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            responses = list(executor.map(create_once, range(6)))
        self.assertEqual(sorted(response.status for response in responses), [201, 409, 409, 409, 409, 409])
        for response in responses:
            self.assert_response(response, response.status)
        winner = next(response.json() for response in responses if response.status == 201)
        self.assertEqual(self.client.request("GET", "/race_alias").headers["location"], winner["long_url"])


if __name__ == "__main__":
    unittest.main()
