"""Apify Actor: pump.fun new token monitor, read from the Solana chain itself.

Input  : `durationSecs`, `maxTokens`, optional `minDevBuyPercent`, `maxDevBuyPercent`,
         `onlyNewCreators`, `nameContains`, `symbolContains`, `includeDevBuy`,
         `rpcUrl`, `rpcWsUrl`.
Output : one row per pump.fun creation: mint, name, symbol, uri, creator, createdAt,
         the creator's buy at birth, the bonding curve at that second, and the
         transaction signature.
Money  : one charge per delivered token row, and nothing for a quiet minute, a filtered
         token, a duplicate or a row that failed to write. The platform no longer offers
         monthly rental to new Actors, so the row is the unit of sale; the buyer sets the
         spending cap and the run stops at it, saying so in the summary. A row is written
         and charged in one SDK call, so nothing is ever paid for and not delivered.

Honesty rules that hold in code, not in promises:
* a run that received nothing, or received notifications but no creation, fails with a
  human message naming the endpoint; it never ends green with an empty dataset;
* `devBuyTokens` is None when the transaction could not be read: unknown is not zero;
* reserves are None when the event's numbers do not describe a fresh curve, and the
  summary counts those rows: a measured 3 of 8 launches carried a virtual-SOL figure
  no newborn curve can hold;
* tokens dropped by filters or unparsable events are counted in the summary, never
  written as empty rows.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
import time
from importlib.metadata import PackageNotFoundError, version as _pkg_version

import httpx
from apify import Actor

from src import feed, pump

SUMMARY_STORE = "pump-fun-new-token-monitor-summary"
# the paid event: one per row written to the dataset
CHARGE_EVENT = "token-delivered"
STATUS_MESSAGE_LIMIT = 400
# The platform kills a run at ACTOR_TIMEOUT_AT. An automatic check gives a fresh Actor
# five minutes, and a 300 s watch window plus container start does not fit: on 07.11 at
# 20:42 the check run was killed as TIMED-OUT before it could write a summary. The window
# is therefore clamped to the platform deadline, leaving this much room for the summary.
PLATFORM_MARGIN_SECS = 20


def _platform_budget_secs() -> float | None:
    """Seconds left before the platform kills this run, or None when it does not say."""
    raw = os.environ.get("ACTOR_TIMEOUT_AT")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    left = moment.timestamp() - time.time()
    return left if left > 0 else 0.0
# measured: pump.fun transactions are version 1, and the public endpoint refuses them
# when the request says maxSupportedTransactionVersion 0
TX_VERSION = 1
# the pace of one RPC call lives in feed.RPC_PACE_SECS: it is the buyer's lever
# (rpcPaceSecs), and the default there is the only interval measured to pass cleanly



def _dep_versions() -> str:
    out = []
    for name in ("apify", "httpx", "websockets"):
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


def _text_contains(haystack, needle: str) -> bool:
    if not needle:
        return True
    return needle in str(haystack or "").lower()


def _row(event: dict, dev_tokens, percent, prior: int, now: float) -> dict:
    created = event.get("timestamp") or 0
    age = None
    if created > 0:
        age = max(0.0, round(now - float(created), 3))
    real_sol = event.get("realSolReserves")
    return {
        "mint": event.get("mint"),
        "name": event.get("name") or "",
        "symbol": event.get("symbol") or "",
        "uri": event.get("uri") or "",
        "creator": event.get("creator") or event.get("user") or "",
        "createdAt": event.get("createdAt"),
        "ageSeconds": age,
        "devBuyTokens": dev_tokens,
        "devBuyPercent": percent,
        "virtualSolReserves": event.get("virtualSolReserves"),
        "realSolReserves": real_sol,
        "curveProgressPercent": pump.curve_progress_percent(real_sol),
        "creatorTokensThisRun": prior,
        "signature": event.get("signature"),
        "source": event.get("source") or "stream",
    }


async def _item_count() -> int | None:
    """Rows the run dataset holds now, or None when it cannot be asked."""
    try:
        dataset = await Actor.open_dataset()
        info = None
        for getter in ("get_metadata", "get_info"):
            method = getattr(dataset, getter, None)
            if method is not None:
                info = await method()
                break
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
    except Exception:  # noqa: BLE001 - a local run may carry no dataset handle
        return None


async def _detect_billing(log) -> bool:
    """Whether this run can charge at all.

    Measured on apify 4.2.0: prices live on the charging manager, not on the Actor. A run with
    no pricing at all (local runs, test doubles) keeps billing on and simply cannot charge; a
    run whose pricing has no CHARGE_EVENT delivers rows free and says so, instead of reading
    "nothing was charged" as a spending cap.
    """
    try:
        pricing = Actor.get_charging_manager().get_pricing_info()
    except Exception:  # noqa: BLE001 - local run or a double without a charging manager
        return True
    if pricing is None:
        return True
    prices = getattr(pricing, "per_event_prices", None) or {}
    if not getattr(pricing, "is_pay_per_event", False):
        log.error("this Actor is not pay-per-event: rows are delivered without a charge")
        return False
    if CHARGE_EVENT not in prices:
        log.error("pricing has no %r event: rows are delivered without a charge", CHARGE_EVENT)
        return False
    return True


async def _push(row: dict, summary: dict, log, billing: bool):
    """Write a row and charge for it in the same call.

    The SDK stores the row and charges it under one lock, so a row cannot be paid for and then
    lost. Returns (delivered, cap_hit): when the spending cap lets nothing through, the SDK
    writes nothing, so the row is not delivered and the run stops instead of giving it away.
    """
    before = await _item_count()
    charged = 0
    cap = False
    error = None
    for attempt in (1, 2):
        try:
            if billing:
                result = await Actor.push_data(row, charged_event_name=CHARGE_EVENT)
                charged = int(getattr(result, "charged_count", 0) or 0)
                cap = bool(getattr(result, "event_charge_limit_reached", False))
            else:
                await Actor.push_data(row)
                charged = 0
                cap = False
            error = None
            break
        except Exception as exc:  # noqa: BLE001
            error = exc
            log.error("dataset write failed for %s (attempt %d): %s", row.get("mint"), attempt, exc)
    if error is None:
        if charged:
            summary["chargedEvents"] += charged
        elif billing:
            summary["deliveredUncharged"] += 1
        if cap and not charged:
            return False, True
        return True, cap
    after = await _item_count()
    if before is not None and after is not None and after <= before:
        summary["datasetWriteErrors"] += 1
        return False, False
    if before is None or after is None:
        summary["datasetWriteUnverified"] = summary.get("datasetWriteUnverified", 0) + 1
    return True, False


async def _set_status_message(text: str, log) -> None:
    """Tell the buyer in the run header why a green run delivered nothing.

    A green empty result with no explanation reads as a broken actor, and the reason
    must live where the operator looks, not only in the log.

    Measured in apify 4.2.0 (apify/_actor.py): set_status_message is `async def`. A call
    without await returns a coroutine, sends nothing and leaves one RuntimeWarning in the
    log; the Store card promised "says in the run header that the filter did it" and the
    header stayed empty, while every test was green because the double was synchronous.
    """
    if not hasattr(Actor, "set_status_message"):
        log.info("status: %s", text)
        return
    try:
        await Actor.set_status_message(text[:STATUS_MESSAGE_LIMIT])
    except Exception as exc:  # noqa: BLE001
        log.warning("could not set the status message: %s", exc)


async def main() -> None:
    async with Actor:
        log = Actor.log
        log.info("deps: %s", _dep_versions())

        actor_input = await Actor.get_input() or {}
        duration = int(_num(actor_input.get("durationSecs"), 90, 10, 3600))
        max_tokens = int(_num(actor_input.get("maxTokens"), 200, 0, 5000))
        min_dev = _num(actor_input.get("minDevBuyPercent"), 0.0, 0.0, 100.0)
        max_dev = _num(actor_input.get("maxDevBuyPercent"), 100.0, 0.0, 100.0)
        only_new = bool(actor_input.get("onlyNewCreators", False))
        name_contains = str(actor_input.get("nameContains") or "").strip().lower()
        symbol_contains = str(actor_input.get("symbolContains") or "").strip().lower()
        include_dev = bool(actor_input.get("includeDevBuy", True))
        rpc_url = str(actor_input.get("rpcUrl") or "").strip() or pump.RPC_HTTP
        rpc_ws = str(actor_input.get("rpcWsUrl") or "").strip() or pump.RPC_WS
        rpc_pace = _num(actor_input.get("rpcPaceSecs"), feed.RPC_PACE_SECS, 0.0, 10.0)

        if min_dev > max_dev:
            await Actor.fail(status_message=(
                "minDevBuyPercent (%s) is above maxDevBuyPercent (%s), so no token could ever "
                "match. Swap the two numbers or clear one of them." % (min_dev, max_dev)))
            return
        if not rpc_ws.startswith(("ws://", "wss://")):
            await Actor.fail(status_message=(
                "rpcWsUrl must start with ws:// or wss:// and it is %r. Leave it empty to use the "
                "public endpoint." % rpc_ws[:80]))
            return
        if not rpc_url.startswith(("http://", "https://")):
            await Actor.fail(status_message=(
                "rpcUrl must start with http:// or https:// and it is %r. Leave it empty to use "
                "the public endpoint." % rpc_url[:80]))
            return

        billing = await _detect_billing(log)
        dev_filter_set = min_dev > 0 or max_dev < 100
        if dev_filter_set and not include_dev:
            log.warning("the creator-buy filter needs includeDevBuy: every token will be kept")

        summary = {
            "durationSecs": duration,
            "maxTokens": max_tokens,
            "tokensSeen": 0,
            "rowsDelivered": 0,
            "filteredOut": 0,
            "filteredBy": {},
            "skippedUnparsable": 0,
            "devBuyUnknown": 0,
            "reservesUnavailable": 0,
            "duplicatesSkipped": 0,
            "notifications": 0,
            "signaturesSeen": 0,
            "signaturesRead": 0,
            "signaturesUnread": 0,
            "chainGapSecs": None,
            "unparsableCreations": 0,
            "rowsUndelivered": 0,
            "queuedNotDelivered": 0,
            "streamFailures": 0,
            "mode": "stream",
            "stopReason": None,
            "rpcUrl": rpc_url,
            "rpcWsUrl": rpc_ws,
            "rpcPaceSecs": rpc_pace,
            "datasetWriteErrors": 0,
            "includeDevBuy": include_dev,
            "chargedEvents": 0,
            "deliveredUncharged": 0,
            "billingActive": billing,
            "delivery": "one charge per delivered token row",
            "filters": {
                "minDevBuyPercent": min_dev,
                "maxDevBuyPercent": max_dev,
                "onlyNewCreators": only_new,
                "nameContains": name_contains,
                "symbolContains": symbol_contains,
            },
        }
        stats = {
            "mode": "stream",
            "streamFailures": 0,
            "seenSignatures": {},
            "lastFeedError": None,
            "notifications": 0,
            "paceSecs": rpc_pace,
        }

        queue: asyncio.Queue = asyncio.Queue()
        seen_mints: set[str] = set()
        creator_counts: dict[str, int] = {}
        started = time.monotonic()
        last_creation = started
        # never overrun the platform deadline: a killed run writes neither rows nor summary
        summary["durationSecsRequested"] = duration
        budget = _platform_budget_secs()
        if budget is not None and duration > budget - PLATFORM_MARGIN_SECS:
            duration = max(10, int(budget - PLATFORM_MARGIN_SECS))
            summary["durationCappedByPlatform"] = True
        else:
            summary["durationCappedByPlatform"] = False
        summary["durationSecs"] = duration
        deadline = started + duration
        stop_reason = None
        last_rpc_at = 0.0

        limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
        async with httpx.AsyncClient(limits=limits) as client:
            cfg = {"rpcUrl": rpc_url, "rpcWsUrl": rpc_ws}
            producer = asyncio.create_task(feed.produce(queue, stats, cfg, log, client))

            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        stop_reason = "duration-reached"
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=min(remaining, 5.0))
                    except asyncio.TimeoutError:
                        continue

                    mint = event.get("mint")
                    if not mint:
                        summary["skippedUnparsable"] += 1
                        continue
                    if mint in seen_mints:
                        summary["duplicatesSkipped"] += 1
                        continue
                    seen_mints.add(mint)
                    summary["tokensSeen"] += 1
                    last_creation = time.monotonic()
                    if not event.get("reservesKnown", False):
                        summary["reservesUnavailable"] += 1

                    creator = event.get("creator") or event.get("user") or ""
                    prior = creator_counts.get(creator, 0)
                    creator_counts[creator] = prior + 1

                    dev_tokens = None
                    tx = event.get("chainTx")
                    if include_dev and tx is not None:
                        # the poll fallback already opened this transaction for its logs:
                        # reading it again would pay the rate limit twice for one fact
                        dev_tokens = pump.dev_buy_tokens(
                            tx, creator, mint, alt_owner=event.get("user"))
                    elif include_dev and event.get("signature"):
                        wait = rpc_pace - (time.monotonic() - last_rpc_at)
                        if wait > 0:
                            await asyncio.sleep(wait)
                        last_rpc_at = time.monotonic()
                        try:
                            tx = await feed.rpc_call(client, rpc_url, "getTransaction",
                                                     [event.get("signature"), {
                                                         "encoding": "jsonParsed",
                                                         "maxSupportedTransactionVersion": TX_VERSION,
                                                         "commitment": "confirmed",
                                                     }])
                            dev_tokens = pump.dev_buy_tokens(
                                tx, creator, mint, alt_owner=event.get("user"))
                        except Exception as exc:  # noqa: BLE001 - an unread buy is unknown, not fatal
                            log.warning("could not read the creator buy for %s: %s", mint, exc)
                            dev_tokens = None
                    percent = pump.dev_buy_percent(dev_tokens, event.get("totalSupply"))
                    if dev_tokens is None:
                        summary["devBuyUnknown"] += 1

                    reason = None
                    if not _text_contains(event.get("name"), name_contains):
                        reason = "name"
                    elif not _text_contains(event.get("symbol"), symbol_contains):
                        reason = "symbol"
                    elif only_new and prior > 0:
                        reason = "notNewCreator"
                    elif dev_tokens is not None and percent is not None and (
                            percent < min_dev or percent > max_dev):
                        reason = "devBuy"
                    if reason:
                        summary["filteredOut"] += 1
                        summary["filteredBy"][reason] = summary["filteredBy"].get(reason, 0) + 1
                        continue

                    row = _row(event, dev_tokens, percent, prior, time.time())
                    delivered, cap_hit = await _push(row, summary, log, billing)
                    if delivered:
                        summary["rowsDelivered"] += 1
                    else:
                        summary["rowsUndelivered"] += 1
                    if cap_hit:
                        # the cap let nothing through, the SDK wrote no row: stop and say why
                        stop_reason = "spending-cap"
                        break
                    if max_tokens and summary["rowsDelivered"] >= max_tokens:
                        stop_reason = "max-tokens"
                        break
            finally:
                producer.cancel()
                # a producer stuck in a websocket teardown must not hold the summary back:
                # the platform kills the run without one, which is how the first check died
                await asyncio.wait({producer}, timeout=5)

        summary["elapsedSecs"] = round(time.monotonic() - started, 2)
        summary["silentForSecs"] = round(time.monotonic() - last_creation, 1)
        summary["streamFailures"] = stats.get("streamFailures", 0)
        summary["mode"] = stats.get("mode", "stream")
        # what the feed heard (socket messages, listed signatures), not what this loop turned
        # into rows: a live socket with no creation read as a dead endpoint while this counted
        # creations, and the run blamed the RPC for a quiet minute on mainnet.
        summary["notifications"] = stats.get("notifications", 0)
        summary["signaturesSeen"] = stats.get("signaturesSeen", 0)
        summary["signaturesRead"] = stats.get("signaturesRead", 0)
        summary["signaturesUnread"] = stats.get("signaturesUnread", 0)
        summary["chainGapSecs"] = stats.get("chainGapSecs")
        summary["unparsableCreations"] = stats.get("unparsableCreations", 0)
        summary["queuedNotDelivered"] = queue.qsize()
        summary["stopReason"] = stop_reason
        api_count = await _item_count()
        if api_count is not None and api_count < summary["rowsDelivered"]:
            # dataset metadata lags seconds behind the writes: re-read once, and when it still
            # trails, say so, because a plain smaller number reads as lost rows
            await asyncio.sleep(2)
            again = await _item_count()
            if again is not None:
                api_count = again
            if api_count < summary["rowsDelivered"]:
                # a number we know is behind is not a measurement: report unknown, not smaller
                summary["datasetItemCountLag"] = True
                api_count = None
        summary["datasetItemCount"] = api_count
        if stats.get("lastFeedError"):
            summary["lastFeedError"] = stats["lastFeedError"]

        try:
            store = await Actor.open_key_value_store(name=SUMMARY_STORE)
            await store.set_value("RUN_SUMMARY", summary)
            run_id = (
                os.environ.get("ACTOR_RUN_ID")
                or Actor.get_env().get("run_id")
                or "run"
            )
            await store.set_value("RUN_SUMMARY-%s" % run_id, summary)
        except Exception as exc:  # noqa: BLE001
            log.warning("the named summary store is unavailable: %s", exc)
        try:
            run_store = await Actor.open_key_value_store()
            await run_store.set_value("RUN_SUMMARY", summary)
        except Exception as exc:  # noqa: BLE001
            log.warning("the run summary could not be stored: %s", exc)

        log.info("done: seen %s, rows %s, filtered %s, reserves unknown %s, mode %s, "
                 "notifications %s, stop %s",
                 summary["tokensSeen"], summary["rowsDelivered"], summary["filteredOut"],
                 summary["reservesUnavailable"], summary["mode"], summary["notifications"],
                 stop_reason)

        if summary["rowsDelivered"] == 0:
            if summary["tokensSeen"] == 0 and summary["notifications"] == 0:
                await Actor.fail(status_message=(
                    "The RPC sent nothing in %d seconds: no notification from %s and no answer from "
                    "%s. The endpoint is down, rate limiting, or blocked from this network; put your "
                    "own rpcUrl and rpcWsUrl into the input and run again. Nothing was delivered."
                    % (duration, rpc_ws, rpc_url)))
            elif (summary["tokensSeen"] == 0 and summary["signaturesSeen"] > 0
                  and summary["signaturesRead"] == 0):
                # live: with rpcPaceSecs 0.3 the public endpoint answered 429 to every
                # getTransaction, and the run blamed the program address instead of the pace
                await Actor.fail(status_message=(
                    "The HTTP fallback listed %d signatures in %d seconds and could not open a "
                    "single transaction, so no creation could be read. Last feed answer: %s. "
                    "Nothing was delivered. Raise rpcPaceSecs (now %s) or put your own rpcUrl "
                    "into the input."
                    % (summary["signaturesSeen"], duration,
                       summary.get("lastFeedError") or "not recorded", rpc_pace)))
            elif summary["tokensSeen"] == 0:
                await Actor.fail(status_message=(
                    "%d notifications came from %s in %d seconds and not one of them was a pump.fun "
                    "creation. That is not a normal minute on mainnet, so the program address or the "
                    "endpoint is wrong. Nothing was delivered."
                    % (summary["notifications"], rpc_ws, duration)))
            elif summary["filteredOut"] == summary["tokensSeen"]:
                await _set_status_message(
                    "Watched %d seconds: %d new token(s) seen, all dropped by your filters %s. "
                    "This is your filter, not a broken feed." % (
                        duration, summary["tokensSeen"], summary["filteredBy"]), log)
            else:
                await _set_status_message(
                    "%d new token(s) were seen in %d seconds and none could be delivered "
                    "(filtered %d, write failures %d, unparsable %d, still queued %d). "
                    "The summary says which."
                    % (summary["tokensSeen"], duration, summary["filteredOut"],
                       summary["rowsUndelivered"], summary["unparsableCreations"],
                       summary["queuedNotDelivered"]), log)


if __name__ == "__main__":
    asyncio.run(main())
