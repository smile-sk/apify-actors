"""Checks for the run loop: what is charged, what is free, and whether the counters add up.

The stand-in for the Actor repeats the SDK contract measured on apify 4.2.0, not my
belief about it: push_data stores the row before it charges, and at the spending cap
the SDK writes nothing at all.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import main, sc  # noqa: E402


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
        self._prices = {"job-delivered": 0.0015} if prices is None else prices

    def get_pricing_info(self):
        class Info:
            pass
        info = Info()
        info.is_pay_per_event = self._is_ppe
        info.per_event_prices = self._prices
        return info


class FakeActor:
    def __init__(self, mode="normal", charging=None):
        self.mode = mode
        self.rows = []
        self.calls = []
        self.charging = charging or FakeChargingManager()

    async def push_data(self, row, charged_event_name=None):
        self.calls.append((row.get("jobId") or row.get("company"), charged_event_name))
        if self.mode == "raise_after_write":
            self.rows.append(row)
            raise RuntimeError("charge failed after the row was written")
        if self.mode == "cap":
            return FakeChargeResult(0, True)
        self.rows.append(row)
        if self.mode == "free":
            return FakeChargeResult(0, False)
        return FakeChargeResult(1, False)

    async def open_dataset(self):
        return FakeDataset(self)

    def get_charging_manager(self):
        return self.charging


class FakeKV:
    def __init__(self):
        self.data = {}

    async def get_value(self, key):
        return self.data.get(key)

    async def set_value(self, key, value):
        self.data[key] = value


class RunActor:
    """The SDK surface main() touches, for one run."""

    def __init__(self, actor_input, cap_after=None, log=None):
        self.input = actor_input
        self.cap_after = cap_after
        self.rows = []
        self.charged = 0
        self.stores = {}
        self.log = log or FakeLog()
        self.status_message = None
        self.failure = None
        self.charging = FakeChargingManager()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_input(self):
        return self.input

    async def open_dataset(self):
        return FakeDataset(self)

    def get_charging_manager(self):
        return self.charging

    def get_env(self):
        return {"run_id": "run-1"}

    async def set_status_message(self, text):
        self.status_message = text

    async def fail(self, status_message=None):
        self.failure = status_message

    async def open_key_value_store(self, name=None):
        return self.stores.setdefault(name or "run", FakeKV())

    async def push_data(self, row, charged_event_name=None):
        if self.cap_after is not None and self.charged >= self.cap_after:
            return FakeChargeResult(0, True)
        self.rows.append(row)
        if charged_event_name:
            self.charged += 1
        return FakeChargeResult(1 if charged_event_name else 0, False)


def _summary():
    return {"chargedEvents": 0, "deliveredUncharged": 0, "chargeUncertain": 0,
            "writeFailures": 0}


def _posting(index, title="Game Designer", country="fr", city="Paris", remote=False,
             department="Game Design", level="Mid-Senior Level", employment="Full-time"):
    """A posting shaped exactly like the ones the API answered with (tests/fixtures)."""
    return {
        "id": str(744000000000000 + index),
        "name": title,
        "releasedDate": "2026-11-06T10:00:00.000Z",
        "refNumber": "REF%d" % index,
        "company": {"identifier": "Ubisoft2", "name": "Ubisoft"},
        "location": {"city": city, "region": None, "country": country, "remote": remote,
                     "hybrid": False, "fullLocation": city},
        "department": {"id": "1", "label": department},
        "function": {"id": "design", "label": "Design"},
        "industry": {"id": "games", "label": "Video Games"},
        "experienceLevel": {"id": "mid", "label": level},
        "typeOfEmployment": {"id": "full", "label": employment},
        "language": {"code": "en", "label": "English"},
        "visibility": "public",
    }


def _listing(postings, total=None, **over):
    out = {"postings": postings, "total": total if total is not None else len(postings),
           "pages": 1, "pagesFailed": 0, "truncated": False, "truncatedReason": None,
           "error": None}
    out.update(over)
    return out


def _run(make_input, listing=None, board="Ubisoft", detail=None, cap_after=None):
    """Run main() with the network replaced by the answers a test declares."""
    original_actor = main.Actor
    original_list = sc.list_postings
    original_board = sc.board_name
    original_detail = sc.fetch_detail
    fake = None
    try:
        def answer(client, identifier, **kw):
            if isinstance(listing, Exception):
                raise listing
            return listing if listing is not None else _listing([_posting(1)])
        sc.list_postings = answer
        sc.board_name = lambda client, identifier: board
        if detail is None:
            sc.fetch_detail = original_detail
        else:
            sc.fetch_detail = detail

        class Bound(RunActor):
            async def get_input(self):
                return make_input

        fake = Bound(make_input, cap_after=cap_after)
        main.Actor = fake
        asyncio.run(main.main())
    finally:
        main.Actor = original_actor
        sc.list_postings = original_list
        sc.board_name = original_board
        sc.fetch_detail = original_detail
    return fake, fake.stores[main.SUMMARY_STORE].data["RUN_SUMMARY"]


# --- the money path --------------------------------------------------------------------

def test_push_charges_once_per_row():
    async def run():
        fake = FakeActor()
        original = main.Actor
        main.Actor = fake
        try:
            summary = _summary()
            delivered, cap, outcome = await main._push(
                {"jobId": "1", "status": "DELIVERED"}, summary, FakeLog(), True, {"written": 0})
            assert (delivered, cap, outcome) == (True, False, "charged")
            assert summary["chargedEvents"] == 1 and len(fake.rows) == 1
            assert fake.calls == [("1", "job-delivered")], fake.calls
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_push: one row, one charge, the event name goes to the SDK"


def test_push_at_cap_writes_nothing():
    async def run():
        fake = FakeActor(mode="cap")
        original = main.Actor
        main.Actor = fake
        try:
            summary = _summary()
            delivered, cap, outcome = await main._push(
                {"jobId": "1", "status": "DELIVERED"}, summary, FakeLog(), True, {"written": 0})
            assert (delivered, cap, outcome) == (False, True, "free"), (delivered, cap, outcome)
            assert fake.rows == [], "the SDK writes nothing when the cap lets nothing through"
            assert summary["deliveredUncharged"] == 0 and summary["chargedEvents"] == 0
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_push: at the spending cap nothing is written and nothing is called free"


def test_a_charge_failure_after_the_write_does_not_duplicate_the_row():
    async def run():
        fake = FakeActor(mode="raise_after_write")
        original = main.Actor
        main.Actor = fake
        try:
            summary = _summary()
            delivered, cap, outcome = await main._push(
                {"jobId": "1", "status": "DELIVERED"}, summary, FakeLog(), True, {"written": 0})
            assert delivered is True and outcome == "free", (delivered, outcome)
            assert len(fake.rows) == 1 and len(fake.calls) == 1, fake.calls
            assert summary["chargeUncertain"] == 1 and summary["deliveredUncharged"] == 1
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_push: a failed charge after the write is delivered and uncertain, never repeated"


def test_only_delivered_postings_are_charged():
    fake, summary = _run(
        {"companies": ["Ubisoft2"], "keywords": "designer"},
        listing=_listing([_posting(1, title="Game Designer"),
                          _posting(2, title="Accountant"),
                          _posting(3, title="Level Designer")]))
    assert summary["jobsSeen"] == 3, summary
    assert summary["jobsDelivered"] == 2 and fake.charged == 2, summary
    assert summary["filteredOut"] == 1 and summary["filteredBy"] == {"keywords": 1}, summary
    assert summary["chargedEvents"] == 2
    statuses = [row["status"] for row in fake.rows]
    assert statuses == ["DELIVERED", "DELIVERED"], statuses
    return "a run: two delivered postings, two charges, the filtered one costs nothing"


def test_a_board_nobody_owns_is_a_row_and_is_not_charged():
    fake, summary = _run({"companies": ["zzzz-not-a-company"]},
                         listing=sc.ScError("HTTP 404", 404), board=None)
    assert summary["companiesNotFound"] == 1, summary
    assert summary["jobsDelivered"] == 0 and fake.charged == 0
    assert fake.rows[0]["status"] == "NOT_CHECKED_BOARD_NOT_FOUND", fake.rows[0]
    assert fake.failure, "a run that delivered nothing must say why in the header"
    assert "nothing was charged" in fake.failure.lower(), fake.failure
    return "a board nobody owns: one row with the reason, zero charges, a failed run that says why"


def test_an_empty_board_is_named_as_empty_not_as_missing():
    fake, summary = _run({"companies": ["Visa"]}, listing=_listing([], total=0), board="Visa")
    assert summary["companiesEmpty"] == 1 and summary["companiesNotFound"] == 0, summary
    assert fake.rows[0]["status"] == "NOT_CHECKED_BOARD_EMPTY", fake.rows[0]
    assert fake.charged == 0
    return "an empty board: NOT_CHECKED_BOARD_EMPTY, not 'does not exist', and free"


def test_the_same_board_twice_is_read_and_charged_once():
    fake, summary = _run({"companies": ["Ubisoft2", "https://jobs.smartrecruiters.com/Ubisoft2"]},
                         listing=_listing([_posting(1), _posting(2)]))
    assert summary["companiesDeduplicated"] == 1, summary
    assert summary["companiesAsked"] == 1, summary
    assert fake.charged == 2, "one board, two postings, two charges, not four"
    return "the same board typed twice: one board, two postings, two charges"


def test_counters_add_up():
    fake, summary = _run({"companies": ["Ubisoft2", "Other"], "keywords": "designer"},
                         listing=_listing([_posting(1, title="Designer"), _posting(2, title="Chef")],
                                          total=298))
    assert summary["jobsSeen"] == 4, summary
    assert summary["jobsAsked"] == 596, summary
    assert "counterMismatch" not in summary, summary
    assert "companyCounterMismatch" not in summary, summary
    assert summary["jobsNotRead"] == 592, summary
    assert summary["jobsSeen"] + summary["jobsNotRead"] == summary["jobsAsked"]
    return "the counters add up: seen plus not read equals the board total"


def test_a_capped_run_counts_what_it_did_not_reach():
    fake, summary = _run({"companies": ["Ubisoft2"]},
                         listing=_listing([_posting(i) for i in range(5)]), cap_after=2)
    assert summary["jobsDelivered"] == 2, summary
    assert summary["jobsNotProcessed"] == 3, summary
    assert summary["stopReason"] == "spending-cap", summary
    assert summary["jobsSeen"] == 5
    assert (summary["jobsDelivered"] + summary["filteredOut"] + summary["jobsUndelivered"]
            + summary["jobsUnverified"] + summary["jobsNotProcessed"]) == summary["jobsSeen"]
    return "at the cap: two delivered, three counted as not processed, sums still match"


def test_a_failed_description_keeps_the_posting():
    def boom(client, identifier, job_id):
        raise sc.ScError("HTTP 404", 404)

    fake, summary = _run({"companies": ["Ubisoft2"], "includeDetails": True},
                         listing=_listing([_posting(1)]), detail=boom)
    assert summary["detailFailures"] == 1 and summary["jobsDelivered"] == 1, summary
    assert fake.rows[0]["detailError"] == "HTTP 404", fake.rows[0]
    assert fake.rows[0]["detailFetched"] is False
    assert fake.charged == 1, "a posting without its description is still the posting he asked for"
    return "a refused description: the posting is delivered, flagged with detailError, and charged"


def test_descriptions_stop_at_max_details():
    calls = []

    def detail(client, identifier, job_id):
        calls.append(job_id)
        return {"jobDescription": "text", "qualifications": None,
                "additionalInformation": None, "companyDescription": None,
                "applyUrl": None, "postingUrl": None}

    fake, summary = _run({"companies": ["Ubisoft2"], "includeDetails": True, "maxDetails": 2},
                         listing=_listing([_posting(i) for i in range(4)]), detail=detail)
    assert len(calls) == 2, calls
    assert summary["detailsSkipped"] == 2 and summary["detailsFetched"] == 2, summary
    assert summary["jobsDelivered"] == 4, "the postings past the bound are still delivered"
    return "maxDetails: two descriptions fetched, two skipped and counted, four rows delivered"


def test_all_invalid_inputs_say_so_in_the_header():
    # measured 07.11.2026 on the published build: the header said "None of the 0 board(s)
    # could be read: no such board" while the dataset held INVALID_INPUT
    fake, summary = _run({"companies": ["not a board !!!"]}, listing=_listing([]))
    assert summary["companiesInvalid"] == 1 and summary["companiesAsked"] == 1, summary
    assert fake.rows and fake.rows[0]["status"] == "INVALID_INPUT", fake.rows
    assert fake.charged == 0, "an input that is not an identifier is a row and costs nothing"
    assert fake.failure, "a run that delivered nothing must say why in the header"
    assert "not board identifiers" in fake.failure, fake.failure
    assert "no such board" not in fake.failure, fake.failure
    assert "0 board(s)" not in fake.failure, fake.failure
    return "all inputs invalid: the header names the identifiers, not a board that does not exist"


def test_a_capped_board_is_truncated_and_counted_once():
    fake, summary = _run({"companies": ["Ubisoft2"]},
                         listing=_listing([_posting(i) for i in range(5)]), cap_after=2)
    assert summary["truncatedCompanies"] == 1, summary
    assert summary["byCompany"]["Ubisoft2"]["truncated"] is True, summary["byCompany"]
    assert "truncatedMismatch" not in summary, summary
    return "a board cut by the spending cap: flagged truncated and counted once"


def test_a_missing_board_is_not_called_truncated():
    fake, summary = _run({"companies": ["zzzz-not-a-company"]},
                         listing=sc.ScError("HTTP 404", 404), board=None)
    assert summary["truncatedCompanies"] == 0, summary
    assert summary["byCompany"]["zzzz-not-a-company"]["truncated"] is False, summary["byCompany"]
    assert "truncatedMismatch" not in summary, summary
    return "a board nobody owns: no truncation flag, counter stays zero, no mismatch"


def test_a_board_that_answers_empty_and_has_no_page_is_not_truncated():
    # the branch Orestes caught: an empty answer plus no public page was flagged truncated,
    # so the counter said one board was cut short while nothing had been cut
    fake, summary = _run({"companies": ["zzzz-not-a-company"]},
                         listing=_listing([], total=0), board=None)
    assert summary["companiesNotFound"] == 1, summary
    assert summary["byCompany"]["zzzz-not-a-company"]["truncated"] is False, summary["byCompany"]
    assert summary["truncatedCompanies"] == 0, summary
    assert "truncatedMismatch" not in summary, summary
    return "an empty answer with no public page: not truncated, counter zero, no mismatch"


def test_the_summary_names_rows_by_status_and_counts_duplicates():
    fake, summary = _run({"companies": ["Ubisoft2"]},
                         listing=_listing([_posting(1), _posting(2)], duplicates=3))
    assert summary["byStatus"] == {"DELIVERED": 2}, summary.get("byStatus")
    assert summary["duplicatesSkipped"] == 3, summary
    assert fake.charged == 2, "three duplicated postings must not become three charges"
    return "byStatus counts rows by status and a duplicated posting is charged once"


def test_a_recovered_free_row_is_not_called_unpaid():
    async def run():
        fake = FakeActor(mode="raise_after_write")
        original = main.Actor
        main.Actor = fake
        try:
            summary = _summary()
            delivered, cap, outcome = await main._push(
                {"status": "INVALID_INPUT", "company": "x", "jobId": None},
                summary, FakeLog(), False, {"written": 0})
            assert (delivered, outcome) == (True, "free"), (delivered, outcome)
            assert summary["deliveredUncharged"] == 0, summary
            assert summary["chargeUncertain"] == 0, summary
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_push: a status row recovered after a write error is free, not 'unpaid'"


def test_detect_billing_reads_the_charging_manager():
    async def run():
        original = main.Actor
        try:
            main.Actor = FakeActor(charging=FakeChargingManager())
            assert await main._detect_billing(FakeLog()) is True
            main.Actor = FakeActor(charging=FakeChargingManager(is_ppe=False))
            assert await main._detect_billing(FakeLog()) is False
            main.Actor = FakeActor(charging=FakeChargingManager(prices={}))
            assert await main._detect_billing(FakeLog()) is False
        finally:
            main.Actor = original

    asyncio.run(run())
    return "_detect_billing: pay-per-event with our event charges, anything else does not"


def run_all():
    checks = [
        test_push_charges_once_per_row,
        test_push_at_cap_writes_nothing,
        test_a_charge_failure_after_the_write_does_not_duplicate_the_row,
        test_only_delivered_postings_are_charged,
        test_a_board_nobody_owns_is_a_row_and_is_not_charged,
        test_an_empty_board_is_named_as_empty_not_as_missing,
        test_the_same_board_twice_is_read_and_charged_once,
        test_counters_add_up,
        test_a_capped_run_counts_what_it_did_not_reach,
        test_a_failed_description_keeps_the_posting,
        test_descriptions_stop_at_max_details,
        test_all_invalid_inputs_say_so_in_the_header,
        test_a_capped_board_is_truncated_and_counted_once,
        test_a_missing_board_is_not_called_truncated,
        test_a_board_that_answers_empty_and_has_no_page_is_not_truncated,
        test_the_summary_names_rows_by_status_and_counts_duplicates,
        test_a_recovered_free_row_is_not_called_unpaid,
        test_detect_billing_reads_the_charging_manager,
    ]
    failed = 0
    for check in checks:
        try:
            note = check()
            print("ok   %-58s %s" % (check.__name__, note or ""))
        except AssertionError as exc:
            failed += 1
            print("FAIL %-58s %s" % (check.__name__, exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERR  %-58s %s: %s" % (check.__name__, type(exc).__name__, exc))
    print("\n%d checks, %d failed" % (len(checks), failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
