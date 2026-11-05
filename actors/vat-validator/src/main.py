"""Apify Actor: VAT validation that separates "wrong number" from "could not check".

The failure this actor exists for: VIES (the EU registry that actually knows
whether a VAT number is registered) goes down, or answers only part of the
time. Most checkers turn that silence into "invalid", and the buyer marks a
real customer as a tax fraud.

So there are three answers, never two:
  VALID / INVALID_VIES  - VIES answered, and we heard it.
  INVALID_FORMAT / INVALID_CHECKSUM - arithmetic proves the number wrong.
  UNVERIFIED            - nobody answered. Not charged, said out loud.

Charge: `vat-checked` - once per number whose verdict was actually produced
(checksum computed, or VIES answered). A number VIES could not answer, a
cached answer, a number taken out of free text without a country prefix, and
a malformed number all cost nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version

import httpx
from apify import Actor

from src import vat as vat_lib
from src.vies import ask_vies

CHARGE_EVENT = "vat-checked"
MAX_ROWS = 4_000
CACHE_STORE = "vat-check-cache"
CACHE_VERSION = "3"
TIME_BUDGET_SECONDS = 3_300

BILLABLE = {"VALID", "INVALID_VIES", "INVALID_CHECKSUM", "NOT_IN_VIES"}


def _dep_versions() -> str:
    out = []
    for name in ("apify", "httpx", "pydantic"):
        try:
            out.append(f"{name}={_pkg_version(name)}")
        except PackageNotFoundError:
            out.append(f"{name}=?")
    return " ".join(out)


def _cache_key(country: str, number: str) -> str:
    raw = f"{CACHE_VERSION}|{vat_lib.vies_code(country)}|{number}".encode()
    return hashlib.sha256(raw).hexdigest()[:40]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def main() -> None:
    async with Actor:
        Actor.log.info("deps: %s", _dep_versions())
        actor_input = await Actor.get_input() or {}

        given = actor_input.get("vatNumbers")
        has_numbers = bool(given) if isinstance(given, list) else bool(str(given or "").strip())
        # The schema promises that `text` is read only when `vatNumbers` is empty.
        text = "" if has_numbers else str(actor_input.get("text") or "")
        hint = str(actor_input.get("countryHint") or "")

        stats: dict = {}
        rows_in = vat_lib.rows_from_input(given, text, hint, stats)
        if not rows_in:
            await Actor.fail(status_message=(
                "No VAT number given: put one per line in 'vatNumbers', or paste a document in 'text'."))
            return

        # What was sent, and what is actually checked: four spellings of one
        # number are four input rows and one number.
        input_rows = int(stats.get("inputRows") or len(rows_in))
        unique_rows = len(rows_in)
        dropped = 0
        if unique_rows > MAX_ROWS:
            dropped = unique_rows - MAX_ROWS
            Actor.log.warning("input has %s distinct numbers, only the first %s are processed",
                              unique_rows, MAX_ROWS)
            rows_in = rows_in[:MAX_ROWS]

        check_vies = actor_input.get("checkVies", True) is not False
        cache_minutes = int(actor_input.get("cacheMinutes", 1440) or 0)
        concurrency = max(1, min(int(actor_input.get("maxConcurrency", 3) or 3), 8))
        timeout = max(5.0, float(actor_input.get("viesTimeoutSeconds") or 30))
        budget = max(60.0, float(actor_input.get("timeBudgetSeconds") or TIME_BUDGET_SECONDS))

        store = None
        if cache_minutes > 0:
            try:
                store = await Actor.open_key_value_store(name=CACHE_STORE)
            except Exception as exc:                              # noqa: BLE001
                Actor.log.warning("cache unavailable: %s", exc)
                store = None

        started = time.monotonic()
        deadline = started + budget
        rows: list[dict] = []
        failures: list[dict] = []
        state = {
            "charged": 0, "capped": 0, "cache_hits": 0, "unverified": 0, "not_billed": 0,
            "not_attempted": 0, "charge_uncertain": 0, "charge_errors": [], "delivery_failures": 0,
            "stop": False, "stop_reason": "",
        }
        cursor = 0
        cursor_lock = asyncio.Lock()
        summary_lock = asyncio.Lock()

        async def write_summary(partial: bool) -> None:
            async with summary_lock:
                try:
                    await Actor.set_value("SUMMARY", {
                        "inputRows": input_rows,
                        "uniqueNumbers": unique_rows,
                        "rowsDroppedByInputLimit": dropped,
                        "vatNumbers": len(rows),
                        "valid": sum(1 for r in rows if r["status"] == "VALID"),
                        # Every row we actually looked at: a VIES that stayed silent was
                        # looked at too, and counting it as zero hid the whole run.
                        "checked": sum(1 for r in rows if r["status"] not in ("NOT_ATTEMPTED", "ERROR")),
                        "byStatus": {s: sum(1 for r in rows if r["status"] == s)
                                     for s in sorted({r["status"] for r in rows})},
                        "viesAnswered": sum(1 for r in rows if r["viesAnswered"]),
                        "viesUnavailable": state["unverified"],
                        "cacheHits": state["cache_hits"],
                        "billingModel": ("vat-checked - charged once per number whose verdict was produced "
                                         "(checksum computed or VIES answered). Numbers VIES could not "
                                         "answer, cached answers and numbers taken from free text "
                                         "without a country prefix are free."),
                        "billableChecks": state["charged"],
                        "cappedNotCharged": state["capped"],
                        "notBilled": state["not_billed"],
                        "notAttempted": state["not_attempted"],
                        "chargeUncertain": state["charge_uncertain"],
                        "chargeErrors": state["charge_errors"],
                        "deliveryFailures": state["delivery_failures"],
                        "stopReason": state["stop_reason"],
                        "elapsedSeconds": round(time.monotonic() - started, 1),
                        "partial": partial,
                    })
                except Exception:                                 # noqa: BLE001
                    pass

        async def deliver(row: dict, was_charged: bool) -> bool:
            """Push one row, twice if the dataset refuses it, then record it for a refund."""
            last_error = ""
            for attempt in (1, 2):
                try:
                    await Actor.push_data(row)
                    return True
                except Exception as exc:                          # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                    await asyncio.sleep(0.4 * attempt)
            state["delivery_failures"] += 1
            failures.append({
                "input": row.get("input"), "country": row.get("country"),
                "vatNumber": row.get("vatNumber"), "status": row.get("status"),
                "charged": was_charged, "error": last_error,
            })
            try:
                await Actor.set_value("DELIVERY_FAILURES", failures)
            except Exception:                                     # noqa: BLE001
                pass
            Actor.log.error("dataset refused a row (%s); the row is recorded for a refund", last_error)
            return False

        async def handle(row_index: int, country: str, number: str, explicit: bool,
                         client: httpx.AsyncClient) -> None:
            row = {
                "rowIndex": row_index,
                "input": f"{country}{number}" if country else number,
                "country": country,
                "vatNumber": number,
                "status": "ERROR",
                "valid": None,
                "reason": "",
                "companyName": "",
                "companyAddress": "",
                "checksumImplemented": False,
                "expected": "",
                "checksumValid": None,
                "fromCache": False,
                "viesAnswered": False,
                "charged": False,
                "checkedAt": _now(),
            }
            try:
                offline = await asyncio.to_thread(vat_lib.check_offline, country, number)
            except Exception as exc:                              # noqa: BLE001
                row["reason"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                offline = {"status": "ERROR", "reason": row["reason"],
                           "checksumImplemented": False, "expected": ""}

            row["checksumImplemented"] = offline["checksumImplemented"]
            row["expected"] = offline["expected"]
            # The checksum verdict comes from the arithmetic, not from a guess: it is
            # None only when no sum was computed, and False when the sum settled nothing.
            row["checksumValid"] = offline.get("checksumValid")

            if offline["status"] == "INVALID_FORMAT":
                row["status"] = "INVALID_FORMAT"
                row["valid"] = False
                row["reason"] = offline["reason"]
            elif offline["status"] == "INVALID_CHECKSUM":
                row["status"] = "INVALID_CHECKSUM"
                row["valid"] = False
                row["reason"] = offline["reason"]
            elif not vat_lib.in_vies(country):
                if offline["status"] == "FORMAT_OK":
                    row["status"] = "NOT_IN_VIES"
                    row["valid"] = None            # present in VIES? unknown, and we say so
                    row["reason"] = (f"VIES does not cover {country}; the national checksum "
                                     f"passes, which is not proof of registration")
                else:
                    row["status"] = "UNSUPPORTED_COUNTRY"
                    row["valid"] = None
                    row["reason"] = f"{country or 'this prefix'} is not a VAT country we know"
            elif not check_vies:
                row["status"] = "NOT_CHECKED"
                row["valid"] = None
                row["reason"] = ("VIES check was switched off in the input; only shape and checksum "
                                 "were verified, registration was not checked. Not charged.")
            else:
                key = _cache_key(country, number)
                cached = None
                if store is not None:
                    try:
                        cached = await store.get_value(key)
                    except Exception:                         # noqa: BLE001
                        cached = None
                if isinstance(cached, dict) and cached.get("at"):
                    try:
                        age_minutes = (datetime.now(timezone.utc)
                                       - datetime.fromisoformat(cached["at"])).total_seconds() / 60
                    except (TypeError, ValueError):
                        age_minutes = cache_minutes + 1          # a broken record is a miss, not a crash
                    if age_minutes <= cache_minutes:
                        row.update({"status": "VALID" if cached.get("valid") else "INVALID_VIES",
                                    "valid": bool(cached.get("valid")),
                                    "companyName": cached.get("name", ""),
                                    "companyAddress": cached.get("address", ""),
                                    "fromCache": True, "viesAnswered": True,
                                    "reason": "VIES answer taken from cache, not charged"})
                        state["cache_hits"] += 1
                if not row["fromCache"]:
                    answer = await ask_vies(client, vat_lib.vies_code(country), number)
                    if answer["answered"]:
                        row["viesAnswered"] = True
                        row["status"] = "VALID" if answer["valid"] else "INVALID_VIES"
                        row["valid"] = answer["valid"]
                        row["companyName"] = answer["name"]
                        row["companyAddress"] = answer["address"]
                        row["reason"] = ("VIES confirmed the number" if answer["valid"]
                                         else "VIES answered: the number is not registered")
                        if store is not None:
                            try:
                                await store.set_value(key, {
                                    "valid": answer["valid"], "name": answer["name"],
                                    "address": answer["address"], "at": _now(),
                                })
                            except Exception:                 # noqa: BLE001
                                pass
                    else:
                        row["status"] = "UNVERIFIED"
                        row["valid"] = None
                        row["reason"] = (f"VIES did not answer after {answer['tries']} tries "
                                         f"({answer['error']}); the number was NOT charged and "
                                         f"is NOT proof of anything - retry later")
                        state["unverified"] += 1

            if not explicit and row["status"] in BILLABLE:
                row["reason"] = ((row["reason"] + " ") if row["reason"] else "") + (
                    "The number came from free text without a country prefix, so it was checked "
                    "but is not charged.")

            # Money: charge first, then deliver. A charge that raised may or may not
            # have billed the row, so the row still goes out and the run stops rather
            # than paying for air in either direction.
            uncertain = False
            billable = (row["status"] in BILLABLE and not row["fromCache"]
                        and explicit and not state["stop"])
            if billable:
                try:
                    charge = await Actor.charge(event_name=CHARGE_EVENT)
                except Exception as exc:                          # noqa: BLE001
                    state["charge_errors"].append(f"{row['input']}: {type(exc).__name__}: {str(exc)[:120]}")
                    Actor.log.error("charge failed: %s", exc)
                    billable = False
                    uncertain = True
                    state["charge_uncertain"] += 1
                    row["chargeUncertain"] = True
                else:
                    billed = int(getattr(charge, "charged_count", 0) or 0)
                    if billed > 0:
                        state["charged"] += billed
                        row["charged"] = True
                    else:
                        billable = False
                        if getattr(charge, "event_charge_limit_reached", False):
                            state["capped"] += 1
                            state["stop"] = True
                            state["stop_reason"] = state["stop_reason"] or "charge-limit"
                            row["reason"] = ((row["reason"] + " ") if row["reason"] else "") + (
                                "The spending limit for this run was reached, so this row is not charged.")
                        else:
                            state["not_billed"] += 1
            row["billable"] = bool(row["charged"])
            rows.append(row)
            delivered = await deliver(row, bool(row["charged"]))
            if not delivered:
                state["stop"] = True
                state["stop_reason"] = "delivery-error"
            elif uncertain and not state["stop"]:
                state["stop"] = True
                state["stop_reason"] = "charge-uncertain"
            if len(rows) % 200 == 0:
                await write_summary(True)

        async def worker(client: httpx.AsyncClient) -> None:
            nonlocal cursor
            while True:
                async with cursor_lock:
                    if state["stop"] or cursor >= len(rows_in):
                        return
                    index = cursor
                    cursor += 1
                country, number, explicit = rows_in[index]
                if time.monotonic() > deadline:
                    state["not_attempted"] += 1
                    row = {
                        "rowIndex": index,
                        "input": f"{country}{number}" if country else number,
                        "country": country, "vatNumber": number,
                        "status": "NOT_ATTEMPTED", "valid": None,
                        "reason": ("The run reached its time budget before this number; nothing was "
                                   "charged. Split the list or raise maxConcurrency."),
                        "companyName": "", "companyAddress": "", "checksumImplemented": False,
                        "expected": "", "checksumValid": None, "fromCache": False,
                        "viesAnswered": False, "charged": False, "billable": False,
                        "checkedAt": _now(),
                    }
                    rows.append(row)
                    await deliver(row, False)
                    continue
                await handle(index, country, number, explicit, client)

        limits = httpx.Limits(max_connections=concurrency + 2, max_keepalive_connections=concurrency)
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            await asyncio.gather(*[worker(client) for _ in range(concurrency)])

        # Anything the run never started is still reported, with the reason, so the
        # buyer never wonders where the rows went.
        while cursor < len(rows_in):
            country, number, _explicit = rows_in[cursor]
            reason = {"charge-limit": "The spending limit for this run was reached before this number.",
                      "delivery-error": "The dataset refused rows, so the run stopped before this number.",
                      "charge-uncertain": "A charge call failed, so the run stopped before this number.",
                      }.get(state["stop_reason"], "The run stopped before this number.")
            row = {
                "rowIndex": cursor,
                "input": f"{country}{number}" if country else number,
                "country": country, "vatNumber": number,
                "status": "NOT_ATTEMPTED", "valid": None,
                "reason": reason + " Nothing was charged.",
                "companyName": "", "companyAddress": "", "checksumImplemented": False,
                "expected": "", "checksumValid": None, "fromCache": False,
                "viesAnswered": False, "charged": False, "billable": False,
                "checkedAt": _now(),
            }
            rows.append(row)
            state["not_attempted"] += 1
            await deliver(row, False)
            cursor += 1

        await write_summary(False)
        Actor.log.info("done: %s numbers, %s valid, %s billed, %s unverified, stop=%s",
                       len(rows), sum(1 for r in rows if r["status"] == "VALID"),
                       state["charged"], state["unverified"], state["stop_reason"] or "none")


if __name__ == "__main__":
    asyncio.run(main())
