"""Tests for main(): what the run summary says about companies it never reached.

The store page promises every company is reported. A spending cap used to leave
the tail of the list looking like "that company has no open jobs", which is a
lie the buyer cannot see through. These tests keep the summary honest.

No network and no platform: the Apify SDK is a stand-in instance and
ats.pull is replaced by a stub.

Run directly:  python tests/test_main.py
"""
import asyncio
import os
import sys
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _ActorFailed(Exception):
    """Actor.fail() stops the run on purpose."""


class _Charge:
    def __init__(self, reached, charged_count=1):
        self.event_charge_limit_reached = reached
        self.charged_count = charged_count


class FakeActor:
    def __init__(self):
        self.rows = []
        self.values = {}
        self.input = {}
        self.charges = 0
        self.limit_after = None
        self.charge_raises = 0
        self.zero_charge = False
        self.failed = None
        self.log = _Log()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_input(self):
        return self.input

    async def push_data(self, row):
        self.rows.append(dict(row))

    async def set_value(self, key, value, *a, **k):
        self.values[key] = value

    async def fail(self, *a, **k):
        self.failed = k
        raise _ActorFailed(k.get("status_message") or "failed")

    async def open_key_value_store(self, name=None):
        return None

    async def charge(self, event_name=None):
        self.charges += 1
        if self.charge_raises:
            self.charge_raises -= 1
            raise RuntimeError("billing is unavailable")
        reached = self.limit_after is not None and self.charges >= self.limit_after
        return _Charge(reached, 0 if self.zero_charge else 1)


FAKE = FakeActor()
_apify = types.ModuleType("apify")
_apify.Actor = FAKE
sys.modules["apify"] = _apify

from src import main as m  # noqa: E402


def _board(company, jobs=3):
    return {
        "input": company, "domain": company + ".com", "display": company, "ats": "greenhouse",
        "slug": company, "count": jobs, "error": None, "hint": None,
        "jobs": [{
            "jobId": f"{company}-{i}", "jobTitle": f"Engineer {i}", "department": "Eng",
            "location": "Remote", "remote": True, "employmentType": None,
            "salaryMin": None, "salaryMax": None, "salaryCurrency": None, "salaryPeriod": None,
            "postedAt": None, "url": f"https://example.com/{company}/{i}",
            "jobDescription": None,
        } for i in range(jobs)],
    }


def _run(companies, limit_after=None, jobs=3, include_empty=False):
    FAKE.__init__()
    FAKE.input = {"companies": companies, "cacheMinutes": 0, "includeEmptyCompanies": include_empty}
    FAKE.limit_after = limit_after
    m.ats.pull = lambda company, detail=False: _board(company, jobs)
    asyncio.run(m.main())
    return FAKE


def test_spending_cap_reports_unchecked_companies():
    fake = _run(["a.com", "b.com", "c.com"], limit_after=4)
    summary = fake.values["SUMMARY"]
    report = fake.values["COMPANY_REPORT"]
    assert summary["companiesAsked"] == 3, summary
    assert summary["companiesProcessed"] == 2, summary
    assert summary["companiesNotChecked"] == 1, summary
    assert summary["stopped"] is True and summary["stopReason"] == "spending-cap", summary
    assert summary["jobsDelivered"] == 4, summary
    assert len(report) == 3, report
    tail = [item for item in report if item["input"] == "c.com"][0]
    assert tail["status"] == "NOT_CHECKED_CAP", tail
    assert tail["reason"], tail


def test_no_cap_means_nothing_unchecked():
    fake = _run(["a.com", "b.com"], jobs=3)
    summary = fake.values["SUMMARY"]
    assert summary["companiesProcessed"] == 2, summary
    assert summary["companiesNotChecked"] == 0, summary
    assert summary["stopped"] is False and summary["stopReason"] is None, summary
    assert summary["companiesResolved"] == 2, summary
    assert summary["jobsDelivered"] == 6, summary


