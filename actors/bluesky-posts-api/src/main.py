"""Bluesky Posts API: a profile's posts as rows, paid per delivered post.

Money rules, the same discipline as the rest of this park:

  * one door for paid rows: Actor.push_data(rows, charged_event_name=...), so a row is
    written and charged by the same call and there is never a charge without a row;
  * the price is read once, synchronously; if it cannot be read the run stops before the
    first payable post and the rest goes out free with a reason;
  * a batch that does not fit the remaining budget is cut here, before the write;
  * a status row never carries a post id, text, author or url, so it can never be
    mistaken for the product;
  * everything that was not sold says why and costs nothing.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from apify import Actor

from . import bsky as bs

ST_OK = "OK"
ST_INVALID = "INVALID_INPUT"
ST_DUPLICATE = "DUPLICATE"
ST_NO_POSTS = "NO_POSTS_FOUND"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_UNVERIFIED = "NOT_CHECKED_CHARGE_UNVERIFIED"

EVENT_NAME = "post-delivered"
SUMMARY_STORE = "bluesky-posts-summary"
BATCH_SIZE = 20
PAGE_COST_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 20.0


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
    money path, and this park has paid for that lesson once already.
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


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}

        raw_actors = raw.get("actors")
        if isinstance(raw_actors, str):
            raw_actors = [raw_actors]
        if not isinstance(raw_actors, list):
            raw_actors = []
        actors: list[str] = []
        for item in raw_actors:
            text = bs.clean(item)
            if text:
                actors.append(text)
        if not actors:
            await Actor.fail(status_message=(
                "actors is empty - pass at least one Bluesky handle, did or profile url "
                "(for example \"bsky.app\"): nothing was read and nothing was charged"))
            return

        max_posts = bs.clamp_max(raw.get("maxPostsPerActor"))
        include_reposts = bool(raw.get("includeReposts"))
        include_replies = bool(raw.get("includeReplies"))

        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()
        can_charge, price_note = price_state(charging)
        billable_claim = None if can_charge is None else bool(can_charge)
        deadline = platform_deadline()

        counts = {
            "actorsRequested": len(actors), "actorsAttempted": 0, "actorsRead": 0,
            "actorsEmpty": 0, "actorsDuplicate": 0, "actorsFailed": 0,
            "actorsInvalid": 0, "actorsLimited": 0, "actorsCutByBudget": 0,
            "postsSeen": 0, "postsDelivered": 0, "postsDuplicate": 0,
            "postsFilteredByKind": 0, "postsWithoutText": 0, "postsLimited": 0,
            "postsCapped": 0, "pagesRead": 0, "pagesFailed": 0,
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
        seen_actors: set[str] = set()
        seen_dids: set[str] = set()
        batch: list[dict] = []

        def note(status: str, amount: int = 1) -> None:
            by_status[status] = by_status.get(status, 0) + amount

        async def free_status(value: str, index: int, status: str, reason: str) -> None:
            """A row that carries a reason and nothing a buyer could use as data."""
            nonlocal rows_written, write_errors, stopping, stop_reason, dataset_note
            row = {"actorInput": value, "actorIndex": index, "postId": None,
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
                counts["postsDelivered"] += len(rows)
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
                    counts["postsCapped"] += 1
                    await free_status(row.get("actorInput") or "",
                                      int(row.get("actorIndex") or 0), ST_CAP,
                                      "the spending cap was reached: not delivered and "
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
                    counts["postsCapped"] += 1
                    await free_status(row.get("actorInput") or "", 0, ST_CAP,
                                      "the remaining budget could not be computed: not "
                                      "delivered and not charged")
                return
            pay, over = rows[:max(0, budget)], rows[max(0, budget):]
            if over:
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in over:
                    counts["postsCapped"] += 1
                    await free_status(row.get("actorInput") or "", 0, ST_CAP,
                                      "the remaining budget ended before this row: not "
                                      "delivered and not charged")
            if not pay:
                return
            for row in pay:
                row["billable"] = billable_claim is not False
            try:
                result = await Actor.push_data(pay, charged_event_name=EVENT_NAME)
            except Exception:  # noqa: BLE001
                # The SDK writes the rows before it charges them, so this batch IS in the
                # dataset: reporting zero delivered would hide real data from the counters.
                # They are delivered, uncharged and not claimed as sold.
                charge_uncertain += len(pay)
                delivered_uncharged += len(pay)
                counts["postsDelivered"] += len(pay)
                rows_written += len(pay)
                note(ST_OK, len(pay))
                for row in pay:
                    row["billable"] = None
                stopping = True
                stop_reason = "charge-error"
                dataset_note = ("a charge failed: the rows stay in the dataset and are "
                                "counted in chargeUncertain, no second row is written")
                return
            charged = getattr(result, "charged_count", None)
            if charged is None:
                for row in pay:
                    row["billable"] = None
                delivered_uncharged += len(pay)
                counts["postsDelivered"] += len(pay)
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
                for row in pay[charged:]:
                    row["billable"] = False
            charged_total += charged
            counts["postsDelivered"] += len(pay)
            rows_written += len(pay)
            note(ST_OK, len(pay))
            delivered_uncharged += len(pay) - charged

        async def flush() -> None:
            nonlocal batch
            if batch:
                rows, batch = batch, []
                await write_paid(rows)

        for index, actor_ref_input in enumerate(actors):
            ref = bs.actor_ref(actor_ref_input)
            key = (ref or actor_ref_input).lower()
            if ref and key in seen_actors:
                counts["actorsDuplicate"] += 1
                await free_status(actor_ref_input, index, ST_DUPLICATE,
                                  "the same profile was already sent in this run")
                continue
            if ref:
                seen_actors.add(key)
            if stopping:
                counts["actorsCutByBudget"] += 1
                await free_status(actor_ref_input, index, ST_CAP,
                                  "the run stopped on money before this profile: not read")
                continue
            left = time_left(deadline)
            if left is not None and left < PAGE_COST_SECONDS * 2:
                counts["actorsCutByBudget"] += 1
                stop_reason = stop_reason or "deadline"
                await free_status(actor_ref_input, index, ST_TIME,
                                  "not enough time was left before the platform deadline")
                continue
            if not ref:
                counts["actorsInvalid"] += 1
                await free_status(actor_ref_input, index, ST_INVALID,
                                  "this is not a Bluesky handle, did or profile url")
                continue

            counts["actorsAttempted"] += 1
            try:
                profile = bs.profile_info(
                    await asyncio.to_thread(bs.post_json, bs.profile_url(ref),
                                            REQUEST_TIMEOUT_SECONDS))
            except Exception as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}: {str(exc)[:120]}"
                counts["actorsFailed"] += 1
                page_errors.append(f"{ref}: {detail}")
                await free_status(actor_ref_input, index, ST_ERROR,
                                  f"the profile could not be read: {detail}")
                continue
            if not profile.get("did"):
                counts["actorsInvalid"] += 1
                await free_status(actor_ref_input, index, ST_INVALID,
                                  "the profile could not be resolved: check the handle")
                continue
            # The input can name one profile in three forms (handle, did, url). The source
            # confirms the identity as a did, so dedup on that and not on the typed text.
            did_key = str(profile.get("did") or "").strip().lower()
            if did_key and did_key in seen_dids:
                counts["actorsDuplicate"] += 1
                await free_status(actor_ref_input, index, ST_DUPLICATE,
                                  "the same profile was already sent in this run under "
                                  "another form (handle, did or url)")
                continue
            if did_key:
                seen_dids.add(did_key)

            collected = 0
            seen_ids: set[str] = set()
            cursor: str | None = None
            failed = ""
            filtered_here = 0
            pages_here = 0
            # A page that arrived is not a page that paid: a first page where everything is
            # filtered still carries a cursor, so the run must keep asking instead of
            # calling an open profile empty. The cap stops a feed that never ends.
            pages_limit = max(3, (max_posts + bs.FEED_PAGE - 1) // bs.FEED_PAGE + 2)
            while collected < max_posts:
                left = time_left(deadline)
                if left is not None and left < PAGE_COST_SECONDS:
                    dataset_note = dataset_note or "the deadline cut the profile short"
                    stop_reason = stop_reason or "deadline"
                    break
                try:
                    url = bs.feed_url(ref, cursor=cursor, include_replies=include_replies)
                    payload = await asyncio.to_thread(bs.post_json, url, REQUEST_TIMEOUT_SECONDS)
                    entries, cursor = bs.parse_page(payload)
                except Exception as exc:  # noqa: BLE001
                    failed = f"{type(exc).__name__}: {str(exc)[:120]}"
                    counts["pagesFailed"] += 1
                    page_errors.append(f"{ref}: {failed}")
                    break
                counts["pagesRead"] += 1
                pages_here += 1
                added_here = 0
                for item in entries:
                    if collected >= max_posts:
                        break
                    counts["postsSeen"] += 1
                    row = bs.parse_post(item, actor_ref_input, profile, index,
                                        include_reposts, include_replies)
                    if row is None:
                        counts["postsFilteredByKind"] += 1
                        filtered_here += 1
                        continue
                    if row["postId"] in seen_ids:
                        counts["postsDuplicate"] += 1
                        continue
                    if not bs.shares_anything(row):
                        counts["postsWithoutText"] += 1
                        continue
                    seen_ids.add(row["postId"])
                    row["billable"] = billable_claim is not False
                    batch.append(row)
                    collected += 1
                    added_here += 1
                    if len(batch) >= BATCH_SIZE:
                        await flush()
                    if stopping:
                        break
                if stopping or not cursor or pages_here >= pages_limit:
                    break

            if failed:
                counts["actorsFailed"] += 1
                await free_status(actor_ref_input, index, ST_ERROR,
                                  f"the feed could not be read: {failed}")
                continue
            if collected == 0:
                counts["actorsEmpty"] += 1
                if filtered_here:
                    await free_status(
                        actor_ref_input, index, ST_NO_POSTS,
                        f"the feed returned {filtered_here} post(s) and every one of them "
                        "was filtered out by includeReposts/includeReplies: nothing was "
                        "delivered and nothing was charged")
                else:
                    await free_status(actor_ref_input, index, ST_NO_POSTS,
                                      "the profile is open and its feed returned no posts")
                continue
            counts["actorsRead"] += 1
            if collected >= max_posts:
                # The limit is a paid boundary, not a free sample: it gets its own row with
                # a reason, and nothing beyond it is read or sold.
                counts["actorsLimited"] += 1
                counts["postsLimited"] += 1
                await free_status(
                    actor_ref_input, index, ST_LIMIT,
                    f"maxPostsPerActor={max_posts} was reached for this profile: more posts "
                    "exist at the source and were neither read nor charged")
            await flush()

        await flush()

        summary = {
            "actorsRequested": counts["actorsRequested"],
            "actorsAttempted": counts["actorsAttempted"],
            "actorsRead": counts["actorsRead"],
            "actorsEmpty": counts["actorsEmpty"],
            "actorsDuplicate": counts["actorsDuplicate"],
            "actorsFailed": counts["actorsFailed"],
            "actorsInvalid": counts["actorsInvalid"],
            "actorsLimited": counts["actorsLimited"],
            "actorsCutByBudget": counts["actorsCutByBudget"],
            "postsSeen": counts["postsSeen"],
            "postsDelivered": counts["postsDelivered"],
            "postsDuplicate": counts["postsDuplicate"],
            "postsFilteredByKind": counts["postsFilteredByKind"],
            "postsWithoutText": counts["postsWithoutText"],
            "postsCapped": counts["postsCapped"],
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
            "maxPostsPerActor": max_posts,
            "includeReposts": include_reposts,
            "includeReplies": include_replies,
            "counterMismatch": (counts["postsDelivered"] != (charged_total + delivered_uncharged)),
            "stopReason": stop_reason,
            "datasetNote": dataset_note,
            "finishedAt": now_iso(),
        }
        await Actor.set_status_message(
            f"{counts['postsDelivered']} post(s) delivered and {charged_total} charged"
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
