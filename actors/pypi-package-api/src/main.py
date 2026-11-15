"""PyPI Package API: one paid row per project that exists on PyPI.

Money rules the park learned the hard way: a paid row is written and charged in ONE call,
the batch is cut by the remaining budget before the write, and an exception from that call
means "charge failed after the write", "the dataset refused the write" or "nobody knows" -
only the dataset counter tells them apart, and when it cannot be read the answer is
"unknown", never "nothing was delivered". A name with no project behind it is a verdict and
comes back FREE with a reason; an unreachable PyPI is not a verdict about the project.

The API is keyless and asks for politeness: ids go in chunks with a pause between calls,
because one call per id would spend the whole run on sleeping.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import httpx
from apify import Actor

from . import pypi

ST_OK = "OK"
ST_INVALID = "INVALID_INPUT"
ST_DUPLICATE = "DUPLICATE"
ST_NOT_FOUND = "NOT_FOUND"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
# A slice by maxPackages is not a spending limit: README defines CAP as the
# buyer's money ceiling, so the two get different statuses.
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_BILLING = "NOT_CHECKED_BILLING"

EVENT_NAME = "package-delivered"
SOURCE = "PyPI JSON API (keyless)"
BATCH_SIZE = 10
FREE_BATCH_SIZE = 100
CALL_COST_SECONDS = 3.0
DEADLINE_MARGIN_SECONDS = 15.0
DEADLINE_MIN_SECONDS = 8.0
MAX_PACKAGES = 500
MONEY_STOPS = ("spending-cap", "charge-error", "billing-price-unknown", "dataset-write-error")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def platform_deadline() -> float | None:
    raw = os.environ.get("ACTOR_TIMEOUT_AT")
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def time_left(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    import time as _time
    return deadline - _time.time()


def request_budget(left: float | None, calls: int = 1) -> float | None:
    """How long ONE PyPI request may take, from the seconds the run has left.

    A chunk of ids is not one request: the reader asks once per name, so a chunk of ten
    spends ten calls. Giving each call the whole remainder meant a chunk of ten overran the
    platform deadline (measured: 30.2 s against a 25 s remainder) and was killed after rows
    were already charged, with no summary written. The remainder is therefore shared.
    """
    if left is None:
        # In httpx timeout=None means "no timeout at all", so a silent socket hung a local run
        # for 180 s. No deadline is a local run, and even there a request has to give up.
        return pypi.TIMEOUT
    share = (left - DEADLINE_MARGIN_SECONDS - pypi.POLITE_SECONDS) / max(1, calls)
    return max(2.0, min(pypi.TIMEOUT, share))


def price_state(charging) -> tuple[bool | None, str]:
    """Can this run charge? True / False / None (unknown), and a plain reason.

    get_pricing_info() is synchronous in the SDK: awaiting it silently zeroes the money path
    of the whole run.
    """
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001
        return None, f"pricing read failed: {type(exc).__name__}"
    if info is None:
        return None, "pricing info is not readable"
    is_ppe = getattr(info, "is_pay_per_event", None)
    prices = getattr(info, "per_event_prices", None)
    names: list = []
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
        return False, "the actor is not sold per event, so nothing is charged"
    if is_ppe is None:
        return None, "the pricing model could not be read"
    if not names:
        return None, "the event price list is empty"
    if EVENT_NAME not in names:
        return None, f"the event {EVENT_NAME} is not in the price list ({', '.join(names)})"
    return True, ""


def _limit(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _run_id() -> str:
    raw = os.environ.get("ACTOR_RUN_ID")
    if raw:
        return str(raw)
    try:
        env = Actor.get_env() or {}
        return str(env.get("run_id") or "")
    except Exception:  # noqa: BLE001
        return ""


async def _item_count(dataset) -> int | None:
    """How many rows the dataset holds, or None when the platform does not say.

    get_metadata() answers a pydantic DatasetMetadata with item_count on it: reading it as a
    dict silently gives None and hides a refused write.
    """
    try:
        meta = await dataset.get_metadata()
    except Exception:  # noqa: BLE001
        return None
    count = getattr(meta, "item_count", None)
    if count is None and isinstance(meta, dict):
        count = meta.get("itemCount", meta.get("item_count"))
    try:
        return int(count)
    except (TypeError, ValueError):
        return None


async def _item_count_after(dataset, tries: int = 3, pause: float = 0.3) -> int | None:
    """The dataset counter, read more than once and taken as the maximum.

    A single read taken right after a refused write is indistinguishable from a counter that
    is one write behind, and the wrong answer costs the buyer a second run and a second
    charge. The maximum of a few reads is the honest number.
    """
    best = None
    for attempt in range(max(1, tries)):
        count = await _item_count(dataset)
        if count is not None and (best is None or count > best):
            best = count
        if attempt + 1 < max(1, tries):
            await asyncio.sleep(pause)
    return best


async def main() -> None:
    await Actor.init()
    started = now_iso()

    data = await Actor.get_input() or {}
    raw = data.get("packages")
    if isinstance(raw, str):
        wanted = [raw]
    elif isinstance(raw, list):
        wanted = [str(item) for item in raw]
    else:
        wanted = []
    tasks = [str(item) for item in wanted]
    if not any(item.strip() for item in tasks):
        await Actor.fail(status_message=(
            "No packages were sent. Pass PyPI project names in the packages field, for "
            "example {\"packages\": [\"requests\", \"numpy==2.0.0\"]}."))
        return

    max_packages = _limit(data.get("maxPackages"), 100, 1, MAX_PACKAGES)
    wanted_abstract = data.get("includeDependencies")
    with_dependencies = True if wanted_abstract is None else bool(wanted_abstract)

    deadline = platform_deadline()
    if deadline is not None:
        left = time_left(deadline) or 0.0
        if left < DEADLINE_MIN_SECONDS:
            await Actor.fail(status_message=(
                f"The run has {int(left)} s left before the platform stops it, which is less "
                "than one package needs. Send fewer packages or raise the timeout."))
            return

    charging = Actor.get_charging_manager()
    can_charge, price_note = price_state(charging)
    billable_claim = None if can_charge is None else bool(can_charge)

    dataset = await Actor.open_dataset()
    baseline = await _item_count(dataset)
    client = httpx.Client(timeout=httpx.Timeout(pypi.TIMEOUT), follow_redirects=True,
                          headers={"User-Agent": pypi.USER_AGENT})

    counts = {"read": 0, "failed": 0, "notFound": 0, "invalid": 0, "duplicate": 0,
              "cutByDeadline": 0, "cutByStop": 0, "cutByLimit": 0, "delivered": 0,
              "capped": 0, "refused": 0}
    by_status: dict = {}
    sold_keys: set = set()
    batch: list = []
    free_batch: list = []
    rows_written = 0
    charged_total = 0
    delivered_uncharged = 0
    delivered_unverified = 0
    charge_uncertain = 0
    write_errors = 0
    stopping = False
    stop_reason = ""
    dataset_note = ""
    first_failure = ""

    def note(status: str, amount: int = 1) -> None:
        by_status[status] = by_status.get(status, 0) + amount

    def status_row(value: str, index: int, status: str, reason: str) -> dict:
        """A row that carries a reason and nothing a buyer could use as data."""
        return {"actorInput": value, "actorIndex": index, "status": status,
                "reason": reason, "billable": False}

    async def flush_free() -> None:
        """Write the buffered free status rows in chunks.

        One dataset call per status row meant that a 500-line run of junk made 500 calls; the
        rows are of one nature, so they travel in batches and the counters follow what
        actually landed.
        """
        nonlocal free_batch, rows_written, write_errors, first_failure, stopping
        nonlocal stop_reason, dataset_note
        rows, free_batch = free_batch, []
        for start in range(0, len(rows), FREE_BATCH_SIZE):
            chunk = rows[start:start + FREE_BATCH_SIZE]
            landed = False
            for attempt in (1, 2):
                try:
                    await Actor.push_data(chunk)
                except Exception as exc:  # noqa: BLE001
                    if attempt == 1:
                        await asyncio.sleep(0.3)
                        continue
                    # A refused free batch used to be counted and skipped, so the header
                    # promised rows the dataset never got. It stops the run instead, and the
                    # summary counts what is really missing.
                    write_errors += len(chunk)
                    first_failure = first_failure or (
                        "a status row could not be written (%s)" % type(exc).__name__)
                    stopping = True
                    stop_reason = stop_reason or "dataset-write-error"
                    dataset_note = dataset_note or (
                        f"{len(chunk)} free status row(s) were refused by the dataset twice: "
                        "they are counted in writeErrors and are not in the dataset")
                else:
                    landed = True
                    break
            if landed:
                rows_written += len(chunk)

    async def free_status(value: str, index: int, status: str, reason: str) -> None:
        free_batch.append(status_row(value, index, status, reason))
        note(status)
        if len(free_batch) >= FREE_BATCH_SIZE:
            await flush_free()

    async def write_paid(rows: list) -> None:
        nonlocal batch, rows_written, charged_total, delivered_uncharged
        nonlocal delivered_unverified, charge_uncertain, write_errors
        nonlocal stopping, stop_reason, dataset_note, first_failure
        if not rows:
            return
        if can_charge is None:
            stopping = True
            stop_reason = stop_reason or "billing-price-unknown"
            for row in rows:
                await free_status(row.get("actorInput") or "", int(row.get("actorIndex") or 0),
                                  ST_BILLING,
                                  f"not delivered and not charged: {price_note}")
            return
        if can_charge is False:
            # The platform says this actor is not sold per event: nothing can be charged, so
            # the rows are delivered free and the summary says so instead of pretending a
            # sale the price list cannot support.
            for row in rows:
                row["billable"] = False
            try:
                await Actor.push_data(rows)
            except Exception as exc:  # noqa: BLE001
                write_errors += 1
                delivered_unverified += len(rows)
                stopping = True
                stop_reason = "dataset-write-error"
                first_failure = first_failure or ("a write raised %s" % type(exc).__name__)
                dataset_note = (f"a free write raised {type(exc).__name__}: delivery is "
                                "unverified, check the dataset before re-running")
                return
            counts["delivered"] += len(rows)
            rows_written += len(rows)
            note(ST_OK, len(rows))
            delivered_uncharged += len(rows)
            dataset_note = dataset_note or (f"nothing is charged: {price_note}; the rows are "
                                            "delivered free")
            return
        if charging.is_event_charge_limit_reached(EVENT_NAME):
            stopping = True
            stop_reason = stop_reason or "spending-cap"
            for row in rows:
                counts["capped"] += 1
                await free_status(row.get("actorInput") or "", int(row.get("actorIndex") or 0),
                                  ST_CAP,
                                  "the spending cap was reached: not delivered and not charged")
            return
        try:
            budget = int(charging.compute_push_data_limit(
                len(rows), EVENT_NAME, is_default_dataset=True))
        except Exception:  # noqa: BLE001
            budget = None
        if budget is None:
            stopping = True
            stop_reason = stop_reason or "spending-cap"
            for row in rows:
                counts["capped"] += 1
                await free_status(row.get("actorInput") or "", int(row.get("actorIndex") or 0),
                                  ST_CAP,
                                  "the remaining budget could not be computed: not delivered "
                                  "and not charged")
            return
        pay, over = rows[:max(0, budget)], rows[max(0, budget):]
        if over:
            stopping = True
            stop_reason = stop_reason or "spending-cap"
            for row in over:
                counts["capped"] += 1
                await free_status(row.get("actorInput") or "", int(row.get("actorIndex") or 0),
                                  ST_CAP,
                                  "the remaining budget ended before this package: not delivered "
                                  "and not charged")
        if not pay:
            return
        for row in pay:
            row["billable"] = billable_claim is not False
        before = None if baseline is None else baseline + rows_written
        try:
            result = await Actor.push_data(pay, charged_event_name=EVENT_NAME)
        except Exception as exc:  # noqa: BLE001
            after = await _item_count_after(dataset)
            grew = None if before is None or after is None else after - before
            if grew is not None and (grew < 0 or grew > len(pay)):
                grew = None
            if grew is not None and grew >= len(pay):
                charge_uncertain += len(pay)
                delivered_uncharged += len(pay)
                counts["delivered"] += len(pay)
                rows_written += len(pay)
                note(ST_OK, len(pay))
                stopping = True
                stop_reason = "charge-error"
                dataset_note = ("a charge failed: the rows stay in the dataset and are counted "
                                "in chargeUncertain, no second row is written")
            else:
                write_errors += 1
                delivered_unverified += len(pay)
                charge_uncertain += len(pay)
                stopping = True
                stop_reason = "dataset-write-error"
                first_failure = first_failure or ("a paid write raised %s" % type(exc).__name__)
                if grew is None:
                    dataset_note = (
                        f"a paid write raised {type(exc).__name__} and the dataset count could "
                        "not be read: delivery and the charge are both unknown, the batch is "
                        "counted in deliveredUnverified and chargeUncertain, check the "
                        "dataset before re-running")
                elif grew == 0:
                    dataset_note = (
                        f"a paid write raised {type(exc).__name__} and the dataset counter did "
                        "not grow: either the write was refused or the counter lags, the "
                        "charge is not confirmed. The batch is counted in deliveredUnverified "
                        "and chargeUncertain, check the dataset before re-running")
                else:
                    dataset_note = (
                        f"a paid write raised {type(exc).__name__} and the dataset counter grew "
                        f"by {grew} of {len(pay)}: delivery is partial and the charge is not "
                        "confirmed. The batch is counted in deliveredUnverified and "
                        "chargeUncertain, check the dataset before re-running")
            return
        charged = getattr(result, "charged_count", None)
        if charged is None:
            counts["delivered"] += len(pay)
            charge_uncertain += len(pay)
            stopping = True
            stop_reason = "charge-error"
            dataset_note = ("the charge result did not report a count: the rows are delivered "
                            "and not claimed as sold")
            rows_written += len(pay)
            note(ST_OK, len(pay))
            return
        charged = int(charged)
        if charged < len(pay):
            dataset_note = (f"the platform charged {charged} of {len(pay)} rows in this batch: "
                            "the rest is delivered and not charged, and the run stops here")
            stopping = True
            stop_reason = stop_reason or "spending-cap"
        charged_total += charged
        counts["delivered"] += len(pay)
        rows_written += len(pay)
        note(ST_OK, len(pay))
        delivered_uncharged += len(pay) - charged

    async def flush() -> None:
        nonlocal batch
        if batch:
            rows, batch = batch, []
            await write_paid(rows)

    try:
        chunk_size = pypi.POOL_SIZE
        for start_at in range(0, len(tasks), chunk_size):
            chunk = list(enumerate(tasks))[start_at:start_at + chunk_size]
            prepared = []
            for index, entry in chunk:
                target, why = pypi.parse_target(entry)
                if target is None:
                    counts["invalid"] += 1
                    await free_status(entry, index, ST_INVALID, why)
                    continue
                if target.key in sold_keys:
                    counts["duplicate"] += 1
                    await free_status(entry, index, ST_DUPLICATE,
                                      "the same PyPI project was already sold in this run (a version selector does not make it a second project)")
                    continue
                if can_charge is None:
                    stopping = True
                    stop_reason = stop_reason or "billing-price-unknown"
                    counts["cutByStop"] += 1
                    await free_status(entry, index, ST_BILLING,
                                      f"not read and not charged: {price_note}")
                    continue
                if stopping:
                    counts["cutByStop"] += 1
                    status = ST_CAP if stop_reason in ("spending-cap", "charge-error",
                                                       "billing-price-unknown") else ST_ERROR
                    await free_status(entry, index, status,
                                      f"the run stopped ({stop_reason or 'earlier error'}) before "
                                      "this package: not read")
                    continue
                if counts["read"] >= max_packages:
                    counts["cutByLimit"] += 1
                    await free_status(entry, index, ST_LIMIT,
                                      f"the run reads at most {max_packages} packages (maxPackages): "
                                      "this line was cut before any request and is not charged")
                    continue
                left = time_left(deadline)
                if left is not None and left < DEADLINE_MIN_SECONDS:
                    counts["cutByDeadline"] += 1
                    stop_reason = stop_reason or "deadline"
                    first_failure = first_failure or (
                        f"the run ran out of time with {int(left)} s left")
                    await free_status(entry, index, ST_TIME,
                                      f"the run had {int(left)} s left: not read")
                    continue
                counts["read"] += 1
                prepared.append((index, entry, target))
            if not prepared:
                continue
            if start_at:
                await asyncio.sleep(pypi.POLITE_SECONDS)
            found, refused, missing = pypi.read_packages(
                client, [item for _, _, item in prepared],
                with_dependencies=with_dependencies,
                timeout=request_budget(time_left(deadline), len(prepared)),
            )
            for index, entry, target in prepared:
                if target.request in refused:
                    # One PyPI request carries one name, so a failure here is a failed read
                    # of this line only and the packages beside it are still sold. Keyed by
                    # the string that was sent, never by the canonical key: a key here would
                    # turn a refused name into a free "no such project" verdict.
                    counts["failed"] += 1
                    counts["refused"] += 1
                    reason = refused[target.request]
                    first_failure = first_failure or reason
                    await free_status(entry, index, ST_ERROR,
                                      f"{reason}: this package was not read and is not "
                                      "charged, send it again later; the packages beside it "
                                      "in this run were read")
                    continue
                if target.request in missing:
                    # A version PyPI never published is a verdict about that line, not a
                    # failure: it is free, and it must not settle the project, or the honest
                    # line for the real version in this same batch would be eaten.
                    counts["notFound"] += 1
                    await free_status(entry, index, ST_NOT_FOUND, missing[target.request])
                    continue
                row = found.get(target.request)
                if row is None:
                    counts["notFound"] += 1
                    await free_status(entry, index, ST_NOT_FOUND,
                                      f'PyPI has no project behind "{str(entry).strip()[:80]}": '
                                      "not charged")
                    continue
                if target.key in sold_keys:
                    # Same project, second line, same batch: one project is one charge.
                    counts["duplicate"] += 1
                    await free_status(entry, index, ST_DUPLICATE,
                                      "the same PyPI project was already sold earlier in this "
                                      "run (a version selector does not make it a second project)")
                    continue
                sold_keys.add(target.key)
                row["actorIndex"] = index
                row["actorInput"] = entry
                row["source"] = SOURCE
                batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    await flush()
        await flush()
    except Exception as exc:  # noqa: BLE001
        stopping = True
        stop_reason = stop_reason or "unexpected-error"
        first_failure = first_failure or (
            f"the run stopped on {type(exc).__name__}: {exc}")
        dataset_note = dataset_note or (
            "the run stopped on an unexpected error: the last batch is written before the "
            "summary, check the dataset before re-running")
        # Rows that were already read are paid for in PyPI's time, so the batch is
        # written instead of thrown away by the exception.
        pending = len(batch)
        try:
            await flush()
        except Exception as inner:  # noqa: BLE001
            write_errors += 1
            delivered_unverified += pending
            first_failure = first_failure or (
                f"the last batch could not be written ({type(inner).__name__})")
    await flush_free()

    requested = len(tasks)
    summary = {
        "requested": requested,
        "attempted": counts["read"],
        "delivered": counts["delivered"],
        "rowsWritten": rows_written,
        "chargedEvents": charged_total,
        "deliveredUncharged": delivered_uncharged,
        "deliveredUnverified": delivered_unverified,
        "chargeUncertain": charge_uncertain,
        "writeErrors": write_errors,
        "cappedNotCharged": counts["capped"],
        "packagesNotFound": counts["notFound"],
        "packagesRefused": counts["refused"],
        "packagesInvalid": counts["invalid"],
        "packagesDuplicate": counts["duplicate"],
        "cutByDeadline": counts["cutByDeadline"],
        "cutByStop": counts["cutByStop"],
        "cutByLimit": counts["cutByLimit"],
        "byStatus": by_status,
        "counterMismatch": rows_written != requested,
        "stopReason": stop_reason,
        "datasetNote": dataset_note,
        "firstFailure": first_failure,
        "billingActive": can_charge,
        "priceNote": price_note,
        "eventName": EVENT_NAME,
        "maxPackages": max_packages,
        "includeDependencies": with_dependencies,
        "source": SOURCE,
        "startedAt": started,
        "finishedAt": now_iso(),
        "runId": _run_id(),
    }

    try:
        store = await Actor.open_key_value_store()
        await store.set_value("RUN_SUMMARY", summary)
        run_id = _run_id()
        if run_id:
            named = await Actor.open_key_value_store(name="pypi-package-summary")
            await named.set_value(f"RUN_SUMMARY-{run_id}", summary)
    except Exception as exc:  # noqa: BLE001
        Actor.log.warning("the run summary could not be stored: %s", type(exc).__name__)

    await Actor.set_status_message(
        f"{counts['delivered']} package(s) delivered, {charged_total} charged, "
        f"{max(0, rows_written - counts['delivered'])} free status row(s)"
        + (f", stopped: {stop_reason}" if stop_reason else ""))

    if (counts["delivered"] == 0 and stop_reason not in MONEY_STOPS
            and (counts["failed"] > 0 or stop_reason == "unexpected-error")):
        # Refused only when every attempt that reached PyPI failed: a name PyPI never saw is a
        # verdict and comes back as a free NOT_FOUND row, so it never counts as a failed read,
        # and a run that stopped on money or on the dataset is not a source
        # problem and must not be called one.
        detail = f"First reason: {first_failure or 'not recorded'}."
        if stop_reason:
            detail += f" The run stopped on: {stop_reason}."
        if price_note:
            detail += f" Pricing: {price_note}."
        if counts["refused"]:
            # The source answered, so the network is not the suspect: this is about the ids.
            head = (f"PyPI refused all {counts['refused']} package(s) in this run and nothing "
                    "was charged. ")
            tail = (" PyPI answered with a server error or a body that is not a project: that "
                    "points at the source rather than at your names, so send them again later. "
                    "A name PyPI does not have comes back as a free NOT_FOUND row instead.")
        elif counts["failed"]:
            head = (f"None of the {counts['failed']} package(s) that reached PyPI could be "
                    "read and nothing was charged. ")
            tail = (" Check that pypi.org is reachable before re-running; a name PyPI "
                    "never saw comes back as a free NOT_FOUND row, so this message is about "
                    "the source, not about the ids.")
        else:
            head = ("The run stopped on an unexpected error before a single package was delivered, "
                    "so nothing was charged. ")
            tail = " Check the dataset before re-running."
        await Actor.fail(status_message=(head + detail + tail))
        client.close()
        return

    client.close()
    await Actor.exit()


if __name__ == "__main__":
    asyncio.run(main())
