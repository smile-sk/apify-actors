"""arXiv Paper API: one paid row per paper that exists on arXiv.

Money rules the park learned the hard way: a paid row is written and charged in ONE call,
the batch is cut by the remaining budget before the write, and an exception from that call
means "charge failed after the write", "the dataset refused the write" or "nobody knows" -
only the dataset counter tells them apart, and when it cannot be read the answer is
"unknown", never "nothing was delivered". An id with no paper behind it is a verdict and
comes back FREE with a reason; an unreachable arXiv is not a verdict about the paper.

The API is keyless and asks for politeness: ids go in chunks with a pause between calls,
because one call per id would spend the whole run on sleeping.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import httpx
from apify import Actor

from . import arxiv

ST_OK = "OK"
ST_INVALID = "INVALID_INPUT"
ST_DUPLICATE = "DUPLICATE"
ST_NOT_FOUND = "NOT_FOUND"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"

EVENT_NAME = "paper-delivered"
SOURCE = "arXiv Atom API (keyless)"
BATCH_SIZE = 10
FREE_BATCH_SIZE = 100
CALL_COST_SECONDS = 3.0
DEADLINE_MARGIN_SECONDS = 15.0
DEADLINE_MIN_SECONDS = 8.0
MAX_PAPERS = 500
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


def request_budget(left: float | None) -> float | None:
    """How long one arXiv request may take, from the seconds the run has left.

    One chunk of ids costs one request plus the polite pause before the next one, so the
    request gets the remainder minus both. Giving it the whole remainder meant the run
    overran the platform deadline and was killed after rows were already charged, with no
    summary written.
    """
    if left is None:
        # In httpx timeout=None means "no timeout at all", so a silent socket hung a local run
        # for 180 s. No deadline is a local run, and even there a request has to give up.
        return arxiv.TIMEOUT
    return max(2.0, min(arxiv.TIMEOUT, left - DEADLINE_MARGIN_SECONDS - arxiv.POLITE_SECONDS))


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
    raw = data.get("papers")
    if isinstance(raw, str):
        wanted = [raw]
    elif isinstance(raw, list):
        wanted = [str(item) for item in raw]
    else:
        wanted = []
    tasks = [str(item) for item in wanted]
    if not any(item.strip() for item in tasks):
        await Actor.fail(status_message=(
            "No papers were sent. Pass arXiv ids or links in the papers field, for "
            "example {\"papers\": [\"1706.03762\", \"https://arxiv.org/abs/2103.00020\"]}."))
        return

    max_papers = _limit(data.get("maxPapers"), 100, 1, MAX_PAPERS)
    wanted_abstract = data.get("includeAbstract")
    with_abstract = True if wanted_abstract is None else bool(wanted_abstract)

    deadline = platform_deadline()
    if deadline is not None:
        left = time_left(deadline) or 0.0
        if left < DEADLINE_MIN_SECONDS:
            await Actor.fail(status_message=(
                f"The run has {int(left)} s left before the platform stops it, which is less "
                "than one paper needs. Send fewer papers or raise the timeout."))
            return

    charging = Actor.get_charging_manager()
    can_charge, price_note = price_state(charging)
    billable_claim = None if can_charge is None else bool(can_charge)

    dataset = await Actor.open_dataset()
    baseline = await _item_count(dataset)
    client = httpx.Client(timeout=httpx.Timeout(arxiv.TIMEOUT), follow_redirects=True)

    counts = {"read": 0, "failed": 0, "notFound": 0, "invalid": 0, "duplicate": 0,
              "cutByDeadline": 0, "cutByStop": 0, "cutByLimit": 0, "delivered": 0,
              "capped": 0, "refused": 0}
    by_status: dict = {}
    seen_inputs: set = set()
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
                                  "the remaining budget ended before this paper: not delivered "
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
        chunk_size = arxiv.MAX_IDS_PER_CALL
        for start_at in range(0, len(tasks), chunk_size):
            chunk = list(enumerate(tasks))[start_at:start_at + chunk_size]
            prepared = []
            for index, entry in chunk:
                paper, why = arxiv.parse_paper(entry)
                if paper is None:
                    counts["invalid"] += 1
                    await free_status(entry, index, ST_INVALID, why)
                    continue
                if paper.key in seen_inputs:
                    counts["duplicate"] += 1
                    await free_status(entry, index, ST_DUPLICATE,
                                      "the same arXiv id was already sent in this run")
                    continue
                seen_inputs.add(paper.key)
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
                                      "this paper: not read")
                    continue
                if counts["read"] >= max_papers:
                    counts["cutByLimit"] += 1
                    await free_status(entry, index, ST_CAP,
                                      f"the run reads at most {max_papers} papers (maxPapers): "
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
                prepared.append((index, entry, paper))
            if not prepared:
                continue
            if start_at:
                await asyncio.sleep(arxiv.POLITE_SECONDS)
            found, error, refused = arxiv.read_papers(
                client, [item.request for _, _, item in prepared],
                with_abstract=with_abstract,
                timeout=request_budget(time_left(deadline)),
            )
            for index, entry, paper in prepared:
                if paper.request in refused:
                    # arXiv answers 500 for the whole id_list when one id in it is bad. The
                    # chunk was split until the offender stood alone, so only this line is a
                    # failed read and the papers beside it are still sold. Keyed by the string
                    # that was sent, never by the canonical key: a key here once turned a
                    # refused id into a free "no such paper" verdict for a paper that exists.
                    counts["failed"] += 1
                    counts["refused"] += 1
                    reason = refused[paper.request]
                    first_failure = first_failure or reason
                    await free_status(entry, index, ST_ERROR,
                                      f"{reason}: this paper was not read and is not charged, "
                                      "send it again later; the other papers of its block "
                                      "were read")
                    continue
                row = found.get(paper.key)
                if row is None and error is not None:
                    counts["failed"] += 1
                    first_failure = first_failure or error
                    await free_status(entry, index, ST_ERROR, error)
                    continue
                if row is None:
                    counts["notFound"] += 1
                    await free_status(entry, index, ST_NOT_FOUND,
                                      f"arXiv has no paper with the id {paper.key}: not charged")
                    continue
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
        # Rows that were already read are paid for in arXiv's time, so the batch is
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
        "papersNotFound": counts["notFound"],
        "papersRefused": counts["refused"],
        "papersInvalid": counts["invalid"],
        "papersDuplicate": counts["duplicate"],
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
        "maxPapers": max_papers,
        "includeAbstract": with_abstract,
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
            named = await Actor.open_key_value_store(name="arxiv-paper-summary")
            await named.set_value(f"RUN_SUMMARY-{run_id}", summary)
    except Exception as exc:  # noqa: BLE001
        Actor.log.warning("the run summary could not be stored: %s", type(exc).__name__)

    await Actor.set_status_message(
        f"{counts['delivered']} paper(s) delivered, {charged_total} charged, "
        f"{max(0, rows_written - counts['delivered'])} free status row(s)"
        + (f", stopped: {stop_reason}" if stop_reason else ""))

    if (counts["delivered"] == 0 and stop_reason not in MONEY_STOPS
            and (counts["failed"] > 0 or stop_reason == "unexpected-error")):
        # Refused only when every attempt that reached arXiv failed: an id arXiv never saw is a
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
            head = (f"arXiv refused all {counts['refused']} paper(s) in this run and nothing "
                    "was charged. ")
            tail = (" The source answered, so this is about these ids rather than the network: "
                    "send them again later, and a paper arXiv does not have comes back as a "
                    "free NOT_FOUND row instead.")
        elif counts["failed"]:
            head = (f"None of the {counts['failed']} paper(s) that reached arXiv could be "
                    "read and nothing was charged. ")
            tail = (" Check that export.arxiv.org is reachable before re-running; an id arXiv "
                    "never saw comes back as a free NOT_FOUND row, so this message is about "
                    "the source, not about the ids.")
        else:
            head = ("The run stopped on an unexpected error before a single paper was delivered, "
                    "so nothing was charged. ")
            tail = " Check the dataset before re-running."
        await Actor.fail(status_message=(head + detail + tail))
        client.close()
        return

    client.close()
    await Actor.exit()


if __name__ == "__main__":
    asyncio.run(main())
