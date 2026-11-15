"""LinkedIn Jobs API: public job search, one row per job, paid per delivered row.

Money rules, the same discipline that passed the gates of the actors before this:

  * one door for paid rows: Actor.push_data(rows, charged_event_name=...); the SDK
    writes the row and then charges it, so there is never a charge without a row;
  * the price is read once, synchronously; if it cannot be read the run stops
    before the first payable job and the rest goes out free with a reason;
  * a row is sold only when it reached the dataset; a status row is never a copy
    of the product: no title, no company, no description, no job id;
  * everything that was not sold says why, and costs nothing.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

from apify import Actor

from . import linkedin as li
from .browser import Browser

ST_OK = "OK"
ST_INVALID = "INVALID_INPUT"
ST_DUPLICATE = "DUPLICATE"
ST_NO_JOBS = "NO_JOBS_FOUND"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_BLOCKED = "NOT_CHECKED_BLOCKED"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"

EVENT_NAME = "job-delivered"
SUMMARY_STORE = "linkedin-jobs-summary"
BATCH_SIZE = 20
MAX_JOBS_DEFAULT = 50
MAX_LENSES_DEFAULT = 3
NAV_TIMEOUT_S = 20.0
PAGE_MARGIN_S = 28.0
DATE_VALUES = ("any", "past24h", "pastWeek", "pastMonth")
EXPERIENCE_VALUES = ("any", "internship", "entry", "associate", "mid-senior", "director", "executive")
REMOTE_VALUES = ("any", "remote", "hybrid", "onsite")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def platform_deadline() -> float | None:
    """When the platform will kill this run, as a unix moment, if it is known."""
    raw = str(os.environ.get("ACTOR_TIMEOUT_AT") or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return moment.timestamp()
    except Exception:  # noqa: BLE001 - an unreadable clock must not stop the run
        return None


def time_left(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.time()


def nav_budget(deadline: float | None) -> float:
    """Never give a page more seconds than the run has left, minus a summary margin."""
    left = time_left(deadline)
    if left is None:
        return NAV_TIMEOUT_S
    return max(5.0, min(NAV_TIMEOUT_S, left - 8.0))


async def dataset_items(dataset) -> int | None:
    try:
        meta = await dataset.get_metadata()
    except Exception:  # noqa: BLE001 - a missing counter is not a lost row
        return None
    count = getattr(meta, "item_count", None)
    if count is None and isinstance(meta, dict):
        count = meta.get("item_count")
    return count if isinstance(count, int) else None


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}
        keywords = li.clean(raw.get("keywords")) or ""
        if not keywords:
            await Actor.fail(status_message=(
                "keywords is empty - pass at least one search phrase (for example "
                "\"python developer\"): nothing was read and nothing was charged"))
            return

        location = li.clean(raw.get("location")) or ""
        date_posted = str(raw.get("datePosted") or "any").strip()
        experience = str(raw.get("experience") or "any").strip()
        remote = str(raw.get("remote") or "any").strip()
        refused = ""
        if date_posted not in DATE_VALUES:
            refused = f"datePosted must be one of {', '.join(DATE_VALUES)}"
        elif experience not in EXPERIENCE_VALUES:
            refused = f"experience must be one of {', '.join(EXPERIENCE_VALUES)}"
        elif remote not in REMOTE_VALUES:
            refused = f"remote must be one of {', '.join(REMOTE_VALUES)}"

        max_jobs = max(1, min(int_or(raw.get("maxJobs"), MAX_JOBS_DEFAULT), li.MAX_JOBS_CLAMP))
        max_lenses = max(1, min(int_or(raw.get("maxSearches"), MAX_LENSES_DEFAULT), li.MAX_LENSES))
        include_description = bool(raw.get("includeDescription", True))
        proxy_url = str(os.environ.get("LINKEDIN_PROXY_URL") or "").strip() or None

        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()
        can_charge, price_note = price_state(charging)
        billable_claim = None if can_charge is None else bool(can_charge)
        deadline = platform_deadline()

        counts = {
            "jobsSeen": 0, "jobsUnique": 0, "jobsDelivered": 0, "jobsDuplicate": 0,
            "jobsLimited": 0, "jobsCapped": 0, "jobsStopped": 0, "jobsUnreadable": 0,
            "pagesRead": 0, "pagesBlocked": 0, "pagesFailed": 0,
        }
        by_status: dict[str, int] = {}
        rows_written = 0
        charged_total = 0
        delivered_uncharged = 0
        charge_uncertain = 0
        write_errors = 0
        dataset_note = None
        stopping = False
        stop_reason = ""
        page_errors: list[str] = []
        seen_ids: set[str] = set()
        batch: list[dict] = []
        browser_mode = "not started"
        browser_note = ""
        proxy_used = bool(proxy_url)
        lens_list: list[str] = []

        def note(status: str, amount: int = 1) -> None:
            by_status[status] = by_status.get(status, 0) + amount

        async def free_status(value: str, index: int, status: str, reason: str) -> None:
            """A row that carries a reason and nothing a buyer could use as data."""
            nonlocal rows_written, write_errors, stopping, stop_reason, dataset_note
            row = {"input": value, "inputIndex": index, "jobId": None, "status": status,
                   "reason": reason, "billable": False}
            try:
                await dataset.push_data([row])
            except Exception:  # noqa: BLE001
                write_errors += 1
                stopping = True
                stop_reason = stop_reason or "dataset-write-error"
                dataset_note = "free status rows were refused by the dataset"
                return
            rows_written += 1
            note(status)

        async def write_paid(rows: list[dict]) -> None:
            """Write and charge one batch; any doubt about money stops the run."""
            nonlocal rows_written, charged_total, delivered_uncharged, charge_uncertain
            nonlocal stopping, stop_reason, dataset_note, write_errors
            if not rows:
                return
            if can_charge is False:
                for row in rows:
                    row["billable"] = False
                try:
                    await dataset.push_data(rows)
                except Exception:  # noqa: BLE001
                    write_errors += 1
                    stopping = True
                    stop_reason = stop_reason or "dataset-write-error"
                    dataset_note = "rows were refused by the dataset"
                    return
                # Not sold per event: the rows are delivered but nothing is charged, and the
                # summary says exactly that instead of a mismatch it cannot explain.
                rows_written += len(rows)
                counts["jobsDelivered"] += len(rows)
                delivered_uncharged += len(rows)
                note(ST_OK, len(rows))
                return
            if can_charge is None:
                # The price could not be read: sell nothing from here on.
                stopping = True
                stop_reason = stop_reason or "billing-price-unknown"
                for row in rows:
                    counts["jobsStopped"] += 1
                    await free_status(row.get("input") or row.get("jobId"), 0, ST_BILLING,
                                      f"not delivered and not charged: {price_note}")
                return
            if charging.is_event_charge_limit_reached(EVENT_NAME):
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in rows:
                    counts["jobsCapped"] += 1
                    await free_status(row.get("input") or row.get("jobId"), 0, ST_CAP,
                                      "the spending cap was reached: not delivered and "
                                      "not charged")
                return
            # The SDK cuts a batch that does not fit the remaining budget, writes the prefix
            # and charges only that prefix, without a word. Cut it here first: no row may
            # vanish, and the counters must follow the charge, not the batch size.
            try:
                budget = int(charging.compute_push_data_limit(
                    len(rows), EVENT_NAME, is_default_dataset=True))
            except Exception:  # noqa: BLE001 - no budget answer means no sale
                budget = None
            if budget is None:
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in rows:
                    counts["jobsCapped"] += 1
                    await free_status(row.get("input") or row.get("jobId"), 0, ST_CAP,
                                      "the remaining budget could not be computed: not "
                                      "delivered and not charged")
                return
            pay, over = rows[:max(0, budget)], rows[max(0, budget):]
            if over:
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in over:
                    counts["jobsCapped"] += 1
                    await free_status(row.get("input") or row.get("jobId"), 0, ST_CAP,
                                      "the spending cap leaves no budget for this row: not "
                                      "delivered and not charged")
            if not pay:
                return
            for row in pay:
                row["billable"] = billable_claim
            try:
                result = await Actor.push_data(pay, charged_event_name=EVENT_NAME)
            except Exception as exc:  # noqa: BLE001
                # One call writes and then charges, so an exception does not say which
                # half failed. A repeat would put two rows per job in the dataset, so
                # the run names the doubt and stops.
                landed = await dataset_items(dataset)
                if landed is not None and landed >= rows_written + len(pay):
                    delivered_uncharged += len(pay)
                    counts["jobsDelivered"] += len(pay)
                    rows_written += len(pay)
                    note(ST_OK, len(pay))
                    dataset_note = (f"a batch of {len(pay)} is in the dataset but the charge "
                                    "is not confirmed: stop after charge-error")
                else:
                    charge_uncertain += len(pay)
                    for row in pay:
                        await free_status(row.get("input") or row.get("jobId"), 0,
                                          "NOT_CHECKED_CHARGE_UNVERIFIED",
                                          "the charge failed and the row was not confirmed in "
                                          "the dataset: not sold")
                stopping = True
                if stop_reason and stop_reason != "charge-error":
                    dataset_note = dataset_note or (f"stopped on {stop_reason} first, then the "
                                                    "charge failed")
                # A dead charge outranks an earlier reason: it is the money fact.
                stop_reason = "charge-error"
                dataset_note = dataset_note or f"charge failed: {type(exc).__name__}"
                return
            charged = getattr(result, "charged_count", None)
            if not isinstance(charged, int) or charged < 0:
                charged = None
            if charged is None:
                # The SDK did not say what it charged: name the doubt, never claim a sale.
                landed = await dataset_items(dataset)
                if landed is not None and landed >= rows_written + len(pay):
                    delivered_uncharged += len(pay)
                    counts["jobsDelivered"] += len(pay)
                    rows_written += len(pay)
                    note(ST_OK, len(pay))
                    dataset_note = ("the batch is in the dataset but the SDK did not report "
                                    "what it charged: stop after charge-error")
                else:
                    charge_uncertain += len(pay)
                    for row in pay:
                        await free_status(row.get("input") or row.get("jobId"), 0,
                                          "NOT_CHECKED_CHARGE_UNVERIFIED",
                                          "the charge result was unreadable and the row was not "
                                          "confirmed in the dataset: not sold")
                stopping = True
                if stop_reason and stop_reason != "charge-error":
                    dataset_note = dataset_note or (f"stopped on {stop_reason} first, then the "
                                                    "charge result was unreadable")
                # A dead charge outranks an earlier reason: it is the money fact.
                stop_reason = "charge-error"
                dataset_note = dataset_note or "the charge result carried no count"
                return
            if charged < len(pay):
                # The budget ran out inside the batch: those rows were never written.
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in pay[charged:]:
                    counts["jobsCapped"] += 1
                    await free_status(row.get("input") or row.get("jobId"), 0, ST_CAP,
                                      "the budget ran out inside this batch: not delivered "
                                      "and not charged")
            rows_written += charged
            counts["jobsDelivered"] += charged
            charged_total += charged
            if charged:
                note(ST_OK, charged)

        async def flush() -> None:
            nonlocal batch
            if batch:
                rows, batch = batch, []
                await write_paid(rows)

        browser = Browser()
        started = await browser.start(proxy_url=proxy_url)
        browser_mode = browser.mode
        browser_note = browser.reason
        if started is not None:
            await Actor.fail(status_message=(
                f"the browser could not start ({started}): nothing was read and nothing "
                "was charged"))
            return
        try:
            if refused:
                await Actor.fail(status_message=(
                    f"{refused}: nothing was read and nothing was charged"))
                return
            if can_charge is None:
                await Actor.fail(status_message=(
                    f"the price could not be read ({price_note}): nothing was read and "
                    "nothing was charged"))
                return

            lens_list = li.lenses(date_posted)[:max_lenses]
            jobs: list[dict] = []
            for lens in lens_list:
                if stopping or len(jobs) >= max_jobs:
                    break
                left = time_left(deadline)
                if left is not None and left < PAGE_MARGIN_S:
                    stopping = True
                    stop_reason = stop_reason or "deadline"
                    break
                url = li.search_url(keywords, location, date_posted=lens,
                                    experience=experience, remote=remote)
                page = await browser.read_cards(url, nav_budget(deadline))
                counts["pagesRead"] += 1
                if not page.get("ok"):
                    counts["pagesFailed"] += 1
                    page_errors.append(str(page.get("error") or "the page answered nothing")[:200])
                    if page.get("timeout") or "timeout" in str(page.get("error", "")).lower():
                        stopping = True
                        stop_reason = stop_reason or "deadline"
                    continue
                found = page.get("jobs") or []
                # A guest search always carries a sign-in banner, so a banner is only a
                # wall when the page brought no cards at all.
                wall = li.text_is_wall(page.get("title"), page.get("body"), bool(found))
                if wall:
                    counts["pagesBlocked"] += 1
                    stopping = True
                    stop_reason = stop_reason or "wall"
                    break
                if not found and lens == lens_list[0]:
                    head = " ".join(str(page.get("body") or "").split())[:180]
                    await free_status(keywords, 0, ST_NO_JOBS,
                                      "the search page carried no job cards for these terms"
                                      + (f": the page said {head!r}" if head else ""))
                for card in found:
                    job_id = li.clean(card.get("id"))
                    if not job_id:
                        continue
                    counts["jobsSeen"] += 1
                    if job_id in seen_ids:
                        counts["jobsDuplicate"] += 1
                        continue
                    seen_ids.add(job_id)
                    jobs.append(card)
                    counts["jobsUnique"] += 1

            for index, card in enumerate(jobs):
                if stopping:
                    break
                if index >= max_jobs:
                    for rest in jobs[index:]:
                        counts["jobsLimited"] += 1
                        await free_status(li.canonical_url(str(rest.get("id"))), index,
                                          ST_LIMIT, f"beyond maxJobs {max_jobs}: not read and "
                                                    "not charged")
                    break
                left = time_left(deadline)
                if left is not None and left < PAGE_MARGIN_S:
                    stopping = True
                    stop_reason = stop_reason or "deadline"
                    for rest in jobs[index:]:
                        counts["jobsStopped"] += 1
                        await free_status(li.canonical_url(str(rest.get("id"))), index,
                                          ST_TIME, "the run ran out of time: not read and "
                                                   "not charged")
                    break
                detail = None
                if include_description:
                    page = await browser.read_job(li.canonical_url(str(card.get("id"))),
                                                  nav_budget(deadline))
                    if not page.get("ok"):
                        counts["jobsUnreadable"] += 1
                        await free_status(li.canonical_url(str(card.get("id"))), index,
                                          ST_ERROR, f"the job page could not be read: "
                                                    f"{page.get('error', 'unknown')}")
                        continue
                    wall = li.text_is_wall(page.get("pageTitle"), page.get("body")) if not page.get("description") else None
                    if wall:
                        counts["jobsUnreadable"] += 1
                        await free_status(li.canonical_url(str(card.get("id"))), index,
                                          ST_BLOCKED, f"a wall answered instead of the job: {wall}")
                        continue
                    detail = page
                row = li.job_row(card, detail)
                row["input"] = li.canonical_url(str(card.get("id")))
                row["inputIndex"] = index
                row["status"] = ST_OK
                row["fetchedAt"] = now_iso()
                batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    await flush()
            await flush()
        finally:
            await browser.close()

        if rows_written == 0 and page_errors:
            stop_reason = stop_reason or "search-page-failed"
            await free_status(keywords, 0, ST_BLOCKED,
                              "the LinkedIn search page could not be read, so no job is "
                              f"invented and nothing is charged: {page_errors[0]}")
        summary = {
            "actor": "linkedin-jobs-api",
            "keywords": keywords,
            "location": location or None,
            "filters": {"datePosted": date_posted, "experience": experience, "remote": remote},
            "maxJobs": max_jobs,
            "searchesPlanned": len(lens_list),
            "pagesRead": counts["pagesRead"],
            "pagesBlocked": counts["pagesBlocked"],
            "pagesFailed": counts["pagesFailed"],
            "pageErrors": page_errors[:3],
            "jobsSeen": counts["jobsSeen"],
            "jobsUnique": counts["jobsUnique"],
            "jobsDelivered": counts["jobsDelivered"],
            "jobsDuplicate": counts["jobsDuplicate"],
            "jobsLimited": counts["jobsLimited"],
            "jobsCapped": counts["jobsCapped"],
            "jobsStopped": counts["jobsStopped"],
            "jobsUnreadable": counts["jobsUnreadable"],
            "rowsWritten": rows_written,
            "byStatus": by_status,
            "chargedEvents": charged_total,
            "deliveredUncharged": delivered_uncharged,
            "chargeUncertain": charge_uncertain,
            "writeErrors": write_errors,
            "billingActive": (None if can_charge is None else bool(can_charge)),
            "priceNote": price_note,
            "priceEvent": EVENT_NAME,
            "counterMismatch": counts["jobsDelivered"] != (charged_total + delivered_uncharged),
            "browserMode": browser_mode,
            "browserNote": browser_note,
            "proxyUsed": proxy_used,
            "stopReason": stop_reason,
            "datasetNote": dataset_note,
            "finishedAt": now_iso(),
        }
        await Actor.set_status_message(
            f"{counts['jobsDelivered']} job(s) delivered and {charged_total} charged"
            + (f", stopped: {stop_reason}" if stop_reason else ""))
        try:
            await Actor.set_value("RUN_SUMMARY", summary)
            run_id = str((Actor.get_env() or {}).get("run_id") or "").strip()
            if run_id:
                store = await Actor.open_key_value_store(name=SUMMARY_STORE)
                await store.set_value("RUN_SUMMARY-" + run_id, summary)
        except Exception:  # noqa: BLE001 - the dataset is the product, the summary is a note
            Actor.log.warning("the run summary could not be written")


def price_state(charging) -> tuple[bool | None, str]:
    """Can this run charge? True / False / None (unknown), and a plain reason.

    get_pricing_info() is synchronous in the SDK: awaiting it silently zeroes the
    whole money path, and this park has paid for that lesson once already.
    """
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001 - any failure means "price unknown"
        return None, f"pricing read failed: {type(exc).__name__}"
    if info is None:
        return None, "pricing info is not readable"
    is_ppe = getattr(info, "is_pay_per_event", None)
    if is_ppe is None and isinstance(info, dict):
        is_ppe = info.get("isPayPerEvent", info.get("is_pay_per_event"))
    prices = getattr(info, "per_event_prices", None)
    if prices is None and isinstance(info, dict):
        prices = info.get("perEventPrices") or info.get("per_event_prices") or {}
    names: list[str] = []
    if isinstance(prices, dict):
        names = [str(key) for key in prices]
    elif isinstance(prices, list):
        for item in prices:
            name = getattr(item, "event_name", None)
            if name is None and isinstance(item, dict):
                name = item.get("eventName") or item.get("event_name")
            if name:
                names.append(str(name))
    if is_ppe is False:
        return False, "the actor is not sold per event"
    if EVENT_NAME not in names:
        if is_ppe is None and not names:
            return None, "the pricing answer is not understood"
        return False, f"{EVENT_NAME} is not priced, rows go out free"
    return True, f"{EVENT_NAME} is priced"


if __name__ == "__main__":
    asyncio.run(main())