def test_failed_company_is_not_counted_as_resolved():
    def pull(company, detail=False):
        if company == "bad.com":
            return {"input": company, "domain": "bad.com", "display": None, "ats": None,
                    "slug": None, "count": 0, "jobs": [], "error": "NO_ATS_FOUND",
                    "hint": "pass the board slug"}
        return _board(company)

    FAKE.__init__()
    FAKE.input = {"companies": ["a.com", "bad.com"], "cacheMinutes": 0}
    m.ats.pull = pull
    asyncio.run(m.main())
    summary = FAKE.values["SUMMARY"]
    assert summary["companiesFailed"] == 1, summary
    assert summary["companiesResolved"] == 1, summary
    assert summary["companiesProcessed"] == 2, summary


def test_unchecked_company_gets_a_row_only_when_empty_rows_are_asked():
    fake = _run(["a.com", "b.com"], limit_after=1)
    statuses = [row.get("status") for row in fake.rows]
    assert "NOT_CHECKED_CAP" not in statuses, statuses
    fake2 = _run(["a.com", "b.com"], limit_after=1, include_empty=True)
    statuses2 = [row.get("status") for row in fake2.rows]
    assert "NOT_CHECKED_CAP" in statuses2, statuses2


def test_max_companies_cut_is_reported_not_hidden():
    """A cut input list used to vanish: asked 100, summary said 50."""
    FAKE.__init__()
    FAKE.input = {"companies": ["a.com", "b.com", "c.com", "d.com", "e.com"],
                  "cacheMinutes": 0, "maxCompanies": 2}
    m.ats.pull = lambda company, detail=False: _board(company)
    asyncio.run(m.main())
    summary = FAKE.values["SUMMARY"]
    report = FAKE.values["COMPANY_REPORT"]
    assert summary["companiesAsked"] == 5, summary
    assert summary["companiesProcessed"] == 2, summary
    assert summary["companiesOverMaxCompanies"] == 3, summary
    assert summary["companiesNotChecked"] == 3, summary
    assert summary["companiesWithNoJobs"] == 0, summary
    assert len(report) == 5, report
    cut = [item for item in report if item.get("status") == "NOT_CHECKED_MAX_COMPANIES"]
    assert [item["input"] for item in cut] == ["c.com", "d.com", "e.com"], cut
    assert all(item.get("reason") for item in cut), cut


def test_max_companies_cut_rows_appear_with_empty_rows():
    FAKE.__init__()
    FAKE.input = {"companies": ["a.com", "b.com", "c.com"], "cacheMinutes": 0,
                  "maxCompanies": 2, "includeEmptyCompanies": True}
    m.ats.pull = lambda company, detail=False: _board(company)
    asyncio.run(m.main())
    statuses = [row.get("status") for row in FAKE.rows]
    assert "NOT_CHECKED_MAX_COMPANIES" in statuses, statuses
    assert FAKE.values["SUMMARY"]["jobsDelivered"] == 6, FAKE.values["SUMMARY"]


def test_summary_always_written():
    fake = _run(["a.com"], jobs=1)
    assert "SUMMARY" in fake.values and fake.values["SUMMARY"]["jobsDelivered"] == 1



def test_per_company_cap_reports_what_it_held_back():
    """maxJobsPerCompany=2 on a 5-job board used to say nothing about the other 3."""
    FAKE.__init__()
    FAKE.input = {"companies": ["a.com"], "cacheMinutes": 0, "maxJobsPerCompany": 2}
    m.ats.pull = lambda company, detail=False: _board(company, jobs=5)
    asyncio.run(m.main())
    summary = FAKE.values["SUMMARY"]
    assert summary["jobsDelivered"] == 2, summary
    assert summary["jobsMatched"] == 5, summary
    assert summary["jobsOverMaxJobsPerCompany"] == 3, summary


