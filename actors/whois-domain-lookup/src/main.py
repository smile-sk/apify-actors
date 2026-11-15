"""Apify Actor: WHOIS (RDAP) and DNS facts for a list of domains.

Input  : `domains` - links, emails or bare names, one per line; optional
         `includeDns`, `maxDomains`.
Output : one row per domain: registration and expiry dates, registrar, EPP
         statuses, nameservers, DNSSEC, plus DNS records (NS, MX, TXT, SPF,
         DMARC) and the resolver that answered.
Charge : `domain-checked` - only for a delivered registry answer: a registered
         domain, or a registry saying the name is free. A TLD with no RDAP
         service, a refused request or a bad line costs nothing, and the row
         says which of those happened.

One row is one domain. The same domain sent twice is answered once: the second
line comes back with NOT_CHECKED_DUPLICATE and its own index, so it is never
charged twice.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version

import httpx
from apify import Actor

from src import dnsrec, domain as dom, rdap

CHARGE_EVENT = "domain-checked"
SUMMARY_STORE = "whois-domain-lookup-summary"

STATUS_REGISTERED = "REGISTERED"
STATUS_AVAILABLE = "AVAILABLE"
STATUS_INVALID = "NOT_CHECKED_INVALID"
STATUS_DUPLICATE = "NOT_CHECKED_DUPLICATE"
STATUS_NO_RDAP = "NOT_CHECKED_NO_RDAP"
STATUS_SOURCE = "NOT_CHECKED_SOURCE"
STATUS_CAP = "NOT_CHECKED_CAP"
STATUS_BILLING = "NOT_CHECKED_BILLING"
STATUS_LIMIT = "NOT_CHECKED_LIMIT"
STATUS_TIME = "NOT_CHECKED_TIME"
STATUS_WRITE = "NOT_CHECKED_ERROR"

CHARGE_ERROR_LIMIT = 100
WAVE = 5
PLATFORM_MARGIN_SECS = 20
MAX_DOMAINS_DEFAULT = 50


def _dep_versions() -> str:
    out = []
    for name in ("apify", "httpx", "dnspython"):
        try:
            out.append("%s=%s" % (name, _pkg_version(name)))
        except PackageNotFoundError:
            out.append("%s=?" % name)
    return " ".join(out)


def _rows_from(value) -> list:
    """Accepts a list, a single string with newlines, or a messy mix."""
    out: list = []
    if value is None:
        return out
    if isinstance(value, str):
        value = value.replace(",", "\n").replace(";", "\n").splitlines()
    if isinstance(value, (list, tuple)):
        for item in value:
            if item is None:
                continue
            text = item if isinstance(item, str) else str(item)
            for line in text.replace(",", "\n").replace(";", "\n").splitlines():
                line = line.strip()
                if line:
                    out.append(line)
    return out


def _platform_budget_secs() -> float | None:
    """Seconds left before the platform kills this run, or None when it does not say."""
    raw = os.environ.get("ACTOR_TIMEOUT_AT")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    left = moment.timestamp() - time.time()
    return left if left > 0 else 0.0


def _row(index: int, source: str) -> dict:
    return {
        "index": index,
        "input": source,
        "domain": None,
        "tld": None,
        "status": STATUS_SOURCE,
        "registered": None,
        "registrySource": None,
        "rdapUrl": None,
        "httpStatus": None,
        "registrar": None,
        "createdAt": None,
        "expiresAt": None,
        "updatedAt": None,
        "ageDays": None,
        "daysUntilExpiry": None,
        "statuses": [],
        "nameservers": [],
        "registrantCountry": None,
        "dnssecSigned": None,
        "dns": None,
        "dnsResolver": None,
        "billable": False,
        "reason": "",
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _answer_message(by_status: dict, requested: int) -> str:
    parts = ", ".join("%s: %d" % (status, count) for status, count in sorted(by_status.items()))
    return ("No domain could be read from %d line(s). %s. A TLD without an RDAP service, a "
            "registry that refused the request and a line that is not a domain all come back "
            "as their own status, and none of them was charged." % (requested, parts))


async def main() -> None:
    async with Actor:
        Actor.log.info("deps: %s", _dep_versions())
        actor_input = await Actor.get_input() or {}
        lines = _rows_from(actor_input.get("domains"))
        if not lines:
            await Actor.fail(status_message=(
                "No domains: put at least one domain, link or email into 'domains'. "
                "Nothing was charged."))
            return

        include_dns = bool(actor_input.get("includeDns", True))
        requested = len(lines)
        max_raw = actor_input.get("maxDomains")
        max_domains = (MAX_DOMAINS_DEFAULT if max_raw is None
                       else int(max_raw))
        if max_domains <= 0:
            max_domains = requested

        budget = _platform_budget_secs()
        deadline = None if budget is None else time.monotonic() + max(5.0, budget - PLATFORM_MARGIN_SECS)

        summary = {
            "requested": requested,
            "uniqueDomains": 0,
            "duplicates": 0,
            "processed": 0,
            "delivered": 0,
            "rowsDelivered": 0,
            "notChecked": 0,
            "chargedEvents": 0,
            "billingActive": True,
            "stopReason": None,
            "byStatus": {},
            "cutByMaxDomains": 0,
            "maxDomains": max_domains,
            "deliveredUncharged": 0,
            "datasetWriteErrors": 0,
            "datasetWriteUnverified": 0,
            "datasetItemCount": None,
            "datasetItemCountApi": None,
            "datasetItemCountLag": False,
            "chargeErrors": [],
            "rdapBootstrapTlds": 0,
            "deadlineCapped": False,
        }
        charge_errors: list = summary["chargeErrors"]
        consecutive_failures = 0
        write_failures = 0
        billing_active = True
        run_stopped = None
        stop_reason = None

        try:
            pricing = Actor.get_charging_manager().get_pricing_info()
        except Exception:  # noqa: BLE001 - a local run carries no pricing
            pricing = None
        if pricing is not None:
            prices = getattr(pricing, "per_event_prices", None) or {}
            if not getattr(pricing, "is_pay_per_event", False):
                billing_active = False
            elif CHARGE_EVENT not in prices:
                billing_active = False
                Actor.log.error("pricing has no %r event: this run delivers rows without charging",
                                CHARGE_EVENT)

        def count_status(row: dict) -> None:
            summary["byStatus"][row["status"]] = summary["byStatus"].get(row["status"], 0) + 1

        def count_row(row: dict) -> None:
            count_status(row)
            summary["rowsDelivered"] += 1

        async def item_count():
            try:
                dataset = await Actor.open_dataset()
                meta = None
                for getter in ("get_metadata", "get_info"):
                    method = getattr(dataset, getter, None)
                    if method is not None:
                        meta = await method()
                        break
                if meta is None:
                    return None
                if isinstance(meta, dict):
                    for key in ("item_count", "itemCount"):
                        if meta.get(key) is not None:
                            return int(meta[key])
                    return None
                for attr in ("item_count", "itemCount"):
                    value = getattr(meta, attr, None)
                    if value is not None:
                        return int(value)
                return None
            except Exception:  # noqa: BLE001 - a local run may have no dataset handle
                return None

        async def push(row: dict) -> bool:
            """Write a row and count it only when the dataset really grew."""
            nonlocal run_stopped, stop_reason, write_failures
            before = await item_count()
            error = None
            for attempt in (1, 2):
                try:
                    await Actor.push_data(row)
                    error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    error = exc
                    Actor.log.error("dataset write failed for %s (attempt %d): %s",
                                    row.get("domain") or row.get("input"), attempt, exc)
            if error is None:
                count_row(row)
                write_failures = 0
                return True
            after = await item_count()
            if before is not None and after is not None:
                landed = after > before
            else:
                Actor.log.warning("could not verify the dataset row count for %s",
                                  row.get("domain") or row.get("input"))
                summary["datasetWriteUnverified"] += 1
                landed = True
            if landed:
                count_row(row)
                return True
            row["status"] = STATUS_WRITE
            row["reason"] = ("The dataset refused to store this row (%s). Nothing was charged "
                             "for it." % type(error).__name__)
            row["billable"] = False
            summary["datasetWriteErrors"] += 1
            count_status(row)
            write_failures += 1
            if stop_reason is None:
                stop_reason = "dataset-write-error"
            if write_failures >= 3:
                run_stopped = stop_reason = "dataset-write-error"
            return False

        # ---- plan the rows: normalize, dedupe, apply the limit ----------------
        plans = []           # (row, needs_lookup)
        seen = {}
        for index, line in enumerate(lines, 1):
            row = _row(index, line)
            try:
                parsed = dom.normalize(line)
            except dom.BadDomain as exc:
                row["status"] = STATUS_INVALID
                row["reason"] = "Not a domain: %s." % exc
                plans.append((row, False))
                continue
            row["domain"] = parsed["domain"]
            row["tld"] = parsed["tld"]
            if parsed["domain"] in seen:
                row["status"] = STATUS_DUPLICATE
                row["reason"] = ("The same domain came in earlier at line %d: it is answered "
                                 "once and this line is not charged." % seen[parsed["domain"]])
                summary["duplicates"] += 1
                plans.append((row, False))
                continue
            seen[parsed["domain"]] = index
            plans.append((row, True))

        lookup_rows = [row for row, wants in plans if wants]
        summary["uniqueDomains"] = len(lookup_rows)
        allowed = lookup_rows[:max_domains]
        beyond = lookup_rows[max_domains:]
        for row in beyond:
            row["status"] = STATUS_LIMIT
            row["reason"] = ("Not read: maxDomains is %d and this domain is beyond the limit. "
                             "Nothing was charged." % max_domains)
            plans = [(r, False if r is row else w) for r, w in plans]
        summary["cutByMaxDomains"] = len(beyond)

        # ---- one domain, one answer -------------------------------------------
        async def read_one(client, row: dict, mapping: dict) -> dict:
            try:
                return await _read(client, row, mapping)
            except Exception as exc:  # noqa: BLE001 - one domain must not lose the wave
                row["status"] = STATUS_SOURCE
                row["reason"] = ("The lookup itself failed: %s: %s. Nothing was charged."
                                 % (type(exc).__name__, str(exc)[:120]))
                return row

        async def _read(client, row: dict, mapping: dict) -> dict:
            answer = await rdap.lookup(client, row["domain"], mapping)
            row["httpStatus"] = answer.get("http")
            row["rdapUrl"] = answer.get("url")
            if answer["status"] == rdap.STATUS_UNSUPPORTED:
                row["status"] = STATUS_NO_RDAP
                row["reason"] = ("The registry for .%s publishes no RDAP service, so there is no "
                                 "registration record to read. DNS records are still returned."
                                 % row["tld"])
            elif answer["status"] == rdap.STATUS_ERROR:
                row["status"] = STATUS_SOURCE
                row["reason"] = ("The registry did not answer: %s. Not a statement about the "
                                 "domain, and nothing was charged." % answer["reason"])
            elif answer["status"] == rdap.STATUS_AVAILABLE:
                row["status"] = STATUS_AVAILABLE
                row["registered"] = False
                row["registrySource"] = "rdap"
                row["reason"] = "The registry says this name is not registered."
                return row
            else:
                facts = rdap.parse(answer["payload"] or {})
                row["status"] = STATUS_REGISTERED
                row["registered"] = True
                row["registrySource"] = "rdap"
                row["registrar"] = facts["registrar"]
                row["createdAt"] = facts["createdAt"]
                row["expiresAt"] = facts["expiresAt"]
                row["updatedAt"] = facts["updatedAt"]
                row["statuses"] = facts["statuses"]
                row["nameservers"] = facts["nameservers"]
                row["registrantCountry"] = facts["registrantCountry"]
                row["dnssecSigned"] = facts["dnssecSigned"]
            row["ageDays"] = dom.age_days(row["createdAt"])
            row["daysUntilExpiry"] = dom.days_until(row["expiresAt"])
            if include_dns and row["status"] in (STATUS_REGISTERED, STATUS_NO_RDAP):
                records = await dnsrec.lookup(row["domain"])
                row["dns"] = records
                row["dnsResolver"] = records.get("resolver")
                if not row["nameservers"]:
                    row["nameservers"] = list(records.get("nameservers") or [])
                if row["dnssecSigned"] is None and records.get("dnssecSigned") is not None:
                    row["dnssecSigned"] = records.get("dnssecSigned")
            return row

        async def run_wave(client, mapping: dict, rows: list) -> None:
            nonlocal run_stopped, stop_reason, consecutive_failures, billing_active
            tasks = [asyncio.create_task(read_one(client, row, mapping)) for row in rows]
            for task in tasks:
                try:
                    row = await task
                except Exception as exc:  # noqa: BLE001
                    row = None
                    Actor.log.error("lookup task failed: %s", exc)
                    continue
                if row is None:
                    continue
                await deliver(row)

        async def deliver(row: dict) -> None:
            nonlocal run_stopped, stop_reason, consecutive_failures, billing_active
            paid_status = row["status"] in (STATUS_REGISTERED, STATUS_AVAILABLE)
            if not paid_status or not billing_active:
                if paid_status and not billing_active:
                    row["billingInactive"] = True
                    if await push(row):
                        summary["deliveredUncharged"] += 1
                    return
                await push(row)
                return
            row["billable"] = True
            before = await item_count()
            try:
                result = await Actor.push_data(row, charged_event_name=CHARGE_EVENT)
                billed = int(getattr(result, "charged_count", 0) or 0)
                if billed:
                    consecutive_failures = 0
                    summary["chargedEvents"] += billed
                    count_row(row)
                    if getattr(result, "event_charge_limit_reached", False):
                        run_stopped = stop_reason = "spending-cap"
                elif getattr(result, "event_charge_limit_reached", False):
                    row["status"] = STATUS_CAP
                    row["reason"] = ("The spending cap was reached, so this domain was not "
                                     "charged and its registry data is not delivered.")
                    row["billable"] = False
                    row["registrar"] = None
                    run_stopped = stop_reason = "spending-cap"
                    await push(row)
                else:
                    billing_active = False
                    summary["deliveredUncharged"] += 1
                    row["billingInactive"] = True
                    await push(row)
            except Exception as exc:  # noqa: BLE001
                after = await item_count()
                Actor.log.error("billing failed for %s: %s", row["domain"], exc)
                if len(charge_errors) < CHARGE_ERROR_LIMIT:
                    charge_errors.append("%s: %s: %s" % (row["domain"], type(exc).__name__,
                                                         str(exc)[:120]))
                if before is not None and after is not None:
                    wrote = after > before
                else:
                    summary["datasetWriteUnverified"] += 1
                    wrote = True
                if wrote:
                    # The row reached the buyer, so it counts as a delivery and as a free one:
                    # the money side failed, the data did not.
                    summary["deliveredUncharged"] += 1
                    count_row(row)
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        billing_active = False
                        if stop_reason is None:
                            run_stopped = stop_reason = "charge-error"
                else:
                    # Nothing was stored: push() decides whether the row is lost, and names it
                    # "dataset-write-error", not a billing failure.
                    await push(row)

        async with httpx.AsyncClient(headers=rdap.HEADERS, follow_redirects=True) as client:
            try:
                mapping = await rdap.bootstrap(client)
            except Exception as exc:  # noqa: BLE001 - the fallback table still works
                Actor.log.warning("bootstrap read failed: %s", exc)
                mapping = dict(rdap.FALLBACK_BASES)
            summary["rdapBootstrapTlds"] = len(mapping)

            queue = [row for row, wants in plans if wants and row["status"] == STATUS_SOURCE]
            while queue:
                if run_stopped:
                    for row in queue:
                        if run_stopped == "spending-cap":
                            row["status"] = STATUS_CAP
                            row["reason"] = "Not read: the run reached the spending cap earlier."
                        else:
                            row["status"] = STATUS_BILLING
                            row["reason"] = ("Not read: the run already knew it could not bill "
                                             "(billingActive=false), so nothing was delivered.")
                        await push(row)
                    queue = []
                    break
                if deadline is not None and time.monotonic() > deadline - 10:
                    summary["deadlineCapped"] = True
                    for row in queue:
                        row["status"] = STATUS_TIME
                        row["reason"] = ("Not read: the run was close to the platform deadline, so "
                                         "these domains were left for a second run. Nothing was "
                                         "charged.")
                        await push(row)
                    queue = []
                    break
                wave, queue = queue[:WAVE], queue[WAVE:]
                await run_wave(client, mapping, wave)

        # ---- rows that never needed a request --------------------------------
        for row, wants in plans:
            if wants:
                continue
            if row["status"] in (STATUS_INVALID, STATUS_DUPLICATE, STATUS_LIMIT):
                await push(row)

        summary["processed"] = sum(count for status, count in summary["byStatus"].items()
                                   if not status.startswith("NOT_CHECKED"))
        summary["notChecked"] = sum(count for status, count in summary["byStatus"].items()
                                    if status.startswith("NOT_CHECKED"))
        summary["delivered"] = summary["byStatus"].get(STATUS_REGISTERED, 0) + \
            summary["byStatus"].get(STATUS_AVAILABLE, 0)
        summary["billingActive"] = billing_active
        summary["stopReason"] = stop_reason
        api_rows = await item_count()
        summary["datasetItemCount"] = summary["rowsDelivered"]
        summary["datasetItemCountApi"] = api_rows
        summary["datasetItemCountLag"] = api_rows is not None and api_rows < summary["rowsDelivered"]

        try:
            store = await Actor.open_key_value_store(name=SUMMARY_STORE)
            run_id = (os.environ.get("ACTOR_RUN_ID")
                      or Actor.get_env().get("run_id")
                      or "run")
            await store.set_value("RUN_SUMMARY-%s" % run_id, summary)
        except Exception as exc:  # noqa: BLE001
            Actor.log.warning("the named summary store is unavailable: %s", exc)
        try:
            run_store = await Actor.open_key_value_store()
            await run_store.set_value("RUN_SUMMARY", summary)
        except Exception as exc:  # noqa: BLE001
            Actor.log.warning("the run summary could not be stored: %s", exc)

        Actor.log.info("done: requested %s, unique %s, delivered %s, charged %s, stop %s",
                       summary["requested"], summary["uniqueDomains"], summary["delivered"],
                       summary["chargedEvents"], stop_reason)

        if summary["delivered"] == 0:
            if stop_reason == "spending-cap":
                await Actor.fail(status_message=(
                    "The spending cap was reached before any domain was delivered: %d line(s) "
                    "were sent. Raise the run's max total charge and run again." % requested))
            elif stop_reason == "dataset-write-error":
                await Actor.fail(status_message=(
                    "The run dataset refused to store rows: %d of %d came back and the rest are "
                    "reported as not checked. Nothing was charged for the lost rows."
                    % (summary["rowsDelivered"], requested)))
            elif summary["byStatus"].get(STATUS_NO_RDAP) or summary["byStatus"].get(STATUS_SOURCE):
                await Actor.fail(status_message=_answer_message(summary["byStatus"], requested))
            else:
                await Actor.fail(status_message=_answer_message(summary["byStatus"], requested))


if __name__ == "__main__":
    asyncio.run(main())
