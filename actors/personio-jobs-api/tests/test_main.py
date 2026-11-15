"""Money and counter checks for the Personio Actor, run through its real main().

The stand-in Actor mirrors apify 4.2.0 instead of my hopes about it: open_dataset and
set_status_message are coroutines, push_data writes the row before it charges, and the
price list comes back as the ActorPricingInfo dataclass.
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from apify._charging import ActorPricingInfo  # noqa: E402
from crawlee.storage_clients.models import DatasetMetadata  # noqa: E402

from src import main  # noqa: E402
from src.client import BoardError, parse_job  # noqa: E402


class Failed(Exception):
    pass


class FakeResult:
    def __init__(self, charged_count, limit_reached=False):
        self.charged_count = charged_count
        self.event_charge_limit_reached = limit_reached


def real_metadata(count):
    """The object apify 4.2.0 really returns: crawlee DatasetMetadata, not a dict.

    Building it here is the point of the test: a hand-written {"item_count": n} proves my
    belief about the SDK, and that is exactly how the dead counter survived three reviews.
    """
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    return DatasetMetadata(id="ds", accessed_at=now, created_at=now, modified_at=now,
                           item_count=count)


class FakeDataset:
    def __init__(self, items, metadata_fails=False):
        self.items = items
        self.metadata_fails = metadata_fails

    async def get_metadata(self):
        if self.metadata_fails:
            raise RuntimeError("metadata endpoint is down")
        return real_metadata(len(self.items))


class FakeStore:
    def __init__(self):
        self.data = {}

    async def set_value(self, key, value):
        self.data[key] = value


class FakeActor:
    """One run. charge=False means the price list has no such event."""

    def __init__(self, actor_input, charge=True, cap_after=None, fail_writes_after=None,
                 metadata_lag=0, metadata_fails=False):
        self.input = actor_input
        self.charge = charge
        self.cap_after = cap_after
        self.fail_writes_after = fail_writes_after
        self.metadata_lag = metadata_lag
        self.metadata_fails = metadata_fails
        self.items = []
        self.charged = 0
        self.kv = {}
        self.named = FakeStore()
        self.messages = []
        self.failed = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get_input(self):
        return self.input

    async def push_data(self, row, charged_event_name=None):
        if self.fail_writes_after is not None and len(self.items) >= self.fail_writes_after:
            raise RuntimeError("dataset refused the write")
        if charged_event_name and self.cap_after is not None and self.charged >= self.cap_after:
            # The budget blocks a charged write only; a free status row still lands, and that
            # is how the buyer keeps telling what was NOT_CHECKED instead of losing it.
            return FakeResult(0, True)
        self.items.append(dict(row))
        if charged_event_name and self.charge:
            self.charged += 1
            return FakeResult(1)
        return FakeResult(0)

    async def open_dataset(self):
        return FakeDataset(self.items, metadata_fails=self.metadata_fails)

    def get_charging_manager(self):
        return self

    def get_pricing_info(self):
        if not self.charge:
            return ActorPricingInfo(pricing_model="PAY_PER_EVENT", is_pay_per_event=False,
                                    per_event_prices={}, max_total_charge_usd=None)
        return ActorPricingInfo(pricing_model="PAY_PER_EVENT", is_pay_per_event=True,
                                per_event_prices={main.EVENT: 0.0015},
                                max_total_charge_usd=4.4)

    async def set_value(self, key, value):
        self.kv[key] = value

    async def open_key_value_store(self, name=None):
        return self.named

    async def set_status_message(self, message):
        # A coroutine on the platform: forgetting await left the header empty in 0.1.
        self.messages.append(message)

    async def fail(self, **kwargs):
        self.failed = kwargs
        raise Failed(kwargs.get("status_message"))


def position(job_id, title="Engineer", office="Munich"):
    return {
        "id": str(job_id), "name": title, "office": office, "additionalOffices": [],
        "subcompany": "ACME GmbH", "department": "Engineering", "recruitingCategory": "IT",
        "employmentType": "permanent", "seniority": "experienced", "schedule": "full-time",
        "keywords": "python,backend", "occupation": "software_development",
        "occupationCategory": "it", "createdAt": "2026-03-23T15:24:29+00:00",
        "sections": [{"name": "Tasks", "value": "<p>Build things</p>"}],
    }


def run(actor, boards, fetch):
    """Run the real main() against the stand-in Actor and the given board answers."""
    original_actor, original_fetch = main.Actor, main.fetch_board

    def fake_fetch(slug, language="", timeout=60):
        answer = fetch(slug)
        if isinstance(answer, Exception):
            raise answer
        return {"jobs": answer, "empty": not answer}

    main.Actor = actor
    main.fetch_board = fake_fetch
    try:
        try:
            asyncio.run(main.main())
        except Failed:
            pass
    finally:
        main.Actor, main.fetch_board = original_actor, original_fetch
    return actor.kv["RUN_SUMMARY"]


def test_normal_run_charges_only_delivered():
    actor = FakeActor({"boards": ["ottonova"]})
    summary = run(actor, ["ottonova"], lambda slug: [position(1), position(2), position(3)])
    assert summary["delivered"] == 3, summary["delivered"]
    assert summary["chargedEvents"] == 3, summary["chargedEvents"]
    assert len(actor.items) == 3, [row["status"] for row in actor.items]
    assert all(row["billable"] is True for row in actor.items), "a priced event makes rows billable"
    assert summary["datasetItemCount"] == 3
    assert "countersMismatch" not in summary, summary.get("countersMismatch")
    assert "truncatedMismatch" not in summary
    assert actor.messages and "delivered" in actor.messages[0]
    assert actor.failed is None


def test_missing_board_is_free_and_named():
    def fetch(slug):
        if slug == "nosuch":
            raise BoardError("not_found", "career page answers 404")
        return [position(1)]

    actor = FakeActor({"boards": ["ottonova", "nosuch"]})
    summary = run(actor, ["ottonova", "nosuch"], fetch)
    assert summary["boardsNotFound"] == 1
    assert summary["delivered"] == 1 and actor.charged == 1
    assert "NOT_CHECKED_BOARD_NOT_FOUND" in [row["status"] for row in actor.items]
    assert actor.failed is None, "one missing board next to a readable one is not a failed run"
    assert summary["boardsNotFound"] == 1 and summary["boardsOk"] == 1


def test_empty_board_is_free_and_different_from_missing():
    actor = FakeActor({"boards": ["quiet"]})
    summary = run(actor, ["quiet"], lambda slug: [])
    assert summary["boardsEmpty"] == 1 and summary["boardsNotFound"] == 0
    assert [row["status"] for row in actor.items] == ["NOT_CHECKED_BOARD_EMPTY"]
    assert actor.charged == 0


def test_cap_stops_the_run_and_names_the_reason():
    actor = FakeActor({"boards": ["ottonova"]}, cap_after=1)
    summary = run(actor, ["ottonova"], lambda slug: [position(1), position(2), position(3)])
    assert summary["delivered"] == 1 and summary["chargedEvents"] == 1
    assert summary["stopReason"] == "spending-cap", summary["stopReason"]
    statuses = [row["status"] for row in actor.items]
    assert statuses.count("NOT_CHECKED_CAP") == 2, statuses
    assert "countersMismatch" not in summary, summary.get("countersMismatch")


def test_unpriced_run_delivers_free_rows():
    actor = FakeActor({"boards": ["ottonova"]}, charge=False)
    summary = run(actor, ["ottonova"], lambda slug: [position(1), position(2)])
    assert summary["delivered"] == 2 and summary["chargedEvents"] == 0
    assert all(row["billable"] is False for row in actor.items)
    assert summary["deliveredUncharged"] == 2
    # delivered two rows and charged none: that IS the signal that billing is not active
    assert summary["billingActive"] is False, summary["billingActive"]


def test_invalid_only_names_the_real_reason():
    actor = FakeActor({"boards": ["not a board !!!"]})
    summary = run(actor, ["not a board !!!"], lambda slug: [])
    assert summary["boardsInvalid"] == 1
    assert actor.failed is not None
    message = actor.failed["status_message"]
    assert "not Personio board identifiers" in message, message
    assert "0 board(s)" not in message, message


def test_duplicate_boards_are_read_once():
    seen = []

    def fetch(slug):
        seen.append(slug)
        return [position(1)]

    actor = FakeActor({"boards": ["ottonova", "https://ottonova.jobs.personio.de/"]})
    summary = run(actor, ["ottonova"], fetch)
    assert seen == ["ottonova"], seen
    assert summary["boardsDeduplicated"] == 1
    assert summary["delivered"] == 1 and actor.charged == 1


def test_max_jobs_per_board_is_counted_not_charged():
    actor = FakeActor({"boards": ["ottonova"], "maxJobsPerBoard": 2})
    summary = run(actor, ["ottonova"], lambda slug: [position(index) for index in range(5)])
    assert summary["delivered"] == 2 and actor.charged == 2
    assert summary["positionsNotRead"] == 3, summary["positionsNotRead"]
    assert summary["truncatedBoards"] == 1
    assert "countersMismatch" not in summary, summary.get("countersMismatch")


def test_dataset_write_failure_stops_and_names_it():
    actor = FakeActor({"boards": ["ottonova"]}, fail_writes_after=0)
    summary = run(actor, ["ottonova"], lambda slug: [position(1), position(2), position(3)])
    assert summary["delivered"] == 0, summary["delivered"]
    assert summary["jobsUnverified"] >= 1
    assert summary["stopReason"] == "dataset-write-error", summary["stopReason"]
    assert "countersMismatch" not in summary, summary.get("countersMismatch")


def test_second_board_is_skipped_after_a_stop():
    def fetch(slug):
        if slug == "first":
            return [position(1), position(2)]
        return [position(9)]

    actor = FakeActor({"boards": ["first", "second"]}, cap_after=1)
    summary = run(actor, ["first", "second"], fetch)
    assert summary["boardsSkipped"] == 1, summary["boardsSkipped"]
    assert summary["boardsAsked"] == 2
    assert "countersMismatch" not in summary, summary.get("countersMismatch")


def test_dataset_count_reads_the_real_metadata_model():
    """The counter is read from the model attribute; a dict-only read returns None here."""
    actor = FakeActor({"boards": ["ottonova"]})
    summary = run(actor, ["ottonova"], lambda slug: [position(1), position(2)])
    assert summary["datasetItemCountRaw"] == 2, summary["datasetItemCountRaw"]
    assert summary["datasetItemCount"] == 2, summary["datasetItemCount"]
    assert "datasetItemCountNote" not in summary


def test_unreadable_counter_is_null_with_a_reason():
    actor = FakeActor({"boards": ["ottonova"]}, metadata_fails=True)
    summary = run(actor, ["ottonova"], lambda slug: [position(1)])
    assert summary["datasetItemCount"] is None
    assert "could not be read" in (summary.get("datasetItemCountNote") or ""), summary


def test_remote_does_not_depend_on_include_details():
    entry = position(1)
    entry["sections"] = [{"name": "About", "value": "<p>This role can be done remote</p>"}]
    with_text = parse_job(entry, "acme", include_details=True)
    without_text = parse_job(entry, "acme", include_details=False)
    assert with_text["remote"] is True, with_text["remote"]
    assert without_text["remote"] is True, "a display option must not change the value"
    assert without_text["description"] is None, "the text itself is still off"


def test_filter_miss_leaves_a_named_status_row():
    actor = FakeActor({"boards": ["ottonova"], "keywords": "no-such-word"})
    summary = run(actor, ["ottonova"], lambda slug: [position(1), position(2)])
    assert summary["delivered"] == 0 and actor.charged == 0
    assert [row["status"] for row in actor.items] == ["NOT_CHECKED_NO_MATCH"], \
        [row["status"] for row in actor.items]
    assert summary["filteredOut"] == 2, summary["filteredOut"]
    assert actor.failed is None, "a filter miss on a readable board is not a failed run"


def run_all():
    failures = 0
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
            except AssertionError as exc:
                failures += 1
                print("FAIL %s: %s" % (name, exc))
            else:
                print("ok   %s" % name)
    print("%d failed" % failures)
    return failures


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
