"""Money-path checks: a run must never claim a sale the SDK did not charge for.

Run: python tests/test_money.py   (exit code 0 means every check ran)

The stand repeats the real SDK, not my belief about it: Actor.push_data asks
ChargingManager.compute_push_data_limit for the remaining budget, writes only that
prefix and charges only it, then returns ChargeResult with charged_count. That silent
cut is exactly what ate eight rows and doubled the counters before this fix, so the
fake keeps it: a fake that is kinder than the SDK hides the defect it should catch.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import main as M  # noqa: E402

CHECKS: list[tuple[str, callable]] = []


def case(fn):
    CHECKS.append((fn.__name__, fn))
    return fn


# --- the stand ------------------------------------------------------------


class ChargeResult:
    """The shape Actor.push_data really returns."""

    def __init__(self, charged_count, limit_reached=False):
        self.charged_count = charged_count
        self.event_charge_limit_reached = limit_reached


class Dataset:
    def __init__(self):
        self.items = []

    async def push_data(self, data):
        data = data if isinstance(data, list) else [data]
        self.items.extend(data)

    async def get_metadata(self):
        class Meta:
            pass
        meta = Meta()
        meta.item_count = len(self.items)
        return meta


class Store:
    def __init__(self):
        self.values = {}

    async def set_value(self, key, value):
        self.values[key] = value


class Log:
    def warning(self, *args, **kwargs):
        pass


class Charging:
    """ChargingManager: budget in events, the SDK's one-row grace included."""

    def __init__(self, budget=None, short_by=0, report=None, priced=True):
        self.budget = budget      # remaining events, None = unlimited
        self.charged = 0
        self.short_by = short_by  # an SDK that charges less than the budget allowed
        self.report = report      # None = normal, "missing" = charged_count is not reported
        self.priced = priced

    def get_pricing_info(self):
        class Info:
            pass
        info = Info()
        info.is_pay_per_event = True
        info.per_event_prices = {M.EVENT_NAME: 0.001} if self.priced else {}
        return info

    def is_event_charge_limit_reached(self, event_name):
        return self.budget is not None and self.budget - self.charged < 0

    def compute_push_data_limit(self, items_count, event_name, is_default_dataset=True):
        if self.budget is None:
            return items_count
        left = self.budget - self.charged
        if left < 0:
            return 0
        if left == 0:
            return 1  # the SDK lets one row through so the platform ends the run
        return min(items_count, left)

    def charge(self, event_name, count=1, idempotency_key=None):
        self.charged += count
        return ChargeResult(count)


class Actor:
    log = Log()

    def __init__(self, dataset, charging, input_data):
        self.dataset = dataset
        self.charging = charging
        self.input = input_data
        self.status = ""
        self.values = {}
        self.failed = None
        self.store = Store()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_input(self):
        return self.input

    async def fail(self, status_message=""):
        self.failed = status_message

    async def open_dataset(self):
        return self.dataset

    def get_charging_manager(self):
        return self.charging

    async def push_data(self, data, charged_event_name=None):
        data = data if isinstance(data, list) else [data]
        if charged_event_name is None:
            await self.dataset.push_data(data)
            return ChargeResult(len(data))
        limit = self.charging.compute_push_data_limit(
            len(data), charged_event_name, is_default_dataset=True)
        if self.charging.report == "missing":
            await self.dataset.push_data(data[:limit] if limit < len(data) else data)
            return ChargeResult(None)
        if self.charging.report == "missing_lost":
            return ChargeResult(None)
        if self.charging.report == "raise":
            await self.dataset.push_data(data[:limit] if limit < len(data) else data)
            raise RuntimeError("the charge call died")
        if self.charging.short_by:
            limit = max(0, limit - self.charging.short_by)
        if limit < len(data):
            await self.dataset.push_data(data[:limit])
        elif limit > 0:
            await self.dataset.push_data(data)
        return self.charging.charge(charged_event_name, count=limit)

    async def set_status_message(self, message):
        self.status = message

    async def set_value(self, key, value):
        self.values[key] = value

    def get_env(self):
        return {}

    async def open_key_value_store(self, name=None):
        return self.store


