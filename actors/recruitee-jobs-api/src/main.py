"""Recruitee Jobs API: read public Recruitee boards, one dataset row per open offer."""

from __future__ import annotations

import asyncio
import inspect
import os
import re

from apify import Actor

from .client import (
    BoardError,
    fetch_board,
    matches,
    merge_positions,
    normalize_board,
    parse_job,
    sort_key,
)

EVENT = "job-delivered"
SUMMARY_STORE = "recruitee-jobs-api-summary"
# A run that stops for any of these stops everywhere: the next board would hand out rows
# nobody can charge for, and the summary would call that a finished run.
STOP_REASONS = ("spending-cap", "charge-error", "dataset-write-error")

ROW_KEYS = (
    "status", "board", "jobId", "title", "department", "location", "city", "state", "country",
    "countryCode", "locations", "remote", "hybrid", "onSite", "employmentType", "experience",
    "education", "category", "salaryMin", "salaryMax", "salaryCurrency", "salaryPeriod", "tags",
    "publishedAt", "url", "applyUrl", "description", "requirements", "reason", "delivered",
    "billable",
)


def blank_row():
    return {key: None for key in ROW_KEYS}


def run_id():
    value = os.environ.get("ACTOR_RUN_ID")
    if value and value.strip():
        return value.strip()
    try:
        env = Actor.get_env() or {}
    except Exception:
        env = {}
    return str(env.get("run_id") or "run")


async def dataset_count():
    """Rows in the run dataset, or None when the SDK could not tell.

    Dataset.get_metadata() in apify 4.2.0 returns a pydantic model
    (crawlee.storage_clients.models.DatasetMetadata), not a dict: the old
    isinstance(meta, dict) guard returned None on every platform run, so datasetItemCount
    was always null and the "the row landed but the charge failed" branch never fired.
    """
    try:
        dataset = await Actor.open_dataset()
        meta = await dataset.get_metadata()
    except Exception:
        return None
    value = getattr(meta, "item_count", None)
    if value is None and isinstance(meta, dict):
        value = meta.get("item_count")
    return value if isinstance(value, int) else None


async def pricing_info():
    try:
        manager = Actor.get_charging_manager()
        value = manager.get_pricing_info()
        if inspect.isawaitable(value):
            value = await value
        return value
    except Exception:
        return None


def event_is_priced(pricing):
    """True when the price list carries our event, None when we cannot tell.

    apify 4.x answers with an ActorPricingInfo dataclass, not with a dict:
    pricing_model, max_total_charge_usd, is_pay_per_event, per_event_prices.
    Waiting for a dict made every delivered row say billable=null on the platform
    while the charge itself went through.
    """
    if pricing is None:
        return None
    if isinstance(pricing, dict):
        per_event = pricing.get("pricingPerEvent")
        if not isinstance(per_event, dict):
            return None
        events = per_event.get("actorChargeEvents")
        if not isinstance(events, dict):
            return None
        return EVENT in events
    prices = getattr(pricing, "per_event_prices", None)
    if isinstance(prices, dict):
        return EVENT in prices
    if getattr(pricing, "is_pay_per_event", None) is False:
        return False
    return None


async def write_row(row, stats):
    """Write one row through the SDK, which writes the row and charges under one lock.

    Returns 'charged', 'free', 'cap', 'written-uncharged' or 'uncertain'. A retry never
    happens: the SDK writes before it charges, so a repeat could put the same row in the
    dataset twice. When the call raises we ask the dataset how many rows it holds: a row
    that landed is delivered but uncharged, a row that did not land is named unverified
    instead of being counted as delivered.
    """
    try:
        result = await Actor.push_data(row, charged_event_name=EVENT)
    except Exception as exc:
        stats["lastChargeError"] = str(exc)[:200]
        count = await dataset_count()
        if count is not None and count > stats["rowsWritten"]:
            stats["rowsWritten"] = count
            stats["pushRecovered"] += 1
            stats["deliveredUncharged"] += 1
            stats["chargeUncertain"] += 1
            stats["consecutiveChargeFailures"] += 1
            stats["consecutiveWriteFailures"] = 0
            return "written-uncharged"
        stats["jobsUnverified"] += 1
        stats["consecutiveWriteFailures"] += 1
        stats["consecutiveChargeFailures"] = 0
        return "uncertain"

    charged = int(getattr(result, "charged_count", 0) or 0)
    capped = bool(getattr(result, "event_charge_limit_reached", False))
    if capped and charged == 0:
        # The SDK wrote no row at all: nothing was delivered, nothing was charged.
        return "cap"

    stats["rowsWritten"] += 1
    stats["consecutiveChargeFailures"] = 0
    stats["consecutiveWriteFailures"] = 0
    if charged > 0:
        stats["chargedEvents"] += charged
        return "charged"
    stats["deliveredUncharged"] += 1
    return "free"


