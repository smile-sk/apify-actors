"""App Details API: one row per app, read from the two open stores.

Money rules, learned the hard way in this park:
  * A paid row is written and charged in ONE call, Actor.push_data(rows,
    charged_event_name=...). A separate charge before the write leaves a row that
    the buyer paid for and cannot download.
  * The batch is cut by the remaining budget BEFORE the write: the SDK silently
    writes and charges only the prefix that fits, and counters built from the
    batch size would then report sales that never happened.
  * Anything that is not an app detail comes back free, with a status and a plain
    reason, and carries no fields a buyer could use as data.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone

from apify import Actor

from . import stores

ST_OK = "OK"
ST_INVALID = "INVALID_INPUT"
ST_DUPLICATE = "DUPLICATE"
ST_NOT_FOUND = "NOT_FOUND"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"

EVENT_NAME = "app-detail"
MONEY_STOPS = {"spending-cap", "billing-price-unknown", "charge-error"}
BATCH_SIZE = 10
APP_COST_SECONDS = 4.0
DEADLINE_MARGIN_SECONDS = 20.0
DEFAULT_COUNTRY = "us"
DEFAULT_LANG = "en"
DEFAULT_MAX_APPS = 25
MAX_APPS_CEILING = 100
ANDROID_TIMEOUT_SECONDS = 20.0
_COUNTRY_RE = re.compile(r"^[a-zA-Z]{2}$")


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

    get_pricing_info() is synchronous in the SDK: awaiting it silently zeroes the
    whole money path.
    """
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001
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

    Dataset.get_metadata() answers a pydantic DatasetMetadata with item_count on it,
    not a dict: reading it as a dict silently gives None and hides a refused write.
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


async def _item_count_after(dataset, tries: int = 3, pause: float = 0.4) -> int | None:
    """The dataset counter after a write: read a few times, keep the highest reading.

    Platform metadata lags the write, so one reading taken right after push_data can
    still show the old number and turn a landed batch into a false "nothing was
    delivered". Three readings over about a second cost nothing and remove most of that
    lag. A lag longer than this window still reads as zero; that boundary is named in
    the README instead of being hidden.
    """
    readings: list[int] = []
    for attempt in range(max(1, tries)):
        count = await _item_count(dataset)
        if count is not None:
            readings.append(count)
        if attempt + 1 < max(1, tries):
            await asyncio.sleep(pause)
    return max(readings) if readings else None


