"""The three points the gates held on the relay branch.

Why this file exists: the relay made one working road and one closed road, and the code
between them could spend the deadline twice, lose the real reason and report a green run
with nothing read. Each of those is money or reputation, so each has a check here.
"""
from __future__ import annotations

import asyncio
import json as _json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import main as m  # noqa: E402
from src import yt as ytm  # noqa: E402

PASSED = 0
FAILED: list = []


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
    "microformat": {"playerMicroformatRenderer": {"publishDate": "2005-04-23",
                                                  "category": "Film"}},
}


class PriceInfo:
    is_pay_per_event = True
    per_event_prices = {"video-delivered": 0.0002}


class Charging:
    def get_pricing_info(self):
        return PriceInfo()

    def is_event_charge_limit_reached(self, _name):
        return False

    def compute_push_data_limit(self, items_count, _event, *, is_default_dataset):
        return items_count


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
        return Meta(len(self.stand.rows))


class Store:
    def __init__(self):
        self.data = {}

    async def set_value(self, key, value):
        self.data[key] = value


class Log:
    def warning(self, *a, **k):
        return None


class Stand:
    def __init__(self, payload):
        self.payload = payload
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
        return Charging()

    async def push_data(self, data, charged_event_name=None):
        if isinstance(data, dict):
            data = [data]
        self.rows.extend(data)
        return Result(len(data))

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
        return {"run_id": "TESTRUNREL"}


def run_relay(payload, relay_mode="refuse", relay_reason="the relay refused this token",
              direct=None, script=None):
    """One run with the relay configured; records the timeout each road was given."""
    calls = {"relay": [], "direct": []}
    stand = Stand(payload)
    m.Actor = stand
    m.yt.Relay = lambda url, token: Relay(relay_mode, relay_reason, calls)
    m.yt.fetch = lambda client, vid, timeout=None: (
        calls["direct"].append(timeout), direct(client, vid))[1]

    def direct_stub(client, vid):
        return None, "YouTube answered a bot check instead of the video (Sign in to confirm you're not a bot)"

    if direct is not None:
        m.yt.fetch = lambda client, vid, timeout=None: (
            calls["direct"].append(timeout), direct(client, vid))[1]
    else:
        m.yt.fetch = lambda client, vid, timeout=None: (
            calls["direct"].append(timeout), direct_stub(client, vid))[1]

    if script is None:
        m.time_left = ORIGINAL_TIME_LEFT
        m.platform_deadline = ORIGINAL_DEADLINE
    else:
        values = list(script)

        def fake_time_left(_deadline):
            return values.pop(0) if len(values) > 1 else values[0]

        m.time_left = fake_time_left
        m.platform_deadline = lambda: 1e12

    os.environ["YT_RELAY_URL"] = "https://relay.test/ytrelay"
    os.environ["YT_RELAY_TOKEN"] = "secret"
    os.environ.pop("YT_RELAY_REQUIRED", None)
    try:
        asyncio.run(m.main())
    except Exception as exc:  # noqa: BLE001
        stand.crashed = f"{type(exc).__name__}: {exc}"
    finally:
        for key in ("YT_RELAY_URL", "YT_RELAY_TOKEN", "YT_RELAY_REQUIRED"):
            os.environ.pop(key, None)
        m.time_left = ORIGINAL_TIME_LEFT
        m.platform_deadline = ORIGINAL_DEADLINE
        ytm.Relay = ORIGINAL_RELAY
    stand.calls = calls
    return stand


class Relay:
    def __init__(self, mode, reason, calls):
        self.mode, self.reason, self.calls = mode, reason, calls

    def fetch(self, client, vid, timeout=None):
        self.calls["relay"].append(timeout)
        if self.mode == "ok":
            return dict(PAYLOAD, videoDetails=dict(PAYLOAD["videoDetails"], videoId=vid)), "", ""
        if self.mode == "video":
            # The relay answered, and its answer is a verdict about the video, not
            # about the relay: there is nothing to fall back from.
            return None, self.reason, "video"
        return None, self.reason, "relay"


ORIGINAL_TIME_LEFT = m.time_left
ORIGINAL_DEADLINE = m.platform_deadline
ORIGINAL_RELAY = ytm.Relay


class Answer:
    def __init__(self, code, headers=None, body=None):
        self.status_code = code
        self.headers = headers or {}
        self._body = body or {}

    def json(self):
        return self._body


