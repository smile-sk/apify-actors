"""Email Validator API: syntax, domain, MX and typo hints for a list of addresses.

Money rules, learned the hard way in this park:
  * A paid row is written and charged in ONE call, Actor.push_data(rows,
    charged_event_name=...). A separate charge before the write leaves a row that the
    buyer paid for and cannot download.
  * The batch is cut by the remaining budget BEFORE the write: the SDK silently
    writes and charges only the prefix that fits.
  * We charge only for an address that got a verdict from DNS. A malformed address, a
    duplicate, an address past the limit and anything we could not resolve are free,
    and their rows carry no verdict fields at all.

What this actor refuses to claim: it never probes SMTP, because port 25 is closed on
our side and inside the container. It says whether the domain accepts mail, not whether
a particular mailbox exists, and the README says so in those words.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from apify import Actor

from . import checks

ST_OK = "OK"
ST_INVALID = "INVALID_SYNTAX"
ST_NO_DOMAIN = "NO_SUCH_DOMAIN"
ST_NO_MAIL = "NO_MAIL_SERVER"
ST_DNS_ERROR = "NOT_CHECKED_DNS_ERROR"
ST_DUPLICATE = "DUPLICATE"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_SYNTAX_ONLY = "SYNTAX_ONLY"

EVENT_NAME = "email-checked"
BATCH_SIZE = 25
# One address can make three lookups (MX, then A, then AAAA). The gate has to admit
# three requests of two seconds plus slack: 16.5 s let in a chain that needed 24 s,
# and the platform killed the run before the summary was written.
ANALYSE_QUERIES = 3
MIN_REQUEST_SECONDS = 1.5
MAX_REQUEST_SECONDS = 8.0
DEADLINE_GATE_SECONDS = ANALYSE_QUERIES * 2 + 3.0
DEFAULT_MAX_EMAILS = 50
MAX_EMAILS_CEILING = 200

ROW_FIELDS = ("email", "normalised", "localPart", "domain", "domainAscii", "syntaxValid",
              "domainExists", "hasMx", "mxHosts", "acceptsMail", "isDisposable", "isRole",
              "isFreeProvider", "suggestion")


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


def request_budget(left: float | None) -> float | None:
    """What one lookup may take, sharing the remaining runway between the three.

    One address can ask MX, then A, then AAAA. Giving each of them the whole runway is
    how a chain of three 8 s requests overshot a 16.5 s window by 7.5 s, and the platform
    killed the run before the summary existed.
    """
    if left is None:
        return None
    return max(MIN_REQUEST_SECONDS,
               min(MAX_REQUEST_SECONDS, (left - 3.0) / ANALYSE_QUERIES))


def _stop_status(reason: str) -> str:
    """The status of a line the run never reached, named for the real cause."""
    return ST_CAP if reason == "spending-cap" else ST_ERROR


def _stop_text(reason: str) -> str:
    """Why the rest of the list was not checked: money is only one of the reasons."""
    return {
        "spending-cap": "the spending cap was reached before this address: not checked",
        "charge-error": "the run stopped on a charge that could not be confirmed "
                        "before this address: not checked",
        "dataset-write-error": "the run stopped because the dataset refused rows "
                               "before this address: not checked",
        "billing-price-unknown": "the run stopped because the price could not be "
                                 "read before this address: not checked",
        "deadline": "the run ran out of time before this address: not checked",
        "unexpected-error": "the run stopped on an unexpected error before this "
                            "address: not checked",
    }.get(reason or "", "the run stopped before this address: not checked")


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


async def main() -> None:
    await Actor.init()
    started = now_iso()

    data = await Actor.get_input() or {}

    raw_list = data.get("emails")
    if isinstance(raw_list, str):
        wanted = [raw_list]
    elif isinstance(raw_list, list):
        wanted = [str(item) for item in raw_list]
    else:
        wanted = []
    if not wanted and data.get("email"):
        wanted = [str(data.get("email"))]
    tasks = [item.strip() for item in wanted if item and str(item).strip()]
    if not tasks:
        await Actor.fail(status_message=(
            "No email addresses were sent. Pass a list in the emails field, for example "
            "{\"emails\": [\"someone@example.com\"]}."))
        return

    check_dns = data.get("checkDns")
    check_dns = True if check_dns is None else bool(check_dns)
    max_emails = _limit(data.get("maxEmails"), DEFAULT_MAX_EMAILS, 1, MAX_EMAILS_CEILING)
    if len(tasks) > max_emails:
        cut = tasks[max_emails:]
        tasks = tasks[:max_emails]
    else:
        cut = []

    deadline = platform_deadline()
    if deadline is not None:
        left = time_left(deadline) or 0.0
        if left < DEADLINE_GATE_SECONDS:
            await Actor.fail(status_message=(
                f"The run has {int(left)} s left before the platform stops it, and one "
                f"address can need up to {int(DEADLINE_GATE_SECONDS)} s of resolver time. "
                "Send fewer addresses or raise the timeout."))
            return

    charging = Actor.get_charging_manager()
    can_charge, price_note = price_state(charging)
    billable_claim = None if can_charge is None else bool(can_charge)

    import httpx

    dataset = await Actor.open_dataset()
    client = httpx.Client(timeout=httpx.Timeout(15.0), follow_redirects=False,
                          headers={"Accept": "application/dns-json",
                                   "User-Agent": "EmailValidatorApi/0.1 (+https://apify.com)"})
    counts = {
        "emailsAttempted": 0,
        "emailsChecked": 0,
        "emailsAcceptMail": 0,
        "emailsInvalidSyntax": 0,
        "emailsSyntaxOnly": 0,
        "emailsNoDomain": 0,
        "emailsNoMailServer": 0,
        "emailsDnsError": 0,
        "emailsDuplicate": 0,
        "emailsLimited": len(cut),
        "emailsCutByBudget": 0,
        "emailsDelivered": 0,
        "emailsCapped": 0,
    }
    by_status: dict[str, int] = {}
    seen: set[str] = set()
    batch: list[dict] = []
    free_buffer: list[dict] = []
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

    def blank(value: str, index: int, status: str, reason: str,
              keep: dict | None = None) -> dict:
        row = {field: None for field in ROW_FIELDS}
        row.update({"email": value, "actorInput": value, "actorIndex": index,
                    "status": status, "reason": reason, "billable": False})
        if keep:
            # A free row may keep what was really computed about the address itself
            # (the format, the lists, the punycode). It never carries a DNS verdict:
            # domainExists, hasMx, mxHosts and acceptsMail stay null.
            for field in LOCAL_FIELDS:
                row[field] = keep.get(field)
        return row

    FREE_ROW_BATCH = 100
    LOCAL_FIELDS = ("normalised", "localPart", "domain", "domainAscii", "syntaxValid",
                    "isDisposable", "isRole", "isFreeProvider", "suggestion")

    async def flush_free() -> None:
        """Write the buffered status rows in batches.

        One write per free line meant a thousand round trips for a thousand addresses
        over the limit, and each one was a chance to stop the run halfway.
        """
        nonlocal rows_written, write_errors, stopping, stop_reason, dataset_note
        while free_buffer:
            chunk = free_buffer[:FREE_ROW_BATCH]
            del free_buffer[:FREE_ROW_BATCH]
            try:
                await dataset.push_data(chunk)
            except Exception:  # noqa: BLE001
                write_errors += 1
                stopping = True
                stop_reason = stop_reason or "dataset-write-error"
                dataset_note = "free status rows were refused by the dataset"
                return
            rows_written += len(chunk)
            for item in chunk:
                note(str(item.get("status") or ST_ERROR))

    async def free_status(value: str, index: int, status: str, reason: str,
                          keep: dict | None = None) -> None:
        """A row that carries a reason and nothing a buyer could use as a verdict."""
        free_buffer.append(blank(value, index, status, reason, keep))
        if len(free_buffer) >= FREE_ROW_BATCH:
            await flush_free()

    async def item_count() -> int | None:
        """The dataset counter, asked three times: platform metadata lags behind writes."""
        for attempt in range(3):
            try:
                meta = await dataset.get_metadata()
                return int(getattr(meta, "item_count", 0) or 0)
            except Exception:  # noqa: BLE001
                if attempt < 2:
                    await asyncio.sleep(0.4)
        return None

    async def write_paid(rows: list[dict]) -> None:
        """Write and charge one batch; any doubt about money stops the run."""
        nonlocal rows_written, charged_total, delivered_uncharged, delivered_unverified
        nonlocal charge_uncertain
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
            counts["emailsDelivered"] += len(rows)
            delivered_uncharged += len(rows)
            note(ST_OK, len(rows))
            return
        if can_charge is None:
            stopping = True
            stop_reason = stop_reason or "billing-price-unknown"
            for row in rows:
                await free_status(row.get("email") or "", int(row.get("actorIndex") or 0),
                                  ST_BILLING, f"not delivered and not charged: {price_note}")
            return
        if charging.is_event_charge_limit_reached(EVENT_NAME):
            stopping = True
            stop_reason = stop_reason or "spending-cap"
            for row in rows:
                counts["emailsCapped"] += 1
                await free_status(row.get("email") or "", int(row.get("actorIndex") or 0),
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
                counts["emailsCapped"] += 1
                await free_status(row.get("email") or "", int(row.get("actorIndex") or 0),
                                  ST_CAP, "the remaining budget could not be computed: not "
                                          "delivered and not charged")
            return
        pay, over = rows[:max(0, budget)], rows[max(0, budget):]
        if over:
            stopping = True
            stop_reason = stop_reason or "spending-cap"
            for row in over:
                counts["emailsCapped"] += 1
                await free_status(row.get("email") or "", int(row.get("actorIndex") or 0),
                                  ST_CAP, "the remaining budget ended before this address: "
                                          "not delivered and not charged")
        if not pay:
            return
        for row in pay:
            row["billable"] = billable_claim is not False
        await flush_free()
        before = await item_count()
        try:
            result = await Actor.push_data(pay, charged_event_name=EVENT_NAME)
        except Exception as exc:  # noqa: BLE001
            # One exception, three different meanings, and only the dataset counter can
            # tell them apart. When the counter itself does not answer, the honest answer
            # is "unknown": claiming "nothing was delivered and nothing was charged" there
            # is exactly how a buyer is made to pay twice for the same addresses.
            after = await item_count()
            if before is None or after is None:
                delivered_unverified += len(pay)
                charge_uncertain += len(pay)
                stopping = True
                stop_reason = stop_reason or "charge-error"
                dataset_note = (
                    "the charge raised and the dataset counter could not be read: "
                    "delivery and the charge are both unknown, check the dataset before "
                    f"re-running ({type(exc).__name__})")
                return
            landed = min(max(0, int(after) - int(before)), len(pay))
            if landed:
                counts["emailsDelivered"] += landed
                rows_written += landed
                note(ST_OK, landed)
            if landed >= len(pay):
                charge_uncertain += len(pay)
                delivered_uncharged += len(pay)
                stopping = True
                stop_reason = stop_reason or "charge-error"
                dataset_note = ("a charge failed: the rows stay in the dataset and are "
                                "counted in chargeUncertain, no second row is written")
                return
            write_errors += 1
            delivered_unverified += len(pay) - landed
            stopping = True
            stop_reason = stop_reason or "dataset-write-error"
            dataset_note = (
                f"the dataset counter grew by {landed} of {len(pay)}: delivery of the "
                f"rest is unknown and nothing is claimed as sold ({type(exc).__name__})")
            return
        charged = getattr(result, "charged_count", None)
        if charged is None:
            delivered_uncharged += len(pay)
            counts["emailsDelivered"] += len(pay)
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
        counts["emailsDelivered"] += len(pay)
        rows_written += len(pay)
        note(ST_OK, len(pay))
        delivered_uncharged += len(pay) - charged

    async def flush() -> None:
        nonlocal batch
        if batch:
            rows, batch = batch, []
            await write_paid(rows)

    cursor = 0
    unexpected = ""
    try:
        for index, raw in enumerate(tasks):
            cursor = index + 1
            lowered = checks.address_key(raw)
            if lowered in seen:
                counts["emailsDuplicate"] += 1
                await free_status(raw, index, ST_DUPLICATE,
                                  "the same address was already sent in this run")
                continue
            seen.add(lowered)
            if stopping:
                counts["emailsCutByBudget"] += 1
                await free_status(raw, index, _stop_status(stop_reason),
                                  _stop_text(stop_reason))
                continue
            left = time_left(deadline)
            if left is not None and left < DEADLINE_GATE_SECONDS:
                counts["emailsCutByBudget"] += 1
                stop_reason = stop_reason or "deadline"
                await free_status(raw, index, ST_TIME, f"the run had {int(left)} s left: not checked")
                continue

            counts["emailsAttempted"] += 1
            # The deadline binds each lookup, and the lookups share what is left.
            request_timeout = request_budget(left)
            row = checks.analyse(client, raw, check_dns=check_dns, timeout=request_timeout)
            status = row.get("status") or ST_OK
            row.update({"actorInput": raw, "actorIndex": index, "billable": True})
            if status == ST_INVALID:
                counts["emailsInvalidSyntax"] += 1
                await free_status(raw, index, status, row.get("reason") or "not a valid address")
                continue
            if status == ST_DNS_ERROR:
                counts["emailsDnsError"] += 1
                await free_status(raw, index, status, row.get("reason") or "the resolver failed")
                continue
            if status == ST_SYNTAX_ONLY:
                counts["emailsSyntaxOnly"] += 1
                await free_status(raw, index, status,
                                  row.get("reason")
                                  or "checkDns is off: the format was checked, the domain was not",
                                  keep=row)
                continue
            if status == ST_NO_DOMAIN:
                counts["emailsNoDomain"] += 1
                row["reason"] = row.get("reason") or "the domain does not exist in DNS"
            elif status == ST_NO_MAIL:
                counts["emailsNoMailServer"] += 1
                row["reason"] = row.get("reason") or "the domain publishes no mail server"
            elif status == ST_OK:
                if row.get("acceptsMail"):
                    counts["emailsAcceptMail"] += 1
                row["reason"] = ""
            counts["emailsChecked"] += 1
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                await flush()
    except Exception as exc:  # noqa: BLE001
        # An unexpected error must not cost the buyer the summary: the rows already
        # written stay, the rest is named, and the summary is written below anyway.
        unexpected = type(exc).__name__
        stopping = True
        stop_reason = stop_reason or "unexpected-error"
        dataset_note = ((dataset_note + "; ") if dataset_note else "") + (
            f"the run stopped on an unexpected error ({unexpected}): rows already "
            "written stay in the dataset, the rest is not checked")
    finally:
        try:
            await flush()
        except Exception as exc:  # noqa: BLE001
            write_errors += 1
            stop_reason = stop_reason or "dataset-write-error"
            dataset_note = dataset_note or (
                f"a pending batch was refused by the dataset ({type(exc).__name__})")
        try:
            await flush_free()
        except Exception:  # noqa: BLE001
            write_errors += 1
        client.close()

    if cursor < len(tasks):
        for position in range(cursor, len(tasks)):
            counts["emailsCutByBudget"] += 1
            await free_status(tasks[position], position, ST_ERROR,
                              _stop_text(stop_reason or "unexpected-error"))
        await flush_free()

    for position, raw in enumerate(cut, start=len(tasks)):
        await free_status(raw, position, ST_LIMIT,
                          f"maxEmails is {max_emails}: this address was not checked")
    await flush_free()

    accounted = sum(by_status.values())
    mismatch = accounted != len(tasks) + len(cut)
    if charged_total + delivered_uncharged != counts["emailsDelivered"]:
        mismatch = True

    summary = {
        "emailsRequested": len(tasks) + len(cut),
        "emailsAttempted": counts["emailsAttempted"],
        "emailsChecked": counts["emailsChecked"],
        "emailsAcceptMail": counts["emailsAcceptMail"],
        "emailsInvalidSyntax": counts["emailsInvalidSyntax"],
        "emailsNoDomain": counts["emailsNoDomain"],
        "emailsNoMailServer": counts["emailsNoMailServer"],
        "emailsDnsError": counts["emailsDnsError"],
        "emailsDuplicate": counts["emailsDuplicate"],
        "emailsLimited": counts["emailsLimited"],
        "emailsCutByBudget": counts["emailsCutByBudget"],
        "emailsDelivered": counts["emailsDelivered"],
        "emailsCapped": counts["emailsCapped"],
        "emailsSyntaxOnly": counts["emailsSyntaxOnly"],
        "emailsUnverified": delivered_unverified,
        "rowsWritten": rows_written,
        "byStatus": by_status,
        "chargedEvents": charged_total,
        "deliveredUncharged": delivered_uncharged,
        "chargeUncertain": charge_uncertain,
        "writeErrors": write_errors,
        "summaryWriteErrors": summary_errors,
        "billingActive": None if can_charge is None else bool(can_charge),
        "priceEvent": EVENT_NAME,
        "priceNote": price_note,
        "checkDns": check_dns,
        "maxEmails": max_emails,
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
            store = await Actor.open_key_value_store(name="email-validator-summary")
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

    told = (f"{counts['emailsChecked']} address(es) checked, {charged_total} charged, "
            f"{rows_written - counts['emailsDelivered']} free status row(s)")
    if stop_reason:
        told += f", stopped: {stop_reason}"
    await Actor.set_status_message(told)

    if counts["emailsChecked"] == 0 and rows_written == 0:
        await Actor.fail(status_message=(
            "Nothing was checked and nothing was written: the input held no usable "
            "address and no status row could be created."))
        return


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
