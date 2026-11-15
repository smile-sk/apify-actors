"""The money path, through the real main(), against a stand that repeats the SDK."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import main as m  # noqa: E402

PASSED = 0
FAILED: list = []
CRASHES: list = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


class PriceInfo:
    def __init__(self, ppe: bool = True, prices: dict | None = None) -> None:
        self.is_pay_per_event = ppe
        self.per_event_prices = {"email-checked": 0.0004} if prices is None else prices


class Charging:
    def __init__(self, budget: int = 100, ppe: bool = True, prices: dict | None = None,
                 capped: bool = False, charged: int | None = None,
                 omit_count: bool = False) -> None:
        self.budget = budget
        self.info = PriceInfo(ppe, prices)
        self.capped = capped
        self.charged = charged
        self.omit_count = omit_count

    def get_pricing_info(self):
        return self.info

    def is_event_charge_limit_reached(self, _name: str) -> bool:
        return self.capped

    def compute_push_data_limit(self, items_count: int, _event: str, *,
                                is_dataset: bool = False, is_default_dataset: bool) -> int:
        assert is_default_dataset is True
        return min(items_count, self.budget)


class ChargeResult:
    def __init__(self, charged: int | None) -> None:
        self.charged_count = charged


class Metadata:
    """Repeats DatasetMetadata: the counter is an attribute, not a key."""

    def __init__(self, count: int) -> None:
        self.item_count = count


class Dataset:
    def __init__(self, stand: "Stand") -> None:
        self.stand = stand

    async def push_data(self, data):
        if isinstance(data, dict):
            data = [data]
        self.stand.dataset_calls += 1
        if self.stand.fail_before_write:
            raise RuntimeError("the dataset refused the rows")
        self.stand.rows.extend(data)

    async def get_metadata(self):
        self.stand.meta_reads += 1
        if self.stand.metadata_raises:
            raise RuntimeError("the dataset metadata is not readable right now")
        if self.stand.meta_reads <= self.stand.lag_reads:
            # The platform counter lags behind the write: it still reports the old size.
            return Metadata(self.stand.stale_count)
        return Metadata(len(self.stand.rows))

    # deliberately no set_value: Dataset in apify 4.2.x has none


class Store:
    def __init__(self) -> None:
        self.data = {}

    async def set_value(self, key, value):
        self.data[key] = value


class Log:
    def warning(self, *_a, **_k):
        return None

    info = debug = error = warning


class Stand:
    def __init__(self, payload: dict, charging: Charging, fail_push: bool = False,
                 fail_before_write: bool = False, metadata_raises: bool = False,
                 lag_reads: int = 0, fail_paid_write: bool = False) -> None:
        self.payload = payload
        self.charging = charging
        self.fail_push = fail_push
        self.fail_before_write = fail_before_write
        self.metadata_raises = metadata_raises
        self.lag_reads = lag_reads
        # A refusal of the paid batch only: status rows still reach the dataset, which is
        # what a transient failure on one call looks like.
        self.fail_paid_write = fail_paid_write
        self.meta_reads = 0
        self.stale_count = 0
        self.rows: list = []
        self.summary = None
        self.status = None
        self.failed = None
        self.store = Store()
        self.log = Log()
        self.crashed = ""
        self.dataset_calls = 0
        self.dataset_obj = Dataset(self)

    async def init(self):
        return None

    async def get_input(self):
        return self.payload

    async def open_dataset(self):
        return self.dataset_obj

    def get_charging_manager(self):
        return self.charging

    async def push_data(self, data, charged_event_name=None):
        if isinstance(data, dict):
            data = [data]
        # The SDK writes the rows and only then charges them.
        self.stale_count = len(self.rows)
        if self.fail_before_write or self.fail_paid_write:
            raise RuntimeError("the dataset refused the rows")
        self.rows.extend(data)
        if self.fail_push:
            raise RuntimeError("the platform refused the charge")
        if self.charging.omit_count:
            return ChargeResult(None)
        charged = self.charging.charged
        return ChargeResult(len(data) if charged is None else charged)

    async def set_value(self, key, value):
        self.store.data[key] = value
        if key == "RUN_SUMMARY":
            self.summary = value

    async def open_key_value_store(self, *_a, **_k):
        return self.store

    async def set_status_message(self, text):
        self.status = text

    async def fail(self, *, status_message="", **_k):
        self.failed = status_message

    def get_env(self):
        return {"run_id": "TESTRUN42"}


def fake_analyse(client, raw, *, check_dns=True, **_kw):
    """Repeats checks.analyse's contract, including the status names."""
    base = {field: None for field in m.ROW_FIELDS}
    base.update({"email": raw, "normalised": raw, "domain": "example.com",
                 "domainAscii": "example.com", "syntaxValid": True, "domainExists": True,
                 "hasMx": True, "mxHosts": ["mx.example.com"], "acceptsMail": True,
                 "isDisposable": False, "isRole": False, "isFreeProvider": False,
                 "suggestion": None, "status": "OK", "reason": ""})
    if "bad@" in raw:
        base.update({"status": "INVALID_SYNTAX", "reason": "the address must hold exactly one @",
                     "syntaxValid": False, "mxHosts": None,
                     "acceptsMail": None, "hasMx": None, "domainExists": None,
                     "domainAscii": None, "domain": None})
    elif "dead" in raw:
        base.update({"status": "NO_SUCH_DOMAIN", "reason": "the domain does not exist in DNS",
                     "domainExists": False, "hasMx": False,
                     "mxHosts": None, "acceptsMail": False})
    elif "dnsfail" in raw:
        base.update({"status": "NOT_CHECKED_DNS_ERROR", "reason": "the resolver answered HTTP 503",
                     "mxHosts": None, "acceptsMail": None, "hasMx": None})
    return base


