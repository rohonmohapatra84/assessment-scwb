"""Loss-bounded, asynchronous click recording with serialized durable flushes."""

import hashlib
import logging
import queue
import threading

from .storage import Click, Repository, utc_now


def device_class(user_agent: str) -> str:
    ua = user_agent.lower()
    if not ua.strip():
        return "unknown"
    if any(word in ua for word in ("bot", "spider", "crawler", "slurp", "headless")):
        return "bot"
    if "ipad" in ua or "tablet" in ua or ("android" in ua and "mobile" not in ua):
        return "tablet"
    if any(word in ua for word in ("mobile", "iphone", "ipod", "android")):
        return "mobile"
    if any(word in ua for word in ("mozilla", "windows", "macintosh", "linux", "chrome", "safari")):
        return "desktop"
    return "unknown"


class Analytics:
    def __init__(self, repository: Repository, capacity: int = 10000,
                 batch_size: int = 256, interval: float = 0.5) -> None:
        if capacity <= 0 or batch_size <= 0 or interval <= 0:
            raise ValueError("Invalid analytics configuration")
        self.repository = repository
        self._salt = repository.visitor_salt
        self._queue: queue.Queue[Click] = queue.Queue(maxsize=capacity)
        self._batch_size = batch_size
        self._interval = interval
        self._flush_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: list[Click] = []
        self._dropped = 0
        self._accepting = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="shortener-analytics", daemon=True)
        self._thread.start()

    @property
    def dropped_events(self) -> int:
        with self._state_lock:
            return self._dropped

    def record(self, code: str, ip: str, user_agent: str, referrer: str | None) -> None:
        # Length-prefixed identity avoids ambiguous concatenations. Raw IP is
        # used only transiently in this method, never queued, logged, or stored.
        ip_bytes = ip.encode("utf-8", errors="replace")
        ua_bytes = user_agent.encode("utf-8", errors="replace")
        visitor = hashlib.sha256(self._salt + len(ip_bytes).to_bytes(8, "big") + ip_bytes + ua_bytes).hexdigest()
        safe_referrer = None
        if referrer:
            safe_referrer = "".join(c for c in referrer[:2048] if ord(c) >= 32 and ord(c) != 127)
        event = Click(code, utc_now(), visitor, safe_referrer, device_class(user_agent))
        with self._state_lock:
            if not self._accepting:
                self._dropped += 1
                return
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._dropped += 1

    def _write_pending(self) -> None:
        if self._pending:
            self.repository.write_clicks(self._pending)
            for _ in self._pending:
                self._queue.task_done()
            self._pending = []

    def flush(self) -> None:
        """Persist everything queued before this call obtains the flush lock.

        Background and foreground drains share this lock, including database
        writes. Failed batches remain pending for retry and are not duplicated.
        A snapshot boundary prevents continuous traffic from starving callers.
        """
        with self._flush_lock:
            remaining = self._queue.qsize()
            self._write_pending()
            while remaining:
                self._pending = [self._queue.get_nowait() for _ in range(min(remaining, self._batch_size))]
                remaining -= len(self._pending)
                self._write_pending()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.flush()
            except Exception:
                # Do not log event contents, request data, or database exceptions.
                logging.getLogger(__name__).error("Analytics flush failed; buffered batch retained")

    def close(self) -> None:
        with self._state_lock:
            self._accepting = False
        self._stop.set()
        self._thread.join()
        self.flush()
