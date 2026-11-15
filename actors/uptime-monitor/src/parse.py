import ipaddress
import re

from urllib.parse import urlparse, urlunparse

_SCHEME_LIKE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:$")
_HOST_RE = re.compile(r"[a-z0-9.\-]+")


def normalize_url(raw: str) -> str:
    """Bare domains become https://domain/; scheme, host case and trailing slash
    are normalized. The query string is KEPT: https://e.com/a?token=1 and
    https://e.com/a?token=2 are different targets and are both checked.

    Public IPv6 literals are allowed and bracketed. Internationalized domain
    names are converted to punycode. URLs with embedded credentials
    (https://user:pass@host/) are rejected: silently stripping them would
    make two different targets share one cache key and one bill. Returns ""
    when the URL is not usable (bad scheme, no host, bad port, userinfo).
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        if _SCHEME_LIKE.match(raw):
            return ""
        raw = "https://" + raw
    try:
        p = urlparse(raw)
    except Exception:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    if p.username is not None or p.password is not None:
        return ""
    host = p.hostname.lower()
    if any(ord(c) > 127 for c in host):
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return ""
        host = f"[{host}]"
    elif not _HOST_RE.fullmatch(host):
        return ""
    try:
        port = p.port
    except ValueError:
        return ""
    default_port = 443 if p.scheme == "https" else 80
    if port is not None and port != default_port:
        host = f"{host}:{port}"
    path = p.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunparse((p.scheme, host, path, p.params, p.query, ""))


def parse_rows(urls):
    """Split input into usable entries and invalid ones; duplicate normalized URLs are kept once."""
    seen = set()
    seen_raw = set()
    rows = []
    for u in urls or []:
        raw = (u or "").strip()
        norm = normalize_url(raw)
        if not norm:
            if raw and raw not in seen_raw:
                seen_raw.add(raw)
                rows.append({"raw": raw, "kind": "invalid"})
            continue
        if norm in seen:
            continue
        seen.add(norm)
        rows.append({"raw": raw, "kind": "ok", "norm": norm})
    return rows
