"""In-process WSGI test client (provided by the platform; tests must not depend on app internals)."""
import io
import json
from typing import Any, Dict, Optional


class Resp:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes):
        self.status, self.headers, self.body = status, headers, body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class Client:
    """Client(app).request(method, path, body=None, headers=None, ip="203.0.113.9", raw_body=None) -> Resp
    `body` is JSON-encoded; `raw_body` sends bytes as-is. Response header names are lower-cased."""

    def __init__(self, app):
        self.app = app

    def request(self, method: str, path: str, body: Any = None, headers: Optional[Dict[str, str]] = None,
                ip: str = "203.0.113.9", raw_body: Optional[bytes] = None) -> Resp:
        path_info, _, query = path.partition("?")
        data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else b"")
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path_info, "QUERY_STRING": query,
                   "CONTENT_LENGTH": str(len(data)), "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(data),
                   "REMOTE_ADDR": ip, "SERVER_NAME": "testserver", "SERVER_PORT": "80", "wsgi.url_scheme": "http"}
        for k, v in (headers or {}).items():
            environ["HTTP_" + k.upper().replace("-", "_")] = v
        captured: Dict[str, Any] = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"] = int(status.split()[0])
            captured["headers"] = {k.lower(): v for k, v in response_headers}

        result = self.app(environ, start_response)
        try:
            body_bytes = b"".join(result)
        finally:
            if hasattr(result, "close"):
                result.close()
        return Resp(captured["status"], captured["headers"], body_bytes)