class Browser:
    """A search page with real cards, one distinct set per lens."""

    def __init__(self, per_lens):
        self.per_lens = per_lens
        self.mode = "stand"
        self.reason = "stand"
        self.calls = 0

    async def start(self, proxy_url=None):
        return None

    async def read_cards(self, url, timeout_s):
        self.calls += 1
        jobs = [{"id": "l%d-job-%d" % (self.calls, i), "title": "Engineer %d" % i,
                 "company": "Acme %d" % i, "location": "Remote", "posted": "1 day ago"}
                for i in range(self.per_lens)]
        return {"ok": True, "title": "Jobs | LinkedIn", "body": "60 jobs found",
                "jobs": jobs}

    async def read_job(self, url, timeout_s):
        return {"ok": True, "pageTitle": "Job", "body": "text", "description": "x" * 200}

    async def close(self):
        pass


def run(charging, jobs=20, max_jobs=None, max_searches=1):
    dataset = Dataset()
    actor = Actor(dataset, charging, {
        "keywords": "python developer",
        "maxJobs": max_jobs if max_jobs is not None else jobs,
        "maxSearches": max_searches,
        "includeDescription": False,
    })
    browser = Browser(jobs)
    old_actor, old_browser = M.Actor, M.Browser
    M.Actor, M.Browser = actor, (lambda: browser)
    try:
        asyncio.run(M.main())
    finally:
        M.Actor, M.Browser = old_actor, old_browser
    return actor, dataset


class FailBrowser(Browser):
    """Every search page answers nothing, the way a wall or a dead network does."""

    def __init__(self, reason):
        super().__init__(0)
        self.reason = reason

    async def read_cards(self, url, timeout_s):
        self.calls += 1
        return {"ok": False, "timeout": False, "error": self.reason}


def run_with(browser, charging, jobs=20, max_searches=1):
    dataset = Dataset()
    actor = Actor(dataset, charging, {
        "keywords": "python developer",
        "maxJobs": jobs,
        "maxSearches": max_searches,
        "includeDescription": False,
    })
    old_actor, old_browser = M.Actor, M.Browser
    M.Actor, M.Browser = actor, (lambda: browser)
    try:
        asyncio.run(M.main())
    finally:
        M.Actor, M.Browser = old_actor, old_browser
    return actor, dataset


def summary_of(actor):
    assert "RUN_SUMMARY" in actor.values, "the run summary was never written"
    return actor.values["RUN_SUMMARY"]


def statuses(dataset):
    return [row.get("status") for row in dataset.items]


# --- the budget -----------------------------------------------------------


@case
def test_a_batch_cut_by_the_budget_names_every_row_it_lost():
    # 20 jobs, budget for 12: the SDK alone would write 12, charge 12 and let the
    # counters claim 20 delivered and 20 charged, with no reason for the other 8.
    actor, dataset = run(Charging(budget=12))
    summary = summary_of(actor)
    assert summary["chargedEvents"] == 12, summary["chargedEvents"]
    assert summary["jobsDelivered"] == 12, summary["jobsDelivered"]
    assert summary["jobsCapped"] == 8, summary["jobsCapped"]
    assert summary["stopReason"] == "spending-cap", summary["stopReason"]
    assert summary["counterMismatch"] is False, summary
    assert statuses(dataset).count("OK") == 12, statuses(dataset)
    assert statuses(dataset).count("NOT_CHECKED_CAP") == 8, statuses(dataset)
    for row in dataset.items:
        if row.get("status") != "OK":
            assert row.get("jobId") is None, row
            assert not row.get("title"), row


@case
def test_the_free_tail_costs_nothing_and_carries_no_product():
    actor, dataset = run(Charging(budget=3))
    summary = summary_of(actor)
    free = [row for row in dataset.items if row.get("status") == "NOT_CHECKED_CAP"]
    assert len(free) == 17, len(free)
    assert summary["chargedEvents"] == 3, summary["chargedEvents"]
    for row in free:
        assert row.get("billable") is False, row
        assert "not delivered and not charged" in row.get("reason", ""), row


