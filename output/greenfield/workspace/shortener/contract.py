"""Contract-first API and canonical SQL schema, packaged without data files."""

from typing import Any

SCHEMA_SQL = """
CREATE TABLE links (
    code TEXT PRIMARY KEY,
    long_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE click_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    ts TEXT NOT NULL,
    visitor_hash TEXT NOT NULL,
    referrer TEXT,
    device TEXT NOT NULL
);
CREATE TABLE click_daily (
    code TEXT NOT NULL,
    day TEXT NOT NULL,
    clicks INTEGER NOT NULL,
    PRIMARY KEY (code, day)
);
CREATE INDEX idx_click_events_code_ts ON click_events (code, ts);
"""


def openapi_document() -> dict[str, Any]:
    """Generate the supplied contract; callers receive an independent document."""
    def response(description: str, schema: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"description": description}
        if schema:
            result["content"] = {"application/json": {"schema": {"$ref": "#/components/schemas/" + schema}}}
        return result

    def code_parameter() -> dict[str, Any]:
        return {"name": "code", "in": "path", "required": True,
                "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}}

    security = [{"ApiKeyAuth": []}]
    paths: dict[str, Any] = {
        "/api/v1/links": {"post": {
            "operationId": "createLink", "summary": "Create a short link", "security": security,
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/CreateLinkRequest"}}}},
            "responses": {"201": response("Created", "Link"),
                          "400": response("Invalid URL, alias or body", "Error"),
                          "401": response("Missing/invalid API key", "Error"),
                          "409": response("Alias already taken", "Error"),
                          "429": response("Rate limited", "Error")}}},
        "/api/v1/links/{code}": {
            "get": {"operationId": "getLink", "summary": "Get link metadata", "security": security,
                    "parameters": [code_parameter()],
                    "responses": {"200": response("OK", "Link"), "401": response("Unauthorized", "Error"),
                                  "404": response("Not found", "Error")}},
            "delete": {"operationId": "deleteLink", "summary": "Delete a link", "security": security,
                       "parameters": [code_parameter()],
                       "responses": {"204": response("Deleted"), "401": response("Unauthorized", "Error"),
                                     "404": response("Not found", "Error")}}},
        "/api/v1/links/{code}/stats": {"get": {
            "operationId": "getStats", "summary": "Click analytics for a link", "security": security,
            "parameters": [code_parameter(), {"name": "days", "in": "query", "required": False,
                "schema": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7}}],
            "responses": {"200": response("OK", "Stats"), "400": response("Invalid days", "Error"),
                          "401": response("Unauthorized", "Error"), "404": response("Not found", "Error")}}},
        "/healthz": {"get": {"operationId": "health", "summary": "Liveness + datastore check",
                              "responses": {"200": response("OK"), "503": response("Datastore unavailable")}}},
        "/openapi.json": {"get": {"operationId": "openapi", "summary": "This specification",
                                   "responses": {"200": response("OpenAPI document")}}},
        "/{code}": {"get": {"operationId": "redirect", "summary": "Redirect to the original URL",
                            "parameters": [code_parameter()],
                            "responses": {"302": {"description": "Redirect", "headers": {
                                "Location": {"schema": {"type": "string"}}}},
                                          "404": response("Unknown or deleted code", "Error")}}}
    }
    return {
        "openapi": "3.0.3",
        "info": {"title": "URL Shortener API", "version": "1.0.0",
                 "description": "Scalable URL shortener with persistence and click analytics."},
        "paths": paths,
        "components": {
            "securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
            "schemas": {
                "CreateLinkRequest": {"type": "object", "required": ["url"], "properties": {
                    "url": {"type": "string", "maxLength": 2048},
                    "custom_alias": {"type": "string", "pattern": "^[A-Za-z0-9_-]{3,32}$"}}},
                "Link": {"type": "object", "required": ["code", "short_url", "long_url", "created_at"],
                         "properties": {"code": {"type": "string"}, "short_url": {"type": "string"},
                                        "long_url": {"type": "string"},
                                        "created_at": {"type": "string", "format": "date-time"}}},
                "Stats": {"type": "object", "properties": {
                    "code": {"type": "string"}, "total_clicks": {"type": "integer"},
                    "unique_visitors": {"type": "integer"}, "window_days": {"type": "integer"},
                    "clicks_by_day": {"type": "array", "items": {"type": "object"}},
                    "top_referrers": {"type": "array", "items": {"type": "object"}},
                    "devices": {"type": "array", "items": {"type": "object"}}}},
                "Error": {"type": "object", "properties": {"error": {"type": "object", "properties": {
                    "code": {"type": "string"}, "message": {"type": "string"}}}}}
            }
        }
    }
