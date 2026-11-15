"""The money path through the real main(), against a stand that repeats the SDK.

The stand writes the rows BEFORE it charges, get_metadata answers an object with
item_count, compute_push_data_limit is keyword only, and the counter can lag or fail.
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
    per_event_prices = {"post-delivered": 0.0003}


class Charging:
    def __init__(self, budget: int = 1000, ppe: bool = True, capped: bool = False,
                 charged=None):
        self.budget = budget
        self.info = PriceInfo()
        self.info.is_pay_per_event = ppe
        self.info.per_event_prices = {"post-delivered": 0.0003} if ppe else {}
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
    def __init__(self):
        self.data = {}

    async def set_value(self, key, value):
        self.data[key] = value


class Log:
    def warning(self, *a, **k):
        return None


class Stand:
    def __init__(self, payload, charging=None, fail_push=False, fail_before_write=False,
                 meta_fail=False, meta_lag=0, fail_push_on=None, fail_write_on=None):
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
        self.store = Store()
        self.log = Log()
        self.crashed = ""
        self.timeouts = []

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
        charged = getattr(self.charging, "charged", None)
        return Result(len(data) if charged is None else charged)

    async def set_value(self, key, value):
        self.store.data[key] = value
        if key == "RUN_SUMMARY":
            self.summary = value

    async def open_key_value_store(self, *a, **k):
        return self.store

    async def set_status_message(self, text):
        self.status = text

    async def fail(self, *, status_message="", **k):
        self.failed = status_message

    async def exit(self):
        return None

    def get_env(self):
        return {"run_id": "TESTRUN46"}


def fake_page(client, host, limit, cursor, timeout=None):
    """Two pages of posts for a known host, a 404 for anyone else."""
    TIMEOUTS.append(timeout)
    if host in SLEEPY:
        import time as _t
        _t.sleep(SLEEPY[host])
    PAGES_SEEN[host] = PAGES_SEEN.get(host, 0) + 1
    if FAIL_PAGE.get(host) == PAGES_SEEN[host]:
        return [], None, "error", "the publication did not answer in time"
    if host == "missing.example":
        return [], None, "not_found", "no publication answers on this host"
    if host.startswith("slow"):
        return [], None, "error", "the publication did not answer in time"
    total = TOTAL_POSTS.get(host, 2)
    start = int(cursor or 0)
    left = max(0, total - start)
    take = min(limit, left)
    bylines = 5 if host in WEIRD_BYLINES else [{"name": "Ann"}]
    posts = [{"id": start + i + 1, "title": f" post {start + i + 1}",
              "canonical_url": f"https://{host}/p/{start + i + 1}",
              "post_date": "2026-11-14T11:01:15.817Z",
              "publishedBylines": bylines, "reaction_count": i,
              "comment_count": 0, "wordcount": 100} for i in range(take)]
    nxt = str(start + take) if start + take < total else None
    return posts, nxt, "", ""


def fake_open(client, host, limit, cursor=None, timeout=None):
    """The first page of the stand: no redirects and no second road of its own."""
    TIMEOUTS.append(timeout)
    current = ALIASES.get(host, host)
    posts, nxt, kind, reason = fake_page(client, current, limit, cursor, timeout)
    return current, posts, nxt, kind, reason


TOTAL_POSTS: dict = {}
TIMEOUTS: list = []
ALIASES: dict = {}
WEIRD_BYLINES: set = set()
SLEEPY: dict = {}
FAIL_PAGE: dict = {}
PAGES_SEEN: dict = {}


def run(payload, charging=None, fail_push=False, fail_before_write=False,
        meta_fail=False, meta_lag=0, fail_push_on=None, totals=None, fail_write_on=None,
        aliases=None, weird=None, sleepy=None, fail_page=None):
    TOTAL_POSTS.clear()
    TOTAL_POSTS.update(totals or {})
    TIMEOUTS.clear()
    ALIASES.clear()
    ALIASES.update(aliases or {})
    WEIRD_BYLINES.clear()
    WEIRD_BYLINES.update(weird or set())
    SLEEPY.clear()
    SLEEPY.update(sleepy or {})
    FAIL_PAGE.clear()
    FAIL_PAGE.update(fail_page or {})
    PAGES_SEEN.clear()
    stand = Stand(payload, charging, fail_push, fail_before_write, meta_fail, meta_lag,
                  fail_push_on, fail_write_on)
    m.Actor = stand
    m.substack.fetch_page = fake_page
    m.substack.open_publication = fake_open
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    return stand


def paid(stand):
    return [r for r in stand.rows if r.get("status") == "OK"]


def run_all() -> int:
    # 1. one normal publication
    s = run({"publications": ["www.thefp.com"]})
    check("normal: no crash", s.crashed == "")
    check("normal: two sold rows", len(paid(s)) == 2)
    check("normal: two charged", (s.summary or {}).get("chargedEvents") == 2)
    check("normal: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("normal: billable claimed", all(r.get("billable") is True for r in paid(s)))
    check("normal: the header counts delivered",
          str(s.status or "").startswith("2 post(s) delivered"))
    check("normal: the summary lands bare and run scoped",
          "RUN_SUMMARY" in s.store.data and "RUN_SUMMARY-TESTRUN46" in s.store.data)
    check("normal: the source is named in the summary",
          "Substack" in str((s.summary or {}).get("source")))

    # 2. junk and a real publication
    s = run({"publications": ["not a host", "www.thefp.com"]})
    check("invalid: a free row with a reason",
          any(r.get("status") == "INVALID_INPUT" and "publication" in str(r.get("reason"))
              for r in s.rows))
    check("invalid: only the real publication is charged",
          (s.summary or {}).get("chargedEvents") == 2)
    check("invalid: the free row carries no post fields",
          all("postId" not in r or r.get("postId") is None for r in s.rows
              if r.get("status") != "OK"))

    # 3. the same publication twice
    s = run({"publications": ["www.thefp.com", "https://www.thefp.com/"]})
    check("duplicate: one sold publication",
          len(paid(s)) == 2 and (s.summary or {}).get("byStatus", {}).get("DUPLICATE") == 1)
    check("duplicate: charged once", (s.summary or {}).get("chargedEvents") == 2)

    # 4. the limit cuts a publication
    s = run({"publications": ["www.thefp.com"], "maxPostsPerPublication": 1}, totals={"www.thefp.com": 5})
    check("limit: one sold", len(paid(s)) == 1)
    check("limit: charged once", (s.summary or {}).get("chargedEvents") == 1)

    # 5. the budget ends inside the batch
    s = run({"publications": ["www.thefp.com"]}, Charging(budget=1), totals={"www.thefp.com": 5})
    check("budget: one sold", len(paid(s)) == 1)
    check("budget: the rest is capped",
          (s.summary or {}).get("byStatus", {}).get("NOT_CHECKED_CAP", 0) >= 1)
    check("budget: the stop reason is money", (s.summary or {}).get("stopReason") == "spending-cap")
    check("budget: the capped row is free",
          all(r.get("billable") is False for r in s.rows
              if r.get("status") == "NOT_CHECKED_CAP"))

    # 6. the dataset refuses before writing anything
    s = run({"publications": ["www.thefp.com"]}, fail_before_write=True)
    check("refused write: nothing claimed as delivered",
          (s.summary or {}).get("postsDelivered") in (0, None))
    check("refused write: the batch is unverified",
          (s.summary or {}).get("deliveredUnverified") == 2)
    check("refused write: the run stops on the dataset",
          (s.summary or {}).get("stopReason") in ("dataset-write-error", "charge-error"))
    check("refused write: the note never claims a delivery",
          "nothing was delivered" not in str((s.summary or {}).get("datasetNote", "")))

    # 7. an unreadable counter is unknown, not nothing
    s = run({"publications": ["www.thefp.com"]}, fail_push=True, meta_fail=True)
    note = str((s.summary or {}).get("datasetNote", "")).lower()
    check("unknown counter: both are unknown",
          "unknown" in note and "nothing was charged" not in note)
    check("unknown counter: the charge is uncertain",
          (s.summary or {}).get("chargeUncertain") == 2)

    # 7b. a lagging counter looks exactly like a refused write
    s = run({"publications": ["www.thefp.com"]}, fail_push=True, meta_lag=1)
    note = str((s.summary or {}).get("datasetNote", "")).lower()
    check("lagging counter: the charge is uncertain",
          (s.summary or {}).get("chargeUncertain") == 2)
    check("lagging counter: the note says not confirmed", "not confirmed" in note)
    check("lagging counter: no claim that nothing was sold",
          "nothing is claimed as sold" not in note)

    # 7c. the first batch is sold, the second fails
    s = run({"publications": ["www.thefp.com"]}, fail_write_on=2,
            totals={"www.thefp.com": 12})
    note = str((s.summary or {}).get("datasetNote", "")).lower()
    check("second batch: the first ten stay sold",
          (s.summary or {}).get("postsDelivered") == 10)
    check("second batch: the stop is the dataset",
          (s.summary or {}).get("stopReason") == "dataset-write-error")
    check("second batch: the note is not the unknown one", "both unknown" not in note)

    # 8. an unknown publication is a free row and a green run
    s = run({"publications": ["missing.example"]})
    check("not found: a free row", (s.summary or {}).get("byStatus", {}).get("NOT_FOUND") == 1)
    check("not found: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not found: a green run, not a failure", s.failed is None)
    check("not found: the header says zero delivered",
          str(s.status or "").startswith("0 post(s) delivered"))

    # 9. a dead road is a failure with the reason
    s = run({"publications": ["slow.example"]})
    check("dead road: the run fails", s.failed is not None)
    check("dead road: the reason is named", "did not answer in time" in str(s.failed))

    # 10. no input at all
    s = run({})
    check("empty input: the run fails with an example", s.failed is not None
          and "publications" in str(s.failed))

    # 11. a free actor delivers free and never claims a sale
    s = run({"publications": ["www.thefp.com"]}, Charging(ppe=False))
    check("not per event: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not per event: the rows still arrive",
          (s.summary or {}).get("postsDelivered") == 2)
    check("not per event: billable is not claimed",
          all(r.get("billable") is False for r in s.rows))
    check("not per event: the note says why nothing is charged",
          "not sold per event" in str((s.summary or {}).get("datasetNote")))

    # 11b. a free actor counts what it delivered free
    s = run({"publications": ["www.thefp.com"]}, Charging(ppe=False))
    check("not per event: the free rows are counted as delivered uncharged",
          (s.summary or {}).get("deliveredUncharged") == 2)
    check("not per event: the reason is named once, not twice",
          str((s.summary or {}).get("datasetNote")).count("not sold per event") == 1)

    # 11c. a publication that published nothing leaves a row of its own
    s = run({"publications": ["www.thefp.com", "www.empty.example"]},
            totals={"www.thefp.com": 2, "www.empty.example": 0})
    check("empty archive: a free EMPTY row",
          (s.summary or {}).get("byStatus", {}).get("EMPTY") == 1)
    check("empty archive: counted in the summary",
          (s.summary or {}).get("publicationsEmpty") == 1)
    check("empty archive: the header counts rows written, not inputs",
          str(s.status or "") == "2 post(s) delivered, 2 charged, 1 free status row(s)")
    check("empty archive: the empty one is not a failure", s.failed is None)

    # 11d. a run with nothing but an empty archive is green, not failed
    s = run({"publications": ["www.empty.example"]}, totals={"www.empty.example": 0})
    check("empty archive alone: nothing charged",
          (s.summary or {}).get("chargedEvents") == 0)
    check("empty archive alone: a green run", s.failed is None)
    check("empty archive alone: the header names the free row",
          str(s.status or "").startswith("0 post(s) delivered, 0 charged, 1 free status row(s)"))

    # 11d2. the header counts rows that were written, not inputs that were sent
    s = run({"publications": ["missing.example"]}, fail_write_on=1)
    check("header: a refused status row is not counted as written",
          str(s.status or "").startswith("0 post(s) delivered, 0 charged, 0 free status row(s)"))
    check("header: the refused status row is reported as a write error",
          (s.summary or {}).get("writeErrors") == 1)

    # 11e. the request timeout follows the platform deadline instead of a fixed 20 s
    import os
    from datetime import datetime, timedelta, timezone as _tz
    when = (datetime.now(_tz.utc) + timedelta(seconds=20)).isoformat(timespec="seconds")
    os.environ["ACTOR_TIMEOUT_AT"] = when.replace("+00:00", "Z")
    try:
        s = run({"publications": ["www.thefp.com"]})
    finally:
        os.environ.pop("ACTOR_TIMEOUT_AT", None)
    check("deadline: the request timeout is cut to what is left, not the client default",
          bool(TIMEOUTS) and all(x is not None and x <= 20.0 for x in TIMEOUTS)
          and any(x < 20.0 for x in TIMEOUTS))
    check("deadline: the run still sells", (s.summary or {}).get("chargedEvents") == 2)

    # 12. an unreadable price stops the sale instead of guessing
    class Broken:
        def get_pricing_info(self):
            raise RuntimeError("no price")

        def is_event_charge_limit_reached(self, _n):
            return False
    s = run({"publications": ["www.thefp.com"]}, Broken())
    check("unreadable price: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("unreadable price: the rows say billing",
          (s.summary or {}).get("byStatus", {}).get("NOT_CHECKED_BILLING") == 2)

    # 13. B1: one publication under two addresses is sold once
    s = run({"publications": ["noahpinion.blog", "noahpinion.substack.com"],
             "maxPostsPerPublication": 3},
            aliases={"noahpinion.blog": "noahpinion.substack.com"},
            totals={"noahpinion.substack.com": 3})
    check("alias: the same publication is sold once",
          (s.summary or {}).get("chargedEvents") == 3)
    check("alias: the second address is a free DUPLICATE",
          (s.summary or {}).get("byStatus", {}).get("DUPLICATE") == 1)
    check("alias: no post is paid for twice",
          len({r.get("postId") for r in paid(s)}) == len(paid(s)) == 3)
    check("alias: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("alias: the free row names the host that answered",
          any("noahpinion.substack.com" in str(r.get("reason"))
              for r in s.rows if r.get("status") == "DUPLICATE"))

    # 14. B2: one empty publication next to a dead one is a green run
    s = run({"publications": ["www.empty.example", "slow.example"]},
            totals={"www.empty.example": 0})
    check("empty plus dead: a green run, not a failure", s.failed is None)
    check("empty plus dead: the empty one is counted",
          (s.summary or {}).get("publicationsEmpty") == 1)

    # 14b. a publication that read one page and then failed is not paid twice via its alias
    s = run({"publications": ["noahpinion.blog", "noahpinion.substack.com"],
             "maxPostsPerPublication": 200},
            aliases={"noahpinion.blog": "noahpinion.substack.com"},
            totals={"noahpinion.substack.com": 60},
            fail_page={"noahpinion.substack.com": 2})
    check("alias after a failed page: no post id is paid twice",
          len({r.get("postId") for r in paid(s)}) == len(paid(s)))
    check("alias after a failed page: every charged row is a unique post",
          (s.summary or {}).get("chargedEvents") == len(paid(s)))
    check("alias after a failed page: the repeat is a free DUPLICATE",
          (s.summary or {}).get("byStatus", {}).get("DUPLICATE", 0) >= 1)

    # 14c. one post cross-posted in two publications is delivered once
    s = run({"publications": ["one.example", "two.example"]})
    check("cross-posted id: the same post id is sold once",
          (s.summary or {}).get("chargedEvents") == 2)
    check("cross-posted id: the second copy is a free DUPLICATE",
          (s.summary or {}).get("byStatus", {}).get("DUPLICATE") == 2)

    # 15. B2: every publication dead is still a failure
    s = run({"publications": ["slow.example", "slow-two.example"]})
    check("all dead: the run fails", s.failed is not None)

    # 16. B3: a byline that is not a list does not kill the run
    s = run({"publications": ["www.thefp.com"]}, weird={"www.thefp.com"})
    check("odd bylines: no crash", s.crashed == "")
    check("odd bylines: the rows are still sold", len(paid(s)) == 2)
    check("odd bylines: the authors are empty, not a crash",
          all(r.get("authors") == [] for r in paid(s)))
    check("odd bylines: the summary still lands", s.summary is not None)
    check("odd bylines: the leading blank of a title is cut",
          all(str(r.get("title") or "").strip() == str(r.get("title") or "")
              for r in paid(s)))

    # 17. B4: the deadline is checked between pages, not only between publications
    import os as _os
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz2
    lead = (_dt.now(_tz2.utc) + _td(seconds=19)).isoformat(timespec="seconds")
    _os.environ["ACTOR_TIMEOUT_AT"] = lead.replace("+00:00", "Z")
    try:
        s = run({"publications": ["www.thefp.com"], "maxPostsPerPublication": 200},
                totals={"www.thefp.com": 200}, sleepy={"www.thefp.com": 6.0})
    finally:
        _os.environ.pop("ACTOR_TIMEOUT_AT", None)
    check("deadline per page: the run stops on time",
          (s.summary or {}).get("stopReason") == "deadline")
    check("deadline per page: the unread part is a free row with the time reason",
          (s.summary or {}).get("byStatus", {}).get("NOT_CHECKED_TIME", 0) >= 1)
    check("deadline per page: the reason is recorded",
          bool((s.summary or {}).get("firstFailure")))
    check("deadline per page: the run does not crash and keeps its summary",
          s.crashed == "" and s.summary is not None)

    # 18. a short charge stops the run instead of only leaving a note
    s = run({"publications": ["www.thefp.com"]}, Charging(charged=1),
            totals={"www.thefp.com": 5})
    check("short charge: the run stops on money",
          (s.summary or {}).get("stopReason") == "charge-error")
    check("short charge: the unpaid rows are counted, not sold",
          (s.summary or {}).get("deliveredUncharged", 0) >= 1)
    check("short charge: the header names the stop",
          "stopped" in str(s.status or ""))

    print(f"{PASSED}/{PASSED + len(FAILED)} passed")
    if FAILED:
        print("FAILED:")
        for name in FAILED:
            print("  -", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
