"""Apify Actor: phone number validation and formatting, bulk, offline.

Input  : `numbers` (list) or `text` (pasted list), optional `defaultCountry`.
Output : one row per input line, the line echoed exactly as given, validity,
         type, country, formats, duplicate marker and the reason for a reject.
Charge : `number-validated` - one event per delivered row that carries a new
         valid number. Invalid lines, unparsed lines, duplicates and extra
         numbers found inside one line cost the buyer nothing.

Money = delivery: each paid row is written and billed by ONE call
`push_data(row, charged_event_name=...)`, so a row can never be billed without
being in the dataset. `billable` in the row is decided BEFORE the write from the
price list (true / false / null when the price cannot be read); it is never a
claim about what was actually billed.
"""
from __future__ import annotations

import inspect
import asyncio
import os
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from apify import Actor

from src import phone

CHARGE_EVENT = "number-validated"
MAX_ROWS = 10_000
MAX_EXTRA_REPORTED = 5_000
SUMMARY_STORE = "phone-validator-summary"


def _dep_versions() -> str:
    """Which dependency versions this build really runs: pins need a live run, not a guess."""
    out = []
    for name in ("apify", "phonenumbers"):
        try:
            out.append(f"{name}={_pkg_version(name)}")
        except PackageNotFoundError:
            out.append(f"{name}=?")
    return " ".join(out)


def _run_id() -> str:
    """Id of this run: env first, then the SDK (it names the key `run_id`)."""
    rid = (os.environ.get("ACTOR_RUN_ID") or "").strip()
    if not rid:
        try:
            rid = str((Actor.get_env() or {}).get("run_id") or "").strip()
        except Exception:                                    # noqa: BLE001
            rid = ""
    return rid or "run"


async def event_is_priced(event: str) -> bool | None:
    """Is `event` in the price list? True / False / None when the price is unreadable.

    SDK 4.2.0: `get_charging_manager()` and `get_pricing_info()` are SYNCHRONOUS
    and return a dataclass `ActorPricingInfo`. Awaiting them raises TypeError, which a
    silent except would turn into "price unknown" and free rows, so both are called
    without await and awaited only if a future SDK returns an awaitable.
    The dict shape is legacy.
    """
    try:
        manager = Actor.get_charging_manager()
        if inspect.isawaitable(manager):
            manager = await manager
        pricing = manager.get_pricing_info()
        if inspect.isawaitable(pricing):
            pricing = await pricing
    except Exception:                                        # noqa: BLE001
        return None
    per = getattr(pricing, "per_event_prices", None)
    if per is None:
        ppe = getattr(pricing, "pricing_per_event", None)
        per = getattr(ppe, "actor_charge_events", None) if ppe is not None else None
    if per is None and isinstance(pricing, dict):
        per = (pricing.get("pricingPerEvent") or {}).get("actorChargeEvents")
    if not isinstance(per, dict):
        return None
    return event in per


