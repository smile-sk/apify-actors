"""Checks that need the whole run, not one function: what the buyer pays for.

Apify is not installed here, so a stub Actor stands in its place. That is the
point: the money branches must be provable without the platform, and the stub
rejects a positional `fail()` exactly as the real SDK does.

No network: every number here is either offline-checked or outside VIES.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeCharge:
    def __init__(self, count: int = 1, limit: bool = False) -> None:
        self.charged_count = count
        self.event_charge_limit_reached = limit


class _ActorMeta(type):
    """The real Actor is used as a class in `async with Actor:`, so the stub is too."""

    async def __aenter__(cls):
        return cls

    async def __aexit__(cls, *args) -> bool:
        return False


class _Log:
    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


class FakeActor(metaclass=_ActorMeta):
    input_data: dict = {}
    pushed: list = []
    charge_calls = 0
    fail_message = ""
    values: dict = {}
    log = _Log()

    @classmethod
    def reset(cls, data: dict) -> None:
        cls.input_data = data
        cls.pushed = []
        cls.charge_calls = 0
        cls.fail_message = ""
        cls.values = {}

    @classmethod
    async def get_input(cls) -> dict:
        return cls.input_data

    @classmethod
    async def set_value(cls, key: str, value) -> None:
        cls.values[key] = value

    @classmethod
    async def push_data(cls, row: dict) -> None:
        cls.pushed.append(row)

    @classmethod
    async def open_key_value_store(cls, name: str | None = None):
        raise RuntimeError("no storage in this test")

    @classmethod
    async def charge(cls, event_name: str) -> FakeCharge:
        cls.charge_calls += 1
        return FakeCharge()

    @classmethod
    async def fail(cls, *, status_message: str = "", **kwargs) -> None:
        # Keyword-only, like apify 4.2.0: a positional call must blow up here.
        cls.fail_message = status_message


_stub = types.ModuleType("apify")
_stub.Actor = FakeActor
sys.modules.setdefault("apify", _stub)

from src import main as actor_main  # noqa: E402


def _run(data: dict) -> None:
    FakeActor.reset(data)
    asyncio.run(actor_main.main())


def test_text_is_ignored_when_numbers_are_given():
    _run({"vatNumbers": "DE811569860",
          "text": "Invoice 2024, order 259137088",
          "countryHint": "DE", "checkVies": False})
    inputs = [row["input"] for row in FakeActor.pushed]
    assert inputs == ["DE811569860"], inputs
    assert FakeActor.pushed[0]["status"] == "INVALID_CHECKSUM"


def test_switched_off_vies_says_unknown_not_valid():
    _run({"vatNumbers": "DE811569869", "checkVies": False})
    row = FakeActor.pushed[0]
    assert row["status"] == "NOT_CHECKED", row
    assert row["valid"] is None, row          # registration was never checked
    assert row["checksumValid"] is True, row
    assert FakeActor.charge_calls == 0


def test_a_number_without_a_prefix_from_text_is_not_charged():
    _run({"text": "our number is 980780684", "countryHint": "GB"})
    row = FakeActor.pushed[0]
    assert row["status"] == "NOT_IN_VIES", row
    assert row["charged"] is False, row
    assert FakeActor.charge_calls == 0, "a guessed number must never be billed"
    assert "not charged" in row["reason"], row


def test_a_prefixed_number_is_charged_once_and_delivered():
    _run({"vatNumbers": "GB980780684"})
    row = FakeActor.pushed[0]
    assert row["status"] == "NOT_IN_VIES", row
    assert row["charged"] is True
    assert FakeActor.charge_calls == 1
    assert FakeActor.values["SUMMARY"]["billableChecks"] == 1


def test_empty_input_fails_with_a_message_not_a_traceback():
    _run({})
    assert "No VAT number given" in FakeActor.fail_message
    assert FakeActor.pushed == []


def test_rows_the_input_limit_dropped_are_counted():
    saved = actor_main.MAX_ROWS
    actor_main.MAX_ROWS = 20
    try:
        rows = "\n".join(f"DE{100000000 + i:09d}" for i in range(25))
        _run({"vatNumbers": rows, "checkVies": False})
    finally:
        actor_main.MAX_ROWS = saved
    summary = FakeActor.values["SUMMARY"]
    assert summary["inputRows"] == 25, summary
    assert summary["rowsDroppedByInputLimit"] == 5, summary


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
