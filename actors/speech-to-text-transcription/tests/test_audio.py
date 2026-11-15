"""Real checks for src/audio.py and src/speech.py: no network, real bytes.

Run: python tests/test_audio.py   (exit code 0 = every check passed)

The audio is generated here with the wave module, so the decoder is measured on a
file whose length is known in advance, and the redirect guard is measured on a fake
client that records which addresses were actually requested.
"""
from __future__ import annotations

import asyncio
import io
import math
import os
import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from src import audio as A  # noqa: E402
from src import speech as S  # noqa: E402
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


def make_wav(seconds: float = 2.0, rate: int = 16000, freq: float = 440.0) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(int(seconds * rate)):
            value = int(12000 * math.sin(2 * math.pi * freq * index / rate))
            frames += struct.pack("<h", value)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


class FakeStream:
    def __init__(self, status_code: int, headers: dict | None = None, body: bytes = b"",
                 chunk: int = 4096, peer: str | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body
        self.chunk = chunk
        self.extensions = {}
        if peer:
            class _Stream:
                @staticmethod
                def get_extra_info(name):
                    return ("10.1.2.3", 80) if name == "server_addr" else None
            self.extensions["network_stream"] = _Stream()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self, size: int = 4096):
        for start in range(0, len(self.body), min(size, self.chunk) or 1):
            yield self.body[start:start + min(size, self.chunk)]


class FakeClient:
    def __init__(self, pages: dict) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def stream(self, method: str, url: str):
        self.requested.append(url)
        if url not in self.pages:
            return FakeStream(404)
        return self.pages[url]


REAL_RESOLVE = A._resolve


def fake_resolve(host: str):
    """public.example is a name this test owns; every other name goes to real DNS."""
    answers = {"public.example": "93.184.216.34", "127.0.0.1": "127.0.0.1"}
    if host in answers:
        return [(None, None, 6, "", (answers[host], 0))]
    return REAL_RESOLVE(host)


A._resolve = fake_resolve


def grab(url: str, max_bytes: int = 10 * 1024 * 1024):
    client = FakeClient(pages=CLIENT.pages)
    return asyncio.run(A.download(client, url, max_bytes)), client


