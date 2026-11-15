"""Apify Actor: IBAN validation with real arithmetic, not a shape check.

Input  : `ibans` - a list, or a block of text pasted from an invoice (IBANs are
         pulled out of it). Optional `bics` - same order, checked against the
         IBAN country, which is the one thing about a BIC we can prove.
Output : one row per IBAN - valid, country, SEPA flag, expected check digits,
         the reason a number was rejected, and whether the row was billable.
Charge : `iban-checked` - only for rows where the checksum was actually
         computed (known country, right length). A malformed line, an unknown
         country or an empty input costs the buyer nothing and is still reported.

Money = delivery: each paid row is written and charged by ONE call
`push_data(row, charged_event_name=...)`, so a row can never be billed
without being in the dataset. `billable` in the row is decided BEFORE the write
from the price list (true / false / null when the price cannot be read), it is
never a claim about what was charged.
"""
from __future__ import annotations

import inspect
import asyncio
import os
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from apify import Actor

from src import iban as iban_lib

CHARGE_EVENT = "iban-checked"
MAX_ROWS = 10_000
SUMMARY_STORE = "iban-validator-summary"


def _dep_versions() -> str:
    """Which dependency versions this build actually runs: pinning them needs a real run."""
    out = []
    for name in ("apify", "pydantic"):
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


