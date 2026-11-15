"""Where new tokens come from: the live logs subscription, with an HTTP fallback.

Measured on mainnet 06.11.2026: `logsSubscribe` on the pump.fun program gave 5654
notifications in 40 seconds (141 a second) and 16 creations (23.9 a minute, so about
34 000 new tokens a day). The creation event carries everything the row needs, so a
stream costs no per-token request.

Three measured facts shape the code:

* pump.fun transactions are version 1, and the public endpoint answers
  "Transaction version (1) is not supported by the requesting client" when a request
  says maxSupportedTransactionVersion 0. Version 1 reads them, so the constant is 1;
* the public endpoint rate-limits hard: one request every 0.3 s earned 429 within ten
  calls, one request every 2.5 s (0.4 a second, the natural rate of creations) went
  through 6 of 6. The pace is therefore a parameter, and the default is the measured one;
* the fallback is a sample, not a copy of the stream: a page of 100 signatures covers
  about 0.71 s of chain at 141 notifications a second, and those 100 transactions read
  at the safe pace take minutes. Polling catches a slice of the flow, and the summary
  says so in numbers (signaturesSeen, signaturesUnread, chainGapSecs) instead of
  promising full coverage.

A signature is remembered only AFTER its transaction came back. Marking it before the
read turned a 429 into permanent blindness: on a stand with 10 signatures and a 429 on
the third, 10 were marked, 3 were read, and the next healthy cycle made zero calls and
reported zero rows without a single counter.

The public websocket drops under load, and that is expected: silence is a reason to
switch to polling the HTTP endpoint, not a reason to end the run with an empty dataset.
A run that hears nothing at all must fail with a human message naming the endpoint,
never end green and empty.
"""
from __future__ import annotations

import asyncio
import json
import time

try:  # the websocket client is optional at import time so the parser tests stay offline
    import websockets
except Exception:  # noqa: BLE001
    websockets = None

from src import pump

SILENCE_SECS = 30.0
MAX_STREAM_FAILURES = 5
POLL_LIMIT = 100
SEEN_SIGNATURES_KEPT = 5000
TX_VERSION = 1
POLL_PAUSE_SECS = 2.5
RATE_LIMIT_PAUSE_SECS = 5.0
# measured: one call every 0.3 s earned 429 within ten calls, one call every 2.5 s went
# through 6 of 6. A buyer with their own endpoint lowers it through rpcPaceSecs.
RPC_PACE_SECS = 2.5


class RpcError(RuntimeError):
    """The endpoint answered with an error instead of a result."""


async def rpc_call(client, url: str, method: str, params, timeout: float = 25.0):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = await client.post(url, json=payload, timeout=timeout)
    if response.status_code == 429:
        # A rate limit is not an answer: it must never read as "no data".
        raise RpcError("%s was rate limited (429)" % method)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RpcError("%s returned a body that is not a JSON object" % method)
    if data.get("error"):
        raise RpcError("%s: %s" % (method, str(data["error"])[:200]))
    return data.get("result")


async def paced_rpc(client, url: str, method: str, params, stats: dict, timeout: float = 25.0):
    """One RPC call, kept at the pace this endpoint tolerates.

    The first call of a cycle never waits; every following one waits out the remainder of
    the interval the run was given. Skipping this in the poll path earned 429 on a burst
    and, with signatures marked before the read, lost them for good.
    """
    pace = stats.get("paceSecs")
    if pace is None:
        pace = RPC_PACE_SECS
    last = stats.get("lastRpcAt")
    if last is not None:
        wait = float(pace) - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
    stats["lastRpcAt"] = time.monotonic()
    return await rpc_call(client, url, method, params, timeout=timeout)


def _logs_of(message: dict):
    result = ((message.get("params") or {}).get("result") or {})
    value = result.get("value") or {}
    return value.get("logs") or []


def _creation_from_notification(message: dict):
    """The creation in one logsSubscribe message, or None."""
    logs = _logs_of(message)
    if not logs:
        return None
    event = pump.parse_create_event(logs)
    if not event:
        return None
    result = ((message.get("params") or {}).get("result") or {})
    value = result.get("value") or {}
    event["signature"] = value.get("signature")
    event["source"] = "stream"
    event["slot"] = value.get("slot") or (result.get("context") or {}).get("slot")
    return event


async def stream_once(ws_url: str, queue, stats: dict, log, silence: float = SILENCE_SECS) -> None:
    """Listen to the program's logs until the socket dies or goes silent.

    Returns only by raising: a normal return would spin the caller in a tight loop.
    """
    if websockets is None:
        raise RpcError("the websockets library is not installed in this environment")
    async with websockets.connect(
        ws_url,
        open_timeout=15,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_size=2 ** 23,
    ) as socket:
        await socket.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [{"mentions": [pump.PUMP_PROGRAM]}, {"commitment": "processed"}],
        }))
        first = json.loads(await asyncio.wait_for(socket.recv(), timeout=silence))
        if first.get("error"):
            raise RpcError("logsSubscribe refused: %s" % str(first["error"])[:180])
        stats["mode"] = "stream"
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=silence)
            # every message the socket hands over is a notification, creation or not: this is
            # what tells "the endpoint is silent" apart from "the chain is quiet right now"
            stats["notifications"] = stats.get("notifications", 0) + 1
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            logs = _logs_of(message)
            event = _creation_from_notification(message)
            if event is None:
                if logs and pump.has_create_event(logs):
                    stats["unparsableCreations"] = stats.get("unparsableCreations", 0) + 1
                continue
            await queue.put(event)


