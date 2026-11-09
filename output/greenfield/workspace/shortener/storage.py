"""SQL-only repository adapter with transactional, versioned migrations.

One serialized connection makes :memory: and file databases behave identically.
SQLite is the single-node adapter, not a distributed storage implementation.
Production adapters implement Repository against shared PostgreSQL or DynamoDB.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
import sqlite3
import threading
from typing import Any, Protocol, Sequence

from .contract import SCHEMA_SQL
from .errors import CodeConflict


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Link:
    code: str
    long_url: str
    created_at: str


@dataclass(frozen=True)
class Click:
    code: str
    ts: str
    visitor_hash: str
    referrer: str | None
    device: str


class Repository(Protocol):
    @property
    def visitor_salt(self) -> bytes: ...
    def insert_link(self, link: Link) -> None: ...
    def get_link(self, code: str) -> Link | None: ...
    def delete_link(self, code: str, deleted_at: str) -> bool: ...
    def write_clicks(self, events: Sequence[Click]) -> None: ...
    def stats(self, code: str, days: int) -> dict[str, Any]: ...
    def health(self) -> bool: ...
    def close(self) -> None: ...


class SQLiteRepository:
    def __init__(self, db_path: str) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(db_path, timeout=10, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA busy_timeout = 10000")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            self._migrate()
        except BaseException:
            self._db.close()
            raise

    def _migrate(self) -> None:
        # executescript implicitly commits; individual statements preserve atomicity.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                visitor_salt TEXT NOT NULL
            )""")
            versions = self._db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            if any(row[0] != 1 for row in versions):
                raise RuntimeError("Unsupported database schema version")
            if not versions:
                for statement in SCHEMA_SQL.split(";"):
                    if statement.strip():
                        self._db.execute(statement)
                self._db.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                                 (1, utc_now(), secrets.token_hex(32)))
            row = self._db.execute("SELECT visitor_salt FROM schema_migrations WHERE version = 1").fetchone()
            self._visitor_salt = bytes.fromhex(row[0])
            if len(self._visitor_salt) != 32:
                raise RuntimeError("Invalid analytics salt")
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    @property
    def visitor_salt(self) -> bytes:
        return self._visitor_salt

    def insert_link(self, link: Link) -> None:
        with self._lock:
            try:
                self._db.execute("INSERT INTO links (code, long_url, created_at) VALUES (?, ?, ?)",
                                 (link.code, link.long_url, link.created_at))
            except sqlite3.IntegrityError as exc:
                if getattr(exc, "sqlite_errorcode", None) in {
                    sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY, sqlite3.SQLITE_CONSTRAINT_UNIQUE
                } or "UNIQUE constraint failed: links.code" in str(exc):
                    raise CodeConflict() from None
                raise

    def get_link(self, code: str) -> Link | None:
        with self._lock:
            row = self._db.execute("SELECT code, long_url, created_at FROM links WHERE code = ? AND deleted_at IS NULL",
                                   (code,)).fetchone()
            return Link(**dict(row)) if row else None

    def delete_link(self, code: str, deleted_at: str) -> bool:
        with self._lock:
            cursor = self._db.execute("UPDATE links SET deleted_at = ? WHERE code = ? AND deleted_at IS NULL",
                                      (deleted_at, code))
            return cursor.rowcount == 1

    def write_clicks(self, events: Sequence[Click]) -> None:
        if not events:
            return
        daily: dict[tuple[str, str], int] = {}
        for event in events:
            key = (event.code, event.ts[:10])
            daily[key] = daily.get(key, 0) + 1
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.executemany(
                    "INSERT INTO click_events (code, ts, visitor_hash, referrer, device) VALUES (?, ?, ?, ?, ?)",
                    [(e.code, e.ts, e.visitor_hash, e.referrer, e.device) for e in events])
                self._db.executemany("""INSERT INTO click_daily (code, day, clicks) VALUES (?, ?, ?)
                    ON CONFLICT(code, day) DO UPDATE SET clicks = click_daily.clicks + excluded.clicks""",
                    [(code, day, clicks) for (code, day), clicks in daily.items()])
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def stats(self, code: str, days: int) -> dict[str, Any]:
        # Windows are UTC calendar days, including today; all metrics use that window.
        today = datetime.now(timezone.utc).date()
        start = (today - timedelta(days=days - 1)).isoformat()
        end = (today + timedelta(days=1)).isoformat()
        with self._lock:
            self._db.execute("BEGIN")
            try:
                totals = self._db.execute("""SELECT COUNT(*), COUNT(DISTINCT visitor_hash)
                    FROM click_events WHERE code = ? AND ts >= ? AND ts < ?""", (code, start, end)).fetchone()
                daily = self._db.execute("""SELECT day, clicks FROM click_daily
                    WHERE code = ? AND day >= ? AND day < ? ORDER BY day""", (code, start, end)).fetchall()
                referrers = self._db.execute("""SELECT COALESCE(referrer, '') AS referrer, COUNT(*) AS clicks
                    FROM click_events WHERE code = ? AND ts >= ? AND ts < ?
                    GROUP BY COALESCE(referrer, '') ORDER BY clicks DESC, referrer LIMIT 10""",
                    (code, start, end)).fetchall()
                devices = self._db.execute("""SELECT device, COUNT(*) AS clicks FROM click_events
                    WHERE code = ? AND ts >= ? AND ts < ? GROUP BY device ORDER BY clicks DESC, device""",
                    (code, start, end)).fetchall()
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return {"code": code, "total_clicks": totals[0], "unique_visitors": totals[1], "window_days": days,
                "clicks_by_day": [dict(row) for row in daily],
                "top_referrers": [dict(row) for row in referrers], "devices": [dict(row) for row in devices]}

    def health(self) -> bool:
        with self._lock:
            try:
                self._db.execute("SELECT code FROM links LIMIT 1").fetchone()
                return True
            except sqlite3.Error:
                return False

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
