"""Money and counters of the Recruitee Actor, on a stand-in Actor that follows the real SDK."""

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from src import main as main_mod  # noqa: E402
from src.client import BoardError  # noqa: E402

FAILS = []

try:  # the real class the SDK returns, so the attribute path is exercised, not a dict
    from crawlee.storage_clients.models import DatasetMetadata as _RealMeta
except Exception:  # pragma: no cover
    _RealMeta = None

try:
    from apify._charging import ActorPricingInfo as _RealPricing
except Exception:  # pragma: no cover
    _RealPricing = None


def check(name, got, want):
    if got != want:
        FAILS.append("%s: got %r, want %r" % (name, got, want))


def offers(n=3):
    rows = []
    for index in range(n):
        rows.append({
            "id": 1000 + index,
            "title": "Engineer %d" % index,
            "department": "Platform",
            "location": "Vienna, Wien, Austria",
            "city": "Vienna",
            "country": "Austria",
            "country_code": "AT",
            "state_name": "Wien",
            "locations": [{"city": "Vienna", "country": "Austria", "country_code": "AT", "state": "Wien"}],
            "remote": index == 0,
            "hybrid": False,
            "on_site": True,
            "employment_type_code": "fulltime_permanent",
            "experience_code": "mid_level",
            "category_code": "banking",
            "tags": ["Backend"],
            "salary": {"min": "1,299", "max": "2,500.50", "currency": "EUR", "period": "month"},
            "published_at": "2026-10-30 10:00:0%d UTC" % index,
            "careers_url": "https://careers.example.com/o/%d" % (1000 + index),
            "careers_apply_url": "https://careers.example.com/o/%d/c/new" % (1000 + index),
            "description": "<p>Build things.</p>",
            "requirements": "<ul><li>Python</li></ul>",
        })
    return rows


class Charge:
    def __init__(self, charged, capped):
        self.charged_count = charged
        self.event_charge_limit_reached = capped
        self.chargeable_within_limit = charged


class Dataset:
    def __init__(self, rows):
        self.rows = rows

    async def get_metadata(self):
        count = len(self.rows)
        if _RealMeta is not None:
            return _RealMeta.model_construct(
                id="test", name="test", accessed_at="2026-01-01T00:00:00Z",
                created_at="2026-01-01T00:00:00Z", modified_at="2026-01-01T00:00:00Z",
                item_count=count,
            )
        return type("Meta", (), {"item_count": count})()


class Store:
    def __init__(self):
        self.data = {}

    async def set_value(self, key, value):
        self.data[key] = value


class Manager:
    def get_pricing_info(self):
        if _RealPricing is not None:
            return _RealPricing(pricing_model="PAY_PER_EVENT", is_pay_per_event=True,
                                per_event_prices={"job-delivered": 0.0015},
                                max_total_charge_usd=4.4)
        return type("P", (), {"is_pay_per_event": True, "per_event_prices": {"job-delivered": 0.0015}})()


