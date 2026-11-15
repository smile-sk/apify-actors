"""Uptime Monitor API: entry point."""
import asyncio
import hashlib
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from apify import Actor

from src.checker import UA, host_classify, probe_apify, probe_gate, ssl_days_remaining
from src.parse import parse_rows

EVENT_NAME = "url-checked"
CACHE_VERSION = "4"

_ERROR_TYPE = {
    "timeout": "timeout",
    "ConnectTimeout": "timeout",
    "ReadTimeout": "timeout",
    "tls_error": "tls_error",
    "SSLError": "tls_error",
    "dns_lookup_failed": "dns_error",
    "redirect_to_private_blocked": "redirect_blocked",
    "too_many_redirects": "too_many_redirects",
    "ConnectError": "connection_error",
    "RemoteProtocolError": "connection_error",
    "ReadError": "connection_error",
    "ProxyError": "connection_error",
}


def _empty_row(norm: str) -> dict:
    return {"url": norm, "checkedAt": None, "up": False, "healthy": False, "healthIssues": [],
            "statusCode": None, "responseTimeMs": None, "httpStatusExpected": None,
            "responseTimeOk": None, "sslValid": None, "sslDaysToExpiry": None, "finalUrl": None,
            "redirected": False, "probes": [], "fromCache": False, "error": None, "errorType": None}


def _cache_key(norm: str, expected, max_ms) -> str:
    exp = ",".join(sorted(str(x) for x in expected)) if expected else "default"
    token = norm + "|" + exp + "|" + (str(max_ms) if max_ms is not None else "-")
    return "obs-" + CACHE_VERSION + "-" + hashlib.sha256(token.encode()).hexdigest()


