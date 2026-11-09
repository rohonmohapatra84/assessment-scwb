"""Bounded in-process TTL/LRU cache. Cross-node invalidation needs Redis."""

from collections import OrderedDict
import threading
import time

from .storage import Link


class LinkCache:
    def __init__(self, capacity: int = 10000, ttl: float = 30.0) -> None:
        if capacity < 1 or ttl <= 0:
            raise ValueError("Invalid cache configuration")
        self.capacity = capacity
        self.ttl = ttl
        self._items: OrderedDict[str, tuple[float, Link]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, code: str) -> Link | None:
        with self._lock:
            item = self._items.get(code)
            if item is None:
                return None
            if item[0] <= time.monotonic():
                del self._items[code]
                return None
            self._items.move_to_end(code)
            return item[1]

    def put(self, link: Link) -> None:
        with self._lock:
            self._items[link.code] = (time.monotonic() + self.ttl, link)
            self._items.move_to_end(link.code)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

    def invalidate(self, code: str) -> None:
        with self._lock:
            self._items.pop(code, None)
