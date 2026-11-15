"""Money, counters and cancellation of the IBAN run. Stand-in Actor shaped like apify 4.2.0."""

import asyncio
import os
import sys
import types
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# Taken before the stand-in Actor replaces sys.modules["apify"]: the price object
# must be the real dataclass, not my idea of it.
try:
    from apify._charging import ActorPricingInfo as RealActorPricingInfo
except Exception:  # noqa: BLE001
    RealActorPricingInfo = None


class ChargeResult:
    def __init__(self, charged_count, event_charge_limit_reached):
        self.charged_count = charged_count
        self.event_charge_limit_reached = event_charge_limit_reached


class FakeDataset:
    def __init__(self, owner):
        self.owner = owner

    async def get_metadata(self):
        try:
            from crawlee.storage_clients.models import DatasetMetadata as RealDatasetMetadata
        except Exception:  # noqa: BLE001
            return {"item_count": len(self.owner.rows)}
        now = datetime.now(timezone.utc)
        return RealDatasetMetadata(
            id="0000000000000000fake",
            name="fake-dataset",
            accessed_at=now,
            created_at=now,
            modified_at=now,
            item_count=len(self.owner.rows),
        )


class _Log:
    """Actor.log is a plain logger: the code calls .info/.warning/.error on it."""

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    error = warning
    debug = warning


class ActorMeta(type):
    """The real class is used as "async with Actor:": that needs a metaclass, not a classmethod."""

    async def __aenter__(cls):
        return cls

    async def __aexit__(cls, exc_type, exc, tb):
        return False


class FakeActor(metaclass=ActorMeta):
    log = _Log()
    input = {}
    rows = []
    plan = []
    charged = 0
    status_messages = []
    failed = None
    values = {}
    summary = {}
    pricing = None
    charged_calls = 0

    @classmethod
    def reset(cls, actor_input, plan=None, pricing="default"):
        cls.input = actor_input
        cls.rows = []
        cls.plan = list(plan or [])
        cls.charged = 0
        cls.status_messages = []
        cls.failed = None
        cls.values = {}
        cls.summary = {}
        cls.charged_calls = 0
        if pricing == "default":
            cls.pricing = _pricing()
        else:
            cls.pricing = pricing

    @classmethod
    async def get_input(cls):
        return cls.input

    @classmethod
    async def open_dataset(cls):
        return FakeDataset(cls)

    @classmethod
    async def push_data(cls, row, *, charged_event_name=None):
        if not charged_event_name:
            cls.rows.append(dict(row))
            return None
        cls.charged_calls += 1
        outcome = cls.plan.pop(0) if cls.plan else "charged"
        if outcome == "cap":
            return ChargeResult(0, True)
        if outcome == "lost":
            raise RuntimeError("the dataset refused the write")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        cls.rows.append(dict(row))
        if outcome == "raise":
            raise RuntimeError("charge exploded after the write")
        if outcome == "charged":
            cls.charged += 1
            return ChargeResult(1, False)
        return ChargeResult(0, False)

    @classmethod
    async def set_value(cls, key, value):
        cls.values[key] = value
        if key == "RUN_SUMMARY":
            cls.summary = value

    class _NamedStore:
        """Named store is shared by the whole account, so it is recorded under its own prefix."""

        def __init__(self, outer):
            self.outer = outer

        async def set_value(self, key, value):
            self.outer.values["named:" + key] = value
            self.outer.summary = value

    @classmethod
    async def open_key_value_store(cls, name=None):
        return cls._NamedStore(cls)

    @classmethod
    async def set_status_message(cls, message, level=None):
        cls.status_messages.append(message)

    @classmethod
    async def fail(cls, *, status_message=None, **kwargs):
        cls.failed = status_message
        raise SystemExit(status_message)

    @classmethod
    def get_env(cls):
        return {"run_id": "TESTRUN"}

    @classmethod
    def get_charging_manager(cls):
        outer = cls

        class Manager:
            def get_pricing_info(self):
                # SDK 4.2.0: get_charging_manager() and get_pricing_info() are SYNCHRONOUS.
                # An async fake hid a real defect: `await` on a dataclass raised TypeError,
                # the price was "unknown" and every row went out free (billable null).
                return outer.pricing

        return Manager()


