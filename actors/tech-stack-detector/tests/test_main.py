"""Money path checks for the tech stack detector.

Run: python tests/test_main.py

The Actor here is a fake that repeats the real SDK on purpose:
  * the paid push is `Actor.push_data(data, *, charged_event_name=...)` and it answers
    with a real `ChargeResult` imported from apify._charging;
  * `Dataset.push_data(data)` takes no event name at all and returns None;
  * `get_pricing_info()` and `is_event_charge_limit_reached()` are synchronous;
  * `get_metadata()` returns an object with `item_count`, not a dict.
A fake that invents its own shapes hides the defects it is meant to catch.

The network is replaced at `fetch_page`, which is exactly the seam the actor itself uses:
one function per site, one dict out.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apify._charging import ChargeResult  # noqa: E402  the real shape, not ours

import src.main as app  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []

# A page carries something to read: a body with a generator tag and a stylesheet and no
# text at all is a stub, and the actor now refuses to sell it as a scanned site.
WORDPRESS = ('<html><head><meta name="generator" content="WordPress 6.4.2"></head>'
             '<body><h1>Our blog</h1><link href="/wp-content/themes/x/a.css"></body></html>')
SHOPIFY = ('<html><head><script src="https://cdn.shopify.com/s/files/x.js"></script>'
           '</head><body>a shop</body></html>')
PLAIN = '<html><head><title>Notes</title></head><body><h1>Hello</h1></body></html>'


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


class Budget:
    def __init__(self, limit=None, charged=0) -> None:
        self.limit = limit
        self.charged = charged

    def exhausted(self) -> bool:
        return self.limit is not None and self.charged >= self.limit

    def over(self) -> bool:
        return self.limit is not None and self.charged > self.limit


class PriceItem:
    def __init__(self, event_name: str, price: float) -> None:
        self.event_name = event_name
        self.price = price


class PricingInfo:
    def __init__(self, is_ppe=True, names=("site-scanned",)) -> None:
        self.is_pay_per_event = is_ppe
        self.per_event_prices = [PriceItem(name, 0.003) for name in names]


class FakeDataset:
    def __init__(self, script=None, metadata_count=None, metadata_raises=False) -> None:
        self.rows: list[dict] = []
        self.script = list(script or [])
        self.metadata_count = metadata_count
        self.metadata_raises = metadata_raises

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


class FakeCharging:
    def __init__(self, info=None, raises=False, budget=None) -> None:
        self.info = info
        self.raises = raises
        self.budget = budget or Budget()

    def get_pricing_info(self):
        if self.raises:
            raise RuntimeError("pricing endpoint is down")
        if self.info is not None:
            return self.info
        return PricingInfo()

    def is_event_charge_limit_reached(self, event_name=None):
        return self.budget.exhausted()


async def push_charged(self, data, event_name):
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


FakeDataset.push_charged = push_charged


def page(html=PLAIN, headers=None, cookies=None, http=200, ctype="text/html; charset=utf-8"):
    return {"body": html.encode("utf-8"), "html": html, "finalUrl": None, "hops": [],
            "status": None, "reason": None, "contentType": ctype, "httpStatus": http,
            "headers": headers or {}, "cookies": cookies or []}


def failed(status, reason):
    return {"body": None, "html": None, "finalUrl": None, "hops": [], "status": status,
            "reason": reason, "contentType": None, "httpStatus": None, "headers": {},
            "cookies": []}


def run_main(input_data, pages=None, host_public=None, charging=None, dataset=None,
             deadline=None):
    """Run the actor once and return (summary, dataset, actor).

    `Actor` is patched with an INSTANCE on purpose: the real SDK is used as
    `async with Actor`, so a fake that only works when instantiated would not survive
    the real call path.
    """
    backend = dataset or FakeDataset()
    charge = charging or FakeCharging()
    # the fake dataset charges from the same budget the fake manager reports
    backend.budget = charge.budget
    actor = FakeActor(input_data, backend, charge)
    pages = pages or {}

    async def fake_fetch(client, url, budget_s=None):
        # the actor normalises a bare host into a url with a trailing slash
        item = pages.get(url, pages.get(url.rstrip("/")))
        if item is None:
            return page(PLAIN)
        if isinstance(item, Exception):
            raise item
        result = dict(item)
        result["finalUrl"] = result.get("finalUrl") or url
        result["hops"] = result.get("hops") or [url]
        return result

    original = (app.Actor, app.fetch_page, app.host_verdict, app.deadline_seconds)
    app.Actor = actor
    app.fetch_page = fake_fetch
    # The names in these tests do not exist in the real DNS, so the guard is replaced by
    # a stub that allows everything unless a test asks for something else. The guard
    # itself is tested on its own, not through the run.
    app.host_verdict = host_public or (lambda host: (True, "ok", None))
    if deadline is not None:
        app.deadline_seconds = lambda: deadline
    try:
        try:
            asyncio.run(app.main())
        except FakeFail:
            pass
    finally:
        app.Actor, app.fetch_page, app.host_verdict, app.deadline_seconds = original
    return actor.summary(), backend, actor


def test_a_scanned_site_is_charged_and_a_blank_one_is_free():
    summary, backend, actor = run_main(
        {"urls": ["https://shop.test", "https://plain.test"]},
        {"https://shop.test": page(SHOPIFY), "https://plain.test": page(PLAIN)})
    equal(summary["processed"], 1, "one site carried a detection")
    equal(summary["noTechnologyFound"], 1, "the plain page is a free answer")
    equal(summary["chargedEvents"], 1, "one event is charged")
    equal(summary["requested"], 2, "two lines were asked for")
    equal(summary["processed"] + summary["filteredOut"] + summary["noTechnologyFound"]
          + summary["notChecked"], summary["requested"], "the counters add up")
    ok(actor.fail_message is None, "a run with one delivered site is green")
    scanned = [row for row in backend.rows if row["status"] == "OK"][0]
    equal(scanned["billable"], True, "the delivered row claims the price")
    ok("Shopify" in scanned["technologyNames"], f"the answer names Shopify: {scanned}")
    equal(scanned["site"], "shop.test", "the row names the site")
    blank = [row for row in backend.rows if row["status"] == "NO_TECHNOLOGY_FOUND"][0]
    equal(blank["billable"], False, "the free row does not claim a price")
    ok(blank["reason"], "and it says why it is free")


def test_filter_makes_a_non_matching_site_free():
    summary, backend, actor = run_main(
        {"urls": ["https://plain.test"], "onlyIf": ["shopify"]},
        {"https://plain.test": page(WORDPRESS)})
    equal(summary["chargedEvents"], 0, "a filtered site costs nothing")
    equal(summary["filteredOut"], 1, "and it is counted as filtered")
    equal(backend.rows[0]["status"], "FILTERED_OUT", "the row says filtered")
    ok("shopify" in (backend.rows[0]["reason"] or ""), "the reason names the filter")
    ok(actor.fail_message is None, "a filter that removed everything is not a failure")
    equal(summary["billableOnlyWhenMatched"], True, "the summary says a filter was on")


def test_filter_lets_a_matching_site_through():
    summary, backend, _ = run_main(
        {"urls": ["https://shop.test"], "onlyIf": ["Shopify"]},
        {"https://shop.test": page(SHOPIFY)})
    equal(summary["chargedEvents"], 1, "a matching site is charged")
    equal(summary["filteredOut"], 0, "nothing was filtered out")


def test_excluded_site_is_free():
    summary, backend, _ = run_main(
        {"urls": ["https://wp.test"], "excludeIf": ["wordpress"]},
        {"https://wp.test": page(WORDPRESS)})
    equal(summary["chargedEvents"], 0, "an excluded site costs nothing")
    equal(backend.rows[0]["status"], "FILTERED_OUT", "the row says filtered")
    ok("excluded" in (backend.rows[0]["reason"] or ""), "the reason is the right one")


def test_a_page_that_was_not_read_is_free_with_its_reason():
    summary, backend, _ = run_main(
        {"urls": ["https://gone.test"]},
        {"https://gone.test": failed(app.ST_HTTP, "the server answered HTTP 404")})
    equal(summary["chargedEvents"], 0, "a 404 is not scanned and not charged")
    equal(summary["httpErrors"], 1, "and it is counted")
    equal(backend.rows[0]["status"], "NOT_CHECKED_HTTP_ERROR", "the row carries the status")
    ok("404" in (backend.rows[0]["reason"] or ""), "and the plain reason")
    ok(backend.rows[0]["billable"] is False, "the free row never claims a price")


def test_private_address_is_refused_before_the_request():
    def host_verdict(host):
        if host == "inside.test":
            return False, "private", "the address 10.0.0.5 is not a public address"
        return True, "ok", None

    summary, backend, _ = run_main(
        {"urls": ["https://inside.test"]},
        {"https://inside.test": page(SHOPIFY)},
        host_public=host_verdict)
    equal(summary["chargedEvents"], 0, "a private address is never charged")
    equal(summary["privateBlocked"], 1, "and it is counted as a private block")
    equal(backend.rows[0]["status"], "NOT_CHECKED_PRIVATE_ADDRESS", "the status is exact")


def test_dns_error_is_not_a_private_address():
    def host_verdict(host):
        if host == "no-such-host.test":
            return False, "dns", "the host name does not resolve"
        return True, "ok", None

    summary, backend, _ = run_main(
        {"urls": ["https://no-such-host.test"]}, {}, host_public=host_verdict)
    equal(backend.rows[0]["status"], "NOT_CHECKED_DNS_ERROR", "a typo is a dns error")
    equal(summary["privateBlocked"], 0, "and it is not blamed on a firewall")
    equal(summary["dnsErrors"], 1, "the counter is its own")


def test_duplicate_invalid_and_limit_lines():
    summary, backend, _ = run_main(
        {"urls": ["https://a.test", "https://a.test", "not a url", "", "https://b.test",
                  "https://c.test"], "maxSites": 2},
        {"https://a.test": page(SHOPIFY), "https://b.test": page(SHOPIFY),
         "https://c.test": page(SHOPIFY)})
    equal(summary["duplicates"], 1, "a repeated line is free")
    equal(summary["invalidLines"], 1, "a junk line is free")
    equal(summary["blankLines"], 1, "an empty line is counted apart")
    equal(summary["cutByMaxSites"], 1, "the third site is cut by maxSites")
    equal(summary["chargedEvents"], 2, "two sites were really scanned")
    statuses = [row["status"] for row in backend.rows]
    ok("NOT_CHECKED_LIMIT" in statuses, f"the cut line has its own status: {statuses}")
    equal(len(backend.rows), 6, "every input line has a row, so the list adds up")


def test_unreadable_price_stops_the_run_instead_of_giving_it_away():
    summary, backend, _ = run_main(
        {"urls": ["https://a.test"]},
        {"https://a.test": page(SHOPIFY)},
        charging=FakeCharging(raises=True))
    equal(summary["chargedEvents"], 0, "nothing is charged when the price is unknown")
    equal(summary["stopReason"], "billing-price-unknown", "the run stops with that reason")
    equal(backend.rows[0]["status"], "NOT_CHECKED_BILLING", "the site is not delivered")
    ok("not charged" in (backend.rows[0]["reason"] or ""), "and the reason says so")


def test_billing_off_delivers_rows_free():
    summary, backend, _ = run_main(
        {"urls": ["https://a.test"]},
        {"https://a.test": page(SHOPIFY)},
        charging=FakeCharging(info=PricingInfo(is_ppe=False)))
    equal(summary["billingActive"], False, "billing is off")
    equal(summary["chargedEvents"], 0, "and nothing is charged")
    equal(backend.rows[0]["status"], "OK", "the row is still delivered")
    equal(backend.rows[0]["billable"], False, "without a price claim")


def test_spending_cap_stops_the_run():
    budget = Budget(limit=1, charged=1)
    summary, backend, _ = run_main(
        {"urls": ["https://a.test", "https://b.test"]},
        {"https://a.test": page(SHOPIFY), "https://b.test": page(SHOPIFY)},
        charging=FakeCharging(budget=budget))
    equal(summary["chargedEvents"], 0, "an exhausted budget charges nothing")
    equal(summary["stopReason"], "spending-cap", "the run says why it stopped")
    equal(summary["byStatus"].get("NOT_CHECKED_CAP"), 2, "both lines get the cap status")


def test_cap_reached_on_the_first_charge_stops_after_it():
    budget = Budget(limit=1, charged=0)
    summary, backend, _ = run_main(
        {"urls": ["https://a.test", "https://b.test"]},
        {"https://a.test": page(SHOPIFY), "https://b.test": page(SHOPIFY)},
        charging=FakeCharging(budget=budget))
    equal(summary["chargedEvents"], 1, "the row that was paid for is delivered")
    equal(summary["stopReason"], "spending-cap", "and the run stops right after")
    equal(summary["byStatus"].get("NOT_CHECKED_CAP"), 1, "the next line is not charged")
    equal(backend.rows[0]["status"], "OK", "the paid row is first")


def test_charge_failure_does_not_duplicate_the_row():
    dataset = FakeDataset(script=[None, "written_then_charge_fails"])
    summary, backend, _ = run_main(
        {"urls": ["https://a.test", "https://b.test"]},
        {"https://a.test": page(SHOPIFY), "https://b.test": page(SHOPIFY)},
        dataset=dataset)
    equal(summary["deliveredUncharged"], 1, "the write that could not be charged is counted")
    equal(summary["stopReason"], "charge-error", "the run stops instead of giving away")
    ok("could not be confirmed" in (summary["datasetItemCountNote"] or ""),
       f"the note explains what happened: {summary['datasetItemCountNote']}")
    equal(len(backend.rows), 2, "one row per site, the uncharged one is already written")
    equal(summary["chargedEvents"], 1, "only the first site was really charged")


def test_no_input_fails_with_a_plain_message():
    summary, backend, actor = run_main({}, {})
    equal(summary["requested"], 0, "nothing was asked for")
    ok(actor.fail_message is not None, "an empty input is a failure, not a green run")
    ok("one site url per line" in (actor.fail_message or ""),
       f"and it says what to do: {actor.fail_message}")


def test_every_line_unreadable_is_a_failure():
    summary, backend, actor = run_main(
        {"urls": ["https://gone.test"]},
        {"https://gone.test": failed(app.ST_NOT_HTML, "the response is image/png")})
    ok(actor.fail_message is not None, "a run that delivered nothing is not green")
    ok("Nothing could be scanned" in (actor.fail_message or ""),
       f"and the message is human: {actor.fail_message}")


def test_all_sites_filtered_is_not_a_failure():
    summary, backend, actor = run_main(
        {"urls": ["https://wp.test"], "onlyIf": ["shopify"]},
        {"https://wp.test": page(WORDPRESS)})
    ok(actor.fail_message is None, "a filter that removed everything is a valid answer")


def test_challenge_page_is_free_and_named():
    """A challenge page is not the site: its stack is not the buyer's answer."""
    challenge = ('<html><head><title>Just a moment...</title></head><body>'
                 'Checking your browser before accessing. Enable JavaScript and cookies '
                 'to continue.</body></html>')
    summary, backend, _ = run_main(
        {"urls": ["https://guarded.test"]}, {"https://guarded.test": page(challenge)})
    equal(summary["chargedEvents"], 0, "a challenge page costs nothing")
    equal(summary["blockedPages"], 1, "and it is counted apart")
    equal(backend.rows[0]["status"], "NOT_CHECKED_BLOCKED_PAGE", "the status is exact")
    ok("challenge page" in (backend.rows[0]["reason"] or ""),
       f"the reason says what happened: {backend.rows[0]['reason']}")


