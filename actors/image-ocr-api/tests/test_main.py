"""The money path under test: one row, one charge, and every refusal free.

The SDK is faked with the real classes of apify 4.2.1, not with the shape that would
be convenient: ChargeResult takes (event_charge_limit_reached, charged_count,
chargeable_within_limit) and ActorPricingInfo takes (pricing_model,
max_total_charge_usd, is_pay_per_event, per_event_prices). A fake that invents its own
shape proves the author's belief, and this park has paid for that lesson four times.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
from types import SimpleNamespace

from PIL import Image

from apify._charging import ActorPricingInfo, ChargeResult

from src import main as mod

# run() below swaps mod.download for a scripted one, so the unit probe of the real
# download keeps its own reference: calling mod.download after any run would test the
# script instead of the code.
REAL_DOWNLOAD = mod.download

RUN = "test-run"


def png_bytes(color=(250, 250, 250)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (60, 40), color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeDataset:
    def __init__(self):
        self.rows: list[dict] = []
        self.refuse_free = 0
        self.paid_mode = "ok"  # ok | refuse_after_write | refuse_before_write
        self.paid_rows: list[dict] = []

    async def push_data(self, row):
        if self.refuse_free:
            self.refuse_free -= 1
            raise RuntimeError("dataset refused the free row")
        self.rows.append(row)

    async def get_metadata(self):
        return SimpleNamespace(item_count=len(self.rows))


class FakeStore:
    def __init__(self):
        self.data: dict = {}

    async def set_value(self, key, value):
        self.data[key] = dict(value)


class FakeCharging:
    def __init__(self, info=None, cap=False, raise_on_info=False, push=None):
        self.info = info
        self.cap = cap
        self.raise_on_info = raise_on_info
        self.push = push or (lambda row, name: ChargeResult(False, 1, {name: 1}))

    def get_pricing_info(self):
        if self.raise_on_info:
            raise RuntimeError("pricing endpoint is down")
        return self.info

    def is_event_charge_limit_reached(self, name):
        return self.cap


class FakeActor:
    charging = None
    dataset = None
    named = None
    default = None
    inp: dict = {}
    failed: list[str] = []
    statuses: list[str] = []
    dataset_refusals = 0
    paid_mode = "ok"

    def __init__(self):
        self.rows = self.dataset.rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get_input(self):
        return dict(self.inp)

    async def open_dataset(self):
        return self.dataset

    async def open_key_value_store(self, name=None):
        return self.named if name else self.default

    def get_charging_manager(self):
        return type(self).charging

    @staticmethod
    def get_env():
        return {"run_id": RUN}

    async def push_data(self, row, charged_event_name=None):
        if self.paid_mode == "refuse_after_write":
            self.dataset.rows.append(dict(row))
            raise RuntimeError("charge failed after the row was written")
        if self.paid_mode == "refuse_before_write":
            raise RuntimeError("nothing was written")
        result = self.charging.push(dict(row), charged_event_name)
        if getattr(result, "charged_count", 0) > 0:
            self.dataset.rows.append(dict(row))
        self.dataset.paid_rows.append(dict(row))
        return result

    async def set_status_message(self, text):
        type(self).statuses.append(text)

    async def fail(self, status_message=None, **kwargs):
        type(self).failed.append(status_message or "")


def paid_info(prices=None, ppe=True):
    return ActorPricingInfo(
        pricing_model=None,
        max_total_charge_usd=5,
        is_pay_per_event=ppe,
        per_event_prices=prices if prices is not None else {mod.EVENT_NAME: 0.006},
    )


def run(inp, *, charging=None, downloads=None, text="HELLO WORLD", confidence=91.5,
        ocr_error=None, ocr_fail_at=None, paid_mode="ok", refuse_free=0,
        host_verdict=None):
    """One whole run with the outside world replaced by a script."""
    dataset = FakeDataset()
    dataset.refuse_free = refuse_free
    named, default = FakeStore(), FakeStore()
    charging = charging or FakeCharging(info=paid_info())
    FakeActor.dataset = dataset
    FakeActor.named = named
    FakeActor.default = default
    FakeActor.inp = inp
    FakeActor.charging = charging
    FakeActor.paid_mode = paid_mode
    FakeActor.failed = []
    FakeActor.statuses = []

    calls = {"download": 0}

    async def fake_download(client, url):
        calls["download"] += 1
        script = (downloads or {}).get(url, {"data": png_bytes()})
        data = script.get("data")
        status, reason = script.get("status"), script.get("reason")
        if status is None and data is not None and mod.imgs.sniff(data) is None:
            status = mod.ST_NOT_IMAGE
            reason = ("the link answered with an html page, not an image: not delivered "
                      "and not charged")
        return {"data": data, "final_url": url, "hops": [url],
                "status": status, "reason": reason, "content_type": "image/png"}

    seen_ocr = {"n": 0}

    def fake_recognize(png, lang, want_words, timeout):
        seen_ocr["n"] += 1
        if ocr_error:
            raise ocr_error
        if ocr_fail_at and seen_ocr["n"] == ocr_fail_at:
            raise RuntimeError("the recogniser died on this picture")
        words = [{"text": "HELLO", "x": 1, "y": 2, "w": 3, "h": 4, "confidence": 91.0}] \
            if want_words else []
        return {"text": text, "confidence": confidence, "words": words}

    mod.Actor = FakeActor()
    mod.download = fake_download
    mod.ocr.recognize = fake_recognize
    mod.ocr.available = lambda: (True, "tesseract at /usr/bin/tesseract")
    mod.ocr.languages = lambda: {"eng", "rus", "ukr"}
    if host_verdict is not None:
        mod.host_verdict = host_verdict
    else:
        mod.host_verdict = lambda host: (
            (False, "private", "the address 127.0.0.1 is not a public address")
            if host in ("127.0.0.1", "10.0.0.5", "100.64.0.1", "localhost")
            else (True, "ok", None))
    os.environ.pop("ACTOR_TIMEOUT_AT", None)
    asyncio.run(mod.main())
    summary = named.data.get(f"RUN_SUMMARY-{RUN}") or {}
    return SimpleNamespace(dataset=dataset, named=named, default=default,
                           summary=summary, calls=calls, failed=FakeActor.failed,
                           statuses=FakeActor.statuses, actor=FakeActor)


def one(url="https://example.com/a.png"):
    return {"imageUrls": [url]}


def test_empty_input_is_a_refusal_not_a_green_zero():
    got = run({"imageUrls": []})
    assert got.failed, "empty input must fail the run"
    assert "empty" in got.failed[0], got.failed[0]
    assert got.dataset.rows == []


def test_one_image_is_read_and_charged_once():
    got = run(one())
    rows = got.dataset.rows
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["status"] == "OK", f"status is {row['status']}"
    assert row["billable"] is True, "a delivered picture must claim its price"
    assert row["text"] == "HELLO WORLD" and row["chars"] == 11
    assert row["words"] and row["words"][0]["text"] == "HELLO"
    assert got.summary["chargedEvents"] == 1 and got.summary["deliveredImages"] == 1
    assert got.summary["billingActive"] is True


def test_a_picture_without_text_is_free():
    got = run(one(), text="   ")
    row = got.dataset.rows[0]
    assert row["status"] == "NOT_CHECKED_NO_TEXT", f"status is {row['status']}"
    assert row["billable"] is False, "a picture with no text must not be billable"
    assert got.summary["chargedEvents"] == 0
    assert got.summary["chargedImages"] == 0, "no text means no money"
    assert got.summary["imagesProcessed"] == 1
    assert got.summary["deliveredImages"] == 1, "the row still reached the buyer"
    assert got.summary["billingActive"] is True, "a run without text is not a bill off run"


def test_html_page_is_not_an_image_and_costs_nothing():
    url = "https://example.com/login"
    got = run(one(url), downloads={url: {"data": b"<html><body>login</body></html>"}})
    row = got.dataset.rows[0]
    assert row["status"] == "NOT_CHECKED_NOT_AN_IMAGE", row
    assert row["billable"] is False
    assert got.summary["chargedEvents"] == 0
    assert got.summary["imagesNotChecked"] == 1


def test_broken_image_keeps_its_own_status():
    url = "https://example.com/broken.jpg"
    got = run(one(url), downloads={url: {"data": b"\xff\xd8\xff\xe0" + b"\x00" * 60}})
    row = got.dataset.rows[0]
    assert row["status"] == "NOT_CHECKED_BROKEN_IMAGE", row
    assert got.summary["chargedEvents"] == 0


def test_the_same_url_twice_is_fetched_once():
    url = "https://example.com/a.png"
    got = run({"imageUrls": [url, url]})
    assert got.calls["download"] == 1, got.calls
    statuses = [r["status"] for r in got.dataset.rows]
    assert statuses == ["OK", "NOT_CHECKED_DUPLICATE"], statuses
    assert got.summary["chargedEvents"] == 1


def test_a_line_that_is_not_a_url_is_free():
    got = run({"imageUrls": ["not a url", ""]})
    statuses = [r["status"] for r in got.dataset.rows]
    assert statuses == ["NOT_CHECKED_INVALID", "NOT_CHECKED_INVALID"], statuses
    assert got.calls["download"] == 0
    assert got.summary["linesThatAreNotImages"] == 2


def test_max_images_cuts_the_list_and_the_counters_close():
    urls = [f"https://example.com/{i}.png" for i in range(3)]
    got = run({"imageUrls": urls, "maxImages": 1})
    assert got.calls["download"] == 1, got.calls
    assert got.summary["imagesAttempted"] == 1
    assert got.summary["linesThatAreNotImages"] == 2
    assert got.summary["requested"] == 3
    assert got.summary["imagesProcessed"] + got.summary["imagesNotChecked"] == 1
    assert "imageMismatch" not in got.summary["byStatus"]


def test_the_cap_stops_the_run_and_the_tail_is_free():
    charging = FakeCharging(info=paid_info(), push=lambda row, name: ChargeResult(True, 0, {name: 0}))
    urls = ["https://example.com/1.png", "https://example.com/2.png"]
    got = run({"imageUrls": urls}, charging=charging)
    statuses = [r["status"] for r in got.dataset.rows]
    assert statuses[0] == "NOT_CHECKED_CAP", statuses
    assert statuses[1] in ("NOT_CHECKED_CAP", "NOT_CHECKED_TIME", "NOT_CHECKED_ERROR"), statuses
    assert got.summary["chargedEvents"] == 0
    assert got.summary["stopReason"] == "spending-cap", got.summary["stopReason"]


def test_a_charge_failure_does_not_write_a_second_row():
    got = run(one(), paid_mode="refuse_after_write")
    assert len(got.dataset.rows) == 1, got.dataset.rows
    assert got.summary["deliveredUncharged"] == 1
    assert got.summary["stopReason"] == "charge-error"
    assert got.summary["chargedEvents"] == 0


def test_an_unreadable_price_stops_before_any_download():
    charging = FakeCharging(raise_on_info=True)
    got = run(one(), charging=charging)
    assert got.calls["download"] == 0
    assert got.dataset.rows[0]["status"] == "NOT_CHECKED_BILLING"
    assert got.summary["stopReason"] == "billing-price-unknown"


def test_a_run_that_is_not_sold_per_event_delivers_free():
    charging = FakeCharging(info=paid_info(ppe=False, prices={}))
    got = run(one(), charging=charging)
    row = got.dataset.rows[0]
    assert row["status"] == "OK" and row["billable"] is False
    assert got.summary["chargedEvents"] == 0 and got.summary["billingActive"] is False


def test_an_event_missing_from_the_price_list_is_free():
    charging = FakeCharging(info=paid_info(prices={"other-event": 0.01}))
    got = run(one(), charging=charging)
    assert got.dataset.rows[0]["billable"] is False
    assert got.summary["chargedEvents"] == 0


def test_the_deadline_closes_the_line_before_the_download():
    from datetime import datetime, timedelta, timezone
    os.environ["ACTOR_TIMEOUT_AT"] = (
        datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    try:
        dataset = FakeDataset()
        FakeActor.dataset = dataset
        FakeActor.named, FakeActor.default = FakeStore(), FakeStore()
        FakeActor.inp = one()
        FakeActor.charging = FakeCharging(info=paid_info())
        FakeActor.failed, FakeActor.statuses = [], []
        mod.Actor = FakeActor()
        calls = {"n": 0}

        async def fake_download(client, url):
            calls["n"] += 1
            return {"data": png_bytes(), "final_url": url, "hops": [url],
                    "status": None, "reason": None, "content_type": "image/png"}

        mod.download = fake_download
        mod.ocr.available = lambda: (True, "ok")
        mod.ocr.languages = lambda: {"eng"}
        mod.host_verdict = lambda host: (True, "ok", None)
        asyncio.run(mod.main())
        assert calls["n"] == 0, calls
        assert dataset.rows[0]["status"] == "NOT_CHECKED_TIME", dataset.rows[0]
    finally:
        os.environ.pop("ACTOR_TIMEOUT_AT", None)


def test_a_private_host_is_refused_before_the_request():
    got = run(one("http://127.0.0.1/secret.png"))
    assert got.calls["download"] == 0
    assert got.dataset.rows[0]["status"] == "NOT_CHECKED_PRIVATE_ADDRESS"
    assert got.summary["chargedEvents"] == 0


def test_word_boxes_can_be_turned_off():
    got = run({"imageUrls": ["https://example.com/a.png"], "includeWordBoxes": False})
    assert got.dataset.rows[0]["words"] == []
    assert got.dataset.rows[0]["text"] == "HELLO WORLD"


def test_the_summary_lives_under_the_run_key_and_has_a_bare_copy():
    got = run(one())
    assert f"RUN_SUMMARY-{RUN}" in got.named.data
    assert "RUN_SUMMARY" in got.default.data
    assert "RUN_SUMMARY" not in got.named.data, "the bare key belongs to the run store"
    assert got.summary["runId"] == RUN
    assert got.summary["engine"].startswith("tesseract")


def test_the_engine_missing_is_a_refusal():
    dataset = FakeDataset()
    FakeActor.dataset = dataset
    FakeActor.named, FakeActor.default = FakeStore(), FakeStore()
    FakeActor.inp = one()
    FakeActor.charging = FakeCharging(info=paid_info())
    FakeActor.failed, FakeActor.statuses = [], []
    mod.Actor = FakeActor()
    mod.ocr.available = lambda: (False, "the tesseract binary is not installed")
    asyncio.run(mod.main())
    assert FakeActor.failed and "tesseract" in FakeActor.failed[0]
    assert dataset.rows == []


def test_an_unexpected_error_still_closes_every_row():
    urls = [f"https://example.com/{i}.png" for i in range(3)]
    got = run({"imageUrls": urls}, ocr_fail_at=2)
    statuses = [r["status"] for r in got.dataset.rows]
    assert len(statuses) == 3, statuses
    assert statuses[0] == "OK", statuses
    assert statuses[1] == "NOT_CHECKED_ERROR", statuses
    assert statuses[2] == "NOT_CHECKED_ERROR", statuses
    assert got.summary["imagesProcessed"] == 1, got.summary["imagesProcessed"]
    assert got.summary["imagesNotChecked"] == 2, got.summary["imagesNotChecked"]
    assert "imageMismatch" not in got.summary["byStatus"], got.summary["byStatus"]
    assert got.failed, "a run that died on an unexpected error must fail"


def test_a_charge_that_never_wrote_a_row_is_not_called_delivered():
    got = run(one(), paid_mode="refuse_before_write")
    assert got.dataset.rows == [], got.dataset.rows
    assert got.summary["deliveredUncharged"] == 0, got.summary["deliveredUncharged"]
    assert got.summary["deliveredUnconfirmed"] == 1, got.summary["deliveredUnconfirmed"]
    assert got.summary["deliveredImages"] == 0, got.summary["deliveredImages"]
    assert got.summary["stopReason"] == "charge-error"
    assert got.summary["chargedEvents"] == 0


def test_a_redirect_into_a_private_network_is_never_requested():
    calls = []

    class FakeResponse:
        is_redirect = True
        status_code = 302
        url = "https://example.com/go"
        headers = {"location": "http://127.0.0.1:8765/secret.png",
                   "content-type": "text/html"}

        async def aiter_bytes(self):
            yield b""

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *args):
            return False

    class FakeClient:
        def stream(self, method, url):
            calls.append(url)
            return FakeStream()

    real = mod.host_verdict
    mod.host_verdict = lambda host: (
        (False, "private", "the address 127.0.0.1 is not a public address")
        if host == "127.0.0.1" else (True, "ok", None))
    try:
        got = asyncio.run(REAL_DOWNLOAD(FakeClient(), "https://example.com/go"))
    finally:
        mod.host_verdict = real
    assert got["status"] == "NOT_CHECKED_PRIVATE_ADDRESS", got
    assert got["data"] is None, "a refused hop must not deliver bytes"
    assert calls == ["https://example.com/go"], f"the private hop was requested: {calls}"


def test_a_charge_error_keeps_the_row_it_wrote_and_does_not_double_it():
    got = run(one(), paid_mode="refuse_after_write")
    assert len(got.dataset.rows) == 1, got.dataset.rows
    assert got.summary["deliveredUncharged"] == 1, got.summary["deliveredUncharged"]
    assert got.summary["deliveredUnconfirmed"] == 0
    assert got.summary["deliveredImages"] == 1
    assert got.summary["chargedImages"] == 0
    assert got.summary["rowsWritten"] == 1, got.summary["rowsWritten"]


def run_all() -> int:
    failed = 0
    names = [n for n in sorted(globals()) if n.startswith("test_")]
    for name in names:
        try:
            globals()[name]()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(names)} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_all())
