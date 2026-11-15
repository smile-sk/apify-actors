"""Domain availability: one row per domain, the verdict comes from the registry.

Money rule of this actor: we charge only for a domain that got a verdict from
RDAP (available or taken). A malformed line, a duplicate, a domain over the
limit and a registry that did not answer are all free and carry a reason.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from apify import Actor

from .rdap import VERDICT_UNKNOWN, bootstrap, lookup, parse_domain

EVENT = "domain-checked"

ST_OK = "OK"
ST_INVALID = "INVALID"
ST_DUPLICATE = "DUPLICATE"
ST_UNKNOWN = "NOT_CHECKED_UNKNOWN"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_CAP = "NOT_CHECKED_CAP"
ST_TIME = "NOT_CHECKED_TIME"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_ERROR = "NOT_CHECKED_ERROR"

TIME_MARGIN_S = 20.0
# Stops that already say why no verdict came back: those runs are not failures.
MONEY_STOPS = frozenset({"spending-cap", "charge-limit", "charge-error"})
EXPLAINED_STOPS = MONEY_STOPS | {"deadline", "billing-price-unknown"}
READ_TRIES = 3
READ_PAUSE_S = 0.4
BATCH = 50


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_left() -> float | None:
    raw = os.environ.get("ACTOR_TIMEOUT_AT")
    if not raw:
        return None
    try:
        deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (deadline - datetime.now(timezone.utc)).total_seconds()


def price_state(charging: Any, event: str) -> tuple[bool | None, str | None]:
    """True - rows are sold, False - selling is off, None - we cannot tell."""
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001
        return None, f"the price list could not be read ({type(exc).__name__})"
    if not bool(getattr(info, "is_pay_per_event", False)):
        return False, None
    prices = getattr(info, "per_event_prices", None) or {}
    if event not in prices:
        return None, f"the event {event} is not in the price list"
    return True, None


async def item_count(dataset: Any) -> int | None:
    for attempt in range(READ_TRIES):
        try:
            meta = await dataset.get_metadata()
        except Exception:  # noqa: BLE001
            meta = None
        count = getattr(meta, "item_count", None)
        if isinstance(count, int):
            return count
        if attempt < READ_TRIES - 1:
            await asyncio.sleep(READ_PAUSE_S)
    return None


def status_row(status: str, reason: str, index: int, raw: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "inputIndex": index,
        "input": raw,
        "domain": None,
        "tld": None,
        "verdict": None,
        "available": None,
        "billable": False,
    }


def paid_row(index: int, raw: str, domain: str, tld: str, found: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": ST_OK,
        "reason": None,
        "inputIndex": index,
        "input": raw,
        "domain": domain,
        "tld": tld,
        "verdict": found["verdict"],
        "available": found["verdict"] == "available",
        "registrar": found.get("registrar"),
        "registeredAt": found.get("registeredAt"),
        "expiresAt": found.get("expiresAt"),
        "updatedAt": found.get("updatedAt"),
        "statuses": found.get("statuses"),
        "nameservers": found.get("nameservers"),
        "httpStatus": found.get("httpStatus"),
        "billable": True,
    }


class Run:
    def __init__(self, dataset: Any, charging: Any, can_charge: bool | None, start_reason: str | None) -> None:
        self.dataset = dataset
        self.charging = charging
        self.can_charge = can_charge
        self.billing_off = can_charge is None
        self.requested = 0
        self.attempted = 0
        self.charged = 0
        self.rows_written = 0
        self.charge_errors = 0
        self.write_errors = 0
        self.delivered_unverified = 0
        self.charge_uncertain = 0
        self.stop_reason = start_reason or ""
        self.notes: list[str] = []
        self.counters: dict[str, int] = {st: 0 for st in
                                         (ST_OK, ST_INVALID, ST_DUPLICATE, ST_UNKNOWN, ST_LIMIT,
                                          ST_CAP, ST_TIME, ST_BILLING, ST_ERROR)}
        self.started = now_iso()

    async def free(self, status: str, reason: str, index: int, raw: str) -> None:
        self.counters[status] += 1
        try:
            await self.dataset.push_data(status_row(status, reason, index, raw))
        except Exception:  # noqa: BLE001 - a refused free row must not kill the summary
            self.write_errors += 1
            return
        self.rows_written += 1

    def stopped(self) -> bool:
        return bool(self.stop_reason)

    async def flush(self, rows: list[dict[str, Any]]) -> None:
        """Write a batch of rows that earned a paid verdict."""
        if not rows:
            return
        if self.billing_off:
            for row in rows:
                self.counters[ST_BILLING] += 1
                await self.dataset.push_data(status_row(
                    ST_BILLING, self.notes[-1] if self.notes else "the price list could not be read",
                    row["inputIndex"], row["input"]))
                self.rows_written += 1
            self.stop_reason = self.stop_reason or "billing-price-unknown"
            return
        limit = len(rows)
        if self.can_charge:
            try:
                limit = self.charging.compute_push_data_limit(len(rows), EVENT, is_default_dataset=True)
            except Exception as exc:  # noqa: BLE001
                self.notes.append(f"the spending limit could not be computed ({type(exc).__name__})")
                for row in rows:
                    self.counters[ST_BILLING] += 1
                    await self.dataset.push_data(status_row(
                        ST_BILLING, "the spending limit could not be computed, so nothing was sold",
                        row["inputIndex"], row["input"]))
                    self.rows_written += 1
                self.stop_reason = self.stop_reason or "billing-price-unknown"
                return
        pay, rest = rows[:max(0, limit)], rows[max(0, limit):]
        for row in rest:
            await self.free(ST_CAP, "the run reached its spending limit before this domain",
                            row["inputIndex"], row["input"])
        if rest:
            self.stop_reason = self.stop_reason or "spending-cap"
        if not pay:
            return
        if self.can_charge is False:
            # Pay-per-event is off: the rows are delivered and nothing is sold.
            for row in pay:
                self.counters[ST_OK] += 1
                await self.dataset.push_data(row)
                self.rows_written += 1
            return

        before = await item_count(self.dataset)
        try:
            result = await Actor.push_data(pay, charged_event_name=EVENT)
        except Exception as exc:  # noqa: BLE001
            self.charge_errors += 1
            after = await item_count(self.dataset)
            grew = None if (before is None or after is None) else after - before
            if grew is None:
                self.delivered_unverified += len(pay)
                self.charge_uncertain += len(pay)
                self.notes.append(
                    "the charge raised and the dataset counter could not be read: delivery and the charge "
                    f"are both unknown, check the dataset before re-running ({type(exc).__name__})")
            elif grew < len(pay):
                self.delivered_unverified += len(pay) - max(0, grew)
                self.notes.append(
                    f"the charge raised and the dataset counter grew by {max(0, grew)} of {len(pay)}: "
                    "delivery of the rest is unknown and nothing is claimed as sold")
                self.rows_written += max(0, grew)
            else:
                self.delivered_unverified += len(pay)
                self.charge_uncertain += len(pay)
                self.rows_written += len(pay)
                self.notes.append("the rows stay in the dataset and the charge is unverified: nothing is claimed as sold")
            self.stop_reason = self.stop_reason or "charge-error"
            return

        got = getattr(result, "charged_count", None)
        if isinstance(got, int):
            self.charged += got
            for _ in range(got):
                self.counters[ST_OK] += 1
        else:
            self.counters[ST_OK] += len(pay)
            self.charge_uncertain += len(pay)
            self.notes.append("the charge result did not report how many rows were charged")
        self.rows_written += len(pay)
        if getattr(result, "event_charge_limit_reached", False):
            self.stop_reason = self.stop_reason or "charge-limit"

    def summary(self, run_id: str) -> dict[str, Any]:
        by_status = {k: v for k, v in self.counters.items() if v}
        accounted = sum(self.counters.values())
        return {
            "runId": run_id or None,
            "startedAt": self.started,
            "finishedAt": now_iso(),
            "requested": self.requested,
            "domainsAttempted": self.attempted,
            "domainsChecked": self.counters[ST_OK],
            "chargedEvents": self.charged,
            "rowsWritten": self.rows_written,
            "byStatus": by_status,
            "chargeErrors": self.charge_errors,
            "writeErrors": self.write_errors,
            "deliveredUnverified": self.delivered_unverified,
            "chargeUncertain": self.charge_uncertain,
            "stopReason": self.stop_reason,
            "counterMismatch": self.requested != accounted,
            "billingActive": self.can_charge,
            "pricingEvent": EVENT,
            "datasetNote": self.notes[-1] if self.notes else None,
        }


async def main() -> None:
    async with Actor:
        env = Actor.get_env()
        run_id = env.get("run_id") or ""

        config = await Actor.get_input() or {}
        lines = config.get("domains")
        if isinstance(lines, str):
            lines = lines.replace(",", "\n").split("\n")
        if not isinstance(lines, list):
            lines = []
        worked = [line for line in lines if isinstance(line, str)]
        if not any(line.strip() for line in worked):
            await Actor.fail(status_message="No domains were sent. Put at least one domain name in the "
                                            "'domains' field, for example example.com.", exit_code=1)
            return

        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()

        def int_setting(name: str, default: int, low: int, high: int) -> int:
            try:
                value = int(config.get(name) if config.get(name) is not None else default)
            except (TypeError, ValueError):
                value = default
            return max(low, min(value, high))

        max_domains = int_setting("maxDomains", 500, 1, 5000)
        concurrency = int_setting("concurrency", 5, 1, 12)
        try:
            request_timeout = float(config.get("requestTimeoutSecs") or 10)
        except (TypeError, ValueError):
            request_timeout = 10.0
        request_timeout = max(2.0, min(request_timeout, 30.0))

        can_charge, price_reason = price_state(charging, EVENT)
        run = Run(dataset, charging, can_charge, None)
        if can_charge is None:
            run.stop_reason = "billing-price-unknown"
        if price_reason:
            run.notes.append(price_reason)
        run.requested = len(worked)

        tasks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, line in enumerate(worked):
            domain, tld, error = parse_domain(line)
            if error:
                tasks.append({"kind": "invalid", "index": index, "raw": line, "reason": error})
            elif domain in seen:
                tasks.append({"kind": "duplicate", "index": index, "raw": line})
            else:
                seen.add(domain)
                tasks.append({"kind": "live", "index": index, "raw": line, "domain": domain, "tld": tld})

        for task in tasks:
            if task["kind"] == "invalid":
                await run.free(ST_INVALID, task["reason"], task["index"], task["raw"])
            elif task["kind"] == "duplicate":
                await run.free(ST_DUPLICATE, "the same domain was already checked in this run",
                               task["index"], task["raw"])

        live = [task for task in tasks if task["kind"] == "live"]
        semaphore = asyncio.Semaphore(concurrency)

        async def one(client: httpx.AsyncClient, task: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                left = seconds_left()
                budget = request_timeout if left is None else min(request_timeout, max(2.0, left - TIME_MARGIN_S))
                found = await lookup(client, task["domain"], budget, mapping)
                return {"task": task, "found": found}

        async def close_tail(start: int, status: str, reason: str) -> None:
            for task in live[start:]:
                await run.free(status, reason, task["index"], task["raw"])

        batch: list[dict[str, Any]] = []
        done = 0
        mapping: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(request_timeout), follow_redirects=True) as client:
            if done < len(live):
                mapping = await bootstrap(client)
                if not mapping:
                    run.notes.append("the IANA RDAP bootstrap could not be read: only the "
                                     "built-in registry table was used")
            while done < len(live) and not run.stopped():
                left = seconds_left()
                if left is not None and left <= TIME_MARGIN_S:
                    run.stop_reason = "deadline"
                    break
                chunk = live[done:done + concurrency]
                done += len(chunk)
                try:
                    results = await asyncio.gather(*(one(client, task) for task in chunk))
                except Exception as exc:  # noqa: BLE001
                    for task in chunk:
                        run.attempted += 1
                        await run.free(ST_ERROR, f"an unexpected error ({type(exc).__name__})",
                                       task["index"], task["raw"])
                    run.stop_reason = "unexpected-error"
                    break
                for item in results:
                    task, found = item["task"], item["found"]
                    run.attempted += 1
                    if found["verdict"] == VERDICT_UNKNOWN:
                        await run.free(ST_UNKNOWN, found.get("reason") or "the registry did not answer",
                                       task["index"], task["raw"])
                        continue
                    if run.charged + len(batch) >= max_domains:
                        await run.free(ST_LIMIT, "the run reached maxDomains before this domain",
                                       task["index"], task["raw"])
                        continue
                    batch.append(paid_row(task["index"], task["raw"], task["domain"], task["tld"], found))
                if len(batch) >= BATCH:
                    await run.flush(batch)
                    batch = []
            if batch:
                # A verdict we already paid for is delivered even when the run is
                # stopping: dropping it would lose money the buyer cannot see.
                await run.flush(batch)
                batch = []

        if done < len(live):
            # A stored verdict is never dropped: every line that got no answer is
            # closed here with the status its stop reason deserves.
            if run.stop_reason == "deadline":
                await close_tail(done, ST_TIME, "the run ran out of time before this domain")
            elif run.stop_reason in MONEY_STOPS:
                await close_tail(done, ST_CAP,
                                 f"the run stopped on money before this domain ({run.stop_reason})")
            elif run.stop_reason == "billing-price-unknown":
                await close_tail(done, ST_BILLING, price_reason or "the price list could not be read")
            else:
                await close_tail(done, ST_ERROR, f"the run stopped before this domain ({run.stop_reason})")

        result = run.summary(run_id)
        await Actor.set_value("RUN_SUMMARY", result)
        if run_id:
            await Actor.set_value(f"RUN_SUMMARY-{run_id}", result)

        checked = run.counters[ST_OK]
        not_checked = max(0, run.requested - checked)
        message = (f"{run.requested} line(s): {checked} domain(s) checked and charged, "
                   f"{not_checked} not checked")
        if run.stop_reason:
            message += f", stopped: {run.stop_reason}"
        await Actor.set_status_message(message)
        if checked == 0 and run.requested and run.stop_reason not in EXPLAINED_STOPS:
            await Actor.fail(status_message=message + ". No domain got a verdict from the registry.", exit_code=1)


if __name__ == "__main__":
    asyncio.run(main())
