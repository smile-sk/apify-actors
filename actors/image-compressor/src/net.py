"""Url input and the address guard.

The actor downloads whatever address it is given, so the same rule as in the rest of
the park applies: every address we resolve must be globally routable. A private,
loopback, link-local or CGNAT address is refused before the request, and every hop of
the redirect chain is checked afterwards, because a public url that answers with 302
to 169.254.169.254 would otherwise reach the container metadata and carry the platform
token away.

`is_global` is the predicate on purpose: a list of flags (is_private, is_loopback,
is_link_local, is_reserved) misses 100.64.0.0/10, the CGNAT range that is exactly the
pod network in Kubernetes.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LEN = 2048

# The guard answers with a code next to the reason. A diagnosis read from a wording
# ("not a public address" in the text) flips the moment somebody rewords the sentence,
# and a private address then goes out as a plain dns error.
CODE_OK = "ok"
CODE_PRIVATE = "private"
CODE_DNS = "dns"
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def _strip(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    scheme = (parts.scheme or "").lower()
    port = parts.port
    # An IPv6 literal must keep its brackets. Without them the rebuilt url is not a
    # url at all, the guard saw an empty host, and a private address was reported as
    # a dns error.
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def normalize_url(line: str) -> tuple[str | None, str | None]:
    """A clean url, or a plain reason why this line is not one."""
    text = "" if line is None else str(line).strip()
    if not text:
        return None, "an empty line"
    if len(text) > MAX_URL_LEN:
        return None, f"the url is longer than {MAX_URL_LEN} characters"
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        return None, f"the url cannot be parsed: {exc}"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None, f"the scheme '{scheme or 'none'}' is not http or https"
    if parts.username or parts.password:
        return None, "a url with credentials embedded is not accepted"
    try:
        host = parts.hostname or ""
        parts.port  # noqa: B018 - reading it raises on a malformed port
    except ValueError:
        return None, "the port is not a number"
    if not host:
        return None, "the url has no host"
    bad = _bad_host(host)
    if bad:
        return None, bad
    return _strip(text), None


def _bad_host(host: str) -> str | None:
    """A host name that cannot be resolved is a reason, not a page to measure."""
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return "the host is not a valid IPv6 address"
        return None
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return "the host name is not a valid name"
    if not _HOST_RE.match(ascii_host):
        return f"'{host}' is not a valid host name"
    return None


def resolve(host: str, resolver=None) -> list[ipaddress._BaseAddress]:
    """Every address the name answers with, v4 and v6."""
    lookup = resolver or socket.getaddrinfo
    infos = lookup(host, None)
    found = []
    for info in infos:
        raw = info[4][0]
        try:
            found.append(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return found


def host_verdict(host: str, resolver=None) -> tuple[bool, str, str | None]:
    """True only when every address of the host is globally routable, with the code.

    The code separates "resolved into a non public address" (private, refused) from
    "did not resolve at all" (dns, a typo in the buyer's list). They are different
    answers and the buyer is owed the right one.
    """
    name = (host or "").strip().strip("[]").lower()
    if not name:
        return False, CODE_DNS, "the url has no host"
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        literal = None
    if literal is not None:
        ok, reason = _address_ok(literal)
        return ok, (CODE_OK if ok else CODE_PRIVATE), reason
    try:
        addresses = resolve(name, resolver)
    except socket.gaierror as exc:
        return False, CODE_DNS, f"the host name does not resolve: {exc}"
    except (UnicodeError, OSError) as exc:
        # A label longer than 63 characters raises UnicodeError, not gaierror, and
        # that is exactly how one junk line used to kill a whole run.
        return False, CODE_DNS, f"the host name is not a valid name: {type(exc).__name__}"
    if not addresses:
        return False, CODE_DNS, "the host name resolves to no address"
    for address in addresses:
        ok, reason = _address_ok(address)
        if not ok:
            return False, CODE_PRIVATE, reason
    return True, CODE_OK, None


def host_is_public(host: str, resolver=None) -> tuple[bool, str | None]:
    """The two value form, kept for callers that only need yes or no."""
    ok, _code, reason = host_verdict(host, resolver)
    return ok, reason


def _address_ok(address) -> tuple[bool, str | None]:
    real = getattr(address, "ipv4_mapped", None) or address
    if real.is_global:
        return True, None
    return False, f"the address {real} is not a public address"


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
