"""Apify actor: SSL Certificate Checker API - Expiry, Chain & Distrust.

Checks TLS certificates for a list of hosts: validity today, validity
after the 15 Apr 2027 Mozilla root removals, anchor root, expiry, SANs.
Only verdict rows are charged; unreachable hosts, cache hits and invalid
input are delivered free.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

from apify import Actor

from src.parse import is_private, parse_host
from src.tlscheck import check_host

CACHE_VERSION = "2"
MAX_ROWS = 5000


def _cache_key(host, port):
    return "h_%s_%d_v%s" % (host.replace(".", "_"), port, CACHE_VERSION)


async def main():
    async with Actor:
        await Actor.set_status_message("starting")
        inp = await Actor.get_input() or {}
        hosts_raw = inp.get("hosts") or []
        if isinstance(hosts_raw, str):
            hosts_raw = [hosts_raw]
        cache_minutes = int(inp.get("cacheMinutes", 1440) or 0)
        concurrency = int(inp.get("maxConcurrency", 8) or 8)
        timeout = int(inp.get("timeoutSeconds", 10) or 10)
        time_budget = inp.get("timeBudgetSeconds") or 0
        concurrency = max(1, min(32, concurrency))
        timeout = max(3, min(30, timeout))

        input_total = len(hosts_raw)

        # parse and deduplicate over the FULL input, then truncate jobs:
        # counters must describe what the buyer sent, not the slice
        seen = set()
        jobs_full = []
        invalid = []
        for raw in hosts_raw:
            host, port = parse_host(raw)
            if host is None:
                invalid.append(raw)
                continue
            key = (host, port)
            if key in seen:
                continue
            seen.add(key)
            jobs_full.append((host, port))

        invalid_rows = len(invalid)
        deduplicated = input_total - invalid_rows - len(jobs_full)
        truncated = len(jobs_full) > MAX_ROWS
        jobs = jobs_full[:MAX_ROWS]
        skipped_truncated = len(jobs_full) - len(jobs)

        store = await Actor.open_key_value_store(
            name="ssl-cert-check-store") if cache_minutes > 0 else None

        billable = 0
        delivered = 0
        unreachable = 0
        cache_hits = 0
        charged_rows = 0
        stopped_by_limit = False
        stopped_by_time = False
        charge_uncertain = False
        delivery_error = False
        delivery_failures = []
        start_ts = datetime.now(timezone.utc)

        for raw in invalid:
            row = {
                "host": str(raw)[:200], "port": None, "verdict": "unreachable",
                "reason": "invalid host input", "billable": False,
                "fromCache": False, "checkedAt": datetime.now(timezone.utc).isoformat(),
            }
            try:
                await Actor.push_data(row)
                delivered += 1
            except Exception:
                delivery_error = True
                delivery_failures.append(
                    {"host": str(raw)[:200], "port": None, "charged": False,
                     "error": "dataset rejected the invalid-input row"})

        sem = asyncio.Semaphore(concurrency)

        async def handle(job):
            nonlocal billable, delivered, unreachable, cache_hits, charged_rows, \
                stopped_by_limit, stopped_by_time, charge_uncertain, delivery_error
            host, port = job
            row = None
            if store is not None:
                try:
                    cached = await store.get_value(_cache_key(host, port))
                    if cached:
                        data = json.loads(cached)
                        try:
                            exp = datetime.fromisoformat(data.get("expiresAt"))
                        except Exception:
                            exp = None
                        if exp is not None and datetime.now(timezone.utc) < exp:
                            row = data
                            row["fromCache"] = True
                            row["billable"] = False
                            row["cachedAt"] = datetime.now(timezone.utc).isoformat()
                            Actor.log.info("cache HIT %s:%d" % (host, port))
                except Exception as e:
                    Actor.log.warning("cache read failed %s: %s" % (host, type(e).__name__))
                    row = None
            if row is None:
                if is_private(host):
                    row = {
                        "host": host, "port": port, "verdict": "unreachable",
                        "reason": "private address not allowed", "billable": False,
                        "fromCache": False,
                        "checkedAt": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    async with sem:
                        row = await asyncio.to_thread(check_host, host, port=port, timeout=timeout)
                    row["checkedAt"] = datetime.now(timezone.utc).isoformat()

            is_free = (row.get("verdict") == "unreachable") or row.get("fromCache")
            if is_free:
                row["billable"] = False
                pushed = False
                for attempt in (1, 2):
                    try:
                        await Actor.push_data(row)
                        delivered += 1
                        pushed = True
                        break
                    except Exception:
                        continue
                if not pushed:
                    delivery_error = True
                    delivery_failures.append(
                        {"host": host, "port": port, "charged": False,
                         "error": "dataset rejected the row twice"})
                if row.get("fromCache"):
                    cache_hits += 1
                else:
                    unreachable += 1
                return

            # billable row: one charge attempt, no retry (a retry can
            # charge twice); an exception means we cannot tell and the
            # row is delivered with charge-uncertain, then the run stops
            charged = False
            limit_reached = False
            try:
                charge = await Actor.charge(event_name="host-checked", count=1)
                if getattr(charge, "charged_count", 0) > 0:
                    charged = True
                    charged_rows += 1
                if getattr(charge, "event_charge_limit_reached", False):
                    limit_reached = True
            except Exception:
                charged = None

            if charged is None:
                row["billable"] = False
                charge_uncertain = True
            else:
                row["billable"] = charged

            # cache only a verdict that was actually charged, and stamp
            # its expiry: an unpaid verdict must not live forever
            if charged and store is not None:
                try:
                    row["expiresAt"] = (
                        datetime.now(timezone.utc)
                        + timedelta(minutes=cache_minutes)).isoformat()
                    await store.set_value(_cache_key(host, port), json.dumps(row))
                    Actor.log.info("cache write %s:%d" % (host, port))
                except Exception as e:
                    Actor.log.warning("cache write failed %s: %s" % (host, type(e).__name__))

            push_ok = False
            for attempt in (1, 2):
                try:
                    await Actor.push_data(row)
                    push_ok = True
                    break
                except Exception:
                    continue
            if not push_ok:
                delivery_error = True
                delivery_failures.append(
                    {"host": host, "port": port, "charged": charged,
                     "error": "dataset rejected the row twice"})
            else:
                delivered += 1
            if charged:
                billable += 1
            if limit_reached or charge_uncertain:
                stopped_by_limit = True

        # run jobs with concurrency
        pending = list(jobs)
        idx = 0
        while idx < len(pending) and not stopped_by_limit and not delivery_error and not charge_uncertain:
            batch = pending[idx:idx + concurrency]
            idx += len(batch)
            await asyncio.gather(*(handle(j) for j in batch))
            if time_budget and (datetime.now(timezone.utc) - start_ts).total_seconds() > time_budget:
                stopped_by_time = True
                break

        if delivery_failures:
            try:
                kvs = await Actor.open_key_value_store(name="ssl-cert-check-store")
                await kvs.set_value("DELIVERY_FAILURES", json.dumps(
                    {"count": len(delivery_failures), "rows": delivery_failures}))
            except Exception:
                pass

        not_read = skipped_truncated + (len(pending) - idx)
        summary = {
            "inputRows": input_total,
            "inputTruncated": truncated,
            "invalidRows": invalid_rows,
            "deduplicated": deduplicated,
            "delivered": delivered,
            "billable": billable,
            "chargedRows": charged_rows,
            "unreachable": unreachable,
            "cacheHits": cache_hits,
            "notRead": not_read,
            "stoppedByChargeLimit": stopped_by_limit,
            "stoppedByTimeBudget": stopped_by_time,
            "chargeUncertain": charge_uncertain,
            "deliveryError": delivery_error,
            "timeBudgetSeconds": time_budget,
        }
        await Actor.push_data({"SUMMARY": summary})
        await Actor.set_status_message("done")

        reason = None
        if stopped_by_limit:
            reason = "charge-limit" if not charge_uncertain else "charge-uncertain"
        elif delivery_error:
            reason = "delivery-error"
        elif stopped_by_time:
            reason = "time-budget"
        status = "done"
        if reason:
            status = "stopped: %s" % reason
        await Actor.set_status_message(status)


if __name__ == "__main__":
    asyncio.run(main())
