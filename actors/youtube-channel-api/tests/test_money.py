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
        info.per_event_prices = {M.EVENT_NAME: 0.0002} if self.priced else {}
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
    """An instance, not a class: `async with Actor:` needs real dunder methods."""

    log = Log()

    def __init__(self, input_data, charging, lag=0):
        self.input_data = input_data
        self.charging = charging
        self.dataset = Dataset(lag=lag)
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


def resolve(cid="UC123"):
    return {"endpoint": {"browseEndpoint": {"browseId": cid}}, "responseContext": {}}


def home(title="Channel", with_videos=True):
    tabs = []
    if with_videos:
        tabs.append({"tabRenderer": {"title": "Videos",
                                     "endpoint": {"browseEndpoint": {"params": "V1"}}}})
    tabs.append({"tabRenderer": {"title": "Shorts",
                                 "endpoint": {"browseEndpoint": {"params": "S1"}}}})
    return {"metadata": {"channelMetadataRenderer": {"title": title}},
            "header": {"subscriberCountText": {"simpleText": "1.2M subscribers"}},
            "contents": {"twoColumnBrowseResultsRenderer": {"tabs": tabs}}}


def short(n):
    return {"contentId": "vid-%s" % n,
            "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
            "metadata": {"lockupMetadataViewModel": {
                "title": {"content": "Video %s" % n},
                "metadata": {"contentMetadataViewModel": {"metadataRows": [
                    {"metadataParts": [{"text": {"content": "%sK views" % n}},
                                       {"text": {"content": "2 days ago"}}]}]}}}},
            "contentImage": {"thumbnailViewModel": {"image": {"sources": [
                {"url": "https://i.ytimg.com/vi/vid-%s/hq720.jpg" % n}]}}}}


def spage(ids, token=None):
    items = [{"richItemRenderer": {"content": {"lockupViewModel": short(i)}}} for i in ids]
    payload = {"contents": {"richGridRenderer": {"contents": list(items)}}}
    if token:
        payload["contents"]["richGridRenderer"]["contents"].append(
            {"continuationItemRenderer": {"continuationEndpoint": {
                "continuationCommand": {"token": token}}}})
    return payload


class Post:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def __call__(self, body, timeout=30.0):
        self.calls += 1
        if not self.payloads:
            return {"contents": {}}
        item = self.payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


BASE = {"channels": ["@chan"], "maxVideosPerChannel": 50}


def run(input_data, charging, payloads, lag=0):
    actor = ActorStub(input_data, charging, lag=lag)
    M.Actor = actor
    poster = Post(payloads)
    M.ch.post = poster
    asyncio.run(M.main())
    return actor, actor.dataset, poster


@case
def test_an_empty_input_is_a_refusal_not_a_green_empty_run():
    actor, dataset, poster = run({"channels": ["   ", ""]}, Charging(), [])
    assert actor.failed, "the run did not refuse an empty input"
    assert dataset.items == [], len(dataset.items)
    assert poster.calls == 0, "a request was made for an empty input"


@case
def test_a_bad_region_is_refused_before_the_first_request():
    actor, dataset, poster = run(dict(BASE, region="USA"), Charging(), [])
    assert actor.failed and "region" in actor.failed, actor.failed
    assert poster.calls == 0, poster.calls


@case
def test_a_normal_run_sells_every_row_it_delivered():
    actor, dataset, poster = run(BASE, Charging(),
                                 [resolve(), home(), spage(["a", "b", "c"])])
    rows = dataset.items
    assert len(rows) == 3, len(rows)
    assert all(r["status"] == "OK" and r["billable"] is True for r in rows), rows
    assert actor.summary["chargedEvents"] == 3, actor.summary["chargedEvents"]
    assert actor.summary["videosDelivered"] == 3
    assert actor.summary["counterMismatch"] is False
    assert actor.summary["stopReason"] == ""


@case
def test_paging_follows_the_token_and_stops_at_the_limit():
    actor, dataset, poster = run(dict(BASE, maxVideosPerChannel=5), Charging(),
                                 [resolve(), home(), spage(["a", "b", "c"], token="T1"),
                                  spage(["c", "d", "e", "f"])])
    rows = dataset.items
    assert len(rows) == 5, len(rows)
    assert actor.summary["pagesRead"] == 2, actor.summary["pagesRead"]
    assert actor.summary["videosDuplicate"] == 1, actor.summary["videosDuplicate"]
    assert poster.calls == 4, poster.calls


