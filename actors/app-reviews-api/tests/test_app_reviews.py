"""Money and rule checks for app-reviews-api.

Run: python tests/test_app_reviews.py

The Actor here repeats the real SDK on purpose:
  * the paid push is `Actor.push_data(data, *, charged_event_name=...)` and answers with a
    real `ChargeResult` from apify._charging;
  * `Dataset.push_data(data)` takes no event name and writes everything;
  * `get_pricing_info()` and `is_event_charge_limit_reached()` are synchronous;
  * `get_metadata()` returns an object with `item_count`, not a dict.
A fake that invents its own shapes hides the defect it is meant to catch.

The network is replaced at the two seams the actor itself uses: `fetch_json` for Apple and
`read_play` for Google Play, so no test ever touches the internet.
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
import src.apps as ap  # noqa: E402

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


class FakeDataset:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.fail_free = False

    async def push_data(self, data) -> None:
        if self.fail_free:
            raise RuntimeError("dataset refused")
        if isinstance(data, dict):
            data = [data]
        self.rows.extend(dict(item) for item in data)

    async def get_metadata(self):
        return SimpleNamespace(item_count=len(self.rows))



class FakeCharging:
    """limit=None means an endless budget, limit=0 means the cap is already reached."""

    def __init__(self, limit=None, ppe=True, priced=True, readable=True) -> None:
        self.limit = limit
        self.charged = 0
        self.ppe = ppe
        self.priced = priced
        self.readable = readable
        self.raise_on_pricing = False

    def get_pricing_info(self):
        if not self.readable:
            raise RuntimeError("pricing down")
        prices = {ap.EVENT_NAME: 0.00009} if self.priced else {}
        return SimpleNamespace(is_pay_per_event=self.ppe, per_event_prices=prices)

    def is_event_charge_limit_reached(self, event_name: str) -> bool:
        return self.limit is not None and self.charged >= self.limit


class _DatasetView:
    """What Actor.open_dataset() gives in the SDK: push_data and get_metadata only.

    The real Dataset has no set_value at all (apify 4.2.0 and 4.2.1), so this view has
    none either: a fake that is kinder than the SDK hides exactly that kind of defect.
    """

    def __init__(self, owner) -> None:
        self.owner = owner

    async def push_data(self, data) -> None:
        await self.owner.push_data_free(data)

    async def get_metadata(self):
        return SimpleNamespace(item_count=len(self.owner.rows))


class FakeActor(FakeDataset):
    def __init__(self, payload: dict, charging: FakeCharging) -> None:
        super().__init__()
        self.payload = payload
        self.charging = charging
        self.pushes: list[int] = []
        self.fail_paid = False
        self.fail_message = ""
        self.status_messages: list[str] = []

    async def get_input(self):
        return dict(self.payload)

    def get_charging_manager(self):
        return self.charging

    async def open_dataset(self):
        return _DatasetView(self)

    async def open_key_value_store(self):
        return self

    async def push_data(self, data, charged_event_name=None):
        if isinstance(data, dict):
            data = [data]
        if charged_event_name is None:
            await self.push_data_free(data)
            return None
        if self.fail_paid:
            raise RuntimeError("charge exploded")
        want = len(data)
        room = want if self.charging.limit is None else max(0, self.charging.limit - self.charging.charged)
        take = min(want, room)
        self.pushes.append(take)
        self.rows.extend(dict(item) for item in data[:take])
        self.charging.charged += take
        return charge_result(take, reached=take < want)

    async def push_data_free(self, data) -> None:
        if self.fail_free:
            raise RuntimeError("dataset refused")
        # Dataset.push_data in the SDK takes one dict or a list, the fake must too.
        if isinstance(data, dict):
            data = [data]
        self.rows.extend(dict(item) for item in data)

    async def set_value(self, key, value) -> None:
        self.rows.append({"_summary": key, "value": value})

    async def set_status_message(self, message) -> None:
        self.status_messages.append(message)

    async def fail(self, *, status_message=None, exit_code=1, exception=None):
        """Actor.fail in the SDK is keyword-only and raises: the fake does the same."""
        self.fail_message = status_message or ""
        raise SystemExit(exit_code)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None


def run_main(payload: dict, *, charging=None, actor=None) -> tuple[dict, FakeActor]:
    charging = charging or FakeCharging()
    holder = actor or FakeActor(payload, charging)
    original = app.Actor
    app.Actor = holder
    try:
        asyncio.run(app.main())
    except SystemExit:  # Actor.fail is a refusal, not a crash
        pass
    finally:
        app.Actor = original
    summary = {}
    for row in holder.rows:
        if isinstance(row, dict) and row.get("_summary") == "RUN_SUMMARY":
            summary = row.get("value") or {}
    return summary, holder


def reviews(*spec):
    """(platform, appId, reviewId, rating) tuples into rows the collectors accept."""
    out = []
    for platform, app_id, review_id, rating in spec:
        out.append({
            "platform": platform, "appId": app_id, "appTitle": "App",
            "appRating": 4.5, "appRatingsTotal": 100, "country": "us",
            "reviewId": review_id, "rating": rating, "title": None, "text": "text",
            "author": "someone", "version": "1.0", "publishedDate": "2026-11-10T00:00:00Z",
            "thumbsUp": 0, "developerReply": None, "developerReplyDate": None,
            "url": "https://example.test", "source": "apple-rss",
        })
    return out


def patch_network(rows_by_platform: dict, pages: int = 1, status: str = ""):
    """Replace the two network seams with canned answers."""
    original_meta = app.apple_meta
    original_apple = app.read_apple
    original_play = app.read_play

    async def meta(client, app_id, country):
        if status:
            return {}, status
        return {"title": "App", "rating": 4.5, "ratingsTotal": 100}, ""

    async def apple(client, task, country, meta, max_pages, budget, deadline,
                    skipped=None):
        return list(rows_by_platform.get("ios", [])), pages, "", ""

    async def play(task, country, max_pages, budget, deadline, skipped=None):
        if status:
            return [], {}, 0, status, "the store said no"
        return list(rows_by_platform.get("android", [])), {"title": "App", "rating": 4.5,
                                                          "ratingsTotal": 100}, pages, "", ""

    app.apple_meta = meta
    app.read_apple = apple
    app.read_play = play
    return (original_meta, original_apple, original_play)


def unpatch(saved) -> None:
    app.apple_meta, app.read_apple, app.read_play = saved


def test_a_line_becomes_an_app_or_a_reason():
    equal(ap.normalize_app("389801252")[0], {"platform": "ios", "appId": "389801252"},
          "a bare numeric id is an App Store app")
    equal(ap.normalize_app("com.instagram.android")[0],
          {"platform": "android", "appId": "com.instagram.android"},
          "a package is a Play app")
    equal(ap.normalize_app("https://apps.apple.com/us/app/instagram/id389801252")[0],
          {"platform": "ios", "appId": "389801252"}, "an App Store link is read")
    equal(ap.normalize_app("https://play.google.com/store/apps/details?id=com.x.y")[0],
          {"platform": "android", "appId": "com.x.y"}, "a Play link is read")
    parsed, problem = ap.normalize_app("hello world")
    equal(parsed, None, "a sentence is not an app")
    ok("not read and not charged" in problem, f"and the reason says so: {problem}")
    equal(ap.normalize_app("   ")[0], None, "an empty line is not an app")


def test_storefronts_are_validated_before_any_request():
    equal(ap.countries_of(None)[0], ["us"], "no storefront means us")
    equal(ap.countries_of("us,de")[0], ["us", "de"], "a comma list is split")
    equal(ap.countries_of(["us", "US", "us"])[0], ["us"], "repeats collapse")
    codes, problem = ap.countries_of(["usa"])
    equal(codes, [], "a three letter code is refused")
    ok("two letter storefront" in problem, f"with the reason: {problem}")


def test_tasks_name_every_line_they_never_read():
    tasks, planned = app.build_tasks(["com.a.b", "com.a.b", "not an app", "com.c.d"], 1)
    equal(len(tasks), 1, "one app is read")
    equal(planned["counts"]["duplicate"], 1, "the repeat is a duplicate")
    equal(planned["counts"]["invalid"], 1, "the sentence is invalid")
    equal(planned["counts"]["limited"], 1, "the app past the limit is named")
    ok(all(row["status"].startswith("NOT_CHECKED") for row in planned["free"]),
       "and every skipped line has a free status row")


def test_filters_are_free_and_counted():
    row = reviews(("ios", "1", "r1", 1))[0]
    equal(ap.keep_review(row, 2, None, None)[0], False, "minRating drops a one star review")
    equal(ap.keep_review(row, 1, 1, None)[0], True, "the same review survives minRating 1")
    old = reviews(("ios", "1", "r1", 5))[0]
    old["publishedDate"] = "2026-01-01T00:00:00Z"
    equal(ap.keep_review(old, None, None, "2026-11-01")[0], False,
          "publishedAfter drops an old review")


def test_a_clean_run_charges_exactly_what_it_delivers():
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5), ("ios", "1", "r2", 4))})
    try:
        summary, holder = run_main({"apps": ["389801252"], "maxReviewsPerApp": 10})
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 2, "two reviews, two charges")
    equal(summary["reviewsDelivered"], 2, "and two delivered")
    equal(summary["byStatus"].get("OK"), 2, "both rows are OK")
    equal(summary["counterMismatch"], False, "the counters add up")
    equal([r["status"] for r in holder.rows if r.get("reviewId")], ["OK", "OK"],
          "the dataset carries the reviews")


def test_the_same_review_from_two_storefronts_is_charged_once():
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5), ("ios", "1", "r2", 4))})
    try:
        summary, holder = run_main({"apps": ["389801252"], "countries": ["us", "de"],
                                    "maxReviewsPerApp": 10})
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 2, "the same two reviews are not sold twice")
    equal(summary["reviewsDuplicate"], 2, "the second storefront is counted as repeats")
    equal(len([r for r in holder.rows if r.get("reviewId")]), 2, "two rows in the dataset")


def test_the_spending_cap_turns_the_rest_into_free_rows():
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5), ("ios", "1", "r2", 4),
                                          ("ios", "1", "r3", 3))})
    try:
        summary, holder = run_main({"apps": ["389801252"], "maxReviewsPerApp": 10},
                                   charging=FakeCharging(limit=2))
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 2, "the cap allowed two")
    equal(summary["reviewsCapped"], 1, "the third is named as capped")
    equal(summary["byStatus"].get("NOT_CHECKED_CAP"), 1, "with its own status")
    statuses = [r["status"] for r in holder.rows if r.get("status")]
    equal(statuses.count("NOT_CHECKED_CAP"), 1, "and the buyer sees the row")


def test_an_unreadable_price_delivers_nothing():
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5))})
    try:
        summary, holder = run_main({"apps": ["389801252"]},
                                   charging=FakeCharging(readable=False))
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 0, "no price, no charge")
    equal(summary["pricingEventPriced"], None, "null, not false")
    equal(summary["billingActive"], None, "billing is unknown, not off")
    equal(summary["stopReason"], "billing-price-unknown", "the run says why it stopped")
    ok(all(r["status"] != "OK" for r in holder.rows if r.get("reviewId")),
       "and no review is sold")


def test_a_charge_error_stops_and_names_the_doubt():
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5))})
    holder = None
    try:
        charging = FakeCharging()
        holder = FakeActor({"apps": ["389801252"]}, charging)
        holder.fail_paid = True
        summary, holder = run_main({"apps": ["389801252"]}, charging=charging, actor=holder)
    finally:
        unpatch(saved)
    equal(summary["stopReason"], "charge-error", "the run stops on a charge error")
    equal(summary["chargedEvents"], 0, "nothing is charged")
    equal(summary["chargeUncertain"] + summary["deliveredUncharged"], 1,
          "the batch is named as uncertain or delivered without money")


def test_a_review_past_the_limit_is_free_but_visible():
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5), ("ios", "1", "r2", 4))})
    try:
        summary, holder = run_main({"apps": ["389801252"], "maxReviewsPerApp": 1})
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 1, "only one review is sold")
    equal(summary["reviewsLimited"], 1, "the other is limited")
    equal(summary["byStatus"].get("NOT_CHECKED_LIMIT"), 1, "and named in the dataset")


def test_an_app_the_store_does_not_know_is_free():
    saved = patch_network({}, status="the App Store does not know this app id")
    try:
        summary, holder = run_main({"apps": ["389801252"]})
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 0, "a missing app costs nothing")
    equal(summary["appsNotFound"], 1, "and is counted as not found")


def test_non_ppe_actor_delivers_free_and_says_so():
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5))})
    try:
        summary, holder = run_main({"apps": ["389801252"]}, charging=FakeCharging(ppe=False))
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 0, "a non PPE actor charges nothing")
    equal(summary["reviewsDelivered"], 1, "but the buyer still gets the review")
    equal(summary["billingActive"], False, "and the summary says billing is off")


def test_batches_are_written_and_charged_in_twenties():
    many = [("ios", "1", f"r{index}", 5) for index in range(45)]
    saved = patch_network({"ios": reviews(*many)})
    try:
        summary, holder = run_main({"apps": ["389801252"], "maxReviewsPerApp": 100})
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 45, "every review is charged")
    equal(holder.pushes, [20, 20, 5], "in batches of twenty, then the tail")
    equal(summary["counterMismatch"], False, "the counters still add up")


def test_an_invalid_setting_is_refused_before_any_request():
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5))})
    try:
        summary, holder = run_main({"apps": ["389801252"], "countries": ["usa"]})
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 0, "a bad storefront sells nothing")
    equal(summary["appsAttempted"], 0, "and nothing is read")
    equal(holder.rows[0]["status"], "NOT_CHECKED_INVALID", "the line says why")
    ok("two letter storefront" in holder.rows[0]["reason"],
       f"with the real reason: {holder.rows[0]['reason']}")


def test_an_empty_feed_for_a_rated_app_is_not_sold_as_no_reviews():
    """Apple answers an empty feed to a non-browser agent: that is a failed read, not an
    app without reviews, and the buyer must see the difference."""
    saved = patch_network({"ios": []})
    try:
        summary, holder = run_main({"apps": ["389801252"]})
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 0, "an empty feed costs nothing")
    equal(summary["appsNoReviews"], 0, "and it is not called an app without reviews")
    equal(summary["appsFailed"], 1, "it is a failed read")
    row = [r for r in holder.rows if r.get("status") == "NOT_CHECKED_ERROR"][0]
    ok("reports 100 rating" in row["reason"], f"with the store's own number: {row['reason']}")


def test_an_empty_input_is_refused_not_silently_green():
    summary, holder = run_main({})
    ok(holder.fail_message, "an empty apps list must fail the run, not return green")
    ok("apps is empty" in holder.fail_message, f"with the real reason: {holder.fail_message}")
    equal([r for r in holder.rows if r.get("status")], [], "and nothing is written")


def test_a_review_past_the_limit_carries_no_text():
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5), ("ios", "1", "r2", 4),
                                          ("ios", "1", "r3", 3))})
    try:
        summary, holder = run_main({"apps": ["389801252"], "maxReviewsPerApp": 1})
    finally:
        unpatch(saved)
    limited = [r for r in holder.rows if r.get("status") == "NOT_CHECKED_LIMIT"]
    equal(len(limited), 2, "the two reviews past the limit are named")
    equal([r.get("reviewId") for r in limited], [None, None], "and carry no review id")
    ok(all(not r.get("text") for r in limited), "and no review text is given away free")
    equal(summary["chargedEvents"], 1, "only the first review is sold")


def test_a_review_without_a_date_is_free_when_a_window_is_set():
    rows = reviews(("ios", "1", "r1", 5))
    rows[0]["publishedDate"] = None
    saved = patch_network({"ios": rows})
    try:
        summary, holder = run_main({"apps": ["389801252"], "publishedAfter": "2026-11-01"})
    finally:
        unpatch(saved)
    equal(summary["chargedEvents"], 0, "no date, no charge")
    equal(summary["reviewsFiltered"], 1, "and the review is named as filtered")
    says, why = ap.keep_review({"rating": 5, "publishedDate": None}, None, None, "2026-11-01")
    equal(says, False, "a review with no date is dropped when a window is set")
    ok("no publication date" in why, f"with the real reason: {why}")


def test_a_charge_error_names_every_read_review():
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5), ("ios", "1", "r2", 4))})
    try:
        charging = FakeCharging()
        holder = FakeActor({"apps": ["389801252"]}, charging)
        holder.fail_paid = True
        summary, holder = run_main({"apps": ["389801252"]}, charging=charging, actor=holder)
    finally:
        unpatch(saved)
    named = [r for r in holder.rows if r.get("status") == "NOT_CHECKED_ERROR"]
    equal(len(named), 2, "each read review has its own row after a charge error")
    ok(all("not delivered" in (r.get("reason") or "") for r in named),
       "and the row says it was read but not delivered")


def test_three_refused_free_rows_stop_the_run():
    holder = FakeActor({"apps": ["a", "b", "c", "d", "e"]}, FakeCharging())
    holder.fail_free = True
    summary, holder = run_main({"apps": ["a", "b", "c", "d", "e"]}, actor=holder)
    equal(summary.get("stopReason"), "dataset-write-error", "three refusals stop the run")
    ok(summary.get("writeErrors", 0) >= 3, f"and they are counted: {summary.get('writeErrors')}")


def test_the_fake_dataset_repeats_the_sdk():
    ok("set_value" not in dir(FakeDataset), "the fake dataset has no set_value, as the SDK one")
    ok("set_value" not in dir(_DatasetView(None)), "and neither does what open_dataset returns")
    ok("set_value" in dir(FakeActor), "while the actor does, as the real Actor does")


def test_the_bare_summary_key_lands_where_the_schema_points():
    """The schema links to records/RUN_SUMMARY, so that key must exist even inside a run."""
    saved = patch_network({"ios": reviews(("ios", "1", "r1", 5))})
    previous = os.environ.get("ACTOR_RUN_ID")
    os.environ["ACTOR_RUN_ID"] = "TESTRUN42"
    try:
        summary, holder = run_main({"apps": ["389801252"]})
    finally:
        if previous is None:
            os.environ.pop("ACTOR_RUN_ID", None)
        else:
            os.environ["ACTOR_RUN_ID"] = previous
        unpatch(saved)
    keys = [r.get("_summary") for r in holder.rows if isinstance(r, dict) and r.get("_summary")]
    ok("RUN_SUMMARY" in keys, f"the bare key the schema links to exists: {keys}")
    ok("RUN_SUMMARY-TESTRUN42" in keys, f"and the run-scoped key sits beside it: {keys}")
    equal(summary.get("reviewsDelivered"), 1, f"with the real summary inside: {summary}")


def run_all() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"test_app_reviews: {CHECKS[0]} checks, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL", fail)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run_all())
