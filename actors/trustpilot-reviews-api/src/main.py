"""Trustpilot reviews: a row per review, and money only for a review that landed.

Money rule of this park: a paid row is written and charged by one call
(Actor.push_data(..., charged_event_name=...)) and the SDK answers how many items it
really charged. Everything else is a free row that says in plain words why the buyer
got nothing: a broken address, a duplicate, a company with no profile, a wall instead
of a listing, a listing with no reviews, reviews the buyer's own filters excluded, a
budget that ran out, or a price that could not be read.

A review that was read but not delivered is never lost in silence: when the run stops
for time, for the spending cap or after a charge error, every read review comes back
as a free row naming the reason, so the dataset always accounts for what the pages
gave us.

The wall in front of Trustpilot is passed the only way it really is passed: a headful
Chromium on a virtual display (see browser.py for the three-step measurement). No
proxy, no third-party key, no user's browser profile.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from apify import Actor

from . import trustpilot as tp
from .browser import Browser


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id() -> str:
    return str(os.environ.get("ACTOR_RUN_ID") or "").strip()


def int_or(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number


def build_tasks(lines: list[str], max_companies: int,
                refuse_all: str = "") -> tuple[list[dict], dict]:
    """Companies to read, and the free rows for the lines we never will."""
    tasks: list[dict] = []
    free: list[dict] = []
    seen: set[str] = set()
    counts = {"invalid": 0, "duplicate": 0, "limited": 0}
    if refuse_all:
        # A wrong setting is refused before the browser opens: every line is named,
        # nothing is read and nothing is charged.
        for index, line in enumerate(lines):
            counts["invalid"] += 1
            free.append(tp.status_row(line, index, tp.ST_INVALID, refuse_all))
        return tasks, {"free": free, "counts": counts}
    for index, line in enumerate(lines):
        domain, problem = tp.normalize_company(line)
        if not domain:
            counts["invalid"] += 1
            free.append(tp.status_row(line, index, tp.ST_INVALID, problem))
            continue
        if domain in seen:
            counts["duplicate"] += 1
            free.append(tp.status_row(line, index, tp.ST_DUPLICATE,
                                      "the same company is already in this run: not "
                                      "read twice and not charged twice"))
            continue
        seen.add(domain)
        if len(tasks) >= max_companies:
            counts["limited"] += 1
            free.append(tp.status_row(line, index, tp.ST_LIMIT,
                                      f"the run is limited to {max_companies} "
                                      "companies: not read and not charged"))
            continue
        tasks.append({"line": line, "index": index, "domain": domain})
    return tasks, {"free": free, "counts": counts}


def company_meta(runs: list[dict]) -> dict:
    for data in runs:
        meta = data.get("meta") or {}
        if meta.get("name"):
            return meta
    return {}


def item_count_of(meta) -> int | None:
    """The dataset item count of the SDK's own metadata model, or None."""
    count = getattr(meta, "item_count", None)
    if count is None and isinstance(meta, dict):
        count = meta.get("item_count")
    return count if isinstance(count, int) else None


