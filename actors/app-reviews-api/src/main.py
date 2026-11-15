"""App reviews: one row per review of an iOS or Google Play app, money only for a row that landed.

Money rule of this park: a paid row is written and charged by one call
(Actor.push_data(..., charged_event_name=...)) and the SDK answers how many items it
really charged. Everything else is a free row that says in plain words why the buyer got
nothing: a line that is not an app, a duplicate, an app the store does not know, a wall,
an app without reviews, reviews the buyer's own filters removed, a budget that ran out,
or a price that could not be read.

A review that was read but not delivered is never lost in silence: when the run stops for
time, for the spending cap or after a charge error, every read review comes back as a free
row naming the reason, so the dataset always accounts for what the stores gave us.

Neither store needs a key, a proxy or a browser: Apple's RSS answers 50 reviews a page for
10 pages per storefront, Google Play answers by continuation token.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import httpx
from apify import Actor

from . import apps as ap


def run_id() -> str:
    return str(os.environ.get("ACTOR_RUN_ID") or "").strip()


def int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def deadline_at() -> float | None:
    """The moment the platform kills the run, as epoch seconds, or None when unknown."""
    raw = os.environ.get("ACTOR_TIMEOUT_AT")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def time_left(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.time()


def build_tasks(lines: list[str], max_apps: int,
                refuse_all: str = "") -> tuple[list[dict], dict]:
    """Apps to read, and the free rows for the lines we never will."""
    tasks: list[dict] = []
    free: list[dict] = []
    seen: set[str] = set()
    counts = {"invalid": 0, "duplicate": 0, "limited": 0}
    if refuse_all:
        for index, line in enumerate(lines):
            counts["invalid"] += 1
            free.append(ap.status_row(line, index, ap.ST_INVALID, refuse_all))
        return tasks, {"free": free, "counts": counts}
    for index, line in enumerate(lines):
        parsed, problem = ap.normalize_app(line)
        if not parsed:
            counts["invalid"] += 1
            free.append(ap.status_row(line, index, ap.ST_INVALID, problem))
            continue
        key = ap.app_key(parsed["platform"], parsed["appId"])
        if key in seen:
            counts["duplicate"] += 1
            free.append(ap.status_row(line, index, ap.ST_DUPLICATE,
                                      "the same app is already in this run: not read "
                                      "twice and not charged twice"))
            continue
        seen.add(key)
        if len(tasks) >= max_apps:
            counts["limited"] += 1
            free.append(ap.status_row(line, index, ap.ST_LIMIT,
                                      f"the run is limited to {max_apps} apps: not read "
                                      "and not charged"))
            continue
        tasks.append({"line": line, "index": index, **parsed})
    return tasks, {"free": free, "counts": counts}


def item_count_of(meta) -> int | None:
    count = getattr(meta, "item_count", None)
    if count is None and isinstance(meta, dict):
        count = meta.get("item_count")
    return count if isinstance(count, int) else None


async def fetch_json(client: httpx.AsyncClient, url: str,
                     who: str) -> tuple[int, dict | None, str]:
    try:
        response = await client.get(url)
    except Exception as exc:  # noqa: BLE001 - the network is not the buyer's fault
        return 0, None, f"the {who} feed could not be reached: {type(exc).__name__}"
    text = response.text or ""
    if response.status_code != 200 and ap.is_blocked(text):
        # A wall is an HTTP answer, not a phrase inside a review: a review that says
        # "access denied" used to turn a whole healthy feed into a wall.
        return response.status_code, None, f"a wall answered instead of the {who} feed"
    if response.status_code == 404:
        return 404, None, f"the {who} does not know this app"
    if response.status_code == 400:
        # Apple's own ceiling: page 11 of a storefront answers 400 (measured).
        return 400, None, ""
    if response.status_code != 200:
        return response.status_code, None, f"the {who} answered HTTP {response.status_code}"
    try:
        return 200, response.json(), ""
    except Exception:  # noqa: BLE001
        return 200, None, f"the {who} feed was not JSON"


async def apple_meta(client: httpx.AsyncClient, app_id: str, country: str) -> tuple[dict, str]:
    status, payload, problem = await fetch_json(
        client, ap.apple_lookup_url(country, app_id), "App Store lookup")
    if payload is None:
        return {}, problem or f"the App Store lookup answered HTTP {status}"
    results = payload.get("results") or []
    if not results:
        return {}, "the App Store does not know this app id"
    item = results[0]
    return {
        "title": item.get("trackName"),
        "rating": item.get("averageUserRating"),
        "ratingsTotal": item.get("userRatingCount"),
    }, ""


async def read_apple(client: httpx.AsyncClient, task: dict, country: str, meta: dict,
                     max_pages: int, budget: int, deadline: float | None,
                     skipped: dict | None = None) -> tuple[list[dict], int, str, str]:
    """Reviews of one Apple app in one storefront: rows, pages read, status, reason."""
    rows: list[dict] = []
    pages = 0
    for page in range(1, max_pages + 1):
        if len(rows) >= budget:
            break
        left = time_left(deadline)
        if left is not None and left < ap.PAGE_MARGIN_S:
            return rows, pages, "deadline", "the run ran out of its time budget"
        try:
            # One page cannot outlive the time the run has left, minus what the summary
            # needs: a slow server used to outlast the platform's kill and lose the summary.
            async with asyncio.timeout(max(2.0, (left if left is not None
                                                 else ap.REQUEST_TIMEOUT_S)
                                          - ap.SUMMARY_MARGIN_S)):
                status, payload, problem = await fetch_json(
                    client, ap.apple_feed_url(country, task["appId"], page), "App Store")
        except TimeoutError:
            return rows, pages, "deadline", "the run ran out of its time budget"
        if status == 400:
            break  # the storefront's own ceiling: not an error and not a charge
        if payload is None:
            if not problem:
                break
            return rows, pages, (ap.ST_BLOCKED if "wall" in problem else ap.ST_HTTP), problem
        pages += 1
        page_rows = ap.apple_rows(payload, task["platform"], task["appId"], country, meta,
                                  skipped)
        if not page_rows:
            break
        rows.extend(page_rows)
    return rows, pages, "", ""


async def read_play(task: dict, country: str, max_pages: int, budget: int,
                    deadline: float | None,
                    skipped: dict | None = None) -> tuple[list[dict], dict, int, str, str]:
    """Reviews of one Play app: rows, app meta, pages read, status, reason."""
    from google_play_scraper import Sort, app as gp_app, reviews as gp_reviews

    def _app():
        return gp_app(task["appId"], lang="en", country=country)

    def _reviews(count: int, token):
        return gp_reviews(task["appId"], lang="en", country=country, sort=Sort.NEWEST,
                          count=count, continuation_token=token)

    try:
        detail = await asyncio.to_thread(_app)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if "NotFound" in name:
            return [], {}, 0, ap.ST_NOT_FOUND, "Google Play does not know this package"
        return [], {}, 0, ap.ST_HTTP, f"Google Play answered {name}"
    meta = {
        "title": detail.get("title"),
        "rating": detail.get("score"),
        "ratingsTotal": detail.get("ratings"),
    }
    rows: list[dict] = []
    pages = 0
    token = None
    while pages < max_pages and len(rows) < budget:
        left = time_left(deadline)
        if left is not None and left < ap.PAGE_MARGIN_S:
            return rows, meta, pages, "deadline", "the run ran out of its time budget"
        count = min(200, max(20, budget - len(rows)))
        try:
            async with asyncio.timeout(max(2.0, (left if left is not None
                                                 else ap.REQUEST_TIMEOUT_S)
                                          - ap.SUMMARY_MARGIN_S)):
                batch, token = await asyncio.to_thread(_reviews, count, token)
        except TimeoutError:
            return rows, meta, pages, "deadline", "the run ran out of its time budget"
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            status = ap.ST_BLOCKED if "Blocking" in name else ap.ST_HTTP
            return rows, meta, pages, status, f"Google Play answered {name}"
        pages += 1
        if not batch:
            break
        rows.extend(ap.play_rows(batch, task["platform"], task["appId"], country, meta,
                                 skipped))
        if token is None:
            break
    return rows, meta, pages, "", ""


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}
        lines = raw.get("apps") or []
        if isinstance(lines, str):
            lines = [item for item in lines.replace(",", "\n").split("\n") if item.strip()]
        lines = [str(item) for item in lines if str(item).strip()]
        if not lines:
            # The first button a buyer presses is Run. An empty input has to say why it
            # cannot work, not come back as a green run with an empty dataset.
            await Actor.fail(status_message=(
                "apps is empty - pass at least one App Store id (389801252), a Play "
                "package (com.instagram.android) or a store link: nothing was read and "
                "nothing was charged"))
            return
        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()
        can_charge, price_note = ap.price_state(charging)
        billable_claim = None if can_charge is None else bool(can_charge)
        price_unknown = can_charge is None

        max_reviews = int_or(raw.get("maxReviewsPerApp"), ap.MAX_REVIEWS_DEFAULT)
        max_reviews = max(1, min(max_reviews, ap.MAX_REVIEWS_HARD))
        max_apps = int_or(raw.get("maxApps"), ap.MAX_APPS_DEFAULT)
        max_apps = max(1, min(max_apps, ap.MAX_APPS_HARD))
        max_pages = int_or(raw.get("maxPagesPerCountry"), ap.MAX_PAGES_DEFAULT)
        max_pages = max(1, min(max_pages, ap.APPLE_MAX_PAGES))
        countries, refuse_all = ap.countries_of(raw.get("countries"))
        if len(countries) > ap.MAX_COUNTRIES_HARD:
            refuse_all = (f"the run reads at most {ap.MAX_COUNTRIES_HARD} storefronts: "
                          "nothing was read and nothing was charged")
            countries = countries[:ap.MAX_COUNTRIES_HARD]
        include_replies = bool(raw.get("includeReplies", True))
        min_rating = int_or(raw.get("minRating"), 0) or None
        max_rating = int_or(raw.get("maxRating"), 0) or None
        if min_rating is not None and not 1 <= min_rating <= 5:
            refuse_all = "minRating must be a star rating from 1 to 5"
        if max_rating is not None and not 1 <= max_rating <= 5:
            refuse_all = "maxRating must be a star rating from 1 to 5"
        cutoff = str(raw.get("publishedAfter") or "").strip() or None
        if cutoff and not (len(cutoff) == 10 and cutoff[4] == "-" and cutoff[7] == "-"):
            refuse_all = (f"publishedAfter \"{cutoff}\" is not a date like 2026-11-01: "
                          "nothing was read and nothing was charged")

        tasks, planned = build_tasks(lines, max_apps, refuse_all)
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
        reviews_limited = 0
        reviews_capped = 0
        reviews_stopped = 0
        reviews_uncertain = 0
        reviews_unparsable = 0
        charged_total = 0
        delivered_uncharged = 0
        charge_uncertain = 0
        pages_fetched = 0
        apps_attempted = 0
        apps_read = 0
        apps_partial = 0
        apps_stopped = 0
        apps_not_found = 0
        apps_blocked = 0
        apps_no_reviews = 0
        apps_filtered_out = 0
        apps_failed = 0
        stop_reason: str | None = "billing-price-unknown" if price_unknown else None
        stopping = bool(price_unknown)
        dataset_note: str | None = None
        deadline = deadline_at()

        def note(status: str) -> None:
            by_status[status] = by_status.get(status, 0) + 1

        free_failures = 0

        async def write_free(row: dict) -> bool:
            nonlocal rows_written, write_errors, dataset_note, free_failures
            nonlocal stopping, stop_reason
            row["billable"] = False
            try:
                await dataset.push_data(row)
            except Exception:  # noqa: BLE001
                write_errors += 1
                free_failures += 1
                dataset_note = "a free status row was refused by the dataset"
                if free_failures >= 3:
                    # Three refusals in a row mean the dataset is gone: going on would
                    # count rows the buyer never receives.
                    stopping = True
                    stop_reason = stop_reason or "dataset-write-error"
                return False
            free_failures = 0
            rows_written += 1
            return True

        async def dataset_items() -> int | None:
            try:
                meta = await dataset.get_metadata()
            except Exception:  # noqa: BLE001
                return None
            return item_count_of(meta)

        async def free_status(line, index, status: str, reason: str) -> None:
            await write_free(ap.status_row(line, index, status, reason))

        buffer: list[dict] = []

        async def write_paid(rows: list[dict]) -> None:
            """Write and charge one batch. Any doubt about money stops the run."""
            nonlocal rows_written, reviews_delivered, charged_total, delivered_uncharged
            nonlocal charge_uncertain, stopping, stop_reason, dataset_note, write_errors
            nonlocal reviews_limited, reviews_capped, reviews_uncertain
            if not rows:
                return
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
                for _ in rows:
                    note(ap.ST_OK)
                return
            if charging.is_event_charge_limit_reached(ap.EVENT_NAME):
                stopping = True
                stop_reason = "spending-cap"
                for row in rows:
                    reviews_limited += 1
                    reviews_capped += 1
                    note(ap.ST_CAP)
                    await free_status(row.get("input"), row.get("inputIndex", 0), ap.ST_CAP,
                                      "the spending cap was reached: not delivered and "
                                      "not charged")
                return
            for row in rows:
                row["billable"] = billable_claim
            try:
                result = await Actor.push_data(rows, charged_event_name=ap.EVENT_NAME)
            except Exception as exc:  # noqa: BLE001
                # One call writes and then charges, so an exception does not say which
                # half failed. A repeated batch would give two rows per review, so the
                # run stops and the doubt is named; the counter is read once, lazily.
                landed = await dataset_items()
                if landed is not None and landed >= rows_written + len(rows):
                    delivered_uncharged += len(rows)
                    reviews_delivered += len(rows)
                    rows_written += len(rows)
                    for _ in rows:
                        note(ap.ST_OK)
                    dataset_note = (f"a paid batch of {len(rows)} is in the dataset "
                                    f"({landed} rows there) and the charge failed: "
                                    "delivered without money")
                else:
                    charge_uncertain += len(rows)
                    reviews_uncertain += len(rows)
                    dataset_note = (f"a paid batch of {len(rows)} could not be confirmed "
                                    f"after {type(exc).__name__}: it may be in the "
                                    "dataset and it may be uncharged")
                    # A review that was read never disappears: each one gets its own
                    # free row naming the doubt.
                    status, reason = ap.undelivered_status("charge-error")
                    for row in rows:
                        note(status)
                        await free_status(row.get("input"), row.get("inputIndex", 0),
                                          status, reason)
                stopping = True
                stop_reason = "charge-error"
                return
            charged = getattr(result, "charged_count", None)
            if charged is None and isinstance(result, dict):
                charged = result.get("charged_count")
            if charged is None:
                # The SDK did not say how many it charged: the summary must not invent
                # money. The rows are in the dataset, so they are named as delivered
                # without money and the run goes on.
                delivered_uncharged += len(rows)
                reviews_delivered += len(rows)
                rows_written += len(rows)
                for _ in rows:
                    note(ap.ST_OK)
                dataset_note = ("the SDK did not report charged_count for this batch: "
                                "the rows are delivered and the summary claims no money")
                return
            charged = int(charged)
            charged_total += charged
            reviews_delivered += charged
            rows_written += charged
            for _ in range(charged):
                note(ap.ST_OK)
            if charged < len(rows):
                surplus = len(rows) - charged
                reviews_limited += surplus
                reviews_capped += surplus
                if charged == 0:
                    stopping = True
                    stop_reason = stop_reason or "spending-cap"
                for row in rows[charged:]:
                    note(ap.ST_CAP)
                    await free_status(row.get("input"), row.get("inputIndex", 0), ap.ST_CAP,
                                      "the spending cap was reached: not delivered and "
                                      "not charged")

        async def deliver(rows: list[dict]) -> None:
            buffer.extend(rows)
            while len(buffer) >= ap.PAID_BATCH:
                batch = buffer[:ap.PAID_BATCH]
                del buffer[:ap.PAID_BATCH]
                await write_paid(batch)

        seen_reviews: set[str] = set()

        async def collect(raw_rows: list[dict], task: dict,
                          counter: dict) -> list[dict]:
            """Filter, deduplicate and cap the reviews of one page; returns payable rows."""
            nonlocal reviews_seen, reviews_unique, reviews_filtered, reviews_duplicate
            nonlocal reviews_limited
            payable: list[dict] = []
            for row in raw_rows:
                reviews_seen += 1
                key = ap.review_key(row["platform"], row["reviewId"])
                if key in seen_reviews:
                    reviews_duplicate += 1
                    note(ap.ST_DUPLICATE)
                    continue
                seen_reviews.add(key)
                reviews_unique += 1
                if not include_replies:
                    row["developerReply"] = None
                    row["developerReplyDate"] = None
                keep, _why = ap.keep_review(row, min_rating, max_rating, cutoff)
                if not keep:
                    reviews_filtered += 1
                    counter["filtered"] += 1
                    note(ap.ST_FILTERED)
                    continue
                row["input"] = task["line"]
                row["inputIndex"] = task["index"]
                if counter["accepted"] >= max_reviews:
                    # The limit protects the buyer's budget: a review past it is named,
                    # not handed over free with its whole text.
                    reviews_limited += 1
                    note(ap.ST_LIMIT)
                    await free_status(row.get("input"), task["index"], ap.ST_LIMIT,
                                      f"beyond maxReviewsPerApp {max_reviews}: read "
                                      "but not delivered, not charged")
                    continue
                counter["accepted"] += 1
                row["status"] = ap.ST_OK
                row["reason"] = ""
                payable.append(row)
            return payable

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(ap.REQUEST_TIMEOUT_S),
            follow_redirects=False,
            # Apple's customer feed answered an empty feed to a non-browser user agent
            # on 2026-11-11 (873 bytes and zero entries for an app with 29.7M ratings,
            # the same URL with a browser agent answered 50 reviews). Not reproduced
            # later that day with either agent; the browser agent stays as a harmless
            # safeguard, and the empty-feed guard is what really protects money.
            headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/131.0 Safari/537.36")},
        )
        try:
            for row in planned["free"]:
                note(row["status"])
                await write_free(row)

            for task in tasks:
                counter = {"accepted": 0, "filtered": 0, "seen": 0, "pages": 0,
                           "error": "", "error_status": "", "ratingsTotal": None,
                           "unparsable": 0}
                left = time_left(deadline)
                if stopping or (left is not None and left < ap.PAGE_MARGIN_S):
                    if not stopping:
                        stopping = True
                        stop_reason = stop_reason or "deadline"
                    status, reason = ap.stopped_status(stop_reason)
                    apps_stopped += 1
                    note(status)
                    await free_status(task["line"], task["index"], status, reason)
                    continue
                apps_attempted += 1
                for country in countries:
                    if stopping:
                        break
                    budget = max_reviews - counter["accepted"]
                    if budget <= 0:
                        break
                    if task["platform"] == ap.APPLE:
                        meta, problem = await apple_meta(client, task["appId"], country)
                        if problem:
                            counter["error"] = problem
                            counter["error_status"] = (
                                ap.ST_NOT_FOUND if "does not know" in problem
                                else ap.ST_BLOCKED if "wall" in problem else ap.ST_HTTP)
                            break
                        counter["ratingsTotal"] = (
                            counter["ratingsTotal"] if counter["ratingsTotal"] is not None
                            else meta.get("ratingsTotal"))
                        rows, pages, status, problem = await read_apple(
                            client, task, country, meta, max_pages, budget, deadline,
                            counter)
                    else:
                        rows, meta, pages, status, problem = await read_play(
                            task, country, max_pages, budget, deadline, counter)
                        counter["ratingsTotal"] = (
                            counter["ratingsTotal"] if counter["ratingsTotal"] is not None
                            else meta.get("ratingsTotal"))
                    pages_fetched += pages
                    counter["pages"] += pages
                    counter["seen"] += len(rows)
                    if status == "deadline":
                        stopping = True
                        stop_reason = stop_reason or "deadline"
                    elif status:
                        counter["error"] = problem
                        counter["error_status"] = status
                    payable = await collect(rows, task, counter)
                    if payable:
                        await deliver(payable)

                reviews_unparsable += counter["unparsable"]
                if counter["error_status"]:
                    if counter["seen"]:
                        apps_partial += 1
                    elif counter["error_status"] == ap.ST_NOT_FOUND:
                        apps_not_found += 1
                    elif counter["error_status"] == ap.ST_BLOCKED:
                        apps_blocked += 1
                    else:
                        apps_failed += 1
                elif counter["seen"] == 0:
                    claimed = counter["ratingsTotal"] or 0
                    if claimed > 0:
                        # The feed came back empty for an app the store says has ratings:
                        # that is a failed read, not an app without reviews. Selling it as
                        # "no reviews" would be the same lie as a made-up number.
                        apps_failed += 1
                        note(ap.ST_ERROR)
                        await free_status(
                            task["line"], task["index"], ap.ST_ERROR,
                            f"the store reports {claimed} rating(s) for this app but its "
                            "review feed came back empty: read failed, nothing charged")
                    else:
                        apps_no_reviews += 1
                        note(ap.ST_NO_REVIEWS)
                        await free_status(task["line"], task["index"], ap.ST_NO_REVIEWS,
                                          "the app is open and the store carried no review "
                                          "for it: nothing to charge")
                elif counter["accepted"] == 0 and counter["filtered"] > 0:
                    apps_filtered_out += 1
                    note(ap.ST_FILTERED)
                    await free_status(task["line"], task["index"], ap.ST_FILTERED,
                                      "reviews were read and your own minRating, "
                                      "maxRating or publishedAfter removed all of them: "
                                      "not charged")
                else:
                    apps_read += 1

            if buffer:
                if stop_reason in ("charge-error", "billing-price-unknown"):
                    # The sale is already in doubt: these rows were read and are named,
                    # not sold a second time.
                    status, reason = ap.undelivered_status(stop_reason)
                    for row in buffer:
                        reviews_limited += 1
                        reviews_stopped += 1
                        note(status)
                        await free_status(row.get("input"), row.get("inputIndex", 0),
                                          status, reason)
                    buffer.clear()
                else:
                    await write_paid(buffer)
                    buffer.clear()

        finally:
            await client.aclose()

        unique_accounted = (reviews_delivered + reviews_filtered + reviews_limited
                            + reviews_uncertain)
        mismatch = reviews_unique != unique_accounted
        summary = {
            "finishedAt": ap.now_iso(),
            "appsRequested": len(lines),
            "appsAttempted": apps_attempted,
            "appsNotAttempted": len(lines) - apps_attempted,
            "appsNotAttemptedNote": ("counts invalid, duplicate and apps past maxApps: "
                                     "each of them has its own free status row"),
            "appsInvalid": invalid,
            "appsDuplicate": duplicates,
            "appsLimited": limited,
            "appsStopped": apps_stopped,
            "appsRead": apps_read,
            "appsPartial": apps_partial,
            "appsNotFound": apps_not_found,
            "appsBlocked": apps_blocked,
            "appsNoReviews": apps_no_reviews,
            "appsFilteredOut": apps_filtered_out,
            "appsFailed": apps_failed,
            "reviewsSeen": reviews_seen,
            "reviewsUnique": reviews_unique,
            "reviewsDelivered": reviews_delivered,
            "reviewsFiltered": reviews_filtered,
            "reviewsDuplicate": reviews_duplicate,
            "reviewsLimited": reviews_limited,
            "reviewsCapped": reviews_capped,
            "reviewsStopped": reviews_stopped,
            "reviewsUncertain": reviews_uncertain,
            "reviewsUnparsable": reviews_unparsable,
            "chargedEvents": charged_total,
            "deliveredUncharged": delivered_uncharged,
            "chargeUncertain": charge_uncertain,
            "byStatus": by_status,
            "pagesFetched": pages_fetched,
            "pricePerReview": ap.PRICE_PER_REVIEW,
            "countries": countries,
            "maxReviewsPerApp": max_reviews,
            "maxApps": max_apps,
            "maxPagesPerCountry": max_pages,
            "minRating": min_rating,
            "maxRating": max_rating,
            "publishedAfter": cutoff,
            "includeReplies": include_replies,
            "pricingEventPriced": None if can_charge is None else bool(can_charge),
            "billingActive": None if can_charge is None else bool(can_charge),
            "pricingNote": price_note,
            "writeErrors": write_errors,
            "datasetNote": dataset_note,
            "stopReason": stop_reason or "",
            "counterMismatch": mismatch,
            "rowsInDataset": rows_written,
        }
        if mismatch:
            summary["counterNote"] = (f"unique reviews {reviews_unique} do not add up to "
                                      f"delivered {reviews_delivered} + filtered "
                                      f"{reviews_filtered} + limited {reviews_limited} + "
                                      f"uncertain {reviews_uncertain}")
        # Dataset has no set_value in the SDK (checked in 4.2.0 and 4.2.1), so the
        # old call vanished inside the except above and the bare key the output
        # schema links to was never written. The bare key belongs to the default
        # key-value store, which is what apiDefaultKeyValueStoreUrl points at.
        try:
            await Actor.set_value("RUN_SUMMARY", summary)
        except Exception:  # noqa: BLE001
            pass
        run = run_id()
        if run:
            try:
                store = await Actor.open_key_value_store()
                await store.set_value(f"RUN_SUMMARY-{run}", summary)
            except Exception:  # noqa: BLE001
                pass
        await Actor.set_status_message(
            f"{len(lines)} line(s), {reviews_delivered} review(s) delivered, "
            f"{charged_total} charged"
            + (f", stopped: {stop_reason}" if stop_reason else ""))
        if stop_reason == "dataset-write-error":
            # The dataset refused the rows themselves: a green run here would be a lie.
            await Actor.fail(status_message=(
                f"the dataset refused {write_errors} row(s): the run stopped instead of "
                "counting rows the buyer never receives"))


if __name__ == "__main__":
    asyncio.run(main())