def test_second_row_on_the_same_board_is_a_duplicate_not_no_jobs():
    """stripe.com and its careers URL share one board: the second must not read as empty."""
    def pull(company, detail=False):
        board = _board("acme", jobs=3)
        board["input"] = company
        board["display"] = company
        return board

    FAKE.__init__()
    FAKE.input = {"companies": ["stripe.com", "https://stripe.com/careers"], "cacheMinutes": 0,
                  "includeEmptyCompanies": True}
    m.ats.pull = pull
    asyncio.run(m.main())
    report = FAKE.values["COMPANY_REPORT"]
    summary = FAKE.values["SUMMARY"]
    assert len(report) == 2, report
    second = report[1]
    assert second.get("status") == "DUPLICATE_BOARD", second
    assert second.get("duplicateOf") == "stripe.com", second
    assert summary["companiesDuplicateBoards"] == 1, summary
    assert summary["companiesWithNoJobs"] == 0, summary
    assert summary["jobsDelivered"] == 3, summary
    statuses = [row.get("status") for row in FAKE.rows]
    assert "NO_JOBS" not in statuses, statuses


def test_company_cut_midway_is_marked_partial():
    """The cap stopped the run inside a company: half a board must not look whole."""
    fake = _run(["a.com"], limit_after=1, jobs=3)
    report = fake.values["COMPANY_REPORT"]
    summary = fake.values["SUMMARY"]
    entry = [item for item in report if item["input"] == "a.com"][0]
    assert entry.get("partial") is True, entry
    assert entry["delivered"] == 1 and entry["jobsOnBoard"] == 3, entry
    assert entry.get("reason"), entry
    assert summary["companiesPartiallyProcessed"] == 1, summary
    assert summary["companiesWithNoJobs"] == 0, summary


def test_duplicate_board_with_a_fresher_answer_delivers_new_jobs():
    """The second row on one board must not throw away jobs only it carried."""
    def pull(company, detail=False):
        board = _board("acme", jobs=5 if "careers" in company else 3)
        board["input"] = company
        board["display"] = company
        return board

    FAKE.__init__()
    FAKE.input = {"companies": ["a.com", "https://a.com/careers"], "cacheMinutes": 0,
                  "includeEmptyCompanies": True}
    m.ats.pull = pull
    asyncio.run(m.main())
    report = FAKE.values["COMPANY_REPORT"]
    summary = FAKE.values["SUMMARY"]
    assert summary["jobsDelivered"] == 5, summary
    assert summary["jobsMatched"] == 3, summary
    second = report[1]
    assert second.get("duplicateOf") == "a.com", second
    assert second["delivered"] == 2, second
    assert "fresher" in (second.get("reason") or ""), second
    assert summary["companiesWithNoJobs"] == 0, summary


def test_two_billing_failures_stop_the_run_and_say_so():
    """Charging that keeps throwing used to hand out the rest of the list for free."""
    FAKE.__init__()
    FAKE.input = {"companies": ["a.com", "b.com"], "cacheMinutes": 0}
    FAKE.charge_raises = 2
    m.ats.pull = lambda company, detail=False: _board(company, jobs=3)
    asyncio.run(m.main())
    summary = FAKE.values["SUMMARY"]
    assert summary["billingActive"] is False, summary
    assert summary["stopReason"] == "billing-error", summary
    assert summary["stopped"] is True, summary
    assert summary["jobsDelivered"] == 2, summary
    first = FAKE.values["COMPANY_REPORT"][0]
    assert first.get("partial") is True, first
    assert "billing failures" in (first.get("reason") or ""), first
    tail = [item for item in FAKE.values["COMPANY_REPORT"] if item["input"] == "b.com"]
    assert tail and tail[0]["status"] == "NOT_CHECKED_BILLING", tail


def test_asked_counts_distinct_companies_and_repeats_are_shown():
    FAKE.__init__()
    FAKE.input = {"companies": ["a.com", "a.com", "A.COM"], "cacheMinutes": 0}
    m.ats.pull = lambda company, detail=False: _board(company)
    asyncio.run(m.main())
    summary = FAKE.values["SUMMARY"]
    assert summary["companiesAsked"] == 1, summary
    assert summary["companiesDeduplicated"] == 2, summary