async def main() -> None:
    async with Actor:
        Actor.log.info("deps: %s", _dep_versions())
        run_id = _run_id()
        actor_input = await Actor.get_input() or {}

        pairs, bic_count = iban_lib.rows_from_input(actor_input.get("ibans"), actor_input.get("bics"))
        extracted = 0
        if not pairs and (actor_input.get("text") or "").strip():
            found = iban_lib.extract(str(actor_input.get("text")), limit=MAX_ROWS)
            extracted = len(found)
            pairs = [(value, "") for value in found]
        if not pairs:
            await Actor.fail(status_message="No IBAN given: put one per line in 'ibans', or paste a document in 'text'.")
            return
        if len(pairs) > MAX_ROWS:
            Actor.log.warning("input has %s rows, only the first %s are processed", len(pairs), MAX_ROWS)
            pairs = pairs[:MAX_ROWS]

        priced = await event_is_priced(CHARGE_EVENT)

        charged = 0
        billing_inactive = False
        capped = 0
        not_checked_error = 0
        delivered_uncharged = 0
        charge_errors: list[str] = []
        stop = False
        stop_reason = ""
        cancelled = False
        rows: list[dict] = []
        semaphore = asyncio.Semaphore(1)          # pure arithmetic: keep the order of input

        async def write_summary(partial: bool) -> None:
            payload = {
                "runId": run_id,
                "ibans": len(rows),
                "asked": len(rows) + capped + not_checked_error,
                "valid": sum(1 for r in rows if r["valid"]),
                "invalid": sum(1 for r in rows if not r["valid"]),
                "byStatus": {status: sum(1 for r in rows if r["status"] == status)
                             for status in sorted({r["status"] for r in rows})},
                "fromText": extracted,
                "bicsGiven": bic_count,
                "bicsRejected": sum(1 for r in rows if r["bicValid"] is False),
                "billingModel": ("iban-checked - charged once per IBAN whose checksum was "
                                 "actually computed (known country and right length); malformed, "
                                 "unknown-country and empty lines are free"),
                "billableIbans": sum(1 for r in rows if r.get("billable") is True),
                "chargedEvents": charged,
                "cappedNotCharged": capped,
                "notCheckedError": not_checked_error,
                "deliveredUncharged": delivered_uncharged,
                "chargeErrors": charge_errors,
                "stopReason": stop_reason or ("completed" if not partial else "in-progress"),
                "billingActive": (None if priced is None else bool((not billing_inactive) and priced)),
                "pricingEventPriced": priced,
                "cancelled": cancelled,
                "partial": partial,
            }
            # named store: the run-scoped key only, because the store is shared by the whole
            # account and a plain name would be overwritten by the next run. The plain
            # RUN_SUMMARY lives in this run's own store, below.
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

        async def handle(raw: str, bic: str) -> None:
            nonlocal charged, capped, stop, stop_reason, delivered_uncharged, billing_inactive, not_checked_error
            async with semaphore:
                try:
                    row = await asyncio.to_thread(iban_lib.check, raw, bic)
                except Exception as exc:                         # noqa: BLE001
                    row = {"input": raw, "iban": "", "valid": False, "status": "ERROR",
                           "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                           "checkable": False, "bicValid": None, "bicReason": "",
                           "countryCode": "", "country": "", "sepa": False,
                           "expectedLength": None, "actualLength": 0, "printed": "",
                           "checkDigits": "", "expectedCheckDigits": None, "bban": "",
                           "checksumRemainder": None, "bic": (bic or "").strip().upper()}
                    Actor.log.error("iban failed: %s: %s", raw[:40], exc)

                row["checkedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

                if row.get("checkable") and priced is None:
                    # The price of the event cannot be read. The row is neither delivered nor
                    # charged and the run stops here: handing a whole list out for free because
                    # the price list was unreadable is our loss, and it must be visible, not silent.
                    row["billable"] = None
                    row["status"] = "NOT_CHECKED_BILLING"
                    row["reason"] = ("the price of iban-checked could not be read: "
                                     "not delivered and not charged")
                    rows.append(row)
                    await Actor.push_data(row)
                    not_checked_error += 1
                    stop = True
                    stop_reason = "billing-price-unknown"
                    Actor.log.error("price list unreadable: stopping before the row is billed")
                    return

                # the price decides the flag, and it is decided BEFORE the row is written
                eligible = bool(row.get("checkable")) and not stop
                row["billable"] = None if priced is None else bool(eligible and priced)

                if not eligible or not priced:
                    if eligible and priced is None:
                        # delivered, price unknown: nothing was billed for this row
                        delivered_uncharged += 1
                    elif eligible and priced is False:
                        # billing is off for this run: the row is delivered, nothing is billed,
                        # and the summary must name that instead of saying "completed"
                        billing_inactive = True
                        row["billable"] = False
                    rows.append(row)
                    await Actor.push_data(row)
                    if len(rows) % 500 == 0:
                        await write_summary(True)
                    return

                try:
                    res = await Actor.push_data(row, charged_event_name=CHARGE_EVENT)
                except Exception as exc:                     # noqa: BLE001
                    # SDK writes the row first and charges after: the buyer sees the row,
                    # the money was not taken. That is delivered-uncharged, not "delivered".
                    rows.append(row)
                    delivered_uncharged += 1
                    charge_errors.append(f"{row['iban'] or raw[:20]}: {type(exc).__name__}: {str(exc)[:120]}")
                    stop = True
                    stop_reason = "charge-error"
                    Actor.log.error("charge failed after the row was written: %s", exc)
                    return

                billed = int(getattr(res, "charged_count", 0) or 0)
                if billed <= 0 and not getattr(res, "event_charge_limit_reached", False):
                    # the row WAS written, the money was not taken: billing is off for this run
                    billing_inactive = True
                    delivered_uncharged += 1          # the row is delivered, no money was taken
                    row["billable"] = False
                    rows.append(row)
                    if len(rows) % 500 == 0:
                        await write_summary(True)
                    return
                if billed <= 0:
                    # nothing was written and nothing was charged: the row is not delivered
                    capped += 1
                    stop = True
                    stop_reason = "spending-cap"
                    Actor.log.warning("User spending limit reached - the row is NOT written and NOT billed")
                    await Actor.push_data({**row, "billable": False, "status": "NOT_CHECKED_CAP",
                                           "reason": "spending limit reached: not checked and not charged"})
                    return

                charged += billed
                rows.append(row)
                if getattr(res, "event_charge_limit_reached", False):
                    # SDK 4.2.0: the flag means THIS row was charged and no more fit
                    stop = True
                    stop_reason = "spending-cap"
                if len(rows) % 500 == 0:
                    await write_summary(True)

        async def write_rest(rest: list[tuple[str, str]], status: str, reason: str) -> None:
            """One free status row per input the run never reached: silence is not an answer."""
            nonlocal capped, not_checked_error
            for raw, _bic in rest:
                if status == "NOT_CHECKED_CAP":
                    capped += 1
                else:
                    not_checked_error += 1
                await Actor.push_data({"input": raw, "iban": "", "valid": False,
                                       "status": status, "reason": reason,
                                       "checkable": False, "billable": False,
                                       "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")})

        try:
            for index, (raw, bic) in enumerate(pairs):
                if stop:
                    rest = pairs[index:]
                    if stop_reason == "spending-cap":
                        await write_rest(rest, "NOT_CHECKED_CAP",
                                         "spending limit reached: not checked and not charged")
                    elif stop_reason == "billing-price-unknown":
                        await write_rest(rest, "NOT_CHECKED_BILLING",
                                         "the price list could not be read: not delivered and "
                                         "not charged")
                    else:
                        await write_rest(rest, "NOT_CHECKED_ERROR",
                                         f"the run stopped on a billing error: {stop_reason}")
                    break
                await handle(raw, bic)
        except asyncio.CancelledError:
            cancelled = True
            stop_reason = "cancelled"
            try:
                await Actor.set_status_message(
                    f"Cancelled after {len(rows)} of {len(pairs)}: rows already delivered are in the dataset.")
            except Exception:                            # noqa: BLE001
                pass
            raise
        finally:
            if priced is None and not stop_reason:
                # The price list could not be read, so nothing could be billed. Leaving the
                # reason as "completed" is the exact shape of the live billing defect
                # (iban 0.1.6: 3 valid rows, 0 charged, billable null), so it is named.
                stop_reason = "billing-price-unknown"
                Actor.log.warning("price list unreadable: rows were delivered and not billed")
            if billing_inactive and not stop_reason:
                # every row was delivered, nothing was billed: say it instead of leaving it blank
                stop_reason = "billing-not-active"
            await write_summary(cancelled or bool(stop_reason and stop_reason != "completed"))

        if not cancelled:
            try:
                await Actor.set_status_message(
                    "%d IBAN line(s) read: %d valid, %d charged event(s); malformed and "
                    "unknown-country lines are free.%s"
                    % (len(rows), sum(1 for r in rows if r["valid"]), charged,
                       "" if not stop_reason or stop_reason == "completed" else f" Stop: {stop_reason}."))
            except Exception:                            # noqa: BLE001
                pass
        Actor.log.info("done: %s IBANs, %s valid, %s billed", len(rows), sum(1 for r in rows if r["valid"]), charged)


if __name__ == "__main__":
    asyncio.run(main())
