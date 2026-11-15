"""Checks for the run itself: rows, filters, duplicates, unknown money facts, silence.

The fake Actor repeats only what the real SDK offers (push_data, open_dataset,
open_key_value_store, get_env, fail, set_status_message). A fake that invents methods
would make these tests prove the fake, not the actor. It is an instance, not a class:
Python looks up `__aenter__` on the type, so `async with` needs a real object.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import feed, main as main_module
from src import pump
from tests.test_pump import CURRENT_NUMBERS, REAL_EVENT, _b64_event


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, *args, **kwargs):
        self.lines.append(("info", args))

    def warning(self, *args, **kwargs):
        self.lines.append(("warning", args))

    def error(self, *args, **kwargs):
        self.lines.append(("error", args))


class FakeDataset:
    def __init__(self, rows, lag=0):
        self.rows = rows
        self.lag = lag

    async def get_metadata(self):
        return {"item_count": max(0, len(self.rows) - self.lag)}


class FakeStore:
    def __init__(self):
        self.data = {}

    async def set_value(self, key, value):
        self.data[key] = value


class ChargeResult:
    """The shape apify 4.2.0 returns from push_data(..., charged_event_name=...)."""

    def __init__(self, charged_count=0, event_charge_limit_reached=False):
        self.charged_count = charged_count
        self.event_charge_limit_reached = event_charge_limit_reached


class FakePricing:
    """Only the fields main() reads from the charging manager's pricing info."""

    def __init__(self, is_pay_per_event=True, per_event_prices=None):
        self.is_pay_per_event = is_pay_per_event
        self.per_event_prices = ({"token-delivered": 0.0006}
                                 if per_event_prices is None else per_event_prices)

    def get_pricing_info(self):
        return self


class FakeActor:
    def __init__(self, input_data, pricing=None, cap_after=None, dataset_lag=0):
        self.log = FakeLog()
        self.input_data = dict(input_data)
        self.rows = []
        self.stores = {}
        self.fail_message = None
        self.status_message = None
        self.status_awaited = False
        self.pricing = pricing
        self.cap_after = cap_after
        self.dataset_lag = dataset_lag
        self.charged_rows = 0
        self.charge_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_input(self):
        return dict(self.input_data)

    async def open_dataset(self):
        return FakeDataset(self.rows, lag=self.dataset_lag)

    async def push_data(self, row, charged_event_name=None):
        # the SDK writes the row and charges it under one lock: with the cap spent it writes
        # nothing, so this double must not append the row either
        self.charge_calls.append(charged_event_name)
        if charged_event_name is not None and self.pricing is not None:
            if self.cap_after is not None and self.charged_rows >= self.cap_after:
                return ChargeResult(charged_count=0, event_charge_limit_reached=True)
            self.charged_rows += 1
            self.rows.append(dict(row))
            return ChargeResult(charged_count=1, event_charge_limit_reached=False)
        self.rows.append(dict(row))
        return None

    def get_charging_manager(self):
        if self.pricing is None:
            raise RuntimeError("a local run has no charging manager")
        return self.pricing

    async def open_key_value_store(self, name=None):
        return self.stores.setdefault(name or "__run__", FakeStore())

    def get_env(self):
        return {}

    async def fail(self, status_message=None, **kwargs):
        self.fail_message = status_message

    async def set_status_message(self, text):
        # async on purpose: in apify 4.2.0 Actor.set_status_message is `async def`, so a
        # call without await must break these tests instead of passing them
        self.status_awaited = True
        self.status_message = text

    def summary(self):
        store = self.stores.get(main_module.SUMMARY_STORE)
        assert store is not None, "the named summary store was never written"
        return store.data["RUN_SUMMARY"]


def _event(name="Token", symbol="TKN", mint_seed=b"\x77", numbers=CURRENT_NUMBERS):
    line = _b64_event(name, symbol, "https://x/y", [mint_seed * 32] * 4, numbers, tail=b"\x00" * 84)
    event = pump.parse_create_event([line])
    event["signature"] = "SIG-%s" % name
    event["source"] = "stream"
    return event


def _run(events, input_data, tx_reader=None, fake_time=None, notifications=None,
         pricing=None, cap_after=None, dataset_lag=0) -> FakeActor:
    """Run main() with a producer that emits these events and then idles."""
    original = {
        "produce": feed.produce,
        "rpc_call": feed.rpc_call,
        "Actor": main_module.Actor,
        "time": main_module.time,
    }

    async def produce(queue, stats, cfg, log, client):
        stats["mode"] = "stream"
        if notifications is not None:
            stats["notifications"] = notifications
        for event in events:
            await queue.put(dict(event))
        await asyncio.sleep(3600)

    async def rpc_call(client, url, method, params, timeout=25.0):
        if tx_reader is None:
            raise feed.RpcError("no transaction in this test")
        assert method == "getTransaction", method
        return tx_reader(params[0])

    actor = FakeActor(input_data, pricing=pricing, cap_after=cap_after, dataset_lag=dataset_lag)
    feed.produce = produce
    feed.rpc_call = rpc_call
    main_module.Actor = actor
    if fake_time is not None:
        main_module.time = fake_time
    try:
        asyncio.run(main_module.main())
    finally:
        feed.produce = original["produce"]
        feed.rpc_call = original["rpc_call"]
        main_module.Actor = original["Actor"]
        main_module.time = original["time"]
    return actor


