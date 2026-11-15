"""Money, counters and statuses of the Workable run. Uses a stand-in Actor shaped like apify 4.2.0."""

import asyncio
import os
import sys
import types
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from src.client import BoardError  # noqa: E402

# Taken before the stand-in Actor replaces sys.modules["apify"]: the price object
# below must be the real dataclass, not my idea of it.
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
        # The real crawlee model, not a dict: a dict stand-in is exactly what hid
        # the "count is always None" defect. The dict stays only as a last resort.
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


class FakeStore:
    def __init__(self):
        self.values = {}

    async def set_value(self, key, value):
        self.values[key] = value


class ActorMeta(type):
    """The real class is used as "async with Actor:": that needs a metaclass, not a classmethod."""

    async def __aenter__(cls):
        return cls

    async def __aexit__(cls, exc_type, exc, tb):
        return False


class FakeActor(metaclass=ActorMeta):
    input = {}
    rows = []
    plan = []
    charged = 0
    status_messages = []
    failed = None
    summary = None
    store = FakeStore()
    pricing = None

    @classmethod
    def reset(cls, actor_input, plan=None, pricing=None):
        cls.input = actor_input
        cls.rows = []
        cls.plan = list(plan or [])
        cls.charged = 0
        cls.status_messages = []
        cls.failed = None
        cls.summary = None
        cls.store = FakeStore()
        cls.pricing = pricing

    @classmethod
    async def get_input(cls):
        return cls.input

    @classmethod
    async def open_dataset(cls):
        # The real Actor.open_dataset is a coroutine: a sync fake hid a missing await and
        # kept datasetItemCount at None in every check.
        return FakeDataset(cls)

    @classmethod
    async def push_data(cls, row, *, charged_event_name=None):
        if not charged_event_name:
            cls.rows.append(dict(row))
            return None
        outcome = cls.plan.pop(0) if cls.plan else "charged"
        if outcome == "cap":
            return ChargeResult(0, True)
        if outcome == "lost":
            raise RuntimeError("the dataset refused the write")
        cls.rows.append(dict(row))
        if outcome == "raise":
            raise RuntimeError("charge exploded after the write")
        if outcome == "charged":
            cls.charged += 1
            return ChargeResult(1, False)
        return ChargeResult(0, False)

    @classmethod
    async def set_value(cls, key, value):
        cls.summary = value

    @classmethod
    async def open_key_value_store(cls, name=None):
        return cls.store

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
                return outer.pricing

        return Manager()


sys.modules["apify"] = types.SimpleNamespace(Actor=FakeActor)

import src.main as M  # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        RESULTS.append((name, "FAIL", str(exc)))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, "ERROR", "%s: %s" % (type(exc).__name__, exc)))
    else:
        RESULTS.append((name, "ok", ""))


def job(index, department="Engineering", published="2026-10-01", title=None):
    return {
        "title": title or "Engineer %d" % index,
        "shortcode": "J%03d" % index,
        "department": department,
        "function": department,
        "industry": "Software",
        "education": "",
        "experience": "Mid-Senior level",
        "employment_type": "Full-time",
        "telecommuting": False,
        "locations": [{"country": "United States", "countryCode": "US", "city": "Boston", "region": "MA"}],
        "country": "United States",
        "city": "Boston",
        "state": "MA",
        "url": "https://apply.workable.com/j/J%03d" % index,
        "application_url": "https://apply.workable.com/j/J%03d/apply" % index,
        "published_on": published,
        "created_at": published,
        "description": "<p>Do the thing</p>",
    }


def multi_city(code, city, country="United States", cc="US"):
    """One entry of a posting that Workable splits per city: same shortcode, one location."""
    doc = job(1)
    doc["shortcode"] = code
    doc["locations"] = [{"country": country, "countryCode": cc, "city": city, "region": None}]
    doc["country"] = country
    doc["city"] = city
    doc["state"] = None
    return doc


def board(name, jobs):
    return {"name": name, "description": "", "jobs": jobs}


def run(actor_input, fetch_map, plan=None, pricing=None):
    FakeActor.reset(actor_input, plan, pricing)
    calls = []

    def fake_fetch(sub, timeout=60, attempts=2):
        calls.append(sub)
        value = fetch_map.get(sub)
        if isinstance(value, Exception):
            raise value
        return value

    original = M.fetch_board
    M.fetch_board = fake_fetch
    try:
        asyncio.run(M.main())
    except SystemExit:
        pass
    finally:
        M.fetch_board = original
    return FakeActor.summary, calls


