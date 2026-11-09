# URL Shortener

Scalable URL shortener with persistence and click analytics. Standard library only (Python 3.9+), contract-first (`openapi.json`).

## Quick start

```bash
python -m shortener.server --port 8080          # serve (SQLite file: ./shortener.db)
python -m unittest discover -s tests -t .       # run the tests
```

```bash
curl -s -X POST localhost:8080/api/v1/links -d '{"url":"https://example.com/some/long/path"}'
curl -si localhost:8080/<code>                    # 302 + Location
curl -s  localhost:8080/api/v1/links/<code>/stats?days=7
```

## API

| Method | Path | Status codes |
|---|---|---|
| POST | `/api/v1/links` | 201, 400, 401, 409, 429 |
| DELETE | `/api/v1/links/{code}` | 204, 401, 404 |
| GET | `/api/v1/links/{code}` | 200, 401, 404 |
| GET | `/api/v1/links/{code}/stats` | 200, 400, 401, 404 |
| GET | `/healthz` | 200, 503 |
| GET | `/openapi.json` | 200 |
| GET | `/{code}` | 302, 404 |

Full contract: [`openapi.json`](openapi.json) (also served at `/openapi.json`).

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `SHORTENER_DB` | `shortener.db` | SQLite file path |
| `SHORTENER_BASE_URL` | `http://localhost:8080` | used to build `short_url` |
| `SHORTENER_API_KEYS` | *(empty = open)* | comma-separated keys required via `X-API-Key` on `/api/v1/*` |
| `SHORTENER_RATE_LIMIT` | `120` | creates per minute per client (0 disables) |
| `SHORTENER_VISITOR_SALT` | dev value | **set in production**; salts the visitor hash |
| `SHORTENER_TRUST_PROXY` | `0` | `1` to trust `X-Forwarded-For` behind a trusted proxy |

## Documentation

- [Architecture](docs/ARCHITECTURE.md) - components, flows, capacity model, scale-out path
- [Decisions (ADRs)](docs/ADR.md)
- [Risks and mitigations](docs/RISKS.md)
- [Testing strategy](docs/TESTING.md) and [traceability](tests/TRACEABILITY.md)
