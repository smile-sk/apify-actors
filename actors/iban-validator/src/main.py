"""Apify Actor: IBAN validation with real arithmetic, not a shape check.

Input  : `ibans` - a list, or a block of text pasted from an invoice (IBANs are
         pulled out of it). Optional `bics` - same order, checked against the
         IBAN country, which is the one thing about a BIC we can prove.
Output : one row per IBAN - valid, country, SEPA flag, expected check digits,
         the reason a number was rejected, and whether the row was billable.
Charge : `iban-checked` - only for rows where the checksum was actually
         computed (known country, right length). A malformed line, an unknown
         country or an empty input costs the buyer nothing and is still reported.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from apify import Actor

from src import iban as iban_lib

CHARGE_EVENT = "iban-checked"
MAX_ROWS = 10_000


def _dep_versions() -> str:
    """Which dependency versions this build actually runs: pinning them needs a real run."""
    out = []
    for name in ("apify", "pydantic"):
        try:
            out.append(f"{name}={_pkg_version(name)}")
        except PackageNotFoundError:
            out.append(f"{name}=?")
    return " ".join(out)


async def main() -> None:
    async with Actor:
        Actor.log.info("deps: %s", _dep_versions())
        actor_input = await Actor.get_input() or {}

        pairs, bic_count = iban_lib.rows_from_input(actor_input.get("ibans"), actor_input.get("bics"))
        extracted = 0
        if not pairs and (actor_input.get("text") or "").strip():
            found = iban_lib.extract(str(actor_input.get("text")), limit=MAX_ROWS)
            extracted = len(found)
            pairs = [(value, "") for value in found]
        if not pairs:
            await Actor.fail("No IBAN given: put one per line in 'ibans', or paste a document in 'text'.")
            return
        if len(pairs) > MAX_ROWS:
            Actor.log.warning("input has %s rows, only the first %s are processed", len(pairs), MAX_ROWS)
            pairs = pairs[:MAX_ROWS]

        charged = 0
        capped = 0
        charge_errors: list[str] = []
        stop = False
        rows: list[dict] = []
        semaphore = asyncio.Semaphore(1)          # pure arithmetic: keep the order of input

        async def write_summary(partial: bool) -> None:
            try:
                await Actor.set_value("SUMMARY", {
                    "ibans": len(rows),
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
                    "billableIbans": charged,
                    "cappedNotCharged": capped,
                    "chargeErrors": charge_errors,
                    "partial": partial,
                })
            except Exception:                                    # noqa: BLE001
                pass

        async def handle(raw: str, bic: str) -> None:
            nonlocal charged, capped, stop
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
                rows.append(row)

                billable = bool(row.get("checkable")) and not stop
                if billable:
                    try:
                        charge = await Actor.charge(event_name=CHARGE_EVENT)
                        billed = int(getattr(charge, "charged_count", 0) or 0)
                        charged += billed
                        if billed == 0:
                            billable = False
                            capped += 1                  # cap hit: the row is delivered, not billed
                        if getattr(charge, "event_charge_limit_reached", False):
                            # SDK 4.2.0: the flag means THIS event was charged and no more fit.
                            stop = True
                    except Exception as exc:             # noqa: BLE001
                        # Billing is unknown here, so the row is delivered but not claimed as billed.
                        billable = False
                        charge_errors.append(f"{row['iban'] or raw[:20]}: {type(exc).__name__}: {str(exc)[:120]}")
                        Actor.log.error("charge failed: %s", exc)

                row["billable"] = billable
                await Actor.push_data(row)
                if len(rows) % 500 == 0:
                    await write_summary(True)

        for raw, bic in pairs:
            await handle(raw, bic)

        await write_summary(False)
        Actor.log.info("done: %s IBANs, %s valid, %s billed",
                       len(rows), sum(1 for r in rows if r["valid"]), charged)


if __name__ == "__main__":
    asyncio.run(main())