class FakeActor:
    rows = []
    status_calls = []
    values = {}
    stores = {}
    failed = None
    input = {}
    billing = True
    cap_after = None
    charge_calls = 0

    def __init__(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @classmethod
    def reset(cls, actor_input, billing=True, cap_after=None):
        cls.rows = []
        cls.status_calls = []
        cls.values = {}
        cls.stores = {}
        cls.failed = None
        cls.input = actor_input
        cls.billing = billing
        cls.cap_after = cap_after
        cls.charge_calls = 0
        return cls

    @classmethod
    async def get_input(cls):
        return cls.input

    @classmethod
    async def push_data(cls, row, charged_event_name=None):
        if charged_event_name and cls.cap_after is not None and cls.charge_calls >= cls.cap_after:
            return Charge(0, True)
        cls.rows.append(row)
        if charged_event_name and cls.billing:
            cls.charge_calls += 1
            return Charge(1, False)
        return Charge(0, False)

    @classmethod
    async def open_dataset(cls):
        return Dataset(cls.rows)

    @classmethod
    async def set_value(cls, key, value):
        cls.values[key] = value

    @classmethod
    async def open_key_value_store(cls, name=None):
        store = Store()
        cls.stores[name] = store
        return store

    @classmethod
    async def set_status_message(cls, message):
        cls.status_calls.append(message)

    @classmethod
    async def fail(cls, *, exit_code=1, exception=None, status_message=None):
        cls.failed = status_message or "failed"

    @classmethod
    def get_charging_manager(cls):
        return Manager()

    @classmethod
    def get_env(cls):
        return {"run_id": "test-run"}


def run(actor_input, boards=None, billing=True, cap_after=None):
    FakeActor.reset(actor_input, billing=billing, cap_after=cap_after)
    if boards is not None:
        def fake_fetch(slug, timeout=60):
            value = boards[slug]
            if isinstance(value, Exception):
                raise value
            return value
        main_mod.fetch_board = fake_fetch
    main_mod.Actor = FakeActor()  # an instance: `async with Actor` needs __aenter__ on the object
    asyncio.run(main_mod.main())
    return FakeActor.values.get("RUN_SUMMARY") or {}


def test_normal_run():
    board = {"jobs": offers(3)}
    s = run({"boards": ["bunq"], "includeDetails": True}, {"bunq": board})
    check("delivered", s["delivered"], 3)
    check("charged", s["chargedEvents"], 3)
    check("status rows", s["statusRows"], 0)
    check("dataset count", s["datasetItemCount"], 3)
    check("no mismatch", s.get("countersMismatch"), None)
    check("no truncated mismatch", s.get("truncatedMismatch"), None)
    check("billable true", FakeActor.rows[0]["billable"], True)
    check("salary number", FakeActor.rows[0]["salaryMin"], 1299.0)
    check("summary store per run", list(FakeActor.stores.get("recruitee-jobs-api-summary").data), ["RUN_SUMMARY-test-run"])
    check("status message set", len(FakeActor.status_calls), 1)
    check("did not fail", FakeActor.failed, None)


def test_cap():
    board = {"jobs": offers(3)}
    s = run({"boards": ["bunq"]}, {"bunq": board}, cap_after=1)
    check("cap delivered", s["delivered"], 1)
    check("cap charged", s["chargedEvents"], 1)
    check("cap stop", s["stopReason"], "spending-cap")
    check("cap not processed", s["positionsNotProcessed"], 2)
    check("cap rows in dataset", len(FakeActor.rows), 3)  # 1 real + 2 status rows
    check("cap statuses", s["byStatus"].get("NOT_CHECKED_CAP"), 2)
    check("cap mismatch", s.get("countersMismatch"), None)


def test_not_found_board():
    s = run({"boards": ["zzzz"]}, {"zzzz": BoardError("not_found", "404")})
    check("notfound boards", s["boardsNotFound"], 1)
    check("notfound delivered", s["delivered"], 0)
    check("notfound charge", s["chargedEvents"], 0)
    check("notfound status", s["byStatus"].get("NOT_CHECKED_BOARD_NOT_FOUND"), 1)
    check("notfound fails the run", bool(FakeActor.failed), True)
    check("notfound message names it", "do not exist" in (FakeActor.failed or ""), True)


def test_empty_board():
    s = run({"boards": ["bunq"]}, {"bunq": {"jobs": [], "empty": True}})
    check("empty boards", s["boardsEmpty"], 1)
    check("empty status", s["byStatus"].get("NOT_CHECKED_BOARD_EMPTY"), 1)
    check("empty charge", s["chargedEvents"], 0)


def test_filter_no_match():
    s = run({"boards": ["bunq"], "city": "Berlin"}, {"bunq": {"jobs": offers(3)}})
    check("no match status", s["byStatus"].get("NOT_CHECKED_NO_MATCH"), 1)
    check("no match delivered", s["delivered"], 0)
    check("no match filtered", s["filteredOut"], 3)
    check("no match mismatch", s.get("countersMismatch"), None)


def test_limit():
    s = run({"boards": ["bunq"], "maxJobsPerBoard": 2}, {"bunq": {"jobs": offers(3)}})
    check("limit delivered", s["delivered"], 2)
    check("limit not read", s["positionsNotRead"], 1)
    check("limit truncated", s["truncatedBoards"], 1)
    check("limit mismatch", s.get("countersMismatch"), None)


def test_empty_input():
    s = run({"boards": []})
    check("no input fails", bool(FakeActor.failed), True)
    check("no input message", "board identifier" in (FakeActor.failed or ""), True)
    check("no input rows", len(FakeActor.rows), 0)


def test_free_run_when_billing_off():
    s = run({"boards": ["bunq"]}, {"bunq": {"jobs": offers(2)}}, billing=False)
    check("free delivered", s["delivered"], 2)
    check("free charged", s["chargedEvents"], 0)
    check("free uncharged", s["deliveredUncharged"], 2)
    check("free billing flag", s["billingActive"], False)


def run_all():
    FAILS.clear()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("%d check(s) failed" % len(FAILS))
    for line in FAILS:
        print("  FAIL", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run_all())
