"""Checks for the chain decoder, on a real mainnet event and on synthetic layouts.

The first fixture below is a create event captured from the live logs subscription on
06.11.2026, not invented: the real bytes are what caught the offset problem, so the
real bytes are what the tests keep.
"""
from __future__ import annotations

import base64
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import pump

# captured live from logsSubscribe on the pump.fun program, 06.11.2026
REAL_EVENT = (
    "Program data: G3KpTd7rY3YDAAAAZnVuAwAAAGZ1bkMAAABodHRwczovL2lwZnMuaW8vaXBmcy9RbVl4RGQ5QXZwRDZCU3poaEpYdXc0"
    "NnRSc1REQmZKemQ1RmplUlZkSGhDYUN5mjW6F9XNpowIx+pWqL01JN9DQ/Nu2ZB0y/nUhtNk5n/AmH/KiE0aEFFMPBTuIUWsYlocOlF/"
    "atyTlb3uUIxKqlzcqXlb5CGqO7FqY4+w4GRDVMcI5f59hnZJ2flPCw5aXNypeVvkIao7sWpjj7DgZENUxwjl/n2GdknZ+U8LDlqZJ+5q"
    "AAAAAAAQ2EfjzwMAAKwj/AYAAAAAeMX7UdECAACAxqR+jQMABt324e51j94YQl285GzN2rYa/E2DuQ0n/r35KNihi/wBAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAKwj/AYAAAAAAAAAAAAAAAAA"
)


def _b64_event(name, symbol, uri, addresses, numbers, tail=b""):
    def chunk(text):
        raw = text.encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    body = chunk(name) + chunk(symbol) + chunk(uri)
    for address in addresses:
        body += address
    if numbers is not None:
        body += struct.pack("<qQQQQ", *numbers)
    body += tail
    raw = bytes.fromhex(pump.CREATE_EVENT_DISC) + body
    return "Program data: " + base64.b64encode(raw).decode()


CURRENT_NUMBERS = (1793992601, 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 1_000_000_000_000_000)


def test_real_event_parses():
    event = pump.parse_create_event([REAL_EVENT])
    assert event is not None, "the captured mainnet event did not parse"
    assert event["name"] == "fun", event["name"]
    assert event["symbol"] == "fun", event["symbol"]
    assert event["uri"].startswith("https://ipfs.io/"), event["uri"]
    assert event["mint"].endswith("pump"), event["mint"]
    assert len(event["creator"]) >= 32, event["creator"]
    assert event["layout"] == "current", event["layout"]
    assert event["virtualSolReserves"] == 30.0, event["virtualSolReserves"]
    assert event["realSolReserves"] == 0.0, event["realSolReserves"]
    assert event["totalSupply"] == 1_000_000_000.0, event["totalSupply"]
    assert event["reservesKnown"] is True
    assert event["createdAt"] and event["createdAt"].startswith("2026-11-06"), event["createdAt"]


def test_extra_tail_is_ignored():
    # the real event carries 84 bytes past the numbers: the row must not shift
    with_tail = _b64_event("tailed", "TLD", "u", [b"\x11" * 32] * 4, CURRENT_NUMBERS, tail=b"\x00" * 84)
    event = pump.parse_create_event([with_tail])
    assert event is not None
    assert event["virtualSolReserves"] == 30.0
    assert event["layout"] == "current"


def test_older_layout_uses_user_as_creator():
    user = b"\x22" * 32
    line = _b64_event("old", "OLD", "u", [b"\x33" * 32, b"\x44" * 32, user], CURRENT_NUMBERS)
    event = pump.parse_create_event([line])
    assert event is not None
    assert event["layout"] == "older", event["layout"]
    assert event["creator"] == event["user"], (event["creator"], event["user"])


def test_foreign_logs_are_not_creations():
    other = "Program data: " + base64.b64encode(bytes.fromhex("deadbeefdeadbeef") + b"\x00" * 40).decode()
    assert pump.parse_create_event([other]) is None
    assert pump.parse_create_event(["Program log: Instruction: Create"]) is None
    assert pump.parse_create_event([]) is None
    assert pump.parse_create_event(None) is None