ORIGINAL_ANALYSE = m.checks.analyse


def run(payload: dict, charging: Charging | None = None, fail_push: bool = False,
        fail_before_write: bool = False, metadata_raises: bool = False,
        lag_reads: int = 0, fail_paid_write: bool = False) -> Stand:
    stand = Stand(payload, charging or Charging(), fail_push=fail_push,
                  fail_before_write=fail_before_write, metadata_raises=metadata_raises,
                  lag_reads=lag_reads, fail_paid_write=fail_paid_write)
    m.Actor = stand
    m.checks.analyse = fake_analyse
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    CRASHES.append(stand)
    return stand


def run_real(payload: dict, charging: Charging | None = None) -> Stand:
    """The real checks.analyse, no stand: a checkDns run must never reach the resolver."""
    stand = Stand(payload, charging or Charging())
    m.Actor = stand
    m.checks.analyse = ORIGINAL_ANALYSE
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    finally:
        m.checks.analyse = fake_analyse
    CRASHES.append(stand)
    return stand


def delivered(stand: Stand) -> list:
    return [r for r in stand.rows if r.get("mxHosts")]


def run_all() -> int:
    # 1. normal: two verdicts, two charges
    s = run({"emails": ["a@example.com", "b@example.com"]})
    check("normal: no crash", s.crashed == "")
    check("normal: two delivered", (s.summary or {}).get("emailsDelivered") == 2)
    check("normal: two charged", (s.summary or {}).get("chargedEvents") == 2)
    check("normal: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("normal: stop reason empty", (s.summary or {}).get("stopReason") == "")
    check("normal: summary written", "RUN_SUMMARY" in s.store.data)
    check("normal: run-scoped copy written", "RUN_SUMMARY-TESTRUN42" in s.store.data)
    check("normal: the accept-mail counter is named for what it counts",
          "emailsAcceptMail" in (s.summary or {})
          and "emailsValid" not in (s.summary or {}))

    # 2. the budget ends in the middle
    s = run({"emails": ["a@e.com", "b@e.com", "c@e.com"]}, Charging(budget=1))
    check("budget: one delivered", (s.summary or {}).get("emailsDelivered") == 1)
    check("budget: two capped", len([r for r in s.rows
                                     if r.get("status") == "NOT_CHECKED_CAP"]) == 2)
    check("budget: charged one", (s.summary or {}).get("chargedEvents") == 1)
    check("budget: stop reason", (s.summary or {}).get("stopReason") == "spending-cap")
    check("budget: the cut rows carry no verdict",
          all(not r.get("mxHosts") and r.get("valid") is None
              for r in s.rows if r.get("status") == "NOT_CHECKED_CAP"))
    check("budget: no mismatch", (s.summary or {}).get("counterMismatch") is False)

    # 3. the cap was reached before the first address
    s = run({"emails": ["a@e.com"]}, Charging(capped=True))
    check("cap before start: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("cap before start: one status row", len(s.rows) == 1)
    check("cap before start: no verdict", not s.rows[0].get("mxHosts"))

    # 4. a charge that raises after the write
    s = run({"emails": ["a@e.com", "b@e.com"]}, fail_push=True)
    check("charge error: rows stay", (s.summary or {}).get("emailsDelivered") == 2)
    check("charge error: uncharged counted", (s.summary or {}).get("deliveredUncharged") == 2)
    check("charge error: stop reason", (s.summary or {}).get("stopReason") == "charge-error")
    check("charge error: no second row", len(s.rows) == 2)

    # 5. the charge result reports no count
    s = run({"emails": ["a@e.com"]}, Charging(omit_count=True))
    check("no count: uncharged", (s.summary or {}).get("deliveredUncharged") == 1)
    check("no count: uncertain", (s.summary or {}).get("chargeUncertain") == 1)
    check("no count: stop reason", (s.summary or {}).get("stopReason") == "charge-error")

    # 6. a broken address is never paid for
    s = run({"emails": ["bad@@example.com", "a@e.com"]})
    check("invalid syntax: free", (s.summary or {}).get("chargedEvents") == 1)
    check("invalid syntax: its own status",
          [r.get("status") for r in s.rows].count("INVALID_SYNTAX") == 1)
    check("invalid syntax: carries no verdict",
          all(not r.get("mxHosts") and r.get("valid") is None
              for r in s.rows if r.get("status") == "INVALID_SYNTAX"))

    # 7. a missing domain is a verdict and is paid for
    s = run({"emails": ["a@dead.example"]})
    check("missing domain: charged once", (s.summary or {}).get("chargedEvents") == 1)
    check("missing domain: status", s.rows[0].get("status") == "NO_SUCH_DOMAIN")

    # 8. a resolver failure is free
    s = run({"emails": ["a@dnsfail.example"]})
    check("resolver failure: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("resolver failure: its own status",
          s.rows[0].get("status") == "NOT_CHECKED_DNS_ERROR")
    check("resolver failure: no verdict", not s.rows[0].get("mxHosts"))

    # 9. the same address twice is charged once
    s = run({"emails": ["a@e.com", " A@E.COM "]})
    check("duplicate: charged once", (s.summary or {}).get("chargedEvents") == 1)
    check("duplicate: second line free",
          [r.get("status") for r in s.rows].count("DUPLICATE") == 1)

    # 10. maxEmails cuts visibly
    s = run({"emails": ["a@e.com", "b@e.com", "c@e.com"], "maxEmails": 2})
    check("maxEmails: two delivered", (s.summary or {}).get("emailsDelivered") == 2)
    check("maxEmails: the cut line is free",
          [r.get("status") for r in s.rows].count("NOT_CHECKED_LIMIT") == 1)
    check("maxEmails: no mismatch", (s.summary or {}).get("counterMismatch") is False)

    # 11. an empty input stops before the dataset
    s = run({"emails": ["  ", ""]})
    check("empty: fails and names the field",
          bool(s.failed) and "No email addresses were sent" in str(s.failed))
    check("empty: nothing written", len(s.rows) == 0)

    # 12. not sold per event
    s = run({"emails": ["a@e.com"]}, Charging(ppe=False))
    check("not ppe: delivered", (s.summary or {}).get("emailsDelivered") == 1)
    check("not ppe: not charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not ppe: billable false", s.rows[0].get("billable") is False)

    # 13. the price cannot be read
    s = run({"emails": ["a@e.com"]}, Charging(prices={"other-event": 0.01}))
    check("unknown price: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("unknown price: billing status",
          s.rows[0].get("status") == "NOT_CHECKED_BILLING")
    check("unknown price: stop reason",
          (s.summary or {}).get("stopReason") == "billing-price-unknown")
    check("unknown price: billing active null", (s.summary or {}).get("billingActive") is None)

    # 14. too little time left
    os.environ["ACTOR_TIMEOUT_AT"] = "2020-01-01T00:00:00Z"
    s = run({"emails": ["a@e.com"]})
    os.environ.pop("ACTOR_TIMEOUT_AT", None)
    check("deadline: fails early", bool(s.failed))
    check("deadline: nothing written", len(s.rows) == 0)

    # 15. checkDns off is never paid for: the actor cannot sell what it did not ask
    s = run_real({"emails": ["Someone@Example.com", "bad@@example.com"], "checkDns": False})
    check("checkDns off: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("checkDns off: the row is syntax only",
          [r.get("status") for r in s.rows].count("SYNTAX_ONLY") == 1)
    check("checkDns off: no mail verdict claimed",
          all(r.get("acceptsMail") is None for r in s.rows))
    check("checkDns off: counted in the summary",
          (s.summary or {}).get("emailsSyntaxOnly") == 1)
    check("checkDns off: no mismatch", (s.summary or {}).get("counterMismatch") is False)

    # 15b. free rows are written in batches, not one by one
    many = [f"u{i}@e.com" for i in range(150)]
    s = run({"emails": many, "maxEmails": 1})
    check("batching: 149 free lines go in a couple of writes", s.dataset_calls <= 4)
    check("batching: all 149 free lines are written",
          (s.summary or {}).get("rowsWritten") == 150)

    # 15c. the same mailbox in two spellings is one address
    s = run({"emails": ["a@m\u00fcnchen.de", "A@xn--mnchen-3ya.de"]})
    check("dedup: one address in two spellings is charged once",
          (s.summary or {}).get("chargedEvents") == 1)
    check("dedup: the second spelling is a duplicate",
          [r.get("status") for r in s.rows].count("DUPLICATE") == 1)

    # 16. the dataset refuses the rows: nothing was delivered and nothing was charged
    s = run({"emails": ["a@e.com", "b@e.com"]}, fail_before_write=True)
    check("write refused: nothing delivered", (s.summary or {}).get("emailsDelivered") == 0)
    check("write refused: nothing written", (s.summary or {}).get("rowsWritten") == 0)
    check("write refused: the rows are counted unverified",
          (s.summary or {}).get("emailsUnverified") == 2)
    check("write refused: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("write refused: its own stop reason",
          (s.summary or {}).get("stopReason") == "dataset-write-error")
    check("write refused: the mismatch is honest",
          (s.summary or {}).get("counterMismatch") is True)
    check("write refused: no row in the dataset", len(s.rows) == 0)

    # 17. the charge raised AND the counter could not be read: the summary says unknown
    s = run({"emails": ["a@e.com", "b@e.com"]}, fail_push=True, metadata_raises=True)
    note = str((s.summary or {}).get("datasetNote") or "")
    check("unknown counter: never claims nothing was delivered",
          "nothing was delivered" not in note)
    check("unknown counter: never claims nothing was charged",
          "nothing was charged" not in note)
    check("unknown counter: the batch is counted unverified",
          (s.summary or {}).get("emailsUnverified") == 2)
    check("unknown counter: the money is counted uncertain",
          (s.summary or {}).get("chargeUncertain") == 2)
    check("unknown counter: nothing is claimed as delivered",
          (s.summary or {}).get("emailsDelivered") == 0)
    check("unknown counter: its stop reason", (s.summary or {}).get("stopReason") == "charge-error")

    # 18. the charge raised and the counter lagged behind the write: the summary reports
    #     the growth it actually saw instead of inventing "nothing was delivered"
    s = run({"emails": ["a@e.com", "b@e.com"]}, fail_push=True, lag_reads=4)
    note = str((s.summary or {}).get("datasetNote") or "")
    check("lagging counter: the growth is named", "grew by 0 of 2" in note)
    check("lagging counter: nothing claimed as delivered",
          (s.summary or {}).get("emailsDelivered") == 0)
    check("lagging counter: nothing claimed as written",
          (s.summary or {}).get("rowsWritten") == 0)
    check("lagging counter: the batch is counted unverified",
          (s.summary or {}).get("emailsUnverified") == 2)
    check("lagging counter: the mismatch is honest",
          (s.summary or {}).get("counterMismatch") is True)
    check("lagging counter: never claims nothing was delivered",
          "nothing was delivered" not in note)

    # 19. checkDns off: the format verdict keeps its own fields and no DNS verdict
    s = run_real({"emails": ["Someone@Gmail.com"], "checkDns": False})
    row = s.rows[0] if s.rows else {}
    check("syntax only: the format fields are kept",
          row.get("syntaxValid") is True and row.get("domainAscii") == "gmail.com"
          and row.get("isFreeProvider") is True and row.get("localPart") == "Someone")
    check("syntax only: no DNS verdict field",
          row.get("domainExists") is None and row.get("hasMx") is None
          and row.get("mxHosts") is None and row.get("acceptsMail") is None)

    # 20. the rest of the list is named for the real stop, not always for money
    many = [f"u{i}@e.com" for i in range(30)]
    s = run({"emails": many}, fail_paid_write=True)
    tail = [r for r in s.rows if r.get("status") == "NOT_CHECKED_ERROR"]
    check("stop cause: the tail names the dataset, not money",
          len(tail) == 5 and all("dataset" in str(r.get("reason") or "") for r in tail))
    check("stop cause: no tail line is called a money stop",
          not [r for r in tail if "money" in str(r.get("reason") or "").lower()])

    # 21. the deadline arithmetic: three lookups share the runway instead of taking it each
    check("budget: no deadline means no per-request limit", m.request_budget(None) is None)
    for runway in (9.0, 12.0, 16.5, 40.0, 300.0):
        budget = m.request_budget(runway)
        check(f"budget: three lookups fit in {runway:g} s",
              budget is not None
              and m.ANALYSE_QUERIES * budget + 3.0 <= runway + 0.001
              and budget >= m.MIN_REQUEST_SECONDS)
    check("budget: a long runway is capped", m.request_budget(300.0) == m.MAX_REQUEST_SECONDS)

    # 22. 12 s of runway is enough for one address; the old gate of 16.5 s refused it
    import datetime as _dt
    soon = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=12)
    os.environ["ACTOR_TIMEOUT_AT"] = soon.strftime("%Y-%m-%dT%H:%M:%SZ")
    s = run({"emails": ["a@e.com"]})
    os.environ.pop("ACTOR_TIMEOUT_AT", None)
    check("gate: 12 s left is enough for one address", s.failed is None)
    check("gate: and the address is checked", (s.summary or {}).get("emailsDelivered") == 1)

    check("no run crashed with a traceback", all(x.crashed == "" for x in CRASHES))

    print("PASSED", PASSED, "FAILED", len(FAILED))
    for name in FAILED:
        print("  FAIL:", name)
    for crash in [x.crashed for x in CRASHES if x.crashed]:
        print("  CRASH:", crash)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
