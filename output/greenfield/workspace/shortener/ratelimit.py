"""Per-node token buckets with bounded client state and monotonic time."""

import threading
import time
from typing import Callable


class TokenBucketLimiter:
    def __init__(self, per_minute: int, max_clients: int = 100000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(per_minute, int) or isinstance(per_minute, bool) or per_minute <= 0:
            raise ValueError("rate_limit_per_minute must be a positive integer")
        self.capacity = per_minute
        self.rate = per_minute / 60.0
        self.max_clients = max_clients
        self.clock = clock
        self._clients: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._next_cleanup = 0.0

    def allow(self, client: str) -> bool:
        with self._lock:
            now = self.clock()
            if now >= self._next_cleanup:
                self._clients = {key: value for key, value in self._clients.items() if now - value[1] < 60.0}
                self._next_cleanup = now + 30.0
            old = self._clients.get(client)
            if old is None:
                # Fail closed rather than evicting active buckets and enabling bypass.
                if len(self._clients) >= self.max_clients:
                    return False
                tokens = float(self.capacity)
            else:
                tokens = min(float(self.capacity), old[0] + max(0.0, now - old[1]) * self.rate)
            allowed = tokens >= 1.0
            self._clients[client] = (tokens - 1.0 if allowed else tokens, now)
            return allowed
