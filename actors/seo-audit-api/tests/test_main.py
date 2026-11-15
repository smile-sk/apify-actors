"""Money path checks for the SEO audit actor.

Run: python tests/test_main.py

The Actor here is a fake that repeats the real SDK on purpose:
  * the paid push is `Actor.push_data(data, *, charged_event_name=...)` and it
    answers with a real `ChargeResult` imported from apify._charging;
  * `Dataset.push_data(data)` takes no event name at all and returns None;
  * `get_pricing_info()` and `is_event_charge_limit_reached()` are synchronous;
  * `get_metadata()` returns an object with `item_count`, not a dict.
A fake that invents its own shapes hides the defects it is meant to catch.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apify._charging import ChargeResult  # noqa: E402  the real shape, not ours

import src.main as app  # noqa: E402
from src.seo import PageError  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []

GOOD = {
    "title": "Example domain page for the audit", "titleLength": 33,
    "metaDescription": "A meta description that sits inside the published window and is neither cut short nor padded until the search engine trims it.",
    "metaDescriptionLength": 125,
    "h1": "Example", "h1Count": 1, "h2Count": 2, "h3Count": 0,
    "canonical": "https://a.test/", "metaRobots": None, "noindex": False,
    "nofollow": False, "htmlLang": "en",
    "viewport": "width=device-width, initial-scale=1", "charset": "utf-8",
    "ogCount": 3, "ogTitle": "Example", "ogImage": "https://a.test/i.png",
    "twitterCount": 2, "jsonLdBlocks": 1, "jsonLdTypes": ["WebPage"],
    "jsonLdInvalid": 0, "images": 4, "imagesMissingAlt": 0, "internalLinks": 5,
    "externalLinks": 2, "nofollowLinks": 0, "emptyAnchors": 0, "hreflangCount": 0,
    "wordCount": 400, "bodyChars": 2400, "requests": 6,
    "resourceHosts": [], "resourceHostsTotal": 0, "resourceHostsTruncated": False,
    "httpStatus": 200, "contentType": "text/html; charset=utf-8",
    "finalUrl": "https://a.test/", "redirects": [], "strategy": "mobile",
    "userAgent": "ua", "issues": [], "issueCount": 0, "criticalIssues": 0,
    "warningIssues": 0, "noticeIssues": 0, "penalty": 0, "seoScore": 100,
    "seoGrade": "good",
}


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


class PriceItem:
    def __init__(self, event_name: str, price: float) -> None:
        self.event_name = event_name
        self.price = price


class PricingInfo:
    def __init__(self, is_ppe=True, names=("page-audited",)) -> None:
        self.is_pay_per_event = is_ppe
        self.per_event_prices = [PriceItem(name, 0.01) for name in names]


class Budget:
    def __init__(self, limit=None, charged=0) -> None:
        self.limit = limit
        self.charged = charged

    def exhausted(self) -> bool:
        return self.limit is not None and self.charged >= self.limit

    def over(self) -> bool:
        return self.limit is not None and self.charged > self.limit


class FakeBackend:
    def __init__(self, script=None, metadata_count=None, metadata_raises=False,
                 budget=None) -> None:
        self.rows: list[dict] = []
        self.script = list(script or [])
        self.metadata_count = metadata_count
        self.metadata_raises = metadata_raises
        self.budget = budget or Budget()
        self.charge_names: list[str | None] = []

    def _next(self):
        return self.script.pop(0) if self.script else None

    async def push_data(self, data, *_a, **_k):
        action = self._next()
        if action == "raise_before":
            raise RuntimeError("dataset refused the row")
        self.rows.append(data)
        return None

    async def get_metadata(self):
        if self.metadata_raises:
            raise RuntimeError("metadata is not readable")
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
        if self.budget.over():
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


class FakeBrowser:
    """Stands in for Chromium: same call shape as the real Browser."""
    behaviour: dict = {}
    calls: list = []

    def __init__(self) -> None:
        self.calls = FakeBrowser.calls
        self.calls.append("start")

    async def start(self):
        return None

    async def close(self):
        self.calls.append("close")

    async def measure(self, url, *, strategy="mobile", timeout_ms=45_000,
                      settle_ms=1500, ignore_https_errors=False):
        self.calls.append(("measure", url))
        action = FakeBrowser.behaviour.get(url)
        if action is None:
            # the actor normalises a bare host to a url with a trailing slash
            action = FakeBrowser.behaviour.get(url.rstrip("/"))
        if isinstance(action, Exception):
            raise action
        if action is None:
            return dict(GOOD, finalUrl=url)
        return dict(GOOD, **action)


def run_main(lines, **kwargs):
    """Run the actor and return (summary, backend, actor instance).

    `Actor` is patched with an INSTANCE on purpose: the real SDK is used as
    `async with Actor`, so a fake that only works when instantiated would not
    survive the first line of main().
    """
    FakeBrowser.behaviour = kwargs.get("behaviour") or {}
    FakeBrowser.calls = []
    budget = kwargs.get("budget") or Budget()
    backend = FakeBackend(budget=budget, **kwargs.get("backend", {}))
    backend.budget = budget
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

    payload = {"urls": list(lines)}
    payload.update(kwargs.get("extra") or {})
    host_public = kwargs.get("host_public") or (lambda host: (True, None))

    actor = FakeActor(payload, backend, charging)
    original = (app.Actor, app.Browser, app.host_is_public)
    app.Actor = actor
    app.Browser = FakeBrowser
    app.host_is_public = host_public
    try:
        try:
            asyncio.run(app.main())
        except FakeFail:
            pass
    finally:
        app.Actor, app.Browser, app.host_is_public = original
    return actor.summary(), backend, actor


def test_three_pages_all_charged():
    summary, backend, actor = run_main(lines=["https://a.test", "https://b.test",
                                              "https://c.test"])
    equal(summary["requested"], 3, "three lines asked")
    equal(summary["processed"], 3, "three pages audited")
    equal(summary["delivered"], 3, "three rows delivered")
    equal(summary["rowsWritten"], 3, "three rows written")
    equal(summary["chargedEvents"], 3, "three events charged")
    ok(summary["billingActive"] is True, "billing is active")
    equal(summary["notChecked"], 0, "nothing left unchecked")
    ok(summary["byGrade"].get("good") == 3, "three good pages counted")
    equal(backend.charge_names, ["page-audited"] * 3, "every paid row names the event")
    ok(all(row["billable"] is True for row in backend.rows), "paid rows say billable")
    ok(actor.fail_message is None, "a delivered run does not fail")
    ok(actor.status_messages and "3" in actor.status_messages[-1],
       "the header reports the numbers")
    ok(("start",) not in FakeBrowser.calls, "the browser is opened once")
    equal(len([c for c in FakeBrowser.calls if c == "close"]), 1, "and closed once")


def test_spending_cap_stops_and_tail_is_free():
    summary, backend, actor = run_main(
        lines=["https://a.test", "https://b.test", "https://c.test"],
        budget=Budget(limit=1))
    equal(summary["delivered"], 1, "only one page is delivered")
    equal(summary["chargedEvents"], 1, "and only one event is charged")
    equal(summary["rowsWritten"], 3, "the other two still get their own row")
    equal(summary["stopReason"], "spending-cap", "the run says why it stopped")
    equal(summary["byStatus"].get("NOT_CHECKED_CAP"), 2, "both tail rows are CAP")
    ok(actor.fail_message is None, "one delivered page is not a failed run")


def test_cap_from_the_start_fails_with_a_reason():
    summary, backend, actor = run_main(lines=["https://a.test"], budget=Budget(limit=0))
    equal(summary["processed"], 0, "nothing is audited")
    equal(summary["chargedEvents"], 0, "nothing is charged")
    equal(summary["stopReason"], "spending-cap", "the stop reason is named")
    ok(backend.rows and backend.rows[0]["status"] == "NOT_CHECKED_CAP",
       "the row says the cap stopped it")
    ok(actor.fail_message is not None, "a green empty run must not happen")
    ok(bool(actor.fail_message) and "NOT_CHECKED_CAP" in actor.fail_message,
       "the failure names the breakdown")


def test_price_unreadable_gives_nothing_away():
    summary, backend, actor = run_main(lines=["https://a.test", "https://b.test"],
                                       pricing="raises")
    equal(summary["processed"], 0, "nothing is delivered free")
    equal(summary["chargedEvents"], 0, "nothing is charged either")
    equal(summary["stopReason"], "billing-price-unknown", "the reason is the price")
    equal(summary["byStatus"].get("NOT_CHECKED_BILLING"), 2, "both rows say billing")
    ok("could not be read" in backend.rows[0]["reason"], "the row explains itself")


def test_not_ppe_delivers_free_rows():
    summary, backend, actor = run_main(lines=["https://a.test"], pricing="not_ppe")
    equal(summary["processed"], 1, "the page is still audited")
    equal(summary["chargedEvents"], 0, "but nothing is charged")
    ok(summary["billingActive"] is False, "billing is off")
    ok(backend.rows[0]["billable"] is False, "the row does not claim money")
    ok(actor.fail_message is None, "a free delivery is still a delivery")
    ok("nothing charged" in actor.status_messages[-1],
       "the header does not claim money that was not taken")


def test_event_missing_from_price_list():
    summary, backend, actor = run_main(lines=["https://a.test"], pricing="other_event")
    equal(summary["chargedEvents"], 0, "an unpriced event charges nothing")
    ok("not priced" in summary["billingReason"], "the reason names the event")
    equal(summary["processed"], 1, "the page is delivered")


def test_private_address_is_refused_before_the_browser():
    summary, backend, actor = run_main(
        lines=["https://private.test/x"],
        host_public=lambda host: (False, "the address 10.0.0.5 is not a public address"))
    equal(summary["privateBlocked"], 1, "the private page is counted")
    equal(summary["byStatus"].get("NOT_CHECKED_PRIVATE_ADDRESS"), 1, "with its own status")
    equal(summary["chargedEvents"], 0, "and never charged")
    ok(not any(isinstance(c, tuple) for c in FakeBrowser.calls),
       "the browser never opened the private page")


def test_http_error_is_free():
    summary, backend, actor = run_main(
        lines=["https://a.test/missing"], behaviour={"https://a.test/missing": {"httpStatus": 404}})
    equal(summary["httpErrors"], 1, "the 404 is counted")
    equal(summary["byStatus"].get("NOT_CHECKED_HTTP_ERROR"), 1, "with its own status")
    equal(summary["chargedEvents"], 0, "a 404 is not charged")


def test_not_html_is_free():
    summary, backend, actor = run_main(
        lines=["https://a.test/file.pdf"],
        behaviour={"https://a.test/file.pdf": {"contentType": "application/pdf"}})
    equal(summary["notHtml"], 1, "the pdf is counted")
    equal(summary["chargedEvents"], 0, "a pdf is not charged")


def test_timeout_and_probe_error_are_named():
    summary, backend, actor = run_main(
        lines=["https://slow.test", "https://broken.test"],
        behaviour={"https://slow.test": PageError("the page did not finish loading in 45 s",
                                                  kind="timeout"),
                   "https://broken.test": PageError("TargetClosedError: boom")})
    equal(summary["timeouts"], 1, "the timeout is counted")
    equal(summary["probeErrors"], 1, "the probe error is counted")
    equal(summary["byStatus"].get("NOT_CHECKED_TIMEOUT"), 1, "timeout has its status")
    equal(summary["byStatus"].get("NOT_CHECKED_PROBE_ERROR"), 1, "so does the probe error")
    equal(summary["chargedEvents"], 0, "neither is charged")


def test_redirect_into_the_private_network():
    def host_public(host):
        return (False, "the address 169.254.169.254 is not a public address") \
            if host == "169.254.169.254" else (True, None)

    summary, backend, actor = run_main(
        lines=["https://a.test"],
        behaviour={"https://a.test": {"redirects": ["https://169.254.169.254/latest/meta-data"]}},
        host_public=host_public)
    equal(summary["privateBlocked"], 1, "the redirect is caught")
    equal(summary["chargedEvents"], 0, "and the row is free")
    ok("redirect" in backend.rows[0]["reason"], "the reason says a redirect did it")


def test_invalid_duplicate_and_limit():
    summary, backend, actor = run_main(
        lines=["", "not a url", "https://a.test", "https://a.test", "https://b.test"],
        extra={"maxPages": 1})
    equal(summary["invalidLines"], 2, "a blank line and a non url line are invalid")
    equal(summary["duplicates"], 1, "the repeated url is a duplicate")
    equal(summary["cutByMaxPages"], 1, "the url beyond maxPages is cut")
    equal(summary["uniqueUrls"], 2, "two unique urls")
    equal(summary["processed"], 1, "one page is audited")
    equal(summary["processed"] + summary["notChecked"], summary["requested"],
          "the counter invariant holds")
    equal(summary["maxPages"], 1, "the applied limit is reported")


def test_charge_failing_after_the_write():
    summary, backend, actor = run_main(
        lines=["https://a.test"],
        backend={"script": ["written_then_charge_fails"]})
    equal(summary["deliveredUncharged"], 1, "the row is delivered without money")
    equal(summary["chargeErrors"], 1, "the attempt is counted")
    equal(summary["stopReason"], "charge-error", "the run stops")
    equal(summary["chargedEvents"], 0, "and reports no money taken")
    equal(len(backend.rows), 1, "one page gets one row, never a second status row")


def test_charge_failure_is_not_blamed_on_the_dataset():
    """One SDK call writes the row and charges for it, row first.

    So an exception says only that the outcome is unknown: the dataset counter lags
    by minutes and a zero read after a write is a lag, not a refusal. The run stops
    as charge-error and no second row is written for the same page.
    """
    summary, backend, actor = run_main(
        lines=["https://a.test"], backend={"script": ["raise_before"]})
    equal(summary["stopReason"], "charge-error",
          "an unknown outcome is a charge error, not a dataset refusal")
    equal(summary["writeErrors"], 0, "the dataset is not blamed on a lag")
    equal(summary["deliveredUncharged"], 1, "the row may exist and carries no money")
    equal(summary["chargedEvents"], 0, "nothing is charged for it")
    ok(summary["datasetItemCountNote"] and "not charged" in summary["datasetItemCountNote"],
       "the summary says the write could not be confirmed")


def test_empty_input_is_a_failure_not_a_green_run():
    summary, backend, actor = run_main(lines=[])
    equal(summary["requested"], 0, "nothing was asked")
    ok(actor.fail_message is not None, "an empty run must not look successful")


def test_summary_shape():
    summary, backend, actor = run_main(lines=["https://a.test"])
    for key in ("requested", "uniqueUrls", "duplicates", "invalidLines", "processed",
                "notChecked", "delivered", "rowsWritten", "chargedEvents",
                "billingActive", "billingReason", "deliveredUncharged", "chargeErrors",
                "writeErrors", "stopReason", "byStatus", "byGrade", "maxPages",
                "cutByMaxPages", "deadlineCapped", "privateBlocked", "httpErrors",
                "notHtml", "timeouts", "probeErrors", "strategy", "datasetItemCount",
                "pageMarginSecs", "privateSubresourceBlocked", "dnsErrors",
                "datasetItemCountApi", "datasetItemCountNote", "priceEvent", "runId"):
        ok(key in summary, f"the summary carries {key}")
    equal(summary["priceEvent"], "page-audited", "the priced event is named")
    equal(summary["runId"], "test-run", "the run id comes from the environment")
    ok(actor.stores[0].values.get("RUN_SUMMARY-test-run") is not None,
       "the named store keeps the run scoped key")
    ok(actor.stores[-1].values.get("RUN_SUMMARY") is not None,
       "the run store keeps the bare key")


def test_metadata_gap_note():
    summary, backend, actor = run_main(lines=["https://a.test"],
                                       backend={"metadata_count": 0})
    equal(summary["datasetItemCountApi"], 0, "the platform counter is reported as it is")
    ok(summary["datasetItemCountNote"] and "lag" in summary["datasetItemCountNote"],
       "the gap is explained instead of hidden")


def test_final_url_of_a_redirect_is_checked():
    """A chain without its last hop says nothing about where the browser ended."""
    def host_public(host):
        return (False, "the address 127.0.0.1 is not a public address") \
            if host == "127.0.0.1" else (True, None)

    summary, backend, actor = run_main(
        lines=["https://a.test"],
        behaviour={"https://a.test": {"redirects": [],
                                      "finalUrl": "http://127.0.0.1:8765/secret"}},
        host_public=host_public)
    equal(summary["privateBlocked"], 1, "the address really loaded is checked")
    equal(summary["chargedEvents"], 0, "and nothing is charged for it")
    ok("redirect" in backend.rows[0]["reason"], "the row names the redirect")


def test_private_subresource_is_not_charged():
    def host_public(host):
        return (False, "the address 169.254.169.254 is not a public address") \
            if host == "169.254.169.254" else (True, None)

    summary, backend, actor = run_main(
        lines=["https://a.test"],
        behaviour={"https://a.test": {"resourceHosts": ["cdn.a.test", "169.254.169.254"]}},
        host_public=host_public)
    equal(summary["privateSubresourceBlocked"], 1, "the subresource is counted")
    equal(summary["chargedEvents"], 0, "a page that reached inside is free")
    ok("subresource" in backend.rows[0]["reason"], "the row names the subresource")


def test_unresolvable_host_is_not_a_private_address():
    summary, backend, actor = run_main(
        lines=["https://typo.test"],
        host_public=lambda host: (False, "the host name does not resolve: [Errno -2]"))
    equal(summary["byStatus"].get("NOT_CHECKED_DNS_ERROR"), 1, "the typo gets its status")
    equal(summary["dnsErrors"], 1, "and its own counter")
    equal(summary["privateBlocked"], 0, "it is not reported as a private address")
    equal(backend.rows[0]["status"], "NOT_CHECKED_DNS_ERROR", "the row says a name problem")


def test_unexpected_error_fills_the_tail_and_fails_the_run():
    """A green run that delivered half its pages is a lie, and the tail must not vanish."""
    def host_public(host):
        if host == "b.test":
            raise RuntimeError("something broke outside a page")
        return (True, None)

    summary, backend, actor = run_main(
        lines=["https://a.test", "https://b.test", "https://c.test"],
        host_public=host_public)
    equal(summary["processed"], 1, "one page was delivered before the crash")
    equal(summary["stopReason"], "unexpected-error", "the reason is named")
    ok(summary["unexpectedError"] and "RuntimeError" in summary["unexpectedError"],
       "the error text is kept")
    equal(summary["byStatus"].get("NOT_CHECKED_ERROR"), 2, "the tail gets its own rows")
    equal(summary["processed"] + summary["notChecked"], summary["requested"],
          "the counters still add up")
    ok(actor.fail_message is not None, "a half delivered run is not green")
    ok("unexpected error" in (actor.fail_message or ""), "the failure says what happened")


def test_page_margin_covers_the_worst_page():
    """The margin must carry a slow page AND the subresource resolves after it."""
    summary, backend, actor = run_main(lines=["https://a.test"],
                                       extra={"pageTimeoutSecs": 45, "settleSecs": 2})
    # 45 s navigation + 5 s networkidle cap + 2 s settle + 5 s host guard + 15 s breath.
    ok(summary["pageMarginSecs"] >= 45 + 5 + 2 + 5 + 15 - 0.2,
       f"the margin covers a full page and the host guard: {summary['pageMarginSecs']} s")


def test_dead_subresource_host_does_not_block_the_page():
    """A dead analytics host inside the buyer's markup is not the buyer's firewall."""
    def host_public(host):
        if host == "dead-host-abc123.invalid":
            return (False, "the host name does not resolve: [Errno 11001] "
                           "getaddrinfo failed")
        return (True, None)

    summary, backend, actor = run_main(
        lines=["https://a.test"],
        behaviour={"https://a.test": {"resourceHosts": ["cdn.a.test",
                                                        "dead-host-abc123.invalid"]}},
        host_public=host_public)
    equal(summary["processed"], 1, "the page is measured anyway")
    equal(summary["chargedEvents"], 1, "and it is paid for")
    equal(summary["subresourceDnsSkipped"], 1, "the dead host is counted, not hidden")
    equal(summary["subresourceHostLookups"], 2, "every name that was resolved is counted")
    equal(summary["privateSubresourceBlocked"], 0, "a dead name is not a private address")
    equal(backend.rows[0]["status"], "OK", "the row carries the measurement")
    ok(not backend.rows[0].get("reason"), "and no false accusation in the reason")
    ok(actor.fail_message is None, "a measured page is a green run")


def test_private_subresource_still_blocks():
    """The guard is not weakened: a name that really resolved inside is still stopped."""
    def host_public(host):
        return (False, "the address 10.1.2.3 is not a public address") \
            if host == "internal.a.test" else (True, None)

    summary, backend, actor = run_main(
        lines=["https://a.test"],
        behaviour={"https://a.test": {"resourceHosts": ["internal.a.test"]}},
        host_public=host_public)
    equal(summary["privateSubresourceBlocked"], 1, "the private resource is still caught")
    equal(summary["chargedEvents"], 0, "and nothing is charged for it")
    equal(summary["subresourceDnsSkipped"], 0, "it is not filed as a dead name")


def test_truncated_host_list_is_visible():
    summary, backend, actor = run_main(
        lines=["https://a.test"],
        behaviour={"https://a.test": {"resourceHosts": ["cdn.a.test"],
                                      "resourceHostsTotal": 30,
                                      "resourceHostsTruncated": True}})
    equal(summary["pagesWithTruncatedHostList"], 1, "a cut host list is counted")
    ok(backend.rows[0]["resourceHostsTruncated"] is True, "and the row says it")


def run_all() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"test_main: {CHECKS[0]} checks, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL", fail)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run_all())