def test_two_tokens_become_two_rows():
    actor = _run([_event("alpha"), _event("beta", mint_seed=b"\x78")],
                 {"durationSecs": 10, "maxTokens": 2, "includeDevBuy": False})
    assert len(actor.rows) == 2, actor.rows
    assert [row["name"] for row in actor.rows] == ["alpha", "beta"]
    summary = actor.summary()
    assert summary["tokensSeen"] == 2, summary
    assert summary["rowsDelivered"] == 2, summary
    assert summary["stopReason"] == "max-tokens", summary
    assert actor.fail_message is None, actor.fail_message
    assert summary["datasetItemCount"] == 2, summary
    assert summary["delivery"].startswith("one charge per delivered token row")


def test_real_fixture_row_keeps_the_chain_numbers():
    event = pump.parse_create_event([REAL_EVENT])
    event["signature"] = "SIG-real"
    event["source"] = "stream"
    actor = _run([event], {"durationSecs": 10, "maxTokens": 1, "includeDevBuy": False})
    row = actor.rows[0]
    assert row["mint"].endswith("pump"), row["mint"]
    assert row["virtualSolReserves"] == 30.0, row
    assert row["curveProgressPercent"] == 0.0, row
    assert row["devBuyTokens"] is None, "an unread buy must stay null, not zero"
    assert row["creatorTokensThisRun"] == 0, row


def test_filters_drop_everything_and_say_so():
    actor = _run([_event("alpha"), _event("beta", mint_seed=b"\x79")],
                 {"durationSecs": 10, "maxTokens": 5, "includeDevBuy": False, "nameContains": "zzz"})
    assert actor.rows == [], actor.rows
    summary = actor.summary()
    assert summary["filteredOut"] == 2, summary
    assert summary["filteredBy"].get("name") == 2, summary
    assert summary["rowsDelivered"] == 0
    assert actor.fail_message is None, "the buyer's own filter is not a broken run"
    assert actor.status_message and "filter" in actor.status_message.lower(), actor.status_message


def test_duplicate_mint_is_written_once():
    event = _event("alpha")
    actor = _run([event, dict(event), _event("beta", mint_seed=b"\x7a")],
                 {"durationSecs": 10, "maxTokens": 5, "includeDevBuy": False})
    assert len(actor.rows) == 2, actor.rows
    assert actor.summary()["duplicatesSkipped"] == 1, actor.summary()


def test_unreadable_buy_is_counted_and_the_row_still_goes_out():
    actor = _run([_event("alpha")], {"durationSecs": 10, "maxTokens": 1, "includeDevBuy": True})
    summary = actor.summary()
    assert summary["devBuyUnknown"] == 1, summary
    assert actor.rows[0]["devBuyTokens"] is None
    assert actor.rows[0]["devBuyPercent"] is None


def test_creator_buy_is_read_from_the_transaction():
    event = _event("alpha")
    real_mint = event["mint"]
    creator = event["creator"]

    def reader(signature):
        return {"meta": {
            "preTokenBalances": [],
            "postTokenBalances": [{"mint": real_mint, "owner": creator,
                                   "uiTokenAmount": {"uiAmount": 50_000_000.0}}],
        }}

    actor = _run([event], {"durationSecs": 10, "maxTokens": 1, "includeDevBuy": True}, tx_reader=reader)
    row = actor.rows[0]
    assert row["devBuyTokens"] == 50_000_000.0, row
    assert row["devBuyPercent"] == 5.0, row
    assert actor.summary()["devBuyUnknown"] == 0


def test_creator_who_bought_nothing_reports_zero_not_null():
    actor = _run([_event("alpha")], {"durationSecs": 10, "maxTokens": 1, "includeDevBuy": True},
                 tx_reader=lambda signature: {"meta": {"preTokenBalances": [], "postTokenBalances": []}})
    row = actor.rows[0]
    assert row["devBuyTokens"] == 0.0, row
    assert row["devBuyPercent"] == 0.0, row
    assert actor.summary()["devBuyUnknown"] == 0, actor.summary()