def test_normal_run_with_a_missing_board():
    summary, calls = run(
        {"boards": ["alpha", "ghost"]},
        {"alpha": board("Alpha", [job(1), job(2), job(3)]),
         "ghost": BoardError("not_found", "Workable has no board named 'ghost' (HTTP 404)")},
        plan=["charged", "charged", "charged"],
    )
    assert calls == ["alpha", "ghost"], calls
    assert FakeActor.charged == 3, FakeActor.charged
    assert summary["delivered"] == 3, summary["delivered"]
    assert summary["chargedEvents"] == 3, summary["chargedEvents"]
    assert summary["statusRows"] == 1, summary["statusRows"]
    assert summary["byStatus"] == {"NOT_CHECKED_BOARD_NOT_FOUND": 1}, summary["byStatus"]
    assert summary["boardsAsked"] == 2, summary["boardsAsked"]
    assert summary["boardsOk"] == 1 and summary["boardsNotFound"] == 1
    assert "countersMismatch" not in summary, summary.get("countersMismatch")
    assert len(FakeActor.rows) == 4
    assert FakeActor.failed is None
    assert summary["billingActive"] is True
    assert "RUN_SUMMARY-TESTRUN" in FakeActor.store.values


def test_spending_cap_stops_and_never_charges_the_rest():
    summary, _ = run(
        {"boards": ["alpha"]},
        {"alpha": board("Alpha", [job(1), job(2), job(3), job(4)])},
        plan=["charged", "cap"],
    )
    assert summary["delivered"] == 1, summary["delivered"]
    assert summary["chargedEvents"] == 1
    assert summary["stopReason"] == "spending-cap", summary["stopReason"]
    assert summary["jobsProcessed"] == 4, summary["jobsProcessed"]
    assert summary["jobsNotProcessed"] == 3, summary["jobsNotProcessed"]
    assert summary["jobsProcessed"] == (
        summary["delivered"] + summary["jobsUnverified"] + summary["jobsNotProcessed"]
    )
    statuses = [r["status"] for r in FakeActor.rows]
    assert statuses == ["DELIVERED", "NOT_CHECKED_CAP", "NOT_CHECKED_CAP", "NOT_CHECKED_CAP"], statuses
    assert FakeActor.charged == 1, "status rows must never be charged"
    assert summary["byBoard"]["alpha"]["truncated"] is True
    assert summary["truncatedBoards"] == 1


def test_billing_inactive_delivers_free():
    summary, _ = run(
        {"boards": ["alpha"]},
        {"alpha": board("Alpha", [job(1), job(2)])},
        plan=["free", "free"],
    )
    assert summary["delivered"] == 2
    assert summary["chargedEvents"] == 0
    assert summary["deliveredUncharged"] == 2, summary["deliveredUncharged"]
    assert summary["billingActive"] is False, summary["billingActive"]
    assert len(FakeActor.rows) == 2


def test_charge_failure_does_not_duplicate_the_row():
    summary, _ = run(
        {"boards": ["alpha"]},
        {"alpha": board("Alpha", [job(1)])},
        plan=["raise"],
    )
    assert len(FakeActor.rows) == 1, "a failed charge must not lead to a second write"
    assert summary["delivered"] == 1
    assert summary["chargeUncertain"] == 1, summary["chargeUncertain"]
    assert summary["deliveredUncharged"] == 1
    assert "exploded" in (summary["lastChargeError"] or "")


def test_filters_never_reach_the_dataset():
    summary, _ = run(
        {"boards": ["alpha"], "department": "engineering"},
        {"alpha": board("Alpha", [job(1, "Engineering"), job(2, "Engineering"), job(3, "Sales")])},
        plan=["charged", "charged"],
    )
    assert summary["filteredOut"] == 1, summary["filteredOut"]
    assert summary["jobsSeen"] == 2
    assert summary["delivered"] == 2
    assert len(FakeActor.rows) == 2
    assert all("Sales" not in (r.get("department") or "") for r in FakeActor.rows)


def test_max_jobs_per_board_is_counted_and_marked():
    summary, _ = run(
        {"boards": ["alpha"], "maxJobsPerBoard": 2},
        {"alpha": board("Alpha", [job(i) for i in range(1, 5)])},
        plan=["charged", "charged"],
    )
    assert summary["jobsSeen"] == 4
    assert summary["jobsNotRead"] == 2, summary["jobsNotRead"]
    assert summary["jobsProcessed"] == 2
    assert summary["delivered"] == 2
    assert summary["byBoard"]["alpha"]["truncated"] is True
    assert summary["truncatedBoards"] == 1
    assert "countersMismatch" not in summary


