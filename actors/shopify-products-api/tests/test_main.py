"""Checks for the run loop: what changes, what is dropped, and where the money is."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json

from src import main, shopify


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, *args):
        self.lines.append(("info", args))

    def warning(self, *args):
        self.lines.append(("warning", args))

    def error(self, *args):
        self.lines.append(("error", args))


class FakeChargeResult:
    def __init__(self, charged=0, cap=False):
        self.charged_count = charged
        self.event_charge_limit_reached = cap


class FakeDataset:
    def __init__(self, owner):
        self.owner = owner

    async def get_metadata(self):
        return {"item_count": len(self.owner.rows)}

    async def get_info(self):
        return {"item_count": len(self.owner.rows)}


class FakeChargingManager:
    def __init__(self, is_ppe=True, prices=None):
        self._is_ppe = is_ppe
        self._prices = {"product-delivered": 0.002} if prices is None else prices

    def get_pricing_info(self):
        class Info:
            pass
        info = Info()
        info.is_pay_per_event = self._is_ppe
        info.per_event_prices = self._prices
        return info


class FakeActor:
    """A stand-in that repeats the SDK contract, not my belief about it."""

    def __init__(self, mode="normal", charging=None):
        self.mode = mode
        self.rows = []
        self.calls = []
        self.charging = charging or FakeChargingManager()

    async def push_data(self, row, charged_event_name=None):
        self.calls.append((row.get("handle"), charged_event_name))
        if self.mode == "raise_after_write":
            self.rows.append(row)
            raise RuntimeError("charge failed after the row was written")
        if self.mode == "raise_before_write":
            raise RuntimeError("dataset refused the row")
        if self.mode == "cap":
            # measured in apify 4.2.0: when the cap lets nothing through, nothing is written
            return FakeChargeResult(0, True)
        self.rows.append(row)
        if self.mode == "free":
            return FakeChargeResult(0, False)
        return FakeChargeResult(1, False)

    async def open_dataset(self):
        return FakeDataset(self)

    def get_charging_manager(self):
        return self.charging


def _summary():
    return {"chargedEvents": 0, "deliveredUncharged": 0, "chargeUncertain": 0,
            "writeFailures": 0}


def _with_actor(fake):
    original = main.Actor
    main.Actor = fake
    return original


# --- what counts as a change -----------------------------------------------------------

def test_change_kind():
    assert main.change_kind(None, {"price": 10, "availability": "in_stock"}) == ("new", None)
    assert main.change_kind({"price": 10, "availability": "in_stock"},
                            {"price": 9, "availability": "in_stock"}) == ("price-down", 10)
    assert main.change_kind({"price": 10, "availability": "in_stock"},
                            {"price": 12, "availability": "in_stock"}) == ("price-up", 10)
    assert main.change_kind({"price": 10, "availability": "in_stock"},
                            {"price": 10, "availability": "out_of_stock"}) == ("out-of-stock", None)
    assert main.change_kind({"price": 10, "availability": "out_of_stock"},
                            {"price": 10, "availability": "in_stock"}) == ("back-in-stock", None)
    assert main.change_kind({"price": 10, "availability": "in_stock"},
                            {"price": 10, "availability": "in_stock"}) == (None, None)
    # stock outranks price: one string per row cannot carry both honestly
    assert main.change_kind({"price": 10, "availability": "in_stock"},
                            {"price": 8, "availability": "out_of_stock"}) == ("out-of-stock", None)
    # an unknown price on either side is not a price change
    assert main.change_kind({"price": None, "availability": "in_stock"},
                            {"price": 8, "availability": "in_stock"}) == (None, None)
    return "change_kind: new, both prices, both stock sides, unchanged, unknown price"


def test_filter_reason():
    settings = {"in_stock_only": False, "min_price": 0.0, "max_price": 0.0, "name_contains": ""}
    row = {"price": 50.0, "availability": "in_stock", "title": "Wool Runner"}
    assert main.filter_reason(row, settings) is None
    assert main.filter_reason(row, dict(settings, min_price=60.0)) == "minPrice"
    assert main.filter_reason(row, dict(settings, max_price=40.0)) == "maxPrice"
    assert main.filter_reason(row, dict(settings, name_contains="wool")) is None
    assert main.filter_reason(row, dict(settings, name_contains="sock")) == "nameContains"
    assert main.filter_reason(dict(row, availability="out_of_stock"),
                              dict(settings, in_stock_only=True)) == "inStockOnly"
    # a product with no price is not dropped by a price filter: it is NO_PRICE, not silent
    assert main.filter_reason(dict(row, price=None), dict(settings, min_price=100.0)) is None
    return "filter_reason: price bounds, title match, stock, and no-price kept for NO_PRICE"


# --- the money path --------------------------------------------------------------------

def test_push_charges_once_per_row():
    async def run():
        fake = FakeActor()
        original = _with_actor(fake)
        try:
            summary = _summary()
            delivered, cap, outcome = await main._push(
                {"handle": "a", "status": "DELIVERED"}, summary, FakeLog(), True, {"written": 0})
            assert (delivered, cap, outcome) == (True, False, "charged")
            assert summary["chargedEvents"] == 1 and len(fake.rows) == 1
            assert fake.calls == [("a", "product-delivered")], fake.calls
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_push: one row, one charge, the event name is passed to the SDK"


def test_push_at_cap_writes_nothing_and_is_not_uncharged():
    async def run():
        fake = FakeActor(mode="cap")
        original = _with_actor(fake)
        try:
            summary = _summary()
            delivered, cap, outcome = await main._push(
                {"handle": "a", "status": "DELIVERED"}, summary, FakeLog(), True, {"written": 0})
            assert (delivered, cap, outcome) == (False, True, "free"), (delivered, cap, outcome)
            assert fake.rows == [], "the SDK writes nothing when the cap lets nothing through"
            assert summary["deliveredUncharged"] == 0, summary
            assert summary["chargedEvents"] == 0
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_push: at the spending cap nothing is written and it is not counted as free"


def test_push_does_not_duplicate_a_row_written_before_a_charge_failure():
    async def run():
        fake = FakeActor(mode="raise_after_write")
        original = _with_actor(fake)
        try:
            summary = _summary()
            delivered, cap, outcome = await main._push(
                {"handle": "a", "status": "DELIVERED"}, summary, FakeLog(), True, {"written": 0})
            assert delivered is True and outcome == "free", (delivered, outcome)
            assert len(fake.rows) == 1, "a second push would put the same product twice"
            assert summary["deliveredUncharged"] == 1 and summary["chargeUncertain"] == 1, summary
            assert len(fake.calls) == 1, fake.calls
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_push: charge failure after the write is delivered and uncertain, never duplicated"


def test_push_calls_an_unconfirmed_row_unverified():
    """Orestes, 07.11: dataset metadata lags the write, so a counter that did not move cannot
    prove the row is missing. Calling it lost while the buyer sees the row was a lie."""
    async def run():
        fake = FakeActor(mode="raise_before_write")
        original = _with_actor(fake)
        try:
            summary = _summary()
            delivered, cap, outcome = await main._push(
                {"handle": "a", "status": "DELIVERED"}, summary, FakeLog(), True, {"written": 0})
            assert (delivered, cap, outcome) == (False, False, "unverified"), \
                (delivered, cap, outcome)
            assert summary["writeFailures"] == 1, summary
            assert summary["chargeUncertain"] == 1, summary
            assert fake.rows == []
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_push: a write the counter cannot confirm is unverified, neither delivered nor lost"


def test_detect_billing_reads_the_charging_manager():
    async def run():
        original = main.Actor
        try:
            main.Actor = FakeActor(charging=FakeChargingManager(True, {}))
            assert await main._detect_billing(FakeLog()) is False, "no event in the price list"
            main.Actor = FakeActor(charging=FakeChargingManager(False, {"product-delivered": 0.002}))
            assert await main._detect_billing(FakeLog()) is False, "not pay per event"
            main.Actor = FakeActor()
            assert await main._detect_billing(FakeLog()) is True
            class NoManager(FakeActor):
                def get_charging_manager(self):
                    raise AttributeError("no charging manager")
            main.Actor = NoManager()
            assert await main._detect_billing(FakeLog()) is False, \
                "no charging manager means no charge: rows are free and the summary says so"
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_detect_billing: reads the charging manager, names every reason a run cannot charge"


# --- a whole run, capped: what it did not read must still be counted --------------------

class FakeResponse:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class FakeHTTP:
    """A stand-in httpx client for one run: answers by substring, records every call."""

    def __init__(self, routes, calls):
        self.routes = routes
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls.append(url)
        for pattern, answer in self.routes.items():
            if pattern in url:
                return answer
        return FakeResponse(404)


class FakeKV:
    def __init__(self):
        self.data = {}

    async def get_value(self, key):
        return self.data.get(key)

    async def set_value(self, key, value):
        self.data[key] = value


class RunActor:
    """The SDK surface main() touches, for one capped run."""

    def __init__(self, cap_after, stores=None, only_changes=False, fail_free_status=None,
                 in_stock_only=False):
        self.cap_after = cap_after
        self.fail_free_status = fail_free_status
        self.stores_input = stores or ["shop-a.example", "shop-b.example"]
        self.only_changes = only_changes
        self.in_stock_only = in_stock_only
        self.rows = []
        self.charged = 0
        self.stores = {}
        self.log = FakeLog()
        self.status_message = None
        self.failure = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_input(self):
        return {"stores": self.stores_input, "onlyChanges": self.only_changes,
                "inStockOnly": self.in_stock_only}

    async def open_dataset(self):
        return FakeDataset(self)

    def get_charging_manager(self):
        return FakeChargingManager()

    def get_env(self):
        return {"run_id": "run-1"}

    async def set_status_message(self, text):
        self.status_message = text

    async def fail(self, status_message=None):
        self.failure = status_message

    async def open_key_value_store(self, name=None):
        return self.stores.setdefault(name or "run", FakeKV())

    async def push_data(self, row, charged_event_name=None):
        if self.fail_free_status is not None and row.get("status") == self.fail_free_status:
            raise RuntimeError("the dataset refused a %s row" % row.get("status"))
        if self.charged >= self.cap_after:
            return FakeChargeResult(0, True)
        self.rows.append(row)
        self.charged += 1
        return FakeChargeResult(1, False)


def _catalog(prefix, count, price="10.00"):
    return {"products": [{"handle": "%s%d" % (prefix, i), "title": "%s%d" % (prefix, i),
                          "variants": [{"price": price, "available": True}]}
                         for i in range(count)]}


def test_a_capped_run_counts_what_it_did_not_read():
    """B2 of the 06.11 gate: cap after 1 of 3 products left the rest of the shop and the whole
    second shop out of every counter, and the invariant the README prints did not add up."""
    async def run():
        routes = {
            "shop-a.example/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "shop-a.example/products.json": FakeResponse(200, json.dumps(_catalog("a", 3))),
            "shop-b.example/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "shop-b.example/products.json": FakeResponse(200, json.dumps(_catalog("b", 1))),
        }
        calls = []
        fake = RunActor(cap_after=1)
        original_actor, original_client = main.Actor, main.httpx.AsyncClient
        original_host_check = shopify.host_is_public
        try:
            main.Actor = fake
            main.httpx.AsyncClient = lambda *args, **kwargs: FakeHTTP(routes, calls)
            # .example is reserved and resolves nowhere: this test is about counters, not DNS
            shopify.host_is_public = lambda host: True
            await main.main()
        finally:
            main.Actor, main.httpx.AsyncClient = original_actor, original_client
            shopify.host_is_public = original_host_check

        summary = fake.stores[main.SUMMARY_STORE].data["RUN_SUMMARY"]
        assert "shop-b.example" not in " ".join(calls), \
            "the second store was never read: %s" % calls
        assert summary["storesAsked"] == 2, summary
        assert summary["storesOk"] == 1 and summary["storesNotChecked"] == 1, summary
        assert summary["productsSeen"] == 2, summary
        assert summary["productsDelivered"] == 1, summary
        assert summary["productsUndelivered"] == 1, summary
        assert summary["productsNotChecked"] == 1, summary
        stats = summary["productsByStore"]["shop-a.example"]
        assert (stats["read"], stats["delivered"], stats["undelivered"],
                stats["notChecked"]) == (3, 1, 1, 1), stats
        assert stats["truncated"] is True, stats
        assert "counterMismatch" not in summary, summary["counterMismatch"]
        assert "storeCounterMismatch" not in summary, summary["storeCounterMismatch"]
        # the counters the README prints really add up on this run
        assert summary["productsSeen"] == (summary["productsDelivered"] + summary["noPrice"]
                                           + summary["filteredOut"]
                                           + summary["productsUndelivered"]), summary
        assert summary["storesAsked"] == (summary["storesOk"] + summary["storesBlocked"]
                                          + summary["storesEmpty"] + summary["invalidInputs"]
                                          + summary["storesNotChecked"]), summary

    asyncio.run(run())
    return "a capped run counts the rest of the shop and the unread shops, and the sums close"


def test_the_same_store_twice_is_read_and_charged_once():
    """Orestes, 07.11: ["a.example", "a.example"] gave 6 rows and 6 charges for 2 products.

    A store typed twice, or typed once with a www copy, is one target: the buyer pays for data,
    not for a duplicate line in their own input.
    """
    async def run():
        routes = {
            "shop-a.example/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "shop-a.example/products.json": FakeResponse(200, json.dumps(_catalog("a", 2))),
        }
        calls = []
        fake = RunActor(cap_after=99, stores=["shop-a.example", "https://www.shop-a.example"])
        original_actor, original_client = main.Actor, main.httpx.AsyncClient
        original_host_check = shopify.host_is_public
        try:
            main.Actor = fake
            main.httpx.AsyncClient = lambda *args, **kwargs: FakeHTTP(routes, calls)
            shopify.host_is_public = lambda host: True
            await main.main()
        finally:
            main.Actor, main.httpx.AsyncClient = original_actor, original_client
            shopify.host_is_public = original_host_check

        summary = fake.stores[main.SUMMARY_STORE].data["RUN_SUMMARY"]
        assert summary["storesAsked"] == 1, summary
        assert summary["storesDeduplicated"] == 1, summary
        assert len(fake.rows) == 2, "one store, two products, got %d rows" % len(fake.rows)
        assert summary["chargedEvents"] == 2, summary
        assert summary["productsSeen"] == 2, summary

    asyncio.run(run())
    return "the same store twice, or its www copy, is read and charged once"


def test_a_capped_product_is_not_written_into_the_snapshot():
    """Orestes, 07.11: the product that hit the spending cap went into the snapshot undelivered,
    so the next onlyChanges run called it unchanged and never delivered it at all."""
    async def run():
        routes = {
            "shop-a.example/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "shop-a.example/products.json": FakeResponse(200, json.dumps(_catalog("a", 3))),
        }
        calls = []
        fake = RunActor(cap_after=1, stores=["shop-a.example"], only_changes=True)
        original_actor, original_client = main.Actor, main.httpx.AsyncClient
        original_host_check = shopify.host_is_public
        try:
            main.Actor = fake
            main.httpx.AsyncClient = lambda *args, **kwargs: FakeHTTP(routes, calls)
            shopify.host_is_public = lambda host: True
            await main.main()
        finally:
            main.Actor, main.httpx.AsyncClient = original_actor, original_client
            shopify.host_is_public = original_host_check

        summary = fake.stores[main.SUMMARY_STORE].data["RUN_SUMMARY"]
        snapshot = fake.stores[main.STATE_STORE].data["CATALOG-shop-a.example"]
        assert summary["productsDelivered"] == 1, summary
        assert sorted(snapshot) == ["a0"], "only delivered products belong in the snapshot: %s" % snapshot

    asyncio.run(run())
    return "the product stopped by the cap stays new for the next run"


def test_a_paid_write_is_never_repeated_after_a_failure():
    """Orestes, 07.11: a charge failure repeated the paid write and put the row twice in the
    dataset. One attempt for a paid write, one metadata call to see what landed."""
    async def run():
        log = FakeLog()
        summary = _summary()
        fake = FakeActor(mode="raise_before_write")
        original = _with_actor(fake)
        try:
            state = {"written": 0}
            delivered, cap, uncharged = await main._push(
                {"handle": "a", "status": "DELIVERED"}, summary, log, True, state)
            assert len(fake.calls) == 1, "a paid write was attempted %d times" % len(fake.calls)
            assert (delivered, cap, uncharged) == (False, False, "unverified"), \
                (delivered, cap, uncharged)
            assert summary["writeFailures"] == 1, summary
            # a free write is retried, because a retry costs no money
            free = FakeActor(mode="raise_before_write")
            main.Actor = free
            await main._push({"handle": "b", "status": "DELIVERED"}, _summary(), log, False,
                             {"written": 0})
            assert len(free.calls) == 2, "a free write was attempted %d times" % len(free.calls)
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_push: a paid write is tried once, a free write twice"


def test_two_shops_under_one_domain_are_two_stores():
    """Orestes, 07.11: the dedup key was the registrable domain, so shop1.example.com and
    shop2.example.com counted as one store and the second shop vanished with its products."""
    async def run():
        routes = {
            "shop1.example.com/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "shop1.example.com/products.json": FakeResponse(200, json.dumps(_catalog("s1", 1))),
            "shop2.example.com/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "shop2.example.com/products.json": FakeResponse(200, json.dumps(_catalog("s2", 1))),
        }
        calls = []
        fake = RunActor(cap_after=99, stores=["shop1.example.com", "shop2.example.com"])
        original_actor, original_client = main.Actor, main.httpx.AsyncClient
        original_host_check = shopify.host_is_public
        try:
            main.Actor = fake
            main.httpx.AsyncClient = lambda *args, **kwargs: FakeHTTP(routes, calls)
            shopify.host_is_public = lambda host: True
            await main.main()
        finally:
            main.Actor, main.httpx.AsyncClient = original_actor, original_client
            shopify.host_is_public = original_host_check

        summary = fake.stores[main.SUMMARY_STORE].data["RUN_SUMMARY"]
        assert summary["storesAsked"] == 2, summary
        assert summary["storesDeduplicated"] == 0, summary
        assert summary["storesOk"] == 2, summary
        assert sorted(row["handle"] for row in fake.rows) == ["s10", "s20"], fake.rows
        assert summary["chargedEvents"] == 2, summary

    asyncio.run(run())
    return "two shops on one domain stay two stores; only a www copy is the same store"


def test_a_no_price_row_that_did_not_land_stays_out_of_the_snapshot():
    """Orestes, 07.11: the NO_PRICE branch ignored what _push returned, so a row the dataset
    refused went into the snapshot and onlyChanges never delivered it again."""
    async def run():
        catalog = {"products": [
            {"handle": "a0", "title": "a0", "variants": [{"price": "10.00", "available": True}]},
            {"handle": "gift", "title": "gift", "variants": [{"available": True}]},
        ]}
        routes = {
            "shop-a.example/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "shop-a.example/products.json": FakeResponse(200, json.dumps(catalog)),
        }
        calls = []
        fake = RunActor(cap_after=99, stores=["shop-a.example"], only_changes=True,
                        fail_free_status="NO_PRICE")
        original_actor, original_client = main.Actor, main.httpx.AsyncClient
        original_host_check = shopify.host_is_public
        try:
            main.Actor = fake
            main.httpx.AsyncClient = lambda *args, **kwargs: FakeHTTP(routes, calls)
            shopify.host_is_public = lambda host: True
            await main.main()
        finally:
            main.Actor, main.httpx.AsyncClient = original_actor, original_client
            shopify.host_is_public = original_host_check

        summary = fake.stores[main.SUMMARY_STORE].data["RUN_SUMMARY"]
        snapshot = fake.stores[main.STATE_STORE].data["CATALOG-shop-a.example"]
        assert summary["noPrice"] == 1, summary
        # a free write is retried once, so one refused row shows two failed attempts
        assert summary["writeFailures"] == 2, summary
        assert summary["productsUnverified"] == 0, \
            "a NO_PRICE row is already counted in noPrice, not as an unverified delivery"
        assert sorted(snapshot) == ["a0"], \
            "a NO_PRICE row that never reached the dataset must stay new: %s" % snapshot

    asyncio.run(run())
    return "a NO_PRICE row the dataset refused is not written into the onlyChanges snapshot"


def test_a_free_write_is_not_repeated_when_it_already_landed():
    """Orestes, 07.11: a free write repeated blindly put the same row in the dataset twice,
    because the exception arrives after the row is stored. The counter decides, not the guess."""
    async def run():
        fake = FakeActor(mode="raise_after_write")
        original = _with_actor(fake)
        try:
            summary = _summary()
            delivered, cap, outcome = await main._push(
                {"handle": "a", "status": "NO_PRICE"}, summary, FakeLog(), False, {"written": 0})
            assert len(fake.calls) == 1, "a free write was pushed %d times" % len(fake.calls)
            assert len(fake.rows) == 1, fake.rows
            assert delivered is True and outcome == "free", (delivered, outcome)
            assert summary["deliveredUncharged"] == 0, summary
            assert summary["chargeUncertain"] == 0, \
                "a row that could never be charged is not charge-uncertain: %s" % summary
        finally:
            main.Actor = original

    asyncio.run(run())
    return "a free write that already landed is not pushed again"


def test_a_filtered_product_is_still_written_into_the_snapshot():
    """Orestes, 07.11: inStockOnly dropped a product before the snapshot, so a later
    onlyChanges run saw "in stock" on both sides, called it unchanged, and the product coming
    back in stock was never delivered. The snapshot follows what was read, not what was sold."""
    async def run():
        calls = []
        fake = RunActor(cap_after=99, stores=["shop-a.example"], only_changes=True,
                        in_stock_only=True)
        original_actor, original_client = main.Actor, main.httpx.AsyncClient
        original_host_check = shopify.host_is_public

        def catalog(available):
            return {"products": [{"handle": "p1", "title": "One",
                                  "variants": [{"price": "10.00", "available": available}]}]}

        def routes_for(available):
            return {
                "shop-a.example/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
                "shop-a.example/products.json": FakeResponse(
                    200, json.dumps(catalog(available))),
            }

        try:
            main.Actor = fake
            shopify.host_is_public = lambda host: True

            main.httpx.AsyncClient = lambda *a, **k: FakeHTTP(routes_for(False), calls)
            await main.main()
            first = fake.stores[main.SUMMARY_STORE].data["RUN_SUMMARY"]
            snapshot = fake.stores[main.STATE_STORE].data["CATALOG-shop-a.example"]
            assert first["productsDelivered"] == 0, first
            assert first["filteredBy"].get("inStockOnly") == 1, first
            assert snapshot.get("p1", {}).get("availability") == "out_of_stock", \
                "a product hidden by inStockOnly must remember it is out of stock: %s" % snapshot

            main.httpx.AsyncClient = lambda *a, **k: FakeHTTP(routes_for(True), calls)
            await main.main()
            second = fake.stores[main.SUMMARY_STORE].data["RUN_SUMMARY"]
            assert second["productsDelivered"] == 1, second
            assert second["changesBy"].get("back-in-stock") == 1, second
        finally:
            main.Actor, main.httpx.AsyncClient = original_actor, original_client
            shopify.host_is_public = original_host_check

    asyncio.run(run())
    return "a product filtered by inStockOnly comes back as back-in-stock on the next run"


def test_a_store_and_its_product_url_deliver_that_product_once():
    """Orestes, 07.11: a.com plus a.com/products/x in one input delivered and charged the same
    product twice. The catalog of the store is read anyway, so the product URL adds nothing."""
    async def run():
        routes = {
            "shop-a.example/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "shop-a.example/products.json": FakeResponse(200, json.dumps(_catalog("a", 2))),
        }
        calls = []
        fake = RunActor(cap_after=99, stores=["shop-a.example",
                                              "https://shop-a.example/products/a0"])
        original_actor, original_client = main.Actor, main.httpx.AsyncClient
        original_host_check = shopify.host_is_public
        try:
            main.Actor = fake
            main.httpx.AsyncClient = lambda *a, **k: FakeHTTP(routes, calls)
            shopify.host_is_public = lambda host: True
            await main.main()
        finally:
            main.Actor, main.httpx.AsyncClient = original_actor, original_client
            shopify.host_is_public = original_host_check

        summary = fake.stores[main.SUMMARY_STORE].data["RUN_SUMMARY"]
        assert summary["storesDeduplicated"] == 1, summary
        assert len(fake.rows) == 2, "one product must not be charged twice: %s" % (
            [row["handle"] for row in fake.rows],)
        assert summary["chargedEvents"] == 2, summary

    asyncio.run(run())
    return "a store plus one of its product URLs delivers each product once"


def test_products_by_store_reports_offsite_addresses_of_a_cut_store():
    """Orestes, 07.11: productsByStore was said to hide skippedOffsite when the store was not
    read in full. This pins the counter on a store cut by maxProductsPerStore."""
    async def run():
        index = ('<?xml version="1.0"?><sitemapindex>'
                 '<sitemap><loc>https://shop-a.example/sitemap_products_1.xml</loc></sitemap>'
                 '<sitemap><loc>https://other.example/sitemap_products_2.xml</loc></sitemap>'
                 '</sitemapindex>')
        product_map = ('<?xml version="1.0"?><urlset>'
                       '<url><loc>https://shop-a.example/products/a0</loc></url>'
                       '<url><loc>https://shop-a.example/products/a1</loc></url>'
                       '</urlset>')
        page = ('<script type="application/ld+json">{"@type":"Product","name":"One",'
                '"offers":[{"price":"10.00","priceCurrency":"USD",'
                '"availability":"https://schema.org/InStock"}]}</script>')
        routes = {
            "shop-a.example/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "shop-a.example/products.json": FakeResponse(403),
            "shop-a.example/sitemap_products_1.xml": FakeResponse(200, product_map),
            "shop-a.example/sitemap.xml": FakeResponse(200, index),
            "shop-a.example/products/a0": FakeResponse(200, page),
            "shop-a.example/products/a1": FakeResponse(200, page),
        }
        calls = []
        fake = RunActor(cap_after=99, stores=["shop-a.example"])
        original_actor, original_client = main.Actor, main.httpx.AsyncClient
        original_host_check = shopify.host_is_public
        original_read = shopify.read_store

        async def cut_to_one(fetcher, target, max_products, log):
            return await original_read(fetcher, target, 1, log)

        try:
            main.Actor = fake
            main.httpx.AsyncClient = lambda *a, **k: FakeHTTP(routes, calls)
            shopify.host_is_public = lambda host: True
            shopify.read_store = cut_to_one
            await main.main()
        finally:
            main.Actor, main.httpx.AsyncClient = original_actor, original_client
            shopify.host_is_public = original_host_check
            shopify.read_store = original_read

        summary = fake.stores[main.SUMMARY_STORE].data["RUN_SUMMARY"]
        stats = summary["productsByStore"]["shop-a.example"]
        assert stats["truncated"] is True, stats
        assert stats["skippedOffsite"] == 1, \
            "the off-site address the store named must be visible per store: %s" % stats
        assert summary["skippedOffsite"] == 1, summary
        assert stats["read"] == 1, stats

    asyncio.run(run())
    return "productsByStore keeps skippedOffsite and truncated together on a cut store"


def run_all():
    checks = [
        test_change_kind,
        test_filter_reason,
        test_push_charges_once_per_row,
        test_push_at_cap_writes_nothing_and_is_not_uncharged,
        test_push_does_not_duplicate_a_row_written_before_a_charge_failure,
        test_push_calls_an_unconfirmed_row_unverified,
        test_detect_billing_reads_the_charging_manager,
        test_a_capped_run_counts_what_it_did_not_read,
        test_the_same_store_twice_is_read_and_charged_once,
        test_a_capped_product_is_not_written_into_the_snapshot,
        test_a_paid_write_is_never_repeated_after_a_failure,
        test_two_shops_under_one_domain_are_two_stores,
        test_a_no_price_row_that_did_not_land_stays_out_of_the_snapshot,
        test_a_free_write_is_not_repeated_when_it_already_landed,
        test_a_filtered_product_is_still_written_into_the_snapshot,
        test_a_store_and_its_product_url_deliver_that_product_once,
        test_products_by_store_reports_offsite_addresses_of_a_cut_store,
    ]
    failed = 0
    for check in checks:
        try:
            note = check()
            print("ok   %-56s %s" % (check.__name__, note or ""))
        except AssertionError as exc:
            failed += 1
            print("FAIL %-56s %s" % (check.__name__, exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERR  %-56s %s: %s" % (check.__name__, type(exc).__name__, exc))
    print("\n%d checks, %d failed" % (len(checks), failed))
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if run_all() else 0)