def test_cloudflare_mitigated_header_names_the_challenge():
    summary, backend, _ = run_main(
        {"urls": ["https://guarded.test"]},
        {"https://guarded.test": page("<html><body>one line</body></html>",
                                      headers={"cf-mitigated": "challenge"})})
    equal(backend.rows[0]["status"], "NOT_CHECKED_BLOCKED_PAGE",
          "the header alone is enough to refuse")


def test_deadline_near_stops_before_the_request():
    summary, backend, _ = run_main(
        {"urls": ["https://a.test"], "timeoutSecs": 20},
        {"https://a.test": page(SHOPIFY)}, deadline=3.0)
    equal(summary["deadlineCapped"], True, "the deadline, not the list, ended the run")
    equal(summary["chargedEvents"], 0, "and nothing was charged")
    equal(backend.rows[0]["status"], "NOT_CHECKED_TIME", "the row says why")


def test_margin_covers_the_whole_site():
    margin = app.site_margin_seconds(20)
    ok(margin >= 20 * (app.MAX_REDIRECTS + 1) + app.SITE_MARGIN_EXTRA_S - 0.01,
       f"the margin covers every hop and the breath: {margin}")


def test_summary_lag_is_named():
    summary, backend, _ = run_main(
        {"urls": ["https://a.test"]},
        {"https://a.test": page(SHOPIFY)},
        dataset=FakeDataset(metadata_count=0))
    ok("metadata lag" in (summary["datasetItemCountNote"] or ""),
       f"a lagging counter is named, not guessed: {summary['datasetItemCountNote']}")


