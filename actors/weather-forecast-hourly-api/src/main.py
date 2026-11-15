"""Weather Forecast API: one row per place, read from MET Norway Locationforecast 2.0
(CC-BY 4.0: commercial use allowed, attribution given in the README and in every summary).

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
  * Anything that is not a place, or a place the source did not answer for, comes back
    free with a status and a plain reason, and carries no field a buyer could use as
    data.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import httpx
from apify import Actor

from . import weather

ST_OK = "OK"
ST_INVALID = "INVALID_INPUT"
ST_DUPLICATE = "DUPLICATE"
ST_NOT_FOUND = "NOT_FOUND"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"

EVENT_NAME = "location-delivered"
BATCH_SIZE = 10
CALL_COST_SECONDS = 2.5
DEADLINE_MARGIN_SECONDS = 15.0
DEFAULT_MAX_LOCATIONS = 50
MAX_LOCATIONS_CEILING = 500


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
    raw_places = data.get("locations")
    if isinstance(raw_places, str):
        wanted = [raw_places]
    elif isinstance(raw_places, list):
        wanted = [str(item) for item in raw_places]
    else:
        wanted = []
    tasks = [str(item).strip() for item in wanted]
    if not any(tasks):
        await Actor.fail(status_message=(
            "No places were sent. Pass coordinates in the locations field, for example "
            "{\"locations\": [\"50.45,30.52\", \"48.85,2.35\"]}."))
        return

    units = str(data.get("units") or "metric").strip().lower()
    if units not in ("metric", "imperial"):
        units = "metric"
    include_daily = bool(data.get("includeDaily", True))
    hours = _limit(data.get("hours"), 24, 1, 72)
    max_locations = _limit(data.get("maxLocations"), DEFAULT_MAX_LOCATIONS, 1,
                           MAX_LOCATIONS_CEILING)
    if len(tasks) > max_locations:
        cut = tasks[max_locations:]
        tasks = tasks[:max_locations]
    else:
        cut = []

    deadline = platform_deadline()
    if deadline is not None:
        left = time_left(deadline) or 0.0
        if left < DEADLINE_MARGIN_SECONDS + CALL_COST_SECONDS:
            await Actor.fail(status_message=(
                f"The run has {int(left)} s left before the platform stops it, which is "
                "less than one place needs. Send fewer places or raise the timeout."))
            return

    charging = Actor.get_charging_manager()
    can_charge, price_note = price_state(charging)
    billable_claim = None if can_charge is None else bool(can_charge)

    dataset = await Actor.open_dataset()
    # The baseline is read once, before any write: taken before every batch it would lag
    # behind and inflate the growth by a whole batch.
    baseline = await _item_count(dataset)
    client = httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=False)

    counts = {"attempted": 0, "read": 0, "notFound": 0, "invalid": 0, "duplicate": 0,
              "failed": 0, "limited": len(cut), "cutByBudget": 0, "cutByDeadline": 0,
              "cutByStop": 0, "delivered": 0, "capped": 0}
    by_status: dict = {}
    seen_inputs: set = set()
    seen_points: set = set()
    batch: list = []
    rows_written = 0
    charged_total = 0
    delivered_uncharged = 0
    delivered_unverified = 0
    charge_uncertain = 0
    write_errors = 0
    summary_errors = 0
    stopping = False
    stop_reason = ""
    dataset_note = ""
    first_failure = ""

    def note(status: str, amount: int = 1) -> None:
        by_status[status] = by_status.get(status, 0) + amount

    def status_row(value: str, index: int, status: str, reason: str) -> dict:
        """A row that carries a reason and nothing a buyer could use as data."""
        return {"actorInput": value, "actorIndex": index, "latitude": None,
                "longitude": None, "units": None, "daily": None, "hourly": None,
                "status": status, "reason": reason, "billable": False}

    async def free_status(value: str, index: int, status: str, reason: str) -> None:
        nonlocal rows_written, write_errors, stopping, stop_reason, dataset_note
        row = status_row(value, index, status, reason)
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

    async def write_paid(rows: list) -> None:
        """Write and charge one batch; any doubt about money stops the run."""
        nonlocal rows_written, charged_total, delivered_uncharged, charge_uncertain
        nonlocal stopping, stop_reason, dataset_note, write_errors, delivered_unverified
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
            rows_written += len(rows)
            counts["delivered"] += len(rows)
            delivered_uncharged += len(rows)
            note(ST_OK, len(rows))
            return
        if can_charge is None:
            stopping = True
            stop_reason = stop_reason or "billing-price-unknown"
            for row in rows:
                await free_status(row.get("actorInput") or "",
                                  int(row.get("actorIndex") or 0), ST_BILLING,
                                  f"not delivered and not charged: {price_note}")
            return
        if charging.is_event_charge_limit_reached(EVENT_NAME):
            stopping = True
            stop_reason = stop_reason or "spending-cap"
            for row in rows:
                counts["capped"] += 1
                await free_status(row.get("actorInput") or "",
                                  int(row.get("actorIndex") or 0), ST_CAP,
                                  "the spending cap was reached: not delivered and not "
                                  "charged")
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
                await free_status(row.get("actorInput") or "",
                                  int(row.get("actorIndex") or 0), ST_CAP,
                                  "the remaining budget could not be computed: not "
                                  "delivered and not charged")
            return
        pay, over = rows[:max(0, budget)], rows[max(0, budget):]
        if over:
            stopping = True
            stop_reason = stop_reason or "spending-cap"
            for row in over:
                counts["capped"] += 1
                await free_status(row.get("actorInput") or "",
                                  int(row.get("actorIndex") or 0), ST_CAP,
                                  "the remaining budget ended before this place: not "
                                  "delivered and not charged")
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
                if grew is None:
                    # The counter is the only thing that separates a refused write from a
                    # failed charge: when it cannot be read, the money is unknown too, and
                    # chargeUncertain was already raised for the whole batch above.
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
            dataset_note = (f"the platform charged {charged} of {len(pay)} rows in this "
                            "batch")
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

    for index, raw in enumerate(tasks):
        point = None
        if raw in seen_inputs:
            counts["duplicate"] += 1
            await free_status(raw, index, ST_DUPLICATE,
                              "the same place was already sent in this run")
            continue
        seen_inputs.add(raw)

        if stopping:
            if stop_reason in ("spending-cap", "charge-error", "billing-price-unknown"):
                counts["cutByBudget"] += 1
                await free_status(raw, index, ST_CAP,
                                  f"the run stopped on money ({stop_reason}) before this "
                                  "place: not read")
            else:
                counts["cutByStop"] += 1
                await free_status(raw, index, ST_ERROR,
                                  f"the run stopped ({stop_reason or 'earlier error'}) "
                                  "before this place: not read")
            continue
        left = time_left(deadline)
        if left is not None and left < CALL_COST_SECONDS * 2:
            counts["cutByDeadline"] += 1
            stop_reason = stop_reason or "deadline"
            await free_status(raw, index, ST_TIME,
                              f"the run had {int(left)} s left: not read")
            continue

        budget_seconds = None if left is None else max(2.0, left - DEADLINE_MARGIN_SECONDS)
        point = weather.coordinates(raw)
        if point is None:
            counts["invalid"] += 1
            reason_why = ("the place is empty" if not raw else
                          "not a coordinate pair: send latitude,longitude such as "
                          "50.45,30.52")
            await free_status(raw, index, ST_INVALID, reason_why)
            continue
        counts["attempted"] += 1
        where = {"latitude": point[0], "longitude": point[1]}
        key = (round(where["latitude"], 3), round(where["longitude"], 3))
        if key in seen_points:
            counts["duplicate"] += 1
            await free_status(raw, index, ST_DUPLICATE,
                              "the same point was already delivered in this run")
            continue

        left = time_left(deadline)
        budget_seconds = None if left is None else max(2.0, left - DEADLINE_MARGIN_SECONDS)
        payload, why = weather.forecast(where["latitude"], where["longitude"], client,
                                        timeout=budget_seconds)
        if payload is None:
            if not first_failure:
                first_failure = why
            counts["failed"] += 1
            await free_status(raw, index, ST_ERROR, why)
            continue

        seen_points.add(key)
        counts["read"] += 1
        batch.append(weather.row(where, payload, raw=raw, index=index, units=units,
                                 hours=hours, with_daily=include_daily))
        if len(batch) >= BATCH_SIZE:
            await flush()

    await flush()
    client.close()

    for start_at in range(0, len(cut), 100):
        chunk = cut[start_at:start_at + 100]
        tail_rows = [status_row(raw, len(tasks) + start_at + offset, ST_LIMIT,
                                f"maxLocations is {max_locations}: this place was not "
                                "attempted")
                     for offset, raw in enumerate(chunk)]
        try:
            await dataset.push_data(tail_rows)
        except Exception:  # noqa: BLE001
            write_errors += 1
            stopping = True
            stop_reason = stop_reason or "dataset-write-error"
            dataset_note = "the tail past maxLocations was refused by the dataset"
            break
        rows_written += len(tail_rows)
        note(ST_LIMIT, len(tail_rows))

    accounted = sum(by_status.values())
    mismatch = accounted != len(tasks) + len(cut)
    if charged_total + delivered_uncharged != counts["delivered"]:
        mismatch = True

    summary = {
        "locationsRequested": len(tasks) + len(cut),
        "locationsAttempted": counts["attempted"],
        "locationsRead": counts["read"],
        "locationsNotFound": counts["notFound"],
        "locationsInvalid": counts["invalid"],
        "locationsDuplicate": counts["duplicate"],
        "locationsFailed": counts["failed"],
        "locationsLimited": counts["limited"],
        "locationsCutByBudget": counts["cutByBudget"],
        "locationsCutByDeadline": counts["cutByDeadline"],
        "locationsCutByStop": counts["cutByStop"],
        "locationsDelivered": counts["delivered"],
        "locationsCapped": counts["capped"],
        "rowsWritten": rows_written,
        "byStatus": by_status,
        "chargedEvents": charged_total,
        "deliveredUncharged": delivered_uncharged,
        "deliveredUnverified": delivered_unverified,
        "chargeUncertain": charge_uncertain,
        "writeErrors": write_errors,
        "summaryWriteErrors": summary_errors,
        "billingActive": None if can_charge is None else bool(can_charge),
        "source": "MET Norway Locationforecast 2.0 (CC-BY 4.0)",
        "priceEvent": EVENT_NAME,
        "priceNote": price_note,
        "units": units,
        "hours": hours,
        "includeDaily": include_daily,
        "maxLocations": max_locations,
        "counterMismatch": mismatch,
        "stopReason": stop_reason,
        "datasetNote": dataset_note,
        "startedAt": started,
        "finishedAt": now_iso(),
    }

    run_id = _run_id()
    try:
        await Actor.set_value("RUN_SUMMARY", summary)
    except Exception as exc:  # noqa: BLE001
        summary_errors += 1
        Actor.log.warning("bare RUN_SUMMARY was not written: %s", exc)
    if run_id:
        try:
            store = await Actor.open_key_value_store(name="weather-forecast-summary")
            await store.set_value("RUN_SUMMARY-" + run_id, summary)
        except Exception as exc:  # noqa: BLE001
            summary_errors += 1
            Actor.log.warning("run-scoped summary was not written: %s", exc)
    if summary_errors:
        summary["summaryWriteErrors"] = summary_errors
        try:
            await Actor.set_value("RUN_SUMMARY", summary)
        except Exception:  # noqa: BLE001
            pass

    told = (f"{counts['delivered']} place(s) delivered, {charged_total} charged, "
            f"{rows_written - counts['delivered']} free status row(s)")
    if stop_reason:
        told += f", stopped: {stop_reason}"
    await Actor.set_status_message(told)

    if counts["attempted"] > 0 and counts["read"] == 0:
        await Actor.fail(status_message=(
            f"Every coordinate pair was asked for and none was read: "
            f"{counts['attempted']} attempt(s). First reason: "
            f"{first_failure or 'not recorded'}. Nothing was delivered and nothing was "
            "charged."))
        return

    if counts["read"] == 0 and counts["invalid"] > 0:
        await Actor.fail(status_message=(
            f"None of the {counts['invalid']} place(s) was a coordinate pair: send "
            "latitude,longitude such as 50.45,30.52. Names are not resolved (geocoding is a "
            "different service), so nothing was asked of the source, delivered or charged."))
        return

    if counts["read"] == 0 and rows_written == 0:
        await Actor.fail(status_message=(
            "Nothing was read and nothing was written: the input held no usable place "
            "and no status row could be created."))
        return


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