def test_partial_reason_names_the_per_company_cap():
    FAKE.__init__()
    FAKE.input = {"companies": ["a.com"], "cacheMinutes": 0, "maxJobsPerCompany": 2}
    FAKE.limit_after = 1
    m.ats.pull = lambda company, detail=False: _board(company, jobs=5)
    asyncio.run(m.main())
    entry = FAKE.values["COMPANY_REPORT"][0]
    assert entry.get("partial") is True, entry
    assert "maxJobsPerCompany" in (entry.get("reason") or ""), entry
    assert FAKE.values["SUMMARY"]["jobsOverMaxJobsPerCompany"] == 3


def test_failed_company_record_carries_a_status():
    def pull(company, detail=False):
        return {"input": company, "domain": company, "display": None, "ats": None, "slug": None,
                "count": 0, "jobs": [], "error": "NO_ATS_FOUND", "hint": "pass the board slug"}

    FAKE.__init__()
    FAKE.input = {"companies": ["bad.com"], "cacheMinutes": 0}
    m.ats.pull = pull
    asyncio.run(m.main())
    entry = FAKE.values["COMPANY_REPORT"][0]
    assert entry["status"] == "FAILED", entry
    assert FAKE.values["SUMMARY"]["companiesWithNoJobs"] == 0, FAKE.values["SUMMARY"]


def test_duplicate_extra_jobs_are_named_in_the_summary():
    """jobsMatched counts a board once, so the jobs a second answer carried need a name."""
    def pull(company, detail=False):
        board = _board("acme", jobs=5 if "careers" in company else 3)
        board["input"] = company
        board["display"] = company
        return board

    FAKE.__init__()
    FAKE.input = {"companies": ["a.com", "https://a.com/careers"], "cacheMinutes": 0}
    m.ats.pull = pull
    asyncio.run(m.main())
    summary = FAKE.values["SUMMARY"]
    assert summary["jobsDelivered"] == 5, summary
    assert summary["jobsMatched"] == 3, summary
    assert summary["jobsFromDuplicateAnswers"] == 2, summary
    assert (summary["jobsMatched"] + summary["jobsFromDuplicateAnswers"]
            == summary["jobsDelivered"]), summary


def test_job_without_id_does_not_collapse_the_board():
    """Five jobs without an id used to share one key: one row delivered, four lost."""
    def pull(company, detail=False):
        board = _board(company, jobs=5)
        for job in board["jobs"]:
            job["jobId"] = None
            job["url"] = None
        return board

    FAKE.__init__()
    FAKE.input = {"companies": ["a.com"], "cacheMinutes": 0}
    m.ats.pull = pull
    asyncio.run(m.main())
    summary = FAKE.values["SUMMARY"]
    assert summary["jobsDelivered"] == 5, summary
    assert len([row for row in FAKE.rows if row.get("status") == "FOUND"]) == 5, FAKE.rows


def test_pull_exception_becomes_a_failed_company_not_an_empty_run():
    """One fetcher raising used to kill the run with zero rows and no summary at all."""
    def pull(company, detail=False):
        if company == "boom.com":
            raise ValueError("unexpected shape")
        return _board(company)

    FAKE.__init__()
    FAKE.input = {"companies": ["a.com", "boom.com", "c.com"], "cacheMinutes": 0}
    m.ats.pull = pull
    asyncio.run(m.main())
    summary = FAKE.values["SUMMARY"]
    assert summary["jobsDelivered"] == 6, summary
    assert summary["companiesFailed"] == 1, summary
    assert summary["companiesProcessed"] == 3, summary
    entry = [item for item in FAKE.values["COMPANY_REPORT"] if item["input"] == "boom.com"][0]
    assert entry["status"] == "FAILED", entry
    assert "PULL_FAILED" in (entry.get("error") or ""), entry


def test_partial_reason_of_a_duplicate_does_not_invent_a_cap():
    """A duplicate entry has no per-company cap, so it must not say '0 more were held back'."""
    def pull(company, detail=False):
        board = _board("acme", jobs=5 if "careers" in company else 3)
        board["input"] = company
        board["display"] = company
        return board

    FAKE.__init__()
    FAKE.input = {"companies": ["a.com", "https://a.com/careers"], "cacheMinutes": 0}
    FAKE.limit_after = 4
    m.ats.pull = pull
    asyncio.run(m.main())
    entries = [item for item in FAKE.values["COMPANY_REPORT"] if item.get("partial")]
    assert entries, FAKE.values["COMPANY_REPORT"]
    reason = entries[-1].get("reason") or ""
    assert "0 more were held back" not in reason, reason
    assert "maxJobsPerCompany" not in reason, reason


