"""The money path, through the real main(), against a stand that repeats the SDK.

Why this file exists: a paid actor with no end to end money test let a blocker reach
the gates in this park once already. The stand must repeat the SDK, not our belief:
compute_push_data_limit is keyword only, push_data returns ChargeResult.charged_count
and writes the rows BEFORE it charges them, and Dataset has no set_value.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
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


APPLE = {
    "trackId": 389801252, "bundleId": "com.burbn.instagram", "trackName": "Instagram",
    "sellerName": "Instagram, Inc.", "averageUserRating": 4.69, "userRatingCount": 29706439,
    "price": 0.0, "currency": "USD", "primaryGenreName": "Photo & Video",
    "genres": ["Photo & Video"], "version": "455.0.0", "releaseDate": "2010-10-06T00:00:00Z",
    "currentVersionReleaseDate": "2026-11-09T15:57:59Z", "contentAdvisoryRating": "12+",
    "minimumOsVersion": "16.3", "fileSizeBytes": "699975680",
    "languageCodesISO2A": ["EN"], "artworkUrl512": "https://is1.example/512.png",
    "trackViewUrl": "https://apps.apple.com/us/app/instagram/id389801252",
}

ANDROID = {
    "appId": "com.whatsapp", "title": "WhatsApp", "developer": "WhatsApp LLC",
    "score": 4.62, "ratings": 247674171, "reviews": 1000, "installs": "10,000,000,000+",
    "realInstalls": 12000000000, "price": 0, "currency": "USD", "free": True,
    "genre": "Communication", "version": "2.24", "released": "Jan 1, 2011",
    "lastUpdatedOn": "Nov 1, 2026", "contentRating": "Everyone", "adSupported": False,
    "offersIAP": True, "androidVersion": "5.0", "icon": "https://play.example/i.png",
    "url": "https://play.google.com/store/apps/details?id=com.whatsapp",
}


class PriceInfo:
    def __init__(self, ppe: bool = True, prices: dict | None = None) -> None:
        self.is_pay_per_event = ppe
        self.per_event_prices = {"app-detail": 0.002} if prices is None else prices


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
                                is_default_dataset: bool) -> int:
        assert is_default_dataset is True
        return min(items_count, self.budget)


class ChargeResult:
    def __init__(self, charged: int | None) -> None:
        self.charged_count = charged


class Metadata:
    """What Dataset.get_metadata() answers in the SDK: an object, not a dict."""

    def __init__(self, item_count: int) -> None:
        self.item_count = item_count


class Dataset:
    def __init__(self, stand: "Stand") -> None:
        self.stand = stand

    async def push_data(self, data):
        if isinstance(data, dict):
            data = [data]
        self.stand.rows.extend(data)

    async def get_metadata(self):
        # The platform counter can fail and it lags the write. A stand that always
        # answers honestly hides the whole class this file exists to catch.
        if self.stand.meta_raises:
            raise RuntimeError("the dataset metadata is not readable in this stand")
        if self.stand.meta_lag_reads > 0:
            self.stand.meta_lag_reads -= 1
            return Metadata(self.stand.meta_frozen)
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
                 fail_before_write: bool = False, meta_raises: bool = False,
                 meta_lag_reads: int = 0, fail_partial: int = 0) -> None:
        self.payload = payload
        self.charging = charging
        self.fail_push = fail_push
        self.fail_before_write = fail_before_write
        self.meta_raises = meta_raises
        self.meta_lag_reads = meta_lag_reads
        self.meta_frozen = 0
        self.fail_partial = fail_partial
        self.rows: list = []
        self.summary = None
        self.status = None
        self.failed = None
        self.store = Store()
        self.log = Log()
        self.crashed = ""
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
        # Two different failures wear the same exception in the SDK: the charge can fail
        # AFTER the rows landed, and the dataset can refuse the write itself, in which
        # case nothing landed. The stand must be able to produce both.
        if self.fail_before_write:
            raise RuntimeError("the dataset refused the rows")
        self.meta_frozen = len(self.rows)
        if self.fail_partial:
            # only part of the batch reached the dataset before the call raised
            self.rows.extend(data[: self.fail_partial])
            raise RuntimeError("the batch was cut in the middle")
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
        # the named store is a different container in the platform, but sharing it here
        # lets the test see both summary keys at once
        return self.store

    async def set_status_message(self, text):
        self.status = text

    async def fail(self, *, status_message="", **_k):
        self.failed = status_message

    def get_env(self):
        return {"run_id": "TESTRUN41"}


def fake_apple(client, *, app_id=None, bundle_id=None, country="us"):
    if app_id == "404404" or bundle_id:
        # bundle lookups answer only for known bundles; the tests use unknown ones
        if bundle_id == "com.burbn.instagram":
            return dict(APPLE), ""
        return None, "the App Store has no app with this id"
    return dict(APPLE, trackId=int(app_id or 389801252)), ""


def fake_android(package, *, country="us", lang="en"):
    if package.startswith("com.missing"):
        return None, "Google Play has no app with this package"
    return dict(ANDROID, appId=package), ""


def run(payload: dict, charging: Charging | None = None, fail_push: bool = False,
        fail_before_write: bool = False, meta_raises: bool = False,
        meta_lag_reads: int = 0, fail_partial: int = 0) -> Stand:
    stand = Stand(payload, charging or Charging(), fail_push=fail_push,
                  fail_before_write=fail_before_write, meta_raises=meta_raises,
                  meta_lag_reads=meta_lag_reads, fail_partial=fail_partial)
    m.Actor = stand
    m.stores.apple_lookup = fake_apple
    m.stores.android_lookup = fake_android
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001 - a crash must fail a named check
        stand.crashed = f"{type(exc).__name__}: {exc}"
    CRASHES.append(stand)
    return stand


def ok_rows(stand: Stand) -> list:
    return [r for r in stand.rows if r.get("status") == "OK"]


def run_all() -> int:
    # 1. normal: one App Store app and one Google Play app, two charges
    s = run({"apps": ["389801252", "com.whatsapp"]})
    check("normal: no crash", s.crashed == "")
    check("normal: two sold rows", len(ok_rows(s)) == 2)
    check("normal: two charged", (s.summary or {}).get("chargedEvents") == 2)
    check("normal: delivered two", (s.summary or {}).get("appsDelivered") == 2)
    check("normal: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("normal: stop reason empty", (s.summary or {}).get("stopReason") == "")
    check("normal: summary written bare", "RUN_SUMMARY" in s.store.data)
    check("normal: a delivered row is claimed as billable",
          all(r.get("billable") is True for r in ok_rows(s)))
    check("normal: the run store holds the run-scoped copy",
          "RUN_SUMMARY-TESTRUN41" in s.store.data)

    # 2. the budget ends in the middle: paid prefix, free tail, no silent trim
    s = run({"apps": ["389801252", "com.whatsapp", "com.a", "com.b"]},
            Charging(budget=2))
    check("budget: two sold", len(ok_rows(s)) == 2)
    check("budget: two capped", len([r for r in s.rows if r.get("status") == "NOT_CHECKED_CAP"]) == 2)
    check("budget: charged two", (s.summary or {}).get("chargedEvents") == 2)
    check("budget: the cut tail carries no data",
          all(not r.get("appId") and not r.get("title") and not r.get("storeUrl")
              for r in s.rows if r.get("status") != "OK"))
    check("budget: stop reason is the cap", (s.summary or {}).get("stopReason") == "spending-cap")
    check("budget: the header counts delivered apps, not read ones",
          str(s.status or "").startswith("2 app(s) delivered"))
    check("budget: no mismatch", (s.summary or {}).get("counterMismatch") is False)

    # 3. the cap was already reached before the first app
    s = run({"apps": ["389801252", "com.whatsapp"]}, Charging(capped=True))
    check("cap before start: nothing sold", len(ok_rows(s)) == 0)
    check("cap before start: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("cap before start: every line has its status",
          len(s.rows) == 2 and (s.summary or {}).get("counterMismatch") is False)

    # 4. a charge that raises after the write
    s = run({"apps": ["389801252", "com.whatsapp"]}, fail_push=True)
    check("charge error: the rows stay in the dataset", len(ok_rows(s)) == 2)
    check("charge error: delivered and uncharged counted",
          (s.summary or {}).get("deliveredUncharged") == 2)
    check("charge error: charge uncertain counted",
          (s.summary or {}).get("chargeUncertain") == 2)
    check("charge error: nothing claimed as charged", (s.summary or {}).get("chargedEvents") == 0)
    check("charge error: stop reason", (s.summary or {}).get("stopReason") == "charge-error")
    check("charge error: no second row for the same app", len(s.rows) == 2)

    # 4b. the dataset refused the write itself: nothing landed, so nothing is delivered
    s = run({"apps": ["389801252", "com.whatsapp"]}, fail_before_write=True)
    check("refused write: no row in the dataset", len(s.rows) == 0)
    check("refused write: nothing delivered", (s.summary or {}).get("appsDelivered") == 0)
    check("refused write: nothing written", (s.summary or {}).get("rowsWritten") == 0)
    check("refused write: the batch is named unverified",
          (s.summary or {}).get("deliveredUnverified") == 2)
    check("refused write: nothing claimed as charged",
          (s.summary or {}).get("chargedEvents") == 0)
    check("refused write: stop reason is the dataset",
          (s.summary or {}).get("stopReason") == "dataset-write-error")
    check("refused write: the note says nothing was charged",
          "nothing was delivered and nothing was charged"
          in ((s.summary or {}).get("datasetNote") or ""))
    check("refused write: the mismatch is honest",
          (s.summary or {}).get("counterMismatch") is True)
    check("refused write: write errors counted", (s.summary or {}).get("writeErrors") == 1)

    # 5. the charge result reports no count
    s = run({"apps": ["389801252", "com.whatsapp"]}, Charging(omit_count=True))
    check("no count: delivered uncharged", (s.summary or {}).get("deliveredUncharged") == 2)
    check("no count: stop reason", (s.summary or {}).get("stopReason") == "charge-error")
    check("no count: no duplicate row", len(s.rows) == 2)

    # 6. the platform charged fewer than the batch
    s = run({"apps": ["389801252", "com.whatsapp"]}, Charging(charged=1))
    check("short charge: one charged", (s.summary or {}).get("chargedEvents") == 1)
    check("short charge: the difference named in the note",
          "charged 1 of 2" in ((s.summary or {}).get("datasetNote") or ""))

    # 7. an app the store does not have is free and carries no product
    s = run({"apps": ["404404", "com.missing.app"]})
    check("not found: no sold rows", len(ok_rows(s)) == 0)
    check("not found: two free rows with reasons",
          len([r for r in s.rows if r.get("status") == "NOT_FOUND"]) == 2 and
          all(r.get("reason") for r in s.rows))
    check("not found: free rows carry no product",
          all(not r.get("appId") and not r.get("iconUrl") for r in s.rows))
    check("not found: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not found: no mismatch", (s.summary or {}).get("counterMismatch") is False)

    # 8. the same app twice is charged once
    s = run({"apps": ["389801252", "https://apps.apple.com/us/app/x/id389801252"]})
    check("duplicate: charged once", (s.summary or {}).get("chargedEvents") == 1)
    check("duplicate: the second line is free",
          [r.get("status") for r in s.rows].count("DUPLICATE") == 1)

    # 9. maxApps cuts without charging and without hiding the cut
    s = run({"apps": ["389801252", "com.whatsapp", "com.c"], "maxApps": 2})
    check("maxApps: two sold", len(ok_rows(s)) == 2)
    check("maxApps: the cut line is free with a reason",
          [r.get("status") for r in s.rows].count("NOT_CHECKED_LIMIT") == 1)
    check("maxApps: no mismatch", (s.summary or {}).get("counterMismatch") is False)

    # 10. junk input is a free status, not a paid row
    s = run({"apps": ["hello world", "389801252"]})
    check("junk: one free invalid row",
          [r.get("status") for r in s.rows].count("INVALID_INPUT") == 1)
    check("junk: the good app is still sold", len(ok_rows(s)) == 1)

    # 11. an empty input stops before the dataset
    s = run({"apps": ["   ", ""]})
    check("empty: the run fails and names the field, not just the counters",
          bool(s.failed) and "No apps were sent" in str(s.failed))
    check("empty: nothing was written", len(s.rows) == 0)

    # 12. not sold per event: rows are delivered free and not claimed
    s = run({"apps": ["389801252"]}, Charging(ppe=False))
    check("not ppe: delivered", len(ok_rows(s)) == 1)
    check("not ppe: not charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not ppe: billable false", all(r.get("billable") is False for r in ok_rows(s)))
    check("not ppe: billing active reported false",
          (s.summary or {}).get("billingActive") is False)

    # 13. the price cannot be read: nothing is delivered, nothing is charged
    s = run({"apps": ["389801252", "com.whatsapp"]}, Charging(prices={"other-event": 0.01}))
    check("unknown price: nothing sold", len(ok_rows(s)) == 0)
    check("unknown price: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("unknown price: every line is billing",
          [r.get("status") for r in s.rows].count("NOT_CHECKED_BILLING") == 2)
    check("unknown price: stop reason",
          (s.summary or {}).get("stopReason") == "billing-price-unknown")
    check("unknown price: billing active is null",
          (s.summary or {}).get("billingActive") is None)

    # 13b. a full country name is refused before any store is asked
    s = run({"apps": ["389801252"], "country": "United States"})
    check("country: a full name is refused with a reason",
          bool(s.failed) and "two letter store country" in str(s.failed))
    check("country: nothing was written for a bad country", len(s.rows) == 0)
    check("country: nothing was charged for a bad country",
          (s.summary or {}).get("chargedEvents", 0) == 0)

    # 14. too little time left: the run refuses before reading anything
    os.environ["ACTOR_TIMEOUT_AT"] = "2020-01-01T00:00:00Z"
    s = run({"apps": ["389801252"]})
    os.environ.pop("ACTOR_TIMEOUT_AT", None)
    check("deadline: the run fails early with a reason", bool(s.failed))
    check("deadline: nothing was written", len(s.rows) == 0)

    # 15. the dataset counter cannot be read at all: delivery and the charge are unknown,
    # and the note must never claim that nothing was delivered
    s = run({"apps": ["389801252", "com.whatsapp"]}, fail_push=True, meta_raises=True)
    note = (s.summary or {}).get("datasetNote") or ""
    check("unknown counter: the note says both are unknown", "both unknown" in note)
    check("unknown counter: the note never claims nothing was delivered",
          "nothing was delivered" not in note and "nothing was charged" not in note)
    check("unknown counter: the batch is named unverified",
          (s.summary or {}).get("deliveredUnverified") == 2)
    check("unknown counter: the charge is named uncertain",
          (s.summary or {}).get("chargeUncertain") == 2)
    check("unknown counter: no sale is claimed",
          (s.summary or {}).get("chargedEvents") == 0)
    check("unknown counter: stop reason is the charge",
          (s.summary or {}).get("stopReason") == "charge-error")

    # 16. the counter lags behind the write: three readings over a second reach the truth
    s = run({"apps": ["389801252", "com.whatsapp"]}, fail_push=True, meta_lag_reads=2)
    check("lagging counter: the rows are seen as delivered",
          (s.summary or {}).get("appsDelivered") == 2)
    check("lagging counter: the note does not claim nothing was delivered",
          "nothing was delivered" not in ((s.summary or {}).get("datasetNote") or ""))
    check("lagging counter: delivered and uncharged counted",
          (s.summary or {}).get("deliveredUncharged") == 2)
    check("lagging counter: stop reason is the charge",
          (s.summary or {}).get("stopReason") == "charge-error")

    # 16a. only part of the batch landed: those rows are delivered, the rest is unknown
    s = run({"apps": ["389801252", "com.whatsapp"]}, fail_partial=1)
    note = (s.summary or {}).get("datasetNote") or ""
    check("partial write: the rows that landed are delivered",
          (s.summary or {}).get("appsDelivered") == 1)
    check("partial write: they are delivered and not claimed as sold",
          (s.summary or {}).get("deliveredUncharged") == 1)
    check("partial write: the landed row is uncertain to charge",
          (s.summary or {}).get("chargeUncertain") == 1)
    check("partial write: the rest is unverified",
          (s.summary or {}).get("deliveredUnverified") == 1)
    check("partial write: the note names how many of the batch landed",
          "grew by 1 of 2" in note)
    check("partial write: the note never claims nothing was delivered",
          "nothing was delivered" not in note)
    check("partial write: stop reason is the dataset",
          (s.summary or {}).get("stopReason") == "dataset-write-error")
    check("partial write: the missing row shows as a mismatch, not as silence",
          (s.summary or {}).get("counterMismatch") is True)

    # 16b. the tail after a NON money stop must not wear a money label
    s = run({"apps": [f"com.a{i}" for i in range(11)]}, fail_before_write=True)
    tail = [r for r in s.rows if r.get("status") == "NOT_CHECKED_ERROR"]
    check("stop tail: counted as a stop tail, not as money",
          (s.summary or {}).get("appsCutByStop") == 1)
    check("stop tail: not counted as cut by the budget",
          (s.summary or {}).get("appsCutByBudget") == 0)
    check("stop tail: it carries the real reason, not a money claim",
          len(tail) == 1 and "dataset-write-error" in str(tail[0].get("reason"))
          and "stopped on money" not in str(tail[0].get("reason")))

    # 17. the Play client hangs: the wait_for timeout turns it into a free error row
    def slow_android(package, *, country="us", lang="en"):
        time.sleep(0.25)
        return dict(ANDROID, appId=package), ""

    keep_timeout = m.ANDROID_TIMEOUT_SECONDS
    m.ANDROID_TIMEOUT_SECONDS = 0.05
    stand = Stand({"apps": ["com.slow.app"], "platform": "android"}, Charging())
    m.Actor = stand
    m.stores.apple_lookup = fake_apple
    m.stores.android_lookup = slow_android
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    CRASHES.append(stand)
    m.ANDROID_TIMEOUT_SECONDS = keep_timeout
    m.stores.android_lookup = fake_android
    check("play timeout: the row is free with a reason",
          [r.get("status") for r in stand.rows] == ["NOT_CHECKED_ERROR"])
    check("play timeout: the reason names the wait",
          "did not answer within" in str((stand.rows[0].get("reason") if stand.rows else "")))
    check("play timeout: nothing charged", (stand.summary or {}).get("chargedEvents") == 0)

    check("no run crashed with a traceback", all(x.crashed == "" for x in CRASHES))

    print("PASSED", PASSED, "FAILED", len(FAILED))
    for name in FAILED:
        print("  FAIL:", name)
    for crash in [x.crashed for x in CRASHES if x.crashed]:
        print("  CRASH:", crash)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
