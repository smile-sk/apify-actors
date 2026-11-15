"""The parts of the speech engine that do not need the engine itself.

Run: python tests/test_speech.py
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.main as app  # noqa: E402
from src import speech  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def test_prepare_text_collapses_whitespace():
    equal(speech.prepare_text("  Hello   world \n"), "Hello world", "whitespace is collapsed")
    equal(speech.prepare_text(None), "", "none is empty text")
    equal(speech.prepare_text(123), "123", "numbers are text too")


def test_store_key_matches_the_platform_alphabet():
    equal(app.store_key("audio/000/x.wav"), "audio_000_x.wav", "a slash never reaches the platform")
    ok(len(app.store_key("a" * 400)) == 256, "a key is cut to 256 characters")
    equal(app.store_key("///"), "audio", "a key of slashes falls back instead of being empty")
    equal(app.store_key(""), "audio", "an empty name still gives a key")
    ok(app.store_key("audio_000-hello.wav") == "audio_000-hello.wav", "a clean key is untouched")


def test_estimate_grows_with_text_and_speed():
    slow = speech.estimate_seconds(1000, 1.0)
    fast = speech.estimate_seconds(1000, 2.0)
    short = speech.estimate_seconds(100, 1.0)
    ok(slow > short, "more characters need more time")
    ok(fast < slow, "a faster speed needs less time")
    ok(speech.estimate_seconds(0, 1.0) >= 2.0, "even empty text keeps a floor for the model")


def test_price_state_reads_the_real_dataclass_shape():
    class Charging:
        def __init__(self, info):
            self.info = info

        def get_pricing_info(self):
            return self.info

    priced = SimpleNamespace(is_pay_per_event=True,
                             per_event_prices={app.EVENT_NAME: 0.002})
    charge, why = app.price_state(Charging(priced))
    equal(charge, True, "our event is priced")
    ok(app.EVENT_NAME in why, "the reason names the event")

    missing = SimpleNamespace(is_pay_per_event=True, per_event_prices={"other": 0.01})
    equal(app.price_state(Charging(missing))[0], False, "an event outside the price list is free")

    not_ppe = SimpleNamespace(is_pay_per_event=False, per_event_prices={})
    equal(app.price_state(Charging(not_ppe))[0], False, "not pay per event means no charge")

    empty = SimpleNamespace(is_pay_per_event=None, per_event_prices=None)
    equal(app.price_state(Charging(empty))[0], None, "an unreadable price is unknown, not free")

    class Broken:
        def get_pricing_info(self):
            raise RuntimeError("no")

    equal(app.price_state(Broken())[0], None, "a failed read is unknown")


def test_stopped_status_names_every_stop():
    equal(app.stopped_status("spending-cap")[0], app.ST_CAP, "cap has its own status")
    equal(app.stopped_status("billing-price-unknown")[0], app.ST_BILLING, "price has its own status")
    equal(app.stopped_status("deadline")[0], app.ST_TIME, "deadline has its own status")
    equal(app.stopped_status("charge-error")[0], app.ST_ERROR, "anything else is an error")


def test_build_tasks_refuses_before_any_work():
    texts = ["hello", "  hello  ", "", "x" * 30, "second"]
    tasks, counts = app.build_tasks(texts, max_clips=1, max_chars=20, fingerprint="v|wav|1.00")
    equal(counts["asked"], 5, "every line is asked")
    equal(counts["blank"], 1, "the empty line is blank")
    equal(counts["duplicates"], 1, "the repeat is a duplicate, whitespace included")
    equal(counts["tooLong"], 1, "a line past the character limit is refused")
    equal(counts["limit"], 1, "a line past maxClips is refused")
    equal(counts["unique"], 3, "unique texts are counted once")
    equal(counts["accepted"], 2, "only texts inside the character limit are accepted")
    work = [t for t in tasks if t["kind"] == "work"]
    equal(len(work), 1, "only one text is really made")
    equal(work[0]["text"], "hello", "and it is the first one, in input order")


def test_wav_bytes_is_a_real_wav():
    import wave
    import io
    raw = b"\x00\x01" * 100
    data = speech.wav_bytes(raw, 22050, 2, 1)
    equal(data[:4], b"RIFF", "a wav starts with RIFF")
    equal(data[8:12], b"WAVE", "and names WAVE")
    with wave.open(io.BytesIO(data)) as handle:
        equal(handle.getframerate(), 22050, "the sample rate is kept")
        equal(handle.getnchannels(), 1, "mono is kept")
        equal(handle.getnframes(), 100, "every sample is written")


def test_mp3_without_encoder_is_a_reason_not_a_crash():
    started = speech.shutil.which
    speech.shutil.which = lambda name: None
    try:
        equal(speech.mp3_available(), False, "no encoder means no mp3")
        try:
            speech.to_mp3(b"RIFF")
            ok(False, "encoding without an encoder must raise")
        except speech.SpeechError as exc:
            ok("encoder" in str(exc), f"the reason names the encoder: {exc}")
    finally:
        speech.shutil.which = started


def test_unknown_voice_is_refused_before_loading():
    started = speech.VOICES_DIR
    speech.VOICES_DIR = speech.Path(os.path.join(os.path.dirname(__file__), "no-such-voices"))
    try:
        equal(speech.available_voices(), [], "an empty image has no voices")
        try:
            speech.synthesize("hello", "en-us-lessac", 1.0, "wav")
            ok(False, "loading a voice that is not on disk must raise")
        except speech.SpeechError as exc:
            ok("not in this image" in str(exc), f"the reason names the missing file: {exc}")
    finally:
        speech.VOICES_DIR = started


def test_unknown_voice_name_is_named():
    try:
        speech._voice_files("no-such-voice")
        ok(False, "an unknown voice name must raise")
    except speech.SpeechError as exc:
        ok("no-such-voice" in str(exc), f"the reason names the voice: {exc}")


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