async def write_status_row(status, board=None, reason=None, stats=None, by_status=None, base=None):
    """A free row that names what was not delivered. Never raises: a broken dataset
    must not turn a status into a traceback."""
    row = dict(base) if isinstance(base, dict) else blank_row()
    for key in ROW_KEYS:
        if key not in row:
            row[key] = None
    row["status"] = status
    row["board"] = board
    row["reason"] = reason
    row["delivered"] = False
    row["billable"] = False
    try:
        await Actor.push_data(row)
    except Exception as exc:
        if stats is not None:
            stats["statusRowFailures"] += 1
            stats["lastChargeError"] = str(exc)[:200]
        return False
    if stats is not None:
        stats["statusRows"] += 1
        stats["rowsWritten"] += 1
    if by_status is not None:
        by_status[status] = by_status.get(status, 0) + 1
    return True


async def mark_not_checked(row, status, reason, stats, by_status):
    """A posting the run never delivered keeps its identity but becomes a free status row."""
    await write_status_row(status, board=row.get("board"), reason=reason,
                           stats=stats, by_status=by_status, base=row)


def board_entry(read=0, unparsable=0, merged=0, filtered=0, seen=0, not_read=0,
                delivered=0, unverified=0, not_processed=0, reason="", skipped=False,
                truncated=False):
    """One board in the summary. truncated means one thing only: this board's list is short.

    The flag is passed in at the moment the list is cut, and truncatedMismatch compares it
    against a recount from the counters: two independent sources, so the check can fail.
    """
    return {
        "read": read, "unparsable": unparsable, "merged": merged, "filtered": filtered,
        "seen": seen, "notRead": not_read, "delivered": delivered,
        "unverified": unverified, "notProcessed": not_processed,
        "truncated": truncated, "skipped": bool(skipped), "reason": reason,
    }


def build_status_message(summary):
    bits = [
        "%d of %d board(s) read: %d position(s), %d delivered"
        % (summary["boardsOk"] + summary["boardsEmpty"], summary["boardsAsked"],
           summary["positionsRead"], summary["delivered"])
    ]
    left_out = []
    if summary["boardsInvalid"]:
        left_out.append("%d not a board identifier" % summary["boardsInvalid"])
    if summary["boardsNotFound"]:
        left_out.append("%d do not exist" % summary["boardsNotFound"])
    if summary["boardsError"]:
        left_out.append("%d could not be read" % summary["boardsError"])
    if summary["boardsSkipped"]:
        left_out.append(
            "%d not checked (%s)" % (summary["boardsSkipped"], summary["stopReason"] or "stopped")
        )
    if left_out:
        bits.append("left out: " + ", ".join(left_out))
    if summary["positionsMerged"]:
        bits.append("%d repeated position id(s) merged into one row" % summary["positionsMerged"])
    if summary["filteredOut"]:
        bits.append("%d did not match the filters" % summary["filteredOut"])
    if summary["positionsUnparsable"]:
        bits.append("%d entr(ies) were not a position" % summary["positionsUnparsable"])
    if summary["positionsNotRead"]:
        bits.append("%d past maxJobsPerBoard" % summary["positionsNotRead"])
    if summary["positionsNotProcessed"]:
        bits.append(
            "%d not processed (%s)" % (summary["positionsNotProcessed"], summary["stopReason"] or "unknown")
        )
    if summary["jobsUnverified"]:
        bits.append("%d row(s) whose write was not confirmed" % summary["jobsUnverified"])
    if summary["chargeUncertain"]:
        bits.append("%d row(s) delivered without a confirmed charge" % summary["chargeUncertain"])
    return "; ".join(bits)


def build_failure_message(summary, last_board_error):
    parts = []
    if summary["boardsInvalid"]:
        parts.append(
            "%d of %d input(s) are not Recruitee board identifiers (a board looks like bunq, "
            "bunq.recruitee.com or https://bunq.recruitee.com/)" % (
                summary["boardsInvalid"], summary["boardsAsked"])
        )
    if summary["boardsNotFound"]:
        parts.append("%d board(s) do not exist on Recruitee" % summary["boardsNotFound"])
    if summary["boardsError"]:
        parts.append(
            "%d board(s) could not be read: %s" % (summary["boardsError"], last_board_error or "unknown error")
        )
    if not parts:
        parts.append(
            "the input carried no board identifier: put at least one Recruitee company name or "
            "career site URL into the boards field"
        )
    return "Nothing was delivered. " + "; ".join(parts) + "."


