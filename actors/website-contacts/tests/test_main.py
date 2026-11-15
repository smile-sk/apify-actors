"""Checks for the run loop: what was read, what was not, and where the money is.

QA 0.1.24 (07.11): a run that read nothing came back SUCCEEDED with an empty statusMessage,
so the buyer saw "success" and one INVALID_URL row. A green run with nothing behind it is
the same defect as a crash without a reason.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

from src import extract, main


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, *args):
        self.lines.append(("info", args))

    def warning(self, *args):
        self.lines.append(("warning", args))

    def error(self, *args):
        self.lines.append(("error", args))


class FakeCharge:
    def __init__(self, charged=1, cap=False):
        self.charged_count = charged
        self.event_charge_limit_reached = cap


class FakeKV:
    def __init__(self):
        self.data = {}

    async def get_value(self, key):
        return self.data.get(key)

    async def set_value(self, key, value):
        self.data[key] = value


class FakeResponse:
    def __init__(self, status, text="", ctype="text/html; charset=utf-8"):
        self.status_code = status
        self.text = text
        self.headers = {"content-type": ctype}
        self.url = "https://shop.example/"


class FakeHTTP:
    """Answers by host substring, the way the real client would: no network in a check."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, follow_redirects=False, headers=None):
        self.calls.append(url)
        for pattern, answer in self.routes.items():
            if pattern in url:
                return answer
        return FakeResponse(404)

    def __call__(self, *args, **kwargs):
        return self


class RunActor:
    """The SDK surface main() touches, as an instance: `async with Actor` looks for
    __aenter__ on the type, so a class instead of an instance never runs."""

    def __init__(self, websites, cap_after=None):
        self.websites = websites
        self.cap_after = cap_after
        self.rows = []
        self.charged = 0
        self.status_message = None
        self.failure = None
        self.summary = None
        self.stores = {}
        self.log = FakeLog()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_input(self):
        return {"websites": self.websites, "cacheMinutes": 0, "includeSocials": True,
                "includeAddress": True, "maxPagesPerSite": 2, "concurrency": 2}

    async def push_data(self, row):
        self.rows.append(row)

    async def charge(self, event_name=None):
        if self.cap_after is not None and self.charged >= self.cap_after:
            return FakeCharge(0, True)
        self.charged += 1
        return FakeCharge(1, False)

    async def set_value(self, key, value):
        if key == "SUMMARY":
            self.summary = value

    async def open_key_value_store(self, name=None):
        return self.stores.setdefault(name or "run", FakeKV())

    async def set_status_message(self, text):
        self.status_message = text

    async def fail(self, status_message=None):
        self.failure = status_message


def run_actor(fake, routes):
    """Run main() against a stand-in Actor and a stand-in network, then put both back."""
    originals = (main.Actor, main.httpx.AsyncClient, extract.resolves_private)
    http = FakeHTTP(routes)
    try:
        main.Actor = fake
        main.httpx.AsyncClient = http
        # .example is reserved and resolves nowhere: this check is about the header,
        # not about DNS, and a real lookup would make the result depend on the network.
        extract.resolves_private = lambda host: False
        asyncio.run(main.main())
    finally:
        main.Actor, main.httpx.AsyncClient, extract.resolves_private = originals
    return http


PAGE_WITH_EMAIL = "<html><body><h1>Shop</h1><a href='mailto:sales@shop.example'>sales@shop.example</a></body></html>"


# --- a run that read nothing must say so, not smile -------------------------------------

def test_an_unusable_input_fails_with_words_and_charges_nothing():
    fake = RunActor(["not a website", "ftp://x"])
    run_actor(fake, {})

    assert [r["status"] for r in fake.rows] == ["INVALID_URL", "INVALID_URL"], fake.rows
    assert fake.charged == 0, "an unusable input was charged %d time(s)" % fake.charged
    assert fake.failure, "the run stayed green with nothing read and an empty header"
    assert "Nothing was read" in fake.failure, fake.failure
    assert "Nothing was charged" in fake.failure, fake.failure
    assert "not a usable address (2)" in fake.failure, fake.failure
    assert fake.status_message is None, "a failed run must not also report a success header"
    return "an unusable input fails with words, and costs nothing"


def test_a_private_address_is_named_and_is_free():
    fake = RunActor(["127.0.0.1"])
    http = run_actor(fake, {"shop.example": FakeResponse(200, PAGE_WITH_EMAIL)})

    assert http.calls == [], "a private address was requested: %s" % http.calls
    assert fake.rows[0]["status"] == "INVALID_URL", fake.rows[0]
    assert "private or loopback" in fake.rows[0]["reason"], fake.rows[0]["reason"]
    assert fake.charged == 0, fake.charged
    assert fake.failure and "Nothing was read" in fake.failure, fake.failure
    return "a private address is refused before any request, named, and free"


def test_a_site_that_did_not_answer_is_an_answer_not_a_failure():
    """An unreachable site is a fact about that site: the run reports it and stays green."""
    fake = RunActor(["https://shop.example"])
    run_actor(fake, {"shop.example": FakeResponse(500)})

    assert fake.rows[0]["status"] == "UNREACHABLE", fake.rows[0]
    assert fake.charged == 0, fake.charged
    assert fake.failure is None, "an unreachable site turned the whole run red: %s" % fake.failure
    assert fake.status_message, "a run that read nothing had no header at all"
    assert "Nothing was read" in fake.status_message, fake.status_message
    assert "did not answer (1)" in fake.status_message, fake.status_message
    return "an unreachable site is reported as an answer, and the header says nothing was read"


def test_a_normal_run_reports_what_it_read_and_what_it_cost():
    fake = RunActor(["https://shop.example"])
    run_actor(fake, {"shop.example": FakeResponse(200, PAGE_WITH_EMAIL)})

    assert fake.rows[0]["status"] == "OK", fake.rows[0]
    assert fake.rows[0]["emails"], fake.rows[0]["emails"]
    assert fake.charged == 1, fake.charged
    assert fake.failure is None, fake.failure
    assert fake.status_message.startswith("Read 1 of 1 site(s)"), fake.status_message
    assert "1 with contacts" in fake.status_message, fake.status_message
    assert "1 site(s) charged" in fake.status_message, fake.status_message
    return "a normal run says how many sites it read and what it charged"


def test_the_header_counts_a_site_left_past_the_cap():
    fake = RunActor(["https://shop.example", "https://shop2.example"], cap_after=1)
    run_actor(fake, {"shop.example": FakeResponse(200, PAGE_WITH_EMAIL),
                     "shop2.example": FakeResponse(200, PAGE_WITH_EMAIL)})

    assert fake.failure is None, fake.failure
    assert fake.charged == 1, fake.charged
    assert "1 past the cap and not charged" in fake.status_message, fake.status_message
    return "a site left past the cap is named in the header, not silently dropped"


def test_the_header_never_claims_success_without_a_page():
    rows = [{"status": "INVALID_URL", "pagesScanned": []},
            {"status": "INVALID_URL", "pagesScanned": []}]
    text = main.build_header(rows, charged=0, capped=0)
    assert text.startswith("Nothing was read"), text
    assert main.only_input_problems(rows) is True
    reached = [{"status": "UNREACHABLE", "pagesScanned": []}]
    assert main.only_input_problems(reached) is False, "an unreachable site is still an answer"
    return "the header and the fail rule agree on what counts as read"


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print("PASS %s" % fn.__name__)
    print("%d/%d passed" % (passed, len(fns)))


if __name__ == "__main__":
    run_all()
