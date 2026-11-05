"""Apify Actor: contacts from any website, with the page each value came from.

Input  : websites (domains or URLs).
Output : one row per unique website - emails, phones, socials, address, pages scanned.
Charge : `site-with-contacts` - only for sites that actually returned an email
         or a phone number, and only for a fresh crawl (a cached row is free).
         A site with no contacts costs the buyer nothing, but it is still
         reported, with the reason, never silently dropped.

Cache  : a repeated run inside `cacheMinutes` is served from a NAMED key-value
         store, so the same website is not crawled twice for the same answer.
         The store is named, therefore it lives in the account of whoever runs
         the Actor - their storage, their data, never mixed with a customer's.

Parsing lives in src/extract.py and is testable without the Apify SDK.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone

import httpx
from apify import Actor
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from src import extract

CHARGE_EVENT = "site-with-contacts"
STORE_NAME = "contact-cache"
MAX_BYTES = 3_000_000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
RETRY_STATUS = {429, 500, 502, 503, 504}


def _retry_after(value: str | None) -> float:
    """Seconds to wait before a retry. No header means wait, not hammer."""
    if not value:
        return 1.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 1.0


REDIRECT_STATUS = {301, 302, 303, 307, 308}
MAX_HOPS = 5


async def fetch(client: httpx.AsyncClient, url: str, attempts: int = 2) -> tuple[int | None, str, str, str]:
    """Return (status, html, problem, final_url). Never raises. 4xx/5xx is a problem.

    Redirects are followed by hand, one hop at a time, and every hop is checked
    against loopback/LAN before the request is made: httpx with
    follow_redirects=True connected to a private address before the guard could
    look at the final URL (QA 0.1.13, SSRF through a DNS name).
    """
    last_problem = ""
    headers = {"User-Agent": UA, "Accept-Language": "en;q=0.9,*;q=0.5"}
    for attempt in range(attempts):
        current = url
        try:
            for _hop in range(MAX_HOPS + 1):
                response = await client.get(current, follow_redirects=False, headers=headers)
                status = response.status_code
                if status in REDIRECT_STATUS:
                    location = (response.headers.get("location") or "").strip()
                    if not location:
                        return status, "", "HTTP redirect without Location", current
                    target = str(response.url.join(location))
                    if not target.lower().startswith(("http://", "https://")):
                        return None, "", "redirect to a non-http address", current
                    target_host = extract.host_of(target)
                    if extract.is_private_host(target_host) or await _private(target_host):
                        return None, "", "redirect to a private or loopback address", target
                    current = target
                    continue
                last_problem = f"HTTP {status}"
                if status in RETRY_STATUS and attempt + 1 < attempts:
                    await asyncio.sleep(min(_retry_after(response.headers.get("retry-after")), 5.0))
                    break                                   # retry from the original URL
                if status >= 400:
                    return status, "", f"HTTP {status}", str(response.url)
                ctype = (response.headers.get("content-type") or "").lower()
                if "html" not in ctype and "xml" not in ctype:
                    return status, "", f"not-html:{ctype[:40] or 'unknown'}", str(response.url)
                return status, response.text[:MAX_BYTES], "", str(response.url)
            else:
                return None, "", f"more than {MAX_HOPS} redirects", current
        except Exception as exc:                                  # noqa: BLE001
            last_problem = f"{type(exc).__name__}: {str(exc)[:140]}"
            if attempt + 1 < attempts:
                await asyncio.sleep(1.0)
    return None, "", last_problem, url


def merge(into: dict, items, key: str) -> None:
    for item in items:
        into.setdefault(item[key], item)


def _dep_versions() -> str:
    """Which dependency versions this build actually runs: pinning them needs a real run."""
    out = []
    for name in ("apify", "crawlee", "pydantic", "httpx", "selectolax"):
        try:
            out.append(f"{name}={_pkg_version(name)}")
        except PackageNotFoundError:
            out.append(f"{name}=?")
    return " ".join(out)


async def _private(host: str) -> bool:
    """A name that points at loopback/LAN is not a buyer's website (localtest.me -> 127.0.0.1)."""
    return await asyncio.to_thread(extract.resolves_private, host)


