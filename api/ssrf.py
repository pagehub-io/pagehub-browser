"""SSRF deny-list for the navigate route layer (runs before any engine call).

A minimal hardcoded deny-list per PLAN's Security section. A configurable
allowlist is a documented follow-up, not v1.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from api.config import settings

_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        # IPv4
        "127.0.0.0/8",          # loopback
        "10.0.0.0/8",           # RFC1918
        "172.16.0.0/12",        # RFC1918
        "192.168.0.0/16",       # RFC1918
        "169.254.0.0/16",       # link-local + cloud metadata 169.254.169.254
        "0.0.0.0/8",            # "this host" / unspecified
        "100.64.0.0/10",        # RFC6598 CGNAT
        "192.0.0.0/24",         # IETF protocol assignments
        "192.0.2.0/24",         # TEST-NET-1
        "198.51.100.0/24",      # TEST-NET-2
        "203.0.113.0/24",       # TEST-NET-3
        "198.18.0.0/15",        # benchmarking
        "224.0.0.0/4",          # multicast
        "240.0.0.0/4",          # reserved
        # IPv6
        "::1/128",              # loopback
        "::/128",               # unspecified
        "::ffff:0:0/96",        # IPv4-mapped (also handled by unwrapping)
        "64:ff9b::/96",         # NAT64
        "fc00::/7",             # ULA (covers fd00::/8)
        "fe80::/10",            # link-local
        "ff00::/8",             # multicast
        "2001:db8::/32",        # documentation
        "100::/64",             # discard-only
    )
)


class NavigationBlocked(Exception):
    """Raised by the route layer when a navigate target fails the SSRF check.

    reason ∈ {"malformed URL", "unsupported scheme", "host not allowed"}.
    """

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"Refused to navigate to '{url}': {reason}.")


def _private_hosts_allowed() -> bool:
    """The dev-only escape hatch — read at call time so tests can flip the setting.

    Honored ONLY when ENV is exactly "development" (deny-by-default; staging / qa / prod
    ignore the flag) AND BROWSER_ALLOW_PRIVATE_HOSTS is explicitly true. Lets the local
    docker-compose loop navigate to a localhost / docker-bridge target frontend; the
    http(s)-only scheme allowlist still applies (file:// etc. stay blocked).
    """
    return settings.env == "development" and settings.browser_allow_private_hosts


def _normalize_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) to its v4 form for range checks."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = _normalize_ip(ip)
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    return any(ip in net for net in _BLOCKED_NETWORKS)


def check_navigate_url(url: str) -> None:
    """Raise NavigationBlocked if `url` is not a safe public http(s) target.

    Order: parse -> scheme allowlist -> hostname checks -> literal-IP range check
    -> DNS resolution + range check on every answer (fail closed). On a DNS *failure*
    this returns normally — the engine will surface a 502 NavigationError; that is not
    an SSRF block.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:  # pragma: no cover - urlsplit rarely raises
        raise NavigationBlocked(url, "malformed URL") from exc

    if not parts.scheme:
        raise NavigationBlocked(url, "malformed URL")
    if parts.scheme.lower() not in ("http", "https"):
        raise NavigationBlocked(url, "unsupported scheme")

    hostname = parts.hostname
    if not hostname:
        raise NavigationBlocked(url, "malformed URL")

    if _private_hosts_allowed():
        return  # dev escape hatch: scheme validated; private/loopback/link-local permitted

    host_lower = hostname.lower()
    if host_lower in ("localhost", "localhost.localdomain") or host_lower.endswith(".localhost"):
        raise NavigationBlocked(url, "host not allowed")

    try:
        literal = ipaddress.ip_address(host_lower)
    except ValueError:
        literal = None

    if literal is not None:
        if _ip_is_blocked(literal):
            raise NavigationBlocked(url, "host not allowed")
        return

    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return  # DNS failure -> let the engine return a 502, not an SSRF block

    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        # strip a possible zone id (e.g. fe80::1%eth0)
        addr = addr.split("%", 1)[0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:  # pragma: no cover
            continue
        if _ip_is_blocked(resolved):
            raise NavigationBlocked(url, "host not allowed")


def navigation_request_is_blocked(url: str) -> bool:
    """Literal-IP / scheme check for the in-page navigation interceptor — NO DNS.

    Used by PlaywrightEngine's context.route handler on redirect targets in the hot path.
    """
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover
        return True
    if parts.scheme.lower() not in ("http", "https"):
        return True
    hostname = parts.hostname
    if not hostname:
        return True
    if _private_hosts_allowed():
        return False  # dev escape hatch — see _private_hosts_allowed
    host_lower = hostname.lower()
    if host_lower in ("localhost", "localhost.localdomain") or host_lower.endswith(".localhost"):
        return True
    try:
        literal = ipaddress.ip_address(host_lower)
    except ValueError:
        return False  # a hostname; no DNS in the hot path — pre-check covered the initial target
    return _ip_is_blocked(literal)
