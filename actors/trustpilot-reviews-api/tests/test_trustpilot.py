"""The polygon of trustpilot-reviews-api: rules, money, and the live wall answers.

Every case below comes from a live measurement on 2026-11-11, not from an idea of how
Trustpilot might behave: the 10-page wall (page 11 -> the login page /users/connect, measured twice),
404 for an unknown domain, the three wall answers we measured from the VPS, and the
measured fact that made the first gate return the build: the first page of `?stars=N`
is NOT filtered, so the star listings only start working from page 2.

The listing fake below copies that measured behaviour, not a convenient one: plain
page 1 and `?stars=N&page=1` answer the same unfiltered set, and a bucket really
filters from page 2. A test that reads page 1 of a bucket would still pass on a fake
that filters everywhere, and would prove nothing about the real site.
"""
from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import trustpilot as tp  # noqa: E402
from src import main as actor_main  # noqa: E402

CHECKS = {"passed": 0, "failed": []}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        CHECKS["passed"] += 1
    else:
        CHECKS["failed"].append(f"{name}: {detail}")


def eq(name: str, got, want) -> None:
    check(name, got == want, f"got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- addresses
def test_normalize() -> None:
    eq("bare domain", tp.normalize_company("amazon.com")[0], "amazon.com")
    eq("www is dropped", tp.normalize_company("www.amazon.com")[0], "amazon.com")
    eq("uppercase", tp.normalize_company("AMAZON.COM")[0], "amazon.com")
    eq("full url", tp.normalize_company("https://www.amazon.com/review/x?page=2")[0], "amazon.com")
    eq("profile url", tp.normalize_company("https://www.trustpilot.com/review/amazon.com")[0],
       "amazon.com")
    eq("trailing dot", tp.normalize_company("amazon.com.")[0], "amazon.com")
    eq("port is ignored", tp.normalize_company("amazon.com:443")[0], "amazon.com")
    eq("empty is refused", tp.normalize_company("   ")[0], None)
    check("a sentence is refused", tp.normalize_company("two words")[0] is None)
    check("ip is refused", tp.normalize_company("192.168.1.1")[0] is None)
    check("ipv6 literal is refused", tp.normalize_company("[::1]/review")[0] is None)
    check("trustpilot itself is refused", tp.normalize_company("trustpilot.com")[0] is None)
    check("no dot is refused", tp.normalize_company("localhost")[0] is None)
    check("a reason is always given", all(tp.normalize_company(x)[1] for x in
                                          ("", "two words", "192.168.1.1", "localhost")))


def test_urls() -> None:
    eq("plain url", tp.review_url("amazon.com"),
       "https://www.trustpilot.com/review/amazon.com?sort=recency")
    eq("page two", tp.review_url("amazon.com", 2),
       "https://www.trustpilot.com/review/amazon.com?sort=recency&page=2")
    eq("star bucket", tp.review_url("amazon.com", 1, 1),
       "https://www.trustpilot.com/review/amazon.com?sort=recency&stars=1")
    eq("bucket and page", tp.review_url("amazon.com", 3, 5),
       "https://www.trustpilot.com/review/amazon.com?sort=recency&stars=5&page=3")


def test_buckets() -> None:
    eq("default is all five", tp.star_buckets(None), [1, 2, 3, 4, 5])
    eq("empty list is all five", tp.star_buckets([]), [1, 2, 3, 4, 5])
    eq("one star", tp.star_buckets([1]), [1])
    eq("strings are read", tp.star_buckets(["1", "2"]), [1, 2])
    eq("out of range is dropped", tp.star_buckets([0, 6, 9]), [])
    eq("junk is dropped", tp.star_buckets(["x", None, True, 3]), [3])
    eq("repeats collapse", tp.star_buckets([2, 2, 2]), [2])


def test_stars_problem() -> None:
    eq("no filter is no problem", tp.stars_problem(None, [1, 2, 3, 4, 5]), "")
    eq("empty list is no problem", tp.stars_problem([], [1, 2, 3, 4, 5]), "")
    eq("a real filter is no problem", tp.stars_problem(["1"], [1]), "")
    check("a rating outside 1..5 is refused",
          bool(tp.stars_problem([9], tp.star_buckets([9]))))
    check("a word is refused", bool(tp.stars_problem(["worst"], [])))


def test_star_plan() -> None:
    """The measured ceiling: page 1 unfiltered, every bucket from page 2."""
    plan = tp.star_plan([1, 2, 3, 4, 5], 10)
    eq("the newest page is read once, without a filter", plan[0], (None, 1, 1))
    eq("each bucket starts at page 2", [entry[1] for entry in plan[1:]], [2, 2, 2, 2, 2])
    eq("a bucket never reads page 1", any(entry[1] == 1 for entry in plan[1:]), False)
    pages = sum(last - first + 1 for _, first, last in plan)
    eq("pages read for all five buckets", pages, 46)
    eq("reviews that gives", pages * tp.REVIEWS_PER_PAGE, 920)
    eq("the hard ceiling is the measured one", tp.MAX_REVIEWS_HARD, 920)
    one = tp.star_plan([1], 10)
    eq("one bucket reads nine pages", sum(b - a + 1 for _, a, b in one), 9)
    eq("one bucket never touches the unfiltered page", any(e[0] is None for e in one), False)
    eq("one page only reads the newest page", tp.star_plan([1, 2, 3, 4, 5], 1), [(None, 1, 1)])


def test_nav_budget() -> None:
    """M4: the navigation is cut from what is left, not from a constant."""
    nav, wait = tp.nav_budget_seconds(None)
    eq("with no deadline the full budget is given", (nav, wait), (tp.NAV_TIMEOUT_S,
                                                                  tp.PAGE_TIME_BUDGET_S))
    nav, wait = tp.nav_budget_seconds(34)
    check("a short run does not give away 45 seconds", nav < 45, f"nav {nav}")
    check("navigation plus wait fit inside what is left",
          nav + wait <= 34 - tp.PAGE_MARGIN_EXTRA_S + 0.001, f"{nav} + {wait}")
    nav, wait = tp.nav_budget_seconds(240)
    eq("a long run still gets a sane navigation", nav, tp.NAV_TIMEOUT_S)
    check("every budget stays positive", all(tp.nav_budget_seconds(x)[0] > 0
                                            for x in (0, 5, 9, 34, 240)))


# --------------------------------------------------------------------------- walls
def test_blocked() -> None:
    drupal = ("<!DOCTYPE html><html><head><title>Client Challenge</title></head>"
              "<body>Please enable JavaScript to continue</body></html>")
    booking = ("<html><body>We need to verify that you're not a robot before you "
               "continue</body></html>")
    bot = '{"error":"Your request has been blocked as it has been identified as coming from a bot"}'
    waf = "<html><title>Verifying Connection</title><body>Checking your browser</body></html>"
    normal = ("<html><head><title>Amazon Reviews</title></head><body>"
              "<p>Should be zero. I pay for Prime.</p></body></html>")
    check("client challenge is a wall", tp.is_blocked(drupal))
    check("robot question is a wall", tp.is_blocked(booking))
    check("bot json is a wall", tp.is_blocked(bot))
    check("waf interstitial is a wall", tp.is_blocked(waf))
    check("a real listing is not a wall", not tp.is_blocked(normal))
    check("403 is a wall", tp.is_blocked("", 403))
    check("429 is a wall", tp.is_blocked("", 429))
    check("empty body alone is not a wall", not tp.is_blocked("", 200))


# --------------------------------------------------------------------------- a review
LIVE_REVIEW = {
    "id": "6af2eb48e908f5fb20a25b9a",
    "rating": 1,
    "title": "Should be zero",
    "text": "Should be zero. I pay for Prime.",
    "language": "en",
    "likes": 0,
    "published": "2026-11-10T12:00:40.000Z",
    "experienced": "2026-11-09T00:00:00.000Z",
    "verified": False,
    "consumer": {"displayName": "Monika", "countryCode": "GB", "numberOfReviews": 31},
    "dates": {"publishedDate": "2026-11-10T12:00:40.000Z",
              "experiencedDate": "2026-11-09T00:00:00.000Z"},
    "labels": {"verification": {"isVerified": False}},
    "reply": {"message": "Hi Monika, thank you for your feedback.",
              "publishedDate": "2026-11-11T14:43:45.000Z"},
}
META = {"name": "www.amazon.com", "display": "Amazon", "score": 1.5, "total": 50290}


def test_review_row() -> None:
    row = tp.slim_review(LIVE_REVIEW, "amazon.com", 1, META)
    eq("company", row["company"], "www.amazon.com")
    eq("score", row["companyTrustScore"], 1.5)
    eq("total", row["companyReviewsTotal"], 50290)
    eq("review id", row["reviewId"], "6af2eb48e908f5fb20a25b9a")
    eq("review url", row["url"], "https://www.trustpilot.com/reviews/6af2eb48e908f5fb20a25b9a")
    eq("rating", row["rating"], 1)
    eq("title", row["title"], "Should be zero")
    eq("language", row["language"], "en")
    eq("author", row["author"], "Monika")
    eq("author country", row["authorCountry"], "GB")
    eq("author reviews", row["authorReviews"], 31)
    eq("verified", row["verified"], False)
    eq("published", row["publishedDate"], "2026-11-10T12:00:40.000Z")
    eq("experienced", row["experiencedDate"], "2026-11-09T00:00:00.000Z")
    eq("reply", row["companyReply"], "Hi Monika, thank you for your feedback.")
    eq("reply date", row["companyReplyDate"], "2026-11-11T14:43:45.000Z")
    eq("bucket", row["starBucket"], 1)
    eq("bucket source", row["starSource"], "star-filter")
    newest = tp.slim_review(LIVE_REVIEW, "amazon.com", None, META)
    eq("the newest page has no bucket", newest["starBucket"], None)
    eq("and says where it came from", newest["starSource"], "newest")
    empty = tp.slim_review({"id": "x"}, "example.com", None, {})
    eq("no reply is None", empty["companyReply"], None)
    eq("no rating is None", empty["rating"], None)
    eq("missing id has no url", empty["url"], "https://www.trustpilot.com/reviews/x")


def test_review_keys() -> None:
    """A review without an id is not a duplicate."""
    with_id = {"reviewId": "abc"}
    eq("the id is the key", tp.review_meta_key(with_id), "abc")
    no_id = {"reviewId": None, "author": "Monika", "publishedDate": "2026-11-10",
             "rating": 1, "title": "t", "text": "x"}
    key = tp.review_meta_key(no_id)
    check("a review without an id still gets a key", bool(key), key)
    eq("the key is stable", tp.review_meta_key(dict(no_id)), key)
    other = dict(no_id, text="another text")
    check("a different review gets a different key", tp.review_meta_key(other) != key)
    eq("nothing at all has no key", tp.review_meta_key({"reviewId": None}), "")


def test_filters() -> None:
    row = tp.slim_review(LIVE_REVIEW, "amazon.com", 1, META)
    check("no cutoff keeps", tp.after_published(row, None))
    check("older cutoff keeps", tp.after_published(row, "2026-11-01"))
    check("same day keeps", tp.after_published(row, "2026-11-10"))
    check("newer cutoff drops", not tp.after_published(row, "2026-11-11"))
    check("broken date drops", not tp.after_published({"publishedDate": None}, "2026-01-01"))
    check("language matches", tp.language_ok(row, "en"))
    check("language is case free", tp.language_ok(row, "EN"))
    check("another language drops", not tp.language_ok(row, "de"))
    check("empty language keeps all", tp.language_ok(row, None))


# --------------------------------------------------------------------------- money
class FakePricing:
    def __init__(self, ppe=True, prices=("review-delivered",)):
        self.is_pay_per_event = ppe
        self.per_event_prices = {name: 0.0004 for name in prices}


class FakeCharging:
    def __init__(self, info=None, cap=False):
        self.info = info if info is not None else FakePricing()
        self.cap = cap

    def get_pricing_info(self):
        if isinstance(self.info, Exception):
            raise self.info
        return self.info

    def is_event_charge_limit_reached(self, name):
        return self.cap


def test_price() -> None:
    eq("priced", tp.price_state(FakeCharging()), (True, "review-delivered is priced"))
    eq("not ppe", tp.price_state(FakeCharging(FakePricing(ppe=False)))[0], False)
    eq("event missing", tp.price_state(FakeCharging(FakePricing(prices=("other",))))[0], False)
    eq("unreadable", tp.price_state(FakeCharging(RuntimeError("boom")))[0], None)
    eq("none info", tp.price_state(FakeCharging(None))[0], True)
    unknown = FakePricing()
    unknown.is_pay_per_event = None
    unknown.per_event_prices = {}
    eq("unknown answer stays unknown", tp.price_state(FakeCharging(unknown))[0], None)


def test_plan() -> None:
    tasks, planned = actor_main.build_tasks(
        ["amazon.com", "https://www.amazon.com/x", "not a domain", "", "revolut.com"], 1)
    eq("one company read", [t["domain"] for t in tasks], ["amazon.com"])
    eq("invalid counted", planned["counts"]["invalid"], 2)
    eq("duplicate counted", planned["counts"]["duplicate"], 1)
    eq("limited counted", planned["counts"]["limited"], 1)
    eq("free rows written", len(planned["free"]), 4)
    statuses = [row["status"] for row in planned["free"]]
    eq("statuses", statuses, [tp.ST_DUPLICATE, tp.ST_INVALID, tp.ST_INVALID, tp.ST_LIMIT])
    for row in planned["free"]:
        eq("a free row is never billable", row["billable"], False)
    refused, planned2 = actor_main.build_tasks(["amazon.com", "revolut.com"], 5,
                                               refuse_all="bad setting")
    eq("a refused run reads nothing", refused, [])
    eq("every line is named", [row["status"] for row in planned2["free"]],
       [tp.ST_INVALID, tp.ST_INVALID])


# --------------------------------------------------------------------------- the flow
class FakeMeta:
    def __init__(self, count):
        self.item_count = count


class FakeDataset:
    def __init__(self):
        self.rows = []
        self.refuse = False

    async def push_data(self, data):
        if self.refuse:
            raise RuntimeError("dataset refused")
        self.rows.extend(data if isinstance(data, list) else [data])

    async def get_metadata(self):
        # The real SDK answers a DatasetMetadata model with item_count on an attribute.
        return FakeMeta(len(self.rows))


class FakeChargeResult:
    def __init__(self, charged, limit=False):
        self.charged_count = charged
        self.event_charge_limit_reached = limit


class FakeStore:
    def __init__(self):
        self.values = {}

    async def set_value(self, key, value):
        self.values[key] = value


class _ActorMeta(type):
    """`async with Actor` on a class, exactly as the real SDK allows it."""

    async def __aenter__(cls):
        return cls

    async def __aexit__(cls, *exc):
        return False


class FakeActor(metaclass=_ActorMeta):
    """The SDK surface this actor really uses, with the live SDK's own charging cut."""

    input = {}
    dataset = FakeDataset()
    charging = FakeCharging()
    cap = None          # the whole run's budget, spent across calls, None = no cap
    spent = 0
    explode = False     # the SDK raises instead of charging
    write_then_explode = False   # the rows land, then the charge call raises
    statuses = []
    values = {}
    fail_message = None

    @classmethod
    def reset(cls, input_data, charging=None, cap=None, explode=False,
              write_then_explode=False):
        cls.input = input_data
        cls.dataset = FakeDataset()
        cls.charging = charging or FakeCharging()
        cls.cap = cap
        cls.spent = 0
        cls.explode = explode
        cls.write_then_explode = write_then_explode
        cls.statuses = []
        cls.values = {}
        cls.fail_message = None

    @classmethod
    async def get_input(cls):
        return cls.input

    @classmethod
    async def open_dataset(cls):
        return cls.dataset

    @classmethod
    def get_charging_manager(cls):
        return cls.charging

    @classmethod
    async def push_data(cls, data, charged_event_name=None):
        rows = data if isinstance(data, list) else [data]
        if cls.explode:
            raise RuntimeError("charge exploded")
        allowed = len(rows)
        if charged_event_name and cls.cap is not None:
            # The real SDK holds a budget for the run, not for the call: the first
            # batches spend it and a later batch is cut where the budget ends.
            budget = max(0, cls.cap - cls.spent)
            allowed = max(0, min(len(rows), budget))
            cls.spent += allowed
        if allowed:
            await cls.dataset.push_data(rows[:allowed])
        if cls.write_then_explode:
            # Rows are in the dataset and the charge call still failed: one call does
            # both, so this is exactly the "delivered without money" case.
            cls.explode = True
            raise RuntimeError("charge exploded after the write")
        return FakeChargeResult(allowed, limit=allowed < len(rows))

    @classmethod
    async def set_status_message(cls, text):
        cls.statuses.append(text)

    @classmethod
    async def set_value(cls, key, value):
        cls.values[key] = value

    @classmethod
    async def open_key_value_store(cls, name=None):
        return FakeStore()

    @classmethod
    async def fail(cls, status_message=""):
        cls.fail_message = status_message


def review_raw(review_id, rating=1, title="t", text="x", author="a",
               published="2026-11-10T00:00:00.000Z"):
    return {"id": review_id, "rating": rating, "title": title, "text": text,
            "language": "en", "dates": {"publishedDate": published,
                                        "experiencedDate": published},
            "consumer": {"displayName": author, "countryCode": "GB", "numberOfReviews": 1},
            "labels": {"verification": {"isVerified": False}}, "reply": None}


def reviews_page(count, prefix, rating=None, mixed=False):
    if mixed:
        ratings = [1, 3, 5, 4, 2]
        return {"status": 200, "meta": META, "reviews": [
            review_raw(f"{prefix}{i}", rating=ratings[i % len(ratings)], title=f"t{i}")
            for i in range(count)]}
    return {"status": 200, "meta": META, "reviews": [
        review_raw(f"{prefix}{i}", rating=1 if rating is None else rating, title=f"t{i}")
        for i in range(count)]}


class MeasuredListing:
    """A listing that behaves exactly as measured: page 1 of a bucket is unfiltered.

    Page 1 of `?stars=N`, `?stars=M` and the plain listing answer the same unfiltered
    set (`hasActiveFilters: false`), the bucket filters from page 2, page 11 answers
    200 with an empty list.
    """

    def __init__(self, per_page=20, pages=10, mixed=True):
        self.per_page = per_page
        self.pages = pages
        self.mixed = mixed
        self.requested = []

    @staticmethod
    def split(url):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        stars = int(query["stars"][0]) if "stars" in query else None
        page = int(query["page"][0]) if "page" in query else 1
        return stars, page

    def __call__(self, url):
        stars, page = self.split(url)
        self.requested.append((stars, page))
        if page > self.pages:
            return {"status": 200, "meta": META, "reviews": []}
        if page == 1 or stars is None:
            # Measured: the first page ignores the star filter completely.
            return reviews_page(self.per_page, f"new{page}-", mixed=self.mixed)
        return reviews_page(self.per_page, f"s{stars}p{page}-", rating=stars)


class FakeBrowser:
    """A browser that answers from a callable, and records what was asked."""

    pages = {}
    mode = "headful"
    reason = "Xvfb started on :99"
    opened = []
    start_error = None

    def __init__(self):
        pass

    async def start(self):
        return type(self).start_error

    async def close(self):
        return None

    async def open_listing(self, url, wait_s=25.0, nav_timeout_s=45.0):
        type(self).opened.append(url)
        return {"ok": True, "ready": True}

    async def page_data(self, url, timeout_s=25.0):
        for key, answer in type(self).pages.items():
            if key in url:
                return answer(url) if callable(answer) else answer
        return {"status": 200, "noJson": False, "reviews": []}


def run_flow(name, pages, input_data, charging=None, cap=None, explode=False,
             write_then_explode=False):
    FakeActor.reset(input_data, charging=charging, cap=cap, explode=explode,
                    write_then_explode=write_then_explode)
    FakeBrowser.pages = pages
    FakeBrowser.opened = []
    FakeBrowser.start_error = None
    actor_main.Actor = FakeActor
    actor_main.Browser = FakeBrowser
    asyncio.run(actor_main.main())
    summary = FakeActor.values.get("RUN_SUMMARY") or {}
    return FakeActor, summary


def paid_rows(actor):
    return [row for row in actor.dataset.rows if row.get("billable") is True]


def assert_invariant(name, summary) -> None:
    eq(f"{name}: the money invariant holds", summary.get("counterMismatch"), False)
    eq(f"{name}: reviews add up", summary.get("reviewsUnique"),
       summary.get("reviewsDelivered", 0) + summary.get("reviewsFiltered", 0)
       + summary.get("reviewsLimited", 0) + summary.get("reviewsUncertain", 0))
    eq(f"{name}: every page review is accounted for", summary.get("reviewsSeen"),
       summary.get("reviewsUnique", 0) + summary.get("reviewsDuplicates", 0)
       + summary.get("reviewsBucketMismatch", 0) + summary.get("reviewsWithoutId", 0))


def test_flow_paid() -> None:
    """B1 guard: the unfiltered first page does not kill the buckets behind it."""
    listing = MeasuredListing()
    actor, summary = run_flow("paid", {"": listing},
                              {"companies": ["amazon.com"], "maxReviewsPerCompany": 40})
    eq("delivered", summary["reviewsDelivered"], 40)
    eq("charged equals delivered", summary["chargedEvents"], 40)
    eq("rows with a charge", len(paid_rows(actor)), 40)
    eq("buckets were read", summary["starBuckets"], [1, 2, 3, 4, 5])
    eq("one navigation per company", len(FakeBrowser.opened), 1)
    eq("status is OK", summary["byStatus"].get(tp.ST_OK), 40)
    eq("no mismatch", summary["counterMismatch"], False)
    eq("billing active", summary["billingActive"], True)
    eq("header names the money", FakeActor.statuses[-1],
       "1 company, 40 reviews delivered, 40 charged")
    assert_invariant("paid", summary)
    eq("the first page was read without a filter", listing.requested[0], (None, 1))
    check("a bucket never reads its own page 1",
          all(page != 1 for stars, page in listing.requested if stars is not None),
          str(listing.requested[:6]))
    buckets = {row["starBucket"] for row in actor.dataset.rows}
    check("the newest page is named as newest", None in buckets, str(buckets))
    eq("and every bucket row carries its own bucket",
       {row["starBucket"] for row in actor.dataset.rows if row["starSource"] == "star-filter"},
       {1})
    mismatched = [row for row in actor.dataset.rows
                  if row.get("starBucket") is not None
                  and row.get("rating") != row.get("starBucket")]
    eq("no row claims a bucket it does not belong to", mismatched, [])


def test_flow_ceiling() -> None:
    """The measured ceiling with all five buckets: 920 rows, not 1000."""
    listing = MeasuredListing()
    actor, summary = run_flow("ceiling", {"": listing},
                              {"companies": ["amazon.com"], "maxReviewsPerCompany": 1000})
    eq("the request is clamped to the measured ceiling",
       summary["maxReviewsPerCompany"], 920)
    eq("delivered is the measured ceiling", summary["reviewsDelivered"], 920)
    eq("charged equals delivered", summary["chargedEvents"], 920)
    eq("pages read", summary["pagesFetched"], 46)
    eq("every bucket was read to its last page",
       sorted({page for stars, page in listing.requested if stars is not None}),
       [2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert_invariant("ceiling", summary)


def test_flow_single_bucket() -> None:
    """A buyer asking only for one star reviews gets only one star reviews."""
    listing = MeasuredListing()
    actor, summary = run_flow("one-star", {"": listing},
                              {"companies": ["amazon.com"], "stars": [1],
                               "maxReviewsPerCompany": 200})
    eq("no unfiltered page is read for a filtered request",
       [entry for entry in listing.requested if entry[0] is None], [])
    eq("nine filtered pages of 20", summary["pagesFetched"], 9)
    eq("delivered", summary["reviewsDelivered"], 180)
    eq("every row is rating 1", {row["rating"] for row in actor.dataset.rows}, {1})
    eq("every row is bucket 1", {row["starBucket"] for row in actor.dataset.rows}, {1})
    assert_invariant("one star", summary)


def test_flow_cap() -> None:
    listing = MeasuredListing()
    actor, summary = run_flow("cap", {"": listing},
                              {"companies": ["amazon.com"], "maxReviewsPerCompany": 40},
                              cap=25)
    eq("charged up to the cap", summary["chargedEvents"], 25)
    eq("the rest is named", summary["reviewsLimited"], 15)
    eq("and counted as capped", summary["reviewsCapped"], 15)
    eq("stopped at the cap", summary["stopReason"], "spending-cap")
    eq("paid rows equal the charge", len(paid_rows(actor)), 25)
    free = [row for row in actor.dataset.rows if row.get("status") == tp.ST_CAP]
    eq("cap rows are written", len(free), 15)
    eq("cap rows are free", all(row["billable"] is False for row in free), True)
    assert_invariant("cap", summary)


def test_flow_charge_error() -> None:
    listing = MeasuredListing()
    actor, summary = run_flow("error", {"": listing}, {"companies": ["amazon.com"]},
                              explode=True)
    eq("nothing charged", summary["chargedEvents"], 0)
    eq("the doubt is named", summary["chargeUncertain"], 20)
    eq("nothing claimed as delivered", summary["deliveredUncharged"], 0)
    eq("the batch that failed is uncertain, not lost", summary["reviewsUncertain"], 20)
    eq("stopped", summary["stopReason"], "charge-error")
    eq("the reviews behind it are named, not dropped", len(actor.dataset.rows), 180)
    eq("and they say they were read", {row["status"] for row in actor.dataset.rows},
       {tp.ST_ERROR})
    eq("an unknown price state is not a lie about billing", summary["billingActive"], True)
    assert_invariant("charge error", summary)


def test_flow_delivered_uncharged() -> None:
    """M3: rows that landed while the charge failed are counted, not left at zero."""
    listing = MeasuredListing()
    actor, summary = run_flow("uncharged", {"": listing}, {"companies": ["amazon.com"]},
                              write_then_explode=True)
    eq("the run stopped", summary["stopReason"], "charge-error")
    eq("the paid rows are in the dataset", len(paid_rows(actor)), 20)
    eq("and are named as delivered without money", summary["deliveredUncharged"], 20)
    eq("they are not also counted as uncertain", summary["chargeUncertain"], 0)
    eq("nothing was charged", summary["chargedEvents"], 0)
    eq("the rest is named", summary["reviewsLimited"], 180)
    check("the note says what was seen", "without money" in str(summary["datasetNote"]),
          str(summary["datasetNote"]))


def test_flow_deadline_after_read() -> None:
    """M1: a review that was read is never lost when the run runs out of time."""
    listing = MeasuredListing()
    original = tp.deadline_seconds
    values = [40.0, 40.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
    state = {"i": 0}

    def fake_deadline():
        index = min(state["i"], len(values) - 1)
        state["i"] += 1
        return values[index]

    tp.deadline_seconds = fake_deadline
    try:
        actor, summary = run_flow("deadline", {"": listing},
                                  {"companies": ["amazon.com"],
                                   "maxReviewsPerCompany": 100})
    finally:
        tp.deadline_seconds = original
    eq("nothing was charged", summary["chargedEvents"], 0)
    eq("nothing was delivered as OK", summary["reviewsDelivered"], 0)
    eq("but the read reviews are named", summary["reviewsStopped"], 20)
    eq("the dataset is not empty", len(actor.dataset.rows), 20)
    eq("with the reason in plain words",
       {row["status"] for row in actor.dataset.rows}, {tp.ST_TIME})
    check("and a reason a buyer can act on",
          all("time budget" in row["reason"] for row in actor.dataset.rows),
          str(actor.dataset.rows[0]["reason"]))
    eq("the run says it stopped for time", summary["stopReason"], "deadline")
    assert_invariant("deadline", summary)


def test_flow_deadline_before_first_page() -> None:
    """M2: a deadline before the first page is not 'this company has no reviews'."""
    listing = MeasuredListing()
    original = tp.deadline_seconds
    tp.deadline_seconds = lambda: 5.0
    try:
        actor, summary = run_flow("deadline-early", {"": listing},
                                  {"companies": ["amazon.com", "revolut.com"]})
    finally:
        tp.deadline_seconds = original
    eq("no page was fetched at all", summary["pagesFetched"], 0)
    eq("no company is called reviewless", summary["byStatus"].get(tp.ST_NO_REVIEWS), None)
    eq("both companies say the truth", summary["byStatus"].get(tp.ST_TIME), 2)
    eq("nothing charged", summary["chargedEvents"], 0)
    eq("the browser was not even opened", FakeBrowser.opened, [])
    assert_invariant("deadline early", summary)


def test_flow_deadline_before_any_page() -> None:
    """M2: the deadline struck after navigation and before the first page.

    The company must not be called "reviewless": not one page was read, and the buyer
    must see the true reason. This is the exact path from the gate report.
    """
    listing = MeasuredListing()
    original = tp.deadline_seconds
    values = [40.0, 5.0, 5.0, 5.0, 5.0]
    state = {"i": 0}

    def fake_deadline():
        index = min(state["i"], len(values) - 1)
        state["i"] += 1
        return values[index]

    tp.deadline_seconds = fake_deadline
    try:
        actor, summary = run_flow("deadline-before-page", {"": listing},
                                  {"companies": ["amazon.com", "revolut.com"]})
    finally:
        tp.deadline_seconds = original
    eq("not one page was fetched", summary["pagesFetched"], 0)
    eq("no review was seen", summary["reviewsSeen"], 0)
    eq("the company is not called reviewless", summary["byStatus"].get(tp.ST_NO_REVIEWS), None)
    eq("it says the truth", summary["byStatus"].get(tp.ST_TIME), 2)
    eq("nothing charged", summary["chargedEvents"], 0)
    eq("the reason names time", "time budget" in actor.dataset.rows[0]["reason"], True)
    assert_invariant("deadline before page", summary)


def test_flow_unreadable_stars() -> None:
    """A typo in the star filter is refused before a request, not sold as no reviews."""
    listing = MeasuredListing()
    actor, summary = run_flow("bad-stars", {"": listing},
                              {"companies": ["amazon.com", "revolut.com"], "stars": [9]})
    eq("no request was made", FakeBrowser.opened, [])
    eq("no page was read", summary["pagesFetched"], 0)
    eq("no company is called reviewless", summary["byStatus"].get(tp.ST_NO_REVIEWS), None)
    eq("both lines are named", summary["byStatus"].get(tp.ST_INVALID), 2)
    eq("nothing charged", summary["chargedEvents"], 0)
    check("the reason names the filter",
          all("1 to 5" in row["reason"] for row in actor.dataset.rows),
          str([row["reason"] for row in actor.dataset.rows]))
    assert_invariant("bad stars", summary)


def test_flow_not_priced() -> None:
    listing = MeasuredListing()
    actor, summary = run_flow("free", {"": listing}, {"companies": ["amazon.com"]},
                              charging=FakeCharging(FakePricing(ppe=False)))
    eq("rows are delivered free", summary["reviewsDelivered"], 200)
    eq("no money", summary["chargedEvents"], 0)
    eq("billing inactive", summary["billingActive"], False)
    eq("rows say not billable", all(row["billable"] is False for row in actor.dataset.rows),
       True)


def test_flow_unreadable_price() -> None:
    """M5: an unreadable price is null, not a false claim about billing."""
    listing = MeasuredListing()
    actor, summary = run_flow("unknown-price", {"": listing}, {"companies": ["amazon.com"]},
                              charging=FakeCharging(RuntimeError("no price")))
    eq("nothing delivered on an unknown price", summary["reviewsDelivered"], 0)
    eq("nothing charged", summary["chargedEvents"], 0)
    eq("status says why", summary["byStatus"].get(tp.ST_BILLING), 1)
    eq("the price is unknown, not priced", summary["pricingEventPriced"], None)
    eq("billing is unknown, not inactive", summary["billingActive"], None)


def test_flow_walls() -> None:
    cases = {
        "not found": ({"": {"status": 404}}, tp.ST_NOT_FOUND),
        "wall words": ({"": {"status": 200, "noJson": True,
                             "head": "Client Challenge: please enable JavaScript"}},
                       tp.ST_BLOCKED),
        "403": ({"": {"status": 403}}, tp.ST_BLOCKED),
        "no reviews": ({"": reviews_page(0, "z")}, tp.ST_NO_REVIEWS),
        "http 500": ({"": {"status": 500}}, tp.ST_HTTP),
        "fetch failure": ({"": {"fetchError": "TypeError: boom"}}, tp.ST_ERROR),
        "timeout": ({"": {"timeout": True}}, tp.ST_ERROR),
        # Measured 2026-11-11: past page 10 Trustpilot answers its login page,
        # and that page comes WITH __NEXT_DATA__, so it parses as a listing with
        # zero reviews. Selling that as "the company has no reviews" is a lie.
        "login page with json": ({"": {"status": 200, "login": True,
                                      "finalUrl": "https://www.trustpilot.com/users/connect",
                                      "reviews": [],
                                      "meta": {"name": "amazon.com", "total": 50290}}},
                                  tp.ST_BLOCKED),
        "login page without json": ({"": {"status": 200, "login": True,
                                            "noJson": True, "head": "Log in"}},
                                      tp.ST_BLOCKED),
    }
    for name, (pages, expected) in cases.items():
        actor, summary = run_flow(name, pages, {"companies": ["amazon.com"]})
        eq(f"{name}: status", summary["byStatus"].get(expected), 1)
        eq(f"{name}: no money", summary["chargedEvents"], 0)
        eq(f"{name}: one free row", len(actor.dataset.rows), 1)
        eq(f"{name}: never billable", actor.dataset.rows[0]["billable"], False)
        if name.startswith("login page"):
            check(f"{name}: the reason names the login page",
                  "login page" in (actor.dataset.rows[0]["reason"] or ""),
                  str(actor.dataset.rows[0]["reason"]))


def test_login_signal_is_produced() -> None:
    """A branch on a field nobody produces would be dead code that tests cannot see."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "browser.py"),
               encoding="utf-8").read()
    check("the extractor reports the final address", "finalUrl: response.url" in src)
    check("the extractor marks the login page", "/users/connect" in src)


def test_flow_filters() -> None:
    listing = MeasuredListing()
    actor, summary = run_flow("filtered", {"": listing},
                              {"companies": ["amazon.com"], "publishedAfter": "2026-12-01"})
    eq("all filtered out", summary["byStatus"].get(tp.ST_FILTERED), 1)
    eq("no money", summary["chargedEvents"], 0)
    eq("nothing delivered", summary["reviewsDelivered"], 0)
    assert_invariant("filtered", summary)
    listing2 = MeasuredListing()
    actor2, summary2 = run_flow("language", {"": listing2},
                                {"companies": ["amazon.com"], "language": "de"})
    eq("language filter is free too", summary2["byStatus"].get(tp.ST_FILTERED), 1)


def test_flow_dedup() -> None:
    """The same reviews answered twice are sold once."""
    actor, summary = run_flow("dedup", {"": reviews_page(20, "same-")},
                              {"companies": ["amazon.com"], "maxReviewsPerCompany": 100})
    eq("the same ids are not sold twice", summary["reviewsDelivered"], 20)
    check("repeats counted", summary["reviewsDuplicates"] >= 20,
          str(summary["reviewsDuplicates"]))
    assert_invariant("dedup", summary)


def test_flow_bucket_that_lies() -> None:
    """A bucket answering a foreign rating is dropped, never sold with a false bucket."""
    def wrong_rating(url):
        return reviews_page(20, "w-", rating=5)
    actor, summary = run_flow("lying-bucket", {"": wrong_rating},
                              {"companies": ["amazon.com"], "stars": [1],
                               "maxReviewsPerCompany": 200})
    eq("no row was sold", summary["reviewsDelivered"], 0)
    eq("the mismatch is counted", summary["reviewsBucketMismatch"], 20)
    eq("the company is not called reviewless", summary["byStatus"].get(tp.ST_NO_REVIEWS), None)
    eq("and the run says what happened", summary["byStatus"].get(tp.ST_ERROR), 1)
    eq("nothing was charged for a foreign rating", summary["chargedEvents"], 0)
    assert_invariant("lying bucket", summary)


def test_flow_browser_dead() -> None:
    FakeActor.reset({"companies": ["amazon.com"]})
    FakeBrowser.pages = {}
    FakeBrowser.start_error = "no display and no Xvfb in the image"
    actor_main.Actor = FakeActor
    actor_main.Browser = FakeBrowser
    asyncio.run(actor_main.main())
    check("the run says why in plain words", bool(FakeActor.fail_message),
          str(FakeActor.fail_message))
    check("no rows pretend to be data",
          all(r.get("billable") is False for r in FakeActor.dataset.rows))
    eq("no money at all", FakeActor.values.get("RUN_SUMMARY"), None)


def test_flow_limit_and_invalid() -> None:
    listing = MeasuredListing(per_page=3)
    actor, summary = run_flow("mixed", {"": listing},
                              {"companies": ["amazon.com", "bad line", "amazon.com",
                                             "revolut.com"], "maxCompanies": 1,
                               "maxReviewsPerCompany": 3})
    eq("requested", summary["companiesRequested"], 4)
    eq("attempted", summary["companiesAttempted"], 1)
    eq("not attempted", summary["companiesNotAttempted"], 3)
    eq("adds up", summary["companiesRequested"],
       summary["companiesAttempted"] + summary["companiesNotAttempted"])
    eq("delivered", summary["reviewsDelivered"], 3)
    assert_invariant("mixed", summary)


def run_all() -> int:
    tests = [test_normalize, test_urls, test_buckets, test_stars_problem, test_star_plan,
             test_nav_budget, test_blocked, test_review_row, test_review_keys, test_filters,
             test_price, test_plan, test_flow_paid, test_flow_ceiling,
             test_flow_single_bucket, test_flow_cap, test_flow_charge_error,
             test_flow_delivered_uncharged, test_flow_deadline_after_read,
             test_flow_deadline_before_first_page, test_flow_deadline_before_any_page,
             test_flow_unreadable_stars,
             test_flow_not_priced, test_flow_unreadable_price, test_flow_walls, test_login_signal_is_produced,
             test_flow_filters, test_flow_dedup, test_flow_bucket_that_lies,
             test_flow_browser_dead, test_flow_limit_and_invalid]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            CHECKS["failed"].append(f"{test.__name__} raised {type(exc).__name__}: {exc}")
    total = CHECKS["passed"] + len(CHECKS["failed"])
    print(f"checks: {total}, passed: {CHECKS['passed']}, failed: {len(CHECKS['failed'])}")
    for failure in CHECKS["failed"]:
        print("FAIL", failure)
    return 0 if not CHECKS["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
