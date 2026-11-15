"""DNS records for a domain: nameservers, mail, SPF and DMARC.

Nameservers, MX and TXT live in DNS, not in RDAP, and a contact or a mail
deliverability check needs them in the same row. Three resolvers are tried in
order (the container's own, then 8.8.8.8, then 1.1.1.1) because a container
network can have any one of them closed. The resolver that actually answered is
reported in the row, so "no records" is never confused with "could not ask".
"""
from __future__ import annotations

import asyncio

import dns.asyncresolver
import dns.exception
import dns.rdatatype
import dns.resolver

CHAIN = (
    ("system", None),
    ("8.8.8.8", ["8.8.8.8"]),
    ("1.1.1.1", ["1.1.1.1"]),
)
TXT_LIMIT = 20
TXT_CHARS = 300
MX_LIMIT = 10
NS_LIMIT = 12
A_LIMIT = 8


def _resolver(nameservers) -> dns.asyncresolver.Resolver:
    resolver = dns.asyncresolver.Resolver(configure=nameservers is None)
    if nameservers:
        resolver.nameservers = list(nameservers)
    resolver.timeout = 3.0
    resolver.lifetime = 5.0
    return resolver


async def _ask(resolver, name: str, rdtype: str):
    return await resolver.resolve(name, rdtype, lifetime=5.0, raise_on_no_answer=False)


def _txt_strings(answer) -> list:
    """Every TXT string, untruncated: SPF may sit past the 20th record, and a
    list cut before the search would report "no SPF" on a domain that has one."""
    out = []
    for record in answer or []:
        try:
            parts = [part.decode("utf-8", "replace") for part in record.strings]
        except AttributeError:
            parts = [str(record)]
        text = "".join(parts).strip()
        if text:
            out.append(text[:TXT_CHARS])
    return out


def _mx(answer) -> list:
    out = []
    for record in answer or []:
        out.append({"preference": int(getattr(record, "preference", 0)),
                    "exchange": str(getattr(record, "exchange", "")).rstrip(".").lower()})
        if len(out) >= MX_LIMIT:
            break
    return out


def _ns(answer) -> list:
    out = []
    for record in answer or []:
        name = str(record).rstrip(".").lower()
        if name and name not in out:
            out.append(name)
        if len(out) >= NS_LIMIT:
            break
    return out


def _addr(answer) -> list:
    out = []
    for record in answer or []:
        out.append(str(record))
        if len(out) >= A_LIMIT:
            break
    return out


def dmarc_policy(records: list) -> tuple:
    """The DMARC record and its p= policy, as written. Nothing is interpreted."""
    for text in records:
        if text.lower().startswith("v=dmarc1"):
            policy = None
            for part in text.split(";"):
                key, _, value = part.partition("=")
                if key.strip().lower() == "p":
                    policy = value.strip().lower() or None
                    break
            return text, policy
    return None, None


async def lookup(domain: str, want_mail: bool = True, want_dnssec: bool = True) -> dict:
    """Records for one domain. `exists` is None when no resolver answered."""
    result = {"nameservers": [], "mx": [], "txt": [], "a": [], "spf": None,
              "dmarc": None, "dmarcPolicy": None, "dnssecSigned": None,
              "resolver": None, "exists": None, "error": None}
    last_error = ""
    for label, servers in CHAIN:
        resolver = _resolver(servers)
        try:
            ns_answer = await _ask(resolver, domain, "NS")
        except dns.resolver.NXDOMAIN:
            result["exists"] = False
            result["resolver"] = label
            return result
        except dns.exception.DNSException as exc:
            last_error = "%s at %s" % (type(exc).__name__, label)
            continue
        except Exception as exc:  # noqa: BLE001 - an unusual resolver error is still "not asked"
            last_error = "%s at %s" % (type(exc).__name__, label)
            continue

        result["exists"] = True
        result["resolver"] = label
        result["nameservers"] = _ns(ns_answer)

        wanted = [("A", "a")]
        if want_mail:
            wanted += [("MX", "mx"), ("TXT", "txt")]
        tasks = {key: asyncio.create_task(_safe_ask(resolver, domain, rdtype))
                 for rdtype, key in wanted}
        if want_mail:
            tasks["dmarc"] = asyncio.create_task(_safe_ask(resolver, "_dmarc." + domain, "TXT"))
        if want_dnssec:
            tasks["ds"] = asyncio.create_task(_safe_ask(resolver, domain, "DS"))
        for key, task in tasks.items():
            answer = await task
            if key == "a":
                result["a"] = _addr(answer)
            elif key == "mx":
                result["mx"] = _mx(answer)
            elif key == "txt":
                all_txt = _txt_strings(answer)
                result["txt"] = all_txt[:TXT_LIMIT]
                for text in all_txt:
                    if text.lower().startswith("v=spf1"):
                        result["spf"] = text
                        break
            elif key == "dmarc":
                record, policy = dmarc_policy(_txt_strings(answer))
                result["dmarc"] = record
                result["dmarcPolicy"] = policy
            elif key == "ds":
                # A DS record at the parent is the registry half of DNSSEC.
                result["dnssecSigned"] = bool(list(answer or []))
        return result
    result["error"] = last_error or "no resolver answered"
    return result


async def _safe_ask(resolver, name: str, rdtype: str):
    try:
        return await _ask(resolver, name, rdtype)
    except Exception:  # noqa: BLE001 - a missing record type is not a failed lookup
        return None