def test_status_message_reads_like_a_sentence():
    summary, backend, actor = run_main(
        {"urls": ["https://a.test", "https://b.test"], "onlyIf": ["shopify"]},
        {"https://a.test": page(SHOPIFY), "https://b.test": page(WORDPRESS)})
    text = actor.status_messages[-1]
    ok("2 sites from 2 lines" in text, f"the header names the work: {text}")
    ok("did not match the filter" in text, "and the filter is mentioned")


def test_price_state_shapes():
    can, reason = app.price_state(FakeCharging())
    equal(can, True, "a priced event reads as chargeable")
    can, _reason = app.price_state(FakeCharging(info=PricingInfo(names=("other",))))
    equal(can, False, "an event that is not in the price list is not chargeable")
    can, _reason = app.price_state(FakeCharging(raises=True))
    equal(can, None, "a failed read is unknown, not false")
    can, _reason = app.price_state(FakeCharging(info=None))
    equal(can, True, "a readable answer with the event priced is true")


def test_stopped_status_mapping():
    equal(app.stopped_status("spending-cap")[0], "NOT_CHECKED_CAP", "cap maps")
    equal(app.stopped_status("deadline")[0], "NOT_CHECKED_TIME", "deadline maps")
    equal(app.stopped_status(None)[0], "NOT_CHECKED_ERROR", "anything else is an error")


