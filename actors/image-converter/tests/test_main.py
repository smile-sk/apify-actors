"""Money path and refusals for the image converter.

Run: python tests/test_main.py

The Actor here is a fake that repeats the real SDK on purpose:
  * the paid push is `Actor.push_data(data, *, charged_event_name=...)` and it answers
    with a real `ChargeResult` imported from apify._charging;
  * `Dataset.push_data(data, dataset_id=None)` takes no event name at all;
  * `get_pricing_info()` and `is_event_charge_limit_reached()` are synchronous;
  * `get_metadata()` returns an object with `item_count`, not a dict;
  * `open_key_value_store()` and `set_value(key, value, *, content_type=None)` are
    coroutines with the real keyword name.
A fake that invents its own shapes hides the defects it is meant to catch.

The images are made with Pillow, so the conversion, the signature check and the
refusal branches run on real bytes. The network is a stub: no test touches the wire.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402
from apify._charging import ChargeResult  # noqa: E402  the real shape, not ours

import src.main as app  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def make_png(size=(40, 20), mode="RGB", colour=(20, 90, 200)) -> bytes:
    image = Image.new(mode, size, colour)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def make_jpeg(size=(40, 20), colour=(200, 40, 40)) -> bytes:
    image = Image.new("RGB", size, colour)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()


class PricingInfo:
    def __init__(self, is_ppe=True, names=("image-converted",)) -> None:
        self.is_pay_per_event = is_ppe
        self.per_event_prices = {name: 0.002 for name in names}


class Budget:
    def __init__(self, limit=None) -> None:
        self.limit = limit
        self.charged = 0

    def exhausted(self) -> bool:
        return self.limit is not None and self.charged >= self.limit


class FakeBackend:
    def __init__(self, script=None, metadata_count=None, budget=None) -> None:
        self.rows: list[dict] = []
        self.script = list(script or [])
        self.metadata_count = metadata_count
        self.budget = budget or Budget()
        self.charge_names: list[str | None] = []

    def _next(self):
        return self.script.pop(0) if self.script else None

    async def push_data(self, data, dataset_id=None):
        # The real Dataset.push_data takes a dataset id and no event name: a paid row
        # has to go through Actor.push_data. A fake that swallows the event name hides
        # the one mistake that would make every paid row fail on the platform.
        if self._next() == "raise_before":
            raise RuntimeError("dataset refused the row")
        self.rows.append(data)
        return None

    async def get_metadata(self):
        count = len(self.rows) if self.metadata_count is None else self.metadata_count
        return SimpleNamespace(item_count=count)

    async def push_charged(self, data, event_name):
        self.charge_names.append(event_name)
        action = self._next()
        if action == "raise_before":
            raise RuntimeError("dataset refused the row")
        if action == "written_then_charge_fails":
            self.rows.append(data)
            raise RuntimeError("charge failed")
        if self.budget.exhausted():
            return ChargeResult(event_charge_limit_reached=True, charged_count=0,
                                chargeable_within_limit={})
        self.rows.append(data)
        self.budget.charged += 1
        return ChargeResult(event_charge_limit_reached=self.budget.exhausted(),
                            charged_count=1, chargeable_within_limit={})


class FakeCharging:
    def __init__(self, info=None, raises=False, budget=None) -> None:
        self.info = info
        self.raises = raises
        self.budget = budget or Budget()

    def get_pricing_info(self):
        if self.raises:
            raise RuntimeError("pricing endpoint is down")
        return self.info

    def is_event_charge_limit_reached(self, event_name: str) -> bool:
        return self.budget.exhausted()


RECORD_KEY = re.compile(r"^[A-Za-z0-9!\-_.'()]{1,256}$")


class FakeInvalidRequestError(Exception):
    """Same name and text as apify_client.InvalidRequestError on a 400 from the store.

    The platform refuses a record key outside letters, digits and !-_.'() : `images/<name>`
    was refused live with 400 invalid-record-key, and every delivered image silently became
    NOT_CHECKED_STORE_ERROR. A fake that accepts any key hides exactly that defect.
    """


class FakeStore:
    def __init__(self, refuse=None) -> None:
        self.values: dict[str, bytes] = {}
        self.refuse = refuse or (lambda key: False)

    async def set_value(self, key, value, *, content_type=None):
        if not RECORD_KEY.match(key or ""):
            raise FakeInvalidRequestError(
                "invalid-record-key: This record key can have up to 256 characters. "
                "Use only a-zA-Z0-9!-_.'()")
        if self.refuse(key):
            raise RuntimeError("store refused the record")
        self.values[key] = value


class FakeFail(Exception):
    pass


class FakeActor:
    def __init__(self, input_data, backend, charging, store_refuse=None) -> None:
        self._input = input_data
        self._backend = backend
        self._charging = charging
        self.store_refuse = store_refuse
        self.stores: list[FakeStore] = []
        self.status_messages: list[str] = []
        self.fail_message: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get_input(self):
        return self._input

    async def set_status_message(self, text, *_a, **_k):
        self.status_messages.append(text)

    async def open_dataset(self):
        return self._backend

    async def push_data(self, data, *, charged_event_name=None):
        return await self._backend.push_charged(data, charged_event_name)

    def get_charging_manager(self):
        return self._charging

    async def open_key_value_store(self, name=None):
        store = FakeStore(self.store_refuse)
        store.name = name
        self.stores.append(store)
        return store

    def get_env(self):
        return {"run_id": "test-run"}

    async def fail(self, *, status_message=None, exit_code=1, exception=None):
        self.fail_message = status_message
        raise FakeFail(status_message)

    def summary(self) -> dict:
        for store in self.stores:
            if "RUN_SUMMARY-test-run" in store.values:
                return store.values["RUN_SUMMARY-test-run"]
        return {}

    def files(self) -> dict:
        return self.stores[0].values if self.stores else {}


def run_main(lines=None, blobs=None, **kwargs):
    blobs = dict(blobs or {})
    if lines is None:
        lines = list(blobs)
    budget = kwargs.get("budget") or Budget()
    backend = FakeBackend(budget=budget, script=kwargs.get("script"),
                          metadata_count=kwargs.get("metadata_count"))
    charging = FakeCharging(budget=budget)
    pricing = kwargs.get("pricing", "ok")
    if pricing == "ok":
        charging.info = PricingInfo()
    elif pricing == "raises":
        charging.raises = True
    elif pricing == "not_ppe":
        charging.info = PricingInfo(is_ppe=False)
    elif pricing == "other_event":
        charging.info = PricingInfo(names=("some-other-event",))

    payload = {"imageUrls": list(lines)}
    payload.update(kwargs.get("extra") or {})

    calls = {"download": []}

    async def fake_download(client, url):
        calls["download"].append(url)
        got = blobs.get(url)
        if callable(got):
            # a slow or failing server on demand: the caller decides how long it takes
            got = await got(url)
        if isinstance(got, dict):
            return {"data": got.get("data"), "final_url": got.get("final_url", url),
                    "hops": got.get("hops") or [url], "status": got.get("status"),
                    "reason": got.get("reason"),
                    "content_type": got.get("content_type", "image/png")}
        return {"data": got, "final_url": url, "hops": [url], "status": None,
                "reason": None, "content_type": "image/png"}

    host_public = kwargs.get("host_public") or (lambda host: (True, None, None))
    actor = FakeActor(payload, backend, charging, kwargs.get("store_refuse"))
    original = (app.Actor, app.download, app.host_verdict)
    app.Actor = actor
    app.download = fake_download
    app.host_verdict = host_public
    env = kwargs.get("env") or {}
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        try:
            asyncio.run(app.main())
        except FakeFail:
            pass
    finally:
        app.Actor, app.download, app.host_verdict = original
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return actor.summary(), backend, actor, calls


def test_two_images_are_converted_and_charged():
    png = make_png()
    summary, backend, actor, calls = run_main(blobs={
        "https://a.test/one.png": png,
        "https://b.test/two.png": make_png((60, 30)),
    })
    equal(summary["requested"], 2, "two lines asked")
    equal(summary["processedImages"], 2, "two images decoded")
    equal(summary["deliveredImages"], 2, "two files delivered")
    equal(summary["chargedEvents"], 2, "two events charged")
    equal(summary["rowsWritten"], 2, "two rows written")
    equal(summary["notCheckedImages"], 0, "nothing left unchecked")
    equal(backend.charge_names, ["image-converted"] * 2, "every paid row names the event")
    ok(all(row["billable"] is True for row in backend.rows), "paid rows say billable")
    ok(all(row["status"] == "OK" for row in backend.rows), "paid rows are OK")
    ok(all(row["outputKey"].startswith("images_") for row in backend.rows),
       "every row says where its file lives")
    equal(len(actor.files()) - 1, 2, "two files plus the zip are in the store")
    equal(summary["imagesInZip"], 2, "both files are in the zip")
    ok(summary["zipBytes"] > 0, "the zip has a size")
    ok(summary["savedBytesTotal"] == summary["bytesInTotal"] - summary["bytesOutTotal"],
       "the saved bytes are the difference, not a guess")
    ok(actor.fail_message is None, "a delivered run does not fail")
    ok(actor.status_messages and "2" in actor.status_messages[-1],
       "the header reports the numbers")


def test_spending_cap_stops_and_tail_is_free():
    png = make_png()
    summary, backend, actor, calls = run_main(blobs={
        "https://a.test/1.png": png,
        "https://b.test/2.png": png,
        "https://c.test/3.png": png,
    }, budget=Budget(limit=1))
    equal(summary["deliveredImages"], 1, "only one file is delivered")
    equal(summary["chargedEvents"], 1, "and only one event is charged")
    equal(summary["stopReason"], "spending-cap", "the run says why it stopped")
    equal(summary["byStatus"].get("NOT_CHECKED_CAP"), 2, "both tail rows are CAP")
    equal(len(calls["download"]), 1, "the cap stops the run at once, the tail is never read")
    ok(actor.fail_message is None, "one delivered file is not a failed run")


def test_cap_from_the_start_fails_with_a_reason():
    summary, backend, actor, calls = run_main(blobs={"https://a.test/1.png": make_png()},
                                              budget=Budget(limit=0))
    equal(summary["deliveredImages"], 0, "nothing is delivered")
    equal(summary["chargedEvents"], 0, "nothing is charged")
    equal(summary["byStatus"].get("NOT_CHECKED_CAP"), 1, "the row says the cap was hit")
    equal(calls["download"], [], "no request is made when the cap is already reached")
    ok(actor.fail_message and "Nothing could be converted" in actor.fail_message,
       "an empty run fails with a plain reason")


def test_unreadable_price_delivers_nothing():
    summary, backend, actor, calls = run_main(blobs={"https://a.test/1.png": make_png()},
                                              pricing="raises")
    equal(summary["deliveredImages"], 0, "an unreadable price delivers nothing")
    equal(summary["billingActive"], False, "and the summary says billing is off")
    equal(summary["stopReason"], "billing-price-unknown", "the stop reason is named")
    equal(calls["download"], [], "nothing is downloaded before the price is known")
    ok(summary["billingReason"].startswith("pricing read failed"),
       "the reason says what failed")


def test_not_ppe_delivers_free():
    summary, backend, actor, calls = run_main(blobs={"https://a.test/1.png": make_png()},
                                              pricing="not_ppe")
    equal(summary["deliveredImages"], 1, "a free run still delivers")
    equal(summary["chargedEvents"], 0, "and charges nothing")
    equal(len(backend.rows), 1, "the row is written")
    equal(backend.rows[0]["billable"], False, "and it claims no money")
    equal(backend.charge_names, [], "the paid door is never used")


def test_event_not_in_the_price_list_is_free_and_named():
    summary, backend, actor, calls = run_main(blobs={"https://a.test/1.png": make_png()},
                                              pricing="other_event")
    equal(summary["chargedEvents"], 0, "no event, no money")
    equal(summary["deliveredImages"], 1, "the file is still delivered")
    ok("is not priced" in summary["billingReason"],
       f"the reason names the missing event, got {summary['billingReason']!r}")


def test_html_is_not_an_image_and_is_free():
    summary, backend, actor, calls = run_main(blobs={
        "https://a.test/page": b"<!doctype html><html><body>login</body></html>",
    })
    equal(summary["byStatus"].get("NOT_CHECKED_NOT_AN_IMAGE"), 1,
          "an html page gets its own free status")
    equal(summary["chargedEvents"], 0, "and nothing is charged for it")
    equal(summary["notAnImage"], 1, "the counter names the reason")
    equal(backend.charge_names, [], "the paid door is never used for html")


def test_duplicate_blank_and_invalid_lines_are_free_and_counted():
    png = make_png()
    summary, backend, actor, calls = run_main(lines=[
        "https://a.test/1.png", "", "not a url at all", "https://a.test/1.png",
    ], blobs={"https://a.test/1.png": png})
    equal(summary["requested"], 4, "four lines were sent")
    equal(summary["duplicates"], 1, "one repeat")
    equal(summary["blankLines"], 1, "one blank line")
    equal(summary["invalidLines"], 1, "one line that is not a url")
    equal(summary["linesThatAreNotImages"], 3, "three lines were never image requests")
    equal(summary["imagesAttempted"], 1, "and one was")
    equal(summary["deliveredImages"], 1, "it is delivered once")
    equal(summary["chargedEvents"], 1, "and charged once")
    ok("imageMismatch" not in summary["byStatus"], "the image count closes")
    ok("lineCountMismatch" not in summary["byStatus"], "the line count closes")


def test_limit_cut_is_visible_and_keeps_the_invariant():
    png = make_png()
    summary, backend, actor, calls = run_main(blobs={
        "https://a.test/1.png": png,
        "https://b.test/2.png": png,
        "https://c.test/3.png": png,
    }, extra={"maxImages": 2})
    equal(summary["cutByMaxImages"], 1, "one line is cut by the limit")
    equal(summary["imagesAttempted"], 2, "two images were attempted")
    equal(summary["deliveredImages"], 2, "two are delivered")
    equal(len(calls["download"]), 2, "the cut line is never downloaded")
    ok("imageMismatch" not in summary["byStatus"], "the cut does not break the count")
    ok("lineCountMismatch" not in summary["byStatus"], "and neither does the line count")


def test_store_error_is_free_and_not_charged():
    summary, backend, actor, calls = run_main(
        blobs={"https://a.test/1.png": make_png()},
        store_refuse=lambda key: key.startswith("images_"))
    equal(summary["byStatus"].get("NOT_CHECKED_STORE_ERROR"), 1,
          "a file that cannot be stored has its own status")
    equal(summary["deliveredImages"], 0, "nothing was delivered")
    equal(summary["chargedEvents"], 0, "and nothing was charged")
    equal(backend.charge_names, [], "the paid door is never used without a file")
    ok(actor.fail_message, "a run with nothing delivered fails")


def test_charge_error_keeps_the_file_and_stops():
    summary, backend, actor, calls = run_main(blobs={
        "https://a.test/1.png": make_png(),
        "https://b.test/2.png": make_png(),
    }, script=["written_then_charge_fails"])
    equal(summary["stopReason"], "charge-error", "the run stops on a failed charge")
    equal(summary["deliveredUncharged"], 1, "the file is named as delivered but not charged")
    equal(summary["chargedEvents"], 0, "no money is claimed")
    stored = [key for key in actor.files() if key.startswith("images_")]
    equal(len(stored), 1, "the converted file is in the store and can be downloaded")
    paid = [row for row in backend.rows if row.get("status") == "OK"]
    equal(len(paid), 1, "there is exactly one delivered row, not a second one on top")
    equal(summary["byStatus"].get("NOT_CHECKED_ERROR"), 1,
          "the tail comes back as ERROR with the reason, free")
    equal(len(calls["download"]), 1, "the tail is not read after the stop")
    header = actor.status_messages[0] if actor.status_messages else ""
    equal("and charged" in header, False,
          "the header never claims a charge that did not happen")
    equal("delivered without a charge" in header, True,
          "it names the delivery and the missing charge")


def test_unexpected_error_closes_the_buckets_and_names_itself():
    """F2: a crash must not leave the counters open or blame the buyer's lines."""
    # sniff sits outside the per-file try: a crash there is a run level crash,
    # exactly the case where the counters used to stay open.
    original = app.imgs.sniff

    def boom(*_a, **_k):
        raise RuntimeError("sniffer exploded")

    app.imgs.sniff = boom
    try:
        summary, backend, actor, calls = run_main(blobs={
            "https://a.test/1.png": make_png(),
            "https://b.test/2.png": make_png(),
        })
    finally:
        app.imgs.sniff = original
    equal(summary["stopReason"], "unexpected-error", "the run names the crash")
    equal(summary["chargedEvents"], 0, "nothing is charged")
    equal(summary["unexpectedError"] is not None, True, "the reason travels in the summary")
    equal(summary["byStatus"].get("imageMismatch"), None,
          "the buckets close by definition, no imageMismatch")
    equal(summary["processedImages"] + summary["notCheckedImages"],
          summary["imagesAttempted"],
          "attempted = processed + notChecked even after a crash")
    equal(actor.fail_message is not None, True, "the run fails with a readable reason")