def _pricing(events=("iban-checked",)):
    """The real ActorPricingInfo when it is available, so the attribute path is really executed."""
    if RealActorPricingInfo is None:
        return {"pricingPerEvent": {"actorChargeEvents": {name: {} for name in events}}}
    return RealActorPricingInfo(
        pricing_model="PAY_PER_EVENT",
        is_pay_per_event=True,
        per_event_prices={name: 0.005 for name in events},
        max_total_charge_usd=4.4,
    )


sys.modules["apify"] = types.SimpleNamespace(Actor=FakeActor)

import src.main as M  # noqa: E402

VALID = "DE89370400440532013000"
VALID2 = "GB82WEST12345698765432"
BROKEN = "ZZ89370400440532013000"


class Failure(AssertionError):
    pass


def _run(actor_input, plan=None, pricing="default"):
    FakeActor.reset(actor_input, plan=plan, pricing=pricing)
    try:
        asyncio.run(M.main())
    except SystemExit:
        pass
    except asyncio.CancelledError:
        pass          # the real Actor lets cancellation out; main re-raises it on purpose
    return FakeActor


def test_normal_run_writes_and_charges_in_one_call():
    fa = _run({"ibans": [VALID, VALID2]}, plan=["charged", "charged"])
    assert len(fa.rows) == 2, "delivered %d rows" % len(fa.rows)
    assert fa.charged == 2, "charged %d" % fa.charged
    assert all(r["billable"] is True for r in fa.rows), "billable must be true by price"
    assert fa.summary["billableIbans"] == 2
    assert fa.summary["stopReason"] == "completed"


def test_source_has_no_separate_charge_call():
    path = os.path.join(os.path.dirname(HERE), "src", "main.py")
    text = open(path, encoding="utf-8").read()
    assert ".charge(" not in text, "a separate charge() call is still in src/main.py"
    assert "charged_event_name=" in text, "push_data is not given the event name"


def test_cap_writes_nothing_and_stops():
    fa = _run({"ibans": [VALID, VALID2]}, plan=["charged", "cap"])
    assert len(fa.rows) == 2, "rows %d" % len(fa.rows)
    assert fa.rows[1]["status"] == "NOT_CHECKED_CAP", fa.rows[1].get("status")
    assert fa.rows[1]["billable"] is False
    assert fa.summary["cappedNotCharged"] == 1
    assert fa.summary["stopReason"] == "spending-cap"


def test_charge_after_write_is_delivered_uncharged():
    fa = _run({"ibans": [VALID, VALID2]}, plan=["raise"])
    assert len(fa.rows) == 2, "the written row plus a status row for the rest: %d" % len(fa.rows)
    assert fa.rows[1]["status"] == "NOT_CHECKED_ERROR", fa.rows[1].get("status")
    assert fa.summary["notCheckedError"] == 1, fa.summary.get("notCheckedError")
    assert fa.summary["deliveredUncharged"] == 1, fa.summary["deliveredUncharged"]
    # the claim and the fact are separate keys: one row was written with billable true,
    # nothing was actually charged because the charge failed after the write
    assert fa.summary["billableIbans"] == 1, fa.summary["billableIbans"]
    assert fa.summary["chargedEvents"] == 0, fa.summary["chargedEvents"]
    assert fa.summary["stopReason"] == "charge-error"
    assert fa.summary["chargeErrors"], "the failure must be named"


def test_price_unreadable_names_the_reason():
    """An unreadable price list used to look like a healthy run: billingActive true,
    zero charged, no errors. It must name itself instead."""
    fa = _run({"ibans": [VALID]}, plan=["charged"], pricing=None)
    s = fa.values["named:RUN_SUMMARY-TESTRUN"]
    assert s["pricingEventPriced"] is None, s["pricingEventPriced"]
    assert s["stopReason"] == "billing-price-unknown", s["stopReason"]
    # An unreadable price must not deliver the row either: handing a whole list out for
    # free is the bug this rule closes. The row becomes a free status row, the run stops.
    assert s["deliveredUncharged"] == 0, s["deliveredUncharged"]
    # the IBAN itself is arithmetically valid (the checksum ran before the price was read);
    # what the run refused is delivery and the charge, and that is what byStatus says
    assert s["byStatus"] == {"NOT_CHECKED_BILLING": 1}, s["byStatus"]
    assert s["notCheckedError"] == 1, s["notCheckedError"]
    assert fa.rows[0]["status"] == "NOT_CHECKED_BILLING", fa.rows[0]["status"]
    assert s["chargedEvents"] == 0, s["chargedEvents"]
    assert s["billableIbans"] == 0, s["billableIbans"]
    # billingActive true next to zero charges is the monitor reading "billing works"
    # where nothing was billed: the honest value is None (unknown), not True
    assert s["billingActive"] is None, s["billingActive"]


