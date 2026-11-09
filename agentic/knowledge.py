"""Domain knowledge used by the design / risk agents.

This is the "engineering judgement" layer: architecture decision records,
risk catalogues and capacity maths. It is data, not code paths, so it can be
reviewed, versioned and replaced by LLM-generated content without touching
the orchestration or the guardrails.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

ID_SPACE = 62 ** 7  # 7-char base62 codes


def capacity_model(new_links_per_month: int = 10_000_000, read_write_ratio: int = 100,
                   peak_factor: int = 10, years: int = 5, bytes_per_link: int = 500,
                   clicks_per_month: int = 1_000_000_000, bytes_per_click: int = 100) -> Dict[str, Any]:
    secs = 30 * 24 * 3600
    writes_avg = new_links_per_month / secs
    reads_avg = writes_avg * read_write_ratio
    total_links = new_links_per_month * 12 * years
    return {
        "assumptions": {"new_links_per_month": new_links_per_month, "read_write_ratio": read_write_ratio,
                        "peak_factor": peak_factor, "retention_years": years},
        "writes_per_sec_avg": round(writes_avg, 1),
        "writes_per_sec_peak": round(writes_avg * peak_factor, 1),
        "redirects_per_sec_avg": round(reads_avg, 1),
        "redirects_per_sec_peak": round(reads_avg * peak_factor, 1),
        "total_links_5y": total_links,
        "link_storage_gb": round(total_links * bytes_per_link / 1e9, 1),
        "click_event_storage_gb_per_month": round(clicks_per_month * bytes_per_click / 1e9, 1),
        "collision_probability_per_insert_at_1B_links": round(1_000_000_000 / ID_SPACE, 6),
        "id_space": ID_SPACE,
    }


SHORTENER_ADRS: List[Dict[str, Any]] = [
    {"id": "ADR-001", "title": "Short-code generation: random base62 with unique-constraint retry",
     "options": ["random base62 (7 chars)", "auto-increment counter + base62", "hash(url) truncation", "pre-generated key service"],
     "decision": "random base62, 7 chars, retry on unique violation",
     "why": "No cross-node coordination, codes are non-enumerable, per-insert collision probability is n/62^7 (~0.03% at 1B links) so a bounded retry loop is enough.",
     "tradeoffs": "Needs a unique index and a retry path; counters are collision-free but leak volume and are enumerable; hash truncation makes dedup easy but collides on truncation and leaks URL equality."},
    {"id": "ADR-002", "title": "Persistence behind a Repository port; SQLite for the prototype",
     "options": ["SQLite", "PostgreSQL", "DynamoDB/Cassandra", "Redis only"],
     "decision": "Repository interface + SQLite(WAL) now; PostgreSQL (hash-sharded by code) or DynamoDB in production",
     "why": "Access pattern is pure key-value (code -> url) with ~100:1 reads; the port isolates storage choice from business logic so scale-out is an adapter swap, not a rewrite.",
     "tradeoffs": "SQLite is single-writer and single-node: fine for a runnable, zero-dependency prototype, not for the stated scale."},
    {"id": "ADR-003", "title": "Redirect uses HTTP 302 (configurable to 301)",
     "options": ["301 permanent", "302 temporary"],
     "decision": "302 by default",
     "why": "Browsers cache 301s, so repeat clicks bypass us and analytics under-count; 302 keeps every click observable and allows later edits/expiry.",
     "tradeoffs": "Higher redirect traffic and no browser/CDN caching offload; 301 is cheaper if analytics are not needed."},
    {"id": "ADR-004", "title": "Analytics are off the redirect hot path (buffered, batched, lossy under overload)",
     "options": ["synchronous insert per click", "bounded in-memory queue + batch flush", "message broker (Kafka) + stream processor"],
     "decision": "bounded queue + background batch flusher; Kafka/ClickHouse is the production path",
     "why": "Redirect latency must not depend on write throughput of the analytics store. Batching turns N inserts into 1 transaction.",
     "tradeoffs": "Clicks can be lost on crash (<= flush interval) or dropped when the queue is full (counted in a metric). Stats are eventually consistent."},
    {"id": "ADR-005", "title": "Read-through TTL/LRU cache for redirect lookups",
     "options": ["no cache", "in-process LRU", "Redis", "CDN edge cache"],
     "decision": "in-process TTL+LRU (Redis in production)",
     "why": "Traffic is Zipfian; a small cache absorbs the hot set and cuts DB reads dramatically.",
     "tradeoffs": "Deleted links can still redirect for up to TTL on other nodes; the local node invalidates immediately."},
    {"id": "ADR-006", "title": "Privacy-preserving analytics",
     "options": ["store raw IP + UA", "store salted hash of IP+UA"],
     "decision": "salted SHA-256 visitor hash; raw IP never stored",
     "why": "Unique-visitor counts without retaining personal data (GDPR data minimisation).",
     "tradeoffs": "Hash of IP+UA is an approximation (NAT, UA changes); cannot be reversed for debugging."},
    {"id": "ADR-007", "title": "Stateless service tier; per-node rate limiting",
     "options": ["sticky sessions", "stateless nodes behind a load balancer"],
     "decision": "stateless; token-bucket rate limit per client per node",
     "why": "Any node can serve any request, so scaling is adding nodes.",
     "tradeoffs": "Per-node limits are approximate (effective limit = limit x nodes); a Redis-backed limiter makes it exact."},
    {"id": "ADR-008", "title": "Contract-first API (OpenAPI 3) enforced by tests",
     "options": ["code-first docs", "contract-first"],
     "decision": "openapi.json is generated first and a contract test fails if routes drift",
     "why": "Parallel agents (code, tests, docs) coordinate through one contract instead of guessing each other's output.",
     "tradeoffs": "Spec and code must be kept in sync; the contract test is what makes that cheap."},
]

SHORTENER_RISKS: List[Dict[str, Any]] = [
    {"id": "R1", "risk": "Short-code collision under concurrent inserts", "likelihood": "low", "impact": "medium",
     "mitigation": "UNIQUE constraint + bounded retry; code length configurable", "covered_by": "test_service::test_collision_retry"},
    {"id": "R2", "risk": "Malicious / phishing / SSRF-style destination URLs", "likelihood": "high", "impact": "high",
     "mitigation": "scheme allow-list (http/https), no credentials in URL, block loopback/private/link-local hosts, length cap; prod: Safe Browsing check + abuse reporting",
     "covered_by": "test_validation"},
    {"id": "R3", "risk": "Click analytics loss or lag", "likelihood": "medium", "impact": "low",
     "mitigation": "bounded queue, drop counter, flush on shutdown; documented eventual consistency", "covered_by": "test_analytics"},
    {"id": "R4", "risk": "Hot-key / viral link overloads the datastore", "likelihood": "medium", "impact": "high",
     "mitigation": "TTL+LRU cache in front of the DB; CDN/Redis tier in production", "covered_by": "test_service::test_cache_hit"},
    {"id": "R5", "risk": "Abuse of create endpoint (spam, enumeration)", "likelihood": "high", "impact": "medium",
     "mitigation": "token-bucket rate limit per client, optional API keys, non-sequential codes", "covered_by": "test_api::test_rate_limit"},
    {"id": "R6", "risk": "Cached deleted link keeps redirecting", "likelihood": "medium", "impact": "low",
     "mitigation": "local invalidation on delete; bounded TTL (60s) for other nodes", "covered_by": "test_service::test_delete_invalidates_cache"},
    {"id": "R7", "risk": "Datastore is a single point of failure", "likelihood": "medium", "impact": "high",
     "mitigation": "Repository port; production: managed replicated store; /healthz checks DB", "covered_by": "test_api::test_healthz"},
    {"id": "R8", "risk": "PII exposure in analytics", "likelihood": "low", "impact": "high",
     "mitigation": "salted visitor hash; no raw IP persisted", "covered_by": "test_storage::test_no_raw_ip"},
    {"id": "R9", "risk": "Schema migration breaks running nodes", "likelihood": "medium", "impact": "high",
     "mitigation": "versioned, forward-only, additive migrations applied transactionally on start", "covered_by": "test_storage::test_migrations_idempotent"},
    {"id": "R10", "risk": "Generated code deviates from the contract (agentic failure mode)", "likelihood": "medium", "impact": "high",
     "mitigation": "contract conformance + schema parity + executed tests are mandatory validation gates", "covered_by": "validator"},
]

AGENT_RISKS: List[Dict[str, Any]] = [
    {"id": "A1", "risk": "Prompt-injection inside the requirement text", "mitigation": "requirement is treated as data; injection patterns are flagged and ignored; agents cannot execute free-text instructions"},
    {"id": "A2", "risk": "Agent writes outside its remit / path traversal", "mitigation": "Workspace sandbox + per-task write scopes + extension allow-list"},
    {"id": "A3", "risk": "Secrets embedded in generated code", "mitigation": "pattern scan on every write"},
    {"id": "A4", "risk": "Runaway retry / repair loops", "mitigation": "bounded attempts (3) and bounded repair rounds (2); then fail and escalate to a human"},
    {"id": "A5", "risk": "Silent bad output flowing downstream", "mitigation": "validator gate; downstream tasks of a failed task are SKIPPED, never run on bad inputs"},
    {"id": "A6", "risk": "Unreviewed change reaches release", "mitigation": "two human approval gates; release dir written only after approval; original repo never mutated"},
]

EXPIRY_RISKS: List[Dict[str, Any]] = [
    {"id": "R1", "risk": "Existing rows have no expires_at", "likelihood": "certain", "impact": "high",
     "mitigation": "nullable column; NULL means 'never expires' so old links keep working", "covered_by": "test_expiry::test_legacy_rows_never_expire"},
    {"id": "R2", "risk": "Migration fails or is not idempotent on running nodes", "likelihood": "medium", "impact": "high",
     "mitigation": "versioned migration v2, additive only, applied transactionally; old binaries ignore the column", "covered_by": "test_expiry::test_migration_from_v1"},
    {"id": "R3", "risk": "Cache serves a link past its expiry", "likelihood": "medium", "impact": "medium",
     "mitigation": "expiry is evaluated at read time on the cached object, not only at cache fill", "covered_by": "test_expiry::test_cached_link_expires"},
    {"id": "R4", "risk": "Clock skew between nodes changes expiry moment", "likelihood": "medium", "impact": "low",
     "mitigation": "UTC everywhere; expiry stored as absolute timestamp; accept skew <= NTP bound", "covered_by": "design note"},
    {"id": "R5", "risk": "API contract break for existing clients", "likelihood": "low", "impact": "high",
     "mitigation": "additive change only: optional request field, new 410 response; OpenAPI diff gate fails on removals", "covered_by": "validator::breaking_change"},
    {"id": "R6", "risk": "Expired rows accumulate", "likelihood": "high", "impact": "low",
     "mitigation": "follow-up: scheduled purge job (out of scope, documented)", "covered_by": "backlog"},
]

HARDENING_RISKS: List[Dict[str, Any]] = [
    {"id": "R1", "risk": "Optimising the wrong bottleneck", "likelihood": "high", "impact": "medium",
     "mitigation": "baseline load test first; profile before choosing between cache / replica / CDN"},
    {"id": "R2", "risk": "Security controls add latency or false positives", "likelihood": "medium", "impact": "medium",
     "mitigation": "budget p95 overhead; shadow-mode rollout for abuse scoring"},
    {"id": "R3", "risk": "Scope creep from vague goals", "likelihood": "high", "impact": "medium",
     "mitigation": "measurable acceptance criteria agreed at the clarification gate"},
    {"id": "R4", "risk": "Regression in redirect path", "likelihood": "medium", "impact": "high",
     "mitigation": "existing test-suite as a regression gate; canary rollout"},
]

SHORTENER_ACCEPTANCE: List[str] = [
    "AC-1 Given a valid https URL, POST /api/v1/links returns 201 with a 7-char code and short_url.",
    "AC-2 Given an existing code, GET /{code} returns 302 with Location equal to the original URL.",
    "AC-3 Given an unknown or deleted code, GET /{code} returns 404 with a JSON error body.",
    "AC-4 Given a custom alias that is taken, POST returns 409; if invalid, 400.",
    "AC-5 Given non-http(s), private-host or credential-bearing URLs, POST returns 400.",
    "AC-6 After redirects occur and the recorder flushes, GET /api/v1/links/{code}/stats reports total clicks, unique visitors, daily series, referrers and devices.",
    "AC-7 Links persist across process restarts (same database file).",
    "AC-8 More than the configured requests/minute from one client on POST returns 429.",
    "AC-9 openapi.json exactly matches the implemented routes (contract test).",
]

EXPIRY_ACCEPTANCE: List[str] = [
    "AC-1 POST /api/v1/links accepts optional expires_in_seconds (60..31536000); response includes expires_at (null when omitted).",
    "AC-2 GET /{code} before expiry returns 302; at/after expiry returns 410 Gone with a JSON error body.",
    "AC-3 Links created before this change (no expires_at) never expire and keep working.",
    "AC-4 Invalid expires_in_seconds (non-integer, <60, >31536000) returns 400.",
    "AC-5 Database migrates v1 -> v2 without data loss and without breaking existing endpoints.",
    "AC-6 All pre-existing tests still pass (no regression); OpenAPI diff is additive only.",
]


# ===================================================================== brownfield recipe
# Precise, anchored edits to an existing codebase. An Edit replaces one exact, unique occurrence of `find`. If the
# anchor is missing or ambiguous the patch engine raises FatalError ("anchor drift") instead of guessing: a human is
# told the codebase changed under the recipe, rather than receiving a silently mis-applied patch.
@dataclass(frozen=True)
class Edit:
    path: str
    find: str
    replace: str
    why: str


EXPIRY_EDITS: List[Edit] = [
    # ---- errors: new domain error mapped to HTTP 410 by the existing handler
    Edit("shortener/errors.py", "class AliasTaken(ShortenerError):",
         'class LinkExpired(ShortenerError):\n    status = 410\n    code = "link_expired"\n\n\nclass AliasTaken(ShortenerError):',
         "new 410 Gone error; the existing error mapping in app.py handles it automatically"),
    # ---- model
    Edit("shortener/models.py", "    long_url: str\n    created_at: str\n",
         "    long_url: str\n    created_at: str\n    expires_at: Optional[str] = None\n",
         "Link carries optional absolute expiry (default None keeps all constructors valid)"),
    # ---- storage: migration v2 + read/write of the new column
    Edit("shortener/storage.py", "]  # --- end of migrations ---",
         '    (2, "ALTER TABLE links ADD COLUMN expires_at TEXT"),\n]  # --- end of migrations ---',
         "additive nullable column; NULL means never expires"),
    Edit("shortener/storage.py", '        created_at=row["created_at"],\n    )',
         '        created_at=row["created_at"],\n        expires_at=row["expires_at"],\n    )',
         "map the new column"),
    Edit("shortener/storage.py",
         "    def create_link(self, code: str, long_url: str, created_at: str) -> Link:",
         "    def create_link(self, code: str, long_url: str, created_at: str, expires_at: Optional[str] = None) -> Link:",
         "optional parameter keeps existing callers working"),
    Edit("shortener/storage.py",
         '                    "INSERT INTO links (code, long_url, created_at) VALUES (?, ?, ?)",\n                    (code, long_url, created_at),',
         '                    "INSERT INTO links (code, long_url, created_at, expires_at) VALUES (?, ?, ?, ?)",\n                    (code, long_url, created_at, expires_at),',
         "persist expiry"),
    Edit("shortener/storage.py",
         "        return Link(code=code, long_url=long_url, created_at=created_at)",
         "        return Link(code=code, long_url=long_url, created_at=created_at, expires_at=expires_at)",
         "return the stored expiry"),
    Edit("shortener/storage.py",
         '"SELECT code, long_url, created_at FROM links WHERE code = ? AND deleted_at IS NULL"',
         '"SELECT code, long_url, created_at, expires_at FROM links WHERE code = ? AND deleted_at IS NULL"',
         "select the new column"),
    # ---- service: validation, computation, enforcement at read time
    Edit("shortener/service.py", "from .errors import AliasTaken, BadRequest, LinkNotFound, ShortenerError",
         "from .errors import AliasTaken, BadRequest, LinkExpired, LinkNotFound, ShortenerError",
         "import the new error"),
    Edit("shortener/service.py", "MAX_CODE_ATTEMPTS = 5",
         "MAX_CODE_ATTEMPTS = 5\nMIN_TTL_SECONDS = 60\nMAX_TTL_SECONDS = 365 * 24 * 3600",
         "bounds for expires_in_seconds"),
    Edit("shortener/service.py",
         "    def create_link(self, url: Any, alias: Optional[str] = None) -> Link:",
         "    def create_link(self, url: Any, alias: Optional[str] = None, expires_in_seconds: Any = None) -> Link:",
         "optional parameter keeps existing callers working"),
    Edit("shortener/service.py", "        created_at = _iso(self._now())\n",
         "        created_at = _iso(self._now())\n        expires_at = self._compute_expiry(expires_in_seconds)  # validates before any write\n",
         "compute absolute expiry once"),
    Edit("shortener/service.py", "return self.repo.create_link(validate_alias(alias), long_url, created_at)",
         "return self.repo.create_link(validate_alias(alias), long_url, created_at, expires_at)",
         "pass expiry (custom alias path)"),
    Edit("shortener/service.py", "return self.repo.create_link(code, long_url, created_at)",
         "return self.repo.create_link(code, long_url, created_at, expires_at)",
         "pass expiry (generated code path)"),
    Edit("shortener/service.py", "        link = self._lookup(code)\n        if self.recorder is not None:",
         "        link = self._lookup(code)\n        if self._is_expired(link):  # checked on every resolve, including cache hits\n"
         "            raise LinkExpired(f\"link '{code}' has expired\")\n        if self.recorder is not None:",
         "enforce expiry before recording the click"),
    Edit("shortener/service.py", "    def delete_link(self, code: str) -> None:",
         "    def _compute_expiry(self, seconds: Any) -> Optional[str]:\n"
         "        if seconds is None:\n            return None\n"
         "        if isinstance(seconds, bool) or not isinstance(seconds, int) \\\n"
         "                or not MIN_TTL_SECONDS <= seconds <= MAX_TTL_SECONDS:\n"
         "            raise BadRequest(\n"
         "                f\"expires_in_seconds must be an integer between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}\")\n"
         "        return _iso(self._now() + timedelta(seconds=seconds))\n\n"
         "    def _is_expired(self, link: Link) -> bool:\n"
         "        return link.expires_at is not None and self._now() >= datetime.fromisoformat(link.expires_at)\n\n"
         "    def delete_link(self, code: str) -> None:",
         "expiry helpers"),
    # ---- http layer
    Edit("shortener/app.py", 'link = app.service.create_link(body.get("url"), body.get("custom_alias"))',
         'link = app.service.create_link(body.get("url"), body.get("custom_alias"), body.get("expires_in_seconds"))',
         "accept the optional request field"),
    Edit("shortener/app.py", '            "created_at": link.created_at,\n        }',
         '            "created_at": link.created_at,\n            "expires_at": link.expires_at,\n        }',
         "expose expires_at in responses"),
]

# Used by the defect-injection harness: dropping this edit simulates an incomplete patch.
EXPIRY_DEFECT_SKIPS = {"shortener/app.py": 'link = app.service.create_link(body.get("url"), body.get("custom_alias"))'}
