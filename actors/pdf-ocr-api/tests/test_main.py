"""Money path and refusals for the PDF OCR actor.

Run: python tests/test_main.py

The Actor here is a fake that repeats the real SDK on purpose:
  * the paid push is `Actor.push_data(data, *, charged_event_name=...)` and it answers
    with a real `ChargeResult` imported from apify._charging;
  * `Dataset.push_data(data, dataset_id=None)` takes no event name at all;
  * `get_pricing_info()` and `is_event_charge_limit_reached()` are synchronous;
  * `get_metadata()` returns an object with `item_count`, not a dict.
A fake that invents its own shapes hides the defects it is meant to catch.

The network is replaced by a stub that hands over real pdf bytes made with pymupdf, so
the parsing, the page ranges and the refusal branches run on real files. The
recogniser is stubbed as well: tesseract is not installed on the machine that runs
these checks, and pretending otherwise would prove nothing. The engine itself is
proven by the build gate in the Dockerfile, which stops the image when the binary or a
language pack is missing, and by the first live run before publication.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402
from apify._charging import ChargeResult  # noqa: E402  the real shape, not ours

import src.main as app  # noqa: E402
from src import ocr  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def make_pdf(pages: int = 1, text: str = "Invoice") -> bytes:
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page()
        if text:
            page.insert_text((72, 100 + number * 20), f"{text} {number + 1}", fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


def make_blank_pdf(pages: int = 1) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def make_encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    doc.close()
    return data


class PriceItem:
    def __init__(self, event_name: str, price: float) -> None:
        self.event_name = event_name
        self.price = price


class PricingInfo:
    def __init__(self, is_ppe=True, names=("page-ocr",)) -> None:
        self.is_pay_per_event = is_ppe
        self.per_event_prices = [PriceItem(name, 0.006) for name in names]


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


class FakeStore:
    def __init__(self) -> None:
        self.values: dict[str, dict] = {}

    async def set_value(self, key, value):
        self.values[key] = value


class FakeFail(Exception):
    pass


class FakeActor:
    def __init__(self, input_data, backend, charging) -> None:
        self._input = input_data
        self._backend = backend
        self._charging = charging
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
        store = FakeStore()
        self.stores.append(store)
        return store

    def get_env(self):
        return {"run_id": "test-run"}

    async def fail(self, *, status_message=None, exit_code=1, exception=None):
        self.fail_message = status_message
        raise FakeFail(status_message)

    def summary(self) -> dict:
        return self.stores[0].values["RUN_SUMMARY-test-run"]


def run_main(lines=None, pdfs=None, **kwargs):
    pdfs = dict(pdfs or {})
    if lines is None:
        lines = list(pdfs)
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

    payload = {"pdfUrls": list(lines)}
    payload.update(kwargs.get("extra") or {})

    ocr_text = kwargs.get("ocr_text", "Scanned text")
    calls = {"download": [], "ocr": [], "ocr_timeouts": []}

    async def fake_download(client, url):
        calls["download"].append(url)
        got = pdfs.get(url)
        if isinstance(got, dict):
            return {"data": got.get("data"), "final_url": got.get("final_url", url),
                    "hops": got.get("hops") or [url], "status": got.get("status"),
                    "reason": got.get("reason"),
                    "content_type": got.get("content_type", "application/pdf")}
        return {"data": got, "final_url": url, "hops": [url], "status": None,
                "reason": None, "content_type": "application/pdf"}

    def fake_recognize(image_png, lang, want_words, timeout):
        calls["ocr"].append(lang)
        calls["ocr_timeouts"].append(timeout)
        return {"text": ocr_text, "confidence": 88.0, "words": [],
                "error": None}

    host_public = kwargs.get("host_public") or (lambda host: (True, None))
    actor = FakeActor(payload, backend, charging)
    original = (app.Actor, app.download, app.host_is_public,
                ocr.available, ocr.languages, ocr.recognize)
    app.Actor = actor
    app.download = fake_download
    app.host_is_public = host_public
    ocr.available = kwargs.get("ocr_available") or (
        lambda: (True, "tesseract at /usr/bin/tesseract"))
    ocr.languages = kwargs.get("ocr_langs") or (lambda: {"eng", "rus", "ukr", "osd"})
    ocr.recognize = fake_recognize
    env = kwargs.get("env") or {}
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        try:
            asyncio.run(app.main())
        except FakeFail:
            pass
    finally:
        app.Actor, app.download, app.host_is_public, ocr.available, ocr.languages, \
            ocr.recognize = original
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    try:
        summary = actor.summary()
    except Exception:  # noqa: BLE001
        summary = {}
    return summary, backend, actor, calls


def test_three_documents_all_charged():
    pdf = make_pdf(1)
    summary, backend, actor, calls = run_main(pdfs={
        "https://a.test/1.pdf": pdf,
        "https://b.test/2.pdf": pdf,
        "https://c.test/3.pdf": pdf,
    })
    equal(summary["requested"], 3, "three lines asked")
    equal(summary["processedDocuments"], 3, "three documents read")
    equal(summary["deliveredPages"], 3, "three pages delivered")
    equal(summary["chargedEvents"], 3, "three events charged")
    equal(summary["rowsWritten"], 3, "three rows written")
    equal(summary["notCheckedDocuments"], 0, "nothing left unchecked")
    equal(summary["pagesFromTextLayer"], 3, "a digital pdf is read from its text layer")
    equal(summary["pagesFromOcr"], 0, "and its pages never go through OCR")
    equal(calls["ocr"], [], "the recogniser is not called for a text layer")
    equal(backend.charge_names, ["page-ocr"] * 3, "every paid row names the event")
    ok(all(row["billable"] is True for row in backend.rows), "paid rows say billable")
    ok(all(row["status"] == "OK" for row in backend.rows), "paid rows are OK")
    ok(all(row["source"] == "text" for row in backend.rows), "and say where the text came from")
    ok(actor.fail_message is None, "a delivered run does not fail")
    ok(actor.status_messages and "3" in actor.status_messages[-1],
       "the header reports the numbers")


def test_spending_cap_stops_and_tail_is_free():
    pdf = make_pdf(1)
    summary, backend, actor, calls = run_main(pdfs={
        "https://a.test/1.pdf": pdf,
        "https://b.test/2.pdf": pdf,
        "https://c.test/3.pdf": pdf,
    }, budget=Budget(limit=1))
    equal(summary["deliveredPages"], 1, "only one page is delivered")
    equal(summary["chargedEvents"], 1, "and only one event is charged")
    equal(summary["stopReason"], "spending-cap", "the run says why it stopped")
    equal(summary["byStatus"].get("NOT_CHECKED_CAP"), 2, "both tail rows are CAP")
    equal(len(calls["download"]), 1, "the cap stops the run at once, the tail is never downloaded")
    ok(actor.fail_message is None, "one delivered page is not a failed run")


def test_cap_from_the_start_fails_with_a_reason():
    summary, backend, actor, calls = run_main(pdfs={"https://a.test/1.pdf": make_pdf(1)},
                                             budget=Budget(limit=0))
    equal(summary["deliveredPages"], 0, "nothing is delivered")
    equal(summary["chargedEvents"], 0, "nothing is charged")
    equal(summary["stopReason"], "spending-cap", "the cap is named")
    ok(actor.fail_message and "Nothing could be read" in actor.fail_message,
       "a run with nothing delivered fails and says why")
    equal(calls["download"], [], "the cap stops the run before the first download")


def test_charge_failure_after_write_is_named():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/1.pdf": make_pdf(1)},
        script=["written_then_charge_fails"])
    equal(summary["deliveredUncharged"], 1, "the row that may be there is counted")
    equal(summary["chargedEvents"], 0, "no money was taken")
    equal(summary["stopReason"], "charge-error", "the stop reason is the charge")
    equal(summary["rowsWritten"], 1, "and no second row is written for the same page")
    equal(len(backend.rows), 1, "the dataset holds exactly one row")


def test_price_unreadable_delivers_nothing():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/1.pdf": make_pdf(1)}, pricing="raises")
    equal(summary["deliveredPages"], 0, "an unknown price delivers nothing")
    equal(summary["chargedEvents"], 0, "and charges nothing")
    equal(summary["stopReason"], "billing-price-unknown", "the reason is named")
    equal(summary["byStatus"].get("NOT_CHECKED_BILLING"), 1, "the row says billing")
    equal(calls["download"], [], "nothing is downloaded for free either")


def test_not_ppe_delivers_free():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/1.pdf": make_pdf(1)}, pricing="not_ppe")
    equal(summary["billingActive"], False, "billing is off")
    equal(summary["deliveredPages"], 1, "the page is still read and written")
    equal(summary["chargedEvents"], 0, "nothing is charged")
    ok(all(row["billable"] is False for row in backend.rows), "and the row does not claim money")
    equal(backend.charge_names, [], "the paid door is not used at all")


def test_scanned_page_goes_through_ocr():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/scan.pdf": make_blank_pdf(1)},
        extra={"ocrMode": "always"}, ocr_text="Invoice total 100")
    equal(summary["deliveredPages"], 1, "a scan is delivered")
    equal(summary["chargedEvents"], 1, "and charged")
    equal(summary["pagesFromOcr"], 1, "the text came from OCR")
    equal(len(calls["ocr"]), 1, "the recogniser ran once")
    equal(backend.rows[0]["source"], "ocr", "the row says ocr")
    equal(backend.rows[0]["text"], "Invoice total 100", "the recognised text is the row text")
    equal(backend.rows[0]["confidence"], 88.0, "the confidence is kept")


def test_blank_page_is_free():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/blank.pdf": make_blank_pdf(1)},
        extra={"ocrMode": "always"}, ocr_text="")
    equal(summary["deliveredPages"], 0, "a page with no readable text is not delivered")
    equal(summary["chargedEvents"], 0, "and is not charged")
    equal(summary["pagesWithoutText"], 1, "it is counted as a page without text")
    equal(summary["byStatus"].get("NOT_CHECKED_NO_TEXT"), 1, "its row says NO_TEXT")
    ok(actor.fail_message, "a run with nothing delivered fails and says why")


def test_ocr_off_keeps_scans_free():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/scan.pdf": make_blank_pdf(1)}, extra={"ocrMode": "never"})
    equal(calls["ocr"], [], "the recogniser is never called")
    equal(summary["deliveredPages"], 0, "a scan is not delivered when OCR is off")
    equal(summary["byStatus"].get("NOT_CHECKED_NO_TEXT"), 1, "its row says NO_TEXT")


def test_not_a_pdf():
    summary, backend, actor, calls = run_main(pdfs={
        "https://a.test/page": {"data": b"<html>login</html>", "status": "NOT_CHECKED_NOT_PDF",
                                "reason": "the link answered with an html page"}})
    equal(summary["notPdf"], 1, "the html page is counted")
    equal(summary["deliveredPages"], 0, "and not delivered")
    equal(summary["chargedEvents"], 0, "and not charged")
    equal(summary["byStatus"].get("NOT_CHECKED_NOT_PDF"), 1, "its row says NOT_PDF")


def test_encrypted_pdf_is_free():
    summary, backend, actor, calls = run_main(pdfs={
        "https://a.test/secret.pdf": make_encrypted_pdf()})
    equal(summary["encrypted"], 1, "the encrypted document is counted")
    equal(summary["deliveredPages"], 0, "nothing is delivered from it")
    equal(summary["byStatus"].get("NOT_CHECKED_ENCRYPTED"), 1, "its row says ENCRYPTED")


def test_duplicate_and_invalid_lines():
    pdf = make_pdf(1)
    summary, backend, actor, calls = run_main(
        lines=["https://a.test/1.pdf", "https://a.test/1.pdf", "not a url", ""],
        pdfs={"https://a.test/1.pdf": pdf})
    equal(summary["requested"], 4, "four lines asked")
    equal(summary["uniqueDocuments"], 1, "one unique url")
    equal(summary["duplicates"], 1, "one duplicate")
    equal(summary["invalidLines"], 1, "one invalid line")
    equal(summary["blankLines"], 1, "one blank line")
    equal(summary["deliveredPages"], 1, "one page delivered")
    equal(summary["chargedEvents"], 1, "one event charged")
    equal(summary["linesThatAreNotDocuments"], 3, "the three non document lines have a bucket")
    equal(summary["documentsAttempted"], 1, "one line was a document")
    ok("documentMismatch" not in summary["byStatus"], "the counts close on a list with junk")
    equal(summary["byStatus"].get("NOT_CHECKED_DUPLICATE"), 1, "the duplicate has its row")
    equal(summary["byStatus"].get("NOT_CHECKED_INVALID"), 2, "both bad lines have theirs")
    equal(calls["download"], ["https://a.test/1.pdf"], "the url is downloaded once")


def test_max_documents_cut_is_free():
    pdf = make_pdf(1)
    summary, backend, actor, calls = run_main(pdfs={
        "https://a.test/1.pdf": pdf, "https://b.test/2.pdf": pdf},
        extra={"maxDocuments": 1})
    equal(summary["deliveredPages"], 1, "only one document is read")
    equal(summary["cutByMaxDocuments"], 1, "the cut is counted")
    equal(summary["linesThatAreNotDocuments"], 1, "a cut line is not a document either")
    equal(summary["byStatus"].get("NOT_CHECKED_LIMIT"), 1, "the cut row says LIMIT")
    equal(len(calls["download"]), 1, "the second url is never downloaded")


def test_max_pages_per_document():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/3.pdf": make_pdf(3)},
        extra={"maxPagesPerDocument": 1})
    equal(summary["deliveredPages"], 1, "one page is read")
    equal(summary["pagesCutByLimit"], 2, "two are left unread and counted")
    equal(backend.rows[0]["pagesInDocument"], 3, "the row knows the document size")
    equal(backend.rows[0]["pagesLeft"], 2, "and says how many were left")


def test_pages_field():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/3.pdf": make_pdf(3)}, extra={"pages": "2"})
    equal(summary["deliveredPages"], 1, "one page is read")
    equal(backend.rows[0]["page"], 2, "and it is the second one")
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/3.pdf": make_pdf(3)}, extra={"pages": "9"})
    equal(summary["deliveredPages"], 0, "a page beyond the document delivers nothing")
    equal(summary["pagesNotInDocument"], 1, "and the miss is counted")
    equal(summary["byStatus"].get("NOT_CHECKED_NO_SUCH_PAGE"), 1,
          "the row says the page is not in the document")
    ok("NOT_CHECKED_INVALID" not in summary["byStatus"],
       "a good url that points at a short document is not an invalid line")


def test_junk_pages_field_fails_before_any_request():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/3.pdf": make_pdf(3)}, extra={"pages": "abc"})
    equal(calls["download"], [], "nothing is downloaded")
    equal(summary, {}, "no summary is written")
    ok(actor.fail_message and "pages field" in actor.fail_message,
       "the run fails and names the field")


def test_deadline_before_first_document():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/1.pdf": make_pdf(1)},
        env={"ACTOR_TIMEOUT_AT": "2020-01-01T00:00:00Z"})
    equal(summary["deadlineCapped"], True, "the deadline is named")
    equal(summary["deliveredPages"], 0, "nothing is delivered")
    equal(summary["chargedEvents"], 0, "nothing is charged")
    equal(summary["stopReason"], "deadline", "the stop reason is the clock")
    equal(summary["byStatus"].get("NOT_CHECKED_TIME"), 1, "the row says TIME")
    equal(calls["download"], [], "no download is started with no time left")


def test_empty_input():
    summary, backend, actor, calls = run_main(lines=[])
    equal(summary["requested"], 0, "nothing was asked")
    ok(actor.fail_message and "No pdf urls" in actor.fail_message, "the run says why it is empty")
    equal(backend.rows, [], "and writes no rows")


def test_private_host_is_never_downloaded():
    def guard(host):
        if host == "internal.test":
            return False, "the address 10.1.2.3 is not a public address"
        return True, None

    summary, backend, actor, calls = run_main(
        pdfs={"https://internal.test/a.pdf": make_pdf(1)}, host_public=guard)
    equal(calls["download"], [], "a private host is not downloaded")
    equal(summary["privateBlocked"], 1, "it is counted")
    equal(summary["byStatus"].get("NOT_CHECKED_PRIVATE_ADDRESS"), 1, "the row says PRIVATE")


def test_unresolvable_host_is_a_dns_error():
    def guard(host):
        return False, "the host name does not resolve: [Errno 11001] getaddrinfo failed"

    summary, backend, actor, calls = run_main(
        pdfs={"https://typo.test/a.pdf": make_pdf(1)}, host_public=guard)
    equal(summary["dnsErrors"], 1, "a typo is counted as a dns error")
    equal(summary["byStatus"].get("NOT_CHECKED_DNS_ERROR"), 1, "the row says DNS, not private")
    ok(summary["privateBlocked"] == 0, "and it is not called a private address")


def test_a_bracketed_ipv6_literal_keeps_its_brackets():
    """A url that is rebuilt without brackets is not a url, and the guard then
    reported a private address as a dns error."""
    from src.net import host_is_public, host_of, normalize_url

    url, why = normalize_url("http://[::ffff:100.64.0.1]/report.pdf")
    equal(why, None, "a bracketed ipv6 literal is a url this actor accepts")
    equal(url, "http://[::ffff:100.64.0.1]/report.pdf", "the rebuilt url keeps its brackets")
    equal(host_of(url), "::ffff:100.64.0.1", "and the host is readable from it")

    allowed, reason = host_is_public("::ffff:100.64.0.1")
    equal(allowed, False, "the cgnat address behind the literal is refused")
    ok("is not a public address" in (reason or ""),
       f"and it is called a private address, not a dns error: {reason}")


def test_dataset_refusals_stop_the_run():
    summary, backend, actor, calls = run_main(
        lines=["not a url", "also not a url", "still not a url", "https://a.test/1.pdf"],
        pdfs={"https://a.test/1.pdf": make_pdf(1)},
        script=["raise_before", "raise_before", "raise_before"])
    equal(summary["stopReason"], "dataset-write-error", "the dataset refusal stops the run")
    equal(summary["writeErrors"], 3, "three refusals in a row")
    equal(summary["deliveredPages"], 0, "nothing is delivered")
    equal(calls["download"], [], "and nothing is downloaded for money at that point")


def test_documents_invariant():
    pdf = make_pdf(1)
    summary, backend, actor, calls = run_main(lines=[
        "https://a.test/1.pdf", "https://internal.test/x.pdf", "nope",
        "https://b.test/2.pdf", "https://c.test/3.pdf"],
        pdfs={"https://a.test/1.pdf": pdf, "https://b.test/2.pdf": pdf,
              "https://c.test/3.pdf": pdf},
        host_public=lambda host: (False, "the address 10.0.0.1 is not a public address")
        if host == "internal.test" else (True, None))
    equal(summary["processedDocuments"] + summary["notCheckedDocuments"],
          summary["documentsAttempted"], "every document is either read or counted as not checked")
    equal(summary["documentsAttempted"] + summary["linesThatAreNotDocuments"],
          summary["requested"], "and the lines that were never documents close the count")
    ok("documentMismatch" not in summary["byStatus"], "and the counts close")


def test_no_ocr_engine_fails_before_any_request():
    actor_fail = {}

    def no_engine():
        return False, "the tesseract binary is not installed in this image"

    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/1.pdf": make_pdf(1)}, ocr_available=no_engine)
    ok(actor.fail_message and "no OCR engine" in actor.fail_message,
       "a missing engine fails the run and says so")
    equal(calls["download"], [], "and nothing is downloaded")


def test_missing_language_pack_fails():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/1.pdf": make_pdf(1)}, extra={"languages": "eng+rus"},
        ocr_langs=lambda: {"eng"})
    ok(actor.fail_message and "rus" in actor.fail_message, "the missing pack is named")
    equal(calls["download"], [], "and nothing is downloaded")


def test_a_document_with_no_text_was_read_not_unreached():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/blank.pdf": make_blank_pdf(1)},
        extra={"ocrMode": "always"}, ocr_text="")
    equal(summary["pagesWithoutText"], 1, "the page is counted as having no text")
    equal(summary["documentsAttempted"], 1, "one document was attempted")
    equal(summary["processedDocuments"], 1, "it was opened and read")
    equal(summary["notCheckedDocuments"], 0, "so it is not a document this run never reached")


def test_the_recogniser_cannot_outlive_the_run():
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/scan.pdf": make_blank_pdf(1)},
        extra={"ocrMode": "always"}, ocr_text="Text")
    equal(calls["ocr_timeouts"][0], 42.0,
          "without a deadline one page gets three margins at 200 dpi")

    # 40 s is inside the document gate (35 s) and below the page budget (42 s),
    # so the recognition is started and the cap is what we can observe.
    moment = datetime.now(timezone.utc) + timedelta(seconds=40)
    summary, backend, actor, calls = run_main(
        pdfs={"https://a.test/scan.pdf": make_blank_pdf(1)},
        extra={"ocrMode": "always"}, ocr_text="Text",
        env={"ACTOR_TIMEOUT_AT": moment.isoformat()})
    got = calls["ocr_timeouts"][0]
    ok(5.0 <= got < 42.0,
       f"the time left before the platform kills the run caps the recognition: got {got}")


def test_the_dataset_door_takes_no_event_name():
    """The real Dataset.push_data has no charged_event_name; the fake must not either."""
    backend = FakeBackend()
    try:
        asyncio.run(backend.push_data({"a": 1}, charged_event_name="page-ocr"))
    except TypeError:
        ok(True, "the dataset door refuses an event name, like the installed SDK")
    else:
        ok(False, "Dataset.push_data must not accept charged_event_name")


def test_the_cut_is_not_a_document_and_the_counts_close():
    """maxDocuments below the number of unique documents, which is where F2 lived.

    A line cut by the limit was never a document. If the count forgets to take it
    out, the same run reports both documentMismatch and lineCountMismatch, so these
    two counters are what pins the class: this test is written so that dropping
    `- counts["limit"]` fails here and nowhere else.
    """
    pdf = make_pdf(1)
    summary, backend, actor, calls = run_main(
        lines=["https://a.test/1.pdf", "https://b.test/2.pdf", "https://c.test/3.pdf"],
        pdfs={"https://a.test/1.pdf": pdf, "https://b.test/2.pdf": pdf,
              "https://c.test/3.pdf": pdf},
        extra={"maxDocuments": 2})
    equal(summary["uniqueDocuments"], 3, "three unique documents were seen")
    equal(summary["cutByMaxDocuments"], 1, "one of them was cut by the limit")
    equal(summary["documentsAttempted"], 2,
          "only the documents inside the limit are attempted")
    equal(summary["linesThatAreNotDocuments"], 1, "the cut line was never a document")
    equal(summary["documentsAttempted"] + summary["linesThatAreNotDocuments"],
          summary["requested"], "and the two buckets close the requested count")
    equal(summary["processedDocuments"], 2, "both attempted documents were read")
    ok("documentMismatch" not in summary["byStatus"], "no document is unaccounted for")
    ok("lineCountMismatch" not in summary["byStatus"], "no line is unaccounted for")
    equal(summary["deliveredPages"], 2, "two pages were delivered")
    equal(len(calls["download"]), 2, "the cut document is never downloaded")


def run_all() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
            except Exception as exc:  # noqa: BLE001
                FAILS.append(f"{name} raised {type(exc).__name__}: {exc}")
    print(f"test_main: {CHECKS[0]} checks, {len(FAILS)} failed")
    for line in FAILS:
        print("  FAIL", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run_all())
