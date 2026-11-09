"""Exact OpenAPI contract and file-backed restart persistence tests."""
import os
import tempfile
import unittest

from shortener.app import create_app
from .wsgi_client import Client


def reference(name):
    return {"$ref": "#/components/schemas/" + name}


def json_response(description, schema):
    return {"description": description, "content": {"application/json": {"schema": reference(schema)}}}


def code_parameter():
    return {"name": "code", "in": "path", "required": True, "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}}


def api_security():
    return [{"ApiKeyAuth": []}]


def expected_openapi():
    """Build the supplied document without depending on implementation files."""
    error = json_response
    return {
        "openapi": "3.0.3",
        "info": {"title": "URL Shortener API", "version": "1.0.0", "description": "Scalable URL shortener with persistence and click analytics."},
        "paths": {
            "/api/v1/links": {
                "post": {
                    "operationId": "createLink", "summary": "Create a short link", "security": api_security(),
                    "requestBody": {"required": True, "content": {"application/json": {"schema": reference("CreateLinkRequest")}}},
                    "responses": {
                        "201": json_response("Created", "Link"),
                        "400": error("Invalid URL, alias or body", "Error"),
                        "401": error("Missing/invalid API key", "Error"),
                        "409": error("Alias already taken", "Error"),
                        "429": error("Rate limited", "Error"),
                    },
                },
            },
            "/api/v1/links/{code}": {
                "get": {
                    "operationId": "getLink", "summary": "Get link metadata", "security": api_security(), "parameters": [code_parameter()],
                    "responses": {"200": json_response("OK", "Link"), "401": error("Unauthorized", "Error"), "404": error("Not found", "Error")},
                },
                "delete": {
                    "operationId": "deleteLink", "summary": "Delete a link", "security": api_security(), "parameters": [code_parameter()],
                    "responses": {"204": {"description": "Deleted"}, "401": error("Unauthorized", "Error"), "404": error("Not found", "Error")},
                },
            },
            "/api/v1/links/{code}/stats": {
                "get": {
                    "operationId": "getStats", "summary": "Click analytics for a link", "security": api_security(),
                    "parameters": [code_parameter(), {"name": "days", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7}}],
                    "responses": {"200": json_response("OK", "Stats"), "400": error("Invalid days", "Error"), "401": error("Unauthorized", "Error"), "404": error("Not found", "Error")},
                },
            },
            "/healthz": {"get": {"operationId": "health", "summary": "Liveness + datastore check", "responses": {"200": {"description": "OK"}, "503": {"description": "Datastore unavailable"}}}},
            "/openapi.json": {"get": {"operationId": "openapi", "summary": "This specification", "responses": {"200": {"description": "OpenAPI document"}}}},
            "/{code}": {
                "get": {
                    "operationId": "redirect", "summary": "Redirect to the original URL", "parameters": [code_parameter()],
                    "responses": {"302": {"description": "Redirect", "headers": {"Location": {"schema": {"type": "string"}}}}, "404": error("Unknown or deleted code", "Error")},
                },
            },
        },
        "components": {
            "securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
            "schemas": {
                "CreateLinkRequest": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string", "maxLength": 2048}, "custom_alias": {"type": "string", "pattern": "^[A-Za-z0-9_-]{3,32}$"}}},
                "Link": {"type": "object", "required": ["code", "short_url", "long_url", "created_at"], "properties": {"code": {"type": "string"}, "short_url": {"type": "string"}, "long_url": {"type": "string"}, "created_at": {"type": "string", "format": "date-time"}}},
                "Stats": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"}, "total_clicks": {"type": "integer"}, "unique_visitors": {"type": "integer"}, "window_days": {"type": "integer"},
                        "clicks_by_day": {"type": "array", "items": {"type": "object"}},
                        "top_referrers": {"type": "array", "items": {"type": "object"}},
                        "devices": {"type": "array", "items": {"type": "object"}},
                    },
                },
                "Error": {"type": "object", "properties": {"error": {"type": "object", "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}}},
            },
        },
    }


class ContractPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(":memory:")
        self.client = Client(self.app)

    def tearDown(self):
        self.app.close()

    def test_ac9_openapi_exactly_matches_supplied_contract(self):
        response = self.client.request("GET", "/openapi.json")
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.headers.get("content-type", "").split(";")[0], "application/json")
        self.assertTrue(response.headers.get("x-request-id"))
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(response.json(), expected_openapi())

    def test_documented_operations_return_documented_statuses(self):
        document = self.client.request("GET", "/openapi.json").json()
        created = self.client.request("POST", "/api/v1/links", body={"url": "https://example.com/", "custom_alias": "contract_link"})
        self.assertEqual(created.status, 201, created.body)
        code = created.json()["code"]
        self.app.flush_analytics()
        calls = [
            ("/api/v1/links", "post", created),
            ("/api/v1/links/{code}", "get", self.client.request("GET", "/api/v1/links/" + code)),
            ("/api/v1/links/{code}/stats", "get", self.client.request("GET", "/api/v1/links/" + code + "/stats")),
            ("/healthz", "get", self.client.request("GET", "/healthz")),
            ("/openapi.json", "get", self.client.request("GET", "/openapi.json")),
            ("/{code}", "get", self.client.request("GET", "/" + code)),
            ("/api/v1/links/{code}", "delete", self.client.request("DELETE", "/api/v1/links/" + code)),
        ]
        for path, method, response in calls:
            with self.subTest(path=path, method=method):
                self.assertIn(str(response.status), document["paths"][path][method]["responses"])
                self.assertTrue(response.headers.get("x-request-id"))
                self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        exercised = {(path, method) for path, method, _ in calls}
        declared = {(path, method) for path, operations in document["paths"].items() for method in operations}
        self.assertEqual(exercised, declared)

    def test_ac7_links_persist_after_close_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shortener.sqlite3")
            first = create_app(path)
            try:
                response = Client(first).request("POST", "/api/v1/links", body={"url": "https://example.org/persist?x=1", "custom_alias": "persisted_link"})
                self.assertEqual(response.status, 201, response.body)
                link = response.json()
            finally:
                first.close()
            for _ in range(2):
                reopened = create_app(path)
                try:
                    client = Client(reopened)
                    metadata = client.request("GET", "/api/v1/links/" + link["code"])
                    self.assertEqual(metadata.status, 200, metadata.body)
                    for field in ("code", "short_url", "long_url", "created_at"):
                        self.assertEqual(metadata.json()[field], link[field])
                    redirect = client.request("GET", "/" + link["code"])
                    self.assertEqual(redirect.status, 302, redirect.body)
                    self.assertEqual(redirect.headers["location"], link["long_url"])
                finally:
                    reopened.close()

    def test_close_flushes_clicks_and_visitor_identity_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "analytics.sqlite3")
            first = create_app(path)
            headers = {"User-Agent": "persistence-test-agent", "Referer": "https://example.org/persistent-source"}
            try:
                client = Client(first)
                created = client.request("POST", "/api/v1/links", body={"url": "https://example.com/", "custom_alias": "persistent_stats"})
                self.assertEqual(created.status, 201, created.body)
                code = created.json()["code"]
                for _ in range(2):
                    click = client.request("GET", "/" + code, headers=headers, ip="203.0.113.50")
                    self.assertEqual(click.status, 302, click.body)
            finally:
                first.close()
            reopened = create_app(path)
            try:
                client = Client(reopened)
                reopened.flush_analytics()
                response = client.request("GET", "/api/v1/links/" + code + "/stats")
                self.assertEqual(response.status, 200, response.body)
                self.assertEqual(response.json()["total_clicks"], 2)
                self.assertEqual(response.json()["unique_visitors"], 1)
                self.assertEqual(client.request("GET", "/" + code, headers=headers, ip="203.0.113.50").status, 302)
                reopened.flush_analytics()
                response = client.request("GET", "/api/v1/links/" + code + "/stats")
                self.assertEqual(response.status, 200, response.body)
                data = response.json()
                self.assertEqual(data["total_clicks"], 3)
                self.assertEqual(data["unique_visitors"], 1)
                self.assertEqual(sum(row["clicks"] for row in data["clicks_by_day"]), 3)
                self.assertEqual(sum(row["clicks"] for row in data["devices"]), 3)
            finally:
                reopened.close()

    def test_soft_deleted_code_stays_reserved_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "deleted.sqlite3")
            first = create_app(path)
            try:
                client = Client(first)
                response = client.request("POST", "/api/v1/links", body={"url": "https://example.com/", "custom_alias": "tombstoned_link"})
                self.assertEqual(response.status, 201, response.body)
                self.assertEqual(client.request("DELETE", "/api/v1/links/tombstoned_link").status, 204)
            finally:
                first.close()
            reopened = create_app(path)
            try:
                client = Client(reopened)
                missing = client.request("GET", "/tombstoned_link")
                self.assertEqual(missing.status, 404, missing.body)
                self.assertIn("error", missing.json())
                duplicate = client.request("POST", "/api/v1/links", body={"url": "https://example.org/", "custom_alias": "tombstoned_link"})
                self.assertEqual(duplicate.status, 409, duplicate.body)
                self.assertIn("error", duplicate.json())
            finally:
                reopened.close()

    def test_separate_default_apps_have_independent_databases(self):
        response = self.client.request("POST", "/api/v1/links", body={"url": "https://example.com/", "custom_alias": "isolated_link"})
        self.assertEqual(response.status, 201, response.body)
        other = create_app(":memory:")
        try:
            client = Client(other)
            missing = client.request("GET", "/isolated_link")
            self.assertEqual(missing.status, 404, missing.body)
            created = client.request("POST", "/api/v1/links", body={"url": "https://example.org/", "custom_alias": "isolated_link"})
            self.assertEqual(created.status, 201, created.body)
            self.assertEqual(client.request("GET", "/isolated_link").headers["location"], "https://example.org/")
            self.assertEqual(self.client.request("GET", "/isolated_link").headers["location"], "https://example.com/")
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