async def main():
    async with Actor:
        actor_input = await Actor.get_input() or {}

        raw_boards = actor_input.get("boards")
        if isinstance(raw_boards, str):
            raw_boards = [raw_boards]
        if not isinstance(raw_boards, list):
            raw_boards = []

        include_details = actor_input.get("includeDetails", True) is not False
        description_format = actor_input.get("descriptionFormat") or "html"
        if description_format not in ("html", "text"):
            description_format = "html"
        try:
            max_per_board = max(0, int(actor_input.get("maxJobsPerBoard") or 0))
        except (TypeError, ValueError):
            max_per_board = 0
        try:
            timeout = max(5, int(actor_input.get("requestTimeoutSecs") or 60))
        except (TypeError, ValueError):
            timeout = 60

        filters = {
            "remoteOnly": actor_input.get("remoteOnly") is True,
            "department": str(actor_input.get("department") or "").strip().lower(),
            "employmentType": str(actor_input.get("employmentType") or "").strip().lower(),
            "experience": str(actor_input.get("experience") or "").strip().lower(),
            "category": str(actor_input.get("category") or "").strip().lower(),
            "city": str(actor_input.get("city") or "").strip().lower(),
            "publishedAfter": str(actor_input.get("publishedAfter") or "").strip(),
            "keywords": [w.strip().lower() for w in str(actor_input.get("keywords") or "").split(",") if w.strip()],
        }

        targets, seen_boards, invalid = [], set(), []
        duplicates = 0
        for raw in raw_boards:
            sub = normalize_board(raw)
            if sub is None:
                invalid.append(raw if isinstance(raw, str) else str(raw))
                continue
            if sub in seen_boards:
                duplicates += 1
                continue
            seen_boards.add(sub)
            targets.append(sub)

        stats = {
            "delivered": 0, "filteredOut": 0, "positionsOnBoard": 0, "positionsRead": 0,
            "positionsMerged": 0, "positionsSeen": 0, "positionsNotRead": 0,
            "positionsProcessed": 0, "positionsNotProcessed": 0, "jobsUnverified": 0,
            "positionsUnparsable": 0, "statusRows": 0, "statusRowFailures": 0,
            "rowsWritten": 0, "chargedEvents": 0, "deliveredUncharged": 0,
            "chargeUncertain": 0, "pushRecovered": 0, "consecutiveChargeFailures": 0,
            "consecutiveWriteFailures": 0, "lastChargeError": None,
        }
        boards = {"ok": 0, "empty": 0, "notFound": 0, "invalid": len(invalid), "error": 0, "skipped": 0}
        by_board, by_status = {}, {}
        stop_reason = None
        last_board_error = None

        priced = event_is_priced(await pricing_info())
        # billable is decided before the write, because the row cannot be edited afterwards:
        # True only when the price list really carries our event, null when we cannot tell.
        billable = None if priced is None else bool(priced)

        for raw in invalid:
            await write_status_row(
                "INVALID_INPUT", board=str(raw)[:120],
                reason="not a Recruitee board identifier: %r" % (raw,),
                stats=stats, by_status=by_status,
            )

        for sub in targets:
            if stop_reason in STOP_REASONS:
                boards["skipped"] += 1
                reason = "not read: the run stopped on %s" % stop_reason
                by_board[sub] = board_entry(reason=reason, skipped=True, truncated=True)
                await write_status_row("NOT_CHECKED_ERROR", board=sub, reason=reason,
                                       stats=stats, by_status=by_status)
                continue

            try:
                board = await asyncio.to_thread(fetch_board, sub, timeout)
            except BoardError as exc:
                if exc.kind == "not_found":
                    boards["notFound"] += 1
                    status = "NOT_CHECKED_BOARD_NOT_FOUND"
                else:
                    boards["error"] += 1
                    status = "NOT_CHECKED_ERROR"
                    last_board_error = exc.reason
                by_board[sub] = board_entry(reason=exc.reason, truncated=False)
                await write_status_row(status, board=sub, reason=exc.reason, stats=stats, by_status=by_status)
                continue

            raw_positions = board["jobs"]
            stats["positionsOnBoard"] += len(raw_positions)
            if not raw_positions:
                boards["empty"] += 1
                reason = "the board exists and has no open positions"
                by_board[sub] = board_entry(reason=reason, truncated=False)
                await write_status_row("NOT_CHECKED_BOARD_EMPTY", board=sub, reason=reason,
                                       stats=stats, by_status=by_status)
                continue

            boards["ok"] += 1
            rows, unparsable = [], 0
            for entry in raw_positions:
                row = parse_job(entry, sub, include_details, description_format)
                if row is None:
                    unparsable += 1
                    continue
                rows.append(row)
            stats["positionsUnparsable"] += unparsable

            rows, merged_here = merge_positions(rows)
            stats["positionsMerged"] += merged_here
            stats["positionsRead"] += len(rows)

            if not rows:
                reason = "the board answered, but none of its %d entr(ies) was a position" % len(raw_positions)
                by_board[sub] = board_entry(unparsable=unparsable, merged=merged_here,
                                            reason=reason, truncated=False)
                await write_status_row("NOT_CHECKED_ERROR", board=sub, reason=reason,
                                       stats=stats, by_status=by_status)
                continue

            matched = [row for row in rows if matches(row, filters)]
            filtered_here = len(rows) - len(matched)
            matched.sort(key=sort_key, reverse=True)
            not_read_here = max(0, len(matched) - max_per_board) if max_per_board else 0
            processed = matched[:max_per_board] if max_per_board else matched

            stats["filteredOut"] += filtered_here
            stats["positionsSeen"] += len(matched)
            stats["positionsNotRead"] += not_read_here
            stats["positionsProcessed"] += len(processed)

            if not matched:
                # A run where every position was filtered out wrote nothing at all: the buyer
                # saw an empty dataset and no reason. One free row names it.
                await write_status_row(
                    "NOT_CHECKED_NO_MATCH", board=sub,
                    reason="no position matched the filters",
                    stats=stats, by_status=by_status,
                )

            board_delivered, board_unverified, board_not_processed = 0, 0, 0
            for index, row in enumerate(processed):
                row["status"] = "DELIVERED"
                row["delivered"] = True
                row["billable"] = billable
                outcome = await write_row(row, stats)
                if outcome == "cap":
                    stop_reason = "spending-cap"
                    rest = processed[index:]
                    reason = "the spending cap stopped the run before this row"
                    for pending in rest:
                        await mark_not_checked(pending, "NOT_CHECKED_CAP", reason, stats, by_status)
                    board_not_processed += len(rest)
                    stats["positionsNotProcessed"] += len(rest)
                    break
                if outcome == "uncertain":
                    board_unverified += 1
                    if stats["consecutiveWriteFailures"] >= 2:
                        stop_reason = "dataset-write-error"
                        rest = processed[index + 1:]
                        reason = "the dataset refused the write; the rest was not checked"
                        for pending in rest:
                            await mark_not_checked(pending, "NOT_CHECKED_ERROR", reason, stats, by_status)
                        board_not_processed += len(rest)
                        stats["positionsNotProcessed"] += len(rest)
                        break
                    continue
                stats["delivered"] += 1
                board_delivered += 1
                if stats["consecutiveChargeFailures"] >= 2:
                    stop_reason = "charge-error"
                    rest = processed[index + 1:]
                    reason = "repeated charge failures; the rest was not checked"
                    for pending in rest:
                        await mark_not_checked(pending, "NOT_CHECKED_ERROR", reason, stats, by_status)
                    board_not_processed += len(rest)
                    stats["positionsNotProcessed"] += len(rest)
                    break

            if stop_reason == "spending-cap":
                reason = "stopped on the spending cap after %d row(s)" % board_delivered
            elif stop_reason == "charge-error":
                reason = "stopped after repeated charge failures"
            elif stop_reason == "dataset-write-error":
                reason = "stopped after the dataset refused the write"
            elif not_read_here:
                reason = "maxJobsPerBoard kept the newest %d of %d matching position(s)" % (
                    max_per_board, len(matched))
            elif filtered_here and not matched:
                reason = "no position matched the filters"
            else:
                reason = "read in full"
            by_board[sub] = board_entry(
                read=len(rows), unparsable=unparsable, merged=merged_here,
                filtered=filtered_here, seen=len(matched), not_read=not_read_here,
                delivered=board_delivered, unverified=board_unverified,
                not_processed=board_not_processed, reason=reason,
                truncated=bool(not_read_here or board_not_processed),
            )

        rid = run_id()
        summary = {
            "runId": rid,
            "boardsAsked": len(targets) + len(invalid),
            "boardsOk": boards["ok"], "boardsEmpty": boards["empty"],
            "boardsNotFound": boards["notFound"], "boardsInvalid": boards["invalid"],
            "boardsError": boards["error"], "boardsSkipped": boards["skipped"],
            "boardsDeduplicated": duplicates,
            "positionsOnBoard": stats["positionsOnBoard"], "positionsRead": stats["positionsRead"],
            "positionsMerged": stats["positionsMerged"],
            "positionsSeen": stats["positionsSeen"], "positionsNotRead": stats["positionsNotRead"],
            "positionsProcessed": stats["positionsProcessed"],
            "positionsNotProcessed": stats["positionsNotProcessed"],
            "jobsUnverified": stats["jobsUnverified"],
            "positionsUnparsable": stats["positionsUnparsable"],
            "filteredOut": stats["filteredOut"], "delivered": stats["delivered"],
            "statusRows": stats["statusRows"], "statusRowFailures": stats["statusRowFailures"],
            "chargedEvents": stats["chargedEvents"],
            "deliveredUncharged": stats["deliveredUncharged"],
            "chargeUncertain": stats["chargeUncertain"],
            "pushRecovered": stats["pushRecovered"],
            "lastChargeError": stats["lastChargeError"],
            "stopReason": stop_reason,
            "byBoard": by_board, "byStatus": by_status,
        }

        summary["pricingEventPriced"] = priced
        summary["billingActive"] = (stats["chargedEvents"] > 0) if stats["delivered"] else None

        flagged = sum(1 for entry in by_board.values() if entry.get("truncated"))
        derived = sum(
            1 for entry in by_board.values()
            if (entry.get("notRead") or 0) + (entry.get("notProcessed") or 0) > 0
            or entry.get("skipped") is True
        )
        summary["truncatedBoards"] = flagged
        if flagged != derived:
            summary["truncatedMismatch"] = derived

        mismatches = []
        if summary["boardsAsked"] != (
            boards["ok"] + boards["empty"] + boards["notFound"] + boards["invalid"]
            + boards["error"] + boards["skipped"]
        ):
            mismatches.append("boards")
        if stats["positionsOnBoard"] != (
            stats["positionsUnparsable"] + stats["positionsMerged"]
            + stats["filteredOut"] + stats["positionsSeen"]
        ):
            mismatches.append("positionsOnBoard")
        if stats["positionsSeen"] != stats["positionsNotRead"] + stats["positionsProcessed"]:
            mismatches.append("positionsSeen")
        if stats["positionsProcessed"] != (
            stats["delivered"] + stats["jobsUnverified"] + stats["positionsNotProcessed"]
        ):
            mismatches.append("positionsProcessed")
        if mismatches:
            summary["countersMismatch"] = mismatches

        expected_rows = stats["delivered"] + stats["statusRows"]
        summary["datasetItemsExpected"] = expected_rows
        count = await dataset_count()
        summary["datasetItemCountRaw"] = count
        if count == expected_rows:
            summary["datasetItemCount"] = count
        else:
            summary["datasetItemCount"] = None
            if count is None:
                summary["datasetItemCountNote"] = (
                    "the dataset counter could not be read, so the row count is unknown; "
                    "the counters above come from this run, not from the dataset"
                )
            else:
                if count > expected_rows and stats["jobsUnverified"]:
                    summary["datasetItemCountNote"] = (
                        "%d row(s) whose write was not confirmed may also be in the dataset" % stats["jobsUnverified"]
                    )
                elif count < expected_rows:
                    summary["datasetItemCountNote"] = "metadata lag: expected %d, reports %d" % (expected_rows, count)
                else:
                    summary["datasetItemCountNote"] = (
                        "metadata reports %d row(s), this run accounted for %d" % (count, expected_rows)
                    )

        await Actor.set_value("RUN_SUMMARY", summary)
        try:
            store = await Actor.open_key_value_store(name=SUMMARY_STORE)
            # Only the per-run key: a bare RUN_SUMMARY here is overwritten by every other run
            # of this Actor, so a buyer could read someone else's numbers.
            await store.set_value("RUN_SUMMARY-%s" % rid, summary)
        except Exception as exc:
            summary["summaryStoreError"] = str(exc)[:200]
            await Actor.set_value("RUN_SUMMARY", summary)

        if summary["boardsAsked"]:
            await Actor.set_status_message(build_status_message(summary))

        if boards["ok"] + boards["empty"] == 0:
            await Actor.fail(status_message=build_failure_message(summary, last_board_error))


if __name__ == "__main__":
    asyncio.run(main())
