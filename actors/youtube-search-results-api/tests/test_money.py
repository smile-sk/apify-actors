"""Money-path checks: a run must never claim a sale the SDK did not charge for.

Run: python tests/test_money.py   (exit code 0 means every check ran)

The stand repeats the real SDK, not my belief about it: Actor.push_data asks
ChargingManager.compute_push_data_limit for the remaining budget, writes only that
prefix and charges only it, and returns ChargeResult with charged_count. A fake that is
kinder than the SDK hides the defect it should catch.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import main as M  # noqa: E402

CHECKS = []


def case(fn):
    CHECKS.append((fn.__name__, fn))
    return fn


class ChargeResult:
    def __init__(self, charged_count, limit_reached=False):
        self.charged_count = charged_count
        self.event_charge_limit_reached = limit_reached


class Dataset:
    def __init__(self, lag=0):
        self.items = []
        self.lag = lag

    async def push_data(self, data):
        data = data if isinstance(data, list) else [data]
        self.items.extend(data)

    async def get_metadata(self):
        class Meta:
            pass
        meta = Meta()
        meta.item_count = max(0, len(self.items) - self.lag)
        return meta


class Store:
    def __init__(self):
        self.values = {}

    async def set_value(self, key, value):
        self.values[key] = value


class Log:
    def warning(self, *args, **kwargs):
        pass
    def info(self, *args, **kwargs):
        pass


class Charging:
    def __init__(self, budget=None, short_by=0, report=None, priced=True):
        self.budget = budget
        self.charged = 0
        self.short_by = short_by
        self.report = report
        self.priced = priced

    def get_pricing_info(self):
        class Info:
            pass
        if self.priced == "unreadable":
            return None
        info = Info()
        info.is_pay_per_event = True
        info.per_event_prices = {M.EVENT_NAME: 0.0004} if self.priced else {}
        return info

    def is_event_charge_limit_reached(self, event_name):
        return self.budget is not None and self.budget - self.charged < 0

    def compute_push_data_limit(self, items_count, event_name, is_default_dataset=True):
        if self.budget is None:
            return items_count
        left = self.budget - self.charged
        return max(0, min(items_count, left))

    def charge(self, batch):
        limit = self.compute_push_data_limit(len(batch), M.EVENT_NAME)
        if self.report == "missing":
            self.charged += limit
            return None
        charged = max(0, limit - self.short_by)
        self.charged += charged
        return charged


class ActorStub:
    """An instance, not a class: `async with Actor:` needs real dunder methods.

    A class-level fake cannot be used as a context manager, and a fake that cannot be
    used the way the SDK is used does not test the run at all.
    """

    log = Log()

    def __init__(self, input_data, charging, lag=0):
        self.input_data = input_data
        self.charging = charging
        self.dataset = Dataset(lag=lag)
        self.pushed = []
        self.env = {}
        self.failed = None
        self.status = None
        self.summary = None
        self.stores = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get_input(self):
        return self.input_data

    async def open_dataset(self):
        return self.dataset

    def get_charging_manager(self):
        return self.charging

    async def push_data(self, data, charged_event_name=None):
        data = data if isinstance(data, list) else [data]
        charged = self.charging.charge(data)
        limit = charged if charged is not None else len(data)
        await self.dataset.push_data(data[:limit])
        self.pushed.append((len(data), charged))
        return ChargeResult(charged)

    async def set_value(self, key, value):
        self.summary = value
        self.stores[key] = value

    async def set_status_message(self, message):
        self.status = message

    async def open_key_value_store(self, name=None):
        return Store()

    def get_env(self):
        return self.env

    async def fail(self, status_message=None, **kwargs):
        self.failed = status_message


def video(n):
    return {"videoId": "vid%03d" % n, "title": {"runs": [{"text": "Video %d" % n}]},
            "ownerText": {"runs": [{"text": "Channel", "navigationEndpoint": {"browseEndpoint": {"browseId": "UC1"}}}]},
            "viewCountText": {"simpleText": "1,000 views"},
            "publishedTimeText": {"simpleText": "2 days ago"},
            "lengthText": {"simpleText": "9:41"}}


def page(indices, token=None):
    payload = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {"contents": [
        {"itemSectionRenderer": {"contents": [{"videoRenderer": video(i)} for i in indices]}}]}}}}}
    if token:
        payload["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"].append(
            {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": token}}}})
    return payload


class Post:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def __call__(self, body, timeout=30.0):
        self.calls += 1
        if not self.pages:
            return page([])
        item = self.pages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run(input_data, charging, pages, lag=0):
    actor = ActorStub(input_data, charging, lag=lag)
    M.Actor = actor
    poster = Post(pages)
    M.yts.post = poster
    asyncio.run(M.main())
    return actor, actor.dataset, poster


BASE = {"queries": ["python tutorial"], "maxResultsPerQuery": 50}


@case
def test_an_empty_input_is_a_refusal_not_a_green_empty_run():
    actor, dataset, poster = run({"queries": ["   ", ""]}, Charging(), [])
    assert actor.failed, "the run did not refuse an empty input"
    assert dataset.items == [], len(dataset.items)
    assert poster.calls == 0, "a request was made for an empty input"


@case
def test_an_unknown_filter_is_refused_before_the_first_request():
    data = dict(BASE, sortBy="date")
    actor, dataset, poster = run(data, Charging(), [])
    assert actor.failed and "sortBy" in actor.failed, actor.failed
    assert poster.calls == 0, poster.calls
    assert dataset.items == []


@case
def test_a_normal_run_sells_every_row_it_delivered():
    actor, dataset, poster = run(BASE, Charging(), [page([1, 2, 3])])
    rows = dataset.items
    assert len(rows) == 3, len(rows)
    assert all(r["status"] == "OK" and r["billable"] is True for r in rows), rows
    assert actor.summary["chargedEvents"] == 3, actor.summary["chargedEvents"]
    assert actor.summary["videosDelivered"] == 3
    assert actor.summary["counterMismatch"] is False
    assert actor.summary["stopReason"] == ""


@case
def test_paging_follows_the_token_and_stops_at_the_limit():
    actor, dataset, poster = run(dict(BASE, maxResultsPerQuery=5), Charging(),
                                 [page([1, 2, 3], token="T1"), page([3, 4, 5, 6])])
    rows = dataset.items
    assert len(rows) == 5, len(rows)
    assert poster.calls == 2, poster.calls
    assert actor.summary["pagesRead"] == 2
    assert actor.summary["videosDuplicate"] == 1, actor.summary["videosDuplicate"]


@case
def test_the_spending_cap_cuts_the_batch_before_the_write():
    actor, dataset, _ = run(dict(BASE, queries=["q1"]), Charging(budget=2), [page([1, 2, 3, 4, 5])])
    rows = dataset.items
    paid = [r for r in rows if r["status"] == "OK"]
    capped = [r for r in rows if r["status"] == "NOT_CHECKED_CAP"]
    assert len(paid) == 2, len(paid)
    assert len(capped) == 3, len(capped)
    assert all(r["videoId"] is None and r["billable"] is False for r in capped), capped[0]
    assert actor.summary["chargedEvents"] == 2, actor.summary["chargedEvents"]
    assert actor.summary["stopReason"] == "spending-cap", actor.summary["stopReason"]


@case
def test_an_unreadable_price_sells_nothing_and_says_why():
    actor, dataset, _ = run(BASE, Charging(priced="unreadable"), [page([1, 2, 3])])
    rows = dataset.items
    assert all(r["status"] == "NOT_CHECKED_BILLING" for r in rows), [r["status"] for r in rows]
    assert all(r["billable"] is False for r in rows)
    assert actor.summary["chargedEvents"] == 0
    assert actor.summary["stopReason"] == "billing-price-unknown", actor.summary["stopReason"]


@case
def test_a_charge_that_does_not_report_counts_is_never_a_sale():
    actor, dataset, _ = run(BASE, Charging(report="missing"), [page([1, 2, 3])])
    assert actor.summary["chargedEvents"] == 0, actor.summary["chargedEvents"]
    assert actor.summary["deliveredUncharged"] == 3, actor.summary["deliveredUncharged"]
    assert actor.summary["stopReason"] == "charge-error", actor.summary["stopReason"]


@case
def test_a_budget_cut_inside_the_batch_frees_the_tail():
    actor, dataset, _ = run(BASE, Charging(budget=10, short_by=3), [page([1, 2, 3, 4, 5])])
    rows = dataset.items
    paid = [r for r in rows if r["status"] == "OK"]
    capped = [r for r in rows if r["status"] == "NOT_CHECKED_CAP"]
    assert len(paid) == 2, len(paid)
    assert len(capped) == 3, len(capped)
    assert actor.summary["chargedEvents"] == 2
    assert actor.summary["stopReason"] == "spending-cap"


@case
def test_a_failed_page_names_the_reason_and_charges_nothing():
    actor, dataset, _ = run(BASE, Charging(), [Exception("URLError: getaddrinfo failed")])
    rows = dataset.items
    assert len(rows) == 1, len(rows)
    assert rows[0]["status"] == "NOT_CHECKED_ERROR", rows[0]["status"]
    assert "getaddrinfo" in rows[0]["reason"], rows[0]["reason"]
    assert actor.summary["chargedEvents"] == 0
    assert actor.summary["pagesFailed"] == 1


@case
def test_a_phrase_with_no_videos_gets_a_free_row():
    actor, dataset, _ = run(BASE, Charging(), [page([])])
    rows = dataset.items
    assert len(rows) == 1, len(rows)
    assert rows[0]["status"] == "NO_RESULTS_FOUND", rows[0]["status"]
    assert rows[0]["billable"] is False
    assert actor.summary["chargedEvents"] == 0


@case
def test_the_same_phrase_twice_is_charged_once():
    actor, dataset, poster = run(dict(BASE, queries=["python", "python"]), Charging(), [page([1, 2])])
    rows = dataset.items
    ok = [r for r in rows if r["status"] == "OK"]
    dup = [r for r in rows if r["status"] == "DUPLICATE"]
    assert len(ok) == 2, len(ok)
    assert len(dup) == 1, len(dup)
    assert dup[0]["billable"] is False
    assert poster.calls == 1, poster.calls


@case
def test_the_summary_lands_where_the_schema_points():
    actor, dataset, _ = run(BASE, Charging(), [page([1, 2])])
    assert isinstance(actor.summary, dict), actor.summary
    assert actor.summary["priceEvent"] == M.EVENT_NAME
    assert "chargedEvents" in actor.summary and "byStatus" in actor.summary


@case
def test_a_phrase_after_the_money_stop_is_not_silently_searched():
    actor, dataset, poster = run(dict(BASE, queries=["q1", "q2"]), Charging(budget=1),
                                 [page([1, 2, 3])])
    rows = dataset.items
    cut = [r for r in rows if r["status"] == "NOT_CHECKED_CAP" and r.get("query") == "q2"]
    assert cut, [r.get("query") for r in rows]
    assert cut[0]["videoId"] is None
    assert poster.calls == 1, poster.calls


@case
def test_the_shorts_shelf_is_counted_in_the_summary():
    """Shorts never become rows, so the summary must say how many were skipped.

    Measured 2026-11-12: a search for "cats" carried 40 Shorts beside 9 videos.
    Without this counter the buyer reads "nothing found" while half a page was
    dropped, which is the silent-loss class this actor exists to avoid.
    """
    payload = page([1, 2])
    shelf = {"itemSectionRenderer": {"contents": [{"shelfRenderer": {"content": {
        "horizontalListRenderer": {"items": [
            {"shortsLockupViewModel": {"entityId": "shorts-shelf-item-s1"}},
            {"shortsLockupViewModel": {"entityId": "shorts-shelf-item-s2"}}]}}}}]}}
    payload["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"].insert(0, shelf)
    actor, dataset, _ = run(BASE, Charging(), [payload])
    assert actor.summary["shortsSkipped"] == 2, actor.summary["shortsSkipped"]
    assert len(dataset.items) == 2, len(dataset.items)
    assert all(r["status"] == "OK" for r in dataset.items)


def run_all() -> int:
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
            print("ok   %s" % name)
        except AssertionError as exc:
            failures += 1
            print("FAIL %s -> %s" % (name, exc))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print("ERR  %s -> %s: %s" % (name, type(exc).__name__, exc))
    print("\n%d checks, %d failed" % (len(CHECKS), failures))
    return failures


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
