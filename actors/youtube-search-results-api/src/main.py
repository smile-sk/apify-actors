"""YouTube Search API: keyword search over YouTube, one row per video, paid per row.

Money rules, the same discipline as the actors before this one:

  * one door for paid rows: Actor.push_data(rows, charged_event_name=...); the SDK
    writes the row and then charges it, so there is never a charge without a row;
  * the price is read once, synchronously; if it cannot be read the run stops before
    the first payable video and the rest goes out free with a reason;
  * a row is sold only when it reached the dataset; a status row never carries a video
    id, a title or a description;
  * a batch that does not fit the remaining budget is cut here, before the write;
  * everything that was not sold says why and costs nothing.

What the source really does (measured 2026-11-12, not assumed):
  * a search page returns 9-20 videos and a continuation token, and five pages gave
    79 unique videos, so paging is real;
  * sortBy=views reorders the page; sortBy=date and sortBy=rating are ignored by the
    endpoint, so they are not offered;
  * the upload date filter (hour/today/week/month/year) does restrict the page;
  * no browser, no API key and no proxy are needed, and it answers from a datacenter.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from apify import Actor

from . import yts

ST_OK = "OK"
ST_INVALID = "INVALID_INPUT"
ST_DUPLICATE = "DUPLICATE"
ST_NO_RESULTS = "NO_RESULTS_FOUND"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_CHARGE_UNVERIFIED = "NOT_CHECKED_CHARGE_UNVERIFIED"

EVENT_NAME = "video-delivered"
SUMMARY_STORE = "youtube-search-results-summary"
BATCH_SIZE = 20
PAGE_COST_SECONDS = 4.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


async def dataset_items(dataset) -> int | None:
    """How many rows the dataset holds right now, or None when the counter lags."""
    try:
        meta = await dataset.get_metadata()
    except Exception:  # noqa: BLE001
        return None
    count = getattr(meta, "item_count", None)
    return count if isinstance(count, int) else None


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}

        raw_queries = raw.get("queries")
        if isinstance(raw_queries, str):
            raw_queries = [raw_queries]
        if not isinstance(raw_queries, list):
            raw_queries = []
        queries: list[str] = []
        for item in raw_queries:
            text = yts.clean(item)
            if text:
                queries.append(text)
        if not queries:
            await Actor.fail(status_message=(
                "queries is empty - pass at least one search phrase (for example "
                "\"python tutorial\"): nothing was read and nothing was charged"))
            return

        sort_by = str(raw.get("sortBy") or "relevance").strip()
        upload_date = str(raw.get("uploadDate") or "any").strip()
        duration = str(raw.get("videoDuration") or "any").strip()
        region = (yts.clean(raw.get("region")) or "US").upper()
        language = (yts.clean(raw.get("language")) or "en").lower()
        max_results = yts.clamp_results(raw.get("maxResultsPerQuery"))

        refused = ""
        if sort_by not in yts.SORT_VALUES:
            refused = f"sortBy must be one of {', '.join(yts.SORT_VALUES)}"
        elif upload_date not in yts.DATE_VALUES:
            refused = f"uploadDate must be one of {', '.join(yts.DATE_VALUES)}"
        elif duration not in yts.DURATION_VALUES:
            refused = f"videoDuration must be one of {', '.join(yts.DURATION_VALUES)}"
        elif len(region) != 2 or not region.isalpha():
            refused = "region must be a two letter country code, for example US or DE"
        elif len(language) != 2 or not language.isalpha():
            refused = "language must be a two letter code, for example en or de"
        if refused:
            await Actor.fail(status_message=(
                f"{refused}: the run stopped before the first request, nothing was read "
                "and nothing was charged"))
            return

        params = yts.search_params(sort_by, upload_date, duration)

        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()
        can_charge, price_note = price_state(charging)
        billable_claim = None if can_charge is None else bool(can_charge)
        deadline = platform_deadline()

        counts = {
            "queriesRequested": len(queries), "queriesAttempted": 0, "queriesRead": 0,
            "queriesEmpty": 0, "queriesDuplicate": 0, "queriesFailed": 0,
            "queriesLimited": 0, "queriesCutByBudget": 0,
            "videosSeen": 0, "videosDelivered": 0, "videosDuplicate": 0,
            "videosLimited": 0, "videosCapped": 0, "videosStopped": 0,
            "pagesRead": 0, "pagesFailed": 0, "shortsSkipped": 0,
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
        seen_queries: set[str] = set()
        batch: list[dict] = []

        def note(status: str, amount: int = 1) -> None:
            by_status[status] = by_status.get(status, 0) + amount

        async def free_status(value: str, index: int, status: str, reason: str) -> None:
            """A row that carries a reason and nothing a buyer could use as data."""
            nonlocal rows_written, write_errors, stopping, stop_reason, dataset_note
            row = {"query": value, "queryIndex": index, "videoId": None, "status": status,
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
                rows_written += len(rows)
                counts["videosDelivered"] += len(rows)
                delivered_uncharged += len(rows)
                note(ST_OK, len(rows))
                return
            if can_charge is None:
                stopping = True
                stop_reason = stop_reason or "billing-price-unknown"
                for row in rows:
                    counts["videosStopped"] += 1
                    await free_status(row.get("query") or "", 0, ST_BILLING,
                                      f"not delivered and not charged: {price_note}")
                return
            if charging.is_event_charge_limit_reached(EVENT_NAME):
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in rows:
                    counts["videosCapped"] += 1
                    await free_status(row.get("query") or "", 0, ST_CAP,
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
                    counts["videosCapped"] += 1
                    await free_status(row.get("query") or "", 0, ST_CAP,
                                      "the remaining budget could not be computed: not "
                                      "delivered and not charged")
                return
            pay, over = rows[:max(0, budget)], rows[max(0, budget):]
            if over:
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in over:
                    counts["videosCapped"] += 1
                    await free_status(row.get("query") or "", 0, ST_CAP,
                                      "the spending cap leaves no budget for this row: not "
                                      "delivered and not charged")
            if not pay:
                return
            for row in pay:
                row["billable"] = billable_claim
            try:
                result = await Actor.push_data(pay, charged_event_name=EVENT_NAME)
            except Exception as exc:  # noqa: BLE001
                landed = await dataset_items(dataset)
                if landed is not None and landed >= rows_written + len(pay):
                    delivered_uncharged += len(pay)
                    counts["videosDelivered"] += len(pay)
                    rows_written += len(pay)
                    note(ST_OK, len(pay))
                    dataset_note = (f"a batch of {len(pay)} is in the dataset but the charge "
                                    "is not confirmed: stop after charge-error")
                else:
                    charge_uncertain += len(pay)
                    for row in pay:
                        await free_status(row.get("query") or "", 0, ST_CHARGE_UNVERIFIED,
                                          "the charge failed and the row was not confirmed in "
                                          "the dataset: not sold")
                stopping = True
                if stop_reason and stop_reason != "charge-error":
                    dataset_note = dataset_note or (f"stopped on {stop_reason} first, then the "
                                                    "charge failed")
                stop_reason = "charge-error"
                dataset_note = dataset_note or f"charge failed: {type(exc).__name__}"
                return
            charged = getattr(result, "charged_count", None)
            if not isinstance(charged, int) or charged < 0:
                charged = None
            if charged is None:
                landed = await dataset_items(dataset)
                if landed is not None and landed >= rows_written + len(pay):
                    delivered_uncharged += len(pay)
                    counts["videosDelivered"] += len(pay)
                    rows_written += len(pay)
                    note(ST_OK, len(pay))
                    dataset_note = ("the batch is in the dataset but the SDK did not report "
                                    "what it charged: stop after charge-error")
                else:
                    charge_uncertain += len(pay)
                    for row in pay:
                        await free_status(row.get("query") or "", 0, ST_CHARGE_UNVERIFIED,
                                          "the charge result was unreadable and the row was not "
                                          "confirmed in the dataset: not sold")
                stopping = True
                if stop_reason and stop_reason != "charge-error":
                    dataset_note = dataset_note or (f"stopped on {stop_reason} first, then the "
                                                    "charge result was unreadable")
                stop_reason = "charge-error"
                dataset_note = dataset_note or "the charge result carried no count"
                return
            if charged < len(pay):
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in pay[charged:]:
                    counts["videosCapped"] += 1
                    await free_status(row.get("query") or "", 0, ST_CAP,
                                      "the budget ran out inside this batch: not delivered "
                                      "and not charged")
            rows_written += charged
            counts["videosDelivered"] += charged
            charged_total += charged
            if charged:
                note(ST_OK, charged)

        async def flush() -> None:
            nonlocal batch
            if batch:
                rows, batch = batch, []
                await write_paid(rows)

        for index, query in enumerate(queries):
            key = query.lower()
            if key in seen_queries:
                counts["queriesDuplicate"] += 1
                await free_status(query, index, ST_DUPLICATE,
                                  "the same search phrase was already sent in this run")
                continue
            seen_queries.add(key)
            if stopping:
                counts["queriesCutByBudget"] += 1
                await free_status(query, index, ST_CAP,
                                  "the run stopped on money before this phrase: not searched")
                continue
            left = time_left(deadline)
            if left is not None and left < PAGE_COST_SECONDS * 2:
                counts["queriesCutByBudget"] += 1
                await free_status(query, index, ST_TIME,
                                  "not enough time was left before the platform deadline")
                continue

            counts["queriesAttempted"] += 1
            seen_ids: set[str] = set()
            collected = 0
            token: str | None = None
            first = True
            query_failed = ""
            while collected < max_results:
                if not first and not token:
                    break
                left = time_left(deadline)
                if left is not None and left < PAGE_COST_SECONDS:
                    if collected:
                        dataset_note = dataset_note or "the deadline cut the phrase short"
                    break
                try:
                    body = yts.build_body(query, region, language, params,
                                          None if first else token)
                    payload = await asyncio.to_thread(yts.post, body)
                    videos, token = yts.parse_page(payload)
                    counts["shortsSkipped"] += yts.count_shorts(payload)
                except Exception as exc:  # noqa: BLE001
                    query_failed = f"{type(exc).__name__}: {str(exc)[:120]}"
                    counts["pagesFailed"] += 1
                    page_errors.append(f"{query}: {query_failed}")
                    break
                first = False
                counts["pagesRead"] += 1
                if not videos:
                    break
                for renderer in videos:
                    if collected >= max_results:
                        break
                    row = yts.parse_video(renderer, query)
                    if row is None:
                        continue
                    counts["videosSeen"] += 1
                    if row["videoId"] in seen_ids:
                        counts["videosDuplicate"] += 1
                        continue
                    seen_ids.add(row["videoId"])
                    row["status"] = ST_OK
                    row["queryIndex"] = index
                    batch.append(row)
                    collected += 1
                    if len(batch) >= BATCH_SIZE:
                        await flush()
                    if stopping:
                        break
                if stopping:
                    break

            if query_failed:
                counts["queriesFailed"] += 1
                await free_status(query, index, ST_ERROR,
                                  f"the search page could not be read: {query_failed}")
                continue
            if collected == 0:
                counts["queriesEmpty"] += 1
                await free_status(query, index, ST_NO_RESULTS,
                                  "the search returned no videos for this phrase")
                continue
            counts["queriesRead"] += 1
            if collected >= max_results:
                counts["queriesLimited"] += 1
            # Settle the money at the end of every phrase: a phrase that starts after the
            # cap must not spend requests on rows nobody can pay for.
            await flush()

        await flush()

        summary = {
            "queriesRequested": counts["queriesRequested"],
            "queriesAttempted": counts["queriesAttempted"],
            "queriesRead": counts["queriesRead"],
            "queriesEmpty": counts["queriesEmpty"],
            "queriesDuplicate": counts["queriesDuplicate"],
            "queriesFailed": counts["queriesFailed"],
            "queriesLimited": counts["queriesLimited"],
            "queriesCutByBudget": counts["queriesCutByBudget"],
            "videosSeen": counts["videosSeen"],
            "videosDelivered": counts["videosDelivered"],
            "videosDuplicate": counts["videosDuplicate"],
            "videosCapped": counts["videosCapped"],
            "videosStopped": counts["videosStopped"],
            "pagesRead": counts["pagesRead"],
            "pagesFailed": counts["pagesFailed"],
            "shortsSkipped": counts["shortsSkipped"],
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
            "sortBy": sort_by,
            "uploadDate": upload_date,
            "videoDuration": duration,
            "region": region,
            "language": language,
            "maxResultsPerQuery": max_results,
            "counterMismatch": counts["videosDelivered"] != (charged_total + delivered_uncharged),
            "stopReason": stop_reason,
            "datasetNote": dataset_note,
            "finishedAt": now_iso(),
        }
        await Actor.set_status_message(
            f"{counts['videosDelivered']} video(s) delivered and {charged_total} charged"
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
        return False, "the actor is not sold per event"
    if EVENT_NAME not in names:
        if is_ppe is None and not names:
            return None, "the pricing answer is not understood"
        return False, f"{EVENT_NAME} is not priced, rows go out free"
    return True, f"{EVENT_NAME} is priced"


if __name__ == "__main__":
    asyncio.run(main())