def test_broken_base64_is_skipped_not_fatal():
    assert pump.parse_create_event(["Program data: !!!not-base64!!!"]) is None


def test_impossible_reserves_become_null_not_a_number():
    # measured on 3 of 8 live launches: virtual SOL far above anything a newborn curve holds
    numbers = (1793992690, 1_073_000_000_000_000, 814_455_172_007, 793_100_000_000_000, 1_000_000_000_000_000)
    line = _b64_event("Finance Inu", "FI", "u", [b"\x55" * 32] * 4, numbers, tail=b"\x00" * 84)
    event = pump.parse_create_event([line])
    assert event is not None, "the mint must still be delivered"
    assert event["reservesKnown"] is False
    assert event["virtualSolReserves"] is None
    assert event["realSolReserves"] is None
    assert pump.curve_progress_percent(event["realSolReserves"]) is None
    assert event["createdAt"] and event["createdAt"].startswith("2026-11-06"), event["createdAt"]
    assert event["mint"], "the mint must survive even when the numbers do not"


def test_garbage_numbers_keep_addresses():
    numbers = (0, 1, 2, 3, 4)
    line = _b64_event("junk", "JNK", "u", [b"\x66" * 32] * 4, numbers)
    event = pump.parse_create_event([line])
    assert event is not None
    assert event["reservesKnown"] is False
    assert event["createdAt"] is None
    assert event["mint"]


def test_b58_round_trip_shape():
    assert pump.b58encode(b"\x00" * 32) == "1" * 32
    assert pump.b58encode(bytes(range(32)))
    assert len(pump.b58encode(b"\x01" * 32)) > 30


def test_curve_progress_bounds():
    assert pump.curve_progress_percent(0.0) == 0.0
    assert pump.curve_progress_percent(42.5) == 50.0
    assert pump.curve_progress_percent(85.0) == 100.0
    assert pump.curve_progress_percent(200.0) == 100.0
    assert pump.curve_progress_percent(None) is None
    assert pump.curve_progress_percent("junk") is None


def _tx(pre, post):
    return {"meta": {"preTokenBalances": pre, "postTokenBalances": post}}


def _bal(owner, mint, amount):
    return {"mint": mint, "owner": owner, "uiTokenAmount": {"uiAmount": amount}}


def test_dev_buy_is_the_balance_delta():
    tx = _tx([_bal("CREATOR", "MINT", 0)], [_bal("CREATOR", "MINT", 1250.5)])
    assert pump.dev_buy_tokens(tx, "CREATOR", "MINT") == 1250.5


def test_dev_buy_is_zero_when_no_tokens_moved():
    # no balance entry for this mint in a transaction that was read: nothing was bought
    tx = _tx([_bal("OTHER", "MINT2", 0)], [_bal("OTHER", "MINT2", 5)])
    assert pump.dev_buy_tokens(tx, "CREATOR", "MINT") == 0.0


def test_dev_buy_is_unknown_when_the_transaction_was_not_read():
    assert pump.dev_buy_tokens(None, "CREATOR", "MINT") is None
    assert pump.dev_buy_tokens({}, "CREATOR", "MINT") is None
    assert pump.dev_buy_tokens({"meta": None}, "CREATOR", "MINT") is None


def test_dev_buy_reads_the_signer_when_the_creator_did_not_buy():
    # some launches buy from the signing wallet, which is not always the creator field
    tx = _tx([_bal("SIGNER", "MINT", 0)], [_bal("SIGNER", "MINT", 777.0)])
    assert pump.dev_buy_tokens(tx, "CREATOR", "MINT", alt_owner="SIGNER") == 777.0


def test_dev_buy_percent():
    assert pump.dev_buy_percent(100.0, 1000.0) == 10.0
    assert pump.dev_buy_percent(0.0, 1000.0) == 0.0
    assert pump.dev_buy_percent(None, 1000.0) is None
    assert pump.dev_buy_percent(10.0, None) is None
    assert pump.dev_buy_percent(10.0, 0) is None


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
