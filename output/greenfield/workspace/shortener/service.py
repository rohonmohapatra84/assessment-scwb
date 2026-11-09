"""Business rules depend on ports, not HTTP or concrete SQL."""

import secrets
import string
import threading
from typing import Any

from .analytics import Analytics
from .cache import LinkCache
from .errors import APIError, CodeConflict
from .storage import Link, Repository, utc_now
from .validation import validate_alias, validate_url


class LinkService:
    def __init__(self, repository: Repository, cache: LinkCache, analytics: Analytics,
                 base_url: str) -> None:
        self.repository = repository
        self.cache = cache
        self.analytics = analytics
        self.base_url = base_url.rstrip("/")
        # Prevent a same-node cache fill racing a delete and resurrecting a link.
        self._cache_fill_lock = threading.Lock()

    def generate_code(self) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(7))

    def representation(self, link: Link) -> dict[str, str]:
        return {"code": link.code, "short_url": self.base_url + "/" + link.code,
                "long_url": link.long_url, "created_at": link.created_at}

    def create(self, body: dict[str, Any]) -> dict[str, str]:
        url = validate_url(body.get("url"))
        alias = validate_alias(body["custom_alias"]) if "custom_alias" in body else None
        for _ in range(1 if alias else 5):
            link = Link(alias or self.generate_code(), url, utc_now())
            try:
                self.repository.insert_link(link)
            except CodeConflict:
                if alias:
                    raise APIError(409, "alias_taken", "Alias is already taken.") from None
                continue
            return self.representation(link)
        raise APIError(503, "code_exhausted", "Unable to allocate a short code. Please retry.")

    def lookup(self, code: str) -> Link:
        with self._cache_fill_lock:
            link = self.cache.get(code)
            if link is None:
                link = self.repository.get_link(code)
                if link is not None:
                    self.cache.put(link)
            if link is None:
                raise APIError(404, "not_found", "Link not found.")
            return link

    def metadata(self, code: str) -> dict[str, str]:
        # Metadata is authoritative even when another process deleted a cached link.
        link = self.repository.get_link(code)
        if link is None:
            raise APIError(404, "not_found", "Link not found.")
        return self.representation(link)

    def delete(self, code: str) -> None:
        with self._cache_fill_lock:
            deleted = self.repository.delete_link(code, utc_now())
            self.cache.invalidate(code)
            if not deleted:
                raise APIError(404, "not_found", "Link not found.")

    def stats(self, code: str, days: int) -> dict[str, Any]:
        self.metadata(code)
        self.analytics.flush()
        return self.repository.stats(code, days)
