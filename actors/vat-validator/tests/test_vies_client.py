"""The one branch that decides whether this actor is honest: silence is not "invalid".

These tests run without Apify and without the network: httpx.MockTransport
answers instead of VIES, so every shape of silence can be reproduced on demand.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vies import ask_vies  # noqa: E402


def _answer(payload: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


def _raise(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return httpx.MockTransport(handler)


def _ask(transport, attempts: int = 3) -> dict:
    async def run() -> dict:
        async with httpx.AsyncClient(transport=transport) as client:
            return await ask_vies(client, "DE", "811569869", attempts=attempts, pause=0)
    return asyncio.run(run())


def test_valid_answer_carries_company_data():
    result = _ask(_answer({"valid": True, "name": "ACME GmbH", "address": "Hauptstr 1, Berlin"}))
    assert result["answered"] is True
    assert result["valid"] is True
    assert result["name"] == "ACME GmbH"
    assert result["address"] == "Hauptstr 1, Berlin"


def test_not_registered_is_an_answer_not_silence():
    result = _ask(_answer({"valid": False, "name": "", "address": ""}))
    assert result["answered"] is True
    assert result["valid"] is False


def test_germany_hides_the_name_and_we_do_not_invent_one():
    result = _ask(_answer({"valid": True, "name": "---", "address": "---"}))
    assert result["answered"] is True
    assert result["name"] == ""
    assert result["address"] == ""


def test_action_succeed_false_is_silence():
    result = _ask(_answer({"valid": False, "actionSucceed": False, "error": "MS_UNAVAILABLE"}))
    assert result["answered"] is False
    assert result["valid"] is None
    assert "MS_UNAVAILABLE" in result["error"]
    assert result["tries"] == 3


def test_http_error_is_silence():
    result = _ask(_answer({"error": "gateway"}, status=502))
    assert result["answered"] is False
    assert "502" in result["error"]


def test_network_failure_is_silence():
    result = _ask(_raise(httpx.ConnectError("no route to host")))
    assert result["answered"] is False
    assert "ConnectError" in result["error"]


def test_timeout_is_silence():
    result = _ask(_raise(httpx.ReadTimeout("timed out")))
    assert result["answered"] is False
    assert "ReadTimeout" in result["error"]


def test_valid_none_without_error_is_silence():
    result = _ask(_answer({"valid": None}))
    assert result["answered"] is False


def test_valid_null_with_an_error_is_silence_not_invalid():
    # The exact shape Orestes reproduced: valid is null, an error is present.
    # Reading it as "not registered" would put a real customer on a blacklist.
    result = _ask(_answer({"valid": None, "error": "MS_UNAVAILABLE"}))
    assert result["answered"] is False
    assert result["valid"] is None
    assert "MS_UNAVAILABLE" in result["error"]


def test_retries_stop_as_soon_as_an_answer_arrives():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json={"valid": True, "name": "X", "address": "Y"})

    result = _ask(httpx.MockTransport(handler))
    assert result["answered"] is True
    assert calls["n"] == 3


def test_single_attempt_when_asked():
    result = _ask(_raise(httpx.ConnectError("down")), attempts=1)
    assert result["answered"] is False
    assert result["tries"] == 1


def run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:                                  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