def test_invalid_input_is_named_not_called_missing():
    summary, calls = run({"boards": ["not a board !!!"]}, {})
    assert calls == [], calls
    assert summary["boardsInvalid"] == 1
    assert summary["boardsAsked"] == 1
    assert FakeActor.failed is not None, "nothing was read, so the run must stop with a reason"
    assert "not Workable board identifiers" in FakeActor.failed, FakeActor.failed
    assert "do not exist" not in FakeActor.failed, FakeActor.failed
    assert len(FakeActor.rows) == 1 and FakeActor.rows[0]["status"] == "INVALID_INPUT"
    assert summary is not None, "the summary is written before the run stops"


def test_read_error_is_not_a_missing_board():
    summary, _ = run(
        {"boards": ["boom"]},
        {"boom": BoardError("error", "the board answered HTTP 503 Service Unavailable")},
    )
    assert summary["boardsError"] == 1
    assert summary["boardsNotFound"] == 0
    assert summary["byStatus"] == {"NOT_CHECKED_ERROR": 1}, summary["byStatus"]
    assert "could not be read" in FakeActor.failed, FakeActor.failed
    assert "do not exist" not in FakeActor.failed


def test_duplicate_boards_are_read_once():
    summary, calls = run(
        {"boards": ["alpha", "https://apply.workable.com/alpha/", "alpha"]},
        {"alpha": board("Alpha", [job(1)])},
        plan=["charged"],
    )
    assert calls == ["alpha"], calls
    assert summary["boardsDeduplicated"] == 2, summary["boardsDeduplicated"]
    assert summary["boardsAsked"] == 1
    assert summary["delivered"] == 1


def test_empty_board_is_an_answer_not_a_failure():
    summary, _ = run({"boards": ["toggl"]}, {"toggl": board("Toggl", [])})
    assert summary["boardsEmpty"] == 1
    assert summary["boardsOk"] == 0
    assert FakeActor.failed is None, FakeActor.failed
    assert summary["byStatus"] == {"NOT_CHECKED_BOARD_EMPTY": 1}
    assert summary["delivered"] == 0


def test_entries_that_are_not_postings_are_counted():
    summary, _ = run(
        {"boards": ["alpha"]},
        {"alpha": board("Alpha", [job(1), "garbage", {}])},
        plan=["charged"],
    )
    assert summary["jobsOnBoard"] == 3
    assert summary["jobsUnparsable"] == 2, summary["jobsUnparsable"]
    assert summary["jobsSeen"] == 1
    assert summary["delivered"] == 1
    assert summary["jobsOnBoard"] == summary["filteredOut"] + summary["jobsSeen"] + summary["jobsUnparsable"]


def test_pricing_flag():
    summary, _ = run(
        {"boards": ["alpha"]},
        {"alpha": board("Alpha", [job(1)])},
        plan=["charged"],
        pricing={"pricingPerEvent": {"actorChargeEvents": {"job-delivered": {}}}},
    )
    assert summary["pricingEventPriced"] is True, summary["pricingEventPriced"]


def test_multicity_posting_is_one_row_and_one_charge():
    entries = [multi_city("C1", "Erlanger"), multi_city("C1", "Louisville"),
               multi_city("C1", "Nashville"), multi_city("C1", "Knoxville")]
    summary, _ = run({"boards": ["celsius"]}, {"celsius": board("Celsius", entries)}, plan=["charged"])
    assert summary["delivered"] == 1, summary["delivered"]
    assert FakeActor.charged == 1, FakeActor.charged
    assert summary["postingsMerged"] == 3, summary["postingsMerged"]
    assert summary["postingsRead"] == 1, summary["postingsRead"]
    delivered = [r for r in FakeActor.rows if r["status"] == "DELIVERED"]
    assert len(delivered) == 1, len(delivered)
    assert len(delivered[0]["locations"]) == 4, delivered[0]["locations"]
    assert summary["jobsOnBoard"] == 4
    assert "countersMismatch" not in summary, summary.get("countersMismatch")


def test_max_jobs_per_board_counts_postings_not_cities():
    entries = [multi_city("A", "Boston"), multi_city("A", "Denver"),
               multi_city("B", "Austin"), multi_city("B", "Dallas")]
    summary, _ = run({"boards": ["alpha"], "maxJobsPerBoard": 2},
                     {"alpha": board("Alpha", entries)}, plan=["charged", "charged"])
    assert summary["delivered"] == 2, summary["delivered"]
    assert summary["postingsMerged"] == 2, summary["postingsMerged"]
    assert summary["jobsNotRead"] == 0, summary["jobsNotRead"]
    assert FakeActor.charged == 2, FakeActor.charged


