"""YouTube comments: one row per comment, money only for a row that landed.

Money rule of this park: a paid row is written and charged by one call
(Actor.push_data(..., charged_event_name=...)) and the SDK answers how many items it
really charged. Everything else is a free row that says in plain words why the buyer got
nothing: a line that is not a video, a duplicate, a video whose comments are closed, a
video the platform no longer serves, a budget that ran out, a deadline, or a price that
could not be read.

A comment that was read but not delivered is never lost in silence: when the run stops
for time, for the spending cap or after a charge error, every read comment comes back as
a free row naming the reason, so the dataset always accounts for what YouTube gave us.

No browser, no key, no proxy: the public innertube endpoint answers the same from a home
connection and from our Frankfurt datacenter address (measured 2026-11-12), so the image
holds python and nothing else.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

from apify import Actor

from . import ytc


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


def build_tasks(lines: list[str], max_videos: int) -> tuple[list[dict], dict]:
    """Split the input into work, and name every line that cannot be work."""
    tasks: list[dict] = []
    seen: set[str] = set()
    invalid = 0
    duplicate = 0
    limited = 0
    for index, value in enumerate(lines):
        video = ytc.video_id(value)
        if not video:
            invalid += 1
            tasks.append({"index": index, "input": value, "video": None,
                          "status": ytc.ST_INVALID})
            continue
        if video in seen:
            duplicate += 1
            tasks.append({"index": index, "input": value, "video": video,
                          "status": ytc.ST_DUPLICATE})
            continue
        if len([t for t in tasks if t.get("video") and not t.get("status")]) >= max_videos:
            limited += 1
            tasks.append({"index": index, "input": value, "video": video,
                          "status": ytc.ST_LIMIT, "overMaxVideos": True})
            continue
        seen.add(video)
        tasks.append({"index": index, "input": value, "video": video, "status": None})
    counts = {"invalid": invalid, "duplicate": duplicate, "limited": limited,
              "planned": len([t for t in tasks if not t.get("status")])}
    return tasks, counts


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}
        given = raw.get("videos") or []
        if isinstance(given, str):
            given = [item for item in given.replace(",", "\n").split("\n") if item.strip()]
        lines = [str(item).strip() for item in given if str(item).strip()]
        if not lines:
            # The first button a buyer presses is Run. An empty input has to say why it
            # cannot work, not come back as a green run with an empty dataset.
            await Actor.fail(status_message=(
                "videos is empty - pass at least one watch url "
                "(https://www.youtube.com/watch?v=jNQXAC9IVRw) or a bare 11 character "
                "video id: nothing was read and nothing was charged"))
            return

        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()
        can_charge, price_note = ytc.price_state(charging)
        billable_claim = None if can_charge is None else bool(can_charge)
        price_unknown = can_charge is None

        max_comments = max(1, min(int_or(raw.get("maxCommentsPerVideo"),
                                         ytc.MAX_COMMENTS_DEFAULT),
                                  ytc.MAX_COMMENTS_HARD))
        max_videos = max(1, min(int_or(raw.get("maxVideos"), ytc.MAX_VIDEOS_DEFAULT),
                                ytc.MAX_VIDEOS_HARD))
        max_per_thread = int_or(raw.get("maxCommentsPerThread"),
                                ytc.MAX_THREAD_DEFAULT)
        max_per_thread = max(0, min(max_per_thread, ytc.MAX_THREAD_HARD))
        include_replies = bool(raw.get("includeReplies", True)) and max_per_thread > 0
        sort = "newest" if str(raw.get("sort") or "top").strip().lower() == "newest" else "top"

        tasks, planned = build_tasks(lines, max_videos)

        by_status: dict[str, int] = {}
        rows_written = 0
        write_errors = 0
        comments_seen = 0
        comments_delivered = 0
        comments_skipped_empty = 0
        comments_duplicate = 0
        comments_undelivered = 0
        charged_total = 0
        delivered_uncharged = 0
        charge_uncertain = 0
        pages_read = 0
        replies_delivered = 0
        videos_attempted = 0
        videos_read = 0
        videos_no_comments = 0
        videos_limited = 0
        videos_cut_by_budget = 0
        videos_stopped = 0
        videos_failed = 0
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
            except Exception:  # noqa: BLE001 - a refused row is counted, not raised
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
            except Exception:  # noqa: BLE001 - a counter we cannot read is not a zero
                return None
            for name in ("item_count", "itemCount"):
                value = getattr(meta, name, None)
                if value is not None:
                    return int(value)
            return None

        async def free_status(value: str, index: int, status: str, reason: str) -> None:
            await write_free(ytc.status_row(value, index, status, reason))

        buffer: list[dict] = []

        async def write_paid(rows: list[dict]) -> None:
            """Write and charge one batch. Any doubt about money stops the run."""
            nonlocal rows_written, comments_delivered, charged_total, delivered_uncharged
            nonlocal charge_uncertain, stopping, stop_reason, dataset_note, write_errors
            nonlocal comments_undelivered
            if not rows:
                return
            if stopping:
                # The run already decided to stop: whatever was read but not yet sold
                # comes back as its own free row instead of a late charge.
                for row in rows:
                    comments_undelivered += 1
                    status, reason = ytc.undelivered_status(stop_reason or "deadline")
                    note(status)
                    await free_status(row.get("input", ""), row.get("inputIndex", 0),
                                      status, reason)
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
                comments_delivered += len(rows)
                for _ in rows:
                    note(ytc.ST_OK)
                return
            if charging.is_event_charge_limit_reached(ytc.EVENT_NAME):
                stopping = True
                stop_reason = "spending-cap"
                for row in rows:
                    comments_undelivered += 1
                    status, reason = ytc.undelivered_status("spending-cap")
                    note(status)
                    await free_status(row.get("input", ""), row.get("inputIndex", 0),
                                      status, reason)
                return
            for row in rows:
                row["billable"] = billable_claim
            try:
                result = await Actor.push_data(rows, charged_event_name=ytc.EVENT_NAME)
            except Exception as exc:  # noqa: BLE001
                # One call writes and then charges, so an exception does not say which
                # half failed. A repeated batch would give two rows per comment, so the
                # run stops and the doubt is named; the counter is read once, lazily.
                landed = await dataset_items()
                if landed is not None and landed >= rows_written + len(rows):
                    delivered_uncharged += len(rows)
                    comments_delivered += len(rows)
                    rows_written += len(rows)
                    for _ in rows:
                        note(ytc.ST_OK)
                    dataset_note = (f"a paid batch of {len(rows)} is in the dataset "
                                    f"({landed} rows there) and the charge failed: "
                                    "delivered without money")
                else:
                    charge_uncertain += len(rows)
                    dataset_note = (f"a paid batch of {len(rows)} could not be confirmed "
                                    f"after {type(exc).__name__}: it may be in the "
                                    "dataset and it may be uncharged")
                    status, reason = ytc.undelivered_status("charge-error")
                    for row in rows:
                        comments_undelivered += 1
                        note(status)
                        await free_status(row.get("input", ""), row.get("inputIndex", 0),
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
                comments_delivered += len(rows)
                rows_written += len(rows)
                for _ in rows:
                    note(ytc.ST_OK)
                dataset_note = (dataset_note or "") + (
                    "the platform did not report how many rows it charged: "
                    f"{len(rows)} rows are delivered without confirmed money")
                return
            # The SDK stops at the spending cap by itself and writes only what it
            # charged, so the counters follow the rows that really landed. Counting the
            # whole batch made byStatus and rowsWritten lie by the tail: measured on
            # 2026-11-12, 2 charged of 6 came out as OK 6 and rowsWritten 10.
            charged_now = max(0, int(charged))
            charged_total += charged_now
            rows_written += charged_now
            for _ in range(charged_now):
                note(ytc.ST_OK)
            comments_delivered += charged_now
            if charged_now < len(rows):
                # The tail is a free row with its own reason, never a copy.
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                for row in rows[charged_now:]:
                    comments_undelivered += 1
                    status, reason = ytc.undelivered_status("spending-cap")
                    note(status)
                    await free_status(row.get("input", ""), row.get("inputIndex", 0),
                                      status, reason)

        for task in tasks:
            if stopping:
                if task.get("status") is None:
                    videos_stopped += 1
                    status, reason = ytc.undelivered_status(stop_reason or "deadline")
                    note(status)
                    await free_status(task["input"], task["index"], status, reason)
                elif task.get("status") == ytc.ST_INVALID:
                    note(ytc.ST_INVALID)
                    await free_status(task["input"], task["index"], ytc.ST_INVALID,
                                      "this line is not a YouTube video: put a watch "
                                      "url or an 11 character video id")
                elif task.get("status") == ytc.ST_DUPLICATE:
                    note(ytc.ST_DUPLICATE)
                    await free_status(task["input"], task["index"], ytc.ST_DUPLICATE,
                                      "the same video appears earlier in the list")
                else:
                    note(ytc.ST_LIMIT)
                    await free_status(task["input"], task["index"], ytc.ST_LIMIT,
                                      f"the run reads at most {max_videos} videos")
                continue
            if task.get("status") == ytc.ST_INVALID:
                note(ytc.ST_INVALID)
                await free_status(task["input"], task["index"], ytc.ST_INVALID,
                                  "this line is not a YouTube video: put a watch url "
                                  "or an 11 character video id")
                continue
            if task.get("status") == ytc.ST_DUPLICATE:
                note(ytc.ST_DUPLICATE)
                await free_status(task["input"], task["index"], ytc.ST_DUPLICATE,
                                  "the same video appears earlier in the list")
                continue
            if task.get("status") == ytc.ST_LIMIT:
                videos_limited += 1
                note(ytc.ST_LIMIT)
                await free_status(task["input"], task["index"], ytc.ST_LIMIT,
                                  f"the run reads at most {max_videos} videos: this one "
                                  "was not read and not charged")
                continue

            videos_attempted += 1
            left = time_left(deadline)
            client = ytc.Innertube(timeout_s=25.0)
            result = await asyncio.to_thread(
                ytc.read_video, client, task["video"], max_comments, sort,
                include_replies, max_per_thread, set(), left)
            pages_read += result.get("pages", 0)
            comments_seen += result.get("seen", 0)
            comments_skipped_empty += result.get("skippedEmpty", 0)
            comments_duplicate += result.get("duplicate", 0)
            meta = result.get("meta") or {}
            comments = result.get("rows") or []

            if result["status"] != ytc.ST_OK:
                if result["status"] == ytc.ST_NO_COMMENTS:
                    videos_no_comments += 1
                else:
                    videos_failed += 1
                note(result["status"])
                await free_status(task["input"], task["index"], result["status"],
                                  result.get("reason") or "the video could not be read")
                if result.get("reason") == "deadline":
                    stopping = True
                    stop_reason = stop_reason or "deadline"
                continue

            videos_read += 1
            for comment in comments:
                comment["input"] = task["input"]
                comment["inputIndex"] = task["index"]
                comment["videoTitle"] = meta.get("videoTitle")
                comment["videoChannel"] = meta.get("channel")
                comment["videoCommentsCountText"] = meta.get("commentsCountText")
                comment["status"] = ytc.ST_OK
                if comment.get("isReply"):
                    replies_delivered += 1
            if result.get("hitLimit"):
                # A video cut by its own maxCommentsPerVideo is not a limit on videos:
                # one counter for two events broke the promise videosRequested =
                # attempted + stopped + invalid + duplicate + limited.
                videos_cut_by_budget += 1
            buffer.extend(comments)
            while len(buffer) >= 25:
                batch, buffer = buffer[:25], buffer[25:]
                await write_paid(batch)
                if stopping:
                    break

        if buffer:
            await write_paid(buffer)
            buffer = []

        if comments_undelivered == 0 and stop_reason is None:
            stop_reason_text = ""
        else:
            stop_reason_text = stop_reason or ""

        summary = {
            "runId": run_id(),
            "videosRequested": len(lines),
            "videosPlanned": planned["planned"],
            "videosAttempted": videos_attempted,
            "videosRead": videos_read,
            "videosNoComments": videos_no_comments,
            "videosInvalid": planned["invalid"],
            "videosDuplicate": planned["duplicate"],
            "videosLimited": videos_limited,
            "videosCutByBudget": videos_cut_by_budget,
            "videosStopped": videos_stopped,
            "videosFailed": videos_failed,
            "commentsSeen": comments_seen,
            "commentsDelivered": comments_delivered,
            "commentsSkippedNoText": comments_skipped_empty,
            "commentsDuplicate": comments_duplicate,
            "commentsUndelivered": comments_undelivered,
            "repliesDelivered": replies_delivered,
            "pagesRead": pages_read,
            "chargedEvents": charged_total,
            "deliveredUncharged": delivered_uncharged,
            "chargeUncertain": charge_uncertain,
            "writeErrors": write_errors,
            "rowsWritten": rows_written,
            "byStatus": by_status,
            "priceEvent": ytc.EVENT_NAME,
            "priceNote": price_note,
            "billingActive": None if can_charge is None else bool(can_charge),
            "stopReason": stop_reason_text,
            "datasetNote": dataset_note,
            "finishedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        counted = (comments_delivered + comments_skipped_empty + comments_duplicate
                   + comments_undelivered)
        summary["counterMismatch"] = counted != comments_seen
        summary["countersNote"] = ("" if not summary["counterMismatch"] else
                                   f"seen {comments_seen} != delivered {comments_delivered} "
                                   f"+ noText {comments_skipped_empty} "
                                   f"+ duplicate {comments_duplicate} "
                                   f"+ undelivered {comments_undelivered}")

        head = (f"{len(lines)} line(s), {videos_read} video(s) read, "
                f"{comments_delivered} comment(s) delivered, {charged_total} charged")
        if stop_reason_text:
            head += f", stopped: {stop_reason_text}"
        await Actor.set_status_message(head)
        try:
            await Actor.set_value("RUN_SUMMARY", summary)
        except Exception as exc:  # noqa: BLE001 - a silent failure is the defect here
            summary["summaryWriteErrors"] = [f"{type(exc).__name__}: {str(exc)[:120]}"]
            try:
                await Actor.set_value("RUN_SUMMARY", summary)
            except Exception:  # noqa: BLE001
                await Actor.set_status_message(head + " (the summary could not be saved)")
            else:
                await Actor.set_status_message(head + " (the summary needed a retry)")


if __name__ == "__main__":
    asyncio.run(main())
