# Risks and mitigations

## Service risks

| ID | Risk | Likelihood | Impact | Mitigation | Covered by |
|---|---|---|---|---|---|
| R1 | Short-code collision under concurrent inserts | low | medium | UNIQUE constraint + bounded retry; code length configurable | `test_service::test_collision_retry` |
| R2 | Malicious / phishing / SSRF-style destination URLs | high | high | scheme allow-list (http/https), no credentials in URL, block loopback/private/link-local hosts, length cap; prod: Safe Browsing check + abuse reporting | `test_validation` |
| R3 | Click analytics loss or lag | medium | low | bounded queue, drop counter, flush on shutdown; documented eventual consistency | `test_analytics` |
| R4 | Hot-key / viral link overloads the datastore | medium | high | TTL+LRU cache in front of the DB; CDN/Redis tier in production | `test_service::test_cache_hit` |
| R5 | Abuse of create endpoint (spam, enumeration) | high | medium | token-bucket rate limit per client, optional API keys, non-sequential codes | `test_api::test_rate_limit` |
| R6 | Cached deleted link keeps redirecting | medium | low | local invalidation on delete; bounded TTL (60s) for other nodes | `test_service::test_delete_invalidates_cache` |
| R7 | Datastore is a single point of failure | medium | high | Repository port; production: managed replicated store; /healthz checks DB | `test_api::test_healthz` |
| R8 | PII exposure in analytics | low | high | salted visitor hash; no raw IP persisted | `test_storage::test_no_raw_ip` |
| R9 | Schema migration breaks running nodes | medium | high | versioned, forward-only, additive migrations applied transactionally on start | `test_storage::test_migrations_idempotent` |
| R10 | Generated code deviates from the contract (agentic failure mode) | medium | high | contract conformance + schema parity + executed tests are mandatory validation gates | `validator` |