async def main() -> None:
    await Actor.init()
    started = now_iso()

    data = await Actor.get_input() or {}

    raw_apps = data.get("apps")
    if isinstance(raw_apps, str):
        wanted = [raw_apps]
    elif isinstance(raw_apps, list):
        wanted = [str(item) for item in raw_apps]
    else:
        wanted = []
    if not wanted and data.get("app"):
        wanted = [str(data.get("app"))]
    tasks = [item.strip() for item in wanted if item and str(item).strip()]
    if not tasks:
        await Actor.fail(status_message=(
            "No apps were sent. Pass App Store ids, Google Play package names or store "
            "links in the apps field, for example {\"apps\": [\"389801252\", "
            "\"com.instagram.android\"]}."))
        return

    raw_country = str(data.get("country") or DEFAULT_COUNTRY).strip()
    if not _COUNTRY_RE.match(raw_country):
        await Actor.fail(status_message=(
            f"The country {raw_country!r} is not a two letter store country. Pass a "
            "code such as us, gb, de or jp: a full name is not accepted, both stores "
            "expect the code."))
        return
    country = raw_country.lower()
    lang = str(data.get("lang") or DEFAULT_LANG).strip().lower()[:5] or DEFAULT_LANG
    platform_mode = str(data.get("platform") or "both").strip().lower()
    if platform_mode not in ("both", "ios", "android"):
        platform_mode = "both"
    include_description = bool(data.get("includeDescription"))
    max_apps = _limit(data.get("maxApps"), DEFAULT_MAX_APPS, 1, MAX_APPS_CEILING)
    if len(tasks) > max_apps:
        cut = tasks[max_apps:]
        tasks = tasks[:max_apps]
    else:
        cut = []

    deadline = platform_deadline()
    if deadline is not None:
        left = time_left(deadline) or 0.0
        if left < DEADLINE_MARGIN_SECONDS + APP_COST_SECONDS:
            await Actor.fail(status_message=(
                f"The run has {int(left)} s left before the platform stops it, which is "
                "less than one app needs. Send fewer apps or raise the timeout."))
            return

    charging = Actor.get_charging_manager()
    can_charge, price_note = price_state(charging)
    billable_claim = None if can_charge is None else bool(can_charge)

    import httpx

    dataset = await Actor.open_dataset()
    client = httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=False,
                          headers={"User-Agent":
                                   "AppDetailsApi/0.1 (+https://apify.com)"})
    counts = {
        "appsAttempted": 0,
        "appsRead": 0,
        "appsNotFound": 0,
        "appsInvalid": 0,
        "appsDuplicate": 0,
        "appsFailed": 0,
        "appsLimited": len(cut),
        "appsCutByBudget": 0,
        "appsCutByStop": 0,
        "appsCutByDeadline": 0,
        "appsDelivered": 0,
        "appsCapped": 0,
    }
    by_status: dict[str, int] = {}
    seen_inputs: set[str] = set()
    seen_resolved: set[str] = set()
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

    def note(status: str, amount: int = 1) -> None:
        by_status[status] = by_status.get(status, 0) + amount

    async def free_status(value: str, index: int, status: str, reason: str) -> None:
        """A row that carries a reason and nothing a buyer could use as data."""
        nonlocal rows_written, write_errors, stopping, stop_reason, dataset_note
        row = {
            "actorInput": value, "actorIndex": index, "platform": None, "appId": None,
            "storeId": None, "title": None, "developer": None, "rating": None,
            "ratingsCount": None, "installs": None, "genres": None, "version": None,
            "iconUrl": None, "storeUrl": None, "status": status, "reason": reason,
            "billable": False,
        }
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
        nonlocal delivered_unverified
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
            counts["appsDelivered"] += len(rows)
            delivered_uncharged += len(rows)
            note(ST_OK, len(rows))
            return
        if can_charge is None:
            stopping = True
            stop_reason = stop_reason or "billing-price-unknown"
            for row in rows:
                await free_status(row.get("actorInput") or "", int(row.get("actorIndex") or 0),
                                  ST_BILLING, f"not delivered and not charged: {price_note}")
            return
        if charging.is_event_charge_limit_reached(EVENT_NAME):
            stopping = True
            stop_reason = stop_reason or "spending-cap"
            for row in rows:
                counts["appsCapped"] += 1
                await free_status(row.get("actorInput") or "", int(row.get("actorIndex") or 0),
                                  ST_CAP, "the spending cap was reached: not delivered and "
                                          "not charged")
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
                counts["appsCapped"] += 1
                await free_status(row.get("actorInput") or "", int(row.get("actorIndex") or 0),
                                  ST_CAP, "the remaining budget could not be computed: not "
                                          "delivered and not charged")
            return
        pay, over = rows[:max(0, budget)], rows[max(0, budget):]
        if over:
            stopping = True
            stop_reason = stop_reason or "spending-cap"
            for row in over:
                counts["appsCapped"] += 1
                await free_status(row.get("actorInput") or "", int(row.get("actorIndex") or 0),
                                  ST_CAP, "the remaining budget ended before this app: not "
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
            # Only the dataset counter tells the two apart: calling the batch delivered
            # without reading it reports sales of rows a buyer cannot download.
            after = await _item_count_after(dataset)
            grew = None if before is None or after is None else after - before
            if grew is None:
                # The counter stayed silent: the rows may be in the dataset and the
                # charge may have gone through. Saying "nothing was delivered" here
                # sends the buyer into a second run that pays twice.
                delivered_unverified += len(pay)
                charge_uncertain += len(pay)
                stopping = True
                stop_reason = "charge-error"
                dataset_note = ("the charge raised and the dataset counter could not be "
                                "read: delivery and the charge are both unknown, check "
                                "the dataset before re-running "
                                f"({type(exc).__name__})")
            elif grew >= len(pay):
                charge_uncertain += len(pay)
                delivered_uncharged += len(pay)
                counts["appsDelivered"] += len(pay)
                rows_written += len(pay)
                note(ST_OK, len(pay))
                stopping = True
                stop_reason = "charge-error"
                dataset_note = ("a charge failed: the rows stay in the dataset and "
                                "are counted in chargeUncertain, no second row is "
                                "written")
            elif grew > 0:
                # Part of the batch landed: those rows are delivered and no sale is
                # claimed for them, the rest is unknown.
                counts["appsDelivered"] += grew
                rows_written += grew
                note(ST_OK, grew)
                delivered_uncharged += grew
                charge_uncertain += grew
                delivered_unverified += len(pay) - grew
                stopping = True
                stop_reason = "dataset-write-error"
                dataset_note = (f"the charge raised and the dataset counter grew by "
                                f"{grew} of {len(pay)}: delivery of the rest is unknown "
                                "and nothing is claimed as sold "
                                f"({type(exc).__name__})")
            else:
                write_errors += 1
                delivered_unverified += len(pay)
                stopping = True
                stop_reason = "dataset-write-error"
                dataset_note = ("the dataset refused the rows: nothing was delivered "
                                f"and nothing was charged ({type(exc).__name__})")
            return
        charged = getattr(result, "charged_count", None)
        if charged is None:
            delivered_uncharged += len(pay)
            counts["appsDelivered"] += len(pay)
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
        counts["appsDelivered"] += len(pay)
        rows_written += len(pay)
        note(ST_OK, len(pay))
        delivered_uncharged += len(pay) - charged

    async def flush() -> None:
        nonlocal batch
        if batch:
            rows, batch = batch, []
            await write_paid(rows)

    for index, raw in enumerate(tasks):
        lowered = raw.lower()
        if lowered in seen_inputs:
            counts["appsDuplicate"] += 1
            await free_status(raw, index, ST_DUPLICATE,
                              "the same app was already sent in this run")
            continue
        seen_inputs.add(lowered)

        kind, value, why = stores.classify(raw)
        if kind is None:
            counts["appsInvalid"] += 1
            await free_status(raw, index, ST_INVALID, why)
            continue
        if stopping:
            if stop_reason in MONEY_STOPS:
                counts["appsCutByBudget"] += 1
                await free_status(raw, index, ST_CAP,
                                  "the run stopped on money before this app: not read")
            else:
                counts["appsCutByStop"] += 1
                await free_status(raw, index, ST_ERROR,
                                  f"the run stopped ({stop_reason or 'error'}) before "
                                  "this app: not read")
            continue
        left = time_left(deadline)
        if left is not None and left < APP_COST_SECONDS * 2:
            counts["appsCutByDeadline"] += 1
            stop_reason = stop_reason or "deadline"
            await free_status(raw, index, ST_TIME,
                              f"the run had {int(left)} s left: not read")
            continue

        if kind == "ios":
            plans = [("ios", {"app_id": value})]
        elif kind == "android":
            plans = [("android", {"package": value})]
        elif platform_mode == "ios":
            plans = [("ios", {"bundle_id": value})]
        elif platform_mode == "android":
            plans = [("android", {"package": value})]
        else:
            plans = [("android", {"package": value}), ("ios", {"bundle_id": value})]

        counts["appsAttempted"] += 1
        record = None
        used = ""
        reasons: list[str] = []
        for store_name, kwargs in plans:
            if store_name == "ios":
                record, reason = stores.apple_lookup(
                    client, country=country, **kwargs)
            else:
                # The Play client is blocking and its own http call has no timeout:
                # a hung connection would sit past the platform deadline and the run
                # would be killed with no summary at all.
                try:
                    record, reason = await asyncio.wait_for(
                        asyncio.to_thread(stores.android_lookup, kwargs["package"],
                                          country=country, lang=lang),
                        timeout=ANDROID_TIMEOUT_SECONDS)
                except (asyncio.TimeoutError, TimeoutError):
                    record, reason = None, (
                        "Google Play did not answer within "
                        f"{int(ANDROID_TIMEOUT_SECONDS)} s")
            if record is not None:
                used = store_name
                break
            reasons.append(reason)
        if record is None:
            joined = "; ".join(reasons) or "no store answered"
            if reasons and all("has no app" in text for text in reasons):
                counts["appsNotFound"] += 1
                await free_status(raw, index, ST_NOT_FOUND, joined)
            else:
                counts["appsFailed"] += 1
                await free_status(raw, index, ST_ERROR, joined)
            continue

        resolved = (f"{used}:{record.get('trackId') or record.get('appId')}").lower()
        if resolved in seen_resolved:
            counts["appsDuplicate"] += 1
            await free_status(raw, index, ST_DUPLICATE,
                              "the same store app was already delivered in this run")
            continue
        seen_resolved.add(resolved)

        if used == "ios":
            row = stores.row_apple(record, raw=raw, index=index, country=country,
                                   with_description=include_description)
        else:
            row = stores.row_android(record, raw=raw, index=index, country=country,
                                     with_description=include_description)
        counts["appsRead"] += 1
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            await flush()

    await flush()
    client.close()

    for position, raw in enumerate(cut, start=len(tasks)):
        await free_status(raw, position, ST_LIMIT,
                          f"maxApps is {max_apps}: this app was not attempted")

    accounted = sum(by_status.values())
    mismatch = accounted != len(tasks) + len(cut)
    if charged_total + delivered_uncharged != counts["appsDelivered"]:
        mismatch = True

    summary = {
        "appsRequested": len(tasks) + len(cut),
        "appsAttempted": counts["appsAttempted"],
        "appsRead": counts["appsRead"],
        "appsNotFound": counts["appsNotFound"],
        "appsInvalid": counts["appsInvalid"],
        "appsDuplicate": counts["appsDuplicate"],
        "appsFailed": counts["appsFailed"],
        "appsLimited": counts["appsLimited"],
        "appsCutByBudget": counts["appsCutByBudget"],
        "appsCutByStop": counts["appsCutByStop"],
        "appsCutByDeadline": counts["appsCutByDeadline"],
        "appsDelivered": counts["appsDelivered"],
        "appsCapped": counts["appsCapped"],
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
        "country": country,
        "lang": lang,
        "platform": platform_mode,
        "includeDescription": include_description,
        "maxApps": max_apps,
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
            store = await Actor.open_key_value_store(name="app-details-summary")
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

    told = (f"{counts['appsDelivered']} app(s) delivered, {charged_total} charged, "
            f"{rows_written - counts['appsDelivered']} free status row(s)")
    if stop_reason:
        told += f", stopped: {stop_reason}"
    await Actor.set_status_message(told)

    if counts["appsRead"] == 0 and rows_written == 0:
        await Actor.fail(status_message=(
            "Nothing was read and nothing was written: the input held no usable app and "
            "no status row could be created."))
        return


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
