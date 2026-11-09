"""WSGI HTTP boundary and dependency composition."""

from dataclasses import dataclass
from http import HTTPStatus
import json
import re
import secrets
import threading
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlsplit

from .analytics import Analytics
from .cache import LinkCache
from .contract import openapi_document
from .errors import APIError
from .ratelimit import TokenBucketLimiter
from .service import LinkService
from .storage import SQLiteRepository
from .validation import location_url


@dataclass(frozen=True)
class Route:
    path: str
    method: str
    operation: str
    pattern: re.Pattern[str]


class Application:
    def __init__(self, service: LinkService, limiter: TokenBucketLimiter,
                 api_keys: tuple, redirect_status: int) -> None:
        self.service = service
        self.repository = service.repository
        self.analytics = service.analytics
        self.limiter = limiter
        self.api_keys = tuple(key.encode("utf-8") for key in api_keys)
        self.redirect_status = redirect_status
        self.document = openapi_document()
        self._handlers: dict[str, Callable[..., tuple[int, Any, list[tuple[str, str]]]]] = {
            "createLink": self._create, "getLink": self._get, "deleteLink": self._delete,
            "getStats": self._stats, "health": self._health, "openapi": self._openapi,
            "redirect": self._redirect
        }
        routes = []
        for path, methods in self.document["paths"].items():
            pattern = re.escape(path).replace(re.escape("{code}"), r"(?P<code>[A-Za-z0-9_-]{1,64})")
            for method, definition in methods.items():
                operation = definition["operationId"]
                if operation not in self._handlers:
                    raise RuntimeError("Contract operation lacks a handler")
                routes.append(Route(path, method.upper(), operation, re.compile(pattern + r"\Z")))
        if {route.operation for route in routes} != set(self._handlers):
            raise RuntimeError("Handler is not documented in the contract")
        self.routes = tuple(routes)
        self._lifecycle = threading.Condition()
        self._active = 0
        self._closing = False
        self._closed = False
        self._close_lock = threading.Lock()

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        request_id = secrets.token_hex(16)
        admitted = False
        extra: list[tuple[str, str]] = []
        try:
            with self._lifecycle:
                if self._closing:
                    raise APIError(503, "unavailable", "Service is shutting down.")
                self._active += 1
                admitted = True
            status, body, extra = self._dispatch(environ)
        except APIError as exc:
            status, body = exc.status, exc.body()
        except Exception:
            status = 500
            body = {"error": {"code": "internal_error", "message": "An internal error occurred."}}
        finally:
            if admitted:
                with self._lifecycle:
                    self._active -= 1
                    self._lifecycle.notify_all()
        try:
            payload = b"" if body is None else json.dumps(body, ensure_ascii=True, allow_nan=False,
                                                        separators=(",", ":")).encode("utf-8")
        except Exception:
            status, extra = 500, []
            payload = b'{"error":{"code":"internal_error","message":"An internal error occurred."}}'
        headers = [("X-Request-Id", request_id), ("X-Content-Type-Options", "nosniff"),
                   ("Content-Length", str(len(payload)))]
        if body is not None or status >= 400:
            headers.append(("Content-Type", "application/json; charset=utf-8"))
        if status == 405:
            headers.append(("Allow", self._allowed_methods(environ.get("PATH_INFO", "/"))))
        if status == 429:
            headers.append(("Retry-After", "60"))
        if status >= 400:
            headers.append(("Cache-Control", "no-store"))
        headers.extend(extra)
        start_response(str(status) + " " + HTTPStatus(status).phrase, headers)
        return [payload]

    def _allowed_methods(self, path: str) -> str:
        return ", ".join(sorted({route.method for route in self.routes if route.pattern.fullmatch(path)}))

    def _dispatch(self, environ: dict[str, Any]) -> tuple[int, Any, list[tuple[str, str]]]:
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET").upper()
        if path.startswith("/api/v1/") and self.api_keys:
            supplied = str(environ.get("HTTP_X_API_KEY", "")).encode("utf-8")
            valid = False
            for key in self.api_keys:
                valid = secrets.compare_digest(supplied, key) | valid
            if not valid:
                raise APIError(401, "unauthorized", "A valid API key is required.")
        matched_path = False
        for route in self.routes:
            match = route.pattern.fullmatch(path)
            if match:
                matched_path = True
                if route.method == method:
                    return self._handlers[route.operation](environ, **match.groupdict())
        if matched_path:
            raise APIError(405, "method_not_allowed", "Method not allowed.")
        raise APIError(404, "not_found", "Resource not found.")

    def _body(self, environ: dict[str, Any]) -> dict[str, Any]:
        length_text = environ.get("CONTENT_LENGTH", "")
        if not length_text:
            # WSGI supplies no framing for an unbounded input stream. Extensions
            # may explicitly mark chunked/terminated streams as safe to read.
            if environ.get("wsgi.input_terminated"):
                raw = environ["wsgi.input"].read(16385)
            else:
                raw = b""
        else:
            if not re.fullmatch(r"[0-9]+", str(length_text)):
                raise APIError(400, "invalid_body", "Invalid Content-Length.")
            try:
                length = int(length_text)
            except ValueError:
                raise APIError(400, "invalid_body", "Invalid Content-Length.") from None
            if length > 16384:
                raise APIError(400, "invalid_body", "Request body exceeds 16 KiB.")
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                raise APIError(400, "invalid_body", "Incomplete request body.")
        if len(raw) > 16384:
            raise APIError(400, "invalid_body", "Request body exceeds 16 KiB.")
        def reject_constant(value: str) -> Any:
            raise ValueError("Non-JSON numeric constant")
        try:
            body = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise APIError(400, "invalid_body", "Body must be a valid JSON object.") from None
        if not isinstance(body, dict):
            raise APIError(400, "invalid_body", "Body must be a JSON object.")
        return body

    def _create(self, environ: dict[str, Any]) -> tuple[int, Any, list[tuple[str, str]]]:
        if not self.limiter.allow(str(environ.get("REMOTE_ADDR", ""))):
            raise APIError(429, "rate_limited", "Creation rate limit exceeded.")
        return 201, self.service.create(self._body(environ)), []

    def _get(self, environ: dict[str, Any], code: str) -> tuple[int, Any, list[tuple[str, str]]]:
        return 200, self.service.metadata(code), []

    def _delete(self, environ: dict[str, Any], code: str) -> tuple[int, Any, list[tuple[str, str]]]:
        self.service.delete(code)
        return 204, None, []

    def _stats(self, environ: dict[str, Any], code: str) -> tuple[int, Any, list[tuple[str, str]]]:
        try:
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True, max_num_fields=100)
            values = query.get("days", ["7"])
            if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
                raise ValueError("days")
            days = int(values[0])
            if not 1 <= days <= 90:
                raise ValueError("days")
        except ValueError:
            raise APIError(400, "invalid_days", "days must be an integer from 1 to 90.") from None
        return 200, self.service.stats(code, days), []

    def _health(self, environ: dict[str, Any]) -> tuple[int, Any, list[tuple[str, str]]]:
        if not self.repository.health():
            raise APIError(503, "datastore_unavailable", "Datastore is unavailable.")
        return 200, {"status": "ok"}, []

    def _openapi(self, environ: dict[str, Any]) -> tuple[int, Any, list[tuple[str, str]]]:
        return 200, self.document, []

    def _redirect(self, environ: dict[str, Any], code: str) -> tuple[int, Any, list[tuple[str, str]]]:
        link = self.service.lookup(code)
        self.analytics.record(code, str(environ.get("REMOTE_ADDR", "")),
                              str(environ.get("HTTP_USER_AGENT", "")), environ.get("HTTP_REFERER"))
        return self.redirect_status, None, [("Location", location_url(link.long_url)), ("Cache-Control", "no-store")]

    def flush_analytics(self) -> None:
        with self._close_lock:
            if not self._closed:
                self.analytics.flush()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            with self._lifecycle:
                self._closing = True
                while self._active:
                    self._lifecycle.wait()
            # On flush failure retain the database so callers can retry close.
            self.analytics.close()
            self.repository.close()
            self._closed = True


