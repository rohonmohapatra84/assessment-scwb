"""URL validation and safe HTTP Location rendering.

DNS names are resolved on creation, never on the redirect hot path. Resolution
failure is rejected rather than silently bypassing private-address validation.
The service does not fetch destinations; subsequent DNS rebinding cannot cause
server-side requests. Production deployments should also enforce abuse policy
and DNS timeouts at their network boundary.
"""

import ipaddress
import re
import socket
from urllib.parse import quote, urlsplit, urlunsplit

from .errors import APIError


def validate_alias(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", value):
        raise APIError(400, "invalid_alias", "Alias must contain 3-32 letters, digits, underscores or hyphens.")
    if value.lower() in {"api", "healthz", "openapi.json", "admin"}:
        raise APIError(400, "invalid_alias", "Alias is reserved.")
    return value


def validate_url(value: object) -> str:
    def invalid() -> APIError:
        return APIError(400, "invalid_url", "URL must be a public HTTP or HTTPS URL without credentials.")

    if not isinstance(value, str) or not value or len(value) > 2048:
        raise invalid()
    if any(c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 for c in value) or "\\" in value:
        raise invalid()
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            raise ValueError("scheme or authority")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise ValueError("credentials")
        host = parts.hostname
        if not host or "%" in host:
            raise ValueError("host")
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("port")
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")):
            raise ValueError("local host")
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            # Reject legacy numeric spellings such as 127.1 and 0x7f000001.
            try:
                addresses = [ipaddress.ip_address(socket.inet_aton(host))]
            except OSError:
                labels = host.split(".")
                if len(labels) < 2 or len(host) > 253 or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in labels
                ):
                    raise ValueError("hostname")
                addresses = [ipaddress.ip_address(item[4][0])
                             for item in socket.getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)]
        if not addresses:
            raise ValueError("unresolved")
        for address in addresses:
            mapped = getattr(address, "ipv4_mapped", None)
            address = mapped or address
            if not address.is_global or address.is_multicast or address.is_reserved:
                raise ValueError("non-public address")
        # Ensure unicode can safely be represented in both JSON and Location.
        value.encode("utf-8")
        location_url(value)
    except (ValueError, UnicodeError, OSError):
        raise invalid() from None
    return value


def location_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").encode("idna").decode("ascii")
    if ":" in host:
        host = "[" + host + "]"
    authority = host + ((":" + str(parts.port)) if parts.port is not None else "")
    return urlunsplit((parts.scheme, authority, quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                       quote(parts.query, safe="/%?:@!$&'()*+,;=-._~"),
                       quote(parts.fragment, safe="/%?:@!$&'()*+,;=-._~")))
