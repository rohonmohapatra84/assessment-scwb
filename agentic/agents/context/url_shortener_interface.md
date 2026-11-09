Python 3.10+, STANDARD LIBRARY ONLY (sqlite3, wsgiref, http, json, threading, queue, secrets, hashlib, ipaddress,
urllib.parse, dataclasses...). No third-party packages.

REQUIRED PUBLIC INTERFACE (tests and the platform depend on it exactly):
  shortener/app.py:
    create_app(db_path: str = ":memory:", *, base_url: str = "http://localhost:8080",
               rate_limit_per_minute: int = 120, api_keys: tuple = (), redirect_status: int = 302)
        -> a WSGI application object (callable(environ, start_response) -> iterable of bytes).
        The returned object ALSO has:  .flush_analytics() -> None   (synchronously persist buffered click events)
                                       .close() -> None             (flush and release the database)
  shortener/server.py:
    main(argv: list | None = None) -> None   # CLI --host --port; reads env SHORTENER_DB, SHORTENER_BASE_URL,
                                             # SHORTENER_API_KEYS (comma separated); serves create_app(...) with a
                                             # threading WSGI server; flushes analytics on shutdown.
  shortener/__init__.py must exist.

BEHAVIOUR: implement the provided OpenAPI contract and SQL schema EXACTLY (same paths, methods, status codes, table and
column names). Error body for every 4xx/5xx: {"error": {"code": "<snake_case>", "message": "<text>"}}.

ENGINEERING REQUIREMENTS:
  * layered: HTTP layer / service (business rules) / repository (SQL only); dependencies injected, no module-level globals
    that block testing; thread safe; type hints; docstrings where non-obvious.
  * persistence: create the tables from schema.sql through a versioned `schema_migrations` table at startup, inside a transaction.
  * codes: random base62, 7 chars, via `secrets`; the PRIMARY KEY is the arbiter; bounded retry (5) on collision.
  * validation: only http/https; reject credentials in URL, whitespace/control chars, loopback/private/link-local/reserved hosts,
    localhost, urls longer than 2048; aliases 3-32 chars [A-Za-z0-9_-], not reserved (api, healthz, openapi.json, admin).
  * redirect: GET /{code} -> redirect_status with Location; unknown or deleted -> 404 JSON. Deleted codes stay reserved (soft delete).
  * analytics: record each redirect OFF the hot path (bounded queue + batch flush; drop and count when full). Store only a salted
    hash of ip+user-agent (never the raw IP), a device class (mobile/tablet/desktop/bot/unknown) and the referrer.
    flush_analytics() MUST be read-your-writes: it must not return while a background flush still holds drained but
    unwritten events (serialise flushes with a lock). /stats returns total_clicks, unique_visitors, window_days, clicks_by_day[{day,clicks}], top_referrers[{referrer,clicks}],
    devices[{device,clicks}]; `days` query param integer 1..90 (default 7) else 400.
  * rate limit: per-client token bucket on POST /api/v1/links -> 429 rate_limited. Client = REMOTE_ADDR (ignore X-Forwarded-For).
  * auth: when api_keys is non-empty, /api/v1/* require header X-API-Key (constant-time compare) else 401.
  * every response carries X-Request-Id and X-Content-Type-Options: nosniff; unexpected exceptions -> generic 500 JSON, no internals.
  * request bodies > 16 KiB -> 400. Body must be a JSON object.