def test_private_address_is_refused_before_the_request():
    summary, backend, actor, calls = run_main(
        blobs={"https://inside.test/1.png": make_png()},
        host_public=lambda host: (False, app.CODE_PRIVATE,
                                     "the address 10.0.0.5 is not a public address"))
    equal(summary["privateBlocked"], 1, "the private address is counted")
    equal(summary["byStatus"].get("NOT_CHECKED_PRIVATE_ADDRESS"), 1, "with its own status")
    equal(calls["download"], [], "the request is never made")
    equal(summary["chargedEvents"], 0, "and nothing is charged")


def test_the_guard_refuses_a_private_hop_before_any_request_goes_out():
    """The guard lives inside download now: it must refuse before the client is used.

    `client` here is a bare object on purpose: if the code touched it, the test would
    crash with AttributeError instead of quietly reaching the network.
    """
    refused = lambda host: (False, app.CODE_PRIVATE,
                            "the address 10.0.0.5 is not a public address")
    original = app.host_verdict
    app.host_verdict = refused
    try:
        got = asyncio.run(app.download(client=object(), url="https://inside.test/1.png"))
    finally:
        app.host_verdict = original
    equal(got["status"], app.ST_PRIVATE, "the private hop gets its own status")
    equal(got["data"], None, "not a single byte is read")
    equal(got["hops"], ["https://inside.test/1.png"], "the chain stops at the refused hop")


