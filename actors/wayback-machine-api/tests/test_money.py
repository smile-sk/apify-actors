"""The money path through the real main(), against a stand that repeats the SDK.

The stand writes the rows BEFORE it charges, get_metadata answers an object with item_count,
compute_push_data_limit is keyword only, and the counter can lag or fail. A stand that always
answers honestly hides the whole class of delivery-versus-charge confusion, so the failure
flags below are part of the proof, not convenience.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import main as m  # noqa: E402

PASSED = 0
FAILED: list = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


class PriceInfo:
    is_pay_per_event = True
    per_event_prices = {"url-checked": 0.0009}


class Charging:
    def __init__(self, budget: int = 1000, ppe: bool = True, capped: bool = False,
                 charged=None):
        self.budget = budget
        self.info = PriceInfo()
        self.info.is_pay_per_event = ppe
        self.info.per_event_prices = {"url-checked": 0.0009} if ppe else {}
        self.capped = capped
        self.charged = charged

    def get_pricing_info(self):
        return self.info

    def is_event_charge_limit_reached(self, _name):
        return self.capped

    def compute_push_data_limit(self, items_count, _event, *, is_default_dataset):
        assert is_default_dataset is True
        return min(items_count, self.budget)


class Result:
    def __init__(self, charged):
        self.charged_count = charged


class Meta:
    def __init__(self, count):
        self.item_count = count


class Dataset:
    def __init__(self, stand):
        self.stand = stand

    async def push_data(self, data):
        if isinstance(data, dict):
            data = [data]
        self.stand.rows.extend(data)

    async def get_metadata(self):
        if self.stand.meta_fail:
            raise RuntimeError("metadata is not readable")
        return Meta(max(0, len(self.stand.rows) - self.stand.meta_lag))


class Store:
    def __init__(self, stand=None):
        self.data = {}
        self.stand = stand

    async def set_value(self, key, value):
        self.data[key] = value
        if key == "RUN_SUMMARY" and self.stand is not None:
            self.stand.summary = value


class Log:
    def warning(self, *a, **k):
        return None


class Stand:
    def __init__(self, payload, charging=None, fail_push=False, fail_before_write=False,
                 meta_fail=False, meta_lag=0, fail_push_on=None, fail_write_on=None,
                 no_count=False):
        self.payload = payload
        self.fail_push_on = fail_push_on
        self.fail_write_on = fail_write_on
        self.push_calls = 0
        self.charging = charging or Charging()
        self.fail_push = fail_push
        self.fail_before_write = fail_before_write
        self.meta_fail = meta_fail
        self.meta_lag = meta_lag
        self.rows = []
        self.summary = None
        self.status = None
        self.failed = None
        self.store = Store(self)
        self.log = Log()
        self.named = {}
        self.no_count = no_count
        self.crashed = ""

    async def init(self):
        return None

    async def get_input(self):
        return self.payload

    async def open_dataset(self):
        return Dataset(self)

    def get_charging_manager(self):
        return self.charging

    async def push_data(self, data, charged_event_name=None):
        if isinstance(data, dict):
            data = [data]
        if self.fail_before_write:
            raise RuntimeError("the dataset refused the rows")
        if self.fail_write_on and self.push_calls == self.fail_write_on - 1:
            self.push_calls += 1
            raise RuntimeError("the dataset refused the rows")
        self.push_calls += 1
        self.rows.extend(data)
        if self.fail_push or (self.fail_push_on and self.push_calls == self.fail_push_on):
            raise RuntimeError("the platform refused the charge")
        if self.no_count:
            return Result(None)
        charged = getattr(self.charging, "charged", None)
        return Result(len(data) if charged is None else charged)

    async def set_value(self, key, value):
        self.store.data[key] = value
        if key == "RUN_SUMMARY":
            self.summary = value

    async def open_key_value_store(self, name=None):
        if name:
            self.named.setdefault(name, Store(self))
            return self.named[name]
        return self.store

    async def set_status_message(self, text):
        self.status = text

    async def fail(self, *, status_message="", **k):
        self.failed = status_message

    async def exit(self):
        return None

    def get_env(self):
        return {"run_id": "TESTWB48"}


READ_CALLS: list = []


def fake_read(client, target, *, max_snapshots=20, from_="", to="", timeout=None):
    """A known page answers a full row, a never archived one answers a verdict, a dead source
    raises. The signature repeats the real reader, timeout included: a stand that cannot take
    the argument it is given hides every change of how the reader is called.
    """
    READ_CALLS.append(target.url)
    if target.host == "dead.com":
        return None, "error", "the archive could not be read (ConnectError)"
    if target.host == "not-found.com":
        return None, "not_found", "the Internet Archive holds no snapshot of this URL"
    row = {
        "status": "OK", "billable": True, "url": target.url, "host": target.host,
        "firstSnapshotAt": "2004-03-01T10:00:00Z", "firstSnapshotUrl": target.url,
        "firstSnapshotStatus": "200", "lastSnapshotAt": "2026-11-13T19:59:09Z",
        "versionsReturned": min(2, max_snapshots), "windowFrom": from_, "windowTo": to,
        "latestVersions": [{"at": "2026-11-13T19:59:09Z", "timestamp": "20261113195909",
                            "url": target.url, "statusCode": "200", "mimeType": "text/html",
                            "digest": "AAA", "lengthBytes": 100}],
        "partial": False, "versionsNote": "", "source": m.SOURCE,
    }
    return row, "", ""


def run(payload, charging=None, fail_push=False, fail_before_write=False,
        meta_fail=False, meta_lag=0, fail_push_on=None, fail_write_on=None, no_count=False):
    READ_CALLS.clear()
    stand = Stand(payload, charging, fail_push, fail_before_write, meta_fail, meta_lag,
                  fail_push_on, fail_write_on, no_count)
    m.Actor = stand
    m.archive.read_history = fake_read
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    return stand


def paid(stand):
    return [r for r in stand.rows if r.get("status") == "OK"]


def free(stand, status):
    return [r for r in stand.rows if r.get("status") == status]


def run_all() -> int:
    # 1. two normal pages
    s = run({"urls": ["example.com", "apify.com/pricing"]})
    check("normal: no crash", s.crashed == "")
    check("normal: two sold rows", len(paid(s)) == 2)
    check("normal: two charged", (s.summary or {}).get("chargedEvents") == 2)
    check("normal: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("normal: billable claimed", all(r.get("billable") is True for r in paid(s)))
    check("normal: the header counts delivered",
          str(s.status or "").startswith("2 URL(s) delivered"))
    named = s.named.get("wayback-machine-summary")
    check("normal: the summary lands bare and run scoped",
          "RUN_SUMMARY" in s.store.data
          and named is not None and "RUN_SUMMARY-TESTWB48" in named.data)
    check("normal: the source is named in the summary",
          "Internet Archive" in str((s.summary or {}).get("source")))
    check("normal: the history fields are in the row",
          paid(s) and paid(s)[0]["firstSnapshotAt"] == "2004-03-01T10:00:00Z")

    # 2. a line that is not a page is free next to a real one
    s = run({"urls": ["127.0.0.1/x", "example.com"]})
    check("invalid: a free row with a reason", len(free(s, "INVALID_INPUT")) == 1)
    check("invalid: the reason is plain", "private address" in free(s, "INVALID_INPUT")[0]["reason"])
    check("invalid: not billable", free(s, "INVALID_INPUT")[0]["billable"] is False)
    check("invalid: the real one is still sold", len(paid(s)) == 1)
    check("invalid: the counters add up", (s.summary or {}).get("counterMismatch") is False)

    # 3. a page the archive never saw is free and the run stays green
    s = run({"urls": ["not-found.com"]})
    check("not found: a free row", len(free(s, "NOT_FOUND")) == 1)
    check("not found: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not found: the run is not failed", s.failed is None)
    check("not found: the row carries no history", "firstSnapshotAt" not in (free(s, "NOT_FOUND")[0] or {}))
    check("not found: the reason is the archive's",
          "no snapshot" in free(s, "NOT_FOUND")[0]["reason"])

    # 4. the same URL twice is charged once
    s = run({"urls": ["example.com", "example.com"]})
    check("duplicate: a free row", len(free(s, "DUPLICATE")) == 1)
    check("duplicate: charged once", (s.summary or {}).get("chargedEvents") == 1)

    # 5. the same page in two spellings is one page
    s = run({"urls": ["example.com", "https://example.com/"]})
    check("duplicate: a spelling does not buy a second row", len(paid(s)) == 1)
    check("duplicate: the second line is free", len(free(s, "DUPLICATE")) == 1)

    # 6. a refused write is not a delivery
    s = run({"urls": ["example.com", "apify.com"]}, fail_before_write=True)
    check("refused write: nothing delivered", (s.summary or {}).get("delivered") == 0)
    check("refused write: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("refused write: counted as unverified", (s.summary or {}).get("deliveredUnverified") == 2)
    check("refused write: the stop names the dataset",
          (s.summary or {}).get("stopReason") == "dataset-write-error")

    # 7. a charge that failed after the rows landed is called unknown, never "nothing"
    s = run({"urls": ["example.com", "apify.com"]}, fail_push=True)
    note = str((s.summary or {}).get("datasetNote"))
    check("charge failed: the rows stay", "stay in the dataset" in note)
    check("charge failed: the charge is uncertain", (s.summary or {}).get("chargeUncertain") == 2)
    check("charge failed: it never says nothing was delivered",
          "nothing was delivered" not in note and "nothing was charged" not in note)

    # 8. a lagging counter after a failed charge is still unknown, not a refusal
    s = run({"urls": ["example.com"]}, fail_push=True, meta_lag=1)
    note = str((s.summary or {}).get("datasetNote"))
    check("lagging counter: it does not claim a refusal", "did not grow" in note)
    check("lagging counter: the charge is not confirmed", "not confirmed" in note)
    check("lagging counter: counted as uncertain", (s.summary or {}).get("chargeUncertain") == 1)
    check("lagging counter: it never says nothing is claimed as sold",
          "nothing is claimed as sold" not in note)

    # 9. an unreadable counter says unknown, not zero
    s = run({"urls": ["example.com"]}, fail_push=True, meta_fail=True)
    note = str((s.summary or {}).get("datasetNote"))
    check("unreadable counter: both unknown", "both unknown" in note)
    check("unreadable counter: nothing was delivered is not claimed",
          "nothing was delivered" not in note)

    # 10. the spending cap cuts the batch and the tail is free with a reason
    s = run({"urls": ["example.com", "apify.com", "nytimes.com"]}, charging=Charging(budget=2))
    check("budget: two sold", len(paid(s)) == 2)
    check("budget: the tail is capped", len(free(s, "NOT_CHECKED_CAP")) == 1)
    check("budget: the tail says why", "budget" in free(s, "NOT_CHECKED_CAP")[0]["reason"])
    check("budget: the counters add up", (s.summary or {}).get("counterMismatch") is False)

    # 11. the platform says the actor is not sold per event: deliver free and say so
    s = run({"urls": ["example.com"]}, charging=Charging(ppe=False))
    check("not per event: the row is delivered", (s.summary or {}).get("delivered") == 1)
    check("not per event: nothing is charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not per event: the summary says it out loud",
          "not sold per event" in str((s.summary or {}).get("datasetNote")))
    check("not per event: the row is not marked billable",
          paid(s) and paid(s)[0].get("billable") is False)

    # 12. the price list does not carry our event: never sell what cannot be charged
    class WrongEvent(Charging):
        def __init__(self):
            super().__init__()
            self.info.per_event_prices = {"something-else": 0.001}

    s = run({"urls": ["example.com"]}, charging=WrongEvent())
    check("wrong price: nothing is sold", (s.summary or {}).get("delivered") == 0)
    check("wrong price: the rows are free with a reason",
          len(free(s, "NOT_CHECKED_BILLING")) == 1)
    check("wrong price: the reason names the event",
          "url-checked" in free(s, "NOT_CHECKED_BILLING")[0]["reason"])

    # 13. a dead source is a failed run that names the reason
    s = run({"urls": ["dead.com"]})
    check("dead source: the run fails", s.failed is not None)
    check("dead source: the reason is in the message", "ConnectError" in str(s.failed))
    check("dead source: the free row is there", len(free(s, "NOT_CHECKED_ERROR")) == 1)

    # 14. an empty input fails with an example instead of a green zero
    s = run({"urls": []})
    check("empty input: refused", s.failed is not None)
    check("empty input: the example is in the message", "urls" in str(s.failed))

    # 15. no charge count in the result is not a sale
    s = run({"urls": ["example.com"]}, no_count=True)
    check("no count: the row is delivered", (s.summary or {}).get("delivered") == 1)
    check("no count: not claimed as sold", (s.summary or {}).get("chargedEvents") == 0)
    check("no count: the status stops on the charge",
          (s.summary or {}).get("stopReason") == "charge-error")

    # 16. a status row that cannot be written is counted, not lost silently
    s = run({"urls": ["127.0.0.1/x"]}, fail_write_on=1)
    check("status write refused once: retried and landed", (s.summary or {}).get("writeErrors") == 0)
    check("status write refused once: the row is in the dataset", len(s.rows) == 1)
    check("status write refused once: no crash", s.crashed == "")

    # 17. an unreadable price costs no requests at all
    class NoPrice(Charging):
        def get_pricing_info(self):
            raise RuntimeError("pricing is not readable")

    s = run({"urls": ["example.com", "apify.com"]}, charging=NoPrice())
    check("no price: the archive is never asked", READ_CALLS == [])
    check("no price: both lines are free billing rows",
          len(free(s, "NOT_CHECKED_BILLING")) == 2)
    check("no price: it is not a source failure", s.failed is None)
    check("no price: the stop names the price",
          (s.summary or {}).get("stopReason") == "billing-price-unknown")
    check("no price: the counters add up", (s.summary or {}).get("counterMismatch") is False)

    # 18. a dead source next to a page the archive never saw is still a failed run
    s = run({"urls": ["dead.com", "not-found.com"]})
    check("dead and missing: the run fails", s.failed is not None)
    check("dead and missing: the message names the source, not the URLs",
          "reachable" in str(s.failed))

    # 19. a spent budget is not a source failure
    s = run({"urls": ["example.com"]}, charging=Charging(capped=True))
    check("cap reached: nothing is sold", (s.summary or {}).get("delivered") == 0)
    check("cap reached: the free row says so", len(free(s, "NOT_CHECKED_CAP")) == 1)
    check("cap reached: the run is not failed", s.failed is None)

    # 20. a charge for fewer rows than were written stops the run
    s = run({"urls": ["example.com", "apify.com", "nytimes.com"]}, charging=Charging(charged=1))
    check("short charge: it stops", (s.summary or {}).get("stopReason") == "spending-cap")
    check("short charge: the note says so", "stops here" in str((s.summary or {}).get("datasetNote")))
    check("short charge: the uncharged rows are counted",
          (s.summary or {}).get("deliveredUncharged") == 2)

    # 21. free status rows travel in one batch, not one call per row
    s = run({"urls": ["127.0.0.1/x", "10.0.0.5", "notaurl"]})
    check("free rows: one batch", s.push_calls == 1)
    check("free rows: all three are written", len(s.rows) == 3)
    check("free rows: the counters add up", (s.summary or {}).get("counterMismatch") is False)

    # 22. the request budget is shared by the two requests of one URL
    check("budget: no deadline still has a timeout, never None",
          m.request_budget(None) == m.archive.TIMEOUT)
    check("budget: a generous remainder is capped by the reader timeout",
          m.request_budget(120.0) == 25.0)
    check("budget: a short remainder is split, not given twice",
          abs(m.request_budget(30.0) - 13.5) < 0.001)
    check("budget: it never goes below a floor", m.request_budget(5.0) == 2.0)

    # 23. a malformed date filter stops the run before any request
    s = run({"urls": ["example.com"], "from": "2025-01-01"})
    check("bad filter: the run is refused", s.failed is not None)
    check("bad filter: the format is in the message", "YYYY" in str(s.failed))
    check("bad filter: nothing was asked", READ_CALLS == [])
    check("bad filter: nothing was written", s.rows == [])

    # 24. the cap counts pages that were read, not lines that were junk
    s = run({"urls": ["127.0.0.1/x", "notaurl", "example.com", "apify.com"], "maxUrls": 1})
    check("cap: junk does not spend the limit", len(paid(s)) == 1)
    check("cap: the real tail is cut with a reason", len(free(s, "NOT_CHECKED_CAP")) == 1)
    check("cap: the reason names maxUrls", "maxUrls" in free(s, "NOT_CHECKED_CAP")[0]["reason"])

    # 25. a port the URL parser refuses is a verdict about the line, not the end of the run
    s = run({"urls": ["https://example.com:99999/x", "example.com", "example.com:abc/y"]})
    check("bad port: the live URL is still delivered", len(paid(s)) == 1)
    check("bad port: both typos come back free", len(free(s, "INVALID_INPUT")) == 2)
    check("bad port: the reason names the port",
          all("port" in row["reason"] for row in free(s, "INVALID_INPUT")))
    check("bad port: the run is not failed", s.failed is None)
    check("bad port: the counters add up", (s.summary or {}).get("counterMismatch") is False)
    check("bad port: nothing is thrown away", len(s.rows) == 3)

    # 26. a live URL first in the list survives a bad port later in the same batch
    s = run({"urls": ["example.com", "example.com:99999"]})
    check("bad port after a live URL: the paid row is in the dataset", len(paid(s)) == 1)

    # 27. a free batch the dataset refuses twice stops the run and is not promised in the header
    class RefuseFree(Stand):
        async def push_data(self, data, charged_event_name=None):
            if isinstance(data, dict):
                data = [data]
            if all(row.get("billable") is False for row in data):
                self.push_calls += 1
                raise RuntimeError("the dataset refused the rows")
            return await super().push_data(data, charged_event_name=charged_event_name)

    stand = RefuseFree({"urls": ["127.0.0.1/x", "notaurl"]})
    m.Actor = stand
    m.archive.read_history = fake_read
    asyncio.run(m.main())
    check("refused free batch: the run stops",
          (stand.summary or {}).get("stopReason") == "dataset-write-error")
    check("refused free batch: the missing rows are counted",
          (stand.summary or {}).get("writeErrors") == 2)
    check("refused free batch: the header does not promise them",
          "0 free status row(s)" in str(stand.status))
    check("refused free batch: the summary admits the loss",
          (stand.summary or {}).get("counterMismatch") is True)

    # 28. an unexpected error does not throw away rows that were already read
    class Exploding(Stand):
        pass

    def half_dead(client, target, *, max_snapshots=20, from_="", to="", timeout=None):
        READ_CALLS.append(target.url)
        if target.host == "boom.com":
            raise RuntimeError("the reader exploded")
        return fake_read(client, target, max_snapshots=max_snapshots, from_=from_, to=to,
                         timeout=timeout)

    stand = Exploding({"urls": ["example.com", "boom.com"]})
    m.Actor = stand
    m.archive.read_history = half_dead
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    check("unexpected error: the read row is not lost", len(paid(stand)) == 1)
    check("unexpected error: the run stops with the reason",
          (stand.summary or {}).get("stopReason") == "unexpected-error")
    check("unexpected error: the dataset carries the row that was read",
          (stand.summary or {}).get("rowsWritten") == 1)
    m.archive.read_history = fake_read

    stand = Exploding({"urls": ["boom.com"]})
    m.Actor = stand
    m.archive.read_history = half_dead
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    check("unexpected error with nothing delivered: the run is refused", stand.failed is not None)
    m.archive.read_history = fake_read

    print(f"money: {PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print("  FAIL:", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
