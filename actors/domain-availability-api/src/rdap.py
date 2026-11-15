"""Domain parsing and RDAP lookups.

The verdict comes from the registry through RDAP (the modern WHOIS).
rdap.org is a bootstrap service: it redirects to the authoritative RDAP
server of the TLD, so one URL covers every registry that publishes RDAP.

Rules of the product:
  * a verdict is definite only when the registry answered: 200 = registered,
    404 = available;
  * anything else (timeout, 429, 5xx, unparsable body) is UNKNOWN and is not
    sold, because we did not learn the answer.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"

# The IANA bootstrap maps a TLD to the registry that answers RDAP for it. A TLD
# outside that map has no RDAP at all: rdap.org answers a 404 notice for it, and
# a 404 notice is NOT a verdict of "free". Such a TLD comes back unknown and free.
#
# The four below are missing from the bootstrap but do answer RDAP. Measured
# 2026-11-09 and again on 2026-11-13: a registered name in each TLD returned 200
# with an ldhName, so these bases are answers, not guesses.
FALLBACK_BASES: dict[str, str] = {
    "io": "https://rdap.identitydigital.services/rdap",
    "me": "https://rdap.identitydigital.services/rdap",
    "us": "https://rdap.nic.us",
    "de": "https://rdap.denic.de",
}

# One registry is one host: asking it faster than it likes turns a run into 429s.
MIN_HOST_INTERVAL = 0.2
RETRY_DELAY_S = 2.0

_bootstrap: dict[str, str] | None = None
_bootstrap_lock = asyncio.Lock()
_host_locks: dict[str, asyncio.Lock] = {}
_host_last: dict[str, float] = {}

# A registry with no RDAP for a TLD answers 404 with a notice in the body.
NO_SERVICE = re.compile(r"no rdap service|does not (?:support|provide) rdap|no service", re.I)

VERDICT_AVAILABLE = "available"
VERDICT_REGISTERED = "registered"
VERDICT_UNKNOWN = "unknown"

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$")


def _clean(raw: str) -> str:
    text = (raw or "").strip().strip("<>\"'")
    text = text.lower()
    for scheme in ("https://", "http://", "//"):
        if text.startswith(scheme):
            text = text[len(scheme):]
    if "@" in text:
        return text
    for cut in ("/", "?", "#", ":"):
        if cut in text:
            text = text.split(cut, 1)[0]
    return text.strip().rstrip(".")


def parse_domain(raw: str) -> tuple[str | None, str | None, str | None]:
    """Return (ascii_domain, tld, error). Exactly one of domain/error is set."""
    text = _clean(raw)
    if not text:
        return None, None, "the line is empty"
    if "@" in text:
        text = text.rsplit("@", 1)[-1].strip()
    if " " in text or not text:
        return None, None, "this is not a domain name: it has a space in it"
    try:
        ascii_name = text.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeDecodeError):
        return None, None, "the name cannot be converted to an ASCII domain (IDN)"
    labels = ascii_name.split(".")
    if len(labels) < 2:
        return None, None, "a domain needs at least one dot, for example example.com"
    for label in labels:
        if not _LABEL.match(label):
            return None, None, "a label of the name is not a valid domain label"
    tld = labels[-1]
    if not _TLD.match(tld):
        return None, None, "the last part of the name is not a valid top level domain"
    if len(ascii_name) > 253:
        return None, None, "the name is longer than 253 characters"
    return ascii_name, tld, None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(parts) or None
    return None


def parse_rdap(payload: Any) -> dict[str, Any]:
    """Pull the fields we sell out of an RDAP domain object."""
    out: dict[str, Any] = {
        "registrar": None,
        "registeredAt": None,
        "expiresAt": None,
        "updatedAt": None,
        "statuses": None,
        "nameservers": None,
    }
    if not isinstance(payload, dict):
        return out

    events = payload.get("events") or []
    seen: dict[str, str] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        action = str(event.get("eventAction") or "").lower()
        date = _text(event.get("eventDate"))
        if date:
            seen[action] = date
    out["registeredAt"] = seen.get("registration") or seen.get("registered")
    out["expiresAt"] = seen.get("expiration") or seen.get("expiry")
    out["updatedAt"] = seen.get("last changed") or seen.get("last update of rdap database")

    for entity in payload.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        roles = [str(r).lower() for r in (entity.get("roles") or [])]
        if "registrar" not in roles:
            continue
        vcard = entity.get("vcardArray") or []
        if len(vcard) == 2 and isinstance(vcard[1], list):
            for item in vcard[1]:
                if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
                    out["registrar"] = _text(item[3])
                    break
        break

    statuses = payload.get("status")
    if isinstance(statuses, list):
        clean = [str(s) for s in statuses if str(s).strip()]
        out["statuses"] = clean or None

    servers = payload.get("nameservers") or []
    hosts = []
    for server in servers:
        if not isinstance(server, dict):
            continue
        name = _text(server.get("ldhName")) or _text(server.get("unicodeName"))
        if name:
            hosts.append(name.lower())
    out["nameservers"] = hosts or None
    return out


async def bootstrap(client: httpx.AsyncClient) -> dict[str, str]:
    """TLD -> RDAP base. Read once per process; a failed read is not cached."""
    global _bootstrap
    if _bootstrap is not None:
        return _bootstrap
    async with _bootstrap_lock:
        if _bootstrap is not None:
            return _bootstrap
        mapping: dict[str, str] = {}
        try:
            response = await client.get(BOOTSTRAP_URL, timeout=20.0)
            payload = response.json() if response.status_code == 200 else None
        except Exception:  # noqa: BLE001 - no bootstrap means "use the fallback table only"
            payload = None
        for group in (payload or {}).get("services") or []:
            tlds, urls = (list(group) + [[], []])[:2]
            if not urls:
                continue
            base = str(urls[0]).rstrip("/")
            for tld in tlds:
                mapping[str(tld).lower()] = base
        for tld, base in FALLBACK_BASES.items():
            mapping.setdefault(tld, base)
        _bootstrap = mapping
        return _bootstrap


async def _polite(host: str) -> None:
    lock = _host_locks.setdefault(host, asyncio.Lock())
    async with lock:
        wait = MIN_HOST_INTERVAL - (time.monotonic() - _host_last.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _host_last[host] = time.monotonic()


def _says_no_service(response: httpx.Response) -> bool:
    """A registry without RDAP for this TLD answers 404 with a notice body."""
    body = response.text or ""
    if not body.strip():
        return False
    return bool(NO_SERVICE.search(body))


def _blank() -> dict[str, Any]:
    return {
        "verdict": VERDICT_UNKNOWN,
        "reason": None,
        "httpStatus": None,
        "registrar": None,
        "registeredAt": None,
        "expiresAt": None,
        "updatedAt": None,
        "statuses": None,
        "nameservers": None,
    }


async def lookup(client: httpx.AsyncClient, domain: str, timeout_s: float,
                 mapping: dict[str, str] | None = None) -> dict[str, Any]:
    """Ask the registry of this TLD. Only its own answer is a verdict.

    A TLD outside the map, a 404 that carries a "no RDAP service" notice, a
    timeout, a 429 and an unparsable body are all UNKNOWN: we did not learn the
    answer, so the row is free and says so.
    """
    result = _blank()
    tld = domain.rsplit(".", 1)[-1]
    base = (mapping or {}).get(tld)
    if not base:
        result["reason"] = ("the registry for .%s publishes no RDAP service, "
                            "so this domain could not be checked" % tld)
        return result

    url = "%s/domain/%s" % (base, domain)
    host = url.split("/")[2]
    last_reason = ""
    for attempt in (1, 2):
        await _polite(host)
        try:
            response = await asyncio.wait_for(
                client.get(url, headers={"Accept": "application/rdap+json, application/json"}),
                timeout=max(2.0, timeout_s),
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            result["reason"] = "the registry did not answer in time"
            return result
        except httpx.HTTPError as exc:
            result["reason"] = "the registry could not be reached (%s)" % type(exc).__name__
            return result

        code = response.status_code
        result["httpStatus"] = code
        if code == 404:
            if _says_no_service(response):
                result["reason"] = ("the registry answered 404 with a notice that it publishes "
                                    "no RDAP service, so the name was not checked")
                return result
            result["verdict"] = VERDICT_AVAILABLE
            return result
        if code in (429, 503) and attempt == 1:
            delay = RETRY_DELAY_S
            try:
                delay = min(10.0, max(1.0, float(response.headers.get("Retry-After") or "")))
            except ValueError:
                pass
            last_reason = "the registry asked to slow down (HTTP %d)" % code
            await asyncio.sleep(delay)
            continue
        if code in (429, 503):
            result["reason"] = "the registry is rate limiting or unavailable (HTTP %d)" % code
            return result
        if code != 200:
            result["reason"] = "the registry answered HTTP %d" % code
            return result

        try:
            payload = response.json()
        except ValueError:
            result["reason"] = "the registry answered with something that is not RDAP JSON"
            return result
        if not isinstance(payload, dict) or not payload:
            result["reason"] = "the registry answered with an empty RDAP object"
            return result

        result["verdict"] = VERDICT_REGISTERED
        result.update(parse_rdap(payload))
        return result

    result["reason"] = last_reason or "the registry did not answer"
    return result