def test_deadline_stops_before_the_image():
    soon = (datetime.now(timezone.utc) + timedelta(seconds=4)).isoformat()
    summary, backend, actor, calls = run_main(blobs={"https://a.test/1.png": make_png()},
                                              env={"ACTOR_TIMEOUT_AT": soon})
    equal(summary["deadlineCapped"], True, "the run says the deadline capped it")
    equal(summary["byStatus"].get("NOT_CHECKED_TIME"), 1, "the row is TIME")
    equal(calls["download"], [], "nothing is downloaded with four seconds left")
    equal(summary["chargedEvents"], 0, "and nothing is charged")


def test_slow_download_is_cut_by_the_deadline_before_the_platform_kills_it():
    """B1: dl+convert must be cut by the time left, not by a per-read timeout.

    The margins and the floor are shrunk here so the test lasts seconds, not minutes:
    two seconds of budget against a server that stays silent for three. Without the
    asyncio.timeout the run would deliver and charge, and on the platform those seconds
    are a kill with no summary and no rows for the tail.
    """
    stop = datetime.now(timezone.utc) + timedelta(seconds=6)
    saved = (app.IMAGE_MARGIN_S, app.MIN_DOWNLOAD_BUDGET_S)
    app.IMAGE_MARGIN_S, app.MIN_DOWNLOAD_BUDGET_S = 3.0, 1.0

    async def crawling(url):
        await asyncio.sleep(3.0)
        return make_png()

    try:
        summary, backend, actor, calls = run_main(
            blobs={"https://a.test/1.png": crawling, "https://b.test/2.png": make_png()},
            env={"ACTOR_TIMEOUT_AT": stop.isoformat()})
    finally:
        app.IMAGE_MARGIN_S, app.MIN_DOWNLOAD_BUDGET_S = saved
    equal(summary["deliveredImages"], 0, "nothing is delivered: the file did not arrive in time")
    equal(summary["chargedEvents"], 0, "and nothing is charged")
    equal(summary["stopReason"], "deadline", "the run stops by its own clock")
    equal(summary["byStatus"].get(app.ST_TIME), 2, "the file and the tail are named TIME, free")
    equal(len(calls["download"]), 1, "the tail is not read after the stop")


