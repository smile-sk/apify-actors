"""Money path: the real main() against a stand-in that repeats the SDK order.

The stand-in writes the rows BEFORE it raises, the way Actor.push_data does, and the
dataset counter can lag, refuse, or fail to be read at all. That is the only way to see
the difference between "the charge failed after the rows were written" and "the dataset
refused the rows".
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, ".")

import src.main as app
from src.main import EVENT


class FakeCharge:
    def __init__(self, charged_count: int | None, reached: bool = False) -> None:
        self.charged_count = charged_count
        self.event_charge_limit_reached = reached


class FakeDataset:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.fail_after_write = False
        self.fail_before_write = False
        self.metadata_raises = False
        self.stale_count: int | None = None

    async def push_data(self, data):
        rows = data if isinstance(data, list) else [data]
        if self.fail_before_write:
            raise RuntimeError("the dataset refused the rows")
        self.items.extend(copy.deepcopy(rows))
        if self.fail_after_write:
            raise RuntimeError("the charge raised")

    async def get_metadata(self):
        if self.metadata_raises:
            raise RuntimeError("metadata is not readable")
        count = len(self.items) if self.stale_count is None else self.stale_count
        self.stale_count = None
        return SimpleNamespace(item_count=count)


class FakeCharging:
    def __init__(self, ppe: bool = True, priced: bool = True, limit: int | None = None,
                 charged_count: int | None = None, reached: bool = False, broken: bool = False) -> None:
        self.ppe = ppe
        self.priced = priced
        self.limit = limit
        self.charged_count = charged_count
        self.reached = reached
        self.broken = broken
        self.calls: list[int] = []

    def get_pricing_info(self):
        if self.broken:
            raise RuntimeError("no price")
        prices = {EVENT: Decimal("0.003")} if self.priced else {}
        return SimpleNamespace(is_pay_per_event=self.ppe, per_event_prices=prices)

    def compute_push_data_limit(self, items_count, event_name, *, is_default_dataset):
        self.calls.append(items_count)
        assert is_default_dataset is True
        return items_count if self.limit is None else min(items_count, self.limit)


class FakeActor:
    """Instance, not a class: `async with Actor` needs special methods on the object."""

    def __init__(self, inputs=None, charging=None):
        self.dataset = FakeDataset()
        self.charging = charging or FakeCharging()
        self.inputs = inputs if inputs is not None else {"domains": ["a.com", "b.com"]}
        self.store = {}
        self.status = ""
        self.failed = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get_env(self):
        return {"run_id": "TESTRUN1"}

    async def open_dataset(self):
        return self.dataset

    def get_charging_manager(self):
        return self.charging

    async def get_input(self):
        return self.inputs

    async def push_data(self, data, *, charged_event_name=None):
        if charged_event_name is not None:
            assert charged_event_name == EVENT
        await self.dataset.push_data(data)
        count = self.charging.charged_count
        if count is None:
            count = len(data) if isinstance(data, list) else 1
        return FakeCharge(count, self.charging.reached)

    async def set_value(self, key, value):
        self.store[key] = value

    async def set_status_message(self, message):
        self.status = message

    async def fail(self, *, status_message=None, exit_code=0, exception=None):
        self.failed = status_message or "failed"


def fake_lookup_factory(verdicts: dict[str, dict]):
    async def fake_lookup(client, domain, timeout_s, mapping=None):
        found = verdicts.get(domain)
        if found is None:
            return {"verdict": "unknown", "reason": "the registry did not answer", "httpStatus": None}
        return dict(found)

    return fake_lookup


AVAILABLE = {"verdict": "available", "httpStatus": 404, "registrar": None, "registeredAt": None,
             "expiresAt": None, "updatedAt": None, "statuses": None, "nameservers": None}
TAKEN = {"verdict": "registered", "httpStatus": 200, "registrar": "Example Registrar",
         "registeredAt": "1995-08-14T04:00:00Z", "expiresAt": "2026-08-13T04:00:00Z",
         "updatedAt": None, "statuses": ["client transfer prohibited"], "nameservers": ["a.iana-servers.net"]}


def run(coro):
    return asyncio.run(coro)


def setup(fake_lookup, inputs, charging=None):
    actor = FakeActor(inputs, charging)
    app.Actor = actor
    app.lookup = fake_lookup
    run(app.main())
    return actor


def test_normal_run_charges_each_verdict():
    actor = setup(fake_lookup_factory({"a.com": AVAILABLE, "b.com": TAKEN}), {"domains": ["a.com", "b.com"]})
    rows = actor.dataset.items
    assert [r["status"] for r in rows] == ["OK", "OK"], rows
    assert rows[0]["available"] is True and rows[1]["available"] is False
    summary = actor.store["RUN_SUMMARY"]
    assert summary["chargedEvents"] == 2 and summary["domainsChecked"] == 2, summary
    assert summary["counterMismatch"] is False
    assert actor.store["RUN_SUMMARY-TESTRUN1"] == summary


def test_invalid_and_duplicate_are_free():
    actor = setup(fake_lookup_factory({"a.com": AVAILABLE}),
                  {"domains": ["a.com", "not a domain", "a.com"]})
    statuses = [r["status"] for r in actor.dataset.items]
    assert statuses.count("OK") == 1
    assert "INVALID" in statuses and "DUPLICATE" in statuses
    summary = actor.store["RUN_SUMMARY"]
    assert summary["chargedEvents"] == 1
    assert summary["counterMismatch"] is False, "free status rows are lines too"
    for row in actor.dataset.items:
        if row["status"] != "OK":
            assert row["verdict"] is None and row["available"] is None
            assert row["reason"]


def test_a_silent_registry_is_free_and_not_a_verdict():
    actor = setup(fake_lookup_factory({}), {"domains": ["a.com"]})
    row = actor.dataset.items[0]
    assert row["status"] == "NOT_CHECKED_UNKNOWN" and row["available"] is None
    assert actor.store["RUN_SUMMARY"]["chargedEvents"] == 0


def test_spending_cap_cuts_the_batch_before_the_write():
    actor = setup(fake_lookup_factory({"a.com": AVAILABLE, "b.com": TAKEN, "c.com": AVAILABLE}),
                  {"domains": ["a.com", "b.com", "c.com"]},
                  FakeCharging(limit=1))
    statuses = [r["status"] for r in actor.dataset.items]
    assert statuses.count("OK") == 1 and statuses.count("NOT_CHECKED_CAP") == 2, statuses
    summary = actor.store["RUN_SUMMARY"]
    assert summary["chargedEvents"] == 1 and summary["stopReason"] == "spending-cap"


def test_a_charge_failure_after_the_write_says_unknown():
    actor = FakeActor({"domains": ["a.com", "b.com"]}, FakeCharging())
    actor.dataset.fail_after_write = True
    app.Actor = actor
    app.lookup = fake_lookup_factory({"a.com": AVAILABLE, "b.com": TAKEN})
    run(app.main())
    summary = actor.store["RUN_SUMMARY"]
    note = summary["datasetNote"] or ""
    assert "unverified" in note and "nothing was delivered" not in note, note
    assert summary["stopReason"] == "charge-error"
    assert summary["chargeUncertain"] == 2 and summary["deliveredUnverified"] == 2
    assert summary["chargedEvents"] == 0
    assert summary["counterMismatch"] is True


def test_the_dataset_refusing_rows_is_not_called_delivered():
    actor = FakeActor({"domains": ["a.com", "b.com"]}, FakeCharging())
    actor.dataset.fail_before_write = True
    app.Actor = actor
    app.lookup = fake_lookup_factory({"a.com": AVAILABLE, "b.com": TAKEN})
    run(app.main())
    summary = actor.store["RUN_SUMMARY"]
    assert summary["chargedEvents"] == 0
    assert summary["rowsWritten"] == 0
    assert "collapsed" not in (summary["datasetNote"] or "")


def test_an_unreadable_price_stops_selling():
    actor = FakeActor({"domains": ["a.com"]}, FakeCharging(broken=True))
    app.Actor = actor
    app.lookup = fake_lookup_factory({"a.com": AVAILABLE})
    run(app.main())
    summary = actor.store["RUN_SUMMARY"]
    assert summary["chargedEvents"] == 0
    assert summary["billingActive"] is None
    assert summary["stopReason"] == "billing-price-unknown"
    statuses = [r["status"] for r in actor.dataset.items]
    assert statuses and all(s == "NOT_CHECKED_BILLING" for s in statuses), statuses
    assert actor.failed == "", "a named stop is not a failed run: " + actor.failed


def test_a_non_ppe_run_is_free_but_delivered():
    actor = FakeActor({"domains": ["a.com"]}, FakeCharging(ppe=False))
    app.Actor = actor
    app.lookup = fake_lookup_factory({"a.com": AVAILABLE})
    run(app.main())
    summary = actor.store["RUN_SUMMARY"]
    assert summary["chargedEvents"] == 0
    assert summary["billingActive"] is False
    assert actor.dataset.items[0]["status"] == "OK"


def test_empty_input_fails_before_the_dataset_is_opened():
    actor = FakeActor({"domains": ["   ", ""]})
    app.Actor = actor
    app.lookup = fake_lookup_factory({})
    run(app.main())
    assert "No domains" in actor.failed
    assert actor.dataset.items == []


def test_max_domains_cuts_with_a_reason():
    actor = setup(fake_lookup_factory({"a.com": AVAILABLE, "b.com": TAKEN}),
                  {"domains": ["a.com", "b.com"], "maxDomains": 1})
    statuses = [r["status"] for r in actor.dataset.items]
    assert statuses.count("OK") == 1 and "NOT_CHECKED_LIMIT" in statuses, statuses


def test_summary_is_valid_json():
    actor = setup(fake_lookup_factory({"a.com": AVAILABLE}), {"domains": ["a.com"]})
    json.dumps(actor.store["RUN_SUMMARY"])



def test_a_deadline_still_delivers_the_verdicts_it_already_has():
    """B2: a verdict that came back before the deadline is never thrown away."""
    actor = FakeActor({"domains": ["a.com", "b.com", "c.com"], "concurrency": 1}, FakeCharging())
    app.Actor = actor
    app.lookup = fake_lookup_factory({"a.com": AVAILABLE, "b.com": TAKEN, "c.com": TAKEN})
    ticks = [30.0, 30.0, 5.0, 5.0, 5.0, 5.0]
    original = app.seconds_left
    app.seconds_left = lambda: ticks.pop(0) if ticks else 5.0
    try:
        run(app.main())
    finally:
        app.seconds_left = original
    statuses = [r["status"] for r in actor.dataset.items]
    summary = actor.store["RUN_SUMMARY"]
    assert statuses.count("OK") == 1, statuses
    assert statuses.count("NOT_CHECKED_TIME") == 2, statuses
    assert summary["stopReason"] == "deadline"
    assert summary["chargedEvents"] == 1 and summary["rowsWritten"] == 3
    assert summary["counterMismatch"] is False


def test_an_unreadable_price_calls_no_registry_at_all():
    """The price is unknown, so the run sells nothing and asks nobody."""
    calls: list[str] = []

    async def counting_lookup(client, domain, timeout_s, mapping=None):
        calls.append(domain)
        return dict(AVAILABLE)

    actor = setup(counting_lookup, {"domains": ["a.com", "b.com"]}, FakeCharging(broken=True))
    summary = actor.store["RUN_SUMMARY"]
    assert calls == [], calls
    assert summary["stopReason"] == "billing-price-unknown"
    statuses = [r["status"] for r in actor.dataset.items]
    assert statuses == ["NOT_CHECKED_BILLING", "NOT_CHECKED_BILLING"], statuses
    assert summary["chargedEvents"] == 0 and summary["counterMismatch"] is False


def test_a_refused_free_row_does_not_kill_the_run():
    """A status row the dataset refuses is counted, not raised."""
    actor = FakeActor({"domains": ["a.com", "not a domain"]}, FakeCharging())
    app.Actor = actor
    app.lookup = fake_lookup_factory({"a.com": AVAILABLE})
    original = actor.dataset.push_data
    state = {"free_seen": False}

    async def refuse_free(data):
        if isinstance(data, dict) and data.get("status") == "INVALID":
            state["free_seen"] = True
            raise RuntimeError("the dataset refused the status row")
        await original(data)

    actor.dataset.push_data = refuse_free
    run(app.main())
    summary = actor.store["RUN_SUMMARY"]
    assert state["free_seen"] is True
    assert summary["writeErrors"] == 1
    assert summary["chargedEvents"] == 1


def test_the_tail_of_a_money_stop_is_free_cap_not_error():
    """The reviewer's stand: 60 domains with a cap of 1 gives 10 CAP rows, not ERROR."""
    domains = [f"d{i}.com" for i in range(60)]
    verdicts = {name: AVAILABLE for name in domains}
    actor = setup(fake_lookup_factory(verdicts), {"domains": domains}, FakeCharging(limit=1))
    statuses = [r["status"] for r in actor.dataset.items]
    summary = actor.store["RUN_SUMMARY"]
    assert statuses.count("OK") == 1, statuses.count("OK")
    assert statuses.count("NOT_CHECKED_CAP") == 59, statuses.count("NOT_CHECKED_CAP")
    assert "NOT_CHECKED_ERROR" not in statuses, "a money stop is not an error"
    assert summary["stopReason"] == "spending-cap"
    assert summary["counterMismatch"] is False


def test_the_status_message_counts_lines_not_attempts():
    """The header used to say "0 not checked" while two lines had no verdict."""
    actor = FakeActor({"domains": ["a.com", "b.com", "c.com"], "concurrency": 1}, FakeCharging())
    app.Actor = actor
    app.lookup = fake_lookup_factory({"a.com": AVAILABLE, "b.com": TAKEN, "c.com": TAKEN})
    ticks = [30.0, 30.0, 5.0, 5.0, 5.0, 5.0]
    original = app.seconds_left
    app.seconds_left = lambda: ticks.pop(0) if ticks else 5.0
    try:
        run(app.main())
    finally:
        app.seconds_left = original
    assert "3 line(s)" in actor.status, actor.status
    assert "1 domain(s) checked and charged" in actor.status, actor.status
    assert "2 not checked" in actor.status, actor.status


TESTS = [(name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def run_all() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__} {exc}")
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)