async def crawl_site(client: httpx.AsyncClient, site: str, max_pages: int,
                     want_socials: bool, want_address: bool) -> dict:
    started = time.time()
    url = extract.to_url(site)
    row: dict = {
        "website": site,
        "url": url,
        "domain": "",
        "company": "",
        "status": "UNREACHABLE",
        "emails": [],
        "phones": [],
        "socials": {},
        "address": None,
        "foreignEmails": [],
        "foreignEmailsTotal": 0,
        "pagesScanned": [],
        "pagesFailed": [],
        "reason": "",
        "fromCache": False,
        "scannedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not url:
        row["status"] = "INVALID_URL"
        row["reason"] = ("not a http(s) website" if (site or "").strip() else "empty input")
        row["elapsedMs"] = 0
        return row
    host = extract.host_of(url)
    if extract.is_private_host(host) or await _private(host):
        row["status"] = "INVALID_URL"
        row["reason"] = "private or loopback address - not scanned, free of charge"
        row["elapsedMs"] = 0
        return row

    status, html, problem, final_url = await fetch(client, url)
    https_problem = problem
    if not html and not problem.startswith("HTTP 4") and url.startswith("https://"):
        status2, html2, problem2, final2 = await fetch(client, "http://" + url[8:])
        if html2:
            status, html, problem, final_url = status2, html2, problem2, final2
        else:
            problem = https_problem          # report the https failure, not the fallback's

    landed = final_url or url
    landed_host = extract.host_of(landed)
    if landed_host and landed_host != extract.host_of(url) and await _private(landed_host):
        row["url"] = landed
        row["status"] = "INVALID_URL"
        row["reason"] = "redirect to a private or loopback address - not read, free of charge"
        row["elapsedMs"] = int((time.time() - started) * 1000)
        return row
    url = landed
    row["url"] = url
    row["domain"] = extract.registrable(extract.host_of(url))
    if not html:
        row["status"] = "UNREACHABLE"
        row["reason"] = problem or f"HTTP {status}"
        row["elapsedMs"] = int((time.time() - started) * 1000)
        return row

    tree = extract.parse(html)
    if extract.is_blocked(html, tree):
        row["status"] = "BLOCKED"
        row["reason"] = f"anti-bot challenge page (HTTP {status}) - free of charge"
        row["elapsedMs"] = int((time.time() - started) * 1000)
        return row
    org = extract.jsonld_org(tree)
    row["company"] = extract.company_name(tree, org, row["domain"])
    text = extract.phone_text(tree)

    emails: dict[str, dict] = {}
    phones: dict[str, dict] = {}
    socials: dict[str, str] = {}
    merge(emails, extract.emails_from(tree, html, url, row["domain"]), "value")
    ld_email = extract.email_from_jsonld(org.get("email", ""), url, row["domain"])
    if ld_email:
        merge(emails, [ld_email], "value")
    ld_phone = extract.phone_from_jsonld(org.get("telephone", ""), url)
    if ld_phone:
        merge(phones, [ld_phone], "value")
    merge(phones, extract.phones_from(tree, text, url), "value")
    if want_socials:
        socials.update(extract.socials_from(html))
    row["pagesScanned"].append(url)

    links = extract.contact_links(tree, url, row["domain"], max(max_pages - 1, 0))
    for link in links[: max(max_pages - 1, 0)]:
        st, sub_html, sub_problem, _final = await fetch(client, link)
        if not sub_html:
            row["pagesFailed"].append({"url": link, "reason": sub_problem or f"HTTP {st}"})
            continue
        sub_tree = extract.parse(sub_html)
        if extract.is_blocked(sub_html, sub_tree):
            row["pagesFailed"].append({"url": link,
                                       "reason": f"anti-bot challenge page (HTTP {st})"})
            continue
        row["pagesScanned"].append(link)
        merge(emails, extract.emails_from(sub_tree, sub_html, link, row["domain"]), "value")
        merge(phones, extract.phones_from(sub_tree, extract.phone_text(sub_tree), link), "value")
        if want_socials:
            for name, link_value in extract.socials_from(sub_html).items():
                socials.setdefault(name, link_value)

    all_emails = list(emails.values())
    foreign_all = [e for e in all_emails if e.get("foreign")]
    row["emails"] = [e for e in all_emails if not e.get("foreign")]
    row["foreignEmails"] = foreign_all[:10]
    row["foreignEmailsTotal"] = len(foreign_all)     # the list above is capped, the count is not
    row["phones"] = list(phones.values())
    row["socials"] = socials
    row["address"] = org.get("address") if want_address else None
    if not row["company"] and org.get("name"):
        row["company"] = org["name"]

    has_contacts = bool(row["emails"] or row["phones"] or row["foreignEmails"])
    row["status"] = "OK" if has_contacts else "NO_CONTACTS"
    if not has_contacts:
        checked = len(row["pagesScanned"])
        row["reason"] = (f"no email or phone on {checked} page(s); "
                         f"socials found: {len(socials)}")
    row["elapsedMs"] = int((time.time() - started) * 1000)
    return row


CACHE_VERSION = "6"          # bump when the row shape changes: stale rows must not be served


def cache_key(site: str, max_pages: int, socials: bool, address: bool) -> str:
    raw = f"{CACHE_VERSION}|{extract.site_key(site)}|{max_pages}|{int(socials)}|{int(address)}"
    return hashlib.sha1(raw.encode()).hexdigest()          # noqa: S324 - cache key only


async def main() -> None:
    async with Actor:
        Actor.log.info("deps: %s", _dep_versions())
        actor_input = await Actor.get_input() or {}
        raw_sites = extract.parse_sites(actor_input.get("websites"))
        if not raw_sites:
            await Actor.fail("No websites given: put at least one domain in 'websites'.")
            return

        sites, duplicates = extract.unique_sites(raw_sites)
        max_pages = max(1, min(int(actor_input.get("maxPagesPerSite") or 5), 20))
        want_socials = bool(actor_input.get("includeSocials", True))
        want_address = bool(actor_input.get("includeAddress", True))
        cache_raw = actor_input.get("cacheMinutes")
        cache_minutes = 1440 if cache_raw is None else max(0, int(cache_raw))
        concurrency = max(1, min(int(actor_input.get("concurrency") or 8), 20))

        store = None
        if cache_minutes:
            try:
                store = await Actor.open_key_value_store(name=STORE_NAME)
            except Exception:                                    # noqa: BLE001
                store = None

        charged_ok = True
        charged = 0
        capped = 0
        charge_errors: list[str] = []
        semaphore = asyncio.Semaphore(concurrency)
        all_rows: list[dict] = []

        def build_summary(partial: bool) -> dict:
            return {
                "sites": len(all_rows),
                "duplicatesRemoved": duplicates,
                "withContacts": sum(1 for r in all_rows if r["status"] == "OK"),
                "withoutContacts": sum(1 for r in all_rows if r["status"] == "NO_CONTACTS"),
                "unreachable": sum(1 for r in all_rows if r["status"] == "UNREACHABLE"),
                "blocked": sum(1 for r in all_rows if r["status"] == "BLOCKED"),
                "invalidUrl": sum(1 for r in all_rows if r["status"] == "INVALID_URL"),
                "failedRows": sum(1 for r in all_rows if r["status"] == "ERROR"),
                "servedFromCache": sum(1 for r in all_rows if r["fromCache"]),
                "chargeErrors": charge_errors,
                "billingModel": ("site-with-contacts - charged once per unique site that returned an "
                                 "email or a phone, fresh crawl only; cached, empty, blocked and "
                                 "unreachable sites are free"),
                "billableSites": charged,
                "cappedNotCharged": capped,
                "partial": partial,
            }

        async def write_summary(partial: bool) -> None:
            """Saved as the run goes: if the buyer's charge cap stops the run, money is not lost."""
            try:
                await Actor.set_value("SUMMARY", build_summary(partial))
            except Exception:                                    # noqa: BLE001
                pass

        async with httpx.AsyncClient(timeout=20.0,
                                     limits=httpx.Limits(max_connections=concurrency * 2)) as client:

            async def handle(site: str) -> None:
                nonlocal charged_ok, charged, capped
                async with semaphore:
                    try:
                        key = cache_key(site, max_pages, want_socials, want_address)
                        row = None
                        if store is not None:
                            cached = await store.get_value(key)
                            if isinstance(cached, dict) and time.time() - cached.get("ts", 0) < cache_minutes * 60:
                                row = cached.get("row")
                                if isinstance(row, dict):
                                    row = dict(row)
                                    row["fromCache"] = True

                        if row is None:
                            row = await crawl_site(client, site, max_pages, want_socials, want_address)
                            if store is not None and row["status"] in ("OK", "NO_CONTACTS"):
                                try:
                                    await store.set_value(key, {"ts": time.time(), "row": row})
                                except Exception:                    # noqa: BLE001
                                    pass

                        all_rows.append(row)
                        await Actor.push_data(row)
                        if len(all_rows) % 25 == 0:
                            await write_summary(True)

                        if row["status"] == "OK" and not row["fromCache"]:
                            if not charged_ok:
                                capped += 1                      # reported, not billed: cap hit
                            else:
                                try:
                                    charge = await Actor.charge(event_name=CHARGE_EVENT)
                                    billed = int(getattr(charge, "charged_count", 0) or 0)
                                    charged += billed                 # what the platform counted
                                    if billed == 0:
                                        capped += 1
                                    if getattr(charge, "event_charge_limit_reached", False):
                                        charged_ok = False            # no more events fit the budget
                                except Exception as exc:             # noqa: BLE001
                                    charged_ok = False
                                    charge_errors.append(f"{site}: {type(exc).__name__}: {str(exc)[:120]}")
                                    Actor.log.error("charge failed for %s: %s", site, exc)
                    except Exception as exc:                         # noqa: BLE001
                        # One bad line must never empty the whole run: it becomes a row.
                        row = {"website": site, "url": "", "domain": extract.site_key(site),
                               "company": "", "status": "ERROR", "emails": [], "phones": [],
                               "socials": {}, "address": None, "foreignEmails": [],
                               "foreignEmailsTotal": 0, "elapsedMs": 0,
                               "pagesScanned": [],
                               "pagesFailed": [], "fromCache": False,
                               "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                               "scannedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                        all_rows.append(row)
                        await Actor.push_data(row)
                        if len(all_rows) % 25 == 0:
                            await write_summary(True)
                        Actor.log.error("site failed: %s: %s", site, exc)

            await asyncio.gather(*(handle(site) for site in sites), return_exceptions=True)

        await write_summary(False)
        Actor.log.info("done: %s unique sites (%s duplicates dropped), %s with contacts, %s billed",
                       len(all_rows), duplicates,
                       sum(1 for r in all_rows if r["status"] == "OK"), charged)


if __name__ == "__main__":
    asyncio.run(main())