def test_impossible_reserves_row_keeps_nulls_and_is_counted():
    numbers = (1793992690, 1_073_000_000_000_000, 8_901_778_365_592,
               793_100_000_000_000, 1_000_000_000_000_000)
    actor = _run([_event("odd", numbers=numbers)],
                 {"durationSecs": 10, "maxTokens": 1, "includeDevBuy": False})
    row = actor.rows[0]
    assert row["virtualSolReserves"] is None, row
    assert row["curveProgressPercent"] is None, row
    assert row["mint"], row
    assert actor.summary()["reservesUnavailable"] == 1, actor.summary()


class FakeTime:
    """A clock that jumps past the deadline after the loop starts."""

    def __init__(self):
        self.reads = 0

    def monotonic(self):
        self.reads += 1
        return 0.0 if self.reads <= 2 else 1000.0

    def time(self):
        return 1_800_000_000.0


def test_silence_fails_with_the_endpoint_named():
    actor = _run([], {"durationSecs": 10, "includeDevBuy": False}, fake_time=FakeTime())
    assert actor.fail_message, "a run that heard nothing left the dashboard green"
    assert "rpc" in actor.fail_message.lower(), actor.fail_message
    summary = actor.summary()
    assert summary["tokensSeen"] == 0, summary
    assert summary["rowsDelivered"] == 0, summary


def test_bad_websocket_scheme_is_refused_before_a_run():
    actor = _run([], {"durationSecs": 10, "rpcWsUrl": "http://not-a-websocket"})
    assert actor.fail_message and "ws://" in actor.fail_message, actor.fail_message
    assert actor.rows == []


def test_impossible_buy_range_is_refused():
    actor = _run([], {"durationSecs": 10, "minDevBuyPercent": 60, "maxDevBuyPercent": 10})
    assert actor.fail_message and "minDevBuyPercent" in actor.fail_message, actor.fail_message


def test_filter_reason_reaches_the_run_header():
    """The header message is a coroutine on the platform: it must be awaited, not created."""
    actor = _run([_event("alpha")], {"durationSecs": 10, "maxTokens": 5,
                                     "includeDevBuy": False, "nameContains": "zzz"})
    assert actor.status_message, "the run header stayed empty"
    assert actor.status_awaited, "set_status_message was called but never awaited"


def test_a_row_the_dataset_refuses_is_counted_as_undelivered():
    original = FakeActor.push_data

    async def push_data(self, row, **kwargs):
        if row.get("name") == "beta":
            raise RuntimeError("the dataset refused this row")
        return await original(self, row, **kwargs)

    FakeActor.push_data = push_data
    try:
        actor = _run([_event("alpha"), _event("beta", mint_seed=b"\x7b")],
                     {"durationSecs": 10, "maxTokens": 2, "includeDevBuy": False})
    finally:
        FakeActor.push_data = original
    summary = actor.summary()
    assert len(actor.rows) == 1, actor.rows
    assert summary["rowsDelivered"] == 1, summary
    assert summary["rowsUndelivered"] == 1, summary
    assert summary["tokensSeen"] == (summary["rowsDelivered"] + summary["filteredOut"]
                                     + summary["rowsUndelivered"]), summary


def test_notifications_are_what_the_feed_heard_not_what_it_delivered():
    actor = _run([_event("alpha")], {"durationSecs": 10, "maxTokens": 1, "includeDevBuy": False},
                 notifications=120)
    summary = actor.summary()
    assert summary["notifications"] == 120, summary
    assert summary["rowsDelivered"] == 1, summary


def test_a_live_socket_without_creations_is_not_blamed_on_the_endpoint():
    actor = _run([], {"durationSecs": 10, "includeDevBuy": False},
                 fake_time=FakeTime(), notifications=40)
    assert actor.fail_message, "a run with no rows must still fail"
    assert "not one of them" in actor.fail_message, actor.fail_message
    assert actor.summary()["notifications"] == 40, actor.summary()


def test_a_fallback_that_could_not_read_one_transaction_says_so():
    """Live case: pace 0.3 s, every getTransaction answered 429, and the run blamed the
    program address. The message has to name the pace, not the endpoint."""
    actor_input = {"durationSecs": 10, "includeDevBuy": False}
    original_produce = feed.produce
    original_actor = main_module.Actor

    async def produce(queue, stats, cfg, log, client):
        stats["mode"] = "poll"
        stats["notifications"] = 900
        stats["signaturesSeen"] = 900
        stats["signaturesUnread"] = 1
        stats["lastFeedError"] = "RpcError: getTransaction was rate limited (429)"
        await asyncio.sleep(3600)

    actor = FakeActor(actor_input)
    feed.produce = produce
    main_module.Actor = actor
    fake_time = FakeTime()
    original_time = main_module.time
    main_module.time = fake_time
    try:
        asyncio.run(main_module.main())
    finally:
        feed.produce = original_produce
        main_module.time = original_time
        main_module.Actor = original_actor
    assert actor.fail_message, "a run that read nothing must fail"
    assert "rpcPaceSecs" in actor.fail_message, actor.fail_message
    assert "429" in actor.fail_message, actor.fail_message
    assert "program address" not in actor.fail_message, actor.fail_message


