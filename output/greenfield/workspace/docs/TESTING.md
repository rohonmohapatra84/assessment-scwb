# Testing strategy

| Layer | Scope | Tool | Gate |
|---|---|---|---|
| unit | codec, validation, cache, rate limiter | unittest | 100% pass |
| component | service + Repository on in-memory SQLite, fake clock | unittest | 100% pass |
| integration | WSGI in-process API tests + real TCP server + restart persistence | unittest + http.client | 100% pass |
| contract | openapi.json <-> implemented routes/status codes | generated contract test + static AST check | zero drift |
| non-functional | load smoke (p50/p95/p99, rps) against a live node | scripts/load_smoke.py | recorded; compare to NFR-2 |

## Verification steps

1. static checks: syntax (AST), secret scan, size/extension policy
2. run full test-suite in an isolated subprocess (timeout, scrubbed env)
3. contract & schema parity checks
4. risk -> test traceability check
5. human approval gate before release