def test_cookie_names_and_parse_page():
    import httpx

    headers = httpx.Headers([("set-cookie", "_shopify_s=abc; Path=/"),
                             ("set-cookie", "_ga=GA1.2; Domain=.x")])
    equal(app.cookie_names(headers), ["_shopify_s", "_ga"], "cookie names are read")
    scripts, metas = app.tech.parse_page(
        '<script src="/a.js"></script><meta name="generator" content="WordPress 6.0">')
    equal(scripts, ["/a.js"], "the script url is read")
    equal(metas.get("generator"), ["WordPress 6.0"], "and the meta tag with its value")


def test_decode_body_follows_the_declared_charset():
    # written as escapes so the source stays ASCII: the park forbids non-latin scripts
    word = "caf\u00e9 cr\u00e8me"
    equal(app.decode_body(word.encode("utf-8"), "text/html; charset=utf-8"), word,
          "utf-8 is read as utf-8")
    body = word.encode("cp1251", errors="replace")
    equal(app.decode_body(body, "text/html; charset=windows-1251"),
          body.decode("cp1251"), "a declared charset is used")
    equal(app.decode_body(b"\xff\xfe\x00", None) is not None, True,
          "undecodable bytes never crash the run")


def test_a_captcha_in_a_form_is_not_a_challenge_page():
    """A normal page with a captcha widget used to be refused with a false diagnosis."""
    html = ('<html><body><h1>Sign in</h1><form action="/login">'
            '<script src="https://www.google.com/recaptcha/api.js"></script>'
            '<div class="g-recaptcha" data-sitekey="k"></div>'
            '<button>Send</button></form></body></html>')
    equal(app.blocked_page(html, {}), None, "a captcha in a form is not a challenge page")