def test_every_delivered_row_is_charged_once():
    actor = _run([_event("alpha"), _event("beta", mint_seed=b"\x78")],
                 {"durationSecs": 10, "maxTokens": 2, "includeDevBuy": False},
                 pricing=FakePricing())
    summary = actor.summary()
    assert summary["billingActive"] is True, summary
    assert summary["chargedEvents"] == 2, summary
    assert summary["chargedEvents"] == summary["rowsDelivered"], summary
    assert summary["deliveredUncharged"] == 0, summary
    assert len(actor.rows) == 2, actor.rows
    assert actor.charge_calls == ["token-delivered", "token-delivered"], actor.charge_calls


def test_spending_cap_stops_the_run_and_keeps_no_free_row():
    events = [_event("alpha"), _event("beta", mint_seed=b"\x78"),
              _event("gamma", mint_seed=b"\x79")]
    actor = _run(events, {"durationSecs": 10, "maxTokens": 0, "includeDevBuy": False},
                 pricing=FakePricing(), cap_after=1)
    summary = actor.summary()
    assert summary["stopReason"] == "spending-cap", summary
    assert summary["rowsDelivered"] == 1, summary
    assert len(actor.rows) == 1, actor.rows
    assert summary["chargedEvents"] == 1, summary
    assert summary["rowsUndelivered"] >= 1, summary


def test_pricing_without_our_event_delivers_free_and_says_so():
    actor = _run([_event("alpha")], {"durationSecs": 10, "maxTokens": 1, "includeDevBuy": False},
                 pricing=FakePricing(per_event_prices={"something-else": 1.0}))
    summary = actor.summary()
    assert summary["billingActive"] is False, summary
    assert summary["chargedEvents"] == 0, summary
    assert len(actor.rows) == 1, actor.rows
    assert actor.charge_calls == [None], actor.charge_calls


def test_a_lagging_dataset_count_is_flagged_never_printed_as_loss():
    # live: the platform reported 24 items while the dataset held 25, and a smaller number in
    # the summary reads as a lost row
    actor = _run([_event("alpha"), _event("beta", mint_seed=b"\x78")],
                 {"durationSecs": 10, "maxTokens": 2, "includeDevBuy": False}, dataset_lag=1)
    summary = actor.summary()
    assert summary["rowsDelivered"] == 2, summary
    assert summary["datasetItemCount"] is None, summary
    assert summary.get("datasetItemCountLag") is True, summary


def test_summary_says_how_long_the_feed_was_silent():
    # the output schema promises silentForSecs, so the key has to exist
    actor = _run([_event("alpha")], {"durationSecs": 10, "maxTokens": 1, "includeDevBuy": False})
    summary = actor.summary()
    assert "silentForSecs" in summary, summary
    assert summary["silentForSecs"] >= 0, summary


def test_window_is_clamped_to_the_platform_deadline():
    # a 5 minute automatic check cannot host a 300 s window plus container start: on 07.11
    # at 20:42 the check run was killed as TIMED-OUT, so the window follows ACTOR_TIMEOUT_AT
    from datetime import datetime, timedelta, timezone
    moment = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    os.environ["ACTOR_TIMEOUT_AT"] = moment
    try:
        actor = _run([_event("alpha")], {"durationSecs": 3600, "maxTokens": 1, "includeDevBuy": False})
    finally:
        os.environ.pop("ACTOR_TIMEOUT_AT", None)
    summary = actor.summary()
    assert summary["durationSecsRequested"] == 3600, summary["durationSecsRequested"]
    assert summary["durationCappedByPlatform"] is True, summary
    assert summary["durationSecs"] == 10, summary["durationSecs"]


def test_window_is_kept_when_the_platform_sets_no_deadline():
    os.environ.pop("ACTOR_TIMEOUT_AT", None)
    actor = _run([_event("alpha")], {"durationSecs": 30, "maxTokens": 1, "includeDevBuy": False})
    summary = actor.summary()
    assert summary["durationCappedByPlatform"] is False, summary
    assert summary["durationSecs"] == 30, summary["durationSecs"]


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failures = []
    for test in tests:
        try:
            test()
            print("ok   %s" % test.__name__)
        except AssertionError as exc:
            failures.append((test.__name__, str(exc)))
            print("FAIL %s: %s" % (test.__name__, exc))
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, "%s: %s" % (type(exc).__name__, exc)))
            print("ERROR %s: %s: %s" % (test.__name__, type(exc).__name__, exc))
    print("\n%d/%d passed" % (len(tests) - len(failures), len(tests)))
    return failures


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)


