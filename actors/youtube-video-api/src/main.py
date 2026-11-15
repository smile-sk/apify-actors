"""YouTube Video API: one row per video, read from the public innertube endpoint.

Money rules, learned the hard way in this park:
  * A paid row is written and charged in ONE call, Actor.push_data(rows,
    charged_event_name=...). A separate charge before the write leaves a row that the
    buyer paid for and cannot download.
  * The batch is cut by the remaining budget BEFORE the write: the SDK silently writes
    and charges only the prefix that fits, and counters built from the batch size would
    report sales that never happened.
  * An exception from that call can mean two different things: the charge failed after
    the rows landed, or the dataset refused the write and nothing landed. Only the
    dataset counter tells them apart.
  * Anything that is not a video comes back free, with a status and a plain reason, and
    carries no field a buyer could use as data.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
from apify import Actor

from . import yt

ST_OK = "OK"
ST_INVALID = "INVALID_INPUT"
ST_DUPLICATE = "DUPLICATE"
ST_NOT_FOUND = "NOT_FOUND"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"

EVENT_NAME = "video-delivered"
BATCH_SIZE = 10
VIDEO_COST_SECONDS = 3.0
DEADLINE_MARGIN_SECONDS = 20.0
# A local run has no platform deadline, but the call still needs a bound: the relay
# talks to YouTube with its own 15 s and 20 s waits, and one hung answer hangs the run.
DEFAULT_CALL_TIMEOUT_SECONDS = 35.0
DEFAULT_MAX_VIDEOS = 50
MAX_VIDEOS_CEILING = 200


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

    get_pricing_info() is synchronous in the SDK: awaiting it silently zeroes the whole
    money path.
    """
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001
        return None, f"pricing read failed: {type(exc).__name__}"
    if info is None:
        return None, "pricing info is not readable"
    is_ppe = getattr(info, "is_pay_per_event", None)
    prices = getattr(info, "per_event_prices", None)
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

    Dataset.get_metadata() answers a pydantic DatasetMetadata with item_count on it, not
    a dict: reading it as a dict silently gives None and hides a refused write.
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


