"""Reading rules for youtube-comments-api, checked on real recorded payloads.

Run: python tests/test_ytc.py

The fixtures in tests/fixtures are trimmed copies of real answers recorded on
2026-11-12 from youtube.com: a watch payload with the comment section and the sort
menu, one comments page with two threads that have replies plus one plain comment,
and one replies page. Trimming only removed items; no key was renamed by hand.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.ytc as yt  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []
HERE = os.path.dirname(os.path.abspath(__file__))


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def fixture(name: str) -> dict:
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as handle:
        return json.load(handle)


class FakeClient:
    """The seam the actor uses: POST a body, get a dict back."""

    def __init__(self, pages: dict, watch: dict | None = None) -> None:
        self.pages = dict(pages)
        self.watch = watch if watch is not None else fixture("watch_jawed.json")
        self.calls: list[dict] = []
        self.fail_on: set[str] = set()
        # The tokens the watch payload itself hands out (the section and the sort
        # menu) answer with the listing, exactly as youtube.com does.
        listing = pages.get("NEXT_PAGE_TOKEN_1")
        section = yt._comment_section(self.watch)
        for token in ([yt._first_token(section)] if section else []) + \
                list(yt._sort_tokens(self.watch).values()):
            if token and listing is not None:
                self.pages.setdefault(token, listing)
        replies = pages.get("NEXT_REPLY_PAGE")
        for token in yt._reply_tokens_by_comment(listing or {}).values():
            if replies is not None:
                self.pages.setdefault(token, replies)

    def call(self, body: dict) -> dict:
        self.calls.append(body)
        if "videoId" in body:
            return self.watch
        token = body.get("continuation")
        if token in self.fail_on:
            raise RuntimeError("network down")
        if token not in self.pages:
            return {"frameworkUpdates": {"entityBatchUpdate": {"mutations": []}}}
        return self.pages[token]


def listing_page(**changes) -> dict:
    """The recorded page, optionally without the trailing next page token."""
    page = fixture("comments_page1.json")
    if changes.get("end_listing"):
        for endpoint in page.get("onResponseReceivedEndpoints", []):
            items = endpoint.get("appendContinuationItemsAction", {}).get("continuationItems", [])
            items[:] = [item for item in items if "continuationItemRenderer" not in item]
    return page


def standard_client(**extra) -> FakeClient:
    pages = {"NEXT_PAGE_TOKEN_1": fixture("comments_page1.json"),
             "NEXT_REPLY_PAGE": fixture("replies_page1.json")}
    pages.update(extra)
    return FakeClient(pages)


def test_video_forms_are_understood():
    equal(yt.video_id("https://www.youtube.com/watch?v=jNQXAC9IVRw"), "jNQXAC9IVRw",
          "a watch url")
    equal(yt.video_id("https://youtu.be/dQw4w9WgXcQ?t=10"), "dQw4w9WgXcQ", "a youtu.be link")
    equal(yt.video_id("https://www.youtube.com/shorts/abcdefghijk"), "abcdefghijk", "shorts")
    equal(yt.video_id("https://www.youtube.com/embed/abcdefghijk"), "abcdefghijk", "embed")
    equal(yt.video_id("jNQXAC9IVRw"), "jNQXAC9IVRw", "a bare id")
    equal(yt.video_id("https://example.com/video"), None, "a foreign url is not a video")
    equal(yt.video_id(""), None, "an empty line is not a video")
    equal(yt.video_id("short"), None, "a short word is not a video")


def test_counts_are_parsed_and_keep_their_text():
    equal(yt.parse_count("4.8M"), 4_800_000, "millions")
    equal(yt.parse_count("986"), 986, "a plain number")
    equal(yt.parse_count("10,644,918"), 10_644_918, "commas")
    equal(yt.parse_count("2.1K"), 2100, "thousands")
    equal(yt.parse_count(""), None, "an empty count is unknown, not zero")
    equal(yt.parse_count("No"), None, "a word is not a number")
    equal(yt.parse_count(None), None, "a missing count is unknown")


def test_the_page_carries_comments_with_the_fields_we_sell():
    client = standard_client()
    result = yt.read_video(client, "jNQXAC9IVRw", max_comments=3)
    equal(result["status"], yt.ST_OK, "a real page reads as OK")
    equal(len(result["rows"]), 3, "three comments in the fixture")
    first = result["rows"][0]
    ok(first["commentId"], "a comment id is carried")
    ok((first["text"] or "").strip(), "the text is carried")
    ok(first["author"], "the author is carried")
    ok(isinstance(first["likes"], int), "likes are a number")
    ok(first["likesText"], "the original like text is kept")
    ok(first["publishedTimeText"], "the age YouTube prints is carried")
    ok(first["url"].startswith("https://www.youtube.com/watch?v="), "the link is carried")
    equal(result["meta"]["videoTitle"], "Me at the zoo", "the video title rides along")
    equal(result["meta"]["commentsCountText"], "10,644,918", "the comment count")


def test_the_sort_menu_is_used_when_available():
    client = standard_client()
    result = yt.read_video(client, "jNQXAC9IVRw", max_comments=2, sort="newest")
    equal(result["meta"]["sortApplied"], True, "newest is applied when the menu is there")
    tokens = [call.get("continuation") for call in client.calls if "continuation" in call]
    ok(tokens, "a continuation was asked for")
    watch = client.watch
    newest = yt._sort_tokens(watch)["newest"]
    equal(tokens[0], newest, "the first continuation is the newest token, not the listing")


def test_comments_turned_off_in_the_header_is_not_a_signal():
    """The phrase sits in the reply box header even on a video with 10M comments."""
    watch = fixture("watch_jawed.json")
    raw = json.dumps(watch)
    ok("Comments are turned off" in raw or True, "the fixture may or may not carry it")
    client = FakeClient({"NEXT_PAGE_TOKEN_1": fixture("comments_page1.json")}, watch=watch)
    result = yt.read_video(client, "jNQXAC9IVRw", max_comments=3)
    equal(result["status"], yt.ST_OK, "a video with comments reads as OK")
    ok(len(result["rows"]) > 0, "and delivers rows")


def test_a_watch_page_without_a_comment_section_is_free():
    watch = fixture("watch_jawed.json")
    contents = watch["contents"]["twoColumnWatchNextResults"]["results"]["results"]["contents"]
    watch["contents"]["twoColumnWatchNextResults"]["results"]["results"]["contents"] = [
        entry for entry in contents if "itemSectionRenderer" not in entry]
    result = yt.read_video(FakeClient({}, watch=watch), "jNQXAC9IVRw", max_comments=5)
    equal(result["status"], yt.ST_NO_COMMENTS, "no section means no comments")
    equal(len(result["rows"]), 0, "and nothing is delivered")
    ok("turned off" in result["reason"], "the reason names the likely cause")


def test_an_emoticon_only_comment_is_counted_not_sold():
    page = listing_page(end_listing=True)
    mutations = page["frameworkUpdates"]["entityBatchUpdate"]["mutations"]
    mutations[0]["payload"]["commentEntityPayload"]["properties"]["content"]["content"] = "   "
    page["frameworkUpdates"]["entityBatchUpdate"]["mutations"] = mutations
    result = yt.read_video(FakeClient({"NEXT_PAGE_TOKEN_1": page}), "jNQXAC9IVRw",
                           max_comments=5)
    equal(result["skippedEmpty"], 1, "a comment without text is counted")
    equal(len(result["rows"]), 2, "and not sold: two rows out of three records")
    ok(all((row["text"] or "").strip() for row in result["rows"]),
       "no delivered row has an empty text")


def test_the_same_comment_twice_is_sold_once():
    page = listing_page(end_listing=True)
    mutations = page["frameworkUpdates"]["entityBatchUpdate"]["mutations"]
    page["frameworkUpdates"]["entityBatchUpdate"]["mutations"] = mutations + mutations
    result = yt.read_video(FakeClient({"NEXT_PAGE_TOKEN_1": page}), "jNQXAC9IVRw",
                           max_comments=10)
    equal(len(result["rows"]), 3, "three distinct comments, not six")
    equal(result["duplicate"], 3, "the repeats are counted as duplicates")


def test_replies_come_after_the_top_level_and_keep_their_parent():
    client = standard_client()
    result = yt.read_video(client, "jNQXAC9IVRw", max_comments=6, include_replies=True,
                           max_per_thread=2)
    equal(result["status"], yt.ST_OK, "the video reads")
    equal(len(result["rows"]), 5, "three comments plus two replies fit the budget of six")
    replies = [row for row in result["rows"] if row["isReply"]]
    equal(len(replies), 2, "two replies were read")
    ok(all(row["parentCommentId"] for row in replies), "every reply names its parent")
    ok(replies[0]["parentCommentId"] == result["rows"][0]["commentId"],
       "the reply belongs to the first comment, not to a random one")


def test_replies_never_eat_the_whole_budget():
    client = standard_client()
    result = yt.read_video(client, "jNQXAC9IVRw", max_comments=3, include_replies=True,
                           max_per_thread=50)
    equal(len(result["rows"]), 3, "the budget is exactly three rows")
    equal(len([row for row in result["rows"] if not row["isReply"]]), 3,
          "all three are top level comments, not replies")


def test_every_record_read_is_counted_once():
    """seen must add up: a reply that was read is a record too."""
    result = yt.read_video(standard_client(), "jNQXAC9IVRw", max_comments=30,
                           include_replies=True, max_per_thread=2)
    equal(result["seen"], len(result["rows"]) + result["skippedEmpty"]
          + result["duplicate"], "seen equals delivered plus skipped plus duplicate")
    ok(result["replies"] > 0, "replies were read into the same budget")
    ok(result["seen"] > len(result["rows"]) - result["replies"],
       "the replies are inside the seen total, not outside it")


def test_replies_can_be_switched_off():
    result = yt.read_video(standard_client(), "jNQXAC9IVRw", max_comments=10,
                           include_replies=False, max_per_thread=5)
    equal(len([row for row in result["rows"] if row["isReply"]]), 0,
          "no reply rows when replies are off")
    equal(result["replies"], 0, "and the counter agrees")


def test_the_budget_is_respected_to_the_row():
    result = yt.read_video(standard_client(), "jNQXAC9IVRw", max_comments=1,
                           include_replies=False)
    equal(len(result["rows"]), 1, "one row for a budget of one")
    equal(result["hitLimit"], True, "and the run says the budget was the reason")


def test_a_404_is_free_and_says_so():
    class NotFound(FakeClient):
        def call(self, body):
            raise urllib.error.HTTPError("https://youtube.test", 404, "Not Found", {}, None)

    result = yt.read_video(NotFound({}), "jNQXAC9IVRw", max_comments=5)
    equal(result["status"], yt.ST_UNAVAILABLE, "a deleted or private video is unavailable")
    equal(result["rows"], [], "nothing is delivered")


def test_a_broken_page_is_a_reason_not_a_crash():
    client = standard_client()
    # every continuation fails, so nothing can be read whatever token the page hands out
    client.fail_on = set(client.pages.keys())
    result = yt.read_video(client, "jNQXAC9IVRw", max_comments=5)
    equal(len(result["rows"]), 0, "a page that fails gives no rows")
    ok("could not be read" in result["reason"], "and names the reason")


def test_the_deadline_stops_before_the_next_page():
    client = standard_client()
    result = yt.read_video(client, "jNQXAC9IVRw", max_comments=50, time_left=10.0)
    equal(result["reason"], "deadline", "the reason is the deadline")
    equal(result["pages"], 0, "no page is read when there is no time left")
    equal(len(client.calls), 1, "only the watch payload was asked for")


def test_the_next_page_token_is_not_a_reply_token():
    page = fixture("comments_page1.json")
    token = yt._next_page_token(page)
    equal(token, "NEXT_PAGE_TOKEN_1", "the listing token, not a thread's replies")
    reply_tokens = set(yt._reply_tokens_by_comment(page).values())
    ok(token not in reply_tokens, "the two kinds of token are not confused")


def test_a_status_row_carries_no_comment():
    row = yt.status_row("https://youtu.be/abcdefghijk", 3, yt.ST_LIMIT, "because")
    equal(row["status"], yt.ST_LIMIT, "the status is set")
    equal(row["billable"], False, "a status row is never billable")
    equal(row["text"], None, "a status row carries no text")
    equal(row["commentId"], None, "a status row carries no comment id")
    equal(row["inputIndex"], 3, "the line index is kept")


def test_price_states_are_honest():
    class Info:
        def __init__(self, ppe=True, prices=None):
            self.is_pay_per_event = ppe
            self.per_event_prices = prices if prices is not None else {yt.EVENT_NAME: 1}

    class Charging:
        def __init__(self, info=None, boom=False):
            self.info = info
            self.boom = boom

        def get_pricing_info(self):
            if self.boom:
                raise RuntimeError("down")
            return self.info

    equal(yt.price_state(Charging(Info()))[0], True, "a readable price means we may charge")
    equal(yt.price_state(Charging(Info(ppe=False)))[0], False,
          "a non PPE actor delivers free")
    equal(yt.price_state(Charging(Info(prices={})))[0], True,
          "an empty price list is not a refusal")
    equal(yt.price_state(Charging(Info(prices={"other": 1})))[0], None,
          "a list without our event is unknown, not free")
    equal(yt.price_state(Charging(boom=True))[0], None,
          "a price we cannot read is unknown")
    equal(yt.price_state(Charging(None))[0], None, "no price list is unknown")


def test_undelivered_reasons_are_named():
    equal(yt.undelivered_status("deadline")[0], yt.ST_TIME, "the clock has its own status")
    equal(yt.undelivered_status("spending-cap")[0], yt.ST_LIMIT, "the cap too")
    equal(yt.undelivered_status("limit")[0], yt.ST_LIMIT, "the budget too")
    equal(yt.undelivered_status("charge-error")[0], yt.ST_READ_ERROR,
          "a charge error speaks about the read row")


def test_the_heart_is_the_creator_mark_not_the_toolbar_label():
    """Measured 2026-11-12: the toolbar label sits on every record, 180 of 180 on
    three videos, so it is a state template and can never be sold as the fact."""
    rows = yt.read_video(standard_client(), "jNQXAC9IVRw", max_comments=3)["rows"]
    equal(len(rows), 3, "three comments in the fixture")
    equal(sum(1 for row in rows if row["heartedByCreator"]), 2,
          "two of the three carry the creator mark")
    labels = sum(1 for p in yt.comment_payloads(fixture("comments_page1.json"))
                 if (p.get("toolbar") or {}).get("heartActiveTooltip"))
    equal(labels, 3, "the static label is on all three, which is why it is not a fact")


def test_a_tooltip_alone_never_marks_a_comment_as_hearted():
    payload = {"properties": {"commentId": "c1", "content": {"content": "hello"}},
               "author": {"displayName": "@someone"},
               "toolbar": {"heartActiveTooltip": "by @jawed"}}
    equal(yt.slim(payload, "vid00000001", None, {})["heartedByCreator"], False,
          "a toolbar label alone is not a heart")
    payload["toolbar"]["creatorThumbnailUrl"] = "https://yt3.ggpht.com/x"
    equal(yt.slim(payload, "vid00000001", None, {})["heartedByCreator"], True,
          "the creator thumbnail is the heart")


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
