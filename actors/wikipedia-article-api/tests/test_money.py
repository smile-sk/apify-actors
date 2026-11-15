"""The money path through the real main(), against a stand that repeats the SDK.

The stand writes the rows BEFORE it charges, get_metadata answers an object with
item_count, compute_push_data_limit is keyword only, and the counter can lag or fail.
A stand that always answers honestly hides the whole class of delivery-versus-charge
confusion, so the failure flags below are part of the proof, not convenience.
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
    per_event_prices = {"article-delivered": 0.0008}


class Charging:
    def __init__(self, budget: int = 1000, ppe: bool = True, capped: bool = False,
                 charged=None):
        self.budget = budget
        self.info = PriceInfo()
        self.info.is_pay_per_event = ppe
        self.info.per_event_prices = {"article-delivered": 0.0008} if ppe else {}
        self.capped = capped
        self.charged = charged

    def get_pricing_info(self):
        return self.info

    def is_event_charge_limit_reached(self, _name):
        return self.capped

    def compute_push_data_limit(self, items_count, _event, *, is_default_dataset):
        assert is_default_dataset is True
        return min(items_count, self.budget)

    # The SDK answers a pydantic model here; every attribute the actor reads is present.
    class _Meta:
        def __init__(self, count):
            self.item_count = count


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
    """The key-value store of the stand: it mirrors the run summary onto the stand."""

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
        return {"run_id": "TESTRUN47"}


READ_CALLS: list = []


def fake_read(client, article, *, include_text=False, include_pageviews=True,
              include_categories=True, pageviews_days=30, timeout=None):
    """A known article answers a full row, a missing one answers a verdict, a dead one raises.

    The signature repeats the real reader's, timeout included: a stand that cannot take the
    argument it is given hides every change of how the reader is called.
    """
    READ_CALLS.append(article.title)
    title = article.title
    if title == "Missing Article":
        return None, "not_found", "Wikipedia has no article at this title"
    if title == "Dead Source":
        return None, "error", "the request to Wikipedia failed (ConnectError)"
    row = {
        "actorInput": title, "actorIndex": 0, "status": "OK", "billable": True,
        "lang": article.lang, "title": title, "pageId": abs(hash(title)) % 100000,
        "url": article.url, "wikidataId": "Q1", "description": "a description",
        "wordCount": 12, "text": "some text", "categories": ["Category:Test"],
        "categoryCount": 1, "pageviews": 100, "pageviewsPerDay": 3.33,
        "pageviewsDaily": [], "pageviewsNote": "", "sitelinkCount": 5, "claimCount": 7,
        "wikidataNote": "", "source": m.SOURCE,
    }
    return row, "", ""


def run(payload, charging=None, fail_push=False, fail_before_write=False,
        meta_fail=False, meta_lag=0, fail_push_on=None, fail_write_on=None, no_count=False):
    READ_CALLS.clear()
    stand = Stand(payload, charging, fail_push, fail_before_write, meta_fail, meta_lag,
                  fail_push_on, fail_write_on, no_count)
    m.Actor = stand
    m.wiki.read_article = fake_read
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
    # 1. two normal articles
    s = run({"articles": ["Berlin", "Paris"]})
    check("normal: no crash", s.crashed == "")
    check("normal: two sold rows", len(paid(s)) == 2)
    check("normal: two charged", (s.summary or {}).get("chargedEvents") == 2)
    check("normal: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("normal: billable claimed", all(r.get("billable") is True for r in paid(s)))
    check("normal: the header counts delivered",
          str(s.status or "").startswith("2 article(s) delivered"))
    named = s.named.get("wikipedia-article-summary")
    check("normal: the summary lands bare and run scoped",
          "RUN_SUMMARY" in s.store.data
          and named is not None and "RUN_SUMMARY-TESTRUN47" in named.data)
    check("normal: the source is named in the summary",
          "Wikimedia" in str((s.summary or {}).get("source")))
    check("normal: the summary does not lie about delivery",
          "nothing was delivered" not in str((s.summary or {}).get("datasetNote")))

    # 2. junk next to a real article
    s = run({"articles": ["https://example.com/wiki/Berlin", "Berlin"]})
    check("invalid: a free row with a reason", len(free(s, "INVALID_INPUT")) == 1)
    check("invalid: the reason is plain",
          "not a Wikipedia project" in free(s, "INVALID_INPUT")[0]["reason"])
    check("invalid: not billable", free(s, "INVALID_INPUT")[0]["billable"] is False)
    check("invalid: the real one is still sold", len(paid(s)) == 1)
    check("invalid: the counters add up", (s.summary or {}).get("counterMismatch") is False)

    # 3. a title Wikipedia does not have is free and is green
    s = run({"articles": ["Missing Article"]})
    check("not found: a free row", len(free(s, "NOT_FOUND")) == 1)
    check("not found: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not found: the run is not failed", s.failed is None)
    check("not found: the reason is the source's", "no article" in free(s, "NOT_FOUND")[0]["reason"])

    # 4. the same article twice is charged once
    s = run({"articles": ["Berlin", "Berlin"]})
    check("duplicate: a free row", len(free(s, "DUPLICATE")) == 1)
    check("duplicate: charged once", (s.summary or {}).get("chargedEvents") == 1)

    # 5. the same article in two spellings is one article
    s = run({"articles": ["Berlin", "berlin"]})
    check("duplicate: case does not buy a second row", len(paid(s)) == 1)

    # 6. a refused write is not a delivery
    s = run({"articles": ["Berlin", "Paris"]}, fail_before_write=True)
    check("refused write: nothing delivered", (s.summary or {}).get("delivered") == 0)
    check("refused write: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("refused write: counted as unverified", (s.summary or {}).get("deliveredUnverified") == 2)
    check("refused write: the stop names the dataset",
          (s.summary or {}).get("stopReason") == "dataset-write-error")

    # 7. a charge that failed after the rows landed is called unknown, never "nothing"
    s = run({"articles": ["Berlin", "Paris"]}, fail_push=True)
    note = str((s.summary or {}).get("datasetNote"))
    check("charge failed: the rows stay", "stay in the dataset" in note)
    check("charge failed: charge is uncertain", (s.summary or {}).get("chargeUncertain") == 2)
    check("charge failed: it never says nothing was delivered",
          "nothing was delivered" not in note and "nothing was charged" not in note)

    # 8. a lagging counter after a failed charge is still "unknown", not "nothing"
    s = run({"articles": ["Berlin"]}, fail_push=True, meta_lag=1)
    note = str((s.summary or {}).get("datasetNote"))
    check("lagging counter: it does not claim a refusal", "did not grow" in note)
    check("lagging counter: the charge is not confirmed", "not confirmed" in note)
    check("lagging counter: counted as uncertain", (s.summary or {}).get("chargeUncertain") == 1)
    check("lagging counter: it never says nothing is claimed as sold",
          "nothing is claimed as sold" not in note)

    # 9. an unreadable counter says unknown, not zero
    s = run({"articles": ["Berlin"]}, fail_push=True, meta_fail=True)
    note = str((s.summary or {}).get("datasetNote"))
    check("unreadable counter: both unknown", "both unknown" in note)
    check("unreadable counter: nothing was delivered is not claimed",
          "nothing was delivered" not in note)

    # 10. a spending cap cuts the batch and the tail is free with a reason
    s = run({"articles": ["Berlin", "Paris", "Rome"]}, charging=Charging(budget=2))
    check("budget: two sold", len(paid(s)) == 2)
    check("budget: the tail is capped", len(free(s, "NOT_CHECKED_CAP")) == 1)
    check("budget: the tail says why", "budget" in free(s, "NOT_CHECKED_CAP")[0]["reason"])
    check("budget: the counters add up", (s.summary or {}).get("counterMismatch") is False)

    # 11. the platform says the actor is not sold per event: deliver free and say so
    s = run({"articles": ["Berlin"]}, charging=Charging(ppe=False))
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

    s = run({"articles": ["Berlin"]}, charging=WrongEvent())
    check("wrong price: nothing is sold", (s.summary or {}).get("delivered") == 0)
    check("wrong price: the rows are free with a reason",
          len(free(s, "NOT_CHECKED_BILLING")) == 1)
    check("wrong price: the reason names the event",
          "article-delivered" in free(s, "NOT_CHECKED_BILLING")[0]["reason"])

    # 13. a dead source is a failed run that names the reason
    s = run({"articles": ["Dead Source"]})
    check("dead source: the run fails", s.failed is not None)
    check("dead source: the reason is in the message", "ConnectError" in str(s.failed))
    check("dead source: the free row is there", len(free(s, "NOT_CHECKED_ERROR")) == 1)

    # 14. an empty input fails with an example instead of a green zero
    s = run({"articles": []})
    check("empty input: refused", s.failed is not None)
    check("empty input: the example is in the message", "articles" in str(s.failed))

    # 15. no charge count in the result is not a sale
    s = run({"articles": ["Berlin"]}, no_count=True)
    check("no count: the row is delivered", (s.summary or {}).get("delivered") == 1)
    check("no count: not claimed as sold", (s.summary or {}).get("chargedEvents") == 0)
    check("no count: the status stops on the charge",
          (s.summary or {}).get("stopReason") == "charge-error")

    # 16. a status row that cannot be written is counted, not lost silently
    s = run({"articles": ["https://example.com/wiki/Berlin"]}, fail_write_on=1)
    check("status write refused: counted", (s.summary or {}).get("writeErrors") == 1)
    check("status write refused: no crash", s.crashed == "")

    # 17. an unreadable price costs no requests at all: nothing can be sold, so nothing is read
    class NoPrice(Charging):
        def get_pricing_info(self):
            raise RuntimeError("pricing is not readable")

    s = run({"articles": ["Berlin", "Paris"]}, charging=NoPrice())
    check("no price: the source is never asked", READ_CALLS == [])
    check("no price: both lines are free billing rows",
          len(free(s, "NOT_CHECKED_BILLING")) == 2)
    check("no price: it is not a source failure", s.failed is None)
    check("no price: the stop names the price",
          (s.summary or {}).get("stopReason") == "billing-price-unknown")
    check("no price: the counters add up", (s.summary or {}).get("counterMismatch") is False)

    # 18. a dead source next to a missing title is still a failed run (the reverse side of B1)
    s = run({"articles": ["Dead Source", "Missing Article"]})
    check("dead and missing: the run fails", s.failed is not None)
    check("dead and missing: the message names the source, not the titles",
          "reachable" in str(s.failed) and "not about the titles" in str(s.failed))

    # 19. a spent budget is not a source failure
    s = run({"articles": ["Berlin"]}, charging=Charging(capped=True))
    check("cap reached: nothing is sold", (s.summary or {}).get("delivered") == 0)
    check("cap reached: the free row says so", len(free(s, "NOT_CHECKED_CAP")) == 1)
    check("cap reached: the run is not failed", s.failed is None)

    # 20. a charge for fewer rows than were written stops the run
    s = run({"articles": ["Berlin", "Paris", "Rome"]}, charging=Charging(charged=1))
    check("short charge: it stops", (s.summary or {}).get("stopReason") == "spending-cap")
    check("short charge: the note says so", "stops here" in str((s.summary or {}).get("datasetNote")))
    check("short charge: the uncharged rows are counted",
          (s.summary or {}).get("deliveredUncharged") == 2)

    # 21. free status rows travel in one batch, not one call per row
    s = run({"articles": ["https://a.com/wiki/X", "https://b.com/wiki/X",
                          "https://c.com/wiki/X"]})
    check("free rows: one batch", s.push_calls == 1)
    check("free rows: all three are written", len(s.rows) == 3)
    check("free rows: the counters add up", (s.summary or {}).get("counterMismatch") is False)

    # 22. the request budget is shared by the three requests of one article
    check("budget: no deadline means the default", m.request_budget(None) is None)
    check("budget: a generous remainder is capped by the reader timeout",
          m.request_budget(120.0) == 25.0)
    check("budget: a short remainder is split, not given three times",
          abs(m.request_budget(30.0) - 9.0) < 0.001)
    check("budget: it never goes below a floor", m.request_budget(5.0) == 1.5)

    print(f"money: {PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print("  FAIL:", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
