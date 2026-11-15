"""YouTube Shorts API: a channel's Shorts shelf as rows, paid per delivered Short.

Money rules, the same discipline as the rest of this park:

  * one door for paid rows: Actor.push_data(rows, charged_event_name=...), so a row
    is written and charged by the same call and there is never a charge without a row;
  * the price is read once, synchronously; if it cannot be read the run stops before
    the first payable Short and the rest goes out free with a reason;
  * a batch that does not fit the remaining budget is cut here, before the write;
  * a status row never carries a video id, a title or a thumbnail, so it can never be
    mistaken for the product;
  * everything that was not sold says why and costs nothing.

What the source really does (measured 2026-11-12, not assumed):

  * the channel page carries five tabs and the Shorts tab has its own params, so the
    params are read from the page itself;
  * one Shorts page gave 48 entries and the continuation token kept giving 48 more:
    three pages gave 144 unique videos;
  * entries are shortsLockupViewModel, with the id inside entityId and the title and
    view text inside overlayMetadata ("79M views");
  * no browser, no API key and no proxy are needed, and it answers from a datacenter.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from apify import Actor

from . import shorts as sh

ST_OK = "OK"
ST_INVALID = "INVALID_INPUT"
ST_DUPLICATE = "DUPLICATE"
ST_NO_SHORTS = "NO_SHORTS_FOUND"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_CHARGE_UNVERIFIED = "NOT_CHECKED_CHARGE_UNVERIFIED"

EVENT_NAME = "short-delivered"
SUMMARY_STORE = "youtube-shorts-summary"
BATCH_SIZE = 20
PAGE_COST_SECONDS = 4.0
REQUEST_TIMEOUT_SECONDS = 20.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def platform_deadline() -> float | None:
    """The moment the platform kills this run, in epoch seconds, if it says so."""
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
    money path, and this park has paid for that lesson once already.
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
        return False, "the actor is not sold per event, so nothing is charged"
    if is_ppe is None:
        return None, "the pricing model could not be read"
    if not names:
        return None, "the event price list is empty"
    if EVENT_NAME not in names:
        return None, f"the event {EVENT_NAME} is not in the price list ({', '.join(names)})"
    return True, ""


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}

        raw_channels = raw.get("channels")
        if isinstance(raw_channels, str):
            raw_channels = [raw_channels]
        if not isinstance(raw_channels, list):
            raw_channels = []
        channels: list[str] = []
        for item in raw_channels:
            text = sh.clean(item)
            if text:
                channels.append(text)
        if not channels:
            await Actor.fail(status_message=(
                "channels is empty - pass at least one channel handle, url or channel id "
                "(for example \"@MrBeast\"): nothing was read and nothing was charged"))
            return

        region = (sh.clean(raw.get("region")) or "US").upper()
        language = (sh.clean(raw.get("language")) or "en").lower()
        max_shorts = sh.clamp_max(raw.get("maxShortsPerChannel"))

        refused = ""
        if len(region) != 2 or not region.isalpha():
            refused = "region must be a two letter country code, for example US or DE"
        elif len(language) != 2 or not language.isalpha():
            refused = "language must be a two letter code, for example en or de"
        if refused:
            await Actor.fail(status_message=(
                f"{refused}: the run stopped before the first request, nothing was read "
                "and nothing was charged"))
            return

        ctx = sh.context(region, language)
        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()
        can_charge, price_note = price_state(charging)
        billable_claim = None if can_charge is None else bool(can_charge)
        deadline = platform_deadline()

        counts = {
            "channelsRequested": len(channels), "channelsAttempted": 0, "channelsRead": 0,
            "channelsEmpty": 0, "channelsDuplicate": 0, "channelsFailed": 0,
            "channelsInvalid": 0, "channelsNoShorts": 0, "channelsLimited": 0,
            "channelsCutByBudget": 0,
            "shortsSeen": 0, "shortsDelivered": 0, "shortsDuplicate": 0,
            "shortsLimited": 0, "shortsCapped": 0, "shortsStopped": 0,
            "pagesRead": 0, "pagesFailed": 0,
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
        seen_channels: set[str] = set()
        batch: list[dict] = []

        def note(status: str, amount: int = 1) -> None:
            by_status[status] = by_status.get(status, 0) + amount

        async def free_status(value: str, index: int, status: str, reason: str) -> None:
            """A row that carries a reason and nothing a buyer could use as data."""
            nonlocal rows_written, write_errors, stopping, stop_reason, dataset_note
            row = {"channelInput": value, "channelIndex": index, "videoId": None,
                   "status": status, "reason": reason, "billable": False}
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
                rows_written += len(rows)
                counts["shortsDelivered"] += len(rows)
                delivered_uncharged += len(rows)
                note(ST_OK, len(rows))
                return
            if can_charge is None:
                stopping = True
                stop_reason = stop_reason or "billing-price-unknown"
                for row in rows:
                    counts["shortsStopped"] += 1
                    await free_status(row.get("channelInput") or "", 0, ST_BILLING,
                                      f"not delivered and not charged: {price_note}")
                return
            if charging.is_event_charge_limit_reached(EVENT_NAME):
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in rows:
                    counts["shortsCapped"] += 1
                    await free_status(row.get("channelInput") or "", 0, ST_CAP,
                                      "the spending cap was reached: not delivered and "
                                      "not charged")
                return
            try:
                budget = int(charging.compute_push_data_limit(
                    len(rows), EVENT_NAME, is_default_dataset=True))
            except Exception:  # noqa: BLE001 - no budget answer means no sale
                budget = None
            if budget is None:
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in rows:
                    counts["shortsCapped"] += 1
                    await free_status(row.get("channelInput") or "", 0, ST_CAP,
                                      "the remaining budget could not be computed: not "
                                      "delivered and not charged")
                return
            pay, over = rows[:max(0, budget)], rows[max(0, budget):]
            if over:
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in over:
                    counts["shortsCapped"] += 1
                    await free_status(row.get("channelInput") or "", 0, ST_CAP,
                                      "the remaining budget ended before this row: not "
                                      "delivered and not charged")
            if not pay:
                return
            for row in pay:
                row["billable"] = billable_claim is not False
            before = None
            try:
                before = await dataset.get_metadata()
                before = getattr(before, "item_count", None)
            except Exception:  # noqa: BLE001
                before = None
            try:
                result = await Actor.push_data(pay, charged_event_name=EVENT_NAME)
            except Exception:  # noqa: BLE001
                charge_uncertain += len(pay)
                stopping = True
                stop_reason = "charge-error"
                dataset_note = ("a charge failed: the rows stay in the dataset and are "
                                "counted in chargeUncertain, and no second row is written")
                return
            charged = getattr(result, "charged_count", None)
            if charged is None:
                for row in pay:
                    row["billable"] = None
                charged_total += 0
                delivered_uncharged += len(pay)
                counts["shortsDelivered"] += len(pay)
                charge_uncertain += len(pay)
                stopping = True
                stop_reason = stop_reason or "charge-error"
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
            counts["shortsDelivered"] += len(pay)
            rows_written += len(pay)
            note(ST_OK, len(pay))
            delivered_uncharged += len(pay) - charged
            after = None
            try:
                after = await dataset.get_metadata()
                after = getattr(after, "item_count", None)
            except Exception:  # noqa: BLE001
                after = None
            if isinstance(before, int) and isinstance(after, int) and after <= before:
                dataset_note = ("the dataset counter did not grow after the write: rows may "
                                "still be landing, the charge is the fact")

        async def flush() -> None:
            nonlocal batch
            if batch:
                rows, batch = batch, []
                await write_paid(rows)

        for index, channel in enumerate(channels):
            key = channel.lower()
            if key in seen_channels:
                counts["channelsDuplicate"] += 1
                await free_status(channel, index, ST_DUPLICATE,
                                  "the same channel was already sent in this run")
                continue
            seen_channels.add(key)
            if stopping:
                counts["channelsCutByBudget"] += 1
                await free_status(channel, index, ST_CAP,
                                  "the run stopped on money before this channel: not read")
                continue
            left = time_left(deadline)
            if left is not None and left < PAGE_COST_SECONDS * 2:
                counts["channelsCutByBudget"] += 1
                await free_status(channel, index, ST_TIME,
                                  "not enough time was left before the platform deadline")
                continue

            url = sh.channel_url(channel)
            if not url:
                counts["channelsInvalid"] += 1
                await free_status(channel, index, ST_INVALID,
                                  "this is not a channel handle, url or channel id")
                continue

            counts["channelsAttempted"] += 1
            channel_failed = ""
            try:
                resolved = await asyncio.to_thread(
                    sh.post, sh.build_resolve(url, ctx), REQUEST_TIMEOUT_SECONDS)
                channel_id = sh.channel_id(resolved)
            except Exception as exc:  # noqa: BLE001
                channel_failed = f"{type(exc).__name__}: {str(exc)[:120]}"
                counts["channelsFailed"] += 1
                page_errors.append(f"{channel}: {channel_failed}")
                await free_status(channel, index, ST_ERROR,
                                  f"the channel page could not be read: {channel_failed}")
                continue
            if not channel_id:
                counts["channelsInvalid"] += 1
                await free_status(channel, index, ST_INVALID,
                                  "the channel could not be resolved: check the handle or url")
                continue

            channel_title = ""
            tab_params = ""
            try:
                home = await asyncio.to_thread(
                    sh.post, sh.build_browse(channel_id, ctx), REQUEST_TIMEOUT_SECONDS)
                channel_title, tab_params = sh.channel_info(home)
            except Exception as exc:  # noqa: BLE001
                channel_failed = f"{type(exc).__name__}: {str(exc)[:120]}"
                counts["channelsFailed"] += 1
                page_errors.append(f"{channel}: {channel_failed}")
                await free_status(channel, index, ST_ERROR,
                                  f"the channel page could not be read: {channel_failed}")
                continue
            if not tab_params:
                counts["channelsNoShorts"] += 1
                await free_status(channel, index, ST_NO_SHORTS,
                                  "this channel publishes no Shorts: its page has no "
                                  "Shorts tab")
                continue

            seen_ids: set[str] = set()
            collected = 0
            token: str | None = None
            first = True
            while collected < max_shorts:
                if not first and not token:
                    break
                left = time_left(deadline)
                if left is not None and left < PAGE_COST_SECONDS:
                    if collected:
                        dataset_note = dataset_note or "the deadline cut the channel short"
                    break
                try:
                    if first:
                        body = sh.build_browse(channel_id, ctx, params=tab_params)
                    else:
                        body = sh.build_browse("", ctx, continuation=token)
                    payload = await asyncio.to_thread(sh.post, body, REQUEST_TIMEOUT_SECONDS)
                    entries, token = sh.parse_page(payload)
                except Exception as exc:  # noqa: BLE001
                    channel_failed = f"{type(exc).__name__}: {str(exc)[:120]}"
                    counts["pagesFailed"] += 1
                    page_errors.append(f"{channel}: {channel_failed}")
                    break
                first = False
                counts["pagesRead"] += 1
                for entry in entries:
                    if collected >= max_shorts:
                        break
                    row = sh.parse_short(entry, channel, channel_title, channel_id,
                                         collected)
                    if row is None:
                        continue
                    counts["shortsSeen"] += 1
                    if row["videoId"] in seen_ids:
                        counts["shortsDuplicate"] += 1
                        continue
                    seen_ids.add(row["videoId"])
                    row["status"] = ST_OK
                    row["channelIndex"] = index
                    row["billable"] = billable_claim is not False
                    batch.append(row)
                    collected += 1
                    if len(batch) >= BATCH_SIZE:
                        await flush()
                    if stopping:
                        break
                if stopping:
                    break

            if channel_failed:
                counts["channelsFailed"] += 1
                await free_status(channel, index, ST_ERROR,
                                  f"the Shorts page could not be read: {channel_failed}")
                continue
            if collected == 0:
                counts["channelsEmpty"] += 1
                await free_status(channel, index, ST_NO_SHORTS,
                                  "the Shorts tab is open but returned no videos")
                continue
            counts["channelsRead"] += 1
            if collected >= max_shorts:
                counts["channelsLimited"] += 1
            await flush()

        await flush()

        summary = {
            "channelsRequested": counts["channelsRequested"],
            "channelsAttempted": counts["channelsAttempted"],
            "channelsRead": counts["channelsRead"],
            "channelsEmpty": counts["channelsEmpty"],
            "channelsDuplicate": counts["channelsDuplicate"],
            "channelsFailed": counts["channelsFailed"],
            "channelsInvalid": counts["channelsInvalid"],
            "channelsNoShorts": counts["channelsNoShorts"],
            "channelsLimited": counts["channelsLimited"],
            "channelsCutByBudget": counts["channelsCutByBudget"],
            "shortsSeen": counts["shortsSeen"],
            "shortsDelivered": counts["shortsDelivered"],
            "shortsDuplicate": counts["shortsDuplicate"],
            "shortsLimited": counts["shortsLimited"],
            "shortsCapped": counts["shortsCapped"],
            "shortsStopped": counts["shortsStopped"],
            "pagesRead": counts["pagesRead"],
            "pagesFailed": counts["pagesFailed"],
            "pageErrors": page_errors[:5],
            "rowsWritten": rows_written,
            "byStatus": by_status,
            "chargedEvents": charged_total,
            "deliveredUncharged": delivered_uncharged,
            "chargeUncertain": charge_uncertain,
            "writeErrors": write_errors,
            "billingActive": (None if can_charge is None else bool(can_charge)),
            "priceNote": price_note,
            "priceEvent": EVENT_NAME,
            "region": region,
            "language": language,
            "maxShortsPerChannel": max_shorts,
            "counterMismatch": (counts["shortsDelivered"] != (charged_total + delivered_uncharged)),
            "stopReason": stop_reason,
            "datasetNote": dataset_note,
            "finishedAt": now_iso(),
        }
        await Actor.set_status_message(
            f"{counts['shortsDelivered']} Short(s) delivered and {charged_total} charged"
            + (f", stopped: {stop_reason}" if stop_reason else ""))
        try:
            await Actor.set_value("RUN_SUMMARY", summary)
            run_id = str((Actor.get_env() or {}).get("run_id") or "").strip()
            if run_id:
                store = await Actor.open_key_value_store(name=SUMMARY_STORE)
                await store.set_value("RUN_SUMMARY-" + run_id, summary)
        except Exception:  # noqa: BLE001 - the dataset is the product, the summary is a note
            Actor.log.warning("the run summary could not be written")


if __name__ == "__main__":
    asyncio.run(main())