def _empty_row(index: int, cell: str, status: str, reason: str) -> dict:
    return {
        "index": index,
        "inputAsGiven": cell,
        "status": status,
        "reason": reason,
        "valid": False,
        "possible": False,
        "type": "unknown",
        "countryCode": None,
        "countryName": "",
        "callingCode": "",
        "e164": "",
        "international": "",
        "national": "",
        "rfc3966": "",
        "extension": None,
        "smsCapable": False,
        "timeZones": [],
        "regionDescription": "",
        "carrier": "",
        "candidatesFound": 0,
        "otherNumbers": [],
        "duplicateOfIndex": None,
        "billable": False,
        "billingInactive": False,
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


async def main() -> None:
    async with Actor:
        Actor.log.info("deps: %s", _dep_versions())
        run_id = _run_id()
        actor_input = await Actor.get_input() or {}

        try:
            default_region = phone.resolve_default_region(actor_input.get("defaultCountry"))
        except ValueError as exc:
            await Actor.fail(status_message=str(exc))
            return

        lines = phone.lines_from_input(actor_input.get("numbers"), actor_input.get("text"))
        if not lines:
            await Actor.fail(status_message=(
                "No phone numbers given. Put one number per line in 'Numbers to validate', "
                "or paste a list into 'text'. For local numbers without a country code, "
                "set 'Default country' (ISO-2), for example UA."
            ))
            return

        head = lines[:MAX_ROWS]
        tail = lines[MAX_ROWS:]
        reported_tail = tail[:MAX_EXTRA_REPORTED]

        priced = await event_is_priced(CHARGE_EVENT)

        rows: list[dict] = []
        first_index: dict[str, int] = {}
        charged_events = 0
        capped = 0
        not_checked_error = 0
        delivered_uncharged = 0
        extra_numbers_free = 0
        charge_errors: list[str] = []
        billing_active = True
        stop = False
        stop_reason = ""
        cancelled = False
        processed = 0

        def build_summary(partial: bool) -> dict:
            by_status: dict[str, int] = {}
            for row in rows:
                by_status[row["status"]] = by_status.get(row["status"], 0) + 1
            return {
                "runId": run_id,
                "inputLines": len(lines),
                "rows": len(rows),
                "byStatus": by_status,
                "validRows": sum(1 for row in rows if row.get("valid")),
                "possibleOnlyRows": sum(1 for row in rows if row["status"] == "POSSIBLE_ONLY"),
                "invalidRows": sum(1 for row in rows if row["status"] in ("INVALID", "NO_NUMBER")),
                "duplicateRows": sum(1 for row in rows if row.get("duplicateOfIndex")),
                "uniqueValidNumbers": len(first_index),
                "extraNumbersFree": extra_numbers_free,
                "chargedEvents": charged_events,
                "cappedNotCharged": capped,
                "notCheckedError": not_checked_error,
                "deliveredUncharged": delivered_uncharged,
                "billingActive": (None if priced is None else bool(billing_active and priced)),
                "pricingEventPriced": priced,
                "truncatedLines": len(tail),
                "extraLinesReported": len(reported_tail),
                "chargeErrors": charge_errors,
                "stopReason": stop_reason or ("completed" if not partial else "in-progress"),
                "cancelled": cancelled,
                "defaultCountry": default_region,
                "perRowMoneyDetail": ("the dataset shows the price claim per row (billable); "
                                      "what was actually charged is counted here, in chargedEvents "
                                      "and deliveredUncharged"),
                "billingModel": (
                    "number-validated: one event per delivered row that carries a new valid "
                    "number. Invalid lines, unparsed lines, duplicates and extra numbers inside "
                    "one line are free."
                ),
                "partial": partial,
                "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }

        async def write_summary(partial: bool) -> None:
            payload = build_summary(partial)
            # named store: the run-scoped key only, because the store is shared by the whole
            # account and a plain name alone would be overwritten by the next run
            for key in (f"RUN_SUMMARY-{run_id}",):
                try:
                    store = await Actor.open_key_value_store(name=SUMMARY_STORE)
                    await store.set_value(key, payload)
                except Exception as exc:                     # noqa: BLE001
                    Actor.log.warning("summary in named store failed: %s", exc)
            try:
                await Actor.set_value("RUN_SUMMARY", payload)
            except Exception as exc:                         # noqa: BLE001
                Actor.log.warning("summary in run store failed: %s", exc)

        try:
            for index, cell in enumerate(head, 1):
                if stop:
                    break
                try:
                    row = phone.analyze_cell(cell, default_region)
                except Exception as exc:                     # noqa: BLE001
                    # A line that breaks the parser is still a row with a reason, never a lost line.
                    row = _empty_row(index, cell, "ERROR",
                                     "This line could not be analysed: %s: %s"
                                     % (type(exc).__name__, str(exc)[:160]))
                    Actor.log.error("line %s failed: %s", index, exc)
                row["index"] = index
                row["billingInactive"] = False
                row["checkedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

                # Which numbers of this row still deserve a charge (first occurrence only).
                to_charge: list[str] = []
                e164 = row.get("e164") or ""
                if row.get("valid") and e164:
                    if e164 in first_index:
                        row["duplicateOfIndex"] = first_index[e164]
                        row["reason"] = row["reason"] + " Duplicate of line %d." % first_index[e164]
                    else:
                        first_index[e164] = index
                        to_charge.append(e164)
                    for other in row.get("otherNumbers") or []:
                        other_e164 = other["e164"]
                        if other_e164 in first_index:
                            continue
                        first_index[other_e164] = index
                        to_charge.append(other_e164)

                if to_charge and priced is None:
                    # The price of the event cannot be read. The line is neither delivered nor
                    # charged and the run stops here: an unreadable price list must never turn
                    # into a whole list delivered for free.
                    row["billable"] = None
                    row["status"] = "NOT_CHECKED_BILLING"
                    row["reason"] = ("the price of number-validated could not be read: "
                                     "not delivered and not charged")
                    rows.append(row)
                    await Actor.push_data(row)
                    not_checked_error += 1
                    stop = True
                    stop_reason = "billing-price-unknown"
                    processed = index
                    Actor.log.error("price list unreadable: stopping before the line is billed")
                    break

                eligible = bool(to_charge) and not stop
                # the price decides the flag, and it is decided BEFORE the row is written
                row["billable"] = None if priced is None else bool(eligible and priced)

                if not eligible or not priced or not billing_active:
                    if eligible and priced is None:
                        # delivered, price unknown: nothing was billed for this row
                        delivered_uncharged += 1
                    if eligible and not billing_active:
                        # billing is off for this run: the row is delivered, nothing is billed
                        row["billable"] = False
                        row["billingInactive"] = True
                    elif eligible and priced is False:
                        billing_active = False
                        row["billable"] = False
                        row["billingInactive"] = True
                    if len(to_charge) > 1:
                        extra_numbers_free += len(to_charge) - 1
                    rows.append(row)
                    await Actor.push_data(row)
                    processed = index
                    if index % 500 == 0:
                        await write_summary(True)
                    continue

                try:
                    # one call: the SDK writes the row and bills it under one lock
                    res = await Actor.push_data(row, charged_event_name=CHARGE_EVENT)
                except Exception as exc:                     # noqa: BLE001
                    # The row is already written: it carries the price claim, and the summary
                    # carries the truth (deliveredUncharged, chargeErrors). Mutating the row
                    # here would never reach the buyer.
                    rows.append(row)
                    delivered_uncharged += 1
                    if len(charge_errors) < 50:               # the summary must stay readable
                        charge_errors.append("%s: %s: %s"
                                             % (e164 or cell[:20], type(exc).__name__, str(exc)[:120]))
                    stop = True
                    stop_reason = "charge-error"
                    Actor.log.error("billing failed after the row was written: %s", exc)
                    processed = index
                    break

                billed = int(getattr(res, "charged_count", 0) or 0)
                limit_reached = bool(getattr(res, "event_charge_limit_reached", False))
                if billed <= 0 and limit_reached:
                    # compute_push_data_limit returned 0: nothing was written and nothing was billed
                    capped += 1
                    stop = True
                    stop_reason = "spending-cap"
                    Actor.log.warning("spending limit reached - the row is NOT written and NOT billed")
                    cap_row = _empty_row(index, cell, "NOT_CHECKED_CAP",
                                         "Not checked: the run reached the spending cap "
                                         "(maxTotalChargeUsd) before this line. Not charged.")
                    rows.append(cap_row)
                    await Actor.push_data(cap_row)
                    processed = index
                    break
                if billed <= 0:
                    # the row WAS written, the money was not taken: billing is off for this run
                    billing_active = False
                    delivered_uncharged += 1          # delivered, no money was taken for it
                    if len(to_charge) > 1:
                        extra_numbers_free += len(to_charge) - 1
                    rows.append(row)
                    processed = index
                    if index % 500 == 0:
                        await write_summary(True)
                    continue

                charged_events += billed
                if len(to_charge) > 1:
                    extra_numbers_free += len(to_charge) - 1
                rows.append(row)
                processed = index
                if getattr(res, "event_charge_limit_reached", False):
                    # SDK 4.2.0: this row WAS billed and no further event fits the budget
                    stop = True
                    stop_reason = "spending-cap"
                    processed = index
                    break
                if index % 500 == 0:
                    await write_summary(True)

            if stop:
                rest_start = processed + 1
                if stop_reason == "charge-error":
                    rest_status = "NOT_CHECKED_BILLING"
                    rest_reason = ("Not checked: billing failed, so the run stopped before this "
                                   "line. Not charged.")
                elif stop_reason == "billing-price-unknown":
                    rest_status = "NOT_CHECKED_BILLING"
                    rest_reason = ("Not checked: the price list could not be read, so the run "
                                   "stopped before this line. Not charged.")
                else:
                    rest_status = "NOT_CHECKED_CAP"
                    rest_reason = ("Not checked: the run reached the spending cap "
                                   "(maxTotalChargeUsd) before this line. Not charged.")
                for index in range(rest_start, len(head) + 1):
                    row = _empty_row(index, head[index - 1], rest_status, rest_reason)
                    rows.append(row)
                    await Actor.push_data(row)
                    if rest_status == "NOT_CHECKED_CAP":
                        capped += 1
                    else:
                        not_checked_error += 1
        except asyncio.CancelledError:
            cancelled = True
            stop_reason = "cancelled"
            try:
                await Actor.set_status_message(
                    "Cancelled after %d of %d line(s): %d row(s) were already delivered and are "
                    "in the dataset." % (len(rows), len(head), len(rows)))
            except Exception:                                # noqa: BLE001
                pass
            raise
        finally:
            for offset, cell in enumerate(reported_tail):
                index = len(head) + offset + 1
                row = _empty_row(index, cell, "NOT_CHECKED_LIMIT",
                                 "Not checked: the run accepts up to %d lines. Split the list into "
                                 "batches. Not charged." % MAX_ROWS)
                rows.append(row)
                await Actor.push_data(row)
            if priced is None and not stop_reason:
                # The price list could not be read, so nothing could be billed. Saying
                # "completed" here hides a run that delivered every row for free.
                stop_reason = "billing-price-unknown"
                Actor.log.warning("price list unreadable: rows were delivered and not billed")
            if not billing_active and not stop_reason:
                # Every row was delivered, nothing was billed: say it instead of leaving it blank.
                stop_reason = "billing-not-active"
                Actor.log.warning(
                    "this run is not pay-per-event, so no event could be billed; "
                    "all rows were delivered free"
                )
            await write_summary(cancelled or bool(stop_reason and stop_reason != "completed"))

        if not cancelled:
            try:
                await Actor.set_status_message(
                    "%d line(s) read: %d with a valid number, %d charged event(s); extra numbers "
                    "on a row, invalid and duplicate lines are free.%s"
                    % (len(lines), sum(1 for row in rows if row.get("valid")), charged_events,
                       "" if not stop_reason or stop_reason == "completed" else f" Stop: {stop_reason}."))
            except Exception:                            # noqa: BLE001
                pass
        Actor.log.info(
            "done: %s lines, %s valid, %s invalid, %s duplicates, %s billed events, stop=%s",
            len(lines),
            sum(1 for row in rows if row.get("valid")),
            sum(1 for row in rows if row["status"] in ("INVALID", "NO_NUMBER")),
            sum(1 for row in rows if row.get("duplicateOfIndex")),
            charged_events,
            stop_reason or "none",
        )


if __name__ == "__main__":
    asyncio.run(main())