def test_repeated_charge_failures_stop_the_whole_run():
    summary, _ = run(
        {"boards": ["alpha", "beta"]},
        {"alpha": board("Alpha", [job(1), job(2), job(3)]), "beta": board("Beta", [job(4)])},
        plan=["raise", "raise", "raise"],
    )
    assert summary["stopReason"] == "charge-error", summary["stopReason"]
    assert summary["boardsSkipped"] == 1, summary["boardsSkipped"]
    assert summary["delivered"] == 2, summary["delivered"]
    assert summary["chargeUncertain"] == 2, summary["chargeUncertain"]
    assert summary["jobsNotProcessed"] == 1, summary["jobsNotProcessed"]
    assert summary["jobsProcessed"] == (
        summary["delivered"] + summary["jobsUnverified"] + summary["jobsNotProcessed"]
    )
    boards_delivered = [r["board"] for r in FakeActor.rows if r["status"] == "DELIVERED"]
    assert "beta" not in boards_delivered, "a board was delivered after the run had stopped"
    assert "countersMismatch" not in summary, summary.get("countersMismatch")


def test_unconfirmed_write_is_not_counted_as_delivered():
    summary, _ = run({"boards": ["alpha"]}, {"alpha": board("Alpha", [job(1)])}, plan=["lost"])
    assert summary["delivered"] == 0, summary["delivered"]
    assert summary["jobsUnverified"] == 1, summary["jobsUnverified"]
    assert summary["chargeUncertain"] == 0, summary["chargeUncertain"]
    assert summary["jobsProcessed"] == (
        summary["delivered"] + summary["jobsUnverified"] + summary["jobsNotProcessed"]
    )
    assert "countersMismatch" not in summary, summary.get("countersMismatch")


def test_repeated_write_failures_stop_the_whole_run():
    summary, _ = run(
        {"boards": ["alpha", "beta"]},
        {"alpha": board("Alpha", [job(1), job(2), job(3)]), "beta": board("Beta", [job(4)])},
        plan=["lost", "lost", "lost"],
    )
    assert summary["stopReason"] == "dataset-write-error", summary["stopReason"]
    assert summary["boardsSkipped"] == 1, summary["boardsSkipped"]
    assert summary["delivered"] == 0
    assert summary["jobsUnverified"] == 2, summary["jobsUnverified"]
    assert summary["jobsNotProcessed"] == 1, summary["jobsNotProcessed"]


def test_header_names_the_boards_that_were_left_out():
    run({"boards": ["alpha", "ghost", "not a board !!!"]},
        {"alpha": board("Alpha", [job(1)]),
         "ghost": BoardError("not_found", "Workable has no board named 'ghost'")},
        plan=["charged"])
    message = FakeActor.status_messages[-1]
    assert "1 of 3 board(s) read" in message, message
    assert "1 do not exist" in message, message
    assert "1 not a board identifier" in message, message


def test_skipped_boards_are_marked_truncated():
    summary, _ = run(
        {"boards": ["alpha", "beta"]},
        {"alpha": board("Alpha", [job(1), job(2)]), "beta": board("Beta", [job(3)])},
        plan=["charged", "cap"],
    )
    assert summary["stopReason"] == "spending-cap", summary["stopReason"]
    assert summary["byBoard"]["beta"]["skipped"] is True
    assert summary["byBoard"]["beta"]["truncated"] is True
    assert summary["byBoard"]["alpha"]["truncated"] is True
    assert summary["truncatedBoards"] == 2, summary["truncatedBoards"]
    assert "truncatedMismatch" not in summary, summary.get("truncatedMismatch")


def test_billable_follows_the_price_list():
    """The platform returns ActorPricingInfo, not a dict.

    On 0.1.1 every delivered row said billable=null on the platform while
    job-delivered was charged, because this test handed the code a dict the SDK
    never returns. The stand-in is now the real dataclass.
    """
    ActorPricingInfo = RealActorPricingInfo
    assert ActorPricingInfo is not None, "the real apify._charging must be importable"

    priced = ActorPricingInfo(pricing_model="PAY_PER_EVENT", max_total_charge_usd=4.4,
                              is_pay_per_event=True, per_event_prices={"job-delivered": 0.0015})
    run({"boards": ["alpha"]}, {"alpha": board("Alpha", [job(1)])}, plan=["charged"], pricing=priced)
    assert FakeActor.rows[0]["billable"] is True, FakeActor.rows[0]["billable"]

    other = ActorPricingInfo(pricing_model="PAY_PER_EVENT", max_total_charge_usd=4.4,
                             is_pay_per_event=True, per_event_prices={"other-event": 0.01})
    run({"boards": ["alpha"]}, {"alpha": board("Alpha", [job(1)])}, plan=["free"], pricing=other)
    assert FakeActor.rows[0]["billable"] is False

    unpaid = ActorPricingInfo(pricing_model="FREE", max_total_charge_usd=None,
                              is_pay_per_event=False, per_event_prices=None)
    run({"boards": ["alpha"]}, {"alpha": board("Alpha", [job(1)])}, plan=["free"], pricing=unpaid)
    assert FakeActor.rows[0]["billable"] is False

    run({"boards": ["alpha"]}, {"alpha": board("Alpha", [job(1)])}, plan=["charged"], pricing=None)
    assert FakeActor.rows[0]["billable"] is None


