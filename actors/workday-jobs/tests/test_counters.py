"""Guards for the counter that lied.

QA 0.1.7, live run Hfw86vfh9O6M8klKm: two boards, 49 of 80 rows dropped for an
unproven country, and SUMMARY said `rowsWithoutCountry: 0` - the counter was read
AFTER the filter that had just thrown those rows away. A run that loses rows must
say so, or the buyer reads "29 delivered" and never learns about the other 49.

Also here: a probe must not smuggle a description into a run that did not ask for
one, the city filter has to work on rows whose location is "5 Locations", and a
board we could not read is not a board we read.
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import workday


class FakeLog:
    def __init__(self):
        self.lines = []

    def _write(self, msg, *args, **kwargs):
        self.lines.append(str(msg))

    info = warning = error = debug = _write


class FakeActor:
    """The parts of apify.Actor that main() touches, and nothing else.

    `cap` models the buyer's spending limit the way SDK 4.2.0 documents it: the
    events that fit are charged plus one more, that last charge reports
    `event_charge_limit_reached=True`, and a run already strictly over budget
    charges nothing at all (`charged_count=0`).
    """

    def __init__(self, input_data, cap=None):
        self.input = input_data
        self.log = FakeLog()
        self.rows = []
        self.values = {}
        self.charged = 0
        self.cap = cap
        self.charge_error = None
        self.push_error = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_input(self):
        return self.input

    async def push_data(self, row):
        if self.push_error:
            raise RuntimeError(self.push_error)
        self.rows.append(row)

    async def set_value(self, key, value):
        self.values[key] = value

    async def open_key_value_store(self, name=None):
        return self

    async def get_value(self, key):
        return None

    async def charge(self, event_name, idempotency_key=None):
        if self.charge_error:
            raise RuntimeError(self.charge_error)
        if self.cap is None:
            self.charged += 1
            return types.SimpleNamespace(event_charge_limit_reached=False, charged_count=1,
                                         chargeable_within_limit={"job-found": 100})
        if self.charged >= self.cap:
            return types.SimpleNamespace(event_charge_limit_reached=True, charged_count=0,
                                         chargeable_within_limit={"job-found": 0})
        self.charged += 1
        return types.SimpleNamespace(event_charge_limit_reached=self.charged >= self.cap,
                                     charged_count=1,
                                     chargeable_within_limit={"job-found": 0})


def _run(input_data, listed, detail=None, cap=None, charge_error=None, push_error=None):
    """Run main() against fake Workday answers and give back the actor."""
    fake = types.ModuleType("apify")
    fake.Actor = object
    sys.modules["apify"] = fake
    import src.main as main_mod

    actor = FakeActor(input_data, cap=cap)
    actor.charge_error = charge_error
    actor.push_error = push_error
    main_mod.Actor = actor

    original_list, original_detail = workday.list_jobs, workday.job_detail
    workday.list_jobs = lambda tenant, wd, site, max_jobs=200, page_delay=0.15, search_text="": listed(tenant)
    workday.job_detail = lambda tenant, wd, site, path: (detail or (lambda t, p: {"error": "HTTP_404"}))(tenant, path)
    try:
        asyncio.run(main_mod.main())
    finally:
        workday.list_jobs, workday.job_detail = original_list, original_detail
    return actor


BOARD = "https://acme.wd1.myworkdayjobs.com/External"


def _posting(title, location, path):
    return {"title": title, "locationsText": location, "externalPath": path,
            "bulletFields": [path.rsplit("_", 1)[-1]], "postedOn": "Posted Today"}


def test_unproven_rows_are_counted_even_when_the_budget_is_zero():
    # 2 rows name no country, 1 names a US state; budget 0 -> 2 rows are LOST
    listed = lambda tenant: {"total": 3, "jobs": [
        _posting("Engineer", "5 Locations", "/job/A_JR1"),
        _posting("Engineer", "Berlin", "/job/B_JR2"),
        _posting("Engineer", "Plano, TX", "/job/C_JR3"),
    ]}
    actor = _run({"companies": [BOARD], "countries": ["US"], "maxCountryProbes": 0,
                  "cacheMinutes": 0}, listed)
    summary = actor.values["SUMMARY"]
    assert summary["jobsDelivered"] == 1, summary
    assert summary["rowsWithoutCountry"] == 2, summary          # was 0
    assert summary["countryProbes"] == 0
    assert any("left unproven" in line for line in actor.log.lines), actor.log.lines


def test_a_failed_probe_is_a_lost_row_not_a_silent_one():
    listed = lambda tenant: {"total": 1, "jobs": [_posting("Engineer", "5 Locations", "/job/A_JR1")]}
    actor = _run({"companies": [BOARD], "countries": ["US"], "maxCountryProbes": 10,
                  "cacheMinutes": 0}, listed,
                 detail=lambda tenant, path: {"error": "HTTP_500"})
    summary = actor.values["SUMMARY"]
    assert summary["jobsDelivered"] == 0
    assert summary["rowsWithoutCountry"] == 1, summary
    assert summary["countryProbesResolved"] == 0


def test_a_probe_tells_the_country():
    listed = lambda tenant: {"total": 1, "jobs": [_posting("Engineer", "5 Locations", "/job/A_JR1")]}
    actor = _run({"companies": [BOARD], "countries": ["US"], "maxCountryProbes": 10,
                  "cacheMinutes": 0}, listed,
                 detail=lambda tenant, path: {"error": None, "country": {"descriptor": "United States of America"},
                                              "location": "Plano, TX"})
    summary = actor.values["SUMMARY"]
    assert summary["jobsDelivered"] == 1, summary
    assert summary["rowsWithoutCountry"] == 0
    row = actor.rows[0]
    assert row["country"] == "US", row


def test_a_probe_does_not_deliver_a_description_nobody_asked_for():
    listed = lambda tenant: {"total": 1, "jobs": [_posting("Engineer", "5 Locations", "/job/A_JR1")]}
    actor = _run({"companies": [BOARD], "countries": ["US"], "maxCountryProbes": 10,
                  "includeDescription": False, "cacheMinutes": 0}, listed,
                 detail=lambda tenant, path: {"error": None, "country": {"descriptor": "United States of America"},
                                              "jobDescription": "<p>long text</p>"})
    assert actor.values["SUMMARY"]["descriptionsFetched"] == 0
    assert actor.rows[0]["jobDescription"] is None, actor.rows[0]


def test_the_city_filter_sees_a_location_resolved_by_the_probe():
    # "5 Locations" can never match "Berlin" before the probe resolves it
    listed = lambda tenant: {"total": 1, "jobs": [_posting("Engineer", "5 Locations", "/job/A_JR1")]}
    actor = _run({"companies": [BOARD], "location": "Berlin", "maxCountryProbes": 10,
                  "cacheMinutes": 0}, listed,
                 detail=lambda tenant, path: {"error": None, "location": "Berlin, Germany"})
    assert actor.values["SUMMARY"]["jobsDelivered"] == 1, actor.values["SUMMARY"]
    assert actor.rows[0]["location"] == "Berlin, Germany"


def test_a_board_that_could_not_be_read_is_not_counted_as_read():
    def listed(tenant):
        if tenant == "broken":
            return {"error": "HTTP_404", "total": None, "jobs": []}
        return {"total": 1, "jobs": [_posting("Engineer", "Plano, TX", "/job/A_JR1")]}

    actor = _run({"companies": ["https://acme.wd1.myworkdayjobs.com/External",
                                "https://broken.wd1.myworkdayjobs.com/External"],
                  "cacheMinutes": 0}, listed)
    summary = actor.values["SUMMARY"]
    assert summary["boardsRead"] == 1, summary
    assert summary["boardsFailed"] == 1, summary


def test_country_is_one_spelling_a_code():
    posting = _posting("Engineer", "US, CA, Santa Clara", "/job/A_JR1")
    job = workday.normalize(posting, "acme", "wd1", "External")
    assert job["country"] == "US"
    posting_de = _posting("Engineer", "Berlin, Germany", "/job/B_JR2")
    assert workday.normalize(posting_de, "acme", "wd1", "External")["country"] == "DE"
    posting_bare = _posting("Engineer", "Somewhere", "/job/C_JR3")
    assert workday.normalize(posting_bare, "acme", "wd1", "External")["country"] is None


def test_a_country_we_do_not_know_is_passed_through_not_dropped():
    assert workday.canonical_country("Wakanda") == "Wakanda"
    assert workday.canonical_country("United States of America") == "US"
    assert workday.canonical_country("united states") == "US"
    assert workday.canonical_country("GB") == "GB"
    assert workday.canonical_country(None) is None


# --------------------------------------------------------------------------- #
# QA 0.1.8: the charge told us the truth and we did not listen
# --------------------------------------------------------------------------- #
def test_the_last_row_the_cap_paid_for_is_delivered():
    """ChargeResult says `event_charge_limit_reached` = this event WAS charged.

    Live run HTwGDQFtghVZKdABV: cap $0.02, 16 charged, 15 rows delivered. The
    16th row was paid for and thrown away - the row that filled the cap is the
    one that must be delivered, and only the NEXT one is refused.
    """
    listed = lambda tenant: {"total": 3, "jobs": [
        _posting("Engineer 1", "Plano, TX", "/job/A_JR1"),
        _posting("Engineer 2", "Plano, TX", "/job/B_JR2"),
        _posting("Engineer 3", "Plano, TX", "/job/C_JR3"),
    ]}
    actor = _run({"companies": [BOARD], "cacheMinutes": 0}, listed, cap=2)
    summary = actor.values["SUMMARY"]
    assert summary["jobsDelivered"] == 2, summary      # was 1 with 2 charged
    assert actor.charged == 2, actor.charged
    assert len([r for r in actor.rows if r.get("status") == "FOUND"]) == 2
    assert summary["stoppedByChargeLimit"] is True
    assert summary["rowsRefusedByChargeLimit"] == 0
    assert summary["stopReason"] == "charge-limit"


def test_a_row_the_cap_refused_is_neither_charged_nor_delivered():
    listed = lambda tenant: {"total": 2, "jobs": [
        _posting("Engineer 1", "Plano, TX", "/job/A_JR1"),
        _posting("Engineer 2", "Plano, TX", "/job/B_JR2"),
    ]}
    actor = _run({"companies": [BOARD], "cacheMinutes": 0}, listed, cap=0)
    summary = actor.values["SUMMARY"]
    assert summary["jobsDelivered"] == 0, summary
    assert summary["rowsRefusedByChargeLimit"] == 1, summary
    assert actor.charged == 0
    assert actor.rows == [] or all(r.get("status") != "FOUND" for r in actor.rows)


def test_a_board_the_run_never_reached_is_reported_not_hidden():
    def listed(tenant):
        return {"total": 1, "jobs": [_posting("Engineer", "Plano, TX", f"/job/{tenant}_JR1")]}

    actor = _run({"companies": ["https://acme.wd1.myworkdayjobs.com/External",
                               "https://beta.wd1.myworkdayjobs.com/External"],
                  "cacheMinutes": 0}, listed, cap=1)
    summary = actor.values["SUMMARY"]
    report = actor.values["BOARD_REPORT"]
    assert summary["jobsDelivered"] == 1, summary
    assert summary["boardsNotReadAfterStop"] == 1, summary
    assert len(report) == 2, report                    # the second board is in the report
    assert report[1]["error"] == "NOT_READ", report[1]


def test_a_charge_that_keeps_failing_delivers_the_row_and_stops():
    """Both attempts raised: no answer can say whether the row was billed.

    Keeping money for a row the buyer never got is the worse of the two mistakes,
    so the row is delivered and the run stops - at most one row per run is handed
    out in that doubt.
    """
    listed = lambda tenant: {"total": 2, "jobs": [
        _posting("Engineer 1", "Plano, TX", "/job/A_JR1"),
        _posting("Engineer 2", "Plano, TX", "/job/B_JR2"),
    ]}
    actor = _run({"companies": [BOARD], "cacheMinutes": 0}, listed, charge_error="HTTP_500")
    summary = actor.values["SUMMARY"]
    assert summary["jobsDelivered"] == 1, summary          # was 0, though it may be billed
    assert summary["chargeFailures"] == 1, summary
    assert summary["stopReason"] == "charge-uncertain", summary
    assert len([r for r in actor.rows if r.get("status") == "FOUND"]) == 1
    assert any("failed twice" in line for line in actor.log.lines), actor.log.lines


def test_a_row_the_dataset_refuses_is_named_in_delivery_failures():
    """A paid row the dataset would not take must leave a trail, not just a count.

    QA 0.1.10: DELIVERY_FAILURES held {"count": 1, "note": ...} with no job id, so
    the promise "a trail to claim a refund against" was empty.
    """
    listed = lambda tenant: {"total": 1, "jobs": [_posting("Engineer 1", "Plano, TX", "/job/A_JR1")]}
    actor = _run({"companies": [BOARD], "cacheMinutes": 0}, listed,
                 push_error="DATASET_WRITE_FAILED")
    summary = actor.values["SUMMARY"]
    record = actor.values["DELIVERY_FAILURES"]
    assert summary["stopReason"] == "delivery-error", summary
    assert summary["jobsDelivered"] == 0, summary
    assert record["count"] == 1, record
    assert record["rows"][0]["jobId"], record            # the row itself, not just a number
    assert record["rows"][0]["tenant"] == "acme", record
    assert "DATASET_WRITE_FAILED" in record["rows"][0]["error"], record


# --------------------------------------------------------------------------- #
# QA 0.1.8 (B2 + yellow): a lost row is only lost if it could have been delivered
# --------------------------------------------------------------------------- #
def test_rows_the_city_filter_would_drop_anyway_are_not_called_lost():
    """countries=US + location=zzz, budget 0: nothing could ever be delivered.

    QA run d2LZImYafrh13IRp7 said rowsWithoutCountry 27 and advised "raise
    maxCountryProbes" - advice that cannot help, because the city filter would
    throw those rows away even with a country.
    """
    listed = lambda tenant: {"total": 2, "jobs": [
        _posting("Engineer 1", "Berlin", "/job/A_JR1"),
        _posting("Engineer 2", "Austin", "/job/B_JR2"),
    ]}
    actor = _run({"companies": [BOARD], "countries": ["US"], "location": "zzz-no-such-city",
                  "maxCountryProbes": 0, "cacheMinutes": 0}, listed)
    summary = actor.values["SUMMARY"]
    assert summary["jobsDelivered"] == 0, summary
    assert summary["rowsWithoutCountry"] == 0, summary   # was 2 - a false alarm


def test_no_probe_is_spent_on_a_row_the_city_filter_already_dropped():
    """A row naming another city can never match: probing it is waste.

    Measured on nvidia: 97 probes for a city filter that could keep 47 rows.
    """
    listed = lambda tenant: {"total": 2, "jobs": [
        _posting("Engineer 1", "Berlin", "/job/A_JR1"),
        _posting("Engineer 2", "5 Locations", "/job/B_JR2"),
    ]}
    actor = _run({"companies": [BOARD], "location": "Santa Clara",
                  "maxCountryProbes": 10, "cacheMinutes": 0}, listed,
                 detail=lambda tenant, path: {"error": None, "location": "Santa Clara, CA"})
    summary = actor.values["SUMMARY"]
    assert summary["countryProbes"] == 1, summary        # was 2
    assert summary["rowsNotProbedCityMismatch"] == 1, summary
    assert summary["jobsDelivered"] == 1, summary
    assert actor.rows[0]["location"] == "Santa Clara, CA"


def test_the_city_filter_reads_additional_locations():
    job = {"jobTitle": "Engineer", "location": "Plano, TX",
           "additionalLocations": [{"descriptor": "Berlin, Germany"}]}
    assert workday.matches(job, "", "Berlin") is True
    assert workday.matches(job, "", "Paris") is False


def test_vague_locations_still_get_a_probe():
    assert workday.location_is_informative({"location": "5 Locations"}) is False
    assert workday.location_is_informative({"location": "Berlin"}) is True
    assert workday.location_is_informative({"location": "Remote"}) is False