@case
def test_an_unlimited_budget_sells_the_whole_batch():
    actor, dataset = run(Charging())
    summary = summary_of(actor)
    assert summary["jobsDelivered"] == 20, summary["jobsDelivered"]
    assert summary["chargedEvents"] == 20, summary["chargedEvents"]
    assert summary["jobsCapped"] == 0, summary["jobsCapped"]
    assert summary["stopReason"] == "", summary["stopReason"]
    assert summary["counterMismatch"] is False, summary
    assert statuses(dataset).count("OK") == 20, statuses(dataset)


@case
def test_a_spent_budget_lets_one_row_through_like_the_sdk_and_says_so():
    # The SDK deliberately lets one row past a spent budget so the platform ends the run.
    actor, dataset = run(Charging(budget=0))
    summary = summary_of(actor)
    assert summary["chargedEvents"] == 1, summary["chargedEvents"]
    assert summary["jobsDelivered"] == 1, summary["jobsDelivered"]
    assert summary["jobsCapped"] == 19, summary["jobsCapped"]
    assert summary["stopReason"] == "spending-cap", summary["stopReason"]


@case
def test_rows_written_counts_what_landed_not_what_we_asked_for():
    actor, dataset = run(Charging(budget=12))
    summary = summary_of(actor)
    assert summary["rowsWritten"] == len(dataset.items), (
        summary["rowsWritten"], len(dataset.items))
    assert summary["byStatus"].get("OK") == 12, summary["byStatus"]
    assert summary["byStatus"].get("NOT_CHECKED_CAP") == 8, summary["byStatus"]


# --- the charge result ----------------------------------------------------


@case
def test_a_charge_smaller_than_the_batch_moves_the_counters_and_names_the_rest():
    # An SDK that charges less than it was allowed must not leave rows unaccounted.
    actor, dataset = run(Charging(short_by=5))
    summary = summary_of(actor)
    assert summary["chargedEvents"] == 15, summary["chargedEvents"]
    assert summary["jobsDelivered"] == 15, summary["jobsDelivered"]
    assert summary["jobsCapped"] == 5, summary["jobsCapped"]
    assert summary["stopReason"] == "spending-cap", summary["stopReason"]
    assert summary["counterMismatch"] is False, summary
    assert statuses(dataset).count("OK") == 15, statuses(dataset)


@case
def test_a_charge_result_without_a_count_never_claims_money():
    # The batch did land, so the rows are the buyer's, but nothing was confirmed as charged:
    # the summary says "delivered, uncharged" and never invents revenue.
    actor, dataset = run(Charging(report="missing"))
    summary = summary_of(actor)
    assert summary["chargedEvents"] == 0, summary["chargedEvents"]
    assert summary["jobsDelivered"] == 20, summary["jobsDelivered"]
    assert summary["deliveredUncharged"] == 20, summary["deliveredUncharged"]
    assert summary["stopReason"] == "charge-error", summary["stopReason"]
    assert summary["counterMismatch"] is False, summary
    assert statuses(dataset).count("OK") == 20, statuses(dataset)


@case
def test_a_dead_charge_inside_a_budget_cut_batch_leaves_one_row_per_job():
    # Budget for 12 of 20, and the charge call dies after the prefix landed. Without the
    # cut done before the write, the run cannot tell what landed: it would report 20 lost
    # while 12 real rows sat in the dataset, and add 20 more status rows on top.
    actor, dataset = run(Charging(budget=12, report="raise"))
    summary = summary_of(actor)
    assert summary["chargedEvents"] == 0, summary["chargedEvents"]
    assert summary["jobsDelivered"] == 12, summary["jobsDelivered"]
    assert summary["deliveredUncharged"] == 12, summary["deliveredUncharged"]
    assert summary["chargeUncertain"] == 0, summary["chargeUncertain"]
    assert summary["stopReason"] == "charge-error", summary["stopReason"]
    assert summary["counterMismatch"] is False, summary
    assert summary["rowsWritten"] == len(dataset.items), (summary["rowsWritten"], len(dataset.items))
    assert statuses(dataset).count("OK") == 12, statuses(dataset)
    assert statuses(dataset).count("NOT_CHECKED_CAP") == 8, statuses(dataset)


