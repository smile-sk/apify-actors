"""Host parsing and private-address guard. No apify imports here so tests
can run in a bare venv.
"""

import re
import socket

HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)+$")

_PRIVATE_NETS = None


def _private_prefixes():
    import ipaddress
    return [
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("x.x.x.x/8"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("x.x.x.x/16"),
        ipaddress.ip_network("x.x.x.x/12"),
        ipaddress.ip_network("x.x.x.x/16"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("fe80::/10"),
    ]


def is_private(host):
    """True when ANY resolved address of host is private/loopback/link-local."""
    import ipaddress
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    nets = _private_prefixes()
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        for net in nets:
            if ip in net:
                return True
    return False


def parse_host(raw):
    """Return (host, port) or (None, None) for invalid input."""
    if not isinstance(raw, str):
        return None, None
    s = raw.strip().lower()
    if not s:
        return None, None
    for bad in ("\x00", "\r", "\n"):
        if bad in s:
            return None, None
    # strip scheme and path
    s = re.sub(r"^[a-z][a-z0-9+\-.]*://", "", s)
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.split("@", 1)[-1]
    host = s
    port = 443
    if s.startswith("["):
        end = s.find("]")
        if end == -1:
            return None, None
        host = s[1:end]
        rest = s[end + 1:]
        if rest:
            if not rest.startswith(":"):
                return None, None
            try:
                port = int(rest[1:])
            except ValueError:
                return None, None
    elif ":" in s:
        host, _, p = s.rpartition(":")
        try:
            port = int(p)
        except ValueError:
            return None, None
    host = host.rstrip(".")
    if not HOST_RE.match(host):
        return None, None
    if not (1 <= port <= 65535):
        return None, None
    return host, port