def test_trailing_slash_is_the_same_board():
    summary, _ = run({"boards": ["celsius/"]}, {"celsius": board("Celsius", [job(1)])},
                     plan=["charged"])
    delivered = [r for r in FakeActor.rows if r.get("status") == "DELIVERED"]
    assert len(delivered) == 1, [r.get("status") for r in FakeActor.rows]


def test_country_code_is_compared_through_aliases():
    def london(index):
        entry = job(index)
        entry["locations"] = [{"country": "United Kingdom", "countryCode": "UK",
                               "city": "London", "region": ""}]
        entry["country"] = "United Kingdom"
        entry["city"] = "London"
        return entry

    for wanted in ("gb", "uk"):
        run({"boards": ["alpha"], "country": wanted},
            {"alpha": board("Alpha", [london(1)])}, plan=["charged"])
        delivered = [r for r in FakeActor.rows if r.get("status") == "DELIVERED"]
        assert len(delivered) == 1, (wanted, [r.get("status") for r in FakeActor.rows])


def test_dataset_count_is_read_from_the_metadata():
    summary, _ = run({"boards": ["alpha"]}, {"alpha": board("Alpha", [job(1)])}, plan=["charged"])
    assert summary["datasetItemCount"] == 1, summary["datasetItemCount"]
    assert summary["datasetItemCountRaw"] == 1, summary["datasetItemCountRaw"]


CHECKS = [
    ("a good board plus a missing one: 3 charged, 1 free status row", test_normal_run_with_a_missing_board),
    ("the spending cap stops the run and charges nothing more", test_spending_cap_stops_and_never_charges_the_rest),
    ("inactive billing delivers rows for free and says so", test_billing_inactive_delivers_free),
    ("a charge failure writes the row once, never twice", test_charge_failure_does_not_duplicate_the_row),
    ("filtered postings are counted and never delivered", test_filters_never_reach_the_dataset),
    ("maxJobsPerBoard is counted, marked truncated and closes its sums", test_max_jobs_per_board_is_counted_and_marked),
    ("invalid input is named as invalid, not as a missing board", test_invalid_input_is_named_not_called_missing),
    ("a read error is not reported as a missing board", test_read_error_is_not_a_missing_board),
    ("the same board twice is read once", test_duplicate_boards_are_read_once),
    ("an empty board is an honest answer, not a failure", test_empty_board_is_an_answer_not_a_failure),
    ("entries that are not postings are counted", test_entries_that_are_not_postings_are_counted),
    ("the price list is checked for our event", test_pricing_flag),
    ("multi-city posting is one row with all locations and one charge", test_multicity_posting_is_one_row_and_one_charge),
    ("maxJobsPerBoard counts postings, not cities", test_max_jobs_per_board_counts_postings_not_cities),
    ("repeated charge failures stop every board, not just this one", test_repeated_charge_failures_stop_the_whole_run),
    ("a write that did not land is not counted as delivered", test_unconfirmed_write_is_not_counted_as_delivered),
    ("repeated write failures stop every board", test_repeated_write_failures_stop_the_whole_run),
    ("the run header names boards left out, not only the read ones", test_header_names_the_boards_that_were_left_out),
    ("skipped boards are marked truncated and the flags add up", test_skipped_boards_are_marked_truncated),
    ("billable follows the price list instead of being always true", test_billable_follows_the_price_list),
    ("the dataset count comes from the metadata, not from None", test_dataset_count_is_read_from_the_metadata),
    ("a trailing slash is the same board, not invalid input", test_trailing_slash_is_the_same_board),
    ("a country code is compared through its aliases", test_country_code_is_compared_through_aliases),
]


def run_all():
    for name, fn in CHECKS:
        check(name, fn)
    failed = [r for r in RESULTS if r[1] != "ok"]
    for name, status, detail in RESULTS:
        if status != "ok":
            print("%-6s %s :: %s" % (status, name, detail))
    print("%d/%d checks passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_all())