class Client:
    """Two answers in a row: the first is a busy relay, the second the video."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.posts = 0

    def post(self, *a, **k):
        self.posts += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def run_all() -> int:
    # 1. the two roads do not share one budget (B1)
    s = run_relay({"videos": ["jNQXAC9IVRw"]}, relay_mode="ok", script=[120.0, 120.0, 90.0])
    check("budget: relay got the first budget", s.calls["relay"] == [100.0])
    check("budget: the direct road was not given the same number",
          s.calls["direct"] == [] or s.calls["direct"][0] != s.calls["relay"][0])

    s = run_relay({"videos": ["jNQXAC9IVRw"]}, relay_mode="refuse", script=[120.0, 120.0, 90.0])
    check("budget: relay asked for 100 s", s.calls["relay"] == [100.0])
    check("budget: the direct road asked for 70 s, not 100",
          s.calls["direct"] == [70.0])

    # 2. too little time left: the direct road is not called at all
    s = run_relay({"videos": ["jNQXAC9IVRw"]}, relay_mode="refuse", script=[120.0, 120.0, 4.0])
    check("deadline: the closed road was not called", s.calls["direct"] == [])
    check("deadline: the row says the relay failed and time ran out",
          any("the relay failed" in str(r.get("reason") or "") for r in s.rows))

    # 3. the relay reason survives the fallback (B2)
    s = run_relay({"videos": ["jNQXAC9IVRw"]}, relay_mode="refuse")
    reasons = [str(r.get("reason") or "") for r in s.rows if r.get("status") != "OK"]
    check("reason: the relay answer is named in the row",
          any("the relay first answered: the relay refused this token" in x for x in reasons))
    check("reason: the video diagnosis is still there",
          any("bot check" in x for x in reasons))

    # 4. a road that answered nothing is a failure, not a green zero (B3)
    s = run_relay({"videos": ["jNQXAC9IVRw"]}, relay_mode="refuse")
    check("closed road: the run fails", bool(s.failed))
    check("closed road: the failure names the attempts",
          "1 attempt(s)" in str(s.failed or ""))
    check("closed road: the failure names the relay refusals",
          "1 relay refusal(s)" in str(s.failed or ""))
    check("closed road: the failure says nothing was charged",
          "nothing was charged" in str(s.failed or "").lower())
    check("closed road: the first reason is quoted",
          "bot check" in str(s.failed or ""))

    # 5. one repeat on 429, and the video is not pushed onto the closed road (B3)
    client = Client([Answer(429, {"Retry-After": "0.2"}), Answer(200, {}, PAYLOAD)])
    payload, reason, kind = ytm.Relay("https://relay.test", "tok").fetch(client, "jNQXAC9IVRw")
    check("429: the relay was asked twice", client.posts == 2)
    check("429: the second answer is used", payload is not None and reason == "")

    client = Client([Answer(429, {"Retry-After": "0.2"})])
    payload, reason, kind = ytm.Relay("https://relay.test", "tok").fetch(client, "jNQXAC9IVRw")
    check("429 twice: still a relay error, not a verdict", payload is None and kind == "relay")
    check("429 twice: the reason names the code and the wait", "HTTP 429" in reason)

    # 6. a list of deleted videos is a normal green run (B4, a regression of this build)
    gone = "YouTube has no video with this id: it was removed or never existed"
    s = run_relay({"videos": ["aaaaaaaaaaa"]}, relay_mode="video", relay_reason=gone)
    check("B4: a deleted video gives one free NOT_FOUND row",
          [r.get("status") for r in s.rows] == ["NOT_FOUND"])
    check("B4: a deleted video does not fail the run", not s.failed)
    check("B4: nothing is charged for a deleted video", "0 charged" in str(s.status or ""))

    s = run_relay({"videos": ["aaaaaaaaaaa", "not a link"]}, relay_mode="video", relay_reason=gone)
    check("B4: a deleted video plus garbage is still a green run", not s.failed)
    check("B4: the garbage row is free INVALID_INPUT",
          any(r.get("status") == "INVALID_INPUT" for r in s.rows))

    s = run_relay({"videos": ["aaaaaaaaaaa"]}, relay_mode="refuse")
    check("B4: a dead relay plus a refused video still fails", bool(s.failed))
    check("B4: that failure sends the buyer to the relay token",
          "relay token" in str(s.failed or ""))

    # 7. the deadline branch records the first reason (M1)
    s = run_relay({"videos": ["jNQXAC9IVRw"]}, relay_mode="refuse", script=[120.0, 120.0, 4.0])
    check("M1: the deadline failure names the relay reason, not 'not recorded'",
          bool(s.failed) and "not recorded" not in str(s.failed)
          and "refused this token" in str(s.failed))

    # 8. a local run bounds the relay call too (M4)
    s = run_relay({"videos": ["jNQXAC4IVRw"]}, relay_mode="ok")
    check("M4: without a platform deadline the relay still gets a real timeout",
          s.calls["relay"] == [35.0])

    # 9. our switch is declared in the actor, not left to the buyer (M3)
    here = Path(__file__).resolve().parents[1]
    card = _json.loads((here / ".actor" / "actor.json").read_text(encoding="utf-8"))
    check("M3: YT_RELAY_REQUIRED is declared in actor.json",
          "YT_RELAY_REQUIRED" in card.get("environmentVariables", {}))
    readme = (here / "README.md").read_text(encoding="utf-8")
    check("M3: the README does not ask the buyer to set it",
          "Set `YT_RELAY_REQUIRED=1` in the" not in readme)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    code = run_all()
    for name in FAILED:
        print("FAIL:", name)
    for stand in []:
        pass
    print(f"{PASSED} passed, {len(FAILED)} failed")
    sys.exit(code)