def run_all() -> int:
    wav = make_wav(2.0)
    flac_like = b"fLaC" + b"\x00" * 64

    # --- URL gates -------------------------------------------------------
    eq("url: https accepted", A.normalize_url(" https://example.com/a.mp3 "),
       "https://example.com/a.mp3")
    eq("url: http accepted", A.normalize_url("http://example.com/a.wav"),
       "http://example.com/a.wav")
    for bad in ("", "   ", "ftp://example.com/a.mp3", "file:///c:/a.mp3",
                "javascript:alert(1)", "example.com/a.mp3", "https://", "http:///a.mp3"):
        eq(f"url: {bad!r} refused", A.normalize_url(bad), None)
    eq("url: space inside refused", A.normalize_url("https://example.com/a b.mp3"), None)
    eq("url: broken bracket refused", A.normalize_url("http://[abc/x.mp3"), None)
    eq("file name from url", A.file_name_of("https://example.com/x/y/clip.mp3?x=1"), "clip.mp3")

    # --- the private address gate ---------------------------------------
    for host, why in (("127.0.0.1", "loopback"), ("169.254.169.254", "metadata"),
                      ("10.0.0.5", "private"), ("192.168.88.1", "lan"),
                      ("100.64.0.1", "cgnat"), ("::1", "ipv6 loopback"),
                      ("::ffff:127.0.0.1", "mapped loopback")):
        ok, _reason = A.public_host(host)
        check(f"guard: {host} refused ({why})", ok is False)
    ok, _reason = A.public_host("8.8.8.8")
    check("guard: public address accepted", ok is True)
    ok, _reason = A.public_host("this-host-does-not-exist.invalid")
    check("guard: unresolvable host refused", ok is False)
    ok, why = A.public_host("a" * 64 + ".example.com")
    check("guard: a label of 64 characters refused, not raised", ok is False, why)
    check("guard: the long label is named as not a name", "not a valid name" in why, why)

    # --- download --------------------------------------------------------
    global CLIENT
    CLIENT = FakeClient(pages={
        "https://public.example/a.wav": FakeStream(200, {"content-type": "audio/wav",
                                                         "content-length": str(len(wav))}, wav),
        "https://public.example/redir": FakeStream(302, {"location": "/b.wav"}),
        "https://public.example/b.wav": FakeStream(200, {"content-type": "audio/wav"}, wav),
        "https://public.example/to-private": FakeStream(302,
                                                        {"location": "http://127.0.0.1/secret"}),
        "https://public.example/big": FakeStream(200, {"content-length": str(50 * 1024 * 1024)},
                                                 b"x"),
        "https://public.example/stream-big": FakeStream(200, {}, b"x" * 50000),
        "https://public.example/peer-private": FakeStream(200, {}, wav, peer="private"),
        "https://public.example/missing": FakeStream(404),
    })
    got, client = grab("https://public.example/a.wav")
    eq("download: bytes", got.data, wav)
    eq("download: size", got.bytes_read, len(wav))
    eq("download: content type", got.content_type, "audio/wav")
    eq("download: one request", len(client.requested), 1)

    got, client = grab("https://public.example/redir")
    eq("redirect: followed to the second file", got.final_url, "https://public.example/b.wav")
    eq("redirect: two requests", len(client.requested), 2)

    try:
        grab("https://public.example/to-private")
        check("redirect: private hop refused", False, "no error raised")
    except A.AudioError as exc:
        eq("redirect: private hop status", exc.status, st.PRIVATE_TARGET)
        check("redirect: private hop named in the reason", "127.0.0.1" in exc.reason, exc.reason)
    client = CLIENT
    check("redirect: the private address was never requested",
          "http://127.0.0.1/secret" not in client.requested, str(client.requested))

    try:
        grab("https://public.example/big")
        check("size: content-length over the cap refused", False, "no error raised")
    except A.AudioError as exc:
        eq("size: status", exc.status, st.TOO_LARGE)

    try:
        grab("https://public.example/stream-big", max_bytes=10000)
        check("size: streaming over the cap refused", False, "no error raised")
    except A.AudioError as exc:
        eq("size: streaming status", exc.status, st.TOO_LARGE)

    try:
        grab("https://public.example/peer-private")
        check("peer: private peer refused", False, "no error raised")
    except A.AudioError as exc:
        eq("peer: status", exc.status, st.PRIVATE_TARGET)

    try:
        grab("https://public.example/missing")
        check("fetch: 404 refused", False, "no error raised")
    except A.AudioError as exc:
        eq("fetch: 404 status", exc.status, st.FETCH_ERROR)
        check("fetch: 404 named", "404" in exc.reason, exc.reason)

    try:
        grab("https://" + "a" * 64 + ".example.com/x.mp3")
        check("long label: refused", False, "no error raised")
    except A.AudioError as exc:
        eq("long label: fetch error, not a private target", exc.status, st.FETCH_ERROR)
        check("long label: reason names the name", "not a valid name" in exc.reason, exc.reason)
    check("long label: nothing was requested", CLIENT.requested == [], str(CLIENT.requested))

    # --- decode ----------------------------------------------------------
    duration = A.probe_duration(wav)
    check("probe: two seconds", duration is not None and abs(duration - 2.0) < 0.05, str(duration))
    samples = A.decode(wav)
    eq("decode: sample count", int(samples.size), 32000)
    eq("decode: float32", str(samples.dtype), "float32")
    check("decode: values in range", float(abs(samples).max()) <= 1.0001,
          str(float(abs(samples).max())))
    check("decode: not silent", float(abs(samples).max()) > 0.1, str(float(abs(samples).max())))

    try:
        A.decode(b"<html><body>this is a page, not audio</body></html>")
        check("decode: html refused", False, "no error raised")
    except A.AudioError as exc:
        eq("decode: html status", exc.status, st.UNSUPPORTED)

    try:
        A.decode(b"")
        check("decode: empty bytes refused", False, "no error raised")
    except A.AudioError as exc:
        eq("decode: empty status", exc.status, st.UNSUPPORTED)

    check("probe: html has no duration", A.probe_duration(b"<html>nope</html>") is None)

    # --- subtitles -------------------------------------------------------
    cues = [{"start": 0.0, "end": 1.5, "text": "first line"},
            {"start": 61.25, "end": 62.0, "text": "second line"}]
    srt = S.to_srt(cues)
    check("srt: numbered", srt.startswith("1\n"), srt[:40])
    check("srt: comma clock", "00:00:00,000 --> 00:00:01,500" in srt, srt[:80])
    check("srt: minute rollover", "00:01:01,250 --> 00:01:02,000" in srt, srt)
    vtt = S.to_vtt(cues)
    check("vtt: header", vtt.startswith("WEBVTT"), vtt[:20])
    check("vtt: dot clock", "00:00:00.000 --> 00:00:01.500" in vtt, vtt[:80])

    # --- threads from the memory budget ---------------------------------
    original = os.environ.get("ACTOR_MEMORY_MBYTES")
    for memory, expected in (("1024", 1), ("4096", 1), ("8192", 2), ("16384", 4), ("65536", 4)):
        os.environ["ACTOR_MEMORY_MBYTES"] = memory
        eq(f"threads: {memory} MB", S.worker_threads(), expected)
    del os.environ["ACTOR_MEMORY_MBYTES"]
    eq("threads: default without the variable", S.worker_threads(), 1)
    if original is not None:
        os.environ["ACTOR_MEMORY_MBYTES"] = original

    # --- the deadline plan is measured on one thread ----------------------
    eq("plan: small on one thread is 2.4", round(S.prior_rtf("small", 1), 4), 2.4)
    check("plan: two threads help but not twice", 1.3 < S.prior_rtf("small", 2) < 2.4,
          str(round(S.prior_rtf("small", 2), 3)))
    eq("plan: base on one thread", S.prior_rtf("base", 1), 1.0)
    eq("plan: an unknown model falls back to small", S.prior_rtf("large", 1), 2.4)
    eq("plan: the first file carries the model load",
       round(S.plan_seconds(0.0, 2.4, False), 1), 8.0)
    eq("plan: a loaded model costs no load", S.plan_seconds(0.0, 2.4, True), 0.0)
    eq("plan: eleven seconds of small on one thread",
       round(S.plan_seconds(11.0, 2.4, True), 1), 31.7)
    check("plan: the old two-thread number is not used",
       not hasattr(S, "PRIOR_RTF"), "PRIOR_RTF still exists and understates the plan")

    print(f"test_audio: {CHECKS} checks, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL:", fail)
    if CHECKS < 40:
        print("  FAIL: too few checks ran")
        return 1
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(run_all())
