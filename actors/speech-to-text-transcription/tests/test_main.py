"""Flow checks for src/main.py with a fake Actor that follows apify 4.2.

Run: python tests/test_main.py   (exit code 0 = every check passed)

The fake repeats the real SDK shapes on purpose: push_data is a coroutine and returns
an object with charged_count / event_charge_limit_reached, the row is written BEFORE
the charge (so a failed charge leaves a row behind), open_dataset and get_metadata are
coroutines, get_env carries the key run_id and get_charging_manager is synchronous.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import audio as audio_mod  # noqa: E402
from src import main as actor_main  # noqa: E402
from src import speech  # noqa: E402
from src import statuses as st  # noqa: E402

CHECKS = 0
FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILS.append(f"{name}{(' -> ' + detail) if detail else ''}")


def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"got {got!r}, expected {expected!r}")


class FakeResult:
    def __init__(self, charged_count: int, limit: bool = False) -> None:
        self.charged_count = charged_count
        self.event_charge_limit_reached = limit


class FakeMeta:
    def __init__(self, count: int) -> None:
        self.item_count = count


class FakeDataset:
    def __init__(self, actor) -> None:
        self.actor = actor

    async def get_metadata(self):
        self.actor.meta_calls += 1
        if self.actor.meta_error:
            raise RuntimeError("metadata is not readable in this run")
        return FakeMeta(len(self.actor.rows))


class FakeStore:
    def __init__(self) -> None:
        self.values: dict = {}

    async def set_value(self, key, value) -> None:
        self.values[key] = value


class FakePricing:
    def __init__(self, is_ppe=True, prices=None) -> None:
        self.is_pay_per_event = is_ppe
        self.per_event_prices = prices if prices is not None else {actor_main.CHARGE_EVENT: 0.01}


class FakeChargingManager:
    def __init__(self, pricing) -> None:
        self._pricing = pricing

    def get_pricing_info(self):
        return self._pricing


class ActorExit(Exception):
    pass


class FakeActor:
    last: "FakeActor | None" = None

    def __init__(self, inp, pricing="ok", charge="ok", cap_at=None, write_error=False,
                 meta_error=False) -> None:
        self.inp = inp
        self.pricing = pricing
        self.charge = charge
        self.cap_at = cap_at
        self.write_error = write_error
        self.meta_error = meta_error
        self.meta_calls = 0
        self.rows: list = []
        self.charged: list = []
        self.status_message = None
        self.failed_with = None
        self.store = FakeStore()
        self.log = types.SimpleNamespace(info=lambda *a, **k: None,
                                         error=lambda *a, **k: None)
        FakeActor.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_input(self):
        return self.inp

    def get_charging_manager(self):
        if self.pricing == "missing":
            raise RuntimeError("no charging manager in a local run")
        if self.pricing == "ok":
            info = FakePricing()
        elif self.pricing == "flat":
            info = FakePricing(is_ppe=False, prices={})
        else:
            info = FakePricing(prices={"other-event": 0.01})
        return FakeChargingManager(info)

    async def open_dataset(self):
        return FakeDataset(self)

    async def open_key_value_store(self):
        return FakeStoreProxy(self.store)

    async def push_data(self, row, charged_event_name=None):
        if self.write_error:
            raise RuntimeError("dataset write refused")
        if charged_event_name:
            if self.charge == "error":
                # the SDK writes the row and then charges: a refused charge leaves the row
                self.rows.append(dict(row))
                raise RuntimeError("charge refused")
            if self.charge == "error_no_write":
                # the write itself failed: nothing reached the dataset
                raise RuntimeError("charge refused before the row was stored")
            if self.charge == "cap" or (self.cap_at is not None
                                        and len(self.charged) >= self.cap_at):
                return FakeResult(0, True)
            self.rows.append(dict(row))
            self.charged.append(row.get("url"))
            return FakeResult(1)
        self.rows.append(dict(row))
        return FakeResult(0)

    async def set_status_message(self, message):
        self.status_message = message

    async def fail(self, *, status_message=None, **kwargs):
        self.failed_with = status_message
        raise ActorExit(status_message)

    @staticmethod
    def get_env():
        return {"run_id": "testRun123"}


class FakeStoreProxy:
    def __init__(self, store) -> None:
        self.store = store

    async def set_value(self, key, value) -> None:
        await self.store.set_value(key, value)


class Downloaded:
    def __init__(self, url: str, data: bytes = b"RIFF....", content_type="audio/mpeg",
                 size=None) -> None:
        self.url = url
        self.final_url = url
        self.file_name = url.rsplit("/", 1)[-1]
        self.data = data
        self.content_type = content_type
        self.bytes_read = size if size is not None else len(data)


FILES = {
    "https://files.example/one.mp3": 60.0,
    "https://files.example/two.wav": 120.0,
    "https://files.example/three.m4a": 30.0,
    "https://files.example/long.mp3": 1200.0,
}
DEFAULT_DURATION = 60.0


def install_fakes(files=None, errors=None, transcribe_error=False) -> dict:
    """Patch the modules main() talks to; returns the record of calls."""
    files = FILES if files is None else files
    errors = errors or {}
    calls = {"downloaded": [], "transcribed": []}
    originals = {
        "download": audio_mod.download,
        "probe": audio_mod.probe_duration,
        "decode": audio_mod.decode,
        "load_model": speech.load_model,
        "transcribe": speech.transcribe,
    }

    async def fake_download(client, url, max_bytes):
        calls["downloaded"].append(url)
        if url in errors:
            raise errors[url]
        payload = url.encode()
        return Downloaded(url, data=payload, size=len(payload))

    def fake_probe(data):
        return duration_lookup.get(bytes(data).decode("utf-8", "ignore"))

    def fake_decode(data):
        seconds = duration_lookup.get(bytes(data).decode("utf-8", "ignore"), DEFAULT_DURATION)
        return np.zeros(int(seconds * audio_mod.SAMPLE_RATE), dtype=np.float32)

    def fake_load_model(name, threads):
        return object()

    def fake_transcribe(model, samples, language, vad_filter):
        if transcribe_error:
            raise RuntimeError("the model exploded")
        seconds = samples.size / audio_mod.SAMPLE_RATE
        calls["transcribed"].append(seconds)
        return {"text": "hello world from the model",
                "segments": [{"start": 0.0, "end": 1.0, "text": "hello world from the model"}],
                "language": language or "en",
                "languageProbability": 0.99,
                "duration": seconds,
                "durationAfterVad": seconds}

    audio_mod.download = fake_download
    audio_mod.probe_duration = fake_probe
    audio_mod.decode = fake_decode
    speech.load_model = fake_load_model
    speech.transcribe = fake_transcribe
    calls["restore"] = originals
    return calls


duration_lookup: dict = {}


def restore(calls) -> None:
    audio_mod.download = calls["restore"]["download"]
    audio_mod.probe_duration = calls["restore"]["probe"]
    audio_mod.decode = calls["restore"]["decode"]
    speech.load_model = calls["restore"]["load_model"]
    speech.transcribe = calls["restore"]["transcribe"]


def run(inp, files=None, errors=None, transcribe_error=False, **actor_kwargs):
    """Run main() against one FakeActor. Returns (actor, exit error)."""
    files = FILES if files is None else files
    # the bytes of a fake file carry its own URL, so the duration follows the file
    duration_lookup.clear()
    duration_lookup.update(files)
    calls = install_fakes(files, errors, transcribe_error)
    actor = FakeActor(inp, **actor_kwargs)
    original_actor = actor_main.Actor
    actor_main.Actor = actor
    error = None
    try:
        asyncio.run(actor_main.main())
    except ActorExit as exc:
        error = exc
    finally:
        actor_main.Actor = original_actor
        restore(calls)
    return actor, error


def summary_of(actor) -> dict:
    return actor.store.values.get("RUN_SUMMARY", {})


def rows(actor) -> list:
    return actor.rows


def transcribed_rows(actor) -> list:
    return [r for r in actor.rows if r.get("status") == st.TRANSCRIBED]


def run_all() -> int:
    before = CHECKS

    # 1. empty input: a human refusal, nothing charged
    actor, error = run({})
    eq("empty input: refused", error is not None, True)
    check("empty input: reason names the field",
          "audioUrls" in (actor.failed_with or ""), str(actor.failed_with))
    eq("empty input: no rows", len(rows(actor)), 0)
    eq("empty input: stop reason", summary_of(actor).get("stopReason"), "empty-input")

    # 2. lines that are not URLs cost nothing and say why
    actor, error = run({"audioUrls": ["ftp://files.example/a.mp3", "just a word"]})
    eq("invalid: two status rows", len(rows(actor)), 2)
    check("invalid: status", all(r["status"] == st.INVALID_URL for r in rows(actor)),
          str([r["status"] for r in rows(actor)]))
    eq("invalid: counted", summary_of(actor)["filesInvalid"], 2)
    check("invalid: refused overall", error is not None, "no failure raised")
    eq("invalid: nothing charged", summary_of(actor)["chargedEvents"], 0)

    # 3. a private address is refused by the guard and named on the row
    actor, _ = run({"audioUrls": ["https://files.example/private"]},
                   errors={"https://files.example/private": audio_mod.AudioError(
                       st.PRIVATE_TARGET, "the address 127.0.0.1 is not public")})
    eq("private: status", rows(actor)[0]["status"], st.PRIVATE_TARGET)
    check("private: reason kept", "not public" in rows(actor)[0]["reason"], rows(actor)[0])
    eq("private: not charged", summary_of(actor)["chargedEvents"], 0)

    # 4. a refused download, an oversized file and a broken file
    for status, reason in ((st.FETCH_ERROR, "the server answered HTTP 404"),
                           (st.TOO_LARGE, "the file is larger than the limit"),
                           (st.UNSUPPORTED, "the file carries no audio stream")):
        actor, _ = run({"audioUrls": ["https://files.example/x.mp3"]},
                       errors={"https://files.example/x.mp3": audio_mod.AudioError(status, reason)})
        eq(f"{status}: on the row", rows(actor)[0]["status"], status)
        eq(f"{status}: not charged", len(actor.charged), 0)
        check(f"{status}: counted", summary_of(actor)["byStatus"].get(status) == 1,
              str(summary_of(actor)["byStatus"]))

    # 5. a file longer than the limit is named with its real length
    actor, _ = run({"audioUrls": ["https://files.example/long.mp3"], "maxMinutesPerFile": 10})
    eq("too long: status", rows(actor)[0]["status"], st.TOO_LONG)
    check("too long: minutes named", "20.0 minutes" in rows(actor)[0]["reason"],
          rows(actor)[0]["reason"])
    eq("too long: not charged", summary_of(actor)["chargedEvents"], 0)
    eq("too long: whole run refused", error_ok(actor), True)

    # 6. the happy path: two files, two charges, one summary
    actor, error = run({"audioUrls": ["https://files.example/one.mp3",
                                      "https://files.example/two.wav"]})
    eq("happy: no exit error", error, None)
    eq("happy: two rows", len(rows(actor)), 2)
    eq("happy: both transcribed", len(transcribed_rows(actor)), 2)
    eq("happy: both charged", summary_of(actor)["chargedEvents"], 2)
    eq("happy: charged urls", actor.charged, ["https://files.example/one.mp3",
                                              "https://files.example/two.wav"])
    eq("happy: rows delivered", summary_of(actor)["rowsDelivered"], 2)
    eq("happy: audio minutes", summary_of(actor)["audioMinutes"], 3.0)
    eq("happy: status message", actor.status_message is not None, True)
    check("happy: headline names the files", "2 file(s) transcribed" in (actor.status_message or ""),
          str(actor.status_message))
    eq("happy: billable on transcript", rows(actor)[0]["billable"], True)
    eq("happy: word count", rows(actor)[0]["wordCount"], 5)
    eq("happy: language", rows(actor)[0]["language"], "en")
    check("happy: segments by default", isinstance(rows(actor)[0].get("segments"), list),
          str(rows(actor)[0].get("segments"))[:60])
    check("happy: no srt by default", "srt" not in rows(actor)[0], str(rows(actor)[0].keys()))

    # 7. counters add up and the summary is readable by both keys
    sm = summary_of(actor)
    eq("counter: statuses add up", sum(sm["byStatus"].values()), sm["filesAsked"])
    check("counter: no mismatch", "counterMismatch" not in sm, str(sm.get("counterMismatch")))
    check("counter: no money mismatch", "moneyMismatch" not in sm, str(sm.get("moneyMismatch")))
    eq("summary: both keys", sorted(k for k in actor.store.values if "SUMMARY" in k),
       ["RUN_SUMMARY", "RUN_SUMMARY-testRun123"])
    eq("summary: run id", sm["runId"], "testRun123")
    eq("summary: dataset count matches", sm["datasetItemCount"], 2)
    eq("summary: platform count matches", sm["datasetItemCountApi"], 2)
    check("summary: metadata not asked per row", actor.meta_calls <= 2, str(actor.meta_calls))

    # 8. subtitles and segments on request
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3"],
                    "includeSubtitles": True, "includeSegments": False})
    row = rows(actor)[0]
    check("subtitles: srt built", row.get("srt", "").startswith("1\n"), str(row.get("srt"))[:30])
    check("subtitles: vtt built", row.get("vtt", "").startswith("WEBVTT"), str(row.get("vtt"))[:20])
    check("subtitles: segments left out", "segments" not in row, str(row.keys()))

    # 9. the same file twice is transcribed once
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3",
                                  "https://files.example/one.mp3"]})
    eq("duplicate: one charge", summary_of(actor)["chargedEvents"], 1)
    eq("duplicate: second row is a status", rows(actor)[1]["status"], st.DUPLICATE)
    eq("duplicate: counted", summary_of(actor)["duplicateFiles"], 1)

    # 10. the list is cut at maxFiles
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3",
                                  "https://files.example/two.wav"], "maxFiles": 1})
    eq("limit: one charge", summary_of(actor)["chargedEvents"], 1)
    eq("limit: second row status", rows(actor)[1]["status"], st.LIMIT)
    eq("limit: counted", summary_of(actor)["filesCutByLimit"], 1)

    # 11. the spending cap stops the run and names the rest
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3",
                                  "https://files.example/two.wav",
                                  "https://files.example/three.m4a"]}, cap_at=1)
    sm = summary_of(actor)
    eq("cap: one charge", sm["chargedEvents"], 1)
    eq("cap: stop reason", sm["stopReason"], "spending-cap")
    eq("cap: cap rows", sm["filesCap"], 2)
    eq("cap: cap rows written", [r["status"] for r in rows(actor)],
       [st.TRANSCRIBED, st.CAP, st.CAP])
    check("cap: cap row has no text", "text" not in rows(actor)[1], str(rows(actor)[1].keys()))

    # 12. a failed charge whose row did land is not written twice
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3"]}, charge="error")
    sm = summary_of(actor)
    eq("charge error: row delivered free", sm["deliveredUncharged"], 1)
    eq("charge error: transcribed", sm["filesTranscribed"], 1)
    eq("charge error: one row only", len(rows(actor)), 1)
    eq("charge error: one charge error", len(sm["chargeErrors"]), 1)

    # 13. a charge error where nothing landed is named as a write error
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3"]}, charge="error_no_write")
    sm = summary_of(actor)
    eq("charge error, no row: write errors", sm["filesWriteError"], 1)
    eq("charge error, no row: status row", rows(actor)[0]["status"], st.ERROR)
    eq("charge error, no row: not transcribed", sm["filesTranscribed"], 0)

    # 14. an unreadable dataset count leaves the row unverified instead of duplicated
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3"]}, charge="error",
                   meta_error=True)
    sm = summary_of(actor)
    eq("charge unknown: counted", sm["filesChargeUnverified"], 1)
    eq("charge unknown: one row", len(rows(actor)), 1)
    check("charge unknown: status named", sm["byStatus"].get(st.CHARGE_UNVERIFIED) == 1,
          str(sm["byStatus"]))

    # 15. three refused writes stop the run
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3",
                                  "https://files.example/two.wav",
                                  "https://files.example/three.m4a"]}, write_error=True)
    sm = summary_of(actor)
    eq("write error: stop reason", sm["stopReason"], "dataset-write-error")
    eq("write error: counted", sm["datasetWriteErrors"], 3)
    eq("write error: nothing charged", sm["chargedEvents"], 0)

    # 16. a price that cannot be read delivers nothing and charges nothing
    actor, error = run({"audioUrls": ["https://files.example/one.mp3"], "maxFiles": 5},
                       pricing="missing")
    sm = summary_of(actor)
    eq("price unknown: status", rows(actor)[0]["status"], st.BILLING)
    eq("price unknown: nothing charged", sm["chargedEvents"], 0)
    eq("price unknown: nothing transcribed", sm["filesTranscribed"], 0)
    check("price unknown: refused overall", error is not None, "no failure raised")
    check("price unknown: reason in the message", "price" in (actor.failed_with or ""),
          str(actor.failed_with))

    # 17. a run that is not priced per event delivers free rows and says so
    for mode, fragment in (("flat", "per event"), ("noevent", "audio-transcribed")):
        actor, error = run({"audioUrls": ["https://files.example/one.mp3"]}, pricing=mode)
        sm = summary_of(actor)
        eq(f"{mode}: no charges", sm["chargedEvents"], 0)
        eq(f"{mode}: billing inactive", sm["billingActive"], False)
        check(f"{mode}: reason names it", fragment in str(sm["billingReason"]), str(sm["billingReason"]))
        eq(f"{mode}: row delivered", len(transcribed_rows(actor)), 1)
        eq(f"{mode}: flagged on the row", rows(actor)[0].get("billingInactive"), True)
        eq(f"{mode}: billable is false, not a promise", rows(actor)[0].get("billable"), False)
        eq(f"{mode}: no exit error", error, None)

    # 18. the platform clock cuts the run and the rest say so
    original_timeout = os.environ.get("ACTOR_TIMEOUT_AT")
    os.environ["ACTOR_TIMEOUT_AT"] = (datetime.now(timezone.utc) + timedelta(seconds=5)) \
        .isoformat().replace("+00:00", "Z")
    try:
        actor, _ = run({"audioUrls": ["https://files.example/one.mp3",
                                      "https://files.example/two.wav"]})
        sm = summary_of(actor)
        eq("deadline: stop reason", sm["stopReason"], "deadline")
        eq("deadline: flagged", sm["deadlineCapped"], True)
        eq("deadline: both rows are time", [r["status"] for r in rows(actor)],
           [st.TIME, st.TIME])
        eq("deadline: nothing charged", sm["chargedEvents"], 0)
    finally:
        if original_timeout is None:
            os.environ.pop("ACTOR_TIMEOUT_AT", None)
        else:
            os.environ["ACTOR_TIMEOUT_AT"] = original_timeout

    # 18b. a short file must fit the platform QA run, which gets five minutes
    os.environ["ACTOR_TIMEOUT_AT"] = (datetime.now(timezone.utc) + timedelta(seconds=300)) \
        .isoformat().replace("+00:00", "Z")
    try:
        actor, _ = run({"audioUrls": ["https://files.example/short-a.mp3",
                                      "https://files.example/short-b.mp3"]},
                       files={"https://files.example/short-a.mp3": 11.0,
                              "https://files.example/short-b.mp3": 11.0})
        sm = summary_of(actor)
        eq("qa run: both transcribed", sm["filesTranscribed"], 2)
        eq("qa run: nothing cut by the clock", sm["deadlineCapped"], False)
        eq("qa run: no time rows", sm["byStatus"].get(st.TIME), None)
    finally:
        os.environ.pop("ACTOR_TIMEOUT_AT", None)

    # 19. a model that is not offered and a language that is not a code
    actor, error = run({"audioUrls": ["https://files.example/one.mp3"], "model": "large"})
    check("bad model: refused", error is not None and "not offered" in (actor.failed_with or ""),
          str(actor.failed_with))
    eq("bad model: no rows", len(rows(actor)), 0)
    actor, error = run({"audioUrls": ["https://files.example/one.mp3"], "language": "english"})
    check("bad language: refused", error is not None and "letter code" in (actor.failed_with or ""),
          str(actor.failed_with))
    eq("bad language: no rows", len(rows(actor)), 0)

    # 20. a model that raises is a status row, and three in a row stop the run
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3"]}, transcribe_error=True)
    eq("model error: status", rows(actor)[0]["status"], st.ERROR)
    check("model error: reason kept", "the model exploded" in rows(actor)[0]["reason"],
          rows(actor)[0]["reason"])
    eq("model error: counted", summary_of(actor)["filesModelError"], 1)
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3",
                                  "https://files.example/two.wav",
                                  "https://files.example/three.m4a"]}, transcribe_error=True)
    eq("model error: run stopped", summary_of(actor)["stopReason"], "transcription-error")

    # 21a. one junk line is a status row, the valid file behind it is still delivered
    actor, _ = run({"audioUrls": ["https://files.example/junk.mp3",
                                  "https://files.example/one.mp3"]},
                   errors={"https://files.example/junk.mp3":
                           UnicodeError("label empty or too long")})
    sm = summary_of(actor)
    eq("junk line: its own row", rows(actor)[0]["status"], st.FETCH_ERROR)
    check("junk line: reason kept", "label empty or too long" in rows(actor)[0]["reason"],
          rows(actor)[0]["reason"])
    eq("junk line: the valid file is delivered", rows(actor)[1]["status"], st.TRANSCRIBED)
    eq("junk line: one charge, not zero", sm["chargedEvents"], 1)
    eq("junk line: nothing lost", len(rows(actor)), 2)

    # 18c. a file the clock cannot fit is refused before the model starts
    os.environ["ACTOR_TIMEOUT_AT"] = (datetime.now(timezone.utc) + timedelta(seconds=300)) \
        .isoformat().replace("+00:00", "Z")
    try:
        actor, _ = run({"audioUrls": ["https://files.example/long200.mp3"]},
                       files={"https://files.example/long200.mp3": 200.0})
        sm = summary_of(actor)
        eq("deadline plan: out of time row", rows(actor)[0]["status"], st.TIME)
        check("deadline plan: reason says out of time",
              "out of time" in (rows(actor)[0]["reason"] or ""), str(rows(actor)[0].get("reason")))
        eq("deadline plan: nothing charged", sm["chargedEvents"], 0)
        eq("deadline plan: stop reason", sm["stopReason"], "deadline")
        eq("deadline plan: no transcript row", len(transcribed_rows(actor)), 0)
    finally:
        os.environ.pop("ACTOR_TIMEOUT_AT", None)

    # 22. after a charge stop the rest name that stop, not "out of time"
    files4 = {f"https://files.example/c{n}.mp3": 10.0 for n in (1, 2, 3, 4)}
    actor, _ = run({"audioUrls": list(files4)}, files=files4, charge="error")
    sm = summary_of(actor)
    eq("charge stop: stop reason", sm["stopReason"], "charge-error")
    eq("charge stop: the rest is an error row", rows(actor)[-1]["status"], st.ERROR)
    check("charge stop: the reason names the stop", "charge-error" in rows(actor)[-1]["reason"],
          rows(actor)[-1]["reason"])
    eq("charge stop: every line has a row", len(rows(actor)), 4)

    # 23. the plan learns from long files only, and never from a lucky fast one
    eq("plan: a five second file teaches nothing", actor_main.learn_rtf(None, 2.0, 5.0), None)
    eq("plan: a twenty-five second file does",
       round(actor_main.learn_rtf(None, 2.0, 25.0), 3), 0.08)
    eq("plan: the slowest file wins", actor_main.learn_rtf(0.4, 0.05, 25.0), 0.4)
    eq("plan: a fast file does not lower it", actor_main.learn_rtf(0.4, 0.05, 200.0), 0.4)

    # 21. settings reach the summary, so a report cannot invent them
    actor, _ = run({"audioUrls": ["https://files.example/one.mp3"], "model": "base",
                    "maxMinutesPerFile": 7, "maxFileMb": 42, "maxFiles": 3,
                    "vadFilter": False, "language": "DE"})
    sm = summary_of(actor)
    eq("settings: model", sm["model"], "base")
    eq("settings: minutes", sm["maxMinutesPerFile"], 7)
    eq("settings: megabytes", sm["maxFileMb"], 42)
    eq("settings: files", sm["maxFiles"], 3)
    eq("settings: vad", sm["vadFilter"], False)
    eq("settings: language lowercased", sm["languageRequested"], "de")

    executed = CHECKS - before
    print(f"test_main: {executed} checks, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL:", fail)
    if executed < 60:
        print("  FAIL: too few checks ran")
        return 1
    return 0 if not FAILS else 1


def error_ok(actor) -> bool:
    """True when the run refused overall (nothing could be transcribed)."""
    return actor.failed_with is not None


if __name__ == "__main__":
    sys.exit(run_all())