async def poll_once(client, rpc_url: str, queue, stats: dict, log, limit: int = POLL_LIMIT) -> int:
    """Read the newest program transactions and enqueue the creations among them.

    Returns how many signatures were actually read. A signature is marked seen only after
    its transaction came back, so a rate-limited page is retried by the next cycle instead
    of disappearing; how much the page missed is counted, not hidden.
    """
    signatures = await rpc_call(client, rpc_url, "getSignaturesForAddress",
                                [pump.PUMP_PROGRAM, {"limit": limit}])
    stats["mode"] = "poll"
    listed = [item for item in (signatures or []) if (item or {}).get("signature")]
    stats["notifications"] = stats.get("notifications", 0) + len(listed)
    stats["signaturesSeen"] = stats.get("signaturesSeen", 0) + len(listed)
    times = [item.get("blockTime") for item in listed
             if isinstance(item.get("blockTime"), (int, float))]
    if len(times) >= 2:
        stats["chainGapSecs"] = int(max(times) - min(times))

    seen = stats.setdefault("seenSignatures", {})
    pending = [item for item in listed if item["signature"] not in seen]
    read = 0
    for item in reversed(pending):  # oldest first, so rows follow creation order
        signature = item["signature"]
        try:
            transaction = await paced_rpc(client, rpc_url, "getTransaction",
                                          [signature, {
                                              "encoding": "jsonParsed",
                                              "maxSupportedTransactionVersion": TX_VERSION,
                                              "commitment": "confirmed",
                                          }], stats)
        except Exception:
            # not seen: the next cycle may still read it, and the counter says it was missed
            stats["signaturesUnread"] = stats.get("signaturesUnread", 0) + 1
            raise
        seen[signature] = True
        read += 1
        logs = ((transaction or {}).get("meta") or {}).get("logMessages") or []
        event = pump.parse_create_event(logs)
        if not event:
            if pump.has_create_event(logs):
                stats["unparsableCreations"] = stats.get("unparsableCreations", 0) + 1
            continue
        event["signature"] = signature
        event["source"] = "poll"
        event["slot"] = (transaction or {}).get("slot")
        # the transaction is already open: the creator buy costs no second request here
        event["chainTx"] = transaction
        await queue.put(event)

    stats["signaturesRead"] = stats.get("signaturesRead", 0) + read
    while len(seen) > SEEN_SIGNATURES_KEPT:
        seen.pop(next(iter(seen)))
    return read


async def produce(queue, stats: dict, cfg: dict, log, client) -> None:
    """Keep feeding the queue for as long as the run lasts: stream, then polling."""
    if not cfg.get("rpcWsUrl"):
        # nothing to subscribe to: start on the HTTP road instead of failing five times
        stats["mode"] = "poll"
        mode = "poll"
    else:
        mode = "stream"
    failures = 0
    while True:
        try:
            if mode == "stream":
                await stream_once(cfg["rpcWsUrl"], queue, stats, log)
                failures = 0
            else:
                await poll_once(client, cfg["rpcUrl"], queue, stats, log)
                failures = 0
                await asyncio.sleep(POLL_PAUSE_SECS)
        except asyncio.CancelledError:
            raise
        except RpcError as exc:
            failures += 1
            stats["streamFailures"] = stats.get("streamFailures", 0) + 1
            stats["lastFeedError"] = "%s: %s" % (type(exc).__name__, str(exc)[:180])
            log.warning("feed %s failed (%d in a row): %s", mode, failures, exc)
            if mode == "stream" and failures >= MAX_STREAM_FAILURES:
                mode = "poll"
                stats["mode"] = "poll"
                failures = 0
                log.warning("switching to HTTP polling: the websocket failed %d times",
                            MAX_STREAM_FAILURES)
                continue
            pause = RATE_LIMIT_PAUSE_SECS if "429" in str(exc) else min(16.0, float(2 ** max(0, failures - 1)))
            await asyncio.sleep(pause)
        except Exception as exc:  # noqa: BLE001 - a dead feed is a reason to retry, not to crash
            failures += 1
            stats["streamFailures"] = stats.get("streamFailures", 0) + 1
            stats["lastFeedError"] = "%s: %s" % (type(exc).__name__, str(exc)[:180])
            log.warning("feed %s failed (%d in a row): %s", mode, failures, exc)
            if mode == "stream" and failures >= MAX_STREAM_FAILURES:
                mode = "poll"
                stats["mode"] = "poll"
                failures = 0
                log.warning("switching to HTTP polling: the websocket failed %d times",
                            MAX_STREAM_FAILURES)
                continue
            await asyncio.sleep(min(16.0, float(2 ** max(0, failures - 1))))