def test_a_shop_with_a_cloudflare_badge_is_not_a_challenge_page():
    html = ('<html><body><h1>Our shop</h1>'
            + '<p>' + 'bought together with this item ' * 20 + '</p>'
            '<footer>DDoS protection by Cloudflare</footer></body></html>')
    equal(app.blocked_page(html, {}), None, "a badge in the footer is not a challenge")


def test_a_large_challenge_page_is_still_a_challenge_page():
    """The reverse mistake: a 16 KB challenge passed as a scanned site and was charged."""
    payload = '<script>' + 'var a=1;' * 1800 + '</script><p>Just a moment...</p>'
    html = '<html><head><title>Just a moment...</title></head><body>' + payload + '</body>'
    ok(len(html) > app.BLOCK_VISIBLE_TEXT_MAX * 2, f"the page is big: {len(html)}")
    ok(app.blocked_page(html, {}) is not None, "a challenge is caught by what it shows")


def test_cloudflare_challenge_header_is_enough():
    ok(app.blocked_page("<html><body>" + "text " * 500 + "</body></html>",
                        {"cf-mitigated": "challenge"}) is not None,
       "the header names a challenge even on a full page")


def test_a_site_that_overruns_its_budget_is_a_free_row():
    """A server dripping 100 bytes every 3 s used to eat a minute of the run."""
    class DripClient:
        def stream(self, method, url):
            class Slow:
                async def __aenter__(self):
                    await asyncio.sleep(5)
                    raise AssertionError("the budget should have stopped this")

                async def __aexit__(self, *_exc):
                    return False

            return Slow()

    original = app.host_verdict
    app.host_verdict = lambda host: (True, "ok", None)
    try:
        result = asyncio.run(app.fetch_page(DripClient(), "https://slow.test/", 0.2))
    finally:
        app.host_verdict = original
    equal(result["status"], app.ST_TIME, "an overrun site is a time row, not a killed run")
    ok("not charged" in (result["reason"] or ""), f"and it says so: {result['reason']}")