def test_empty_input_fails():
    summary, backend, actor, calls = run_main(lines=[])
    equal(summary["requested"], 0, "no lines asked")
    ok(actor.fail_message and "No image urls" in actor.fail_message,
       "an empty run fails with a plain reason")


def test_zip_error_does_not_undo_delivery():
    summary, backend, actor, calls = run_main(
        blobs={"https://a.test/1.png": make_png()},
        store_refuse=lambda key: key == "converted-images.zip")
    equal(summary["deliveredImages"], 1, "the file is still delivered")
    equal(summary["chargedEvents"], 1, "and still charged")
    equal(summary["imagesInZip"], 0, "the archive is not claimed")
    ok(summary["zipError"] and "outputKey" in summary["zipError"],
       "the zip failure names where the files are instead")


def test_wrong_format_stops_before_any_request():
    summary, backend, actor, calls = run_main(blobs={"https://a.test/1.png": make_png()},
                                              extra={"format": "gif"})
    equal(calls["download"], [], "an unsupported target stops before the first request")
    equal(summary.get("requested", 0), 0, "no summary row for a refused run")
    ok(actor.fail_message and "cannot be written" in actor.fail_message,
       "the reason says the format cannot be written")


def test_store_keys_fit_the_platform_rule():
    for name in ("\u0437\u0430\u043a\u0430\u0442 \u043d\u0430\u0434 \u0440\u0435\u043a\u043e\u0439.png",
                 "a b/c%d.webp", "x" * 300, "", "!!!.jpg"):
        key = app.store_key(name)
        ok(bool(RECORD_KEY.match(key)), f"a record key the platform accepts: {key!r}")
        ok("/" not in key, "a record key carries no slash")
    ok(bool(RECORD_KEY.match(app.ZIP_KEY)), f"the archive key is a record key: {app.ZIP_KEY}")


def test_a_delivered_image_key_is_a_record_key():
    summary, backend, actor, calls = run_main(blobs={"https://a.test/1.png": make_png()})
    equal(summary["byStatus"].get("NOT_CHECKED_STORE_ERROR", 0), 0,
          "the store accepted the key of a delivered image")
    equal(summary["deliveredImages"], 1, "the image is delivered")
    equal(summary["chargedEvents"], 1, "and paid for")

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
