"""Money checks: the real main() against a stand that repeats the SDK.

The stand writes rows BEFORE it charges, like Actor.push_data does, and can refuse the
write, fail the charge, lag the counter, refuse the counter and refuse the price - the four
ways one exception used to hide the truth. A stand that always answers honestly hides the
whole class.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import main as app


class Meta:
    def __init__(self, item_count):
        self.item_count = item_count


class Result:
    def __init__(self, charged_count, limit_reached=False):
        self.charged_count = charged_count
        self.event_charge_limit_reached = limit_reached


class Pricing:
    def __init__(self, pay_per_event, prices):
        self.is_pay_per_event = pay_per_event
        self.per_event_prices = prices


class Charging:
    def __init__(self, stand):
        self.stand = stand

    def get_pricing_info(self):
        if self.stand["price_raises"]:
            raise RuntimeError("price list is not readable")
        ppe = self.stand["ppe"]
        prices = {app.EVENT_NAME: 0.0008} if ppe else {}
        return Pricing(ppe, prices)

    def is_event_charge_limit_reached(self, name):
        return bool(self.stand["limit_reached"])

    def compute_push_data_limit(self, count, name, is_default_dataset=True):
        room = self.stand["room"]
        return count if room is None else max(0, min(count, room))


class Dataset:
    def __init__(self, stand):
        self.stand = stand
        self.rows = []

    async def get_metadata(self):
        s = self.stand
        s["meta_reads"] += 1
        if s["meta_raises"] > 0:
            s["meta_raises"] -= 1
            raise RuntimeError("metadata is not readable")
        count = len(self.rows)
        if s["lag_reads"] > 0:
            s["lag_reads"] -= 1
            count = max(0, count - s["lag_size"])
        return Meta(count)

    async def push_data(self, data, charged_event_name=None):
        s = self.stand
        s["push_calls"] += 1
        s["push_sizes"].append(len(data))
        if s["fail_before_write"] or (s["fail_before_write_at"]
                                      and s["push_calls"] == s["fail_before_write_at"]):
            raise RuntimeError("the dataset refused the rows")
        self.rows.extend(data)
        if not s["ppe"]:
            return Result(0)
        if s["fail_charge"]:
            raise RuntimeError("the charge raised")
        room = s["room"]
        charged = len(data) if room is None else max(0, min(len(data), room))
        if room is not None:
            s["room"] = room - charged
        if s["short_by"]:
            charged = max(0, charged - s["short_by"])
        s["charged"] += charged
        return Result(charged, charged < len(data))


class Store:
    def __init__(self, stand, name):
        self.stand = stand
        self.name = name

    async def set_value(self, key, value):
        self.stand["stores"].append((self.name, key))
        if self.name is None:
            self.stand["summary"] = value


class Log:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class FakeActor:
    stand = {}
    log = Log()

    @classmethod
    async def init(cls):
        return None

    @classmethod
    async def get_input(cls):
        return cls.stand["input"]

    @classmethod
    def get_charging_manager(cls):
        return Charging(cls.stand)

    @classmethod
    async def open_dataset(cls):
        if "dataset" not in cls.stand:
            cls.stand["dataset"] = Dataset(cls.stand)
        return cls.stand["dataset"]

    @classmethod
    async def open_key_value_store(cls, name=None):
        return Store(cls.stand, name)

    @classmethod
    async def push_data(cls, data, charged_event_name=None):
        dataset = await cls.open_dataset()
        return await dataset.push_data(data, charged_event_name)


    @classmethod
    async def set_status_message(cls, text):
        cls.stand["status"] = text

    @classmethod
    async def fail(cls, status_message=""):
        cls.stand["failed"] = status_message
        raise SystemExit(1)

    @classmethod
    async def exit(cls):
        return None


def stand(**over):
    base = {
        "input": {"papers": []},
        "ppe": True,
        "price_raises": False,
        "room": None,
        "limit_reached": False,
        "fail_before_write": False,
        "fail_before_write_at": 0,
        "fail_charge": False,
        "meta_raises": 0,
        "lag_reads": 0,
        "lag_size": 1,
        "short_by": 0,
        "meta_reads": 0,
        "push_calls": 0,
        "push_sizes": [],
        "charged": 0,
        "stores": [],
        "rows_read": {},
        "refused": {},
        "read_error": None,
        "read_calls": 0,
        "status": "",
        "failed": "",
        "summary": {},
    }
    base.update(over)
    base["datasetId"] = "dataset-under-test"
    return base


def row_for(paper_id):
    return {"arxivId": paper_id, "version": 1, "title": f"paper {paper_id}",
            "authors": ["A. Author"], "publishedAt": "2020-01-01T00:00:00Z",
            "updatedAt": "2020-01-01T00:00:00Z", "primaryCategory": "cs.CL",
            "categories": ["cs.CL"], "comment": None, "journalRef": None, "doi": None,
            "abstract": "text", "absUrl": f"https://arxiv.org/abs/{paper_id}",
            "pdfUrl": f"https://arxiv.org/pdf/{paper_id}", "billable": True}


def fake_read(client, ids, with_abstract=True, timeout=None):
    stand_ref["read_calls"] += 1
    refused = {key: value for key, value in stand_ref["refused"].items() if key in ids}
    if stand_ref["read_error"] is not None:
        return {}, stand_ref["read_error"], {}
    found = {key: dict(value) for key, value in stand_ref["rows_read"].items()
             if key in ids and key not in refused}
    return found, (stand_ref["read_error"] or ("arXiv answered HTTP 500" if refused else None)), refused


stand_ref = {}


def run(**over):
    global stand_ref
    data = stand(**over)
    stand_ref = data
    os.environ["ACTOR_RUN_ID"] = "test-run-1"
    FakeActor.stand = data
    app.Actor = FakeActor
    app.arxiv.read_papers = fake_read
    try:
        asyncio.run(app.main())
    except SystemExit:
        pass
    return data


def by_status(data):
    return data["summary"].get("byStatus", {})


# ---------------------------------------------------------------- checks

def test_healthy_run_sells_and_writes():
    d = run(input={"papers": ["1706.03762", "2103.00020"]},
            rows_read={"1706.03762": row_for("1706.03762"),
                       "2103.00020": row_for("2103.00020")})
    s = d["summary"]
    return (s["delivered"] == 2 and s["chargedEvents"] == 2 and s["rowsWritten"] == 2
            and s["counterMismatch"] is False and s["stopReason"] == ""
            and by_status(d) == {"OK": 2}
            and d["status"].startswith("2 paper(s) delivered, 2 charged, 0 free"))


def test_missing_paper_is_free():
    d = run(input={"papers": ["1706.03762", "9999.99999"]},
            rows_read={"1706.03762": row_for("1706.03762")})
    s = d["summary"]
    rows = d["dataset"].rows
    free = [r for r in rows if r.get("status") == "NOT_FOUND"]
    return (s["delivered"] == 1 and s["chargedEvents"] == 1 and s["papersNotFound"] == 1
            and len(free) == 1 and free[0]["billable"] is False)


def test_duplicate_is_free():
    d = run(input={"papers": ["1706.03762", "1706.03762v3"]},
            rows_read={"1706.03762": row_for("1706.03762")})
    s = d["summary"]
    return s["delivered"] == 1 and s["chargedEvents"] == 1 and s["papersDuplicate"] == 1


def test_default_max_papers_matches_the_schema():
    # The schema and README promise 100; a code default of 500 made a buyer who leaves the
    # field out read and pay for five times more than the card says.
    d = run(input={"papers": ["1706.03762"]},
            rows_read={"1706.03762": row_for("1706.03762")})
    return d["summary"]["maxPapers"] == 100


def test_nested_archive_is_sold_from_the_canonical_key():
    # arXiv answers a sub-archive id with an empty feed, so before the fix this line came
    # back as a free NOT_FOUND for a paper that exists. Both forms are one paper.
    d = run(input={"papers": ["cond-mat.mes-hall/0211022"]},
            rows_read={"cond-mat/0211022": row_for("cond-mat/0211022")})
    s = d["summary"]
    rows = d["dataset"].rows
    paid = [r for r in rows if r.get("billable") is True]
    return (s["delivered"] == 1 and s["chargedEvents"] == 1 and s["papersNotFound"] == 0
            and len(paid) == 1 and paid[0]["arxivId"] == "cond-mat/0211022"
            and paid[0]["actorInput"] == "cond-mat.mes-hall/0211022")


def test_two_forms_of_one_paper_are_sold_once():
    d = run(input={"papers": ["cond-mat.mes-hall/0211022", "cond-mat/0211022"]},
            rows_read={"cond-mat/0211022": row_for("cond-mat/0211022")})
    s = d["summary"]
    return (s["delivered"] == 1 and s["chargedEvents"] == 1 and s["papersDuplicate"] == 1
            and s["counterMismatch"] is False)


def test_junk_line_is_free_and_never_asked():
    d = run(input={"papers": ["hello", "1706.03762"]},
            rows_read={"1706.03762": row_for("1706.03762")})
    return (d["summary"]["papersInvalid"] == 1 and d["summary"]["delivered"] == 1
            and d["read_calls"] == 1)


def test_max_papers_cuts_the_tail():
    d = run(input={"papers": ["1706.03762", "2103.00020"], "maxPapers": 1},
            rows_read={"1706.03762": row_for("1706.03762"),
                       "2103.00020": row_for("2103.00020")})
    s = d["summary"]
    return (s["delivered"] == 1 and s["cutByLimit"] == 1 and s["maxPapers"] == 1
            and by_status(d).get("NOT_CHECKED_CAP") == 1)


def test_refused_write_is_not_delivery():
    d = run(input={"papers": ["1706.03762", "2103.00020"]}, fail_before_write=True,
            rows_read={"1706.03762": row_for("1706.03762"),
                       "2103.00020": row_for("2103.00020")})
    s = d["summary"]
    note = s["datasetNote"]
    return (s["delivered"] == 0 and s["rowsWritten"] == 0 and s["writeErrors"] == 1
            and s["deliveredUnverified"] == 2 and s["stopReason"] == "dataset-write-error"
            and s["counterMismatch"] is True
            and "nothing was delivered" not in note)


def test_charge_failing_after_the_write_keeps_the_rows():
    d = run(input={"papers": ["1706.03762", "2103.00020"]}, fail_charge=True,
            rows_read={"1706.03762": row_for("1706.03762"),
                       "2103.00020": row_for("2103.00020")})
    s = d["summary"]
    return (s["delivered"] == 2 and s["deliveredUncharged"] == 2
            and s["chargeUncertain"] == 2 and s["stopReason"] == "charge-error")


def test_unreadable_counter_says_unknown_not_nothing():
    d = run(input={"papers": ["1706.03762"]}, fail_charge=True, meta_raises=99,
            rows_read={"1706.03762": row_for("1706.03762")})
    s = d["summary"]
    note = s["datasetNote"]
    return (s["deliveredUnverified"] == 1 and s["chargeUncertain"] == 1
            and "both unknown" in note and "nothing was delivered" not in note
            and "nothing was charged" not in note)


def test_lagging_counter_is_not_read_as_a_refused_write():
    d = run(input={"papers": ["1706.03762"]}, fail_charge=True, lag_reads=5,
            rows_read={"1706.03762": row_for("1706.03762")})
    s = d["summary"]
    note = s["datasetNote"]
    return (s["chargeUncertain"] == 1 and "did not grow" in note
            and "nothing is claimed as sold" not in note)


def test_refused_second_batch_counts_against_written_rows():
    papers = [f"1706.{index:05d}" for index in range(12)]
    d = run(input={"papers": papers}, fail_before_write_at=2, lag_reads=1,
            rows_read={paper: row_for(paper) for paper in papers})
    s = d["summary"]
    note = s["datasetNote"]
    return (s["delivered"] == 10 and s["chargedEvents"] == 10 and s["writeErrors"] == 1
            and "did not grow" in note and "both unknown" not in note)


def test_actor_not_sold_per_event_delivers_free():
    d = run(input={"papers": ["1706.03762"]}, ppe=False,
            rows_read={"1706.03762": row_for("1706.03762")})
    s = d["summary"]
    return (s["delivered"] == 1 and s["chargedEvents"] == 0 and s["deliveredUncharged"] == 1
            and s["billingActive"] is False and all(r["billable"] is False
                                                    for r in d["dataset"].rows))


def test_unreadable_price_reads_nothing():
    d = run(input={"papers": ["1706.03762"]}, price_raises=True,
            rows_read={"1706.03762": row_for("1706.03762")})
    s = d["summary"]
    return (d["read_calls"] == 0 and s["delivered"] == 0
            and s["stopReason"] == "billing-price-unknown"
            and by_status(d).get("NOT_CHECKED_BILLING") == 1)


def test_dead_source_is_a_refusal_not_a_sale():
    d = run(input={"papers": ["1706.03762", "2103.00020"]},
            read_error="arXiv answered HTTP 503")
    s = d["summary"]
    return (s["delivered"] == 0 and s["papersNotFound"] == 0 and s["chargedEvents"] == 0
            and by_status(d).get("NOT_CHECKED_ERROR") == 2
            and "HTTP 503" in d["failed"] and "nothing was charged" in d["failed"])


def test_spending_cap_reached_stops_before_reading():
    d = run(input={"papers": ["1706.03762"]}, limit_reached=True,
            rows_read={"1706.03762": row_for("1706.03762")})
    s = d["summary"]
    return (s["delivered"] == 0 and s["stopReason"] == "spending-cap"
            and s["chargedEvents"] == 0)


def test_short_charge_is_reported():
    d = run(input={"papers": ["1706.03762", "2103.00020"]}, short_by=1,
            rows_read={"1706.03762": row_for("1706.03762"),
                       "2103.00020": row_for("2103.00020")})
    s = d["summary"]
    return (s["chargedEvents"] == 1 and s["delivered"] == 2
            and s["deliveredUncharged"] == 1 and s["stopReason"] == "spending-cap"
            and "charged 1 of 2" in s["datasetNote"])


def test_empty_input_is_refused():
    d = run(input={"papers": []})
    return "No papers were sent" in d["failed"] and d["read_calls"] == 0


def test_summary_names_the_source_and_the_event():
    d = run(input={"papers": ["1706.03762"]},
            rows_read={"1706.03762": row_for("1706.03762")})
    s = d["summary"]
    return (s["source"].startswith("arXiv") and s["eventName"] == "paper-delivered"
            and s["includeAbstract"] is True
            and s["requested"] == 1 and s["attempted"] == 1
            and any(name == "arxiv-paper-summary" for name, _ in d["stores"]))


def test_free_rows_are_written_in_one_batch():
    d = run(input={"papers": ["hello", "junk", "nope"]})
    return d["push_sizes"] == [3] and d["summary"]["papersInvalid"] == 3


def test_one_refused_id_does_not_cost_the_block():
    # Live case: [cond-mat.mes-hall/0211022v1, hep-th/9901001v1, 1706.03762v7] - arXiv answered
    # 500 for the whole id_list because of one id, and all three lines came back as errors.
    # The refused line is a VERSIONED id on purpose: its key (1706.03763) and the string sent
    # to the API (1706.03763v2) differ, and keying the map by the key would quietly turn the
    # refusal into "no such paper" - the trap the first version of this fix fell into.
    d = run(input={"papers": ["1706.03762", "1706.03763v2"]},
            rows_read={"1706.03762": row_for("1706.03762")},
            refused={"1706.03763v2": "arXiv answered HTTP 500"})
    s = d["summary"]
    rows = d["dataset"].rows
    failed = [r for r in rows if r.get("status") == "NOT_CHECKED_ERROR"]
    not_found = [r for r in rows if r.get("status") == "NOT_FOUND"]
    return (s["delivered"] == 1 and s["chargedEvents"] == 1 and s["papersRefused"] == 1
            and s["counterMismatch"] is False and len(failed) == 1 and not_found == []
            and failed[0]["billable"] is False
            and "send it again later" in failed[0]["reason"]
            and d["failed"] == "")


def test_every_refused_id_fails_the_run_without_blaming_the_network():
    d = run(input={"papers": ["1706.03762", "1706.03763v2"]},
            refused={"1706.03762": "arXiv answered HTTP 500",
                     "1706.03763v2": "arXiv answered HTTP 500"})
    return (d["summary"]["papersRefused"] == 2 and d["summary"]["delivered"] == 0
            and "refused all 2 paper(s)" in d["failed"]
            and "export.arxiv.org is reachable" not in d["failed"])


CHECKS = [
    ("a healthy run sells and writes", test_healthy_run_sells_and_writes),
    ("a missing paper is free", test_missing_paper_is_free),
    ("a duplicate is free", test_duplicate_is_free),
    ("the code default for maxPapers matches the schema", test_default_max_papers_matches_the_schema),
    ("a sub-archive id is sold from the canonical key", test_nested_archive_is_sold_from_the_canonical_key),
    ("two forms of one paper are sold once", test_two_forms_of_one_paper_are_sold_once),
    ("junk is free and never asked upstream", test_junk_line_is_free_and_never_asked),
    ("maxPapers cuts the tail with a reason", test_max_papers_cuts_the_tail),
    ("a refused write is not delivery", test_refused_write_is_not_delivery),
    ("a charge failing after the write keeps the rows", test_charge_failing_after_the_write_keeps_the_rows),
    ("an unreadable counter says unknown", test_unreadable_counter_says_unknown_not_nothing),
    ("a lagging counter is not a refused write", test_lagging_counter_is_not_read_as_a_refused_write),
    ("a refused second batch is counted against the rows already written",
     test_refused_second_batch_counts_against_written_rows),
    ("an actor not sold per event delivers free", test_actor_not_sold_per_event_delivers_free),
    ("an unreadable price reads nothing", test_unreadable_price_reads_nothing),
    ("a dead source is a refusal, not a sale", test_dead_source_is_a_refusal_not_a_sale),
    ("the spending cap stops before reading", test_spending_cap_reached_stops_before_reading),
    ("a short charge is reported", test_short_charge_is_reported),
    ("an empty input is refused", test_empty_input_is_refused),
    ("the summary names the source and the event", test_summary_names_the_source_and_the_event),
    ("free rows go out in one batch", test_free_rows_are_written_in_one_batch),
    ("one refused id does not cost the block", test_one_refused_id_does_not_cost_the_block),
    ("every id refused fails without blaming the network",
     test_every_refused_id_fails_the_run_without_blaming_the_network),
]


def run_all() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: raised {type(exc).__name__}: {exc}")
            failed += 1
            continue
        if not ok:
            print(f"FAIL {name}")
            failed += 1
    total = len(CHECKS)
    print(f"{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_all())
