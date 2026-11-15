"""Charging and delivery tests with a fake Actor: money and rows must agree.

The fake mirrors apify 4.2.0 `Actor.push_data(data, *, charged_event_name=...)`:
the SDK decides how many items fit the budget, writes them, and only then bills
under one lock. So a row is written when the budget allows it, and a billing
exception leaves the row in the dataset.

Run: python tests/test_main.py
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apify._charging import ActorPricingInfo  # noqa: E402  real dataclass, not a dict

import src.main as main_mod  # noqa: E402


class FakeFail(Exception):
    pass


class FakeCharge:
    """Mirror of apify 4.2.0 ChargeResult.

    (n, True)  - n charged, no budget left;
    (0, True)  - budget strictly exhausted: the SDK wrote NOTHING;
    (0, False) - run is not pay-per-event: the row WAS written, nothing billed.
    """

    def __init__(self, charged_count=1, limit_reached=False):
        self.charged_count = charged_count
        self.event_charge_limit_reached = limit_reached


class FakeStore:
    def __init__(self, sink):
        self.sink = sink

    async def set_value(self, key, value):
        self.sink[key] = value


class FakeLog:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class FakeCharging:
    def __init__(self, priced=True):
        self._priced = priced
        self._info = ActorPricingInfo(
            pricing_model="PAY_PER_EVENT",
            max_total_charge_usd=Decimal("4.4"),
            is_pay_per_event=True,
            per_event_prices=({"number-validated": Decimal("0.0008")}
                              if priced is not False else {}),
        )

    def get_pricing_info(self):
        if self._priced is None:
            # price list unreachable: the run must name that, not report "completed"
            raise RuntimeError("price list unavailable")
        # SDK 4.2.0: this is SYNCHRONOUS. An async fake hid a real defect where `await`
        # raised TypeError, the price looked unknown and every valid row went out free.
        return self._info


class FakeActor:
    def __init__(self, payload, charges=None, priced=True, cancel_at=None):
        self.cancel_at = cancel_at
        self.payload = payload
        self.charges = list(charges or [])
        self.pushed = []
        self.kv = {}
        self.charge_calls = 0
        self.log = FakeLog()
        self._charging = FakeCharging(priced)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get_input(self):
        return self.payload

    def get_env(self):
        return {"run_id": "TESTRUN"}

    def get_charging_manager(self):
        return self._charging

    async def push_data(self, data, *, charged_event_name=None):
        if self.cancel_at is not None and len(self.pushed) + 1 >= self.cancel_at:
            raise asyncio.CancelledError()
        if charged_event_name is None:
            self.pushed.append(dict(data))
            return FakeCharge(0, False)
        self.charge_calls += 1
        item = self.charges.pop(0) if self.charges else FakeCharge(1, False)
        if isinstance(item, Exception):
            # the SDK writes the row first and bills after: the row stays in the dataset
            self.pushed.append(dict(data))
            raise item
        if item.charged_count <= 0 and item.event_charge_limit_reached:
            # compute_push_data_limit returned 0: nothing written, nothing billed
            return item
        self.pushed.append(dict(data))
        return item

    async def set_value(self, key, value):
        self.kv["run:" + key] = value

    async def set_status_message(self, message, is_terminal=None):
        self.kv["status"] = message

    async def open_key_value_store(self, name=None):
        return FakeStore(self.kv)

    async def fail(self, *, exit_code=1, exception=None, status_message=None):
        raise FakeFail(status_message or '')


def run(payload, charges=None, priced=True, cancel_at=None):
    fake = FakeActor(payload, charges, priced, cancel_at)
    main_mod.Actor = fake
    failure = None
    try:
        asyncio.run(main_mod.main())
    except FakeFail as exc:
        failure = str(exc)
    except asyncio.CancelledError:
        pass                                  # main() wrote the summary and re-raised
    return fake, failure


def test_valid_rows_charged_and_delivered():
    fake, failure = run({"numbers": ["+1 202 555 0173", "+44 20 7946 0018"]})
    assert failure is None, failure
    assert [row["status"] for row in fake.pushed] == ["VALID", "VALID"]
    assert fake.charge_calls == 2
    assert all(row["billable"] for row in fake.pushed)
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["chargedEvents"] == 2
    assert summary["uniqueValidNumbers"] == 2
    assert summary["billingActive"] is True
    assert summary["stopReason"] == "completed"
    assert len(fake.pushed) == 2


def test_trunk_bracket_line_is_valid_and_charged_once():
    fake, failure = run({"numbers": ["+34 (0) 91 123 45 67", "+34 91 123 45 67"]})
    assert failure is None, failure
    assert fake.pushed[0]["status"] == "VALID", fake.pushed[0]["reason"]
    assert fake.pushed[0]["e164"] == "+34911234567"
    assert fake.pushed[1]["duplicateOfIndex"] == 1
    assert fake.charge_calls == 1


def test_invalid_rows_are_free():
    fake, failure = run({"numbers": ["junk line", "+1 202 555 0173", "+999 123456789"]})
    assert failure is None, failure
    assert fake.charge_calls == 1
    assert fake.pushed[0]["billable"] is False
    assert fake.pushed[1]["billable"] is True
    assert fake.pushed[2]["billable"] is False
    assert fake.pushed[2]["status"] == "INVALID"
    assert "is not recognised" in fake.pushed[2]["reason"]


def test_duplicate_charged_once():
    fake, failure = run({"numbers": ["+1 202 555 0173", "+1 (202) 555-0173"]})
    assert failure is None, failure
    assert fake.charge_calls == 1
    assert fake.pushed[0]["duplicateOfIndex"] is None
    assert fake.pushed[1]["duplicateOfIndex"] == 1
    assert fake.pushed[1]["billable"] is False
    assert "Duplicate of line 1" in fake.pushed[1]["reason"]
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["duplicateRows"] == 1
    assert summary["chargedEvents"] == 1


def test_cap_writes_no_paid_row_and_delivers_status_rows():
    # (0, True): the budget is strictly gone, the SDK wrote NOTHING for this line.
    fake, failure = run({"numbers": ["+1 202 555 0173", "+44 20 7946 0018",
                                     "+380 44 123 45 67"]},
                        charges=[FakeCharge(0, True)])
    assert failure is None, failure
    assert [row["status"] for row in fake.pushed] == ["NOT_CHECKED_CAP"] * 3
    assert all(row["billable"] is False for row in fake.pushed)
    assert fake.charge_calls == 1
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["stopReason"] == "spending-cap"
    assert summary["cappedNotCharged"] == 3
    assert summary["chargedEvents"] == 0
    assert summary["byStatus"].get("VALID") is None


def test_non_pay_per_event_run_delivers_every_row_free():
    # The price list has no such event: nothing can be billed, so no charge is even attempted.
    numbers = ["+1 202 555 0173", "+44 20 7946 0018", "+380 44 123 45 67"]
    fake, failure = run({"numbers": numbers}, priced=False)
    assert failure is None, failure
    assert [row["status"] for row in fake.pushed] == ["VALID"] * 3
    assert all(row["billable"] is False for row in fake.pushed)
    assert all(row["billingInactive"] is True for row in fake.pushed)
    assert fake.charge_calls == 0
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["billingActive"] is False
    assert summary["stopReason"] == "billing-not-active"
    assert summary["cappedNotCharged"] == 0
    assert summary["chargedEvents"] == 0
    assert summary["deliveredUncharged"] == 0      # nothing was attempted, so nothing is "uncharged"
    assert summary["uniqueValidNumbers"] == 3
    assert summary["byStatus"].get("NOT_CHECKED_CAP") is None


def test_priced_event_that_takes_no_money_counts_delivered_uncharged():
    # (0, False) on a PRICED event: the row is written, no money is taken, billing stops.
    numbers = ["+1 202 555 0173", "+44 20 7946 0018"]
    fake, failure = run({"numbers": numbers}, charges=[FakeCharge(0, False)])
    assert failure is None, failure
    assert [row["status"] for row in fake.pushed] == ["VALID"] * 2
    assert fake.charge_calls == 1                 # billing is off: no further attempts
    assert fake.pushed[1]["billable"] is False    # decided before the write
    assert fake.pushed[1]["billingInactive"] is True
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["chargedEvents"] == 0
    assert summary["deliveredUncharged"] == 1
    assert summary["billingActive"] is False
    assert summary["stopReason"] == "billing-not-active"


def test_limit_reached_charges_this_event_then_stops():
    fake, failure = run({"numbers": ["+1 202 555 0173", "+44 20 7946 0018"]},
                        charges=[FakeCharge(1, True)])
    assert failure is None, failure
    assert fake.pushed[0]["billable"] is True
    assert fake.pushed[1]["status"] == "NOT_CHECKED_CAP"
    assert fake.charge_calls == 1
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["chargedEvents"] == 1
    assert summary["cappedNotCharged"] == 1
    assert summary["stopReason"] == "spending-cap"


def test_billing_exception_delivers_row_and_stops():
    fake, failure = run({"numbers": ["+1 202 555 0173", "+44 20 7946 0018",
                                     "+380 44 123 45 67"]},
                        charges=[RuntimeError("boom")])
    assert failure is None, failure
    assert fake.pushed[0]["status"] == "VALID"
    # the row was written before the charge: the price claim stays, the summary has the truth
    assert fake.pushed[0]["billable"] is True
    assert fake.pushed[1]["status"] == "NOT_CHECKED_BILLING"
    assert fake.pushed[2]["status"] == "NOT_CHECKED_BILLING"
    assert "billing failed" in fake.pushed[1]["reason"]
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["stopReason"] == "charge-error"
    assert summary["deliveredUncharged"] == 1
    assert len(summary["chargeErrors"]) == 1


def test_multi_number_row_is_one_event_and_extra_numbers_free():
    # Money follows delivery: one delivered row, one event. Numbers found inside the
    # same line beyond the first ride free, and the summary says so.
    fake, failure = run({"numbers": ["Call +1 202 555 0173 or +44 20 7946 0018"]})
    assert failure is None, failure
    assert fake.charge_calls == 1
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["chargedEvents"] == 1
    assert summary["extraNumbersFree"] == 1
    assert sum(len(r.get("otherNumbers") or []) for r in fake.pushed) == 1


def test_text_used_only_when_numbers_empty():
    fake, failure = run({"numbers": ["+1 202 555 0173"],
                         "text": "+44 20 7946 0018\n+380 44 123 45 67"})
    assert failure is None, failure
    assert len(fake.pushed) == 1
    assert fake.pushed[0]["e164"] == "+12025550173"


def test_empty_input_fails_loudly():
    fake, failure = run({"numbers": [], "text": "   "})
    assert failure is not None
    assert "No phone numbers given" in failure
    assert fake.pushed == []


def test_bad_default_country_fails():
    fake, failure = run({"numbers": ["044 123 45 67"], "defaultCountry": "Ukraine"})
    assert failure is not None
    assert "not an ISO-2" in failure
    assert fake.pushed == []


def test_default_country_makes_local_numbers_valid():
    fake, failure = run({"numbers": ["044 123 45 67"], "defaultCountry": "ua"})
    assert failure is None, failure
    assert fake.pushed[0]["status"] == "VALID"
    assert fake.pushed[0]["e164"] == "+380441234567"
    assert fake.kv["RUN_SUMMARY-TESTRUN"]["defaultCountry"] == "UA"


def test_order_and_echo_preserved():
    lines = ["  +1 202 555 0173  ", "junk", "+44 20 7946 0018"]
    fake, failure = run({"numbers": lines})
    assert failure is None, failure
    assert [row["index"] for row in fake.pushed] == [1, 2, 3]
    assert [row["inputAsGiven"] for row in fake.pushed] == lines


def test_blank_cell_keeps_line_numbers():
    fake, failure = run({"numbers": ["+1 202 555 0173", "", "+44 20 7946 0018"]})
    assert failure is None, failure
    assert [row["index"] for row in fake.pushed] == [1, 2, 3]
    assert fake.pushed[1]["status"] == "NO_NUMBER"
    assert fake.pushed[1]["inputAsGiven"] == ""
    assert fake.pushed[2]["e164"] == "+442079460018"


def test_tail_over_limit_is_reported_not_dropped():
    original = main_mod.MAX_ROWS
    main_mod.MAX_ROWS = 2
    try:
        fake, failure = run({"numbers": ["+1 202 555 0173", "+44 20 7946 0018",
                                         "+380 44 123 45 67", "+49 30 901820"]})
    finally:
        main_mod.MAX_ROWS = original
    assert failure is None, failure
    assert [row["status"] for row in fake.pushed] == ["VALID", "VALID",
                                                     "NOT_CHECKED_LIMIT", "NOT_CHECKED_LIMIT"]
    assert fake.charge_calls == 2
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["truncatedLines"] == 2
    assert summary["extraLinesReported"] == 2
    assert summary["inputLines"] == 4


def test_price_unreadable_names_the_reason():
    # Unreadable price list: rows are delivered free. Reporting a healthy "completed"
    # run here is the exact shape of the live billing defect, so the reason is named.
    fake, failure = run({"numbers": ["+1 202 555 0173"]}, priced=None)
    assert failure is None, failure
    assert fake.pushed[0]["billable"] is None, fake.pushed[0]["billable"]
    assert fake.charge_calls == 0, fake.charge_calls
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["pricingEventPriced"] is None, summary["pricingEventPriced"]
    assert summary["stopReason"] == "billing-price-unknown", summary["stopReason"]
    assert summary["chargedEvents"] == 0, summary["chargedEvents"]
    # An unreadable price must not deliver the line either (F1): the line is a free status
    # row and the run stops, instead of a whole list going out for free.
    assert summary["deliveredUncharged"] == 0, summary["deliveredUncharged"]
    # the number parsed fine; what the run refused is delivery and the charge, and that
    # is exactly what byStatus reports
    assert summary["byStatus"] == {"NOT_CHECKED_BILLING": 1}, summary["byStatus"]
    assert summary["notCheckedError"] == 1, summary["notCheckedError"]
    assert fake.pushed[0]["status"] == "NOT_CHECKED_BILLING", fake.pushed[0]["status"]
    # billingActive true beside zero charges reads as "billing works" where nothing was
    # billed; the honest value is None (unknown)
    assert summary["billingActive"] is None, summary["billingActive"]


def test_summary_written_to_named_store_with_run_id():
    fake, failure = run({"numbers": ["+1 202 555 0173"]})
    assert failure is None, failure
    # the named store is shared by the whole account: only the run-scoped key is written,
    # so two parallel runs can never read each other's summary
    assert "RUN_SUMMARY" not in fake.kv
    assert "RUN_SUMMARY-TESTRUN" in fake.kv               # named store, run-scoped key only
    assert "run:RUN_SUMMARY" in fake.kv                   # run store keeps the plain key
    assert fake.kv["RUN_SUMMARY-TESTRUN"]["rows"] == 1
    assert fake.kv["RUN_SUMMARY-TESTRUN"]["runId"] == "TESTRUN"


def test_partial_summary_mentions_partial_flag():
    fake, failure = run({"numbers": ["+1 202 555 0173"]})
    assert failure is None, failure
    assert fake.kv["RUN_SUMMARY-TESTRUN"]["partial"] is False


def test_cancellation_writes_summary_and_status():
    # a user presses Abort after the first row: the summary must exist and say so
    fake, failure = run({"numbers": ["+1 202 555 0173", "+44 20 7946 0018",
                                     "+380 44 123 45 67"]}, cancel_at=2)
    assert failure is None, failure
    summary = fake.kv["RUN_SUMMARY-TESTRUN"]
    assert summary["cancelled"] is True
    assert summary["stopReason"] == "cancelled"
    assert summary["partial"] is True
    assert summary["chargedEvents"] == 1          # the row delivered before the abort
    assert len(fake.pushed) == 1
    assert "Cancelled after" in fake.kv["status"]


def test_source_has_no_separate_billing_call():
    src = Path(main_mod.__file__).read_text(encoding="utf-8")
    assert ".charge(" not in src, "a separate billing call must not exist: money follows delivery"
    assert "charged_event_name=CHARGE_EVENT" in src


def test_successful_run_says_what_happened_in_the_header():
    fake, failure = run({"numbers": ["+1 202 555 0173"]})
    assert failure is None, failure
    msg = fake.kv.get("status")
    assert msg, "the run header stayed empty on a successful run"
    assert "1 line(s) read" in msg, msg
    assert "1 charged event(s)" in msg, msg


def test_successful_header_does_not_end_with_an_empty_stop():
    # stop_reason stays an empty string on success: the header must not print "Stop: ."
    fake, failure = run({"numbers": ["+1 202 555 0173"]})
    assert failure is None, failure
    msg = fake.kv.get("status")
    assert "Stop:" not in msg, msg


def run_all():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %s" % (test.__name__, exc))
        except Exception as exc:                                 # noqa: BLE001
            failed += 1
            print("ERROR %s: %s: %s" % (test.__name__, type(exc).__name__, exc))
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
