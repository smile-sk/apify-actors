"""Checks for the feed: RPC answers, the notification decoder, polling and the switch."""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import feed
from src import pump
from tests.test_pump import REAL_EVENT

# a base64 "Program data" line that carries our discriminator and a body too short to decode
UNREADABLE_CREATION = ("Program data: "
                       + base64.b64encode(bytes.fromhex(pump.CREATE_EVENT_DISC) + b"\x00\x00").decode())


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return self.responses.pop(0)


class FakeLog:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def test_rate_limit_is_an_error_not_empty_data():
    client = FakeClient([FakeResponse(429)])
    try:
        asyncio.run(feed.rpc_call(client, "https://rpc", "getSlot", []))
    except feed.RpcError as exc:
        assert "429" in str(exc), str(exc)
    else:
        raise AssertionError("429 was accepted as an answer")


def test_rpc_error_payload_is_raised():
    client = FakeClient([FakeResponse(200, {"error": {"code": -32015, "message": "bad version"}})])
    try:
        asyncio.run(feed.rpc_call(client, "https://rpc", "getTransaction", ["sig", {}]))
    except feed.RpcError as exc:
        assert "bad version" in str(exc), str(exc)
    else:
        raise AssertionError("an RPC error was read as a result")


def test_rpc_result_passes_through():
    client = FakeClient([FakeResponse(200, {"result": 466223239})])
    assert asyncio.run(feed.rpc_call(client, "https://rpc", "getSlot", [])) == 466223239


def test_notification_decodes_the_real_event():
    message = {"params": {"result": {"context": {"slot": 1}, "value": {"signature": "SIG", "logs": [REAL_EVENT]}}}}
    event = feed._creation_from_notification(message)
    assert event is not None
    assert event["signature"] == "SIG"
    assert event["source"] == "stream"
    assert event["virtualSolReserves"] == 30.0


def test_notification_without_creation_is_none():
    message = {"params": {"result": {"value": {"signature": "SIG", "logs": ["Program log: hello"]}}}}
    assert feed._creation_from_notification(message) is None
    assert feed._creation_from_notification({}) is None


def _fake_rpc(signatures, transactions):
    async def call(client, url, method, params, timeout=25.0):
        if method == "getSignaturesForAddress":
            return [{"signature": signature} for signature in signatures]
        if method == "getTransaction":
            return {"meta": {"logMessages": transactions.get(params[0], [])}, "slot": 5}
        raise AssertionError("unexpected method %s" % method)
    return call


def test_poll_reads_each_signature_once_and_marks_source():
    original = feed.rpc_call
    feed.rpc_call = _fake_rpc(["A", "B"], {"A": [REAL_EVENT], "B": ["Program log: buy"]})
    queue: asyncio.Queue = asyncio.Queue()
    stats: dict = {}
    try:
        first = asyncio.run(feed.poll_once(None, "https://rpc", queue, stats, FakeLog()))
        second = asyncio.run(feed.poll_once(None, "https://rpc", queue, stats, FakeLog()))
    finally:
        feed.rpc_call = original
    assert first == 2, first
    assert second == 0, "the same signatures were read twice"
    assert queue.qsize() == 1, queue.qsize()
    event = queue.get_nowait()
    assert event["source"] == "poll"
    assert event["signature"] == "A"
    assert stats["mode"] == "poll"


def test_produce_switches_to_poll_after_stream_failures():
    original_stream, original_poll, original_max = feed.stream_once, feed.poll_once, feed.MAX_STREAM_FAILURES
    polls = []

    async def broken_stream(*args, **kwargs):
        raise feed.RpcError("websocket died")

    async def counting_poll(*args, **kwargs):
        polls.append(1)
        await asyncio.sleep(3600)

    feed.stream_once = broken_stream
    feed.poll_once = counting_poll
    feed.MAX_STREAM_FAILURES = 1
    stats: dict = {"mode": "stream"}
    feed.SILENCE_SECS = 0.01
    original_pause = feed.POLL_PAUSE_SECS
    feed.POLL_PAUSE_SECS = 0.01

    async def run():
        task = asyncio.create_task(feed.produce(asyncio.Queue(), stats, {"rpcUrl": "u", "rpcWsUrl": "w"}, FakeLog(), None))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(run())
    finally:
        feed.stream_once, feed.poll_once, feed.MAX_STREAM_FAILURES = original_stream, original_poll, original_max
        feed.POLL_PAUSE_SECS = original_pause
    assert stats["mode"] == "poll", stats
    assert polls, "polling never started after the stream failed"
    assert stats["streamFailures"] >= 1