def create_app(db_path: str = ":memory:", *, base_url: str = "http://localhost:8080",
               rate_limit_per_minute: int = 120, api_keys: tuple = (),
               redirect_status: int = 302) -> Application:
    """Compose independent, thread-safe dependencies for a WSGI application."""
    if redirect_status not in {301, 302, 303, 307, 308}:
        raise ValueError("redirect_status must be an HTTP redirect status")
    if not isinstance(base_url, str) or any(c.isspace() or ord(c) < 32 for c in base_url):
        raise ValueError("Invalid base_url")
    try:
        parts = urlsplit(base_url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or
                parts.username is not None or parts.password is not None or parts.query or parts.fragment):
            raise ValueError("Invalid base_url")
        parts.port
        base_url.encode("utf-8")
    except (ValueError, UnicodeError):
        raise ValueError("Invalid base_url") from None
    if not isinstance(api_keys, tuple) or any(not isinstance(key, str) or not key for key in api_keys):
        raise ValueError("api_keys must be a tuple of nonempty strings")
    limiter = TokenBucketLimiter(rate_limit_per_minute)
    repository = SQLiteRepository(db_path)
    analytics = None
    try:
        analytics = Analytics(repository)
        service = LinkService(repository, LinkCache(), analytics, base_url)
        return Application(service, limiter, api_keys, redirect_status)
    except BaseException:
        if analytics is not None:
            analytics.close()
        repository.close()
        raise
