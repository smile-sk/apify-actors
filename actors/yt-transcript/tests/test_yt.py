"""Tests for transcript parsing, track choice and subtitle output.

Pure functions only: no network, no Apify SDK. The one network entry point
(`fetch`) is tested for its local guard, not for YouTube's answer.

Run directly:  python tests/test_yt.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from yt import (  # noqa: E402
    _clock,
    _pick_track,
    _segments_from_json,
    fetch,
    to_srt,
    to_vtt,
    video_id,
)

WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_video_id_watch_url():
    assert video_id(WATCH) == "dQw4w9WgXcQ"
    assert video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s") == "dQw4w9WgXcQ"


def test_video_id_other_shapes():
    assert video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_video_id_bare_id():
    assert video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("  dQw4w9WgXcQ  ") == "dQw4w9WgXcQ"


def test_video_id_rejects_junk():
    assert video_id("") is None
    assert video_id(None) is None
    assert video_id("https://example.com/video") is None
    assert video_id("too-short") is None


def test_clock_formats():
    assert _clock(0) == "00:00:00.000"
    assert _clock(3661.5) == "01:01:01.500"
    assert _clock(1.234, comma=True) == "00:00:01,234"


def test_segments_from_json():
    raw = b'{"events":[{"tStartMs":0,"dDurationMs":1500,"segs":[{"utf8":"Hello "},{"utf8":"world"}]},' \
          b'{"tStartMs":1500,"dDurationMs":2000,"segs":[{"utf8":"\\nsecond line"}]},' \
          b'{"tStartMs":4000,"dDurationMs":10,"segs":[]}]}'
    segments = _segments_from_json(raw)
    assert len(segments) == 2, segments
    assert segments[0] == {"start": 0.0, "duration": 1.5, "text": "Hello world"}
    assert segments[1]["text"] == "second line"
    assert segments[1]["start"] == 1.5


def test_to_srt_numbering_and_comma():
    srt = to_srt([{"start": 0.0, "duration": 1.5, "text": "Hello"},
                  {"start": 1.5, "duration": 2.0, "text": "World"}])
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,500\nHello")
    assert "\n2\n00:00:01,500 --> 00:00:03,500\nWorld" in srt


def test_to_vtt_header_and_dot():
    vtt = to_vtt([{"start": 0.0, "duration": 1.0, "text": "Hello"}])
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.000" in vtt


def _track(code, kind=None):
    return {"languageCode": code, "baseUrl": f"https://example.com/{code}" + (f"&kind={kind}" if kind else ""),
            **({"kind": kind} if kind else {})}


def test_pick_track_requested_language():
    tracks = [_track("de"), _track("en")]
    picked = _pick_track(tracks, "de", True, None)
    assert picked["languageCode"] == "de"
    assert not picked.get("_language_fallback")


def test_pick_track_missing_language_falls_back_with_flag():
    # A language the video does not carry must not kill the run.
    tracks = [_track("en"), _track("ru")]
    picked = _pick_track(tracks, "ja", True, None)
    assert picked["languageCode"] == "en"
    assert picked["_language_fallback"] is True


def test_pick_track_prefers_manual_over_asr():
    tracks = [_track("en", "asr"), _track("en")]
    assert _pick_track(tracks, "en", True, None).get("kind") != "asr"
    assert _pick_track(tracks, "en", False, None).get("kind") == "asr"


def test_pick_track_uses_youtube_default_when_no_language():
    tracks = [_track("en"), _track("de"), _track("fr")]
    assert _pick_track(tracks, None, True, 1)["languageCode"] == "de"


def test_pick_track_prefers_english_when_no_language():
    tracks = [_track("de"), _track("en")]
    assert _pick_track(tracks, None, True, None)["languageCode"] == "en"


def test_pick_track_empty_list():
    assert _pick_track([], "en", True, None) is None


def test_fetch_rejects_non_youtube_link_without_network():
    result = fetch("https://example.com/not-a-video")
    assert result["ok"] is False
    assert result["error"] == "BAD_URL"
    assert "reason" in result


def run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:
            failed.append((name, exc))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