async def main() -> None:
    async with Actor:
        raw_input = await Actor.get_input() or {}
        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()
        can_charge, charge_reason = tp.price_state(charging)
        billable_claim = None if can_charge is None else bool(can_charge)
        # An unreadable price is a stop, not a gift: the park rule is "do not deliver
        # and do not charge". Delivering everything free here would be money lost by
        # the seller and a made-up promise to the buyer.
        price_unknown = can_charge is None

        lines = raw_input.get("companies") or []
        if isinstance(lines, str):
            lines = [lines]
        lines = [str(item) for item in lines]

        max_reviews = int_or(raw_input.get("maxReviewsPerCompany"), tp.MAX_REVIEWS_DEFAULT)
        max_reviews = max(1, min(max_reviews, tp.MAX_REVIEWS_HARD))
        max_pages = int_or(raw_input.get("maxPagesPerCompany"), tp.MAX_PAGES_DEFAULT)
        max_pages = max(1, min(max_pages, tp.MAX_PAGES_HARD))
        max_companies = int_or(raw_input.get("maxCompanies"), tp.MAX_COMPANIES_DEFAULT)
        max_companies = max(1, min(max_companies, tp.MAX_COMPANIES_HARD))
        buckets = tp.star_buckets(raw_input.get("stars"))
        plan = tp.star_plan(buckets, max_pages)
        cutoff = str(raw_input.get("publishedAfter") or "").strip() or None
        language = str(raw_input.get("language") or "").strip() or None
        include_replies = bool(raw_input.get("includeReplies", True))

        tasks, planned = build_tasks(lines, max_companies,
                                     tp.stars_problem(raw_input.get("stars"), buckets))
        free_rows = planned["free"]
        invalid = planned["counts"]["invalid"]
        duplicates = planned["counts"]["duplicate"]
        limited = planned["counts"]["limited"]

        by_status: dict[str, int] = {}
        rows_written = 0
        write_errors = 0
        reviews_seen = 0
        reviews_unique = 0
        reviews_delivered = 0
        reviews_filtered = 0
        reviews_duplicate = 0
        reviews_mismatch = 0
        reviews_unkeyed = 0
        reviews_limited = 0
        reviews_capped = 0
        reviews_stopped = 0
        charged_total = 0
        delivered_uncharged = 0
        charge_errors = 0
        charge_uncertain = 0
        pages_fetched = 0
        companies_attempted = 0
        companies_read = 0
        companies_partial = 0
        companies_stopped = 0
        companies_skipped = 0
        companies_mismatch = 0
        reviews_uncertain = 0
        companies_not_found = 0
        companies_blocked = 0
        companies_no_reviews = 0
        companies_filtered = 0
        companies_failed = 0
        stop_reason: str | None = "billing-price-unknown" if price_unknown else None
        stopping = bool(price_unknown)
        dataset_note: str | None = None
        browser_mode = "not started"
        browser_reason = ""

        def note(status: str) -> None:
            by_status[status] = by_status.get(status, 0) + 1

        async def write_free(row: dict) -> bool:
            nonlocal rows_written, write_errors, dataset_note
            row["billable"] = False
            try:
                await dataset.push_data(row)
            except Exception:  # noqa: BLE001
                write_errors += 1
                dataset_note = "a free status row was refused by the dataset"
                return False
            rows_written += 1
            return True

        async def dataset_items() -> int | None:
            try:
                meta = await dataset.get_metadata()
            except Exception:  # noqa: BLE001 - an unreadable counter is not a failure
                return None
            return item_count_of(meta)

        async def free_status_row(line, index, status: str, reason: str) -> None:
            await write_free(tp.status_row(line, index, status, reason))

        async def flush_undelivered(rows: list[dict]) -> None:
            """Name every review that was read but did not reach the dataset."""
            nonlocal reviews_limited, reviews_stopped
            status, reason = tp.undelivered_status(stop_reason)
            for row in rows:
                reviews_limited += 1
                reviews_stopped += 1
                note(status)
                await free_status_row(row.get("input"), row.get("inputIndex", 0),
                                      status, reason)

        async def write_paid(rows: list[dict]) -> None:
            """Write and charge one batch. Stops the run on any doubt about money."""
            nonlocal rows_written, reviews_delivered, charged_total, delivered_uncharged
            nonlocal charge_errors, charge_uncertain, stopping, stop_reason, dataset_note
            nonlocal write_errors, reviews_limited, reviews_capped, reviews_uncertain
            if can_charge is False:
                for row in rows:
                    row["billable"] = False
                try:
                    await dataset.push_data(rows)
                except Exception:  # noqa: BLE001
                    write_errors += 1
                    dataset_note = "free rows were refused by the dataset"
                    stopping = True
                    stop_reason = stop_reason or "dataset-write-error"
                    return
                rows_written += len(rows)
                reviews_delivered += len(rows)
                return
            if charging.is_event_charge_limit_reached(tp.EVENT_NAME):
                stopping = True
                stop_reason = "spending-cap"
                for row in rows:
                    reviews_limited += 1
                    reviews_capped += 1
                    note(tp.ST_CAP)
                    await free_status_row(row.get("input"), row.get("inputIndex", 0),
                                          tp.ST_CAP,
                                          "the spending cap was reached: not delivered "
                                          "and not charged")
                return
            for row in rows:
                row["billable"] = billable_claim
            try:
                result = await Actor.push_data(rows, charged_event_name=tp.EVENT_NAME)
            except Exception as exc:  # noqa: BLE001
                # One call writes and then charges, so an exception does not say
                # which half failed. Repeating the batch could give the buyer two
                # rows per review, so the run stops and the doubt is named. The
                # dataset counter is read once, lazily: if it moved by this batch,
                # those rows are delivered without money and the summary says so.
                landed = await dataset_items()
                if landed is not None and landed >= rows_written + len(rows):
                    delivered_uncharged += len(rows)
                    reviews_delivered += len(rows)
                    rows_written += len(rows)
                    dataset_note = (f"a paid batch of {len(rows)} is in the dataset "
                                    f"({landed} rows there) and the charge failed: "
                                    "delivered without money")
                else:
                    charge_uncertain += len(rows)
                    reviews_uncertain += len(rows)
                    dataset_note = (f"a paid batch of {len(rows)} could not be confirmed "
                                    f"after {type(exc).__name__}: it may be in the "
                                    "dataset and it may be uncharged")
                charge_errors += 1
                stopping = True
                stop_reason = "charge-error"
                return
            charged = int(getattr(result, "charged_count", 0) or 0)
            limit_reached = bool(getattr(result, "event_charge_limit_reached", False))
            if charged < len(rows):
                # The SDK writes exactly the items it charged for and drops the rest
                # of the batch, so those reviews never reached the dataset and must
                # be named, not lost.
                for _ in range(charged):
                    note(tp.ST_OK)
                reviews_delivered += charged
                charged_total += charged
                rows_written += charged
                rest = rows[charged:]
                reviews_limited += len(rest)
                reviews_capped += len(rest)
                for row in rest:
                    note(tp.ST_CAP)
                    await free_status_row(row.get("input"), row.get("inputIndex", 0),
                                          tp.ST_CAP,
                                          "the spending cap was reached: not delivered "
                                          "and not charged")
                stopping = True
                stop_reason = "spending-cap" if limit_reached else "charge-limit"
                return
            for _ in range(charged):
                note(tp.ST_OK)
            reviews_delivered += charged
            charged_total += charged
            rows_written += len(rows)

        browser = Browser()
        start_error = await browser.start()
        browser_mode = browser.mode
        browser_reason = browser.reason
        if start_error:
            # No window means no listing at all: every company gets its free row
            # with the reason, and the run stops with a human message, not a crash.
            for task in tasks:
                note(tp.ST_ERROR)
                await free_status_row(task["line"], task["index"], tp.ST_ERROR,
                                      f"the browser could not start: {start_error}")
            await Actor.fail(status_message=(
                f"The browser could not start: {start_error}. Nothing was read and "
                "nothing was charged."))
            return

        try:
            for task in tasks:
                if stopping:
                    companies_skipped += 1
                    status, reason = tp.stopped_status(stop_reason)
                    note(status)
                    await free_status_row(task["line"], task["index"], status, reason)
                    continue

                left = tp.deadline_seconds()
                if left is not None and left < tp.page_margin_seconds():
                    stopping = True
                    stop_reason = "deadline"
                    companies_skipped += 1
                    status, reason = tp.stopped_status(stop_reason)
                    note(status)
                    await free_status_row(task["line"], task["index"], status, reason)
                    continue

                companies_attempted += 1
                domain = task["domain"]
                collected: dict[str, dict] = {}
                company_runs: list[dict] = []
                blocked = False
                not_found = False
                login_wall = False
                http_error: int | None = None
                failure = ""
                company_seen = 0
                company_mismatch = 0
                nav_s, wait_s = tp.nav_budget_seconds(left)
                opened = await browser.open_listing(tp.review_url(domain),
                                                    wait_s=wait_s,
                                                    nav_timeout_s=nav_s)
                if opened is None:
                    opened = {"ok": True}
                if not opened.get("ok"):
                    failure = str(opened.get("error") or "the listing did not open")

                if not failure:
                    for bucket, start_page, last_page in plan:
                        for page in range(start_page, last_page + 1):
                            if len(collected) >= max_reviews:
                                break
                            left = tp.deadline_seconds()
                            if left is not None and left < tp.page_margin_seconds():
                                stopping = True
                                stop_reason = "deadline"
                                break
                            url = tp.review_url(domain, page, bucket)
                            data = await browser.page_data(url,
                                                           timeout_s=tp.PAGE_TIME_BUDGET_S)
                            pages_fetched += 1
                            status_code = data.get("status")
                            if data.get("timeout"):
                                failure = "a listing page did not answer in time"
                                break
                            if data.get("fetchError"):
                                failure = str(data.get("fetchError"))[:200]
                                break
                            if status_code == 404:
                                not_found = True
                                break
                            if status_code in (403, 429):
                                blocked = True
                                break
                            if data.get("login"):
                                # Past the anonymous cap Trustpilot does not answer an
                                # empty list, it sends the reader to its login page.
                                # Measured 2026-11-11: page 11 of a plain listing and
                                # of a star listing both land on /users/connect, so a
                                # login page must never read as "company has no reviews".
                                # No right now: a login page is a wall, and a wall is free.
                                login_wall = True
                                break
                            # A page that is not a listing is never a paid page.
                            if data.get("noJson"):
                                head = str(data.get("head") or "")
                                if tp.is_blocked(head, status_code):
                                    blocked = True
                                else:
                                    http_error = status_code
                                break
                            if status_code and status_code >= 400:
                                http_error = status_code
                                break
                            reviews = data.get("reviews") or []
                            company_runs.append(data)
                            if not reviews:
                                break
                            before = len(collected)
                            stop_pages = False
                            for raw in reviews:
                                reviews_seen += 1
                                company_seen += 1
                                review = tp.slim_review(raw, domain, bucket,
                                                        company_meta(company_runs))
                                if bucket is not None and review.get("rating") != bucket:
                                    # The star listing answered with a foreign rating:
                                    # it would be a paid row the buyer did not ask for.
                                    reviews_mismatch += 1
                                    company_mismatch += 1
                                    continue
                                key = tp.review_meta_key(review)
                                if not key:
                                    reviews_unkeyed += 1
                                    continue
                                if key in collected:
                                    reviews_duplicate += 1
                                    continue
                                collected[key] = review
                                if len(collected) >= max_reviews:
                                    stop_pages = True
                                    break
                            if stop_pages:
                                break
                            if len(collected) == before:
                                # Every id on this page was already sold from another
                                # bucket: deeper pages of this listing repeat them, so
                                # stop here instead of paying for the same reviews twice.
                                break
                        if blocked or login_wall or not_found or http_error or failure or stopping:
                            break
                        if len(collected) >= max_reviews:
                            break

                if not collected:
                    # Nothing came out of this company: exactly one status row, and
                    # the reason is the true one, not "no reviews" by default.
                    if failure:
                        companies_failed += 1
                        status, reason = tp.ST_ERROR, (f"the listing could not be read: "
                                                       f"{failure}")
                    elif not_found:
                        companies_not_found += 1
                        status, reason = tp.ST_NOT_FOUND, (
                            "Trustpilot has no company profile at this domain: nothing "
                            "to read, not charged")
                    elif login_wall:
                        companies_blocked += 1
                        status, reason = tp.ST_BLOCKED, (
                            "Trustpilot answered with its login page instead of the "
                            "listing (the anonymous cap): nothing was read and nothing "
                            "was charged")
                    elif blocked:
                        companies_blocked += 1
                        status, reason = tp.ST_BLOCKED, (
                            "a bot wall answered instead of the listing: nothing was "
                            "read and nothing was charged")
                    elif http_error:
                        status, reason = tp.ST_HTTP, (
                            f"the listing answered HTTP {http_error}: nothing was read "
                            "and nothing was charged")
                    elif company_mismatch:
                        companies_mismatch += 1
                        status, reason = tp.ST_ERROR, (
                            f"{company_mismatch} reviews came back from a star listing "
                            "with a rating it does not carry: nothing was delivered and "
                            "nothing was charged")
                    elif stopping:
                        companies_stopped += 1
                        status, reason = tp.stopped_status(stop_reason)
                    else:
                        companies_no_reviews += 1
                        status, reason = tp.ST_NO_REVIEWS, (
                            "the company profile is open and has no reviews to read in "
                            "the pages asked: not charged")
                    note(status)
                    await free_status_row(task["line"], task["index"], status, reason)
                    continue

                meta = company_meta(company_runs)
                reviews_unique += len(collected)
                keep: list[dict] = []
                for review in collected.values():
                    if (not tp.after_published(review, cutoff)
                            or not tp.language_ok(review, language)):
                        reviews_filtered += 1
                        continue
                    row = dict(review)
                    row["input"] = task["line"]
                    row["inputIndex"] = task["index"]
                    row["status"] = tp.ST_OK
                    row["reason"] = ""
                    if not include_replies and row.get("companyReply"):
                        row["companyReply"] = None
                        row["companyReplyDate"] = None
                    keep.append(row)

                if not keep:
                    companies_filtered += 1
                    note(tp.ST_FILTERED)
                    await free_status_row(
                        task["line"], task["index"], tp.ST_FILTERED,
                        f"{len(collected)} reviews were read and every one was excluded "
                        "by the filters you set (publishedAfter / language): not charged")
                    continue

                companies_read += 1
                if failure or blocked or not_found or http_error:
                    companies_partial += 1
                start = 0
                while start < len(keep):
                    if stopping:
                        await flush_undelivered(keep[start:])
                        break
                    batch = keep[start:start + tp.PAID_BATCH]
                    await write_paid(batch)
                    start += len(batch)
                    if stopping:
                        await flush_undelivered(keep[start:])
                        break

        finally:
            await browser.close()

        for row in free_rows:
            # Every row counts in byStatus, including the ones made before the
            # browser opened: a status table that skips them is a table that lies.
            note(str(row.get("status") or ""))
            await write_free(row)

        companies_requested = len(lines)
        companies_not_attempted = invalid + duplicates + limited + companies_skipped
        consistent = (
            companies_requested == companies_attempted + companies_not_attempted
            and reviews_unique == (reviews_delivered + reviews_filtered + reviews_limited
                                   + reviews_uncertain)
            and reviews_seen == (reviews_unique + reviews_duplicate
                                 + reviews_mismatch + reviews_unkeyed)
        )
        summary = {
            "generatedAt": now_iso(),
            "runId": run_id(),
            "browserMode": browser_mode,
            "browserNote": browser_reason,
            "eventName": tp.EVENT_NAME,
            "pricingEventPriced": None if can_charge is None else bool(can_charge),
            "billingActive": None if can_charge is None else bool(can_charge),
            "pricingNote": charge_reason,
            "stopReason": stop_reason or "",
            "companiesRequested": companies_requested,
            "companiesAttempted": companies_attempted,
            "companiesRead": companies_read,
            "companiesPartial": companies_partial,
            "companiesStopped": companies_stopped,
            "companiesSkipped": companies_skipped,
            "companiesBucketMismatch": companies_mismatch,
            "companiesNotAttempted": companies_not_attempted,
            "companiesInvalid": invalid,
            "companiesDuplicate": duplicates,
            "companiesLimited": limited,
            "companiesNotFound": companies_not_found,
            "companiesBlocked": companies_blocked,
            "companiesNoReviews": companies_no_reviews,
            "companiesFilteredOut": companies_filtered,
            "companiesFailed": companies_failed,
            "pagesFetched": pages_fetched,
            "pagePlan": [[bucket, first, last] for bucket, first, last in plan],
            "reviewsSeen": reviews_seen,
            "reviewsUnique": reviews_unique,
            "reviewsDelivered": reviews_delivered,
            "reviewsFiltered": reviews_filtered,
            "reviewsDuplicates": reviews_duplicate,
            "reviewsLimited": reviews_limited,
            "reviewsCapped": reviews_capped,
            "reviewsStopped": reviews_stopped,
            "reviewsBucketMismatch": reviews_mismatch,
            "reviewsWithoutId": reviews_unkeyed,
            "reviewsUncertain": reviews_uncertain,
            "starBucketsFromPage": 2,
            "chargedEvents": charged_total,
            "deliveredUncharged": delivered_uncharged,
            "chargeUncertain": charge_uncertain,
            "chargeErrors": charge_errors,
            "rowsWritten": rows_written,
            "writeErrors": write_errors,
            "byStatus": by_status,
            "counterMismatch": not consistent,
            "datasetNote": dataset_note,
            "starBuckets": buckets,
            "maxReviewsPerCompany": max_reviews,
            "maxPagesPerCompany": max_pages,
            "maxCompanies": max_companies,
            "publishedAfter": cutoff,
            "language": language,
        }

        header = (f"{companies_requested} compan{'y' if companies_requested == 1 else 'ies'}, "
                  f"{reviews_delivered} reviews delivered, {charged_total} charged")
        if stop_reason:
            header += f", stopped: {stop_reason}"
        try:
            await Actor.set_status_message(header)
        except Exception:  # noqa: BLE001 - a missing header must not fail a run
            pass

        run = run_id()
        for key in (["RUN_SUMMARY", f"RUN_SUMMARY-{run}"] if run else ["RUN_SUMMARY"]):
            try:
                await Actor.set_value(key, summary)
            except Exception:  # noqa: BLE001
                pass
        try:
            store = await Actor.open_key_value_store(name=tp.SUMMARY_STORE)
            await store.set_value(f"RUN_SUMMARY-{run}" if run else "RUN_SUMMARY", summary)
        except Exception:  # noqa: BLE001 - the run store still holds the summary
            pass

        if not tasks and not free_rows:
            await Actor.fail(status_message=(
                "No companies in the input. Add at least one domain, for example "
                "amazon.com."))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
