"""Money path and refusals for the text to speech actor.

Run: python tests/test_main.py

The Actor here is a fake that repeats the real SDK on purpose:
  * the paid push is `Actor.push_data(data, *, charged_event_name=...)` and it answers
    with a real `ChargeResult` imported from apify._charging;
  * `Dataset.push_data(data)` takes no event name at all;
  * `get_pricing_info()` and `is_event_charge_limit_reached()` are synchronous;
  * `get_metadata()` returns an object with `item_count`, not a dict;
  * `open_key_value_store(name=...)` and `set_value(key, value, content_type=None)`
    are coroutines with the real keyword names.
A fake that invents its own shapes hides the defects it is meant to catch.

The speech engine is a stub: the tests are about money and refusals, never about the
quality of a voice.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apify._charging import ChargeResult  # noqa: E402  the real shape, not ours

import src.main as app  # noqa: E402
from src import speech as real_speech  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


class FakeDataset:
    def __init__(self, refuse_free: bool = False):
        self.rows: list[dict] = []
        self.refuse_free = refuse_free
        self.count = 0

    async def push_data(self, row):
        if self.refuse_free:
            raise RuntimeError("dataset is closed")
        self.rows.append(row)
        self.count += 1

    async def get_metadata(self):
        return SimpleNamespace(item_count=self.count)


class FakeStore:
    def __init__(self, fail_on: str | None = None):
        self.values: dict[str, bytes] = {}
        self.types: dict[str, str | None] = {}
        self.fail_on = fail_on

    async def set_value(self, key, value, content_type=None):
        if self.fail_on and self.fail_on in key:
            raise RuntimeError("store is closed")
        if isinstance(value, (bytes, bytearray)):
            self.values[key] = bytes(value)
        else:
            self.values[key] = value
        self.types[key] = content_type


class FakeCharging:
    def __init__(self, can_charge=True, reason="clip-synthesized is priced", cap_at=None):
        self.can_charge = can_charge
        self.reason = reason
        self.cap_at = cap_at
        self.charged_events = 0

    def get_pricing_info(self):
        if self.can_charge is None:
            return None
        return SimpleNamespace(
            is_pay_per_event=bool(self.can_charge),
            per_event_prices={app.EVENT_NAME: 0.002} if self.can_charge else {},
        )

    def is_event_charge_limit_reached(self, event_name):
        return self.cap_at is not None and self.charged_events >= self.cap_at


class FakeActor:
    def __init__(self, input_data, *, can_charge=True, reason="clip-synthesized is priced",
                 cap_at=None, charged=1, raiser=None, landed=False, refuse_free=False,
                 store_fail=None):
        self.input_data = input_data
        self.dataset = FakeDataset(refuse_free=refuse_free)
        self.stores: dict[str | None, FakeStore] = {}
        self.default_store = FakeStore(fail_on=store_fail)
        self.charging = FakeCharging(can_charge, reason, cap_at)
        self.charged = charged
        self.raiser = raiser
        self.landed = landed
        self.pushes: list[dict] = []
        self.failed: str | None = None
        self.status: str | None = None

    async def init(self):
        return None

    @staticmethod
    def get_env():
        return {"run_id": "test-run"}

    async def open_dataset(self):
        return self.dataset

    async def open_key_value_store(self, *, id=None, alias=None, name=None, force_cloud=False):
        if name is None:
            return self.default_store
        return self.stores.setdefault(name, FakeStore())

    async def get_input(self):
        return self.input_data

    def get_charging_manager(self):
        return self.charging

    async def push_data(self, data, *, charged_event_name=None):
        self.pushes.append({"data": dict(data), "event": charged_event_name})
        if self.raiser is not None:
            if self.landed:
                await self.dataset.push_data(data)
            raise self.raiser
        if self.charged <= 0:
            return ChargeResult(event_charge_limit_reached=True, charged_count=0,
                                chargeable_within_limit={})
        self.charging.charged_events += 1
        await self.dataset.push_data(data)
        return ChargeResult(event_charge_limit_reached=False, charged_count=self.charged,
                            chargeable_within_limit={})

    async def set_status_message(self, text, is_terminal=None):
        self.status = text

    async def fail(self, *, status_message=None, exit_code=None, exception=None):
        self.failed = status_message


class FakeSpeech:
    """Same names the pipeline calls, none of the engine."""

    DEFAULT_VOICE = "en-us-lessac"
    FIRST_CLIP_EXTRA_S = 20.0
    SpeechError = real_speech.SpeechError
    mp3_ok = True
    voices = ["en-us-lessac", "ru-ru-irina"]
    calls: list[str] = []
    error: Exception | None = None

    prepare_text = staticmethod(real_speech.prepare_text)

    @classmethod
    def available_voices(cls):
        return list(cls.voices)

    @classmethod
    def mp3_available(cls):
        return cls.mp3_ok

    @classmethod
    def estimate_seconds(cls, chars, speed):
        return 1.0

    @classmethod
    def synthesize(cls, text, voice="en-us-lessac", speed=1.0, fmt="wav"):
        cls.calls.append(text)
        if cls.error is not None:
            raise cls.error
        return {
            "data": b"RIFF0000WAVEdata" + b"\x00" * 8,
            "format": fmt,
            "contentType": "audio/mpeg" if fmt == "mp3" else "audio/wav",
            "sampleRate": 22050,
            "channels": 1,
            "seconds": 1.25,
        }


def run_main(input_data, **knobs):
    FakeSpeech.calls = []
    FakeSpeech.error = knobs.pop("speech_error", None)
    FakeSpeech.voices = knobs.pop("voices", ["en-us-lessac", "ru-ru-irina"])
    FakeSpeech.mp3_ok = knobs.pop("mp3_ok", True)
    actor = FakeActor(input_data, **knobs)
    started = app.Actor, app.speech
    app.Actor, app.speech = actor, FakeSpeech
    try:
        asyncio.run(app.main())
    finally:
        app.Actor, app.speech = started
    store = actor.stores.get(app.SUMMARY_STORE)
    summary = store.values.get("RUN_SUMMARY-test-run") if store else None
    import json
    if isinstance(summary, (bytes, bytearray)):
        summary = json.loads(summary)
    return summary, actor


def test_two_texts_two_charges():
    summary, actor = run_main({"texts": ["hello there", "second line"]})
    equal(summary["clipsDelivered"], 2, "two clips delivered")
    equal(summary["chargedEvents"], 2, "two events charged")
    equal(summary["rowsWritten"], 2, "two rows written")
    equal(len(actor.dataset.rows), 2, "and the dataset holds them")
    equal([p["event"] for p in actor.pushes], [app.EVENT_NAME] * 2,
          "every paid row goes through the one SDK door with the event name")
    ok(actor.dataset.rows[0]["audioKey"].startswith("audio_000-"), "the first key is numbered")
    ok("/" not in actor.dataset.rows[0]["audioKey"], "no key carries a slash")
    equal(summary["requested"], 2, "requested counts the lines")
    equal(summary["textsAttempted"], 2, "every line is a text")
    equal(summary["textsProcessed"] + summary["notCheckedTexts"], 2, "counters close")
    equal(summary["billingActive"], True, "billing is on")
    ok("textMismatch" not in summary["byStatus"], "no mismatch on a clean run")
    equal(actor.failed, None, "a clean run is not a failure")
    equal(actor.stores[app.SUMMARY_STORE].values.get("RUN_SUMMARY-test-run") is not None, True,
          "the summary is stored under the run key")
    equal(actor.default_store.values.get("clips.zip") is not None, True, "a zip of the clips exists")


def test_refusals_are_free_and_do_not_reach_the_engine():
    summary, actor = run_main({"texts": ["same", " same ", "", "x" * 40, "other"],
                               "maxClips": 1, "maxCharactersPerLine": 20})
    statuses = [row["status"] for row in actor.dataset.rows]
    equal(statuses.count(app.ST_BLANK), 1, "the blank line is refused")
    equal(statuses.count(app.ST_DUPLICATE), 1, "the repeat is refused")
    equal(statuses.count(app.ST_TOO_LONG), 1, "the long line is refused")
    equal(statuses.count(app.ST_LIMIT), 1, "the line past maxClips is refused")
    equal(summary["clipsDelivered"], 1, "only one clip is made")
    equal(summary["chargedEvents"], 1, "and only one is charged")
    equal(FakeSpeech.calls, ["same"], "the engine is asked exactly once")
    equal(summary["linesThatAreNotTexts"], 4, "refused lines have their own bucket")
    ok("lineCountMismatch" not in summary["byStatus"], "the line count still closes")


def test_spending_cap_stops_before_the_next_clip():
    summary, actor = run_main({"texts": ["one", "two", "three"]}, cap_at=1)
    equal(summary["stopReason"], "spending-cap", "the run stops on the cap")
    equal(summary["clipsDelivered"], 1, "only the clip before the cap is delivered")
    equal(summary["chargedEvents"], 1, "one charge")
    equal(summary["textsProcessed"] + summary["notCheckedTexts"], 3, "counters still close")
    equal([r["status"] for r in actor.dataset.rows].count(app.ST_CAP), 2, "the rest are free CAP rows")


def test_unreadable_price_makes_nothing_and_charges_nothing():
    summary, actor = run_main({"texts": ["one", "two"]}, can_charge=None)
    equal(FakeSpeech.calls, [], "the engine is never asked when the price is unknown")
    equal(summary["clipsDelivered"], 0, "nothing is delivered")
    equal(summary["chargedEvents"], 0, "nothing is charged")
    equal([r["status"] for r in actor.dataset.rows], [app.ST_BILLING] * 2,
          "every line says the price could not be read")
    equal(actor.failed is not None, True, "a run that delivered nothing says why")


def test_not_pay_per_event_delivers_free():
    summary, actor = run_main({"texts": ["one"]}, can_charge=False)
    equal(summary["clipsDelivered"], 1, "the clip is still made")
    equal(summary["chargedEvents"], 0, "but nothing is charged")
    equal(actor.dataset.rows[0]["billable"], False, "the row does not claim money")
    equal(summary["billingActive"], False, "the summary says billing is off")
    equal(actor.pushes, [], "the paid door is not used at all")


def test_charge_failure_with_a_landed_row_is_delivered_uncharged():
    summary, actor = run_main({"texts": ["one"]}, raiser=RuntimeError("charge blew up"),
                              landed=True)
    equal(summary["deliveredUncharged"], 1, "a landed row is counted as delivered, unpaid")
    equal(summary["deliveredUnconfirmed"], 0, "and not as missing")
    equal(summary["stopReason"], "charge-error", "the run stops after the first one")
    equal(summary["chargedEvents"], 0, "no money")


def test_charge_failure_without_a_row_is_not_delivered():
    summary, actor = run_main({"texts": ["one"]}, raiser=RuntimeError("charge blew up"),
                              landed=False)
    equal(summary["deliveredUnconfirmed"], 1, "a row the counter did not see is not delivered")
    equal(summary["deliveredUncharged"], 0, "and it is not called delivered either")
    equal(summary["clipsDelivered"], 0, "no clip is claimed")


def test_store_failure_is_free_and_named():
    summary, actor = run_main({"texts": ["one"]}, store_fail="audio_")
    equal([r["status"] for r in actor.dataset.rows], [app.ST_STORE], "the row says the store refused")
    equal(summary["storeErrors"], 1, "and the summary counts it")
    equal(summary["chargedEvents"], 0, "nothing is charged for a file nobody can download")
    equal(actor.pushes, [], "the paid door is not used")


def test_engine_error_is_free_and_does_not_kill_the_run():
    summary, actor = run_main({"texts": ["one", "two"]},
                              speech_error=real_speech.SpeechError("the engine failed"))
    equal(summary["synthesisErrors"], 2, "both texts are counted as engine errors")
    equal(summary["clipsDelivered"], 0, "nothing is delivered")
    equal(summary["chargedEvents"], 0, "nothing is charged")
    equal(actor.failed is not None, True, "the run says it delivered nothing")


def test_deadline_closes_the_run_before_the_engine():
    soon = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    os.environ["ACTOR_TIMEOUT_AT"] = soon
    try:
        summary, actor = run_main({"texts": ["one", "two"]})
    finally:
        os.environ.pop("ACTOR_TIMEOUT_AT", None)
    equal(FakeSpeech.calls, [], "no text is started when the deadline is close")
    equal(summary["stopReason"], "deadline", "the run says the deadline stopped it")
    equal([r["status"] for r in actor.dataset.rows], [app.ST_TIME] * 2, "both lines are free TIME rows")
    equal(summary["deadlineCapped"], True, "and the summary flags the cap")


def test_empty_input_fails_with_a_plain_reason():
    summary, actor = run_main({"texts": []})
    equal(summary["requested"], 0, "nothing was asked")
    ok(actor.failed and "No text was sent" in actor.failed, f"the reason is plain: {actor.failed}")


def test_voice_missing_from_the_image_is_refused_before_anything():
    summary, actor = run_main({"texts": ["one"], "voice": "ru-ru-irina"},
                              voices=["en-us-lessac"])
    ok(actor.failed and "ru-ru-irina" in actor.failed, f"the reason names the voice: {actor.failed}")
    equal(FakeSpeech.calls, [], "nothing is synthesised")


def test_mp3_without_an_encoder_is_refused():
    summary, actor = run_main({"texts": ["one"], "format": "mp3"}, mp3_ok=False)
    ok(actor.failed and "mp3" in actor.failed, f"the reason names mp3: {actor.failed}")
    equal(FakeSpeech.calls, [], "nothing is synthesised")


def test_unexpected_error_still_writes_the_tail_and_the_summary():
    summary, actor = run_main({"texts": ["one", "two", "three"]}, refuse_free=False,
                              raiser=TypeError("boom"))
    ok(summary is not None, "the summary is written even on a crash path")
    equal(summary["requested"], 3, "all three lines are accounted for")
    equal(summary["textsProcessed"] + summary["notCheckedTexts"], 3, "counters close after the stop")
    equal(summary["stopReason"], "charge-error", "the first text stops the run honestly")


def main() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print(f"{CHECKS[0]} checks, {len(FAILS)} failed")
    for line in FAILS:
        print("FAIL:", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
