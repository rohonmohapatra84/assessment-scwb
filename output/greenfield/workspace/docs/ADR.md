# Architecture Decision Records

## ADR-001: Short-code generation: random base62 with unique-constraint retry

- **Options:** random base62 (7 chars), auto-increment counter + base62, hash(url) truncation, pre-generated key service
- **Decision:** random base62, 7 chars, retry on unique violation
- **Why:** No cross-node coordination, codes are non-enumerable, per-insert collision probability is n/62^7 (~0.03% at 1B links) so a bounded retry loop is enough.
- **Trade-offs:** Needs a unique index and a retry path; counters are collision-free but leak volume and are enumerable; hash truncation makes dedup easy but collides on truncation and leaks URL equality.

## ADR-002: Persistence behind a Repository port; SQLite for the prototype

- **Options:** SQLite, PostgreSQL, DynamoDB/Cassandra, Redis only
- **Decision:** Repository interface + SQLite(WAL) now; PostgreSQL (hash-sharded by code) or DynamoDB in production
- **Why:** Access pattern is pure key-value (code -> url) with ~100:1 reads; the port isolates storage choice from business logic so scale-out is an adapter swap, not a rewrite.
- **Trade-offs:** SQLite is single-writer and single-node: fine for a runnable, zero-dependency prototype, not for the stated scale.

## ADR-003: Redirect uses HTTP 302 (configurable to 301)

- **Options:** 301 permanent, 302 temporary
- **Decision:** 302 by default
- **Why:** Browsers cache 301s, so repeat clicks bypass us and analytics under-count; 302 keeps every click observable and allows later edits/expiry.
- **Trade-offs:** Higher redirect traffic and no browser/CDN caching offload; 301 is cheaper if analytics are not needed.

## ADR-004: Analytics are off the redirect hot path (buffered, batched, lossy under overload)

- **Options:** synchronous insert per click, bounded in-memory queue + batch flush, message broker (Kafka) + stream processor
- **Decision:** bounded queue + background batch flusher; Kafka/ClickHouse is the production path
- **Why:** Redirect latency must not depend on write throughput of the analytics store. Batching turns N inserts into 1 transaction.
- **Trade-offs:** Clicks can be lost on crash (<= flush interval) or dropped when the queue is full (counted in a metric). Stats are eventually consistent.

## ADR-005: Read-through TTL/LRU cache for redirect lookups

- **Options:** no cache, in-process LRU, Redis, CDN edge cache
- **Decision:** in-process TTL+LRU (Redis in production)
- **Why:** Traffic is Zipfian; a small cache absorbs the hot set and cuts DB reads dramatically.
- **Trade-offs:** Deleted links can still redirect for up to TTL on other nodes; the local node invalidates immediately.

## ADR-006: Privacy-preserving analytics

- **Options:** store raw IP + UA, store salted hash of IP+UA
- **Decision:** salted SHA-256 visitor hash; raw IP never stored
- **Why:** Unique-visitor counts without retaining personal data (GDPR data minimisation).
- **Trade-offs:** Hash of IP+UA is an approximation (NAT, UA changes); cannot be reversed for debugging.

## ADR-007: Stateless service tier; per-node rate limiting

- **Options:** sticky sessions, stateless nodes behind a load balancer
- **Decision:** stateless; token-bucket rate limit per client per node
- **Why:** Any node can serve any request, so scaling is adding nodes.
- **Trade-offs:** Per-node limits are approximate (effective limit = limit x nodes); a Redis-backed limiter makes it exact.

## ADR-008: Contract-first API (OpenAPI 3) enforced by tests

- **Options:** code-first docs, contract-first
- **Decision:** openapi.json is generated first and a contract test fails if routes drift
- **Why:** Parallel agents (code, tests, docs) coordinate through one contract instead of guessing each other's output.
- **Trade-offs:** Spec and code must be kept in sync; the contract test is what makes that cheap.
