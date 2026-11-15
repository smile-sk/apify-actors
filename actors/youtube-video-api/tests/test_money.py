"""The money path, through the real main(), against a stand that repeats the SDK.

Why this file exists: a paid actor with no end to end money test let a blocker reach the
gates in this park twice. The stand must repeat the SDK, not our belief:
compute_push_data_limit is keyword only, push_data returns ChargeResult.charged_count and
writes the rows BEFORE it charges them, Dataset.get_metadata answers an object with
item_count, and Dataset has no set_value.
"""
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


PAYLOAD = {
    "playabilityStatus": {"status": "OK"},
    "videoDetails": {"videoId": "jNQXAC9IVRw", "title": "Me at the zoo", "lengthSeconds": "19",
                     "viewCount": "448439214", "author": "jawed", "channelId": "UC4",
                     "keywords": ["zoo"], "isLiveContent": False,
                     "shortDescription": "first video",
                     "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/x.jpg"}]}},
    "microformat": {"playerMicroformatRenderer": {"publishDate": "2005-04-23T20:31:52-07:00",
                                                 "category": "Film & Animation"}},
}


class PriceInfo:
    def __init__(self, ppe: bool = True, prices: dict | None = None) -> None:
        self.is_pay_per_event = ppe
        self.per_event_prices = {"video-delivered": 0.0002} if prices is None else prices


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
        # The platform counter lags behind the write and can refuse to answer at all:
        # both happen in production and both must be survived, not hoped away.
        if self.stand.meta_fail:
            raise RuntimeError("the dataset metadata is not readable")
        return Metadata(max(0, len(self.stand.rows) - self.stand.meta_lag))

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
                 fail_before_write: bool = False, meta_fail: bool = False,
                 meta_lag: int = 0) -> None:
        self.payload = payload
        self.charging = charging
        self.fail_push = fail_push
        self.fail_before_write = fail_before_write
        self.meta_fail = meta_fail
        self.meta_lag = meta_lag
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
        # Two different failures wear the same exception: the charge can fail AFTER the
        # rows landed, and the dataset can refuse the write itself.
        if self.fail_before_write:
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
        return {"run_id": "TESTRUN43"}


def fake_fetch(client, vid, timeout=None):
    if vid == "notfound001":
        return None, "YouTube has no video with this id (Video unavailable)"
    if vid == "broken00001":
        return None, "YouTube answered HTTP 500"
    return dict(PAYLOAD, videoDetails=dict(PAYLOAD["videoDetails"], videoId=vid)), ""


def run(payload: dict, charging: Charging | None = None, fail_push: bool = False,
        fail_before_write: bool = False, meta_fail: bool = False,
        meta_lag: int = 0, payload_override: dict | None = None) -> Stand:
    stand = Stand(payload, charging or Charging(), fail_push=fail_push,
                  fail_before_write=fail_before_write, meta_fail=meta_fail,
                  meta_lag=meta_lag)
    m.Actor = stand
    m.yt.fetch = fake_fetch
    if payload_override is not None:
        override = payload_override
        m.yt.fetch = lambda client, vid, timeout=None: (
            dict(override, videoDetails=dict(override["videoDetails"], videoId=vid)), "")
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001 - a crash must fail a named check
        stand.crashed = f"{type(exc).__name__}: {exc}"
    CRASHES.append(stand)
    return stand


ORIGINAL_RELAY = None


def run_relay(input_payload: dict, video: dict | None = None, mode: str = "ok",
              required: bool = False, charging=None) -> Stand:
    """One run with the relay configured, the way the platform will run it.

    input_payload is what the buyer sends; video is the player answer the relay gives.
    """
    global ORIGINAL_RELAY
    if ORIGINAL_RELAY is None:
        ORIGINAL_RELAY = m.yt.Relay
    os.environ["YT_RELAY_URL"] = "https://relay.test/ytrelay"
    os.environ["YT_RELAY_TOKEN"] = "secret"
    if required:
        os.environ["YT_RELAY_REQUIRED"] = "1"
    answer = PAYLOAD if video is None else video
    stand = Stand(input_payload, charging or Charging())
    m.Actor = stand
    direct_calls = {"n": 0}

    def counting_fetch(client, vid, timeout=None):
        direct_calls["n"] += 1
        return fake_fetch(client, vid, timeout=timeout)

    class FakeRelay:
        def __init__(self, url, token):
            self.url, self.token = url, token

        def fetch(self, client, vid, timeout=None):
            if mode == "ok":
                return dict(answer, videoDetails=dict(answer["videoDetails"],
                                                      videoId=vid)), "", ""
            if mode == "verdict":
                return None, "YouTube has no video with this id (Video unavailable)", ""
            return None, "the relay did not answer: ConnectError", "relay"

    m.yt.fetch = counting_fetch
    m.yt.Relay = FakeRelay
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    finally:
        for key in ("YT_RELAY_URL", "YT_RELAY_TOKEN", "YT_RELAY_REQUIRED"):
            os.environ.pop(key, None)
        m.yt.Relay = ORIGINAL_RELAY
        m.yt.fetch = fake_fetch
    stand.direct_calls = direct_calls["n"]
    CRASHES.append(stand)
    return stand


