"""Registry data through RDAP, the open replacement for port 43 WHOIS.

The IANA bootstrap file maps a TLD to the registry that answers RDAP for it.
It is read once per process and cached in memory. Four TLDs that the bootstrap
does not list were measured by hand on 2026-11-09 and are listed below with the
endpoint that really answered 200 for a registered name.

A registry that has no RDAP service at all (for example .co, .eu, .ru) is not
guessed at: the caller gets "unsupported" and the row says so in plain words.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict

import httpx

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"

# TLDs missing from the IANA bootstrap whose registry answers RDAP anyway.
# Measured 2026-11-09 from our machine: a registered name in each TLD returned
# 200 with an ldhName, so these bases are real answers, not guesses.
FALLBACK_BASES = {
    "io": "https://rdap.identitydigital.services/rdap",
    "me": "https://rdap.identitydigital.services/rdap",
    "us": "https://rdap.nic.us",
    "de": "https://rdap.denic.de",
}

HEADERS = {
    "Accept": "application/rdap+json, application/json",
    "User-Agent": "whois-domain-lookup/0.1 (Apify Actor)",
}

STATUS_REGISTERED = "registered"
STATUS_AVAILABLE = "available"
STATUS_UNSUPPORTED = "unsupported"
STATUS_ERROR = "error"

_bootstrap: dict | None = None
_bootstrap_lock = asyncio.Lock()

# One registry is one host: asking it faster than it likes turns a run into 429s.
_host_locks: dict = defaultdict(asyncio.Lock)
_host_last: dict = defaultdict(float)
MIN_HOST_INTERVAL = 0.2
RETRY_DELAY_SECS = 2.0


async def bootstrap(client: httpx.AsyncClient) -> dict:
    """TLD -> RDAP base. Read once per process; a failed read is not cached."""
    global _bootstrap
    if _bootstrap is not None:
        return _bootstrap
    async with _bootstrap_lock:
        if _bootstrap is not None:
            return _bootstrap
        mapping: dict = {}
        try:
            response = await client.get(BOOTSTRAP_URL, timeout=20.0)
            response.raise_for_status()
            payload = response.json()
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


def base_for(tld: str, mapping: dict) -> str | None:
    return mapping.get(tld.lower())


async def _polite(host: str) -> None:
    lock = _host_locks[host]
    async with lock:
        wait = MIN_HOST_INTERVAL - (time.monotonic() - _host_last[host])
        if wait > 0:
            await asyncio.sleep(wait)
        _host_last[host] = time.monotonic()


async def lookup(client: httpx.AsyncClient, domain: str, mapping: dict) -> dict:
    """One domain against its own registry.

    Returns {status, http, url, payload, reason}. `status` is one of the four
    constants above; a network problem is `error`, never `available`.
    """
    tld = domain.rsplit(".", 1)[-1]
    base = base_for(tld, mapping)
    if not base:
        return {"status": STATUS_UNSUPPORTED, "http": None, "url": None, "payload": None,
                "reason": "the registry for .%s publishes no RDAP service" % tld}

    url = "%s/domain/%s" % (base, domain)
    host = url.split("/")[2]
    last_reason = ""
    for attempt in (1, 2):
        await _polite(host)
        try:
            response = await client.get(url, timeout=15.0, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            last_reason = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            continue
        code = response.status_code
        if code == 200:
            try:
                payload = response.json()
            except ValueError:
                last_reason = "the registry answered 200 with a body that is not JSON"
                continue
            return {"status": STATUS_REGISTERED, "http": code, "url": str(response.url),
                    "payload": payload, "reason": ""}
        if code == 404:
            # 404 is the registry saying "this name is not delegated": an answer,
            # not a failure, and the cheapest one a domain hunter can get.
            return {"status": STATUS_AVAILABLE, "http": code, "url": url,
                    "payload": None, "reason": "not registered"}
        if code in (429, 503) and attempt == 1:
            retry_after = response.headers.get("Retry-After") or ""
            delay = RETRY_DELAY_SECS
            try:
                delay = min(10.0, max(1.0, float(retry_after)))
            except ValueError:
                pass
            last_reason = "the registry answered %d and asked to slow down" % code
            await asyncio.sleep(delay)
            continue
        last_reason = "the registry answered %d" % code
    return {"status": STATUS_ERROR, "http": None, "url": url, "payload": None,
            "reason": last_reason or "the registry did not answer"}


def _vcard_text(entity: dict, field: str) -> str:
    vcard = entity.get("vcardArray") or [None, []]
    items = vcard[1] if len(vcard) > 1 else []
    for item in items:
        if isinstance(item, list) and len(item) >= 4 and item[0] == field:
            value = item[3]
            if isinstance(value, list):
                value = " ".join(str(part) for part in value if part)
            return str(value).strip()
    return ""


def _country(entity: dict) -> str | None:
    """Country from an address in the vCard, when the registry shows it at all."""
    vcard = entity.get("vcardArray") or [None, []]
    items = vcard[1] if len(vcard) > 1 else []
    for item in items:
        if isinstance(item, list) and len(item) >= 4 and item[0] == "adr" and isinstance(item[3], list):
            parts = item[3]
            if len(parts) >= 7 and isinstance(parts[6], str) and parts[6].strip():
                return parts[6].strip().upper()[:2]
    return None


def _entity(entities: list, role: str) -> dict | None:
    for entity in entities or []:
        roles = entity.get("roles") or []
        if role in roles:
            return entity
    return None


def _nameservers(payload: dict) -> list:
    out = []
    for server in payload.get("nameservers") or []:
        name = server.get("ldhName") or ""
        if name:
            out.append(str(name).rstrip(".").lower())
    return out


def parse(payload: dict) -> dict:
    """Registry facts from an RDAP domain object. Missing fields stay None."""
    events = {}
    for event in payload.get("events") or []:
        action = str(event.get("eventAction") or "").lower()
        if action:
            events[action] = event.get("eventDate")
    entities = payload.get("entities") or []
    registrar = _entity(entities, "registrar")
    registrant = _entity(entities, "registrant")
    secure = payload.get("secureDNS") or {}
    signed = secure.get("delegationSigned")
    return {
        "createdAt": events.get("registration"),
        "expiresAt": events.get("expiration"),
        "updatedAt": events.get("last changed") or events.get("last update"),
        "statuses": [str(item) for item in payload.get("status") or []],
        "nameservers": _nameservers(payload),
        "registrar": (_vcard_text(registrar, "fn") or None) if registrar else None,
        "registrantCountry": _country(registrant) if registrant else None,
        "dnssecSigned": signed if isinstance(signed, bool) else None,
    }