@case
def test_the_spending_cap_cuts_the_batch_before_the_write():
    actor, dataset, _ = run(dict(BASE, channels=["@a"]), Charging(budget=2),
                            [resolve(), home(), spage(["a", "b", "c", "d", "e"])])
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
    actor, dataset, _ = run(BASE, Charging(priced="unreadable"),
                            [resolve(), home(), spage(["a", "b"])])
    rows = dataset.items
    assert all(r["status"] == "NOT_CHECKED_BILLING" for r in rows), [r["status"] for r in rows]
    assert all(r["billable"] is False for r in rows)
    assert actor.summary["chargedEvents"] == 0
    assert actor.summary["stopReason"] == "billing-price-unknown", actor.summary["stopReason"]


@case
def test_a_charge_that_does_not_report_a_count_is_never_a_sale():
    actor, dataset, _ = run(BASE, Charging(report="missing"),
                            [resolve(), home(), spage(["a", "b"])])
    assert actor.summary["chargedEvents"] == 0, actor.summary["chargedEvents"]
    assert actor.summary["deliveredUncharged"] == 2, actor.summary["deliveredUncharged"]
    assert actor.summary["stopReason"] == "charge-error", actor.summary["stopReason"]


@case
def test_a_channel_without_a_videos_tab_costs_nothing():
    actor, dataset, _ = run(BASE, Charging(), [resolve(), home(with_videos=False)])
    rows = dataset.items
    assert len(rows) == 1 and rows[0]["status"] == "NO_VIDEOS_FOUND", rows
    assert rows[0]["videoId"] is None and rows[0]["billable"] is False
    assert actor.summary["channelsNoVideos"] == 1, actor.summary["channelsNoVideos"]
    assert actor.summary["chargedEvents"] == 0


@case
def test_a_channel_that_cannot_be_resolved_costs_nothing():
    actor, dataset, _ = run(BASE, Charging(), [{"responseContext": {}}])
    rows = dataset.items
    assert len(rows) == 1 and rows[0]["status"] == "INVALID_INPUT", rows
    assert actor.summary["channelsInvalid"] == 1, actor.summary["channelsInvalid"]


@case
def test_the_same_channel_twice_is_charged_once():
    actor, dataset, _ = run(dict(BASE, channels=["@a", "@A"]), Charging(),
                            [resolve(), home(), spage(["a", "b"])])
    rows = [r for r in dataset.items if r["status"] == "OK"]
    assert len(rows) == 2, len(rows)
    assert actor.summary["channelsDuplicate"] == 1, actor.summary["channelsDuplicate"]
    assert actor.summary["chargedEvents"] == 2


@case
def test_a_broken_page_names_the_reason_and_charges_nothing():
    actor, dataset, _ = run(BASE, Charging(),
                            [resolve(), home(), RuntimeError("connection reset")])
    rows = dataset.items
    assert any(r["status"] == "NOT_CHECKED_ERROR" and "connection reset" in (r.get("reason") or "")
               for r in rows), rows
    assert actor.summary["chargedEvents"] == 0
    assert actor.summary["pagesFailed"] == 1, actor.summary["pagesFailed"]


@case
def test_the_summary_lands_where_the_schema_points():
    actor, _dataset, _ = run(BASE, Charging(), [resolve(), home(), spage(["a"])])
    assert actor.stores.get("RUN_SUMMARY"), "the bare summary key is missing"
    assert actor.stores["RUN_SUMMARY"]["videosDelivered"] == 1


@case
def test_an_entry_without_an_id_is_skipped_and_not_sold():
    broken = {"richItemRenderer": {"content": {"lockupViewModel": {"contentId": "",
                                                                  "contentType": "LOCKUP_CONTENT_TYPE_VIDEO"}}}}
    payload = spage(["a"])
    payload["contents"]["richGridRenderer"]["contents"].insert(0, broken)
    actor, dataset, _ = run(BASE, Charging(), [resolve(), home(), payload])
    assert len([r for r in dataset.items if r["status"] == "OK"]) == 1
    assert actor.summary["chargedEvents"] == 1


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