def ok_rows(stand: Stand) -> list:
    return [r for r in stand.rows if r.get("status") == "OK"]


A = "jNQXAC9IVRw"
B = "dQw4w9WgXcQ"
C = "kPI9Mc35mOg"
D = "aaaaaaaaaaa"


def run_all() -> int:
    # 1. normal
    s = run({"videos": [A, B]})
    check("normal: no crash", s.crashed == "")
    check("normal: two sold rows", len(ok_rows(s)) == 2)
    check("normal: two charged", (s.summary or {}).get("chargedEvents") == 2)
    check("normal: delivered two", (s.summary or {}).get("videosDelivered") == 2)
    check("normal: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("normal: stop reason empty", (s.summary or {}).get("stopReason") == "")
    check("normal: bare summary written", "RUN_SUMMARY" in s.store.data)
    check("normal: run-scoped summary written", "RUN_SUMMARY-TESTRUN43" in s.store.data)
    check("normal: a sold row claims billable",
          all(r.get("billable") is True for r in ok_rows(s)))
    check("normal: the header counts delivered videos",
          str(s.status or "").startswith("2 video(s) delivered"))

    # 2. budget ends in the middle
    s = run({"videos": [A, B, C, D]}, Charging(budget=2))
    check("budget: two sold", len(ok_rows(s)) == 2)
    check("budget: two capped", [r.get("status") for r in s.rows].count("NOT_CHECKED_CAP") == 2)
    check("budget: charged two", (s.summary or {}).get("chargedEvents") == 2)
    check("budget: the tail carries no data",
          all(not r.get("videoId") and not r.get("title") for r in s.rows
              if r.get("status") != "OK"))
    check("budget: stop reason is the cap", (s.summary or {}).get("stopReason") == "spending-cap")
    check("budget: no mismatch", (s.summary or {}).get("counterMismatch") is False)
    check("budget: the header counts delivered videos",
          str(s.status or "").startswith("2 video(s) delivered"))

    # 3. cap already reached
    s = run({"videos": [A, B]}, Charging(capped=True))
    check("cap before start: nothing sold", len(ok_rows(s)) == 0)
    check("cap before start: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("cap before start: every line has its status",
          len(s.rows) == 2 and (s.summary or {}).get("counterMismatch") is False)

    # 4. a charge that raises after the write
    s = run({"videos": [A, B]}, fail_push=True)
    check("charge error: the rows stay", len(ok_rows(s)) == 2)
    check("charge error: delivered and uncharged", (s.summary or {}).get("deliveredUncharged") == 2)
    check("charge error: charge uncertain", (s.summary or {}).get("chargeUncertain") == 2)
    check("charge error: nothing claimed as charged", (s.summary or {}).get("chargedEvents") == 0)
    check("charge error: stop reason", (s.summary or {}).get("stopReason") == "charge-error")
    check("charge error: no second row", len(s.rows) == 2)

    # 4b. the dataset refused the write itself
    s = run({"videos": [A, B]}, fail_before_write=True)
    check("refused write: no row in the dataset", len(s.rows) == 0)
    check("refused write: nothing delivered", (s.summary or {}).get("videosDelivered") == 0)
    check("refused write: nothing written", (s.summary or {}).get("rowsWritten") == 0)
    check("refused write: the batch is named unverified",
          (s.summary or {}).get("deliveredUnverified") == 2)
    check("refused write: nothing claimed as charged", (s.summary or {}).get("chargedEvents") == 0)
    check("refused write: stop reason is the dataset",
          (s.summary or {}).get("stopReason") == "dataset-write-error")
    note_text = ((s.summary or {}).get("datasetNote") or "")
    check("refused write: the note never claims nothing was delivered",
          "nothing was delivered" not in note_text and "nothing was charged" not in note_text)
    check("refused write: the note names both numbers",
          "grew by 0 of 2" in note_text)
    check("refused write: the mismatch is honest",
          (s.summary or {}).get("counterMismatch") is True)
    check("refused write: write errors counted", (s.summary or {}).get("writeErrors") == 1)

    # 5. the charge result reports no count
    s = run({"videos": [A, B]}, Charging(omit_count=True))
    check("no count: delivered uncharged", (s.summary or {}).get("deliveredUncharged") == 2)
    check("no count: stop reason", (s.summary or {}).get("stopReason") == "charge-error")
    check("no count: no duplicate row", len(s.rows) == 2)

    # 6. the platform charged fewer than the batch
    s = run({"videos": [A, B]}, Charging(charged=1))
    check("short charge: one charged", (s.summary or {}).get("chargedEvents") == 1)
    check("short charge: the difference named", "charged 1 of 2"
          in ((s.summary or {}).get("datasetNote") or ""))

    # 7. a video YouTube does not have is free and carries no product
    s = run({"videos": ["notfound001", "broken00001"]})
    check("not found: no sold rows", len(ok_rows(s)) == 0)
    check("not found: one NOT_FOUND and one ERROR",
          [r.get("status") for r in s.rows].count("NOT_FOUND") == 1 and
          [r.get("status") for r in s.rows].count("NOT_CHECKED_ERROR") == 1)
    check("not found: free rows carry no product",
          all(not r.get("videoId") and not r.get("thumbnailUrl") for r in s.rows))
    check("not found: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not found: no mismatch", (s.summary or {}).get("counterMismatch") is False)

    # 8. the same video twice is charged once
    s = run({"videos": [A, "https://youtu.be/" + A]})
    check("duplicate: charged once", (s.summary or {}).get("chargedEvents") == 1)
    check("duplicate: the second line is free",
          [r.get("status") for r in s.rows].count("DUPLICATE") == 1)

    # 9. maxVideos cuts without charging and without hiding the cut
    s = run({"videos": [A, B, C], "maxVideos": 2})
    check("maxVideos: two sold", len(ok_rows(s)) == 2)
    check("maxVideos: the cut line is free with a reason",
          [r.get("status") for r in s.rows].count("NOT_CHECKED_LIMIT") == 1)
    check("maxVideos: no mismatch", (s.summary or {}).get("counterMismatch") is False)

    # 10. junk input is a free status, not a paid row
    s = run({"videos": ["hello world", A]})
    check("junk: one free invalid row",
          [r.get("status") for r in s.rows].count("INVALID_INPUT") == 1)
    check("junk: the good video is still sold", len(ok_rows(s)) == 1)

    # 11. an empty input stops before the dataset
    s = run({"videos": ["   ", ""]})
    check("empty: the run fails and names the field",
          bool(s.failed) and "No videos were sent" in str(s.failed))
    check("empty: nothing was written", len(s.rows) == 0)

    # 12. not sold per event
    s = run({"videos": [A]}, Charging(ppe=False))
    check("not ppe: delivered", len(ok_rows(s)) == 1)
    check("not ppe: not charged", (s.summary or {}).get("chargedEvents") == 0)
    check("not ppe: billable false", all(r.get("billable") is False for r in ok_rows(s)))

    # 13. the price cannot be read
    s = run({"videos": [A, B]}, Charging(prices={"other-event": 0.01}))
    check("unknown price: nothing sold", len(ok_rows(s)) == 0)
    check("unknown price: nothing charged", (s.summary or {}).get("chargedEvents") == 0)
    check("unknown price: every line is billing",
          [r.get("status") for r in s.rows].count("NOT_CHECKED_BILLING") == 2)
    check("unknown price: stop reason",
          (s.summary or {}).get("stopReason") == "billing-price-unknown")
    check("unknown price: billing active is null",
          (s.summary or {}).get("billingActive") is None)

    # 14. too little time left
    os.environ["ACTOR_TIMEOUT_AT"] = "2020-01-01T00:00:00Z"
    s = run({"videos": [A]})
    os.environ.pop("ACTOR_TIMEOUT_AT", None)
    check("deadline: the run fails early with a reason", bool(s.failed))
    check("deadline: nothing was written", len(s.rows) == 0)

    # 15. B1: the paid write raised and the dataset counter could not be read at all.
    # The run must say delivery is unknown, not that nothing happened: the rows may be
    # in the dataset and the money may be taken, so a silent "nothing" invites a second
    # run and a second charge.
    s = run({"videos": [A, B]}, fail_push=True, meta_fail=True)
    text = ((s.summary or {}).get("datasetNote") or "")
    check("unknown delivery: the batch is unverified", (s.summary or {}).get("deliveredUnverified") == 2)
    check("unknown delivery: nothing claimed as written", (s.summary or {}).get("rowsWritten") == 0)
    check("unknown delivery: nothing claimed as delivered", (s.summary or {}).get("videosDelivered") == 0)
    check("unknown delivery: the note says unknown", "delivery is unknown" in text)
    check("unknown delivery: the note never says nothing was delivered",
          "nothing was delivered" not in text and "nothing was charged" not in text)
    check("unknown delivery: the mismatch is honest", (s.summary or {}).get("counterMismatch") is True)
    check("unknown delivery: stop reason names the dataset",
          (s.summary or {}).get("stopReason") == "dataset-write-error")

    # 16. B1 second face: the counter lags by two rows.
    s = run({"videos": [A, B]}, fail_push=True, meta_lag=2)
    text = ((s.summary or {}).get("datasetNote") or "")
    check("lagging counter: both numbers are named", "grew by 0 of 2" in text)
    check("lagging counter: the note never says nothing was delivered",
          "nothing was delivered" not in text and "nothing was charged" not in text)
    check("lagging counter: the batch is unverified", (s.summary or {}).get("deliveredUnverified") == 2)
    check("lagging counter: nothing claimed as written", (s.summary or {}).get("rowsWritten") == 0)

    # 17. M1: the playability verdict carries the reason YouTube gave, and it is a field
    # of the answer, not a property of the video.
    payload = dict(PAYLOAD, playabilityStatus={"status": "UNPLAYABLE",
                                              "reason": "The page needs to be reloaded"})
    s = run({"videos": [A]}, payload_override=payload)
    sold = ok_rows(s)
    check("playability: the row is still sold", len(sold) == 1)
    check("playability: the status is kept", sold and sold[0].get("playability") == "UNPLAYABLE")
    check("playability: the reason is kept",
          sold and sold[0].get("playabilityReason") == "The page needs to be reloaded")

    # 18. M2: a stop that is not about money must not wear the money label.
    many = ["v%010d" % n for n in range(12)]
    s = run({"videos": many}, fail_before_write=True)
    statuses = [r.get("status") for r in s.rows]
    check("stop by dataset: the tail is not marked as money", "NOT_CHECKED_CAP" not in statuses)
    check("stop by dataset: the tail has its own counter",
          (s.summary or {}).get("videosCutByStop", 0) >= 1)
    check("stop by dataset: the cut by budget stays zero",
          (s.summary or {}).get("videosCutByBudget") == 0)

    # 19. M4: a video id is case sensitive, so two cases are two videos.
    s = run({"videos": ["dQw4w9WgXcQ", "dQw4w9WgXcQ".upper()[:11]]})
    check("case: two different ids are two videos", len(ok_rows(s)) == 2)
    check("case: neither is called a duplicate",
          [r.get("status") for r in s.rows].count("DUPLICATE") == 0)

    # 20. M10: an empty line is a line of the input, not silence.
    s = run({"videos": [A, "", B]})
    check("empty line: counted in the requested total", (s.summary or {}).get("videosRequested") == 3)
    check("empty line: it gets its own free status",
          [r.get("status") for r in s.rows].count("INVALID_INPUT") == 1)
    check("empty line: no mismatch", (s.summary or {}).get("counterMismatch") is False)

    # 21. the relay: the platform network is refused by the player endpoint, so the
    # actor asks our own relay and the summary says which road every row took.
    s = run_relay({"videos": [A, B]}, mode="ok")
    check("relay: two sold rows", len(ok_rows(s)) == 2)
    check("relay: two charged", (s.summary or {}).get("chargedEvents") == 2)
    check("relay: the summary names the relay", (s.summary or {}).get("gateway") == "relay")
    check("relay: both rows went through it", (s.summary or {}).get("relayCalls") == 2
          and (s.summary or {}).get("transports") == {"relay": 2, "direct": 0})
    check("relay: no direct call when the relay works", s.direct_calls == 0)

    s = run_relay({"videos": [A, B]}, mode="verdict")
    check("relay verdict: a missing video is a not-found, not an error",
          [r.get("status") for r in s.rows].count("NOT_FOUND") == 2)
    check("relay verdict: nothing sold", len(ok_rows(s)) == 0)
    check("relay verdict: it is not counted as a relay fault",
          (s.summary or {}).get("relayErrors") == 0)

    s = run_relay({"videos": [A, B]}, mode="dead")
    check("relay dead: the direct call saves the rows", len(ok_rows(s)) == 2)
    check("relay dead: the fallback is counted", (s.summary or {}).get("relayFallbacks") == 2)
    check("relay dead: the fault is counted too", (s.summary or {}).get("relayErrors") == 2)
    check("relay dead: the summary shows the road actually taken",
          (s.summary or {}).get("transports") == {"relay": 0, "direct": 2})

    s = run_relay({"videos": [A, B]}, mode="dead", required=True)
    check("relay required: no direct call is made", s.direct_calls == 0)
    check("relay required: every line gets a free error status",
          [r.get("status") for r in s.rows].count("NOT_CHECKED_ERROR") == 2)
    check("relay required: nothing is sold", len(ok_rows(s)) == 0)
    check("relay required: no mismatch", (s.summary or {}).get("counterMismatch") is False)

    check("no run crashed with a traceback", all(x.crashed == "" for x in CRASHES))

    print("PASSED", PASSED, "FAILED", len(FAILED))
    for name in FAILED:
        print("  FAIL:", name)
    for crash in [x.crashed for x in CRASHES if x.crashed]:
        print("  CRASH:", crash)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