@case
def test_a_lost_batch_with_an_unreadable_charge_leaves_one_row_per_job():
    # The rows never reached the dataset and the charge is unknown: no product, one row
    # per job that says so, and nothing sold.
    actor, dataset = run(Charging(report="missing_lost"))
    summary = summary_of(actor)
    assert summary["chargedEvents"] == 0, summary["chargedEvents"]
    assert summary["jobsDelivered"] == 0, summary["jobsDelivered"]
    assert summary["chargeUncertain"] == 20, summary["chargeUncertain"]
    assert summary["stopReason"] == "charge-error", summary["stopReason"]
    assert statuses(dataset).count("NOT_CHECKED_CHARGE_UNVERIFIED") == 20, statuses(dataset)
    for row in dataset.items:
        assert row.get("jobId") is None, row
        assert row.get("billable") is False, row


@case
def test_an_unpriced_event_delivers_free_and_says_it_is_uncharged():
    # Not sold per event: nothing is charged, and the summary must not call it a mismatch.
    actor, dataset = run(Charging(priced=False))
    summary = summary_of(actor)
    assert summary["chargedEvents"] == 0, summary["chargedEvents"]
    assert summary["jobsDelivered"] == 20, summary["jobsDelivered"]
    assert summary["deliveredUncharged"] == 20, summary["deliveredUncharged"]
    assert summary["counterMismatch"] is False, summary
    assert summary["billingActive"] is False, summary["billingActive"]


@case
def test_the_spending_cap_prestop_keeps_every_row_out_of_the_dataset_as_product():
    # budget already in the red: the pre-stop fires and nothing is sold.
    actor, dataset = run(Charging(budget=-1))
    summary = summary_of(actor)
    assert summary["chargedEvents"] == 0, summary["chargedEvents"]
    assert summary["jobsDelivered"] == 0, summary["jobsDelivered"]
    assert summary["stopReason"] == "spending-cap", summary["stopReason"]
    assert statuses(dataset).count("OK") == 0, statuses(dataset)


@case
def test_a_page_that_never_loads_names_the_reason_and_charges_nothing():
    # A run that read nothing used to leave stopReason empty and no row at all: the buyer
    # saw an empty dataset and no reason, which is the same lie as a made-up number.
    reason = "playwright._impl._errors.Error: Page.goto: net::ERR_CONNECTION_RESET"
    actor, dataset = run_with(FailBrowser(reason), Charging())
    summary = summary_of(actor)
    assert summary["jobsDelivered"] == 0, summary["jobsDelivered"]
    assert summary["chargedEvents"] == 0, summary["chargedEvents"]
    assert summary["pagesFailed"] == 1, summary["pagesFailed"]
    assert summary["stopReason"] == "search-page-failed", summary["stopReason"]
    assert summary["pageErrors"] and "ERR_CONNECTION_RESET" in summary["pageErrors"][0], summary["pageErrors"]
    rows = dataset.items
    assert len(rows) == 1, len(rows)
    assert rows[0]["status"] == "NOT_CHECKED_BLOCKED", rows[0]["status"]
    assert rows[0]["billable"] is False, rows[0]["billable"]
    assert "ERR_CONNECTION_RESET" in rows[0]["reason"], rows[0]["reason"]


@case
def test_a_search_page_that_times_out_keeps_the_deadline_reason_and_still_names_it():
    reason = "TimeoutError: page.goto: Timeout 25000ms exceeded"
    actor, dataset = run_with(FailBrowser(reason), Charging())
    summary = summary_of(actor)
    assert summary["stopReason"] == "deadline", summary["stopReason"]
    rows = dataset.items
    assert len(rows) == 1, len(rows)
    assert rows[0]["billable"] is False, rows[0]["billable"]
    assert "Timeout" in rows[0]["reason"], rows[0]["reason"]


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
