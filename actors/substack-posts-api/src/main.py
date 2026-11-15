"""Substack Publication Posts API: one paid row per post, read from the public
keyless archive endpoint of a Substack publication.

Money rules, the same ones the rest of this park learned the hard way:
  * A paid row is written and charged in ONE call, Actor.push_data(rows,
    charged_event_name=...). A separate charge before the write leaves a row the buyer
    paid for and cannot download.
  * The batch is cut by the remaining budget BEFORE the write: the SDK silently writes
    and charges only the prefix that fits, so counters built from the batch size would
    report sales that never happened.
  * An exception from that call can mean two different things: the charge failed after
    the rows landed, or the dataset refused the write and nothing landed. Only the
    dataset counter tells them apart, and when it cannot be read the answer is
    "unknown", never "nothing was delivered".
  * A publication the source does not know, or a host that does not resolve, comes back
    free with a status and a plain reason, and carries no field a buyer could use as
    data. A publication that simply published nothing is a green run, not a failure.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import httpx
from apify import Actor

from . import substack

ST_OK = "OK"
ST_INVALID = "INVALID_INPUT"
ST_DUPLICATE = "DUPLICATE"
ST_NOT_FOUND = "NOT_FOUND"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_EMPTY = "EMPTY"

EVENT_NAME = "post-delivered"
SOURCE = "Substack public archive API (keyless)"
BATCH_SIZE = 10
CALL_COST_SECONDS = 1.5
DEADLINE_MARGIN_SECONDS = 15.0
DEFAULT_MAX_POSTS = 50
MAX_POSTS_CEILING = 500


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


def price_state(charging) -> tuple[bool | None, str]:
    """Can this run charge? True / False / None (unknown), and a plain reason.

    get_pricing_info() is synchronous in the SDK: awaiting it silently zeroes the money
    path of the whole run.
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

    get_metadata() answers a pydantic DatasetMetadata with item_count on it: reading it as
    a dict silently gives None and hides a refused write.
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

    The counter is the only proof of delivery, and it lags behind a write: a single read
    taken right after a refused write is indistinguishable from a counter that is simply
    one write behind. One read therefore cannot tell "the write was refused" from "the
    charge failed and the counter has not caught up", and the wrong answer costs the buyer
    a second run and a second charge. The maximum of a few reads is the honest number.
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
    raw = data.get("publications")
    if isinstance(raw, str):
        wanted = [raw]
    elif isinstance(raw, list):
        wanted = [str(item) for item in raw]
    else:
        wanted = []
    tasks = [str(item).strip() for item in wanted]
    if not any(tasks):
        await Actor.fail(status_message=(
            "No publications were sent. Pass Substack publications in the publications "
            "field, for example {\"publications\": [\"www.thefp.com\", \"platformer\"]}."))
        return

    max_posts = _limit(data.get("maxPostsPerPublication"), DEFAULT_MAX_POSTS, 1,
                       MAX_POSTS_CEILING)
    include_body = bool(data.get("includeBodyText", False))

    deadline = platform_deadline()
    if deadline is not None:
        left = time_left(deadline) or 0.0
        if left < DEADLINE_MARGIN_SECONDS + CALL_COST_SECONDS:
            await Actor.fail(status_message=(
                f"The run has {int(left)} s left before the platform stops it, which is "
                "less than one publication needs. Send fewer publications or raise the "
                "timeout."))
            return

    charging = Actor.get_charging_manager()
    can_charge, price_note = price_state(charging)
    billable_claim = None if can_charge is None else bool(can_charge)

    dataset = await Actor.open_dataset()
    baseline = await _item_count(dataset)
    client = httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=False)

    counts = {"read": 0, "failed": 0, "notFound": 0, "invalid": 0, "duplicate": 0,
              "pubsRead": 0, "cutByBudget": 0, "cutByDeadline": 0, "cutByStop": 0,
              "delivered": 0, "postsSeen": 0, "capped": 0, "empty": 0}
    by_status: dict = {}
    seen_inputs: set = set()
    seen_posts: set = set()
    seen_hosts: set = set()
    batch: list = []
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

    async def free_status(value: str, index: int, status: str, reason: str) -> None:
        nonlocal rows_written, write_errors, first_failure
        row = status_row(value, index, status, reason)
        try:
            await Actor.push_data([row])
        except Exception as exc:  # noqa: BLE001
            write_errors += 1
            first_failure = first_failure or (
                "a status row could not be written (%s)" % type(exc).__name__)
            return
        rows_written += 1
        note(status)

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
            # The platform says this actor is not sold per event: nothing can be charged,
            # so the rows are delivered free and the summary says so instead of pretending
            # a sale that the price list cannot support.
            for row in rows:
                row["billable"] = False
            before_free = None if baseline is None else baseline + rows_written
            try:
                await Actor.push_data(rows)
            except Exception as exc:  # noqa: BLE001
                after = await _item_count_after(dataset)
                grew = None if before_free is None or after is None else after - before_free
                write_errors += 1
                delivered_unverified += len(rows)
                stopping = True
                stop_reason = "dataset-write-error"
                first_failure = first_failure or ("a write raised %s" % type(exc).__name__)
                grew_text = ("" if grew is None
                             else f" and the dataset counter grew by {grew} of {len(rows)}")
                dataset_note = (f"a free write raised {type(exc).__name__}{grew_text}: "
                                "delivery is unverified, check the dataset before re-running")
                return
            counts["delivered"] += len(rows)
            rows_written += len(rows)
            delivered_uncharged += len(rows)
            note(ST_OK, len(rows))
            dataset_note = dataset_note or (
                f"the rows are delivered and nothing is charged: {price_note}")
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
                                  "the remaining budget ended before this post: not delivered "
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
                dataset_note = ("a charge failed: the rows stay in the dataset and are "
                                "counted in chargeUncertain, no second row is written")
            else:
                write_errors += 1
                delivered_unverified += len(pay)
                charge_uncertain += len(pay)
                stopping = True
                stop_reason = "dataset-write-error"
                first_failure = first_failure or ("a paid write raised %s" % type(exc).__name__)
                if grew is None:
                    dataset_note = (
                        f"a paid write raised {type(exc).__name__} and the dataset count "
                        "could not be read: delivery and the charge are both unknown, the "
                        "batch is counted in deliveredUnverified and chargeUncertain, check "
                        "the dataset before re-running")
                elif grew == 0:
                    dataset_note = (
                        f"a paid write raised {type(exc).__name__} and the dataset counter "
                        "did not grow: either the write was refused or the counter lags, the "
                        "charge is not confirmed. The batch is counted in deliveredUnverified "
                        "and chargeUncertain, check the dataset before re-running")
                else:
                    dataset_note = (
                        f"a paid write raised {type(exc).__name__} and the dataset counter "
                        f"grew by {grew} of {len(pay)}: delivery is partial and the charge is "
                        "not confirmed. The batch is counted in deliveredUnverified and "
                        "chargeUncertain, check the dataset before re-running")
            return
        charged = getattr(result, "charged_count", None)
        if charged is None:
            delivered_uncharged += len(pay)
            counts["delivered"] += len(pay)
            charge_uncertain += len(pay)
            stopping = True
            stop_reason = "charge-error"
            dataset_note = ("the charge result did not report a count: the rows are "
                            "delivered and not claimed as sold")
            rows_written += len(pay)
            note(ST_OK, len(pay))
            return
        charged = int(charged)
        if charged < len(pay):
            stopping = True
            stop_reason = stop_reason or "charge-error"
            dataset_note = (f"the platform charged {charged} of {len(pay)} rows in this batch: "
                            "the charge stopped short, the run stops here")
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
        for index, entry in enumerate(tasks):
            name, host = substack.as_publication(entry)
            if not host:
                counts["invalid"] += 1
                await free_status(entry, index, ST_INVALID,
                                  "not a Substack publication: send a host such as "
                                  "www.thefp.com or a bare subdomain such as platformer")
                continue
            if host in seen_inputs:
                counts["duplicate"] += 1
                await free_status(entry, index, ST_DUPLICATE,
                                  "the same publication was already sent in this run")
                continue
            seen_inputs.add(host)

            if stopping:
                if stop_reason in ("spending-cap", "charge-error", "billing-price-unknown"):
                    counts["cutByBudget"] += 1
                    await free_status(entry, index, ST_CAP,
                                      f"the run stopped on money ({stop_reason}) before this "
                                      "publication: not read")
                else:
                    counts["cutByStop"] += 1
                    await free_status(entry, index, ST_ERROR,
                                      f"the run stopped ({stop_reason or 'earlier error'}) before "
                                      "this publication: not read")
                continue

            left = time_left(deadline)
            if left is not None and left < CALL_COST_SECONDS * 2:
                counts["cutByDeadline"] += 1
                stop_reason = stop_reason or "deadline"
                first_failure = first_failure or (
                    f"the run had {int(left)} s left before {host}: not read")
                await free_status(entry, index, ST_TIME,
                                  f"the run had {int(left)} s left: not read")
                continue

            per_call = None
            if deadline is not None:
                left_now = time_left(deadline) or 0.0
                per_call = max(3.0, min(20.0, left_now - 5.0))
            collected = 0
            cursor = None
            pages = 1
            failed_kind, failed_reason = "", ""
            posts_before = counts["postsSeen"]
            current, posts, cursor, kind, reason = substack.open_publication(
                client, host, min(substack.PAGE_SIZE, max_posts), timeout=per_call)
            timed_out = False
            if not kind:
                if current in seen_hosts:
                    counts["duplicate"] += 1
                    await free_status(entry, index, ST_DUPLICATE,
                                      "the same publication was already read in this run as "
                                      + current + ": nothing was read and nothing was charged")
                    continue
                seen_hosts.add(current)
            while True:
                if kind:
                    failed_kind, failed_reason = kind, reason
                    break
                if not posts:
                    break
                for post in posts:
                    counts["postsSeen"] += 1
                    # A Substack post id is unique across the platform, so one id under two
                    # publications of the same run is the same post: it is delivered once.
                    key = str(post.get("id"))
                    if key in seen_posts:
                        counts["duplicate"] += 1
                        await free_status(entry, index, ST_DUPLICATE,
                                          f"post {post.get('id')} was already delivered in this run")
                        continue
                    seen_posts.add(key)
                    row = substack.post_row(post, name, include_body, current)
                    if row is None:
                        counts["invalid"] += 1
                        await free_status(entry, index, ST_INVALID,
                                          "this post carried no id, so it was not sold")
                        continue
                    row["actorInput"] = entry
                    row["actorIndex"] = index
                    batch.append(row)
                    counts["read"] += 1
                    collected += 1
                    if len(batch) >= BATCH_SIZE:
                        await flush()
                    if stopping or collected >= max_posts:
                        break
                if stopping or not cursor or collected >= max_posts:
                    break
                if pages >= 20:
                    break
                left = time_left(deadline)
                if left is not None and left < CALL_COST_SECONDS * 2:
                    timed_out = True
                    counts["cutByDeadline"] += 1
                    stop_reason = stop_reason or "deadline"
                    first_failure = first_failure or (
                        f"the run had {int(left)} s left before page {pages + 1} of {current}")
                    await free_status(entry, index, ST_TIME,
                                      f"the run had {int(left)} s left: the next page was not read, "
                                      "nothing was charged for it")
                    break
                if left is not None:
                    per_call = max(2.0, min(20.0, left - 5.0))
                want = min(substack.PAGE_SIZE, max_posts - collected)
                posts, cursor, kind, reason = substack.fetch_page(client, current, want, cursor,
                                                                  timeout=per_call)
                pages += 1
            if not failed_kind:
                counts["pubsRead"] += 1
                if counts["postsSeen"] == posts_before and not stopping and not timed_out:
                    counts["empty"] += 1
                    await free_status(entry, index, ST_EMPTY,
                                      f"the archive of {current} answered normally and holds no "
                                      "post: nothing was sold and nothing was charged")
            if failed_kind:
                counts["failed"] += 1
                first_failure = first_failure or failed_reason
                if failed_kind == "not_found":
                    counts["notFound"] += 1
                    await free_status(entry, index, ST_NOT_FOUND, failed_reason)
                elif failed_kind == "invalid":
                    counts["invalid"] += 1
                    await free_status(entry, index, ST_INVALID, failed_reason)
                else:
                    await free_status(entry, index, ST_ERROR,
                                      f"not read: {failed_reason}")

    except Exception as exc:  # noqa: BLE001
        stop_reason = stop_reason or "unexpected-error"
        first_failure = first_failure or (
            "the run hit an unexpected error: %s: %s" % (type(exc).__name__, exc))

    await flush()

    requested = len(tasks)
    accounted = sum(by_status.values())
    summary = {
        "startedAt": started,
        "finishedAt": now_iso(),
        "source": SOURCE,
        "requested": requested,
        "publicationsRead": counts["pubsRead"],
        "publicationsFailed": max(0, counts["failed"] - counts["notFound"]),
        "publicationsNotFound": counts["notFound"],
        "publicationsEmpty": counts["empty"],
        "postsSeen": counts["postsSeen"],
        "postsDelivered": counts["delivered"],
        "rowsWritten": rows_written,
        "chargedEvents": charged_total,
        "deliveredUncharged": delivered_uncharged,
        "deliveredUnverified": delivered_unverified,
        "chargeUncertain": charge_uncertain,
        "writeErrors": write_errors,
        "byStatus": dict(sorted(by_status.items())),
        "counterMismatch": (accounted != rows_written)
                            or (counts["delivered"] != by_status.get(ST_OK, 0)),
        "billingActive": billable_claim is True,
        "stopReason": stop_reason,
        "datasetNote": dataset_note,
        "firstFailure": first_failure,
    }
    try:
        store = await Actor.open_key_value_store()
        run_id = str(_run_id() or "")
        if run_id:
            await store.set_value("RUN_SUMMARY-" + run_id, summary)
        await store.set_value("RUN_SUMMARY", summary)
    except Exception:  # noqa: BLE001
        pass
    try:
        # The default store is where a buyer looks first: the bare key is written through
        # the Actor so that a run whose custom store failed still leaves the summary.
        await Actor.set_value("RUN_SUMMARY", summary)
    except Exception:  # noqa: BLE001
        pass

    if counts["delivered"]:
        free_rows = max(0, rows_written - counts["delivered"])
        message = (f"{counts['delivered']} post(s) delivered, {charged_total} charged, "
                   f"{free_rows} free status row(s)")
        if stopping:
            message += f", stopped: {stop_reason}"
        if dataset_note:
            message += f" | {dataset_note}"
        await Actor.set_status_message(message)
    elif counts["failed"] and not counts["pubsRead"] and not counts["notFound"]:
        await Actor.fail(status_message=(
            f"None of the {requested} publication(s) could be read: {counts['failed']} "
            f"refusal(s). First reason: {first_failure or 'not recorded'}. Check the "
            "publication host before re-running."))
        return
    else:
        free_rows = max(0, rows_written - counts["delivered"])
        await Actor.set_status_message(
            f"0 post(s) delivered, 0 charged, {free_rows} free status row(s)")

    client.close()
    await Actor.exit()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
