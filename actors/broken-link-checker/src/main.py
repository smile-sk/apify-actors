"""Apify Actor: broken link checker.

Input  : a list of URLs and/or a sitemap URL, plus health rules.
Output : one row per URL - status code, broken flag, error type, redirect
         chain, response time, retries used.
Charge : `url-checked`, only for rows that were actually checked and
         delivered. Invalid and private targets are free, cached rows are
         free, and a redirect chain that ends on a private address is free.

The niche leader's own 2-star review says links that still work are scanned
as broken. That is what this Actor fixes at the code level:

  * every check retries on network errors, timeouts, DNS failures, 429 and
    5xx before calling a link broken - a transient failure never bills the
    buyer for a wrong verdict;
  * custom headers and User-Agent are first-class input, because the leader's
    FAQ literally says it does not support custom headers, so its users pay
    for 403s reported as broken links;
  * a sitemap URL is accepted directly, instead of telling the buyer to buy
    a second Actor to extract URLs.

Money rules, copied from the proven charge flow of our Workday actor:

  * the charge is issued BEFORE the row is pushed, and the row is pushed only
    when that charge actually counted. The SDK's ChargeResult says it plainly:
    event_charge_limit_reached means "this event was charged and no more will
    fit", so breaking before push_data sold a row the buyer never received;
  * if both charge attempts raise, the row is still delivered and the run
    stops with charge-uncertain - the cost of one free row per run is cheaper
    than billing a row the buyer never receives;
  * two push failures stop the run with a delivery-error and a
    DELIVERY_FAILURES record, so no further charges land in a dataset that is
    refusing writes. Free rows (invalid, private, cached, private redirect
    targets) are delivered through the same protected push.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from xml.etree import ElementTree

import httpx
from apify import Actor

from src import checker, parse

CHARGE_EVENT = "url-checked"
CACHE_STORE = "broken-link-checker-cache"
SUMMARY_STORE = "broken-link-checker-run-summaries"
DEFAULT_CACHE_MINUTES = 60
MIN_ERROR_CACHE_SECONDS = 300
CACHE_VERSION = "2"


def _list_of_strings(raw) -> list[str]:
    """Accept an array of strings or one multi-line string. A string is split
    on newlines and semicolons only - a comma stays inside a URL, so
    `https://e.com/a?x=1,2` is never torn into a bogus second target."""
    if isinstance(raw, str):
        return [p.strip() for p in re.split(r"[\r\n;]+", raw) if p.strip()]
    return [str(x).strip() for x in (raw or []) if str(x).strip()]


def _int_setting(value, default: int) -> int:
    """Settings that allow 0 (retries, maxRedirects): None or an empty string
    means 'not set' and gets the default, an explicit 0 must survive."""
    return default if value in (None, "") else int(value)


def _expected_rule(raw):
    rule = [int(x) for x in (raw or [])]
    return sorted(set(rule)) if rule else list(range(200, 400))


def _cache_key(url: str, expected, headers: dict, ua: str, slow_ms: int,
               max_redirects: int, retries: int, timeout: int) -> str:
    """sha1 hex - colons are not allowed in Apify cache keys (they silently
    break the cache), and a hex digest never contains one. Every probe
    setting is part of the key: a cached verdict must be the verdict the
    buyer would have got with the current settings."""
    blob = json.dumps([url, expected, headers, ua, slow_ms, max_redirects,
                       retries, timeout, CACHE_VERSION],
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _cached_row(entry_row: dict, now_iso: str) -> dict:
    """A reused verdict keeps its original checkedAt (the moment of the
    actual probe) and carries cachedAt separately."""
    row = dict(entry_row)
    row["fromCache"] = True
    row["cachedAt"] = now_iso
    return row


def _all_healthy(broken, invalid, private, delivery_failures, omitted,
                 dropped_by_cap, refused) -> bool:
    """allHealthy means every listed URL was checked and healthy. Omitted,
    dropped, refused or failed rows make it false - a release gate must
    never pass on a half-checked list."""
    return (broken == 0 and invalid == 0 and private == 0
            and delivery_failures == 0 and omitted == 0
            and dropped_by_cap == 0 and refused == 0)


def _coverage_complete(delivered, refused, delivery_failures, selected_len,
                       invalid_count, private_count) -> bool:
    return (delivered + refused + delivery_failures
            == selected_len + invalid_count + private_count)


def _omitted(selected_len, delivered, refused, delivery_failures) -> int:
    return max(0, selected_len - delivered - refused - delivery_failures)


def _monetization_off(charge) -> bool:
    """True when this run is not pay-per-event at all (local dev, free Actor)."""
    return (not getattr(charge, "chargeable_within_limit", None)
            and not getattr(charge, "event_charge_limit_reached", False))


def parse_sitemap(xml_text: str, cap: int) -> list[str]:
    """Extract <loc> entries from a sitemap; a sitemap index is NOT followed
    here (the caller fetches children). Returns at most `cap` URLs."""
    if cap <= 0:
        return []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    out = []
    for el in root.iter():
        if el.tag.endswith("loc") and el.text:
            u = el.text.strip()
            if u:
                out.append(u)
                if len(out) >= cap:
                    return out
    return out


async def fetch_sitemap(client, sitemap_url: str, cap: int, timeout: float) -> list[str]:
    """Fetch one sitemap (index or flat) and collect links up to cap."""
    try:
        resp = await client.get(sitemap_url, timeout=timeout, follow_redirects=True)
        if resp.status_code != 200:
            Actor.log.warning(f"sitemap {sitemap_url}: HTTP {resp.status_code}, no links collected")
            return []
        urls = parse_sitemap(resp.text, cap)
        Actor.log.info(f"sitemap {sitemap_url}: {len(urls)} links")
        return urls
    except Exception as exc:
        Actor.log.warning(f"sitemap {sitemap_url} failed: {exc}")
        return []


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        raw_urls = _list_of_strings(inp.get("urls"))
        sitemap_url = str(inp.get("sitemapUrl") or "").strip()

        expected = _expected_rule(inp.get("expectedStatusCodes"))
        headers = {str(k): str(v) for k, v in (inp.get("headers") or {}).items()}
        ua = str(inp.get("userAgent") or "broken-link-checker/0.1 (Apify Actor)")
        timeout = int(inp.get("timeoutSec") or 15)
        retries = _int_setting(inp.get("retries"), 2)
        max_redirects = _int_setting(inp.get("maxRedirects"), 5)
        slow_ms = int(inp.get("slowThresholdMs") or 2000)
        max_items = int(inp.get("maxItems") or 10000)
        concurrency = int(inp.get("concurrency") or 8)
        raw_cache = inp.get("cacheMinutes")
        cache_minutes = DEFAULT_CACHE_MINUTES if raw_cache is None else int(raw_cache)
        cache_seconds = max(0, cache_minutes) * 60
        time_budget = float(int(inp.get("timeBudgetSeconds") or 3300))

        if not raw_urls and not sitemap_url:
            await Actor.fail(status_message="urls is empty and sitemapUrl is empty - pass at least one URL")
            return

        # the sitemap load counts against the time budget, so the clock
        # starts before it
        started = time.time()

        request_headers = {"User-Agent": ua}
        request_headers.update(headers)
        limits = httpx.Limits(max_connections=max(concurrency, 8) * 2,
                              max_keepalive_connections=min(concurrency, 8))
        async with httpx.AsyncClient(headers=request_headers, follow_redirects=False,
                                     limits=limits, verify=True) as client:
            sitemap_links = []
            if sitemap_url:
                first = await fetch_sitemap(client, sitemap_url, max_items, timeout)
                # a sitemap index lists child sitemaps: follow up to 10 of them
                if first and all(u.rstrip("/").endswith(".xml") for u in first[:3]):
                    children = first[:10]
                    Actor.log.info(f"sitemap index with {len(children)} child sitemaps")
                    collected = set()
                    for child in children:
                        remaining = max(0, max_items - len(collected))
                        if remaining == 0:
                            break
                        links = await fetch_sitemap(client, child, remaining, timeout)
                        for u in links:
                            collected.add(u)
                    sitemap_links = list(collected)[:max_items]
                else:
                    sitemap_links = first[:max_items]

            parsed_list = parse.parse_rows(raw_urls)
            parsed_sitemap = parse.parse_rows(sitemap_links)
            for r in parsed_list:
                r["source"] = "list"
            for r in parsed_sitemap:
                r["source"] = "sitemap"

            dupes = 0
            list_norm = {r["norm"] for r in parsed_list if r["kind"] == "ok"}
            rows_sitemap = []
            for r in parsed_sitemap:
                if r["kind"] == "ok":
                    if r["norm"] in list_norm:
                        dupes += 1
                        continue
                    list_norm.add(r["norm"])
                rows_sitemap.append(r)
            rows = parsed_list + rows_sitemap

            cache = None
            if cache_seconds:
                try:
                    cache = await Actor.open_key_value_store(name=CACHE_STORE)
                except Exception as exc:
                    Actor.log.warning(f"Cache store unavailable, running without cache: {exc}")

            summary_store = None
            try:
                summary_store = await Actor.open_key_value_store(name=SUMMARY_STORE)
            except Exception as exc:
                Actor.log.warning(f"Summary store unavailable: {exc}")

            now = started
            invalid_count = private_count = 0
            broken_count = healthy_count = cached_count = checked_count = 0
            delivered = 0
            refused = delivery_failures = 0
            omitted = 0
            delivery_failure_rows = []
            stopped = False
            stop_reason = ""

            sem = asyncio.Semaphore(max(1, min(concurrency, 32)))
            charge_lock = asyncio.Lock()

            async def push_protected(row: dict, *, charged: bool) -> bool:
                """Push a row with two attempts. Free rows get the same
                protection as charged ones: a dataset that refuses writes
                must not be hit by further charges, so a double failure
                stops the run."""
                nonlocal delivered, delivery_failures, stopped, stop_reason
                for attempt in (1, 2):
                    try:
                        await Actor.push_data(row)
                        delivered += 1
                        return True
                    except Exception as exc:
                        if attempt == 2:
                            delivery_failures += 1
                            delivery_failure_rows.append({
                                "url": row.get("url"), "error": str(exc)[:300]})
                            stopped = True
                            stop_reason = "delivery-error"
                            Actor.log.error(f"Row {row.get('url')} was refused by the "
                                            f"dataset twice: {exc}")
                return False

            selected: list[dict] = []
            for r in rows:
                if stopped:
                    break
                if r["kind"] == "invalid":
                    invalid_count += 1
                    await push_protected({
                        "url": r["raw"], "statusCode": None, "broken": True,
                        "errorType": "invalid_input", "error": "not a usable HTTP(S) URL",
                        "finalUrl": None, "redirectCount": 0, "responseTimeMs": None,
                        "slow": False, "retriesUsed": 0,
                        "expectedStatusCodes": expected, "source": "list",
                        "fromCache": False,
                        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }, charged=False)
                    continue
                cls = await checker.host_classify(r["norm"])
                if cls == "private":
                    private_count += 1
                    await push_protected({
                        "url": r["norm"], "statusCode": None, "broken": True,
                        "errorType": "private_target",
                        "error": "host resolves to a private address and was not contacted",
                        "finalUrl": None, "redirectCount": 0, "responseTimeMs": None,
                        "slow": False, "retriesUsed": 0,
                        "expectedStatusCodes": expected, "source": r.get("source", "list"),
                        "fromCache": False,
                        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }, charged=False)
                    continue
                selected.append(r)

            dropped_by_cap = max(0, len(selected) - max_items)
            if dropped_by_cap:
                Actor.log.warning(f"maxItems={max_items} - {dropped_by_cap} URL(s) NOT checked this run; "
                                  f"raise maxItems to check them")
            selected = selected[:max_items]
            if dupes:
                Actor.log.info(f"{dupes} URL(s) present both in urls and sitemap collapsed to one check")

            async def check_and_deliver(r: dict):
                nonlocal checked_count, broken_count, healthy_count, cached_count
                nonlocal refused, stopped, stop_reason
                async with sem:
                    if stopped:
                        return
                    if time_budget and time.time() - started >= time_budget:
                        return
                    url = r["norm"]
                    source = r.get("source", "list")
                    key = _cache_key(url, expected, headers, ua, slow_ms,
                                     max_redirects, retries, timeout)
                    cached_row = None
                    if cache is not None:
                        try:
                            entry = await cache.get_value(key)
                        except Exception as exc:
                            entry = None
                            Actor.log.warning(f"Cache read failed for {url}: {exc}")
                        if isinstance(entry, dict) and isinstance(entry.get("row"), dict):
                            ttl = cache_seconds
                            if now - float(entry.get("fetchedAt") or 0) <= ttl:
                                cached_row = entry["row"]
                    if cached_row is not None:
                        row = _cached_row(cached_row, datetime.now(timezone.utc).isoformat(timespec="seconds"))
                        cached_count += 1
                        if row.get("broken"):
                            broken_count += 1
                        else:
                            healthy_count += 1
                        await push_protected(row, charged=False)
                        return
                    row = await checker.check_url(
                        client, url, timeout=timeout, max_redirects=max_redirects,
                        retries=retries, expected=expected, slow_threshold_ms=slow_ms,
                        source=source)
                    row["checkedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

                    # a redirect chain that ends on a private address is a
                    # private target: reported, delivered, and free
                    free_private = row.get("errorType") == "private_target"
                    idem = key
                    charge = None
                    charged = 0
                    charge_raised = False
                    if not free_private:
                        async with charge_lock:
                            if stopped:
                                return
                            for attempt in (1, 2):
                                try:
                                    charge = await Actor.charge(event_name=CHARGE_EVENT,
                                                                idempotency_key=idem)
                                    charged = int(getattr(charge, "charged_count", 0) or 0)
                                    break
                                except Exception as exc:
                                    charge_raised = True
                                    if attempt == 2:
                                        Actor.log.error(f"Charge failed twice for {url}: {exc} - "
                                                        f"the row IS delivered and the run stops")
                                        charge = None
                            uncertain = False
                            if charge is None:
                                uncertain = True
                            elif charged <= 0 and not _monetization_off(charge):
                                if charge_raised:
                                    uncertain = True
                                    Actor.log.warning(f"Charge raised once and the idempotent retry "
                                                      f"answered 0 for {url} - the row IS delivered and "
                                                      f"the run stops here")
                                else:
                                    refused += 1
                                    stop_reason = "charge-limit"
                                    stopped = True
                                    Actor.log.warning("User spending limit reached - no event was charged, "
                                                      "so the row is NOT delivered and NOT billed")
                                    return
                    pushed = await push_protected(row, charged=not free_private)
                    if not pushed:
                        return
                    checked_count += 1
                    if row.get("broken"):
                        broken_count += 1
                    else:
                        healthy_count += 1
                    if cache is not None and row.get("errorType") not in ("invalid_input", "private_target"):
                        try:
                            await cache.set_value(key, {"fetchedAt": now, "row": row})
                        except Exception as exc:
                            Actor.log.warning(f"Cache write failed for {url}: {exc}")
                    if free_private:
                        return
                    if uncertain:
                        stop_reason = "charge-uncertain"
                        stopped = True
                        return
                    if getattr(charge, "event_charge_limit_reached", False):
                        stop_reason = "charge-limit"
                        stopped = True
                        Actor.log.warning("User spending limit reached - the last row was charged "
                                          "and delivered, stopping here")

            await asyncio.gather(*[check_and_deliver(r) for r in selected],
                                 return_exceptions=False)

            elapsed = time.time() - started
            omitted = _omitted(len(selected), delivered, refused, delivery_failures)
            summary = {
                "urlsAsked": len(raw_urls) + len(sitemap_links),
                "urlsInvalid": invalid_count,
                "urlsPrivate": private_count,
                "urlsCollapsedDuplicates": dupes,
                "urlsSkippedByMaxItems": dropped_by_cap,
                "urlsChecked": checked_count,
                "urlsFromCache": cached_count,
                "brokenCount": broken_count,
                "healthyCount": healthy_count,
                "allHealthy": _all_healthy(broken_count, invalid_count, private_count,
                                           delivery_failures, omitted, dropped_by_cap, refused),
                "coverageComplete": _coverage_complete(delivered, refused, delivery_failures,
                                                       len(selected), invalid_count, private_count),
                "deliveryFailures": delivery_failures,
                "rowsRefusedByChargeLimit": refused,
                "omittedByTimeBudget": omitted,
                "stopReason": stop_reason or None,
                "cacheMinutes": cache_minutes,
                "cacheHits": cached_count,
                "seconds": round(elapsed, 1),
                "note": ("Only checked, non-private rows are charged. Invalid, private, "
                         "private-redirect and cached rows are free."),
            }
            await Actor.set_value("RUN_SUMMARY", summary)
            if summary_store is not None:
                try:
                    run_id = os.environ.get("ACTOR_RUN_ID", "local")
                    await summary_store.set_value(f"RUN_SUMMARY-{run_id}", summary)
                except Exception as exc:
                    Actor.log.warning(f"Named-store summary write failed: {exc}")
            if delivery_failures:
                await Actor.set_value("DELIVERY_FAILURES", {
                    "count": delivery_failures,
                    "rows": delivery_failure_rows,
                    "note": ("The rows below were refused by the dataset twice. Charged rows "
                             "keep this record as the trail for a refund claim."),
                })
            Actor.log.info(f"Done: {checked_count} checked, {broken_count} broken, "
                           f"{cached_count} cached, {invalid_count} invalid, {private_count} private "
                           f"in {elapsed:.0f}s (stop={stop_reason or 'none'})")


if __name__ == "__main__":
    os.environ.setdefault("ACTOR_LOG_LEVEL", "INFO")
    asyncio.run(main())
