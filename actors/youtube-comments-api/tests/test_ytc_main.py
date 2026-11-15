"""Money and rules for youtube-comments-api, run through the real main().

Run: python tests/test_ytc_main.py

The Actor below repeats the real SDK on purpose:
  * the paid push is `Actor.push_data(data, *, charged_event_name=...)` and answers with a
    real `ChargeResult` from apify._charging;
  * `Dataset.push_data(data)` takes no event name, and the dataset view has no
    `set_value`: in apify 4.2.0 and 4.2.1 `Dataset.set_value` does not exist at all, only
    `Actor.set_value` writes the summary into the run's default store;
  * `get_pricing_info()` and `is_event_charge_limit_reached()` are synchronous;
  * `get_metadata()` returns an object with `item_count`, not a dict;
  * `Actor.fail` is keyword only and raises.
A fake that invents kinder shapes hides the defect it is meant to catch.

The network seam is `ytc.read_video`, so no test touches youtube.com.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from apify._charging import ChargeResult  # the real shape
except Exception:  # pragma: no cover - older SDK
    ChargeResult = None

import src.main as app  # noqa: E402
import src.ytc as yt  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def charge_result(count: int, reached: bool = False):
    """The real object the SDK answers with, so the fake cannot invent its own shape."""
    if ChargeResult is not None:
        try:
            return ChargeResult(charged_count=count, event_charge_limit_reached=reached,
                                chargeable_within_limit={})
        except TypeError:  # an older signature
            return ChargeResult(charged_count=count, event_charge_limit_reached=reached)
    return SimpleNamespace(charged_count=count, event_charge_limit_reached=reached)


class FakeCharging:
    """limit=None means an endless budget, limit=0 means the cap is already reached."""

    def __init__(self, limit=None, ppe=True, priced=True, readable=True) -> None:
        self.limit = limit
        self.charged = 0
        self.ppe = ppe
        self.priced = priced
        self.readable = readable

    def get_pricing_info(self):
        if not self.readable:
            raise RuntimeError("pricing down")
        prices = {yt.EVENT_NAME: 0.0004} if self.priced else {}
        return SimpleNamespace(is_pay_per_event=self.ppe, per_event_prices=prices)

    def is_event_charge_limit_reached(self, event_name: str) -> bool:
        return self.limit is not None and self.charged >= self.limit


class _DatasetView:
    """What Actor.open_dataset() gives: push_data and get_metadata, and no set_value."""

    def __init__(self, owner) -> None:
        self.owner = owner

    async def push_data(self, data) -> None:
        await self.owner.push_free(data)

    async def get_metadata(self):
        return SimpleNamespace(item_count=len(self.owner.rows))


class FakeActor:
    def __init__(self, payload: dict, charging: FakeCharging) -> None:
        self.payload = payload
        self.charging = charging
        self.rows: list[dict] = []
        self.values: dict[str, dict] = {}
        self.pushes: list[int] = []
        self.fail_paid = False
        self.fail_free = False
        self.charged_count_none = False
        self.status_messages: list[str] = []
        self.fail_message = ""

    async def get_input(self):
        return dict(self.payload)

    def get_charging_manager(self):
        return self.charging

    async def open_dataset(self):
        return _DatasetView(self)

    async def push_free(self, data) -> None:
        if self.fail_free:
            raise RuntimeError("dataset refused")
        if isinstance(data, dict):
            data = [data]
        self.rows.extend(dict(item) for item in data)

    async def push_data(self, data, charged_event_name=None):
        if isinstance(data, dict):
            data = [data]
        if charged_event_name is None:
            await self.push_free(data)
            return None
        if self.fail_paid:
            raise RuntimeError("charge exploded")
        if self.charged_count_none:
            self.rows.extend(dict(item) for item in data)
            self.pushes.append(len(data))
            return SimpleNamespace(charged_count=None)
        want = len(data)
        room = want if self.charging.limit is None else max(0, self.charging.limit - self.charging.charged)
        take = min(want, room)
        self.pushes.append(take)
        self.rows.extend(dict(item) for item in data[:take])
        self.charging.charged += take
        return charge_result(take, reached=take < want)

    async def set_value(self, key, value) -> None:
        self.values[key] = value

    async def set_status_message(self, message) -> None:
        self.status_messages.append(message)

    async def fail(self, *, status_message=None, exit_code=1, exception=None):
        self.fail_message = status_message or ""
        raise SystemExit(exit_code)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None


def comment(cid: str, text: str = "a real comment", reply_to: str | None = None) -> dict:
    return {"commentId": cid, "parentCommentId": reply_to, "isReply": reply_to is not None,
            "author": "@someone", "text": text, "likes": 3, "likesText": "3",
            "replyCount": 0, "replyCountText": None, "publishedTimeText": "2 days ago",
            "heartedByCreator": False, "pinnedText": None, "url": "https://youtu.be/x"}


def video_result(video: str, comments: list[dict], status: str = yt.ST_OK,
                 reason: str = "", seen: int | None = None, skipped: int = 0,
                 duplicate: int = 0, hit_limit: bool = False) -> dict:
    return {"status": status, "rows": [dict(c) for c in comments] if status == yt.ST_OK else [],
            "pages": 1, "reason": reason, "meta": {"videoTitle": f"title {video}",
                                                   "channel": "chan", "commentsCountText": "10"},
            "replies": 0, "repliesOffered": 0,
            "seen": seen if seen is not None else len(comments),
            "skippedEmpty": skipped, "duplicate": duplicate, "hitLimit": hit_limit}


def run_main(payload: dict, results: dict, *, charging=None, actor=None) -> tuple[dict, FakeActor]:
    charging = charging or FakeCharging()
    holder = actor or FakeActor(payload, charging)
    calls: list[str] = []

    def fake_read_video(client, video, max_comments, sort, include_replies,
                        max_per_thread, known, time_left):
        calls.append(video)
        value = results.get(video)
        if value is None:
            return video_result(video, [])
        return value

    original_actor = app.Actor
    original_read = app.ytc.read_video
    app.Actor = holder
    app.ytc.read_video = fake_read_video
    try:
        asyncio.run(app.main())
    except SystemExit:  # Actor.fail is a refusal, not a crash
        pass
    finally:
        app.Actor = original_actor
        app.ytc.read_video = original_read
    return holder.values.get("RUN_SUMMARY", {}), holder


def dataset_rows(holder: FakeActor) -> list[dict]:
    return [row for row in holder.rows if "status" in row]


def test_an_empty_input_is_refused_not_silently_green():
    summary, holder = run_main({"videos": ["   ", ""]}, {})
    equal(dataset_rows(holder), [], "no row is written for an empty input")
    ok("videos is empty" in holder.fail_message, "the refusal says what is missing")
    equal(holder.charging.charged, 0, "and nothing is charged")


def test_a_clean_run_charges_exactly_what_it_delivers():
    results = {"vid00000001": video_result("vid00000001", [comment("c1"), comment("c2")]),
               "vid00000002": video_result("vid00000002", [comment("c3")])}
    summary, holder = run_main({"videos": ["vid00000001", "vid00000002"]}, results)
    equal(holder.charging.charged, 3, "three delivered rows, three charges")
    equal(summary["commentsDelivered"], 3, "the summary counts three delivered")
    equal(summary["chargedEvents"], 3, "and three charged")
    equal(summary["videosRead"], 2, "two videos read")
    equal(summary["counterMismatch"], False, "the counters add up")
    ok("3 comment(s) delivered, 3 charged" in holder.status_messages[-1],
       "the status message is human")
    paid = [row for row in dataset_rows(holder) if row["status"] == yt.ST_OK]
    equal(len(paid), 3, "three OK rows in the dataset")
    ok(all(row["billable"] is True for row in paid), "each OK row claims the price")
    ok(all(row["videoTitle"] for row in paid), "the video title rides on every row")


def test_a_line_that_is_not_a_video_is_free():
    results = {"vid00000001": video_result("vid00000001", [comment("c1")])}
    summary, holder = run_main({"videos": ["https://example.com/nope", "vid00000001"]}, results)
    equal(holder.charging.charged, 1, "only the real video is charged")
    statuses = [row["status"] for row in dataset_rows(holder)]
    ok(yt.ST_INVALID in statuses, "the bad line has its own free row")
    invalid = [row for row in dataset_rows(holder) if row["status"] == yt.ST_INVALID][0]
    equal(invalid["billable"], False, "a bad line is never billable")
    equal(invalid["text"], None, "a bad line carries no comment")


def test_a_repeated_video_is_read_once_and_named():
    results = {"vid00000001": video_result("vid00000001", [comment("c1")])}
    summary, holder = run_main({"videos": ["vid00000001", "vid00000001"]}, results)
    equal(holder.charging.charged, 1, "one video, one charge")
    equal(summary["videosDuplicate"], 1, "the repeat is counted")
    statuses = [row["status"] for row in dataset_rows(holder)]
    equal(statuses.count(yt.ST_DUPLICATE), 1, "the repeat has a free row of its own")


def test_a_video_without_comments_is_free_and_explains_itself():
    results = {"vid00000001": video_result("vid00000001", [], status=yt.ST_NO_COMMENTS,
                                    reason="the watch page exposes no comment section")}
    summary, holder = run_main({"videos": ["vid00000001"]}, results)
    equal(holder.charging.charged, 0, "a closed comment section costs nothing")
    equal(summary["videosNoComments"], 1, "and the summary says so")
    row = dataset_rows(holder)[0]
    equal(row["status"], yt.ST_NO_COMMENTS, "the row carries the status")
    ok("turned off" in row["reason"] or "comment section" in row["reason"],
       "the reason is in plain words")


def test_the_spending_cap_turns_the_rest_into_free_rows():
    results = {"vid00000001": video_result("vid00000001", [comment(f"c{i}") for i in range(6)])}
    charging = FakeCharging(limit=2)
    summary, holder = run_main({"videos": ["vid00000001"]}, results, charging=charging)
    equal(holder.charging.charged, 2, "the cap allows exactly two")
    equal(summary["commentsDelivered"], 2, "two are delivered")
    equal(summary["commentsUndelivered"], 4, "four are named as not delivered")
    equal(summary["stopReason"], "spending-cap", "the reason is the cap")
    ok(all(row["text"] is None for row in dataset_rows(holder) if row["status"] != yt.ST_OK),
       "an undelivered row never carries the text it was not paid for")


def test_an_unreadable_price_delivers_nothing():
    results = {"vid00000001": video_result("vid00000001", [comment("c1")])}
    charging = FakeCharging(readable=False)
    summary, holder = run_main({"videos": ["vid00000001"]}, results, charging=charging)
    equal(holder.charging.charged, 0, "an unknown price means no money moves")
    equal(summary["commentsDelivered"], 0, "and nothing is sold")
    equal(summary["billingActive"], None, "the summary says unknown, not off")
    equal(summary["stopReason"], "billing-price-unknown", "the reason is named")
    ok(all(row["status"] == yt.ST_BILLING for row in dataset_rows(holder)),
       "the line is named as not checked for billing")


def test_a_non_ppe_actor_delivers_free_and_says_so():
    results = {"vid00000001": video_result("vid00000001", [comment("c1")])}
    charging = FakeCharging(ppe=False)
    summary, holder = run_main({"videos": ["vid00000001"]}, results, charging=charging)
    equal(holder.charging.charged, 0, "no event price, no charge")
    equal(summary["commentsDelivered"], 1, "the buyer still gets the row")
    equal(summary["billingActive"], False, "and the summary is honest about billing")


def test_a_charge_error_stops_and_names_the_doubt():
    # The first video alone fills a batch, so the charge is attempted mid list and the
    # second video is never read: that is the case where a video must not vanish.
    many = [comment(f"c{i}") for i in range(26)]
    results = {"vid00000001": video_result("vid00000001", many),
               "vid00000002": video_result("vid00000002", [comment("c99")])}
    charging = FakeCharging()
    holder = FakeActor({"videos": ["vid00000001", "vid00000002"]}, charging)
    holder.fail_paid = True
    summary, holder = run_main({"videos": ["vid00000001", "vid00000002"]}, results, actor=holder)
    equal(summary["stopReason"], "charge-error", "the run stops on a charge error")
    equal(holder.charging.charged, 0, "nothing was charged")
    equal(summary["commentsDelivered"], 0, "and nothing counts as delivered")
    equal(summary["commentsUndelivered"], 26, "every read comment is named, not lost")
    equal(summary["videosStopped"], 1, "the video the run never reached is named too")
    ok(all(row["status"] != yt.ST_OK for row in dataset_rows(holder)),
       "no row claims OK when the money failed")


def test_a_missing_charged_count_does_not_invent_money():
    results = {"vid00000001": video_result("vid00000001", [comment("c1")])}
    holder = FakeActor({"videos": ["vid00000001"]}, FakeCharging())
    holder.charged_count_none = True
    summary, holder = run_main({"videos": ["vid00000001"]}, results, actor=holder)
    equal(summary["chargedEvents"], 0, "the summary does not invent a charge")
    equal(summary["deliveredUncharged"], 1, "the row is named as delivered without money")
    ok("did not report" in (summary.get("datasetNote") or ""), "and the note says why")


def test_more_videos_than_allowed_are_named_free():
    results = {f"vid0000000{i}": video_result(f"vid0000000{i}", [comment(f"c{i}")]) for i in range(3)}
    summary, holder = run_main({"videos": ["vid00000000", "vid00000001", "vid00000002"], "maxVideos": 1}, results)
    equal(holder.charging.charged, 1, "only one video may be read")
    equal(summary["videosLimited"], 2, "two videos are over the limit")
    limited = [row for row in dataset_rows(holder) if row["status"] == yt.ST_LIMIT]
    equal(len(limited), 2, "the two others are named free rows")


def test_the_budget_stops_a_video_and_the_row_says_so():
    results = {"vid00000001": video_result("vid00000001", [comment(f"c{i}") for i in range(2)])}
    summary, holder = run_main({"videos": ["vid00000001"], "maxCommentsPerVideo": 2}, results)
    equal(holder.charging.charged, 2, "two comments delivered")
    equal(summary["commentsDelivered"], 2, "and counted")


def test_a_comment_without_text_is_counted_and_never_sold():
    results = {"vid00000001": video_result("vid00000001", [comment("c1")], seen=2, skipped=1)}
    summary, holder = run_main({"videos": ["vid00000001"]}, results)
    equal(summary["commentsSeen"], 2, "both records are counted")
    equal(summary["commentsSkippedNoText"], 1, "one had no text")
    equal(holder.charging.charged, 1, "and only one is charged")
    equal(summary["counterMismatch"], False, "the counters add up")


def test_the_counters_add_up_with_duplicates_and_undelivered():
    results = {"vid00000001": video_result("vid00000001", [comment("c1")], seen=5, duplicate=2)}
    summary, holder = run_main({"videos": ["vid00000001"]}, results)
    equal(summary["commentsSeen"], 5, "five records seen")
    equal(summary["commentsDelivered"], 1, "one delivered")
    equal(summary["commentsDuplicate"], 2, "two duplicates")
    equal(summary["counterMismatch"], True, "a gap must be visible, not hidden")
    ok(summary["countersNote"], "and the note names it")


def test_the_dataset_view_has_no_set_value():
    """apify 4.2.0 and 4.2.1: Dataset has no set_value, only Actor does."""
    holder = FakeActor({"videos": ["vid00000001"]}, FakeCharging())
    view = _DatasetView(holder)
    ok(not hasattr(view, "set_value"), "the fake dataset is as poor as the real one")
    ok(hasattr(holder, "set_value"), "the summary is written through the Actor")


def test_the_summary_lands_where_the_schema_points():
    results = {"vid00000001": video_result("vid00000001", [comment("c1")])}
    summary, holder = run_main({"videos": ["vid00000001"]}, results)
    ok("RUN_SUMMARY" in holder.values, "the bare key is written through Actor.set_value")
    equal(holder.values["RUN_SUMMARY"]["chargedEvents"], 1, "and carries the money")


def test_by_status_matches_the_rows_written():
    results = {"vid00000001": video_result("vid00000001", [comment("c1"), comment("c2")]),
               "nope": None}
    summary, holder = run_main({"videos": ["vid00000001", "not a video"]}, results)
    total = sum(summary["byStatus"].values())
    equal(total, summary["rowsWritten"], "byStatus adds up to the rows written")


def test_a_video_cut_by_its_own_budget_is_not_a_limit():
    """One counter for two events broke the promise videosRequested = attempted +
    stopped + invalid + duplicate + limited: measured 2026-11-12, 4 against 6."""
    results = {"vid00000001": video_result("vid00000001", [comment("c1")], hit_limit=True),
               "vid00000002": video_result("vid00000002", [comment("c2")])}
    summary, holder = run_main({"videos": ["vid00000001", "vid00000002", "nope"]}, results)
    equal(summary["videosLimited"], 0, "no video was over the video limit")
    equal(summary["videosCutByBudget"], 1, "the first video ran out of its own budget")
    parts = (summary["videosAttempted"] + summary["videosStopped"]
             + summary["videosInvalid"] + summary["videosDuplicate"]
             + summary["videosLimited"])
    equal(parts, summary["videosRequested"], "every requested line is counted once")
    equal(summary["videosRead"], 2, "a cut video was read, so it is counted in attempted")
    ok(summary["videosCutByBudget"] <= summary["videosAttempted"],
       "the budget cut is a property of a read video, not a line of its own")


def test_the_counters_follow_the_rows_the_sdk_really_wrote():
    """The SDK writes only what it charged: counting the batch made byStatus and
    rowsWritten lie by the tail (measured 2026-11-12: OK 6 and rowsWritten 10 for 2)."""
    results = {"vid00000001": video_result("vid00000001",
                                           [comment(f"c{i}") for i in range(6)])}
    summary, holder = run_main({"videos": ["vid00000001"]}, results,
                               charging=FakeCharging(limit=2))
    equal(summary["chargedEvents"], 2, "two rows were charged")
    equal(summary["byStatus"].get(yt.ST_OK, 0), 2, "byStatus counts the paid rows only")
    equal(summary["rowsWritten"], 6, "two paid rows and four free status rows")
    equal(len(dataset_rows(holder)), 6, "and that is exactly the dataset")
    equal(sum(summary["byStatus"].values()), summary["rowsWritten"], "the statuses add up")


def run_all() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - a crashing test is a failed check
            FAILS.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")
    for line in FAILS:
        print("FAIL:", line)
    print(f"{CHECKS[0]} checks, {len(FAILS)} failed")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
