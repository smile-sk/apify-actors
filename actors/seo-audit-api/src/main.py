"""SEO audit: a 0-100 on-page score per page, with a fix hint for every issue.

Money rule of this actor, the same as everywhere in this park: a row is delivered
with a charge, or it is delivered free with a status that says in plain words why
the buyer got no audit. Nothing is charged for a page we could not measure, for a
page that answered 404, for content that is not a page, or for an address that
points inside the private network.

The paid push goes through Actor.push_data(..., charged_event_name=...), the only
SDK entry point that writes the row and charges for it under one charge lock, and
it answers with a ChargeResult. The budget is checked BEFORE that call as well: on
an exhausted budget the SDK deliberately lets one more row through so the platform
kills the run, and that row would be the buyer paying for a run that dies.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from apify import Actor

from .net import host_is_public, host_of, normalize_url
from .seo import Browser, PageError

EVENT_NAME = "page-audited"
SUMMARY_STORE = "seo-audit-api-summary"
MAX_PAGES_DEFAULT = 10
# Worst case for one page is its own navigation timeout, plus the network idle wait
# capped at 5 s, plus settleSecs, plus the browser tearing the context down. The
# margin is computed from those settings instead of being a constant: a constant
# smaller than the page it guards lets the platform kill the run mid page, and a
# killed run leaves no summary at all.
PAGE_MARGIN_EXTRA_S = 15.0
NETWORK_IDLE_CAP_MS = 5_000
# After a page is measured its subresource hosts are resolved one by one, up to 25
# names. On a slow container resolver that is seconds of work the page itself never
# pays for, so the margin has to carry the worst case or the next page starts on a
# clock that is already spent.
SUBRESOURCE_GUARD_S = 5.0

ST_OK = "OK"
ST_INVALID = "NOT_CHECKED_INVALID"
ST_DUPLICATE = "NOT_CHECKED_DUPLICATE"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_PRIVATE = "NOT_CHECKED_PRIVATE_ADDRESS"
ST_DNS = "NOT_CHECKED_DNS_ERROR"
ST_TIMEOUT = "NOT_CHECKED_TIMEOUT"
ST_HTTP = "NOT_CHECKED_HTTP_ERROR"
ST_HTML = "NOT_CHECKED_NOT_HTML"
ST_PROBE = "NOT_CHECKED_PROBE_ERROR"

MAX_REFUSALS_IN_A_ROW = 3


def run_id() -> str:
    return (
        os.environ.get("ACTOR_RUN_ID")
        or (Actor.get_env() or {}).get("run_id")
        or "run"
    )


def deadline_seconds() -> float | None:
    """Seconds left before the platform kills the run, or None when unknown."""
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
    return (moment - datetime.now(timezone.utc)).total_seconds()


def page_margin_seconds(page_timeout_ms: int, settle_ms: int) -> float:
    """How long one page can take in the worst case, plus a breath."""
    navigation = page_timeout_ms / 1000.0
    idle = min(NETWORK_IDLE_CAP_MS, page_timeout_ms) / 1000.0
    return (navigation + idle + settle_ms / 1000.0 + SUBRESOURCE_GUARD_S
            + PAGE_MARGIN_EXTRA_S)


def host_status(reason: str | None) -> str:
    """A host that does not resolve is not a private address.

    The buyer who typed a host name that does not exist needs to see a typo, not a
    network investigation: 'private address' would send them looking for a firewall
    that has nothing to do with it.
    """
    return ST_PRIVATE if "not a public address" in (reason or "") else ST_DNS


def subresource_problem(hosts) -> tuple[tuple[str, str] | None, int, int]:
    """The first subresource host that resolves inside the private network.

    Subresources are checked after the page has loaded, and this is said plainly
    rather than dressed up: by the time the numbers are read the browser has already
    asked for those resources, because the page's own requests start inside the
    navigation and a route handler does not see every one of them. So the promise is
    that such a page is recognised and never charged, not that the request was
    stopped. Blocking it for real needs a filtering proxy in front of the browser.

    A host that does not resolve at all is NOT a private address. A dead analytics
    or ad host inside the buyer's markup says nothing about the buyer's network, and
    calling it private sent people hunting for a firewall that had nothing to do
    with it while a perfectly measurable page went unpaid. Those hosts are counted
    in subresourceDnsSkipped and the page is measured as usual; only a name that
    really answered with a non public address stops the row.
    """
    skipped = lookups = 0
    for raw in hosts or []:
        name = str(raw or "").strip().lower()
        if not name:
            continue
        lookups += 1
        ok, why = host_is_public(name)
        if ok:
            continue
        if "not a public address" not in (why or ""):
            skipped += 1
            continue
        return (name, why or "the address is not public"), skipped, lookups
    return None, skipped, lookups


def status_row(line: str, index: int, status: str, reason: str) -> dict:
    return {
        "url": None,
        "input": line,
        "inputIndex": index,
        "status": status,
        "reason": reason,
        "billable": False,
    }


def stopped_status(stop_reason: str | None) -> tuple[str, str]:
    mapping = {
        "spending-cap": (ST_CAP, "the spending cap stopped the run earlier"),
        "billing-price-unknown": (
            ST_BILLING, "the run stopped earlier: the price was unknown"
        ),
        "deadline": (ST_TIME, "the run stopped earlier: the deadline was close"),
        "charge-error": (ST_ERROR, "the run stopped earlier: a charge failed"),
        "dataset-write-error": (
            ST_ERROR, "the run stopped earlier: the dataset refused rows"
        ),
        "unexpected-error": (ST_ERROR, "the run stopped earlier on an unexpected error"),
    }
    return mapping.get(stop_reason or "", (ST_ERROR, "the run stopped earlier"))


def build_tasks(lines: list, max_pages: int) -> tuple[list[dict], dict]:
    tasks: list[dict] = []
    seen: set[str] = set()
    unique = duplicates = invalid = blank = cut = 0
    for index, raw in enumerate(lines):
        line = "" if raw is None else str(raw).strip()
        url, reason = normalize_url(line)
        if url is None:
            invalid += 1
            if not line:
                blank += 1
            tasks.append({"kind": "row", "row": status_row(line, index, ST_INVALID, reason)})
            continue
        if url in seen:
            duplicates += 1
            tasks.append({"kind": "row", "row": status_row(
                line, index, ST_DUPLICATE, "the same url came earlier")})
            continue
        seen.add(url)
        unique += 1
        if max_pages and unique > max_pages:
            cut += 1
            tasks.append({"kind": "row", "row": status_row(
                line, index, ST_LIMIT, f"beyond maxPages={max_pages}")})
            continue
        tasks.append({"kind": "page", "url": url, "line": line, "index": index})
    return tasks, {"unique": unique, "duplicates": duplicates, "invalid": invalid,
                   "blank": blank, "cut": cut, "asked": len(lines)}


def price_state(charging) -> tuple[bool | None, str]:
    """Can this run charge? True / False / None (unknown) and a plain reason."""
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001 - any failure means "price unknown"
        return None, f"pricing read failed: {type(exc).__name__}"
    if info is None:
        return None, "pricing info is not readable"
    is_ppe = getattr(info, "is_pay_per_event", None)
    if is_ppe is None and isinstance(info, dict):
        is_ppe = info.get("isPayPerEvent", info.get("is_pay_per_event"))
    if is_ppe is False:
        return False, "the actor is not sold per event"
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
    if is_ppe is None:
        if names:
            return (EVENT_NAME in names), (
                f"{EVENT_NAME} is priced" if EVENT_NAME in names
                else f"{EVENT_NAME} is not in the price list")
        return None, "the pricing answer is not understood"
    if EVENT_NAME not in names:
        return False, f"{EVENT_NAME} is not priced, rows go out free"
    return True, f"{EVENT_NAME} is priced"


async def dataset_count(dataset) -> int | None:
    try:
        meta = await dataset.get_metadata()
    except Exception:  # noqa: BLE001 - a missing counter is not a lost row
        return None
    value = getattr(meta, "item_count", None)
    if value is None and isinstance(meta, dict):
        value = meta.get("itemCount", meta.get("item_count"))
    return value if isinstance(value, int) else None


def make_row(url: str, line: str, index: int, metrics: dict) -> dict:
    row = {
        "url": url,
        "input": line,
        "inputIndex": index,
        "status": ST_OK,
        "reason": None,
        "billable": False,
    }
    row.update(metrics)
    return row


def is_html(content_type: str | None) -> bool:
    """A page, not a pdf and not an image. An empty type is treated as a page,
    because some servers do not send one and refusing those would be a guess."""
    text = (content_type or "").lower()
    if not text:
        return True
    return "html" in text or "xml" in text


async def main() -> None:
    async with Actor:
        raw_input = await Actor.get_input() or {}
        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()
        can_charge, charge_reason = price_state(charging)
        # billable is a claim about the price, not a receipt: one paid push writes
        # the row first and charges second, so a row whose charge fails is already
        # in the dataset. The money that was really taken is chargedEvents in the
        # summary, and such a row is counted in deliveredUncharged.
        billable_claim = None if can_charge is None else bool(can_charge)

        lines = raw_input.get("urls") or []
        if isinstance(lines, str):
            lines = [lines]
        lines = list(lines)

        try:
            max_pages = int(raw_input.get("maxPages") or 0)
        except (TypeError, ValueError):
            max_pages = 0
        if max_pages <= 0:
            max_pages = MAX_PAGES_DEFAULT

        strategy = str(raw_input.get("strategy") or "mobile").lower()
        if strategy not in ("mobile", "desktop"):
            strategy = "mobile"
        try:
            settle_ms = int(raw_input.get("settleSecs", 2) or 0) * 1000
        except (TypeError, ValueError):
            settle_ms = 2000
        settle_ms = max(0, min(settle_ms, 10_000))
        try:
            page_timeout_ms = int(float(raw_input.get("pageTimeoutSecs", 45) or 45) * 1000)
        except (TypeError, ValueError):
            page_timeout_ms = 45_000
        page_timeout_ms = max(5_000, min(page_timeout_ms, 120_000))
        ignore_https_errors = bool(raw_input.get("ignoreHttpsErrors"))
        page_margin_s = page_margin_seconds(page_timeout_ms, settle_ms)

        tasks, counts = build_tasks(lines, max_pages)

        by_status: dict[str, int] = {}
        by_grade: dict[str, int] = {}
        title_seen: dict[str, int] = {}
        rows_written = 0
        delivered = 0
        charged_total = 0
        delivered_uncharged = 0
        charge_errors = 0
        write_errors = 0
        private_blocked = 0
        subresource_blocked = 0
        subresource_dns_skipped = 0
        subresource_lookups = 0
        host_list_truncated = 0
        dns_errors = 0
        http_errors = 0
        not_html = 0
        timeouts = 0
        probe_errors = 0
        stop_reason: str | None = None
        deadline_capped = False
        unexpected_error: str | None = None
        dataset_api_count: int | None = None
        dataset_note: str | None = None
        billing_active = can_charge is True
        stopping = False
        refusals_in_a_row = 0
        failed_at = 0
        browser = Browser()

        def note(status: str) -> None:
            by_status[status] = by_status.get(status, 0) + 1

        async def write_free(row: dict) -> bool:
            nonlocal rows_written, write_errors, dataset_note
            nonlocal stopping, stop_reason, refusals_in_a_row
            row["billable"] = False
            try:
                await dataset.push_data(row)
            except Exception:  # noqa: BLE001
                write_errors += 1
                refusals_in_a_row += 1
                dataset_note = "a free status row was refused by the dataset"
                if refusals_in_a_row >= MAX_REFUSALS_IN_A_ROW:
                    stopping = True
                    stop_reason = stop_reason or "dataset-write-error"
                return False
            rows_written += 1
            refusals_in_a_row = 0
            return True

        async def stop_row(line: str, index: int) -> None:
            status, reason = stopped_status(stop_reason)
            note(status)
            if stop_reason == "dataset-write-error":
                return
            await write_free(status_row(line, index, status, reason))

        try:
            for position, task in enumerate(tasks):
                failed_at = position
                if task["kind"] == "row":
                    # Invalid, duplicate and limit lines are decided before any page
                    # is opened, so they are written as they are, whatever the run
                    # decided afterwards: their own status is the true one.
                    row = task["row"]
                    written = await write_free(row)
                    note(row["status"] if written else ST_ERROR)
                    continue

                if stopping:
                    await stop_row(task["line"], task["index"])
                    continue

                left = deadline_seconds()
                if left is not None and left < page_margin_s:
                    deadline_capped = True
                    stopping = True
                    stop_reason = stop_reason or "deadline"
                    note(ST_TIME)
                    await write_free(status_row(
                        task["line"], task["index"], ST_TIME,
                        "the platform deadline was close, nothing was delivered and "
                        "nothing was charged"))
                    continue

                if can_charge is None:
                    stopping = True
                    stop_reason = "billing-price-unknown"
                    note(ST_BILLING)
                    await write_free(status_row(
                        task["line"], task["index"], ST_BILLING,
                        f"the price could not be read ({charge_reason}): not delivered "
                        "and not charged"))
                    continue

                if can_charge and charging.is_event_charge_limit_reached(EVENT_NAME):
                    stopping = True
                    stop_reason = "spending-cap"
                    note(ST_CAP)
                    await write_free(status_row(
                        task["line"], task["index"], ST_CAP,
                        "the spending cap was reached: not delivered and not charged"))
                    continue

                url = task["url"]
                ok, why = host_is_public(host_of(url))
                if not ok:
                    status = host_status(why)
                    if status == ST_PRIVATE:
                        private_blocked += 1
                    else:
                        dns_errors += 1
                    note(status)
                    await write_free(status_row(
                        task["line"], task["index"], status,
                        f"the address cannot be used, the page was not opened: {why}"))
                    continue

                try:
                    metrics = await browser.measure(
                        url,
                        strategy=strategy,
                        timeout_ms=page_timeout_ms,
                        settle_ms=settle_ms,
                        ignore_https_errors=ignore_https_errors,
                    )
                except PageError as exc:
                    if exc.kind == "timeout":
                        timeouts += 1
                    else:
                        probe_errors += 1
                    status = ST_TIMEOUT if exc.kind == "timeout" else ST_PROBE
                    note(status)
                    await write_free(status_row(
                        task["line"], task["index"], status,
                        f"{exc.reason}: nothing was delivered and nothing was charged"))
                    continue
                except Exception as exc:  # noqa: BLE001 - one page never kills the run
                    probe_errors += 1
                    note(ST_PROBE)
                    await write_free(status_row(
                        task["line"], task["index"], ST_PROBE,
                        f"{type(exc).__name__}: the page could not be measured, nothing "
                        "was delivered and nothing was charged"))
                    continue

                status_code = metrics.get("httpStatus")
                if isinstance(status_code, int) and status_code >= 400:
                    http_errors += 1
                    note(ST_HTTP)
                    await write_free(status_row(
                        task["line"], task["index"], ST_HTTP,
                        f"the page answered HTTP {status_code}: not measured and not "
                        "charged"))
                    continue
                if metrics.get("resourceHostsTruncated"):
                    # The host list is capped, so the guard below sees at most the
                    # first hosts of the page. A cut list that nobody counts looks
                    # exactly like a complete one, and a private address beyond the
                    # cap would pass unseen.
                    host_list_truncated += 1
                if not is_html(metrics.get("contentType")):
                    not_html += 1
                    note(ST_HTML)
                    await write_free(status_row(
                        task["line"], task["index"], ST_HTML,
                        f"the response is {metrics.get('contentType')}, not a page: "
                        "not charged"))
                    continue

                # A public url may answer with a redirect into the private network.
                # The browser has already followed it by now, so the whole chain and
                # the address that was really loaded are checked, and the final url
                # is checked too: a chain without its last hop says nothing about
                # where the browser ended up.
                hops = [*(metrics.get("redirects") or []), metrics.get("finalUrl") or ""]
                redirected_private = None
                for hop in hops:
                    if not hop:
                        continue
                    hop_ok, hop_why = host_is_public(host_of(hop))
                    if not hop_ok:
                        redirected_private = f"{hop}: {hop_why}"
                        break
                if redirected_private:
                    private_blocked += 1
                    note(ST_PRIVATE)
                    await write_free(status_row(
                        task["line"], task["index"], ST_PRIVATE,
                        f"a redirect led to a non public address ({redirected_private}): "
                        "the page was recognised after loading and nothing was charged"))
                    continue

                private_resource, dns_skipped, lookups = subresource_problem(
                    metrics.get("resourceHosts"))
                subresource_dns_skipped += dns_skipped
                subresource_lookups += lookups
                if private_resource:
                    subresource_blocked += 1
                    private_blocked += 1
                    note(ST_PRIVATE)
                    await write_free(status_row(
                        task["line"], task["index"], ST_PRIVATE,
                        "a subresource host is not public "
                        f"({private_resource[0]}: {private_resource[1]}): the page was "
                        "not measured and nothing was charged"))
                    continue

                row = make_row(url, task["line"], task["index"], metrics)
                grade_name = metrics.get("seoGrade")
                if grade_name:
                    by_grade[grade_name] = by_grade.get(grade_name, 0) + 1
                # The same title on two pages of one site is a real problem that a
                # single page cannot see, so it is counted across the whole run.
                title_key = (metrics.get("title") or "").strip().lower()
                if title_key:
                    title_seen[title_key] = title_seen.get(title_key, 0) + 1

                if can_charge:
                    if charging.is_event_charge_limit_reached(EVENT_NAME):
                        note(ST_CAP)
                        await write_free(status_row(
                            task["line"], task["index"], ST_CAP,
                            "the spending cap was reached: not delivered and not charged"))
                        stopping = True
                        stop_reason = "spending-cap"
                        continue
                    try:
                        result = await Actor.push_data(
                            {**row, "billable": billable_claim},
                            charged_event_name=EVENT_NAME,
                        )
                    except Exception as exc:  # noqa: BLE001
                        # One SDK call writes the row and then charges for it, so an
                        # exception here does not say which half failed, and the
                        # dataset counter does not answer that either: it lags by
                        # minutes, and a zero read right after a write is a lag, not
                        # a refusal. The one honest conclusion is that the row may be
                        # in the dataset and no money was taken. A second status row
                        # for the same input would hand the buyer two rows for one
                        # page, and calling it a dataset refusal would be a guess.
                        charge_errors += 1
                        refusals_in_a_row = 0
                        delivered_uncharged += 1
                        rows_written += 1
                        note(ST_OK)
                        stopping = True
                        stop_reason = "charge-error"
                        dataset_note = (
                            f"a paid row could not be confirmed after "
                            f"{type(exc).__name__}: it may be in the dataset and it "
                            "was not charged"
                        )
                        continue
                    charged = int(getattr(result, "charged_count", 0) or 0)
                    limit_reached = bool(getattr(result, "event_charge_limit_reached", False))
                    if charged <= 0:
                        status = ST_CAP if limit_reached else ST_ERROR
                        note(status)
                        await write_free(status_row(
                            task["line"], task["index"], status,
                            "the spending cap was reached: not delivered and not charged"
                            if limit_reached
                            else "nothing was written and nothing was charged"))
                        stopping = True
                        stop_reason = "spending-cap" if limit_reached else "charge-error"
                        continue
                    rows_written += 1
                    delivered += 1
                    charged_total += charged
                    refusals_in_a_row = 0
                    note(ST_OK)
                    if limit_reached:
                        stopping = True
                        stop_reason = "spending-cap"
                    continue

                row["billable"] = False
                written = await write_free(row)
                note(ST_OK if written else ST_ERROR)
        except Exception as exc:  # noqa: BLE001 - a crash must not lose the summary
            unexpected_error = f"{type(exc).__name__}: {exc}"[:300]
            stop_reason = stop_reason or "unexpected-error"
            # The tail gets its own free row with the reason. Without it the counters
            # do not add up, and the buyer cannot tell a page that was never tried
            # from one that was measured and lost.
            for rest in tasks[failed_at:]:
                if rest.get("kind") == "row":
                    row = rest.get("row") or {}
                    line = row.get("input") or ""
                    index = row.get("inputIndex") or 0
                else:
                    line = rest.get("line") or ""
                    index = rest.get("index") or 0
                note(ST_ERROR)
                await write_free(status_row(
                    line, index, ST_ERROR,
                    "the run stopped on an unexpected error before this page: nothing "
                    "was charged"))
        finally:
            await browser.close()
            dataset_api_count = await dataset_count(dataset)

        processed = by_status.get(ST_OK, 0)
        not_checked = sum(value for key, value in by_status.items() if key != ST_OK)
        asked = counts["asked"]
        if processed + not_checked != asked:
            by_status["counterMismatch"] = asked - (processed + not_checked)

        summary = {
            "requested": asked,
            "uniqueUrls": counts["unique"],
            "duplicates": counts["duplicates"],
            "invalidLines": counts["invalid"],
            "blankLines": counts["blank"],
            "processed": processed,
            "notChecked": not_checked,
            "delivered": processed,
            "rowsWritten": rows_written,
            "chargedEvents": charged_total if billing_active else 0,
            "billingActive": billing_active,
            "billingReason": charge_reason,
            "deliveredUncharged": delivered_uncharged,
            "chargeErrors": charge_errors,
            "writeErrors": write_errors,
            "stopReason": stop_reason,
            "unexpectedError": unexpected_error,
            "byStatus": by_status,
            "byGrade": by_grade,
            "duplicateTitles": sum(value - 1 for value in title_seen.values() if value > 1),
            "pagesWithDuplicateTitle": sum(1 for value in title_seen.values() if value > 1),
            "maxPages": max_pages or None,
            "cutByMaxPages": counts["cut"],
            "deadlineCapped": deadline_capped,
            "pageMarginSecs": round(page_margin_s, 1),
            "privateBlocked": private_blocked,
            "privateSubresourceBlocked": subresource_blocked,
            "subresourceDnsSkipped": subresource_dns_skipped,
            "subresourceHostLookups": subresource_lookups,
            "pagesWithTruncatedHostList": host_list_truncated,
            "dnsErrors": dns_errors,
            "httpErrors": http_errors,
            "notHtml": not_html,
            "timeouts": timeouts,
            "probeErrors": probe_errors,
            "strategy": strategy,
            "datasetItemCount": rows_written,
            "datasetItemCountApi": dataset_api_count,
            "datasetItemCountNote": dataset_note,
            "priceEvent": EVENT_NAME,
            "runId": run_id(),
            "finishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if dataset_api_count is not None and dataset_api_count < rows_written:
            lag = f"metadata lag: expected {rows_written}, reports {dataset_api_count}"
            # A note that already explains something is not thrown away for the lag:
            # both facts are true and the buyer needs the specific one first.
            summary["datasetItemCountNote"] = (
                f"{dataset_note}; {lag}" if dataset_note else lag
            )

        store = await Actor.open_key_value_store(name=SUMMARY_STORE)
        await store.set_value(f"RUN_SUMMARY-{run_id()}", summary)
        default_store = await Actor.open_key_value_store()
        await default_store.set_value("RUN_SUMMARY", summary)

        billed = "checked and charged" if billing_active else "checked, nothing charged"
        text = (
            f"{counts['unique']} unique url(s) from {asked} line(s), "
            f"{processed} {billed}, {not_checked} not checked"
        )
        if not billing_active:
            text += f" (billing is off: {charge_reason})"
        if stop_reason:
            text += f", stopped: {stop_reason}"
        await Actor.set_status_message(text)

        # A green run with nothing delivered is the same defect as a traceback: the
        # buyer sees SUCCEEDED and an empty dataset and cannot tell why.
        if asked == 0:
            await Actor.fail(
                status_message=(
                    "No urls were sent: nothing to audit, nothing charged. Put one page "
                    "url per line into the urls field."
                )
            )
        if unexpected_error:
            await Actor.fail(
                status_message=(
                    f"The run stopped on an unexpected error ({unexpected_error}). "
                    f"{processed} of {asked} page(s) delivered, {not_checked} not "
                    f"checked, {charged_total if billing_active else 0} charged."
                )
            )
        if asked > 0 and processed == 0:
            breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
            await Actor.fail(
                status_message=(
                    f"Nothing could be audited: {breakdown or 'no rows written'}. "
                    "Nothing was charged."
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
