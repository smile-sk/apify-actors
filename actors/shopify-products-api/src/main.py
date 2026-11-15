"""Apify Actor: Shopify products API, catalog with prices and stock, and a change monitor.

Input  : `stores` (domains or product URLs), `maxProductsPerStore`, `onlyChanges`,
         `inStockOnly`, `minPrice`, `maxPrice`, `nameContains`, `includeVariants`,
         `requestPaceSecs`.
Output : one row per product: price (lowest variant), priceMax, compareAtPrice, currency,
         availability, variants, tags, productUrl, and when onlyChanges is on, the change.
Money  : PAY_PER_EVENT, one charge per delivered product row. A product the store publishes
         without a price, a blocked store, a failed page, a filtered product and an unchanged
         product cost nothing. A row is written and charged in one SDK call, so nothing is
         paid for and not delivered.

Honesty rules that live in code, not in promises:
* a store that refuses every path is a row with the HTTP code and a reason, never an empty
  dataset that reads as an empty shop;
* a product without a price is a NO_PRICE row and is not charged;
* a 3xx answer is never parsed as a page;
* the first onlyChanges run on a store is a baseline: every product is new, and the summary
  says how many stores were baselined;
* a write that raised and a dataset counter that did not move is an unknown outcome, not a
  lost row and not a delivered one.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from importlib.metadata import PackageNotFoundError, version as _pkg_version

import httpx
from apify import Actor

from src import shopify

SUMMARY_STORE = "shopify-products-api-summary"
STATE_STORE = "shopify-products-api-state"
CHARGE_EVENT = "product-delivered"
STATUS_MESSAGE_LIMIT = 400
CHANGE_KINDS = ("new", "price-down", "price-up", "out-of-stock", "back-in-stock")


def _dep_versions() -> str:
    out = []
    for name in ("apify", "httpx"):
        try:
            out.append("%s=%s" % (name, _pkg_version(name)))
        except PackageNotFoundError:
            out.append("%s=?" % name)
    return " ".join(out)


def _num(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def change_kind(previous, row: dict):
    """What changed since the stored snapshot: kind and the price it had.

    Returns (kind, previousPrice). Stock comes before price: a product that went out of stock
    and got cheaper in the same run matters more as a stock signal, and one string per row
    cannot carry both honestly.
    """
    if previous is None:
        return "new", None
    was_available = previous.get("availability")
    now_available = row.get("availability")
    if was_available and now_available and was_available != now_available:
        return ("back-in-stock" if now_available == "in_stock" else "out-of-stock"), None
    old_price = previous.get("price")
    new_price = row.get("price")
    if old_price is not None and new_price is not None and old_price != new_price:
        return ("price-down" if new_price < old_price else "price-up"), old_price
    return None, None


def filter_reason(row: dict, settings: dict):
    """Why this product is dropped, or None to keep it."""
    if settings["in_stock_only"] and row.get("availability") != "in_stock":
        return "inStockOnly"
    price = row.get("price")
    if price is not None:
        if settings["min_price"] and price < settings["min_price"]:
            return "minPrice"
        if settings["max_price"] and price > settings["max_price"]:
            return "maxPrice"
    needle = settings["name_contains"]
    if needle and needle not in str(row.get("title") or "").lower():
        return "nameContains"
    return None


async def _item_count():
    """Rows the run dataset holds now, or None when it cannot be asked."""
    try:
        dataset = await Actor.open_dataset()
    except Exception:  # noqa: BLE001 - a local run may carry no dataset handle
        return None
    info = None
    for getter in ("get_metadata", "get_info"):
        method = getattr(dataset, getter, None)
        if method is None:
            continue
        try:
            info = await method()
            break
        except Exception:  # noqa: BLE001
            continue
    if info is None:
        return None
    if isinstance(info, dict):
        for key in ("item_count", "itemCount"):
            if info.get(key) is not None:
                return int(info[key])
        return None
    for attr in ("item_count", "itemCount"):
        value = getattr(info, attr, None)
        if value is not None:
            return int(value)
    return None


async def _detect_billing(log) -> bool:
    """Whether this run can charge at all.

    Measured on apify 4.2.0: prices live on the charging manager, not on the Actor. A run with
    no pricing (local runs, test doubles) keeps billing on and simply cannot charge; a run
    whose pricing has no CHARGE_EVENT delivers rows free and logs why, instead of reading
    "nothing was charged" as a spending cap.
    """
    try:
        pricing = Actor.get_charging_manager().get_pricing_info()
    except Exception:  # noqa: BLE001 - local run or a double without a charging manager
        return False
    if pricing is None:
        log.info("no pricing is configured for this run: rows are delivered without a charge")
        return False
    prices = getattr(pricing, "per_event_prices", None) or {}
    if not getattr(pricing, "is_pay_per_event", False):
        log.error("this Actor is not pay-per-event: rows are delivered without a charge")
        return False
    if CHARGE_EVENT not in prices:
        log.error("pricing has no %r event: rows are delivered without a charge", CHARGE_EVENT)
        return False
    return True


async def _push(row: dict, summary: dict, log, billing: bool, state: dict):
    """Write a row and charge for it in the same call. Returns (delivered, cap, outcome).

    `outcome` is "charged" (in the dataset and paid for), "free" (in the dataset and not
    charged: no billing in this run or the budget let the row through for nothing) or
    "unverified" (the write raised and the dataset counter could not confirm the row). A
    "unverified" row is neither delivered nor lost, and the caller counts it as such.

    Measured in apify 4.2.0: push_data stores the row and then charges it under one lock, so
    an exception can arrive AFTER the row is already in the dataset. Repeating such a write
    duplicates the row for the buyer, so a paid write is never repeated: one attempt, and the
    dataset counter decides what landed. A free write is retried once, but only after the
    counter shows the first attempt did not land: measured 07.11.2026, a blind repeat put the
    same row in the dataset twice.
    """
    attempts = 1 if billing else 2
    for attempt in range(1, attempts + 1):
        try:
            if billing:
                result = await Actor.push_data(row, charged_event_name=CHARGE_EVENT)
                charged = int(getattr(result, "charged_count", 0) or 0)
                cap = bool(getattr(result, "event_charge_limit_reached", False))
                if charged:
                    summary["chargedEvents"] += charged
                    state["written"] += 1
                    return True, cap, "charged"
                if cap:
                    # measured: when nothing may be written, the SDK writes nothing
                    return False, True, "free"
                summary["deliveredUncharged"] += 1
                state["written"] += 1
                return True, False, "free"
            await Actor.push_data(row)
            state["written"] += 1
            return True, False, "free"
        except Exception as exc:  # noqa: BLE001
            summary["writeFailures"] += 1
            log.error("dataset write failed for %s (attempt %d of %d): %s",
                      row.get("handle") or row.get("store"), attempt, attempts, exc)
            if billing:
                return await _after_write_failure(summary, log, state, billing=True)
            # A free write can fail after the row is already stored, so the dataset counter is
            # asked before repeating it: a blind repeat puts the same row in twice.
            after = await _item_count()
            if after is not None and after > state["written"]:
                state["written"] = after
                return True, False, "free"
            if attempt == attempts:
                return await _after_write_failure(summary, log, state, billing=False)
    return False, False, "unverified"


async def _after_write_failure(summary: dict, log, state: dict, billing: bool = True):
    """What a failed write left behind, decided by the dataset counter and nothing else.

    Measured 07.11.2026: dataset metadata lags the write, so a counter that has not moved is
    not a lost row. One short re-read is taken; if the row still does not show, the outcome is
    "unverified" and the caller counts it as such. Calling it lost was the same class of lie
    as calling an unwritten row delivered: the buyer could see the row in the dataset while
    the summary counted it as missing.
    """
    after = await _item_count()
    if after is not None and after <= state["written"]:
        await asyncio.sleep(2)
        after = await _item_count()
    if after is not None and after > state["written"]:
        state["written"] = after
        summary["deliveredUncharged"] += 1
        if billing:
            summary["chargeUncertain"] += 1
        return True, False, "free"
    if billing:
        summary["chargeUncertain"] += 1
    return False, False, "unverified"


async def _set_status_message(text: str, log) -> None:
    """Tell the buyer in the run header why a green run delivered nothing.

    Measured in apify 4.2.0: set_status_message is a coroutine. A call without await returns
    it, sends nothing, and leaves a RuntimeWarning; the header stays empty while every test
    is green if the double in the test is synchronous.
    """
    if not hasattr(Actor, "set_status_message"):
        log.info("status: %s", text)
        return
    try:
        await Actor.set_status_message(text[:STATUS_MESSAGE_LIMIT])
    except Exception as exc:  # noqa: BLE001
        log.warning("could not set the status message: %s", exc)


def _row_for_store(result: dict) -> dict:
    """One row that says a store could not be read, and why."""
    not_shopify = result.get("status") == "not-shopify"
    return {
        "status": "NOT_CHECKED_NOT_SHOPIFY" if not_shopify else "NOT_CHECKED_BLOCKED",
        "store": result.get("store"),
        "handle": result.get("handle"),
        "title": None,
        "price": None,
        "priceMax": None,
        "compareAtPrice": None,
        "currency": result.get("currency"),
        "availability": None,
        "variantCount": None,
        "variants": [],
        "productType": None,
        "vendor": None,
        "tags": [],
        "productUrl": None,
        "imageUrl": None,
        "updatedAt": None,
        "source": None,
        "change": None,
        "previousPrice": None,
        "reason": result.get("error") or result.get("note") or "the store was not readable",
        "count": None,
        "httpStatus": result.get("httpStatus"),
    }


def _store_stats(result: dict) -> dict:
    """Per-store counters, the shape the summary publishes under productsByStore.

    The README promised "how many products were read per store" and the summary had no such
    counter: a shop cut in half by maxProductsPerStore looked exactly like a shop that has that
    many products. `truncated` is the flag that separates the two, and `skippedOffsite` is the
    count of addresses the store named that this run refused to fetch.
    """
    return {
        "status": result.get("status"),
        "source": result.get("source"),
        "read": len(result.get("products") or []),
        "delivered": 0,
        "undelivered": 0,
        "unverified": 0,
        "notChecked": 0,
        "noPrice": 0,
        "filteredOut": 0,
        "pages": int(result.get("pages") or 0),
        "pagesFailed": int(result.get("pagesFailed") or 0),
        "skippedOffsite": int(result.get("skippedOffsite") or 0),
        "truncated": bool(result.get("truncated")),
        "truncatedReason": result.get("truncatedReason"),
    }


def _store_key(host: str) -> str:
    """A store host with a leading www. dropped: a shop and its www copy are one shop."""
    name = str(host or "").lower()
    return name[4:] if name.startswith("www.") else name


async def main() -> None:
    async with Actor:
        log = Actor.log
        log.info("deps: %s", _dep_versions())

        actor_input = await Actor.get_input() or {}
        raw_stores = actor_input.get("stores") or []
        if isinstance(raw_stores, str):
            raw_stores = [line for line in raw_stores.splitlines() if line.strip()]
        # Measured 2026-11-07: the same store typed twice used to be read twice and charged
        # twice. A store and its www copy are one store; two product URLs of one store are
        # two different targets and both stay - unless the whole catalog of that store is in
        # the input as well, and then the product URL is the same work a second time.
        planned = []
        for raw in raw_stores:
            target = shopify.normalize_target(raw)
            if target is None:
                key = "raw:" + str(raw).strip().lower()
            else:
                # The full host with a leading www. dropped: a store and its www copy are one
                # store, while shop1.example.com and shop2.example.com are two different shops.
                # Measured 07.11.2026: a registrable-domain key merged those two and the second
                # shop disappeared with all its products, counted only as one dedup.
                key = _store_key(target["store"])
                if target.get("handle"):
                    key = key + "/products/" + target["handle"].lower()
            planned.append((raw, target, key))
        catalogued = {_store_key(target["store"]) for _, target, _ in planned
                      if target is not None and not target.get("handle")}
        deduped = []
        seen_targets = set()
        stores_deduplicated = 0
        for raw, target, key in planned:
            if (target is not None and target.get("handle")
                    and _store_key(target["store"]) in catalogued):
                # the catalog of this store is read anyway, so the product URL would deliver
                # and charge the same product a second time (measured 07.11.2026: a.com and
                # a.com/products/x in one input gave 2 rows and 2 charges for one product)
                stores_deduplicated += 1
                continue
            if key in seen_targets:
                stores_deduplicated += 1
                continue
            seen_targets.add(key)
            deduped.append((raw, target))
        raw_stores = deduped
        max_products = int(_num(actor_input.get("maxProductsPerStore"), 250, 0, 10000))
        only_changes = bool(actor_input.get("onlyChanges", False))
        in_stock_only = bool(actor_input.get("inStockOnly", False))
        min_price = _num(actor_input.get("minPrice"), 0.0, 0.0, 1_000_000_000.0)
        max_price = _num(actor_input.get("maxPrice"), 0.0, 0.0, 1_000_000_000.0)
        name_contains = str(actor_input.get("nameContains") or "").strip().lower()
        include_variants = bool(actor_input.get("includeVariants", True))
        pace = _num(actor_input.get("requestPaceSecs"), 0.5, 0.0, 30.0)

        settings = {
            "in_stock_only": in_stock_only,
            "min_price": min_price,
            "max_price": max_price,
            "name_contains": name_contains,
        }

        billing = await _detect_billing(log)
        started = time.monotonic()
        summary = {
            "storesAsked": len(raw_stores),
            "storesDeduplicated": stores_deduplicated,
            "storesOk": 0,
            "storesBlocked": 0,
            "storesEmpty": 0,
            "invalidInputs": 0,
            "productsSeen": 0,
            "duplicatesSkipped": 0,
            "productsDelivered": 0,
            "noPrice": 0,
            "filteredOut": 0,
            "filteredBy": {},
            "storesNotChecked": 0,
            "productsUndelivered": 0,
            "productsUnverified": 0,
            "productsNotChecked": 0,
            "skippedOffsite": 0,
            "truncatedStores": 0,
            "productsByStore": {},
            "pagesRead": 0,
            "pagesFailed": 0,
            "requests": 0,
            "modeBreakdown": {},
            "changes": 0,
            "changesBy": {},
            "chargedEvents": 0,
            "deliveredUncharged": 0,
            "chargeUncertain": 0,
            "billingActive": billing,
            "writeFailures": 0,
            "stopReason": None,
            "maxProductsPerStore": max_products,
            "onlyChanges": only_changes,
            "requestPaceSecs": pace,
            "baselineStores": 0,
            "lastStoreError": None,
        }
        by_status = {}
        change_counts = {}

        state_store = None
        try:
            state_store = await Actor.open_key_value_store(name=STATE_STORE)
        except Exception as exc:  # noqa: BLE001
            log.warning("the snapshot store is unavailable, onlyChanges reads nothing: %s", exc)

        limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
        timeout = httpx.Timeout(25.0, connect=10.0)
        stop_reason = "completed"
        first_store_at = 0.0
        write_state = {"written": 0}

        async with httpx.AsyncClient(limits=limits, timeout=timeout,
                                     follow_redirects=False) as client:
            fetcher = shopify.Fetcher(client, log)
            for index, (raw, target) in enumerate(raw_stores):
                if index and pace:
                    await asyncio.sleep(pace)
                first_store_at = first_store_at or time.monotonic()
                if target is None:
                    summary["invalidInputs"] += 1
                    row = _row_for_store({"store": str(raw)[:200], "status": "invalid",
                                          "error": "not a store domain or a product URL"})
                    row["status"] = "INVALID_INPUT"
                    await _push(row, summary, log, False, write_state)
                    continue
                if not await asyncio.to_thread(shopify.host_is_public, target["store"]):
                    summary["storesBlocked"] += 1
                    row = _row_for_store({"store": target["store"], "status": "blocked",
                                          "error": "the host resolves into a private or "
                                                   "loopback network and is not read",
                                          "httpStatus": None})
                    row["status"] = "NOT_CHECKED_PRIVATE_TARGET"
                    await _push(row, summary, log, False, write_state)
                    continue

                result = await shopify.read_store(fetcher, target, max_products, log)
                summary["requests"] = fetcher.requests
                summary["skippedOffsite"] += int(result.get("skippedOffsite", 0))
                summary["pagesFailed"] += int(result.get("pagesFailed", 0))
                status = result.get("status")
                key = target["store"]
                if key in summary["productsByStore"]:
                    key = "%s#%d" % (key, index + 1)
                stats = _store_stats(result)
                summary["productsByStore"][key] = stats
                if stats["truncated"]:
                    summary["truncatedStores"] += 1
                if status != "ok":
                    summary["lastStoreError"] = "%s: %s" % (target["store"],
                                                            result.get("error") or result.get("note"))
                if status == "empty":
                    summary["storesEmpty"] += 1
                    continue
                if status != "ok":
                    summary["storesBlocked"] += 1
                    row = _row_for_store(result)
                    delivered, cap_hit, _ = await _push(row, summary, log, False, write_state)
                    by_status[row["status"]] = by_status.get(row["status"], 0) + 1
                    if cap_hit:
                        stop_reason = "spending-cap"
                        break
                    continue

                summary["storesOk"] += 1
                summary["pagesRead"] += int(result.get("pages", 0))
                summary["duplicatesSkipped"] += int(result.get("duplicates", 0))
                source = result.get("source") or "json"
                summary["modeBreakdown"][source] = summary["modeBreakdown"].get(source, 0) + 1

                previous = {}
                baseline = True
                if state_store is not None:
                    try:
                        previous = await state_store.get_value("CATALOG-%s" % target["store"]) or {}
                    except Exception as exc:  # noqa: BLE001
                        log.warning("could not read the snapshot for %s: %s", target["store"], exc)
                        previous = {}
                if previous:
                    baseline = False
                elif only_changes:
                    summary["baselineStores"] += 1

                # The snapshot follows what was READ, not what was delivered: a product hidden
                # by inStockOnly must remember it is out of stock, or a later onlyChanges run
                # sees "in stock" in the snapshot and in the shop, calls it unchanged, and never
                # reports the product coming back. A product the cap stopped on is popped below,
                # and the tail the cap never reached is not in here at all, so both stay new.
                read_snapshot = {}
                for position, product in enumerate(result["products"]):
                    summary["productsSeen"] += 1
                    handle = product.get("handle")
                    if handle:
                        read_snapshot[handle] = {
                            "price": product.get("price"),
                            "availability": product.get("availability"),
                            "title": product.get("title"),
                        }
                    reason = filter_reason(product, settings)
                    if reason:
                        summary["filteredOut"] += 1
                        stats["filteredOut"] += 1
                        summary["filteredBy"][reason] = summary["filteredBy"].get(reason, 0) + 1
                        by_status["FILTERED"] = by_status.get("FILTERED", 0) + 1
                        continue
                    change = None
                    previous_price = None
                    if only_changes:
                        change, previous_price = change_kind(
                            previous.get(handle) if handle else None, product)
                        if change is None:
                            summary["filteredOut"] += 1
                            stats["filteredOut"] += 1
                            summary["filteredBy"]["unchanged"] = (
                                summary["filteredBy"].get("unchanged", 0) + 1)
                            by_status["UNCHANGED"] = by_status.get("UNCHANGED", 0) + 1
                            continue
                        summary["changes"] += 1
                        change_counts[change] = change_counts.get(change, 0) + 1
                    if product.get("price") is None:
                        summary["noPrice"] += 1
                        stats["noPrice"] += 1
                        row = dict(product)
                        row["status"] = "NO_PRICE"
                        row["change"] = change
                        row["previousPrice"] = previous_price
                        row["reason"] = "the store publishes no price for this product"
                        row["count"] = None
                        row["httpStatus"] = None
                        row["variants"] = row.get("variants") if include_variants else []
                        delivered, _, outcome = await _push(row, summary, log, False, write_state)
                        if not delivered and handle:
                            # the row the dataset refused or left unknown stays new
                            read_snapshot.pop(handle, None)
                        by_status["NO_PRICE"] = by_status.get("NO_PRICE", 0) + 1
                        continue
                    row = dict(product)
                    row["status"] = "DELIVERED"
                    row["change"] = change
                    row["previousPrice"] = previous_price
                    row["reason"] = None
                    row["count"] = None
                    row["httpStatus"] = None
                    row["variants"] = row.get("variants") if include_variants else []
                    delivered, cap_hit, outcome = await _push(row, summary, log, billing,
                                                              write_state)
                    if delivered:
                        summary["productsDelivered"] += 1
                        stats["delivered"] += 1
                        by_status["DELIVERED"] = by_status.get("DELIVERED", 0) + 1
                    elif outcome == "unverified":
                        # neither delivered nor lost: the counter could not confirm the write
                        summary["productsUnverified"] += 1
                        stats["unverified"] += 1
                        by_status["UNVERIFIED"] = by_status.get("UNVERIFIED", 0) + 1
                        if handle:
                            read_snapshot.pop(handle, None)
                    else:
                        summary["productsUndelivered"] += 1
                        stats["undelivered"] += 1
                        by_status["UNDELIVERED"] = by_status.get("UNDELIVERED", 0) + 1
                        if handle:
                            read_snapshot.pop(handle, None)
                    if cap_hit:
                        stop_reason = "spending-cap"
                        # the products of this store that were never reached, and every store
                        # after it, are counted instead of disappearing from the summary
                        leftover = len(result["products"]) - position - 1
                        if leftover:
                            summary["productsNotChecked"] += leftover
                            stats["notChecked"] += leftover
                            stats["truncated"] = True
                        remaining = len(raw_stores) - index - 1
                        if remaining:
                            summary["storesNotChecked"] += remaining
                        break

                merged = dict(previous)
                merged.update(read_snapshot)
                if state_store is not None and (merged or previous):
                    try:
                        await state_store.set_value("CATALOG-%s" % target["store"], merged)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("could not store the snapshot for %s: %s", target["store"], exc)
                if stop_reason == "spending-cap":
                    break

        if summary["pagesFailed"]:
            row = _row_for_store({
                "store": "several",
                "status": "blocked",
                "error": "%d product page(s) could not be read, so those products are not in "
                         "this dataset" % summary["pagesFailed"],
                "httpStatus": None,
                "currency": None,
            })
            row["status"] = "NOT_CHECKED_ERROR"
            row["count"] = int(summary["pagesFailed"])
            await _push(row, summary, log, False, write_state)

        # the counters the README promises to add up; a mismatch is shown, not rounded away
        buckets = (summary["productsDelivered"] + summary["noPrice"]
                   + summary["filteredOut"] + summary["productsUndelivered"]
                   + summary["productsUnverified"])
        if buckets != summary["productsSeen"]:
            summary["counterMismatch"] = {"productsSeen": summary["productsSeen"],
                                          "buckets": buckets}
            log.error("counter mismatch: productsSeen %s, buckets %s",
                      summary["productsSeen"], buckets)
        store_buckets = (summary["storesOk"] + summary["storesBlocked"]
                         + summary["storesEmpty"] + summary["invalidInputs"]
                         + summary["storesNotChecked"])
        if store_buckets != summary["storesAsked"]:
            summary["storeCounterMismatch"] = {"storesAsked": summary["storesAsked"],
                                               "buckets": store_buckets}
            log.error("store counter mismatch: storesAsked %s, buckets %s",
                      summary["storesAsked"], store_buckets)

        summary["changesBy"] = change_counts
        summary["stopReason"] = stop_reason
        summary["elapsedSecs"] = round(time.monotonic() - started, 2)
        summary["requests"] = fetcher.requests

        api_count = await _item_count()
        if api_count is not None:
            delivered_rows = sum(count for key, count in by_status.items()
                                 if key not in ("FILTERED", "UNCHANGED", "UNDELIVERED",
                                                "UNVERIFIED"))
            if api_count < delivered_rows:
                await asyncio.sleep(2)
                again = await _item_count()
                if again is not None:
                    api_count = again
                if api_count < delivered_rows:
                    summary["datasetItemCountLag"] = True
                    api_count = None
        summary["datasetItemCount"] = api_count

        try:
            store = await Actor.open_key_value_store(name=SUMMARY_STORE)
            run_id = (os.environ.get("ACTOR_RUN_ID")
                      or Actor.get_env().get("run_id")
                      or "run")
            summary["runId"] = run_id
            await store.set_value("RUN_SUMMARY", summary)
            await store.set_value("RUN_SUMMARY-%s" % run_id, summary)
        except Exception as exc:  # noqa: BLE001
            log.warning("the named summary store is unavailable: %s", exc)
        try:
            run_store = await Actor.open_key_value_store()
            await run_store.set_value("RUN_SUMMARY", summary)
        except Exception as exc:  # noqa: BLE001
            log.warning("the run summary could not be stored: %s", exc)

        log.info("done: stores ok %s, blocked %s, products seen %s, delivered %s, "
                 "no price %s, filtered %s, changes %s, stop %s",
                 summary["storesOk"], summary["storesBlocked"], summary["productsSeen"],
                 summary["productsDelivered"], summary["noPrice"], summary["filteredOut"],
                 summary["changes"], stop_reason)

        if not raw_stores:
            await Actor.fail(status_message=(
                "No stores in the input: `stores` was empty, so nothing could be read. "
                "Put a Shopify domain such as allbirds.com or a product URL into it."))
            return

        if summary["productsDelivered"] == 0:
            if summary["storesOk"] == 0 and summary["storesEmpty"] == 0:
                await Actor.fail(status_message=(
                    "None of the %d store(s) could be read: %s. Each store is reported as a "
                    "row with the HTTP codes, so the dataset shows which path refused. "
                    "Nothing was delivered and nothing was charged."
                    % (summary["storesAsked"] - summary["invalidInputs"],
                       summary.get("lastStoreError") or "no answer")))
            elif summary["productsSeen"] == 0:
                await Actor.fail(status_message=(
                    "The store(s) answered but returned no products at all: %s. An empty "
                    "catalog on a live shop is not normal, so the endpoint is probably "
                    "password-protected or not Shopify. Nothing was delivered."
                    % (summary.get("lastStoreError") or "empty catalog")))
            elif summary["noPrice"] == summary["productsSeen"]:
                await _set_status_message(
                    "%d product(s) read and not one carries a price, so nothing was billable. "
                    "They are in the dataset as NO_PRICE rows." % summary["productsSeen"], log)
            else:
                await _set_status_message(
                    "%d product(s) read in %.1f s and none was delivered: filtered %d %s, "
                    "already seen %d, write failures %d. The summary says which."
                    % (summary["productsSeen"], summary["elapsedSecs"],
                       summary["filteredOut"], summary["filteredBy"],
                       summary["filteredBy"].get("unchanged", 0),
                       summary["writeFailures"]), log)


if __name__ == "__main__":
    asyncio.run(main())
