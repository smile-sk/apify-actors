"""The money path through the real main(), against a stand that repeats the SDK.

Why this file exists: in this park a paid actor twice reached the gates with a money
branch nothing exercised. The stand must repeat the SDK, not our belief: push_data
returns charged_count, writes the rows BEFORE it charges, Dataset.get_metadata answers an
object with item_count, compute_push_data_limit is keyword only.
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


SERIES = {
    "properties": {
        "meta": {"updated_at": "2026-11-14T09:20:20Z"},
        "timeseries": [
            {"time": "2026-11-14T10:00:00Z",
             "data": {"instant": {"details": {"air_temperature": 6.3, "wind_speed": 6.0}},
                      "next_1_hours": {"summary": {"symbol_code": "cloudy"},
                                       "details": {"precipitation_amount": 0.0}}}},
        ],
    }
}


class PriceInfo:
    is_pay_per_event = True
    per_event_prices = {"location-delivered": 0.0005}


class Charging:
    def __init__(self, budget: int = 1000, ppe: bool = True, capped: bool = False,
                 charged=None):
        self.budget = budget
        self.info = PriceInfo()
        self.info.is_pay_per_event = ppe
        self.info.per_event_prices = ({"location-delivered": 0.0005} if ppe
                                      else {})
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
                 meta_fail=False, meta_lag=0, fail_push_on=None):
        self.payload = payload
        self.fail_push_on = fail_push_on
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
        self.push_calls += 1
        self.rows.extend(data)
        if self.fail_push or (self.fail_push_on and self.push_calls == self.fail_push_on):
            raise RuntimeError("the platform refused the charge")
        charged = self.charging.charged
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

    def get_env(self):
        return {"run_id": "TESTRUN45"}


def fake_forecast(lat, lon, client, timeout=None):
    if round(lat, 2) == 11.11:
        return None, "the forecast service answered HTTP 500"
    return SERIES, ""


def run(payload, charging=None, fail_push=False, fail_before_write=False,
        meta_fail=False, meta_lag=0, fail_push_on=None):
    stand = Stand(payload, charging, fail_push, fail_before_write, meta_fail, meta_lag,
                  fail_push_on)
    m.Actor = stand
    m.weather.forecast = fake_forecast
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    return stand


def paid(stand):
    return [r for r in stand.rows if r.get("status") == "OK"]


def run_all() -> int:
    # 1. two normal places
    s = run({"locations": ["50.45,30.52", "52.52,13.40"]})
    check("normal: no crash", s.crashed == "")
    check("normal: two sold rows", len(paid(s)) == 2)
    check("normal: two charged", (s.summary or {}).get("chargedEvents") == 2)
    check("normal: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("normal: billable claimed", all(r.get("billable") is True for r in paid(s)))
    check("normal: the header counts delivered",
          str(s.status or "").startswith("2 place(s) delivered"))
    check("normal: the summary is written twice, bare and run scoped",
          "RUN_SUMMARY" in s.store.data and "RUN_SUMMARY-TESTRUN45" in s.store.data)

    # 2. a bad pair and a good one
    s = run({"locations": ["Kyiv", "50.45,30.52"]})
    check("invalid: one free row with a reason",
          any(r.get("status") == "INVALID_INPUT" and "latitude,longitude" in str(r.get("reason"))
              for r in s.rows))
    check("invalid: only one charge", (s.summary or {}).get("chargedEvents") == 1)
    check("invalid: the free row carries no data",
          all(not r.get("hourly") and not r.get("latitude") for r in s.rows
              if r.get("status") != "OK"))

    # 3. a repeated pair
    s = run({"locations": ["50.45,30.52", "50.45,30.52"]})
    check("duplicate: one paid and one free",
          len(paid(s)) == 1 and (s.summary or {}).get("byStatus", {}).get("DUPLICATE") == 1)
    check("duplicate: charged once", (s.summary or {}).get("chargedEvents") == 1)

    # 4. the cap past maxLocations
    s = run({"locations": ["50.45,30.52", "52.52,13.40"], "maxLocations": 1})
    check("limit: one sold", len(paid(s)) == 1)
    check("limit: the other row says it was not attempted",
          (s.summary or {}).get("byStatus", {}).get("NOT_CHECKED_LIMIT") == 1)

    # 5. the budget ends inside the batch
    s = run({"locations": ["50.45,30.52", "52.52,13.40"]}, Charging(budget=1))
    check("budget: one sold", len(paid(s)) == 1)
    check("budget: one capped", (s.summary or {}).get("byStatus", {}).get("NOT_CHECKED_CAP") == 1)
    check("budget: the stop reason is money", (s.summary or {}).get("stopReason") == "spending-cap")
    check("budget: the capped row is free", all(r.get("billable") is False for r in s.rows
                                                if r.get("status") == "NOT_CHECKED_CAP"))

    # 6. the dataset refuses the write before it writes anything
    s = run({"locations": ["50.45,30.52", "52.52,13.40"]}, fail_before_write=True)
    check("refused write: nothing claimed as delivered",
          (s.summary or {}).get("locationsDelivered") in (0, None))
    check("refused write: the batch is counted as unverified",
          (s.summary or {}).get("deliveredUnverified") == 2)
    check("refused write: the run stops on the dataset", (s.summary or {}).get("stopReason")
          in ("dataset-write-error", "charge-error"))
    check("refused write: the note never claims a delivery",
          "nothing was delivered" not in str((s.summary or {}).get("datasetNote", "")))

    # 7. an unreadable counter is "unknown", not "nothing happened"
    s = run({"locations": ["50.45,30.52"]}, fail_push=True, meta_fail=True)
    note = str((s.summary or {}).get("datasetNote", "")).lower()
    check("unknown counter: the note says both are unknown",
          "unknown" in note and "nothing was charged" not in note)
    check("unknown counter: the charge is called uncertain",
          (s.summary or {}).get("chargeUncertain") == 1)

    # 7b. a readable but lagging counter looks exactly like a refused write
    s = run({"locations": ["50.45,30.52"]}, fail_push=True, meta_lag=1)
    note = str((s.summary or {}).get("datasetNote", "")).lower()
    check("lagging counter: the charge is called uncertain",
          (s.summary or {}).get("chargeUncertain") == 1)
    check("lagging counter: the note says the charge is not confirmed",
          "not confirmed" in note)
    check("lagging counter: the note never claims nothing was sold",
          "nothing is claimed as sold" not in note)
    check("lagging counter: the row is not claimed as delivered",
          (s.summary or {}).get("locationsDelivered") == 0)

    # 7c. the FIRST batch is sold, the SECOND fails: the baseline must not swallow it.
    # Orestes v02: before = baseline instead of baseline + rows_written stayed 80/80 green,
    # because no test ever wrote a good batch before a failing one.
    places = [f"{50 + i / 100:.2f},30.52" for i in range(12)]
    s = run({"locations": places}, fail_push_on=2)
    note = str((s.summary or {}).get("datasetNote", "")).lower()
    check("second batch fails: the note says the rows stay", "the rows stay" in note)
    check("second batch fails: the note never says unknown", "unknown" not in note)
    check("second batch fails: the first batch is still sold",
          (s.summary or {}).get("chargedEvents") == 10)
    check("second batch fails: the run stops on the charge",
          (s.summary or {}).get("stopReason") == "charge-error")
    check("second batch fails: all twelve rows are counted as delivered",
          (s.summary or {}).get("locationsDelivered") == 12)

    # 8. every place failed: the run is not green
    s = run({"locations": ["11.11,11.11"]})
    check("closed road: the run fails", bool(s.failed))
    check("closed road: the failure names the attempts",
          "1 attempt(s)" in str(s.failed or ""))
    check("closed road: the first reason is quoted", "HTTP 500" in str(s.failed or ""))

    # 8b. an input of names is not a closed road: nobody was asked
    s = run({"locations": ["Kyiv", "Berlin"]})
    said = str(s.failed or "")
    check("names: the run fails with the format, not with attempts",
          "None of the 2 place(s) was a coordinate pair" in said)
    check("names: the source was never asked", (s.summary or {}).get("locationsAttempted") == 0)
    check("names: the failure does not pretend the source answered",
          "was asked for" not in said)
    check("names: both rows are free INVALID_INPUT",
          [r.get("status") for r in s.rows] == ["INVALID_INPUT", "INVALID_INPUT"])

    # 8c. the summary carries the attribution the README promises
    s = run({"locations": ["50.45,30.52"]})
    check("summary: the source and its licence are named",
          str((s.summary or {}).get("source", "")).startswith("MET Norway Locationforecast"))
    s = run({"locations": ["Kyiv"]})
    free_row = s.rows[0]
    check("free row: no empty place, country, admin, timezone or current column",
          all(key not in free_row for key in ("place", "country", "admin", "timezone",
                                              "current")))

    # 9. the platform charges fewer rows than the batch
    s = run({"locations": ["50.45,30.52", "52.52,13.40"]}, Charging(charged=1))
    check("partial charge: the shortfall is named",
          (s.summary or {}).get("deliveredUncharged") == 1)
    check("partial charge: one event paid", (s.summary or {}).get("chargedEvents") == 1)

    # 10. no price: nothing is sold, the rows are billing rows
    s = run({"locations": ["50.45,30.52"]}, Charging(ppe=False))
    check("no price: nothing is claimed as sold", (s.summary or {}).get("billingActive") is False)
    check("no price: the row is free", all(r.get("billable") is False for r in s.rows))

    return 0 if not FAILED else 1


if __name__ == "__main__":
    code = run_all()
    for name in FAILED:
        print("FAIL:", name)
    print(f"PASSED {PASSED} FAILED {len(FAILED)}")
    sys.exit(code)