def test_produce_starts_on_poll_without_a_websocket_url():
    original_poll = feed.poll_once
    polls = []

    async def counting_poll(*args, **kwargs):
        polls.append(1)
        await asyncio.sleep(3600)

    feed.poll_once = counting_poll
    original_pause = feed.POLL_PAUSE_SECS
    feed.POLL_PAUSE_SECS = 0.01
    stats: dict = {"mode": "stream"}

    async def run():
        task = asyncio.create_task(feed.produce(asyncio.Queue(), stats, {"rpcUrl": "u", "rpcWsUrl": ""}, FakeLog(), None))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(run())
    finally:
        feed.poll_once = original_poll
        feed.POLL_PAUSE_SECS = original_pause
    assert stats["mode"] == "poll", stats


def test_has_create_event_sees_what_the_parser_cannot_read():
    assert pump.has_create_event([UNREADABLE_CREATION]) is True
    assert pump.parse_create_event([UNREADABLE_CREATION]) is None
    assert pump.has_create_event(["Program log: hello"]) is False
    assert pump.has_create_event([]) is False


def test_a_rate_limited_page_keeps_the_signatures_it_could_not_read():
    signatures = ["S%d" % i for i in range(10)]
    healthy = _fake_rpc(signatures, {s: [REAL_EVENT] for s in signatures})
    calls = {"n": 0}

    async def flaky(client, url, method, params, timeout=25.0):
        if method == "getTransaction":
            calls["n"] += 1
            if calls["n"] == 3:
                raise feed.RpcError("getTransaction was rate limited (429)")
        return await healthy(client, url, method, params, timeout)

    original = feed.rpc_call
    feed.rpc_call = flaky
    queue: asyncio.Queue = asyncio.Queue()
    stats: dict = {"paceSecs": 0}
    try:
        try:
            asyncio.run(feed.poll_once(None, "https://rpc", queue, stats, FakeLog()))
        except feed.RpcError as exc:
            assert "429" in str(exc), str(exc)
        else:
            raise AssertionError("a rate limit was swallowed as an empty page")
        marked = len(stats["seenSignatures"])
        feed.rpc_call = healthy
        read = asyncio.run(feed.poll_once(None, "https://rpc", queue, stats, FakeLog()))
    finally:
        feed.rpc_call = original
    assert marked == 2, "a signature was marked before its transaction came back: %d" % marked
    assert stats["signaturesUnread"] == 1, stats
    assert read == 8, "the eight signatures left were never read: %s" % read
    assert queue.qsize() == 10, queue.qsize()


def test_poll_reports_the_chain_gap_and_what_it_heard():
    async def call(client, url, method, params, timeout=25.0):
        if method == "getSignaturesForAddress":
            return [{"signature": "A", "blockTime": 1000}, {"signature": "B", "blockTime": 1005}]
        return {"meta": {"logMessages": ["Program log: not ours"]}, "slot": 1}

    original = feed.rpc_call
    feed.rpc_call = call
    stats: dict = {"paceSecs": 0}
    try:
        read = asyncio.run(feed.poll_once(None, "https://rpc", asyncio.Queue(), stats, FakeLog()))
    finally:
        feed.rpc_call = original
    assert read == 2, read
    assert stats["chainGapSecs"] == 5, stats
    assert stats["signaturesSeen"] == 2, stats
    assert stats["notifications"] == 2, stats


def test_a_creation_we_cannot_decode_is_counted_not_lost():
    async def call(client, url, method, params, timeout=25.0):
        if method == "getSignaturesForAddress":
            return [{"signature": "A"}]
        return {"meta": {"logMessages": [UNREADABLE_CREATION]}, "slot": 1}

    original = feed.rpc_call
    feed.rpc_call = call
    queue: asyncio.Queue = asyncio.Queue()
    stats: dict = {"paceSecs": 0}
    try:
        asyncio.run(feed.poll_once(None, "https://rpc", queue, stats, FakeLog()))
    finally:
        feed.rpc_call = original
    assert queue.qsize() == 0, queue.qsize()
    assert stats["unparsableCreations"] == 1, stats


def test_calls_are_spaced_by_the_given_pace():
    async def call(client, url, method, params, timeout=25.0):
        if method == "getSignaturesForAddress":
            return [{"signature": "A"}, {"signature": "B"}, {"signature": "C"}]
        return {"meta": {"logMessages": []}, "slot": 1}

    original = feed.rpc_call
    feed.rpc_call = call
    stats: dict = {"paceSecs": 0.05}
    started = time.monotonic()
    try:
        asyncio.run(feed.poll_once(None, "https://rpc", asyncio.Queue(), stats, FakeLog()))
    finally:
        feed.rpc_call = original
    elapsed = time.monotonic() - started
    assert elapsed >= 0.09, "three reads were not spaced by the pace: %.3f" % elapsed


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failures = []
    for test in tests:
        try:
            test()
            print("ok   %s" % test.__name__)
        except AssertionError as exc:
            failures.append((test.__name__, str(exc)))
            print("FAIL %s: %s" % (test.__name__, exc))
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, "%s: %s" % (type(exc).__name__, exc)))
            print("ERROR %s: %s: %s" % (test.__name__, type(exc).__name__, exc))
    print("\n%d/%d passed" % (len(tests) - len(failures), len(tests)))
    return failures


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