async def main() -> None:
    await Actor.init()
    started = now_iso()

    data = await Actor.get_input() or {}
    raw_videos = data.get("videos")
    if isinstance(raw_videos, str):
        wanted = [raw_videos]
    elif isinstance(raw_videos, list):
        wanted = [str(item) for item in raw_videos]
    else:
        wanted = []
    tasks = [str(item).strip() for item in wanted]
    if not any(tasks):
        await Actor.fail(status_message=(
            "No videos were sent. Pass YouTube ids or links in the videos field, for "
            "example {\"videos\": [\"jNQXAC9IVRw\", "
            "\"https://www.youtube.com/watch?v=dQw4w9WgXcQ\"]}."))
        return

    include_description = bool(data.get("includeDescription"))
    max_videos = _limit(data.get("maxVideos"), DEFAULT_MAX_VIDEOS, 1, MAX_VIDEOS_CEILING)
    if len(tasks) > max_videos:
        cut = tasks[max_videos:]
        tasks = tasks[:max_videos]
    else:
        cut = []

    deadline = platform_deadline()
    if deadline is not None:
        left = time_left(deadline) or 0.0
        if left < DEADLINE_MARGIN_SECONDS + VIDEO_COST_SECONDS:
            await Actor.fail(status_message=(
                f"The run has {int(left)} s left before the platform stops it, which is "
                "less than one video needs. Send fewer videos or raise the timeout."))
            return

    charging = Actor.get_charging_manager()
    can_charge, price_note = price_state(charging)
    billable_claim = None if can_charge is None else bool(can_charge)

    dataset = await Actor.open_dataset()
    client = httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=False)

    # The player endpoint refuses the platform network, so the actor asks our own relay
    # on the VPS first. A direct call stays as the fallback for the case where the
    # relay itself is what failed: a dead relay must not turn into "no videos".
    relay_url = (os.environ.get("YT_RELAY_URL") or "").strip()
    relay_token = (os.environ.get("YT_RELAY_TOKEN") or "").strip()
    relay = yt.Relay(relay_url, relay_token) if relay_url and relay_token else None
    relay_required = (os.environ.get("YT_RELAY_REQUIRED") or "").strip() == "1"

    counts = {"videosAttempted": 0, "videosRead": 0, "videosNotFound": 0,
              "videosInvalid": 0, "videosDuplicate": 0, "videosFailed": 0,
              "videosLimited": len(cut), "videosCutByBudget": 0,
              "videosCutByDeadline": 0, "videosCutByStop": 0,
              "videosDelivered": 0, "videosCapped": 0,
              "relayCalls": 0, "relayFallbacks": 0, "relayErrors": 0}
    transports = {"relay": 0, "direct": 0}
    by_status: dict[str, int] = {}
    seen_inputs: set[str] = set()
    seen_ids: set[str] = set()
    batch: list[dict] = []
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
        return {"actorInput": value, "actorIndex": index, "videoId": None, "url": None,
                "title": None, "channelTitle": None, "channelId": None, "views": None,
                "lengthSeconds": None, "publishedAt": None, "category": None,
                "keywords": None, "isLive": None, "thumbnailUrl": None,
                "playability": None, "playabilityReason": None, "status": status,
                "reason": reason, "billable": False}

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

    async def write_paid(rows: list[dict]) -> None:
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
            counts["videosDelivered"] += len(rows)
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
                counts["videosCapped"] += 1
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
                counts["videosCapped"] += 1
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
                counts["videosCapped"] += 1
                await free_status(row.get("actorInput") or "",
                                  int(row.get("actorIndex") or 0), ST_CAP,
                                  "the remaining budget ended before this video: not "
                                  "delivered and not charged")
        if not pay:
            return
        for row in pay:
            row["billable"] = billable_claim is not False
        before = await _item_count(dataset)
        try:
            result = await Actor.push_data(pay, charged_event_name=EVENT_NAME)
        except Exception as exc:  # noqa: BLE001
            # The SDK writes the rows before it charges them, so a failed charge usually
            # leaves the rows in the dataset. But the same exception is raised when the
            # dataset refuses the write itself, and then nothing was delivered at all.
            # Only the dataset counter tells the two apart.
            after = await _item_count(dataset)
            grew = None if before is None or after is None else after - before
            if grew is not None and grew >= len(pay):
                charge_uncertain += len(pay)
                delivered_uncharged += len(pay)
                counts["videosDelivered"] += len(pay)
                rows_written += len(pay)
                note(ST_OK, len(pay))
                stopping = True
                stop_reason = "charge-error"
                dataset_note = ("a charge failed: the rows stay in the dataset and are "
                                "counted in chargeUncertain, no second row is written")
            else:
                write_errors += 1
                delivered_unverified += len(pay)
                stopping = True
                stop_reason = "dataset-write-error"
                if grew is None:
                    dataset_note = (
                        f"a paid write raised {type(exc).__name__} and the dataset count "
                        "could not be read: delivery is unknown, the batch is counted in "
                        "deliveredUnverified and nothing is claimed as sold")
                else:
                    dataset_note = (
                        f"a paid write raised {type(exc).__name__} and the dataset count "
                        f"grew by {grew} of {len(pay)}: delivery is partial, the batch is "
                        "counted in deliveredUnverified and nothing is claimed as sold")
            return
        charged = getattr(result, "charged_count", None)
        if charged is None:
            delivered_uncharged += len(pay)
            counts["videosDelivered"] += len(pay)
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
        counts["videosDelivered"] += len(pay)
        rows_written += len(pay)
        note(ST_OK, len(pay))
        delivered_uncharged += len(pay) - charged

    async def flush() -> None:
        nonlocal batch
        if batch:
            rows, batch = batch, []
            await write_paid(rows)

    for index, raw in enumerate(tasks):
        if raw in seen_inputs:
            counts["videosDuplicate"] += 1
            await free_status(raw, index, ST_DUPLICATE,
                              "the same video was already sent in this run")
            continue
        seen_inputs.add(raw)

        vid, why = yt.video_id(raw)
        if vid is None:
            counts["videosInvalid"] += 1
            await free_status(raw, index, ST_INVALID, why)
            continue
        if vid in seen_ids:
            counts["videosDuplicate"] += 1
            await free_status(raw, index, ST_DUPLICATE,
                              "the same video was already delivered in this run")
            continue
        if stopping:
            if stop_reason in ("spending-cap", "charge-error", "billing-price-unknown"):
                counts["videosCutByBudget"] += 1
                await free_status(raw, index, ST_CAP,
                                  f"the run stopped on money ({stop_reason}) before this "
                                  "video: not read")
            else:
                counts["videosCutByStop"] += 1
                await free_status(raw, index, ST_ERROR,
                                  f"the run stopped ({stop_reason or 'earlier error'}) "
                                  "before this video: not read")
            continue
        left = time_left(deadline)
        if left is not None and left < VIDEO_COST_SECONDS * 2:
            counts["videosCutByDeadline"] += 1
            stop_reason = stop_reason or "deadline"
            await free_status(raw, index, ST_TIME, f"the run had {int(left)} s left: not read")
            continue

        counts["videosAttempted"] += 1
        request_budget = (DEFAULT_CALL_TIMEOUT_SECONDS if left is None
                          else max(2.0, left - DEADLINE_MARGIN_SECONDS))
        transport = "direct"
        relay_reason = ""
        if relay is not None:
            counts["relayCalls"] += 1
            payload, reason, kind = relay.fetch(client, vid, timeout=request_budget)
            transport = "relay"
            if payload is None and kind == "relay":
                counts["relayErrors"] += 1
                relay_reason = reason
                # The relay and the direct road do not share one budget: the first call
                # may already have eaten most of the time left, and handing the second
                # call the same number lets one video run up to twice the deadline.
                left2 = time_left(deadline)
                if relay_required:
                    reason = f"{relay_reason} (the direct fallback is switched off)"
                elif left2 is not None and left2 < VIDEO_COST_SECONDS * 2:
                    counts["videosCutByDeadline"] += 1
                    stop_reason = stop_reason or "deadline"
                    if not first_failure:
                        first_failure = relay_reason or "the run ran out of time"
                    await free_status(raw, index, ST_TIME,
                                      f"the relay failed ({relay_reason}) and the run "
                                      f"had {int(left2)} s left, less than one video "
                                      "needs: not read")
                    continue
                else:
                    budget2 = (None if left2 is None
                               else max(2.0, left2 - DEADLINE_MARGIN_SECONDS))
                    payload, reason = yt.fetch(client, vid, timeout=budget2)
                    transport = "direct"
                    if payload is not None:
                        counts["relayFallbacks"] += 1
        else:
            payload, reason = yt.fetch(client, vid, timeout=request_budget)
        if payload is None and relay_reason and relay_reason not in reason:
            # The buyer reads the row, not the summary: the relay reason must survive the
            # fallback, otherwise our own broken token looks like a verdict about the video.
            reason = f"{reason} (the relay first answered: {relay_reason})"
        if payload is None:
            if not first_failure:
                first_failure = reason
            if "has no video" in reason:
                counts["videosNotFound"] += 1
                await free_status(raw, index, ST_NOT_FOUND, reason)
            else:
                counts["videosFailed"] += 1
                await free_status(raw, index, ST_ERROR, reason)
            continue
        seen_ids.add(vid)
        transports[transport] = transports.get(transport, 0) + 1
        counts["videosRead"] += 1
        batch.append(yt.row(payload, raw=raw, index=index,
                            with_description=include_description))
        if len(batch) >= BATCH_SIZE:
            await flush()

    await flush()
    client.close()

    for start_at in range(0, len(cut), 100):
        chunk = cut[start_at:start_at + 100]
        tail_rows = [status_row(raw, len(tasks) + start_at + offset, ST_LIMIT,
                                f"maxVideos is {max_videos}: this video was not attempted")
                     for offset, raw in enumerate(chunk)]
        try:
            await dataset.push_data(tail_rows)
        except Exception:  # noqa: BLE001
            write_errors += 1
            stopping = True
            stop_reason = stop_reason or "dataset-write-error"
            dataset_note = "the tail past maxVideos was refused by the dataset"
            break
        rows_written += len(tail_rows)
        note(ST_LIMIT, len(tail_rows))

    accounted = sum(by_status.values())
    mismatch = accounted != len(tasks) + len(cut)
    if charged_total + delivered_uncharged != counts["videosDelivered"]:
        mismatch = True

    summary = {
        "videosRequested": len(tasks) + len(cut),
        "videosAttempted": counts["videosAttempted"],
        "videosRead": counts["videosRead"],
        "videosNotFound": counts["videosNotFound"],
        "videosInvalid": counts["videosInvalid"],
        "videosDuplicate": counts["videosDuplicate"],
        "videosFailed": counts["videosFailed"],
        "videosLimited": counts["videosLimited"],
        "videosCutByBudget": counts["videosCutByBudget"],
        "videosCutByDeadline": counts["videosCutByDeadline"],
        "videosCutByStop": counts["videosCutByStop"],
        "videosDelivered": counts["videosDelivered"],
        "videosCapped": counts["videosCapped"],
        "gateway": "relay" if relay is not None else "direct",
        "relayCalls": counts["relayCalls"],
        "relayFallbacks": counts["relayFallbacks"],
        "relayErrors": counts["relayErrors"],
        "transports": transports,
        "rowsWritten": rows_written,
        "byStatus": by_status,
        "chargedEvents": charged_total,
        "deliveredUncharged": delivered_uncharged,
        "deliveredUnverified": delivered_unverified,
        "chargeUncertain": charge_uncertain,
        "writeErrors": write_errors,
        "summaryWriteErrors": summary_errors,
        "billingActive": None if can_charge is None else bool(can_charge),
        "priceEvent": EVENT_NAME,
        "priceNote": price_note,
        "includeDescription": include_description,
        "maxVideos": max_videos,
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
            store = await Actor.open_key_value_store(name="youtube-video-summary")
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

    told = (f"{counts['videosDelivered']} video(s) delivered, {charged_total} charged, "
            f"{rows_written - counts['videosDelivered']} free status row(s)")
    if stop_reason:
        told += f", stopped: {stop_reason}"
    await Actor.set_status_message(told)

    # A run that read nothing is a failure only when a road was in trouble. A list of
    # deleted videos answers free NOT_FOUND rows: that is a legitimate result, and failing
    # it would send the buyer to check a healthy relay token.
    road_trouble = counts["videosFailed"] > 0 or counts["relayErrors"] > 0
    if counts["videosAttempted"] > 0 and counts["videosRead"] == 0 and road_trouble:
        if counts["relayErrors"] > 0:
            advice = "Check the relay token and the relay health before re-running."
        else:
            advice = "The endpoint refused every video: see the reason in the rows."
        await Actor.fail(status_message=(
            f"Every video was asked for and none was read: "
            f"{counts['videosAttempted']} attempt(s), "
            f"{counts['videosFailed']} refusal(s) from YouTube, "
            f"{counts['relayErrors']} relay refusal(s). First reason: "
            f"{first_failure or 'not recorded'}. Nothing was delivered and nothing "
            f"was charged. {advice}"))
        return

    if counts["videosRead"] == 0 and rows_written == 0:
        await Actor.fail(status_message=(
            "Nothing was read and nothing was written: the input held no usable video "
            "and no status row could be created."))
        return


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