def test_the_budget_does_not_touch_a_normal_page():
    import httpx

    class FastResponse:
        url = "https://fast.test/"
        status_code = 200
        is_redirect = False
        headers = httpx.Headers({"content-type": "text/html; charset=utf-8"})

        async def aiter_bytes(self):
            yield b'<html><body><script src="https://cdn.shopify.com/x.js"></script>'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class FastClient:
        def stream(self, method, url):
            return FastResponse()

    original = app.host_verdict
    app.host_verdict = lambda host: (True, "ok", None)
    try:
        result = asyncio.run(app.fetch_page(FastClient(), "https://fast.test/", 10))
    finally:
        app.host_verdict = original
    equal(result["status"], None, "a page that answers inside its budget is normal")
    ok("shopify" in (result["html"] or ""), "and its html is read")


def test_the_header_counts_charged_rows_not_the_price_list():
    """`billingActive` is a claim; a failed charge must not read as 'charged'."""
    summary, backend, actor = run_main(
        {"urls": ["https://shop.test"]},
        {"https://shop.test": page(SHOPIFY)},
        dataset=FakeDataset(script=["written_then_charge_fails"]))
    text = actor.status_messages[-1]
    equal(summary["chargedEvents"], 0, "nothing was really charged")
    equal(summary["deliveredUncharged"], 1, "the row is counted as delivered without money")
    ok("scanned, nothing charged" in text, f"the header tells the truth: {text}")
    ok("scanned and charged" not in text, "and does not claim a charge that failed")


