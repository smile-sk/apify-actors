"""Apify Actor: posts of public Telegram channels, read through t.me/s.

Input  : `channels` - usernames, @names or links, one per line; optional
         `maxPostsPerChannel`, `sinceDate`, `maxChannels`.
Output : one row per post (text, exact date, views, reactions, forwards, reply
         target, media, links, hashtags) plus one free `rowType: "channel"` row
         per channel that says how the channel reads and why it did not.
Charge : `message-delivered` - only for a post that reached the dataset. A
         channel that does not exist, is a group, hides its public preview or
         answers with an error costs nothing, and the row names the reason.

Why a channel row exists at all: Telegram answers 200 for a channel that is not
there, for a group and for a channel with the preview switched off. An actor that
returns "no results" for all three wastes the buyer's run, and that is the exact
complaint in the one-star reviews of this niche.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version

import httpx
from apify import Actor

from src import tg

CHARGE_EVENT = "message-delivered"

STATUS_DELIVERED = "DELIVERED"
STATUS_CAP = tg.ST_CAP
STATUS_CHARGE_UNVERIFIED = tg.ST_CHARGE_UNVERIFIED
STATUS_TIME = tg.ST_TIME
STATUS_ERROR = tg.ST_ERROR

# channel statuses that mean the channel could not be read at all, as opposed to a
# channel that was read and simply had nothing inside the date range
CHANNEL_UNREADABLE = frozenset({tg.ST_NOT_FOUND, tg.ST_NOT_A_CHANNEL, tg.ST_NO_PREVIEW,
                                tg.ST_SOURCE, tg.ST_TIME, tg.ST_ERROR})

MAX_CONSECUTIVE_CHARGE_ERRORS = 3
MAX_CONSECUTIVE_WRITE_ERRORS = 3
MIN_PAGE_INTERVAL_SEC = 0.7      # measured: 0.5 s is fine, 0.7 is polite to Telegram
MAX_PAGES_PER_CHANNEL = 500      # a hard stop so one channel can never eat the run
DEADLINE_RESERVE_SEC = 20.0      # writing the summary needs time too
PAGE_TIMEOUT_SEC = 25.0
RETRIES = 2


def _dep_versions() -> dict:
    out = {}
    for name in ("apify", "httpx"):
        try:
            out[name] = _pkg_version(name)
        except PackageNotFoundError:
            out[name] = None
    return out


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


def _parse_since(raw) -> datetime | None:
    """'2026-11-01' or '2026-11-01T10:00:00' into an aware UTC moment."""
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt, size in (("%Y-%m-%d", 10), ("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d %H:%M:%S", 19),
                      ("%Y-%m-%dT%H:%M", 16)):
        try:
            return datetime.strptime(text[:size], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _post_datetime(post: tg.Post) -> datetime | None:
    if not post.date:
        return None
    try:
        moment = datetime.fromisoformat(post.date.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _channel_row(username: str, status: str | None, reason: str | None, header: dict | None,
                 seen: int, delivered: int, pages: int, truncated: bool = False) -> dict:
    """messagesSeen is how many posts the page carried inside the range before any
    limit, messagesDelivered is how many of them reached the dataset."""
    header = header or {}
    return {
        "rowType": "channel",
        "channel": username,
        "channelTitle": header.get("title"),
        "channelDescription": header.get("description"),
        "channelPhotoUrl": header.get("photoUrl"),
        "subscribers": header.get("subscribers"),
        "subscribersApprox": header.get("subscribersApprox", True),
        "channelPhotos": header.get("photos"),
        "channelVideos": header.get("videos"),
        "channelLinks": header.get("links"),
        "messagesSeen": seen,
        "messagesDelivered": delivered,
        "pagesFetched": pages,
        "truncated": truncated,
        "status": status,
        "reason": reason,
        "billable": False,
    }


def _status_post_row(post: tg.Post, status: str, reason: str) -> dict:
    """A post that could not be delivered: the buyer sees which one and why."""
    return {
        "rowType": "message",
        "channel": post.channel,
        "messageId": post.id,
        "url": post.url,
        "date": post.date,
        "text": None,
        "textLength": None,
        "status": status,
        "reason": reason,
        "billable": False,
    }


async def main() -> None:
    async with Actor:
        raw_input = await Actor.get_input() or {}
        raw_channels = raw_input.get("channels") or []
        try:
            max_posts = int(raw_input.get("maxPostsPerChannel") if raw_input.get("maxPostsPerChannel") is not None else 100)
        except (TypeError, ValueError):
            max_posts = 100
        try:
            max_channels = int(raw_input.get("maxChannels") if raw_input.get("maxChannels") is not None else 20)
        except (TypeError, ValueError):
            max_channels = 20
        if max_posts < 0:
            max_posts = 0
        if max_channels < 0:
            max_channels = 0
        since_text = str(raw_input.get("sinceDate") or "").strip()
        since = _parse_since(since_text)
        if since_text and since is None:
            # 01.11.2026 is what a person types, and dropping the filter silently means
            # he pays for posts outside the window he asked for
            await Actor.fail(status_message=(
                f"The date in 'sinceDate' could not be read: {since_text[:40]!r}. "
                "Use YYYY-MM-DD, for example 2026-11-01. Nothing was requested and "
                "nothing was charged."))
            return

        if isinstance(raw_channels, str):
            raw_channels = raw_channels.splitlines()
        lines = [str(x) for x in (raw_channels or []) if str(x).strip()]

        summary = {
            "channelsRequested": len(lines),
            "channelsUnique": 0,
            "channelsRead": 0,
            "channelsEmpty": 0,
            "channelsUnreadable": 0,
            "channelsInvalid": 0,
            "channelsCutByLimit": 0,
            "duplicateChannels": 0,
            "pagesFetched": 0,
            "messagesSeen": 0,
            "messagesDelivered": 0,
            "messagesCutByLimit": 0,
            "messagesBeforeSinceDate": 0,
            "messagesCap": 0,
            "messagesChargeUnverified": 0,
            "messagesWriteError": 0,
            "rowsDelivered": 0,
            "chargedEvents": 0,
            "deliveredUncharged": 0,
            "billingActive": True,
            "billingReason": None,
            "stopReason": None,
            "deadlineCapped": False,
            "sinceDate": raw_input.get("sinceDate") or None,
            "sinceDateParsed": since.isoformat() if since else None,
            "maxPostsPerChannel": max_posts,
            "maxChannels": max_channels,
            "byStatusChannels": {},
            "byStatusMessages": {},
            "chargeErrors": [],
            "datasetWriteErrors": 0,
            "datasetItemCount": None,
            "datasetItemCountNote": None,
            "versions": _dep_versions(),
        }
        charge_errors: list = summary["chargeErrors"]
        consecutive_charge_errors = 0
        consecutive_write_errors = 0
        billing_active = True
        stop_reason: str | None = None
        run_stopped = False
        started = time.monotonic()
        budget = _platform_budget_secs()

        def deadline_left() -> float | None:
            if budget is None:
                return None
            return budget - (time.monotonic() - started) - DEADLINE_RESERVE_SEC

        try:
            pricing = Actor.get_charging_manager().get_pricing_info()
        except Exception:  # noqa: BLE001 - a local run carries no pricing
            pricing = None
        if pricing is not None:
            prices = getattr(pricing, "per_event_prices", None) or {}
            if not getattr(pricing, "is_pay_per_event", False):
                billing_active = False
                summary["billingActive"] = False
                summary["billingReason"] = "this run is not priced per event"
            elif CHARGE_EVENT not in prices:
                billing_active = False
                summary["billingActive"] = False
                summary["billingReason"] = f"pricing has no {CHARGE_EVENT!r} event"
                Actor.log.error("pricing has no %r event: this run delivers rows without charging",
                                CHARGE_EVENT)

        def count_channel(status: str) -> None:
            bucket = summary["byStatusChannels"]
            bucket[status] = bucket.get(status, 0) + 1

        def count_message(status: str) -> None:
            bucket = summary["byStatusMessages"]
            bucket[status] = bucket.get(status, 0) + 1

        async def item_count():
            try:
                dataset = await Actor.open_dataset()
                meta = await dataset.get_metadata()
            except Exception:  # noqa: BLE001 - metadata is a check, not the result
                return None
            return getattr(meta, "item_count", None)

        async def push(row: dict) -> bool:
            nonlocal consecutive_write_errors, run_stopped, stop_reason
            try:
                await Actor.push_data(row)
            except Exception as exc:  # noqa: BLE001 - the dataset refused the row
                consecutive_write_errors += 1
                summary["datasetWriteErrors"] += 1
                Actor.log.error("dataset refused a row: %s", exc)
                if consecutive_write_errors >= MAX_CONSECUTIVE_WRITE_ERRORS:
                    run_stopped, stop_reason = True, "dataset-write-error"
                return False
            consecutive_write_errors = 0
            summary["rowsDelivered"] += 1
            return True

        async def deliver_post(post: tg.Post) -> None:
            """One post, one paid row. The charge rides on the write, never apart."""
            nonlocal billing_active, consecutive_charge_errors, consecutive_write_errors
            nonlocal run_stopped, stop_reason
            row = post.as_row()
            if not billing_active:
                row["billingInactive"] = True
                if await push(row):
                    summary["messagesDelivered"] += 1
                    summary["deliveredUncharged"] += 1
                    count_message(STATUS_DELIVERED)
                return
            try:
                result = await Actor.push_data(row, charged_event_name=CHARGE_EVENT)
            except Exception as exc:  # noqa: BLE001 - money may or may not have been taken
                consecutive_charge_errors += 1
                charge_errors.append({"channel": post.channel, "messageId": post.id,
                                      "error": f"{type(exc).__name__}: {exc}"[:300]})
                Actor.log.error("the write or the charge failed for %s/%s: %s",
                                post.channel, post.id, exc)
                # the dataset count is read only here, once per failure: asking before
                # every row would cost one API call per post and prove nothing more
                after = await item_count()
                if after is not None and after > summary["rowsDelivered"]:
                    # the count stands above every row we know about: this post landed free
                    summary["rowsDelivered"] += 1
                    summary["messagesDelivered"] += 1
                    summary["deliveredUncharged"] += 1
                    count_message(STATUS_DELIVERED)
                elif after is not None and after == summary["rowsDelivered"]:
                    # the count keeps step with our own writes and still has no room for
                    # this row: the dataset refused it, and it is named as a write error
                    consecutive_charge_errors = 0
                    consecutive_write_errors += 1
                    summary["datasetWriteErrors"] += 1
                    summary["messagesWriteError"] += 1
                    count_message(STATUS_ERROR)
                    await push(_status_post_row(
                        post, STATUS_ERROR,
                        "the dataset refused this row: it is not stored and not charged"))
                    if consecutive_write_errors >= MAX_CONSECUTIVE_WRITE_ERRORS:
                        run_stopped, stop_reason = True, "dataset-write-error"
                    return
                else:
                    # metadata is missing or sits behind our own writes, so the row may or
                    # may not be in the dataset: claiming either way would be a guess, and
                    # a second row for the same post would be a duplicate for the buyer
                    summary["messagesChargeUnverified"] += 1
                    count_message(STATUS_CHARGE_UNVERIFIED)
                if consecutive_charge_errors >= MAX_CONSECUTIVE_CHARGE_ERRORS:
                    run_stopped, stop_reason = True, "charge-error"
                return
            consecutive_charge_errors = 0
            billed = int(getattr(result, "charged_count", 0) or 0)
            limit_reached = bool(getattr(result, "event_charge_limit_reached", False))
            if billed:
                summary["rowsDelivered"] += 1
                summary["chargedEvents"] += billed
                summary["messagesDelivered"] += 1
                count_message(STATUS_DELIVERED)
                if limit_reached:
                    run_stopped, stop_reason = True, "spending-cap"
            elif limit_reached:
                summary["messagesCap"] += 1
                count_message(STATUS_CAP)
                await push(_status_post_row(
                    post, STATUS_CAP,
                    "the spending cap was reached, so this post is not delivered and not charged"))
                run_stopped, stop_reason = True, "spending-cap"
            else:
                billing_active = False
                summary["billingActive"] = False
                summary["billingReason"] = "the platform reported no charge for a delivered row"
                summary["rowsDelivered"] += 1
                summary["messagesDelivered"] += 1
                summary["deliveredUncharged"] += 1
                count_message(STATUS_DELIVERED)

        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(PAGE_TIMEOUT_SEC),
            headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        last_request_at = 0.0

        async def fetch(url: str) -> str:
            nonlocal last_request_at, run_stopped, stop_reason
            attempt = 0
            while True:
                wait = MIN_PAGE_INTERVAL_SEC - (time.monotonic() - last_request_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                last_request_at = time.monotonic()
                try:
                    response = await client.get(url)
                except httpx.HTTPError as exc:
                    if attempt >= RETRIES:
                        raise tg.SourceError(f"the request failed: {type(exc).__name__}") from exc
                    attempt += 1
                    await asyncio.sleep(1.5 * attempt)
                    continue
                if response.status_code == 200:
                    return response.text
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt >= RETRIES:
                        if response.status_code == 429:
                            run_stopped, stop_reason = True, "rate-limited"
                        raise tg.SourceError(f"Telegram answered HTTP {response.status_code}",
                                             response.status_code)
                    attempt += 1
                    await asyncio.sleep(2.0 * attempt)
                    continue
                raise tg.SourceError(f"Telegram answered HTTP {response.status_code}",
                                     response.status_code)

        seen_ids: set[tuple[str, int]] = set()
        seen_channels: set[str] = set()
        accepted: list[str] = []

        try:
            for line in lines:
                username, why = tg.normalize_channel(line)
                if username is None:
                    status = tg.ST_PRIVATE if tg.is_invite_link(line) else tg.ST_INVALID
                    await push(_channel_row(line.strip()[:64], status, why, None, 0, 0, 0))
                    count_channel(status)
                    summary["channelsInvalid"] += 1
                    continue
                key = username.lower()
                if key in seen_channels:
                    await push(_channel_row(username, tg.ST_DUPLICATE,
                                            "the same channel came earlier in this run, it is read once",
                                            None, 0, 0, 0))
                    count_channel(tg.ST_DUPLICATE)
                    summary["duplicateChannels"] += 1
                    continue
                seen_channels.add(key)
                if max_channels and len(accepted) >= max_channels:
                    await push(_channel_row(username, tg.ST_LIMIT,
                                            f"the list was cut at maxChannels={max_channels}",
                                            None, 0, 0, 0))
                    count_channel(tg.ST_LIMIT)
                    summary["channelsCutByLimit"] += 1
                    continue
                accepted.append(username)
            summary["channelsUnique"] = len(seen_channels)

            for username in accepted:
                if run_stopped:
                    await push(_channel_row(username, STATUS_ERROR,
                                            f"the run stopped earlier: {stop_reason}", None, 0, 0, 0))
                    count_channel(STATUS_ERROR)
                    summary["channelsUnreadable"] += 1
                    continue
                left = deadline_left()
                if left is not None and left <= 0:
                    summary["deadlineCapped"] = True
                    run_stopped, stop_reason = True, stop_reason or "deadline"
                    await push(_channel_row(username, STATUS_TIME,
                                            "the platform deadline left no time for this channel",
                                            None, 0, 0, 0))
                    count_channel(STATUS_TIME)
                    summary["channelsUnreadable"] += 1
                    continue

                posts: list[tg.Post] = []
                header: dict | None = None
                pages = 0
                status: str | None = None
                reason: str | None = None
                empty_first_page = False
                in_range = 0
                cursor_url = f"https://t.me/s/{username}"
                try:
                    while True:
                        page = await fetch(cursor_url)
                        pages += 1
                        summary["pagesFetched"] += 1
                        if pages == 1:
                            header = tg.parse_header(page)
                        page_posts = tg.parse_page(page)
                        if not page_posts:
                            if pages == 1:
                                empty_first_page = True
                            break
                        summary["messagesSeen"] += len(page_posts)
                        for post in sorted(page_posts, key=lambda p: p.id, reverse=True):
                            if (post.channel.lower(), post.id) in seen_ids:
                                continue
                            moment = _post_datetime(post)
                            if since is not None and moment is not None and moment < since:
                                summary["messagesBeforeSinceDate"] += 1
                                continue
                            seen_ids.add((post.channel.lower(), post.id))
                            in_range += 1
                            posts.append(post)
                        page_times = [t for t in (_post_datetime(p) for p in page_posts) if t]
                        if since is not None and page_times and min(page_times) < since:
                            break
                        if max_posts and in_range >= max_posts:
                            break
                        if pages >= MAX_PAGES_PER_CHANNEL:
                            break
                        oldest_id = min(p.id for p in page_posts)
                        cursor_url = f"https://t.me/s/{username}?before={oldest_id}"
                        left = deadline_left()
                        if left is not None and left <= 0:
                            summary["deadlineCapped"] = True
                            status = STATUS_TIME
                            reason = "the platform deadline stopped the walk through the history"
                            break
                except tg.SourceError as exc:
                    status = tg.ST_SOURCE
                    reason = str(exc)
                except Exception as exc:  # noqa: BLE001 - one channel must not kill the run
                    status = tg.ST_SOURCE
                    reason = f"unexpected error while reading: {type(exc).__name__}: {exc}"

                if empty_first_page and status is None:
                    try:
                        landing = await fetch(f"https://t.me/{username}")
                        status, info = tg.classify_silent_page(landing)
                    except tg.SourceError as exc:
                        status, info = tg.ST_SOURCE, {"reason": str(exc)}
                    header = header or {}
                    if status == tg.ST_NOT_A_CHANNEL:
                        reason = ("this is a group or chat, not a channel: Telegram's public web "
                                  "preview covers channels only")
                        if info.get("title") and not header.get("title"):
                            header["title"] = info.get("title")
                        if info.get("members"):
                            header["subscribers"] = info.get("members")
                    elif status == tg.ST_NO_PREVIEW:
                        reason = ("the channel exists but shows no public posts: the public preview "
                                  "is switched off in its settings, or it has no posts yet")
                        if info.get("title") and not header.get("title"):
                            header["title"] = info.get("title")
                        if info.get("subscribers"):
                            header["subscribers"] = info.get("subscribers")
                    elif status == tg.ST_NOT_FOUND:
                        reason = "no such public channel: Telegram does not know this username"
                    else:
                        reason = (info or {}).get("reason") if isinstance(info, dict) else None

                # the page is chronological (oldest on top) and the walk goes back in
                # time, so the newest posts carry the highest ids: order by id before the
                # limit cuts, or the limit would keep the oldest posts it read
                posts.sort(key=lambda p: p.id, reverse=True)
                matched = len(posts)
                if max_posts and len(posts) > max_posts:
                    summary["messagesCutByLimit"] += len(posts) - max_posts
                    posts = posts[:max_posts]
                truncated = len(posts) < matched

                if posts:
                    summary["channelsRead"] += 1
                    if status is None:
                        status = "READ"
                else:
                    if status is None:
                        status = tg.ST_EMPTY
                        if since is not None:
                            reason = ("the channel has no posts inside the date range "
                                      f"since {since.date().isoformat()}")
                        else:
                            reason = "the channel answered with no posts"
                    if status in CHANNEL_UNREADABLE:
                        summary["channelsUnreadable"] += 1
                    else:
                        summary["channelsEmpty"] += 1

                await push(_channel_row(username, status, reason, header, matched, len(posts),
                                        pages, truncated))
                count_channel(status)

                stopped_rows = 0
                for post in posts:
                    if run_stopped and stop_reason in ("spending-cap", "charge-error",
                                                       "dataset-write-error", "rate-limited"):
                        summary["messagesCap"] += 1
                        count_message(STATUS_CAP)
                        if stopped_rows < 3:
                            stopped_rows += 1
                            await push(_status_post_row(
                                post, STATUS_CAP,
                                f"the run stopped earlier: {stop_reason}; this post is not delivered "
                                "and not charged"))
                        continue
                    await deliver_post(post)

            if not lines:
                stop_reason = stop_reason or "empty-input"
        finally:
            await client.aclose()
            summary["stopReason"] = stop_reason
            summary["counterSum"] = (summary["messagesDelivered"] + summary["messagesCap"]
                                     + summary["messagesWriteError"]
                                     + summary["messagesChargeUnverified"])
            channel_sum = (summary["channelsRead"] + summary["channelsEmpty"]
                           + summary["channelsUnreadable"] + summary["channelsCutByLimit"])
            if channel_sum != summary["channelsUnique"]:
                summary["channelsMismatch"] = (
                    f"unique {summary['channelsUnique']} != read {summary['channelsRead']}"
                    f" + empty {summary['channelsEmpty']}"
                    f" + unreadable {summary['channelsUnreadable']}"
                    f" + cut {summary['channelsCutByLimit']}")
            if summary["messagesDelivered"] > summary["messagesSeen"]:
                summary["counterMismatch"] = (
                    f"delivered {summary['messagesDelivered']} exceeds seen {summary['messagesSeen']}")
            elif summary["messagesDelivered"] > summary["chargedEvents"] + summary["deliveredUncharged"]:
                summary["counterMismatch"] = "delivered exceed charged plus uncharged"
            summary["durationSecs"] = round(time.monotonic() - started, 1)
            run_env = Actor.get_env() or {}
            run_id = run_env.get("run_id") or os.environ.get("ACTOR_RUN_ID") or "run"
            summary["runId"] = run_id
            summary["summaryKey"] = f"RUN_SUMMARY-{run_id}"

            api_count = await item_count()
            summary["datasetItemCount"] = summary["rowsDelivered"]
            summary["datasetItemCountApi"] = api_count
            if api_count is None:
                summary["datasetItemCountNote"] = "the platform did not report a dataset count for this run"
            elif api_count != summary["rowsDelivered"]:
                summary["datasetItemCountNote"] = (f"metadata lag: expected {summary['rowsDelivered']}, "
                                                   f"reports {api_count}")
            try:
                store = await Actor.open_key_value_store()
                await store.set_value(summary["summaryKey"], summary)
                # the default store of a run belongs to this run alone, so a plain key
                # here cannot be overwritten by another run, and the output schema
                # template points exactly at it
                await store.set_value("RUN_SUMMARY", summary)
            except Exception as exc:  # noqa: BLE001 - the summary must not fail the run
                Actor.log.error("could not write the summary: %s", exc)

            headline = (f"{summary['channelsRead']} channel(s) read, "
                        f"{summary['messagesDelivered']} post(s) delivered, "
                        f"{summary['chargedEvents']} charged")
            if summary["stopReason"]:
                headline += f", stopped: {summary['stopReason']}"
            if not summary["billingActive"]:
                headline += ", billing inactive"
            try:
                await Actor.set_status_message(headline)
            except Exception:  # noqa: BLE001 - a local run has no status message
                pass
            Actor.log.info("%s | %s | %s", headline,
                            summary["byStatusChannels"], summary["byStatusMessages"])

        if not lines:
            await Actor.fail(status_message=(
                "No channels in the input. Put at least one channel username or link into "
                "'channels'; nothing was charged."))
        elif summary["channelsRead"] == 0:
            reasons = ", ".join(sorted(summary["byStatusChannels"])) or "no status"
            await Actor.fail(status_message=(
                "No channel could be read: every line came back with its own status "
                f"({reasons}). Nothing was charged, and each line has a row that says why."))


if __name__ == "__main__":
    asyncio.run(main())