def compute_verdict(status, final_url, error, norm, expected, max_ms, gate_probe, probes) -> dict:
    """Pure verdict logic, unit-testable.

    B1 guard: a 404/500 seen ONLY by the second observer must be unhealthy,
    not a silent all-clear. The health rule (expectedStatusCodes or the
    200-399 default) is applied to the gate's reported code as well.
    """
    rule = set(expected) if expected else set(range(200, 400))
    apify_ok = [p["latency_ms"] for p in probes
                if p.get("location") == "apify"
                and p.get("status_code") is not None
                and p.get("status_code") < 500
                and p.get("latency_ms") is not None]
    response_time_ms = apify_ok[-1] if apify_ok else None
    apify_up = status is not None and 200 <= status <= 399
    gate_alive = bool(gate_probe and gate_probe.get("alive") is True)

    row = {
        "up": None, "statusCode": None, "finalUrl": None, "redirected": False,
        "httpStatusExpected": None, "responseTimeOk": None,
        "healthIssues": [], "error": None, "errorType": None,
        "responseTimeMs": response_time_ms,
    }
    issues = []
    if apify_up:
        row["up"] = True
        row["statusCode"] = status
        row["finalUrl"] = final_url
        row["redirected"] = final_url is not None and final_url != norm
        row["httpStatusExpected"] = status in rule
        slow = (max_ms is not None and response_time_ms is not None
                and response_time_ms > max_ms)
        row["responseTimeOk"] = not slow
        if status not in rule:
            issues.append("unexpected_status")
        if slow:
            issues.append("slow_response")
    elif gate_alive:
        row["up"] = True
        gcode = status if status is not None else gate_probe.get("http_code")
        if gcode is not None:
            row["statusCode"] = gcode
            row["httpStatusExpected"] = gcode in rule
            if gcode not in rule:
                issues.append("unexpected_status")
        else:
            issues.append("gate_unverified")
    else:
        row["up"] = False
        row["error"] = error
        row["errorType"] = _ERROR_TYPE.get(error, error)
        if row["errorType"]:
            issues.append(row["errorType"])
        if status is not None:
            row["statusCode"] = status
            row["httpStatusExpected"] = status in rule
            if status not in rule:
                issues.append("unexpected_status")
    row["healthIssues"] = issues
    row["healthy"] = (bool(row["up"]) or (status is not None and error is None)) and not issues
    return row


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        urls = actor_input.get("urls") or []
        expected = actor_input.get("expectedStatusCodes") or []
        max_ms = actor_input.get("maxResponseTimeMs")
        timeout = int(actor_input.get("timeoutSec") or 15)
        max_items = int(actor_input.get("maxItems") or 200)
        concurrency = int(actor_input.get("concurrency") or 8)
        raw_cache = actor_input.get("cacheMinutes")
        cache_minutes = 5 if raw_cache is None else int(raw_cache)
        time_budget = int(actor_input.get("timeBudgetSeconds") or 3300)

        gate_url = os.environ.get("UPTIME_GATE_URL")
        gate_auth = os.environ.get("UPTIME_GATE_AUTH")
        Actor.log.info("gate configured: %s", bool(gate_url and gate_auth))

        entries = parse_rows(urls)
        ok_entries = [e for e in entries if e["kind"] == "ok"]
        selected = ok_entries[:max_items]
        omitted_by_max_items = max(0, len(ok_entries) - len(selected))

        store = None
        if cache_minutes > 0:
            try:
                store = await Actor.open_key_value_store(name="uptime-observations")
            except Exception as exc:
                Actor.log.warning("named store unavailable: %s", exc)
                store = None

        sem = asyncio.Semaphore(max(1, concurrency))
        charge_lock = asyncio.Lock()

        stats = {"healthy": 0, "unhealthy": 0, "invalid": 0, "private": 0,
                 "cached": 0, "delivered": 0, "refused": 0, "omitted_by_budget": 0,
                 "delivery_failures": 0}
        unhealthy_urls = []
        stopped_by_charge = False
        # Rows actually pushed to the dataset. Unlike "started tasks", this
        # count cannot lie when the time budget cancels half of the work:
        # complete=true with zero rows was the false all-clear of 0.1.8.
        emitted = 0

        async def _push_row(row: dict, norm: str) -> bool:
            """Push with one retry. A swallowed delivery failure is the same
            class as a false all-clear: the buyer paid and got nothing, so the
            run must report it loudly."""
            last = None
            for attempt in (1, 2):
                try:
                    await Actor.push_data(row)
                    return True
                except Exception as exc:
                    last = exc
                    Actor.log.warning("push_data failed (attempt %d of 2) for %s: %s",
                                      attempt, norm, exc)
            Actor.log.error("delivery failed for %s after 2 attempts: %s", norm, last)
            return False

        async def record(row: dict, norm: str, health: bool = True) -> bool:
            """B2 guard: EVERY pushed row flows through one door, so rows that
            bypass the charge (cache, private, invalid) still feed the health
            counters and all_healthy cannot lie."""
            nonlocal emitted
            if not await _push_row(row, norm):
                return False
            emitted += 1
            if not health:
                return True
            if row.get("healthy") is True:
                stats["healthy"] += 1
            elif row.get("healthy") is False:
                stats["unhealthy"] += 1
                unhealthy_urls.append(norm)
            return True

        async def _deliver_inner(row: dict, norm: str) -> bool:
            """Charge, then deliver. Returns True when the row was delivered."""
            nonlocal stopped_by_charge
            try:
                async with charge_lock:
                    charge = await Actor.charge(event_name=EVENT_NAME)
            except Exception:
                row["error"] = (row["error"] or "") + " (charge uncertain)"
                if await record(row, norm):
                    stats["delivered"] += 1
                else:
                    stats["delivery_failures"] += 1
                return True
            if getattr(charge, "charged_count", 0) > 0:
                if await record(row, norm):
                    stats["delivered"] += 1
                else:
                    stats["delivery_failures"] += 1
                if getattr(charge, "event_charge_limit_reached", False):
                    stopped_by_charge = True
                return True
            stats["refused"] += 1
            if getattr(charge, "event_charge_limit_reached", False):
                stopped_by_charge = True
            return False

        async def deliver(row: dict, norm: str) -> bool:
            """Charge+push as one inseparable pair: a task cancelled between
            the two must not take the money and drop the row (0.1.8 blocker)."""
            inner = asyncio.ensure_future(_deliver_inner(row, norm))
            try:
                return await asyncio.shield(inner)
            except asyncio.CancelledError:
                try:
                    await inner
                except Exception:
                    pass
                raise

        async def check_one(entry: dict) -> None:
            async with sem:
                if stopped_by_charge:
                    stats["refused"] += 1
                    return
                norm = entry["norm"]

                if store is not None:
                    key = _cache_key(norm, expected, max_ms)
                    try:
                        cached = await store.get_value(key)
                    except Exception as exc:
                        Actor.log.warning("cache read failed for %s: %s", norm, exc)
                        cached = None
                    if cached and isinstance(cached, dict):
                        try:
                            age_s = (datetime.now(timezone.utc)
                                     - datetime.fromisoformat(cached["at"])).total_seconds()
                        except Exception:
                            age_s = 10 ** 9
                        if age_s <= cache_minutes * 60:
                            row = dict(cached["row"])
                            row["fromCache"] = True
                            row["url"] = norm
                            if await record(row, norm):
                                stats["cached"] += 1
                            else:
                                stats["delivery_failures"] += 1
                            Actor.log.info("cache hit for %s", norm)
                            return
                        try:
                            await store.delete(key)
                        except Exception:
                            pass

                kind = host_classify(norm)

                if kind == "private":
                    row = _empty_row(norm)
                    row["checkedAt"] = datetime.now(timezone.utc).isoformat()
                    row["error"] = "non-public target"
                    row["errorType"] = "private_target"
                    if await record(row, norm, health=False):
                        stats["private"] += 1
                    else:
                        stats["delivery_failures"] += 1
                    return

                if kind == "unresolvable":
                    row = _empty_row(norm)
                    row["checkedAt"] = datetime.now(timezone.utc).isoformat()
                    row["probes"] = [{"location": "apify", "attempt": 1, "status_code": None,
                                      "latency_ms": None, "error": "dns_lookup_failed"}]
                    row["error"] = "dns_lookup_failed"
                    row["errorType"] = "dns_error"
                    row["healthIssues"] = ["dns_error"]
                    row["up"] = False
                    row["healthy"] = False
                    delivered = await deliver(row, norm)
                    if delivered and store is not None:
                        await store.set_value(_cache_key(norm, expected, max_ms),
                                              {"at": row["checkedAt"], "row": row})
                    return

                status = final_url = error = None
                gate_probe = None
                probes = []
                try:
                    async with httpx.AsyncClient(follow_redirects=False, verify=True,
                                                 headers={"User-Agent": UA}) as client:
                        attempts, status, final_url, error = await probe_apify(client, norm, timeout)
                        probes = list(attempts)
                        if error is not None or status is None or status >= 500:
                            a2, s2, f2, e2 = await probe_apify(client, norm, timeout)
                            offset = max((p.get("attempt") or 0) for p in probes) if probes else 0
                            for p in a2:
                                p["attempt"] += offset
                            probes.extend(a2)
                            if s2 is not None and (s2 < 500 or e2 is None):
                                status, final_url, error = s2, f2, e2
                        gate_probe = await probe_gate(client, norm, gate_url, gate_auth, 15)
                        if gate_probe:
                            probes.append(gate_probe)
                except Exception as exc:
                    probes = [{"location": "apify", "attempt": 1, "status_code": None,
                               "latency_ms": None, "error": type(exc).__name__}]
                    status, final_url, error = None, None, type(exc).__name__

                row = _empty_row(norm)
                row["checkedAt"] = datetime.now(timezone.utc).isoformat()
                row["probes"] = probes
                row.update(compute_verdict(status, final_url, error, norm, expected,
                                           max_ms, gate_probe, probes))

                if norm.startswith("https://"):
                    phost = urlparse(norm).hostname
                    pport = urlparse(norm).port or 443
                    row["sslDaysToExpiry"] = await asyncio.to_thread(
                        ssl_days_remaining, phost, pport, 8.0)
                    answered = any(p.get("status_code") is not None
                                   for p in probes if p.get("location") == "apify")
                    if not answered and gate_probe and gate_probe.get("alive") is True \
                            and gate_probe.get("http_code") is not None:
                        answered = True
                    if row["errorType"] == "tls_error":
                        row["sslValid"] = False
                    elif answered:
                        row["sslValid"] = True

                delivered = await deliver(row, norm)
                if delivered and store is not None:
                    try:
                        await store.set_value(_cache_key(norm, expected, max_ms),
                                              {"at": row["checkedAt"], "row": row})
                        Actor.log.info("cache write for %s", norm)
                    except Exception as exc:
                        Actor.log.warning("cache write failed for %s: %s", norm, exc)

        tasks = [asyncio.create_task(check_one(e)) for e in selected]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=time_budget)
        except asyncio.TimeoutError:
            Actor.log.warning("time budget exhausted; %d entries not delivered",
                              len(selected) - emitted)

        stats["omitted_by_budget"] = max(
            0, len(selected) - emitted - stats["refused"] - stats["delivery_failures"])

        for entry in entries:
            if entry["kind"] == "invalid":
                row = _empty_row(entry["raw"])
                row["checkedAt"] = datetime.now(timezone.utc).isoformat()
                row["error"] = "invalid URL"
                row["errorType"] = "invalid_url"
                if await record(row, entry["raw"], health=False):
                    stats["invalid"] += 1
                else:
                    stats["delivery_failures"] += 1

        summary = {
            "requested_unique_urls": len(ok_entries) + stats["invalid"],
            "selected_urls": len(selected),
            "checked_urls": (stats["delivered"] + stats["cached"]
                             + stats["private"] + stats["invalid"]),
            "omitted_by_max_items": omitted_by_max_items,
            "omitted_by_time_budget": stats["omitted_by_budget"],
            "invalid_count": stats["invalid"],
            "private_count": stats["private"],
            "cached_count": stats["cached"],
            "refused_by_charge_limit": stats["refused"],
            "delivery_failures": stats["delivery_failures"],
            "healthy_count": stats["healthy"],
            "unhealthy_count": stats["unhealthy"],
            "unhealthy_urls": unhealthy_urls,
            "finished": True,
            "run_error": False,
            "complete": (omitted_by_max_items == 0 and stats["refused"] == 0
                         and stats["omitted_by_budget"] == 0
                         and stats["delivery_failures"] == 0),
            # B2: rows that bypassed the charge (cache hits with a down
            # verdict, private targets, invalid URLs) must not allow an
            # all-clear either.
            "all_healthy": (stats["unhealthy"] == 0 and stats["refused"] == 0
                            and omitted_by_max_items == 0
                            and stats["omitted_by_budget"] == 0
                            and stats["invalid"] == 0 and stats["private"] == 0
                            and stats["delivery_failures"] == 0),
        }
        run_id = os.environ.get("ACTOR_RUN_ID") or "local"
        try:
            await Actor.set_value("RUN_SUMMARY", summary)
        except Exception as exc:
            Actor.log.warning("could not write RUN_SUMMARY to run store: %s", exc)
        if store is not None:
            try:
                await store.set_value("RUN_SUMMARY-" + run_id, summary)
            except Exception as exc:
                Actor.log.warning("could not write RUN_SUMMARY to named store: %s", exc)
        await Actor.set_status_message(
            f"delivered {stats['delivered']}, cached {stats['cached']}, "
            f"healthy {stats['healthy']}, unhealthy {stats['unhealthy']}")


if __name__ == "__main__":
    asyncio.run(main())