def test_a_list_cut_by_max_sites_is_not_a_scanned_site():
    _summary, _backend, actor = run_main(
        {"urls": ["https://a.test", "https://b.test", "https://c.test"], "maxSites": 1},
        {"https://a.test": page(SHOPIFY), "https://b.test": page(SHOPIFY),
         "https://c.test": page(SHOPIFY)})
    text = actor.status_messages[-1]
    ok("1 site from 3 lines" in text, f"the header names what was worked on: {text}")
    ok("cut by maxSites" in text, "and says the list was cut")


def test_counters_are_read_from_the_statuses_not_defined_by_subtraction():
    summary, backend, _actor = run_main(
        {"urls": ["https://shop.test", "https://plain.test", "nonsense line //"]},
        {"https://shop.test": page(SHOPIFY), "https://plain.test": page(PLAIN)})
    counted = sum(count for status, count in summary["byStatus"].items()
                  if status.startswith("NOT_CHECKED"))
    equal(summary["notChecked"], counted, "notChecked is the number of NOT_CHECKED statuses")
    ok("counterMismatch" not in summary["byStatus"],
       f"and the counters add up: {summary['byStatus']}")


LIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live")


def live_meta(name: str) -> dict:
    with open(os.path.join(LIVE, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def live_body(name: str) -> bytes:
    with open(os.path.join(LIVE, name + ".html"), "rb") as fh:
        return fh.read()


def live_page(name: str) -> dict:
    """A real page saved from the live site: the bytes, the status and the headers."""
    meta = live_meta(name)
    body = live_body(name)
    item = page(body.decode("utf-8", errors="replace"), headers=meta.get("headers") or {},
                http=meta["status"], ctype=meta.get("contentType") or "text/html")
    item["body"] = body
    item["finalUrl"] = meta["url"]
    item["hops"] = [meta["url"]]
    return item


class LiveResponse:
    def __init__(self, url, status, body, headers) -> None:
        import httpx

        self.url = url
        self.status_code = status
        self.is_redirect = False
        self.headers = httpx.Headers(headers)
        self._body = body

    async def aiter_bytes(self):
        yield self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class LiveClient:
    def __init__(self, response) -> None:
        self._response = response

    def stream(self, method, url):
        return self._response


def fetch_once(name: str, url: str) -> dict:
    """One live body through the real `_fetch_page`, with the address guard neutral."""
    meta = live_meta(name)
    client = LiveClient(LiveResponse(url, meta["status"], live_body(name),
                                     meta.get("headers") or {}))
    original = app.host_verdict
    app.host_verdict = lambda host: (True, "ok", None)
    try:
        return asyncio.run(app.fetch_page(client, url, 10))
    finally:
        app.host_verdict = original


def test_a_live_client_challenge_is_never_charged():
    """drupal.org answered 200 with "Client Challenge": 3038 bytes, 303 chars of text.

    The saved body is the real one. Before this fix the page was scanned, sold as Fastly
    and charged: the buyer paid for the edge that answered instead of the site.
    """
    body = live_body("drupal_org")
    summary, backend, _ = run_main({"urls": ["https://www.drupal.org/"]},
                                   {"https://www.drupal.org/": live_page("drupal_org")})
    equal(summary["chargedEvents"], 0, "a challenge page is not charged")
    equal(summary["blockedPages"], 1, "and it is counted as blocked")
    equal(backend.rows[0]["status"], app.ST_BLOCKED, "the row says blocked")
    ok(not backend.rows[0].get("technologyNames"),
       f"no technology is sold from a wall: {backend.rows[0].get('technologyNames')}")
    ok("challenge page" in (backend.rows[0]["reason"] or ""),
       f"the reason says what the page was: {backend.rows[0]['reason']}")
    ok("client challenge" in (backend.rows[0]["reason"] or "").lower(),
       f"and names the words the page itself used: {backend.rows[0]['reason']}")
    ok(len(body) > app.BLOCK_BODY_MIN_BYTES, "the saved page is the whole answer")
    equal(summary["processed"] + summary["filteredOut"] + summary["noTechnologyFound"]
          + summary["notChecked"], summary["requested"],
          "a blocked site still leaves the counters adding up")


def test_live_challenge_pages_that_answer_202_are_no_content():
    """booking.com and imdb.com answer 202: accepted, nothing inside.

    That used to read as a scanned page and sold the CDN as the stack of the site.
    """
    for name, url in (("booking_com", "https://www.booking.com/"),
                      ("imdb_com", "https://www.imdb.com/")):
        meta = live_meta(name)
        result = fetch_once(name, url)
        equal(result["status"], app.ST_NO_CONTENT, f"{name}: 202 is not a page")
        ok("not scanned" in (result["reason"] or ""), f"{name}: the reason is plain")
        ok(app.blocked_page(live_body(name).decode("utf-8", errors="replace"),
                            meta.get("headers") or {}, len(live_body(name))) is not None,
           f"{name}: the same body is a challenge when it answers 200")


def test_a_live_normal_site_is_still_scanned_and_charged():
    """The reverse half: real pages must not be caught by the new rule."""
    for name, url, technology in (("example_com", "https://example.com/", "Cloudflare"),
                                  ("getbootstrap_com", "https://getbootstrap.com/", "Bootstrap")):
        summary, backend, _ = run_main({"urls": [url]}, {url: live_page(name)})
        equal(summary["chargedEvents"], 1, f"{name}: a real site is charged")
        equal(backend.rows[0]["status"], "OK", f"{name}: the row is a scan")
        ok(technology in (backend.rows[0].get("technologyNames") or []),
           f"{name}: {technology} is read from the real page")

    summary, backend, _ = run_main({"urls": ["https://tailwindcss.com/"]},
                                   {"https://tailwindcss.com/": live_page("tailwindcss_com")})
    equal(backend.rows[0]["status"], "OK", "a huge page with text inside is a site")
    ok("Next.js" in (backend.rows[0].get("technologyNames") or []),
       f"and its stack is read: {backend.rows[0].get('technologyNames')}")


def test_an_empty_answer_gets_its_own_free_status():
    meta = {"content-type": "text/html; charset=utf-8"}
    client = LiveClient(LiveResponse("https://empty.test/", 200, b"", meta))
    original = app.host_verdict
    app.host_verdict = lambda host: (True, "ok", None)
    try:
        result = asyncio.run(app.fetch_page(client, "https://empty.test/", 10))
    finally:
        app.host_verdict = original
    equal(result["status"], app.ST_EMPTY_BODY, "an empty answer is not a page")
    ok("not charged" in (result["reason"] or ""), f"and it says so: {result['reason']}")


def test_a_wall_of_markup_without_text_is_not_sold():
    html = ('<html><head><title></title><script>' + "var a=1;" * 800 + "</script>"
            '</head><body><div id="app"></div></body></html>')
    ok(app.blocked_page(html, {}, len(html.encode())) is not None,
       "3 KB of script and no text is a wall, not a site")
    summary, backend, _ = run_main({"urls": ["https://wall.test"]},
                                   {"https://wall.test": page(html)})
    equal(summary["chargedEvents"], 0, "and it is free")
    equal(backend.rows[0]["status"], app.ST_BLOCKED, "with the reason named")

    stub = '<html><body><div id="app"></div></body></html>'
    summary, backend, _ = run_main({"urls": ["https://stub.test"]},
                                   {"https://stub.test": page(stub)})
    equal(backend.rows[0]["status"], app.ST_EMPTY_BODY,
          "a stub with nothing to read has its own status")
    equal(summary["chargedEvents"], 0, "and costs nothing")


def test_the_structural_rule_leaves_a_small_page_alone():
    equal(app.blocked_page("<html><body><h1>Hello</h1><p>A short note.</p></body></html>",
                           {}, 200), None,
          "a small page is judged by its words, not by its size")


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