def test_row_with_zero_charged_is_delivered_uncharged():
    """SDK wrote the row and took nothing (billing off): the summary must count it as
    delivered-uncharged, the same way phone-number-validator does."""
    fa = _run({"ibans": [VALID]}, plan=["zero"])
    s = fa.values["named:RUN_SUMMARY-TESTRUN"]
    assert s["deliveredUncharged"] == 1, s["deliveredUncharged"]
    assert s["chargedEvents"] == 0, s["chargedEvents"]
    assert s["billingActive"] is False, s["billingActive"]
    assert s["stopReason"] == "billing-not-active", s["stopReason"]


def test_free_row_is_not_charged():
    fa = _run({"ibans": [BROKEN]}, plan=["charged"])
    assert len(fa.rows) == 1
    assert fa.charged_calls == 0, "a free row must not touch the billing path"
    assert fa.rows[0]["billable"] is False


def test_billable_is_null_when_price_unreadable():
    fa = _run({"ibans": [VALID]}, plan=["charged"], pricing=None)
    assert fa.rows[0]["billable"] is None, fa.rows[0]["billable"]
    assert fa.rows[0]["status"] == "NOT_CHECKED_BILLING", fa.rows[0]["status"]
    assert fa.charged_calls == 0, "an unreadable price must not charge blindly"


def test_summary_written_under_both_keys_with_run_id():
    fa = _run({"ibans": [VALID]})
    assert set(fa.values) == {"RUN_SUMMARY", "named:RUN_SUMMARY-TESTRUN"}, sorted(fa.values)
    assert "named:RUN_SUMMARY" not in fa.values, \
        "the shared named store must not carry a plain key: parallel runs would overwrite it"
    assert fa.values["named:RUN_SUMMARY-TESTRUN"]["runId"] == "TESTRUN"


def test_cancellation_leaves_a_summary():
    fa = _run({"ibans": [VALID, VALID2]}, plan=["cancel"])
    assert fa.values.get("named:RUN_SUMMARY-TESTRUN"), "no summary after cancellation"
    s = fa.values["named:RUN_SUMMARY-TESTRUN"]
    assert s["cancelled"] is True, s.get("cancelled")
    assert s["stopReason"] == "cancelled", s.get("stopReason")
    assert any("Cancelled after" in m for m in fa.status_messages), fa.status_messages


def test_ask_and_delivered_close_on_cancel():
    fa = _run({"ibans": [VALID, VALID2, BROKEN]}, plan=["charged", "cancel"])
    s = fa.values["named:RUN_SUMMARY-TESTRUN"]
    assert s["asked"] == s["ibans"] + s["cappedNotCharged"], s


def test_successful_run_says_what_happened_in_the_header():
    fa = _run({"ibans": [VALID, "NOTANIBAN"]}, plan=["charged", "free"])
    assert fa.status_messages, "the run header stayed empty on a successful run"
    msg = fa.status_messages[-1]
    assert "2 IBAN line(s) read" in msg, msg
    assert "1 valid" in msg, msg
    assert "1 charged event(s)" in msg, msg


def test_successful_header_does_not_end_with_an_empty_stop():
    """On success stop_reason stays an empty string: the header must not print "Stop: ."."""
    fa = _run({"ibans": [VALID]}, plan=["charged"])
    msg = fa.status_messages[-1]
    assert "Stop:" not in msg, msg


def test_billing_off_is_named_instead_of_completed():
    """The price list is readable but our event is not in it: billing is off for the run.
    Rows are delivered free and the summary must say billing-not-active, not completed."""
    fa = _run({"ibans": [VALID, VALID2]}, plan=["charged", "charged"],
              pricing=_pricing(events=("other-event",)))
    s = fa.values["named:RUN_SUMMARY-TESTRUN"]
    assert s["pricingEventPriced"] is False, s["pricingEventPriced"]
    assert s["billingActive"] is False, s["billingActive"]
    assert s["stopReason"] == "billing-not-active", s["stopReason"]
    assert fa.charged_calls == 0, "nothing may be charged when billing is off"
    assert all(r["billable"] is False for r in fa.rows), [r["billable"] for r in fa.rows]


TESTS = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]


def run_all():
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %s" % (name, exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
        else:
            print("ok   %s" % name)
    print("%d/%d passed" % (len(TESTS) - failed, len(TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_all())