def test_empty_input_stops_with_a_message_not_a_traceback():
    """Run pressed with an empty box: a readable stop, not a Python traceback."""
    FAKE.__init__()
    FAKE.input = {"companies": []}
    stopped = False
    try:
        asyncio.run(m.main())
    except _ActorFailed:
        stopped = True
    assert stopped, "main() must end through Actor.fail()"
    assert FAKE.failed and FAKE.failed.get("status_message"), FAKE.failed
    assert "companies is empty" in FAKE.failed["status_message"], FAKE.failed


def test_zero_charge_means_billing_is_not_active():
    """SDK 4.x answers (0, False) when the run is not pay-per-event: the summary
    used to claim every row was paid for."""
    FAKE.__init__()
    FAKE.input = {"companies": ["a.com"], "cacheMinutes": 0}
    FAKE.zero_charge = True
    m.ats.pull = lambda company, detail=False: _board(company, 3)
    asyncio.run(m.main())
    summary = FAKE.values["SUMMARY"]
    assert summary["jobsDelivered"] == 3, summary
    assert summary["billingActive"] is False, summary
    assert summary["stopReason"] == "billing-not-active", summary
    assert summary["stopped"] is False, summary
    assert FAKE.charges == 1, ("charging must stop once the platform says nothing is charged",
                              FAKE.charges)


def test_partial_company_is_not_counted_as_resolved():
    """The cap stopped inside the second company: it was read in part, so it is not resolved."""
    fake = _run(["a.com", "b.com"], limit_after=4)
    summary = fake.values["SUMMARY"]
    assert summary["companiesProcessed"] == 2, summary
    assert summary["companiesPartiallyProcessed"] == 1, summary
    assert summary["companiesResolved"] == 1, summary
    assert summary["companiesFailed"] == 0, summary


def test_duplicate_cap_counts_only_fresh_jobs():
    """A second row for the same board: the cap applies to the fresh jobs it adds,
    not to the raw answer (three fresh jobs behind two known ones used to vanish)."""
    def pull(company, detail=False):
        board = _board("shared", 5 if company == "b.com" else 2)
        board["input"] = company
        return board

    FAKE.__init__()
    FAKE.input = {"companies": ["a.com", "b.com"], "cacheMinutes": 0, "maxJobsPerCompany": 2}
    m.ats.pull = pull
    asyncio.run(m.main())
    summary = FAKE.values["SUMMARY"]
    report = FAKE.values["COMPANY_REPORT"]
    assert summary["jobsDelivered"] == 4, summary
    assert summary["jobsFromDuplicateAnswers"] == 2, summary
    second = [item for item in report if item["input"] == "b.com"][0]
    assert second.get("duplicateOf") == "a.com", second
    assert second.get("jobsOverMaxJobsPerCompany") == 1, second
    assert "nothing new" not in (second.get("reason") or ""), second


def test_job_key_is_stable_across_answer_order():
    """A board with neither jobId nor url must not be charged twice when it comes back reordered."""
    first = {"jobTitle": "Engineer", "location": "Remote", "department": "Eng", "postedAt": None}
    second = {"jobTitle": "Designer", "location": "Kyiv", "department": "Design", "postedAt": None}
    key_a = m.job_key("greenhouse", "acme", first, 0)
    key_b = m.job_key("greenhouse", "acme", second, 1)
    assert key_a == m.job_key("greenhouse", "acme", first, 7), (key_a, "position must not leak in")
    assert key_b == m.job_key("greenhouse", "acme", second, 0), key_b
    assert key_a != key_b, (key_a, key_b)


def run_all():

    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:
            failed.append((name, exc))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
