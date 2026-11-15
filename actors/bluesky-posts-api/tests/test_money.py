"""The money path, through the real main(), against a stand that repeats the SDK.

The lesson this file exists for: a paid actor with no end to end money test let a
blocker reach the gates once already. The stand must repeat the SDK, not our belief:
compute_push_data_limit is keyword only, push_data returns ChargeResult.charged_count,
and Dataset has no set_value.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import main as m  # noqa: E402

PASSED = 0
FAILED = []
CRASHES: list = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


class PriceInfo:
    def __init__(self, ppe: bool = True, prices: dict | None = None) -> None:
        self.is_pay_per_event = ppe
        self.per_event_prices = {"post-delivered": 0.0003} if prices is None else prices


class Charging:
    def __init__(self, budget: int = 100, ppe: bool = True, prices: dict | None = None,
                 capped: bool = False, charged: int | None = None,
                 omit_count: bool = False) -> None:
        self.budget = budget
        self.info = PriceInfo(ppe, prices)
        self.capped = capped
        self.charged = charged
        self.omit_count = omit_count

    def get_pricing_info(self):
        return self.info

    def is_event_charge_limit_reached(self, _name: str) -> bool:
        return self.capped

    def compute_push_data_limit(self, items_count: int, _event: str, *,
                               is_default_dataset: bool) -> int:
        assert is_default_dataset is True
        return min(items_count, self.budget)


class ChargeResult:
    def __init__(self, charged: int | None) -> None:
        self.charged_count = charged


class Dataset:
    def __init__(self, stand: "Stand") -> None:
        self.stand = stand

    async def push_data(self, data):
        if isinstance(data, dict):
            data = [data]
        self.stand.rows.extend(data)

    # deliberately no set_value: Dataset in apify 4.2.x has none


class Store:
    def __init__(self) -> None:
        self.data = {}

    async def set_value(self, key, value):
        self.data[key] = value


class Log:
    def warning(self, *_a, **_k):
        return None

    info = debug = error = warning


class Stand:
    def __init__(self, payload: dict, charging: Charging, fail_push: bool = False) -> None:
        self.payload = payload
        self.charging = charging
        self.fail_push = fail_push
        self.rows: list[dict] = []
        self.wrote_before_failing = 0
        self.summary = None
        self.status = None
        self.failed = None
        self.store = Store()
        self.log = Log()
        self.crashed = ""
        self.dataset_obj = Dataset(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get_input(self):
        return self.payload

    async def open_dataset(self):
        return self.dataset_obj

    def get_charging_manager(self):
        return self.charging

    async def push_data(self, data, charged_event_name=None):
        # The real SDK writes the rows and only then charges them, so a failed charge
        # leaves the rows in the dataset. A stand that raises before the write would
        # hide exactly that class of bug.
        if isinstance(data, dict):
            data = [data]
        self.rows.extend(data)
        if self.fail_push:
            self.wrote_before_failing += len(data)
            raise RuntimeError("the platform refused the charge")
        if self.charging.omit_count:
            return ChargeResult(None)
        charged = self.charging.charged
        return ChargeResult(len(data) if charged is None else charged)

    async def set_value(self, key, value):
        self.store.data[key] = value
        if key == "RUN_SUMMARY":
            self.summary = value

    async def open_key_value_store(self, *_a, **_k):
        return Store()

    async def set_status_message(self, text):
        self.status = text

    async def fail(self, *, status_message="", **_k):
        self.failed = status_message

    def get_env(self):
        return {"run_id": "TESTRUN40"}


FIXTURES = HERE / "fixtures"


def fake_post_json(url: str, timeout: float = 20.0):
    if "getProfile" in url:
        return json.loads((FIXTURES / "profile.json").read_text(encoding="utf-8"))
    return json.loads((FIXTURES / "feed_page.json").read_text(encoding="utf-8"))


def run_filtered_first_page() -> tuple[Stand, dict]:
    """Page 1 carries only reposts, page 2 carries a real post: the run must keep asking."""
    payload = {"actors": ["bsky.app"], "maxPostsPerActor": 5}
    stand = Stand(payload, Charging())
    pages = {"seen": 0}

    def post(url, timeout=20.0):
        if "getProfile" in url:
            return json.loads((FIXTURES / "profile.json").read_text(encoding="utf-8"))
        pages["seen"] += 1
        if pages["seen"] == 1:
            item = {"post": {"uri": "at://did:plc:x/app.bsky.feed.post/r1",
                             "record": {"text": "somebody else's post", "createdAt": "2026-11-12T10:00:00Z"}},
                    "reason": {"$type": "app.bsky.feed.defs#reasonRepost"}}
            return {"cursor": "c1", "feed": [item]}
        item = {"post": {"uri": "at://did:plc:x/app.bsky.feed.post/own1",
                         "record": {"text": "my own post", "createdAt": "2026-11-12T11:00:00Z"}}}
        return {"cursor": None, "feed": [item]}

    m.Actor = stand
    m.bs.post_json = post
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    return stand, pages


def run(main_input: dict, charging: Charging | None = None, fail_push: bool = False,
        feed_error: bool = False) -> Stand:
    stand = Stand(main_input, charging or Charging(), fail_push=fail_push)

    pages = {"seen": 0}
    urls: list[str] = []
    stand.urls = urls

    def post(url, timeout=20.0):
        urls.append(url)
        if "getAuthorFeed" in url:
            if feed_error:
                raise RuntimeError("HTTP 502")
            pages["seen"] += 1
            if pages["seen"] > 1:
                return {"cursor": None, "feed": []}
        return fake_post_json(url, timeout)

    m.Actor = stand
    m.bs.post_json = post
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001 - a crash must fail a named check below
        stand.crashed = f"{type(exc).__name__}: {exc}"
    CRASHES.append(stand)
    return stand


def run_all() -> int:
    payload = {"actors": ["bsky.app"], "maxPostsPerActor": 3, "includeReposts": True}
    # the captured page has one own post and two reposts, so reposts are asked for here

    # 1. normal: three real posts, three charges
    s = run(payload)
    ok_rows = [r for r in s.rows if r.get("status") == "OK"]
    limit_rows = [r for r in s.rows if r.get("status") == "NOT_CHECKED_LIMIT"]
    check("normal: three sold rows", len(ok_rows) == 3)
    check("normal: the cut limit gets its own free row", len(limit_rows) == 1)
    check("normal: the limit row carries no product",
          all(not r.get("postId") and not r.get("text") and not r.get("url") for r in limit_rows))
    check("normal: the limit row is free", all(r.get("billable") is False for r in limit_rows))
    check("normal: three charged", (s.summary or {}).get("chargedEvents") == 3)
    check("normal: delivered three", (s.summary or {}).get("postsDelivered") == 3)
    check("normal: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("normal: stop reason empty", (s.summary or {}).get("stopReason") == "")
    check("normal: summary written to the run store", "RUN_SUMMARY" in s.store.data)

    # 1b. reposts are off by default: one own post paid, two reposts filtered, not charged
    s = run({"actors": ["bsky.app"], "maxPostsPerActor": 3})
    check("default: reposts filtered", (s.summary or {}).get("postsFilteredByKind") == 2)
    check("default: one own post delivered", len(s.rows) == 1)
    check("default: one charged only", (s.summary or {}).get("chargedEvents") == 1)

    # 2. budget 1 of 3: one sold, two free with a reason
    s = run(payload, Charging(budget=1))
    check("budget: charged one", (s.summary or {}).get("chargedEvents") == 1)
    check("budget: stop is spending-cap", (s.summary or {}).get("stopReason") == "spending-cap")
    cap = [r for r in s.rows if r.get("status") == "NOT_CHECKED_CAP"]
    check("budget: two free cap rows", len(cap) == 2)
    check("budget: cap rows carry no product",
          all(not r.get("postId") and not r.get("text") and not r.get("url") for r in cap))

    # 3. the charge result says nothing: delivered, not claimed as sold
    s = run(payload, Charging(charged=0))
    s2 = run(payload, Charging(omit_count=True))
    check("unverified: no sale claimed", (s2.summary or {}).get("chargedEvents") == 0)
    check("unverified: counted as delivered uncharged",
          (s2.summary or {}).get("deliveredUncharged") == 3)
    check("unverified: stop is charge-error", (s2.summary or {}).get("stopReason") == "charge-error")

    # 4. the charge throws: rows stay, no second row, run stops
    s = run(payload, fail_push=True)
    sold = [r for r in s.rows if r.get("postId")]
    check("charge error: the written rows stay in the dataset", len(sold) == 3)
    check("charge error: no duplicate rows", len({r.get("postId") for r in sold}) == 3)
    check("charge error: the summary counts them as delivered",
          (s.summary or {}).get("postsDelivered") == 3)
    check("charge error: nothing is claimed as sold", (s.summary or {}).get("chargedEvents") == 0)
    check("charge error: they are counted as delivered and uncharged",
          (s.summary or {}).get("deliveredUncharged") == 3)
    check("charge error: billable is unknown in those rows",
          all(r.get("billable") is None for r in sold))
    check("charge error: stop is charge-error", (s.summary or {}).get("stopReason") == "charge-error")
    check("charge error: uncertainty counted", (s.summary or {}).get("chargeUncertain") == 3)

    # 4b. the same profile named twice (handle and did) is one read, one set of rows
    same = {"actors": ["bsky.app", "did:plc:z72i7hdynmk6r22z27h6tvur"],
            "maxPostsPerActor": 3, "includeReposts": True}
    s = run(same)
    dup = [r for r in s.rows if r.get("status") == "DUPLICATE"]
    check("dedup: handle and did are one profile", len(dup) == 1)
    check("dedup: counted as a duplicate", (s.summary or {}).get("actorsDuplicate") == 1)
    check("dedup: the profile was read once", (s.summary or {}).get("actorsRead") == 1)
    check("dedup: the duplicate row is free and carries no product",
          all(not r.get("postId") and not r.get("text") and r.get("billable") is False
              for r in dup))
    check("dedup: three sold rows, not six",
          len([r for r in s.rows if r.get("status") == "OK"]) == 3)

    # 4c. includeReplies must reach the query: the source drops replies itself, so a flag
    # that only filters rows downstream is a dead switch
    replies_on = dict(payload, includeReplies=True)
    s_on = run(replies_on)
    feed_urls = [u for u in s_on.urls if "getAuthorFeed" in u]
    check("replies on: the source was asked for replies",
          bool(feed_urls) and all("filter=posts_with_replies" in u for u in feed_urls))
    s_off = run(payload)
    off_urls = [u for u in s_off.urls if "getAuthorFeed" in u]
    check("replies off: the source was asked for posts only",
          bool(off_urls) and all("filter=posts_no_replies" in u for u in off_urls))

    # 5. not sold per event: delivered free and said so
    s = run(payload, Charging(ppe=False))
    check("not ppe: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not ppe: rows delivered free", (s.summary or {}).get("deliveredUncharged") == 3)
    check("not ppe: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("not ppe: billable is false in the row", all(r.get("billable") is False for r in s.rows))

    # 5b. page 1 all filtered, page 2 has a post: no false "no posts found"
    s3, pages3 = run_filtered_first_page()
    check("filtered page: the second page was asked for", pages3["seen"] == 2)
    check("filtered page: the real post was delivered",
          len([r for r in s3.rows if r.get("status") == "OK"]) == 1)
    check("filtered page: no false empty profile",
          not [r for r in s3.rows if r.get("status") == "NO_POSTS_FOUND"])
    check("filtered page: the repost is counted as filtered",
          (s3.summary or {}).get("postsFilteredByKind") == 1)

    # 6. the price cannot be read: nothing delivered, nothing charged
    s = run(payload, Charging(prices={}))
    paid = [r for r in s.rows if r.get("status") == "OK"]
    check("price unknown: no paid rows", paid == [])
    check("price unknown: stop names the price",
          (s.summary or {}).get("stopReason") == "billing-price-unknown")
    check("price unknown: billingActive is null", (s.summary or {}).get("billingActive") is None)

    # 7. empty input: a human refusal, no rows, nothing charged
    s = run({"actors": ["   ", ""]})
    check("empty input: failed with a message", bool(s.failed))
    check("empty input: no rows", s.rows == [])

    # 8. the same profile twice: one table, one duplicate row
    s = run({"actors": ["bsky.app", "https://bsky.app/profile/bsky.app"],
             "maxPostsPerActor": 3, "includeReposts": True})
    check("duplicate: duplicate row present",
          any(r.get("status") == "DUPLICATE" for r in s.rows))
    check("duplicate: charged once only", (s.summary or {}).get("chargedEvents") == 3)

    # 9. a word that is not a profile: refused before any request
    s = run({"actors": ["hello"]})
    check("invalid: refused", any(r.get("status") == "INVALID_INPUT" for r in s.rows))
    check("invalid: nothing charged", (s.summary or {}).get("chargedEvents") == 0)

    # 10. the feed does not answer: a free row with the reason
    s = run(payload, feed_error=True)
    err = [r for r in s.rows if r.get("status") == "NOT_CHECKED_ERROR"]
    check("feed error: free error row", len(err) == 1)
    check("feed error: reason names the failure", "502" in str(err[0].get("reason", "")) if err else False)
    check("feed error: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("feed error: pageErrors listed", bool((s.summary or {}).get("pageErrors")))

    # a scenario that crashed is a named failure, never a silent traceback
    for name in sorted({s.crashed for s in CRASHES if s.crashed}):
        FAILED.append("a scenario crashed instead of a named check: " + name)

    print(f"money checks: {PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print("  FAILED:", name)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(run_all())
