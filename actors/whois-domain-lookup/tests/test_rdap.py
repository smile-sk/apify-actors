"""Local checks for the RDAP layer, on real registry answers. Run: python tests/test_rdap.py

The fixtures in tests/fixtures are real answers captured on 2026-11-09 from the
three registries this actor leans on: Verisign (.com), DENIC (.de) and Identity
Digital (.io). A parser tested on a hand written payload only proves the test.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import rdap

HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []
CHECKS = [0]


def ok(cond, name, extra=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, extra))


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


class FakeResponse:
    def __init__(self, status, payload=None, headers=None,
                 url="https://rdap.verisign.com/com/v1/domain/example.com"):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def get(self, url, timeout=None, follow_redirects=None):
        self.calls.append(url)
        answer = self.answers.pop(0) if self.answers else FakeResponse(500)
        if isinstance(answer, Exception):
            raise answer
        return answer


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def with_no_waits(fn):
    """Keep the retry path testable: real sleeps only waste the test's time."""
    old_interval, old_delay = rdap.MIN_HOST_INTERVAL, rdap.RETRY_DELAY_SECS
    rdap.MIN_HOST_INTERVAL, rdap.RETRY_DELAY_SECS = 0.0, 0.0
    try:
        return fn()
    finally:
        rdap.MIN_HOST_INTERVAL, rdap.RETRY_DELAY_SECS = old_interval, old_delay


def test_bootstrap_from_a_real_services_payload():
    payload = {"services": [[["com", "net"], ["https://rdap.verisign.com/com/v1/"]],
                            [["org"], ["https://rdap.publicinterestregistry.org/rdap/"]]]}

    async def go():
        client = FakeClient([FakeResponse(200, payload)])
        return await rdap.bootstrap(client), len(client.calls)

    rdap._bootstrap = None
    mapping, calls = run(go())
    ok(calls == 1, "bootstrap read once", calls)
    ok(mapping["com"] == "https://rdap.verisign.com/com/v1", "com base", mapping.get("com"))
    ok(mapping["org"].endswith("publicinterestregistry.org/rdap"), "org base", mapping.get("org"))
    ok(mapping.get("io") == rdap.FALLBACK_BASES["io"], "io comes from the measured fallback table")
    ok(mapping.get("de") == "https://rdap.denic.de", "de comes from the measured fallback table")
    ok(mapping.get("co") is None, "co has no RDAP service and is not invented")


def test_bootstrap_failure_does_not_kill_the_run():
    async def go():
        client = FakeClient([RuntimeError("network down")])
        return await rdap.bootstrap(client)

    rdap._bootstrap = None
    mapping = run(go())
    ok(mapping and "io" in mapping and "de" in mapping, "fallback table survives a failed bootstrap")
    rdap._bootstrap = None


def test_parse_a_real_com_answer():
    facts = rdap.parse(fixture("rdap_example_com.json"))
    ok(facts["createdAt"] == "1995-08-14T04:00:00Z", "registration date", facts["createdAt"])
    ok(facts["expiresAt"] == "2027-08-13T04:00:00Z", "expiry date", facts["expiresAt"])
    ok(len(facts["statuses"]) == 3, "statuses", facts["statuses"])
    ok(facts["nameservers"] == ["elliott.ns.cloudflare.com", "hera.ns.cloudflare.com"],
       "nameservers", facts["nameservers"])
    ok(facts["registrar"] == "RESERVED-Internet Assigned Numbers Authority",
       "registrar", facts["registrar"])
    ok(facts["dnssecSigned"] is True, "dnssec flag comes from secureDNS", facts["dnssecSigned"])


def test_parse_a_thin_de_answer_keeps_unknowns_null():
    facts = rdap.parse(fixture("rdap_google_de.json"))
    ok(facts["createdAt"] is None, "DENIC publishes no creation date", facts["createdAt"])
    ok(facts["registrar"] is None, "DENIC publishes no registrar entity", facts["registrar"])
    ok(len(facts["nameservers"]) == 4, "nameservers still read", facts["nameservers"])
    ok(facts["updatedAt"] is not None, "change date is read", facts["updatedAt"])


def test_parse_an_io_answer_with_a_registrar_entity():
    facts = rdap.parse(fixture("rdap_google_io.json"))
    ok(facts["registrar"] == "MarkMonitor Inc.", "registrar from vCard", facts["registrar"])
    ok(facts["createdAt"] == "2002-10-01T01:00:00Z", "io creation date", facts["createdAt"])


def test_lookup_registered():
    async def go():
        client = FakeClient([FakeResponse(200, fixture("rdap_example_com.json"))])
        return await rdap.lookup(client, "example.com", {"com": "https://rdap.verisign.com/com/v1"})

    out = with_no_waits(lambda: run(go()))
    ok(out["status"] == rdap.STATUS_REGISTERED, "200 is registered", out["status"])
    ok(out["http"] == 200, "http code kept")
    ok("example.com" in (out["url"] or ""), "the url is reported", out["url"])


def test_lookup_free_name_is_an_answer_not_a_failure():
    async def go():
        client = FakeClient([FakeResponse(404)])
        return await rdap.lookup(client, "example.com", {"com": "https://rdap.verisign.com/com/v1"})

    out = with_no_waits(lambda: run(go()))
    ok(out["status"] == rdap.STATUS_AVAILABLE, "404 is available", out["status"])


def test_lookup_unsupported_tld_costs_no_request():
    client = FakeClient([])
    out = run(rdap.lookup(client, "example.co", {"com": "https://rdap.verisign.com/com/v1"}))
    ok(out["status"] == rdap.STATUS_UNSUPPORTED, "a TLD with no RDAP is unsupported", out["status"])
    ok(client.calls == [], "no request was made", client.calls)
    ok(".co" in out["reason"], "the reason names the TLD", out["reason"])


def test_lookup_server_error_is_never_available():
    async def go():
        client = FakeClient([FakeResponse(500), FakeResponse(500)])
        return await rdap.lookup(client, "example.com", {"com": "https://rdap.verisign.com/com/v1"})

    out = with_no_waits(lambda: run(go()))
    ok(out["status"] == rdap.STATUS_ERROR, "two 500s are an error, not a free domain", out["status"])
    ok(out["reason"], "the reason is written down", out["reason"])


def test_lookup_retries_a_rate_limit_once():
    async def go():
        client = FakeClient([FakeResponse(429, headers={"Retry-After": "1"}),
                             FakeResponse(200, fixture("rdap_example_com.json"))])
        return await rdap.lookup(client, "example.com", {"com": "https://rdap.verisign.com/com/v1"}), len(client.calls)

    out, calls = with_no_waits(lambda: run(go()))
    ok(out["status"] == rdap.STATUS_REGISTERED, "429 then 200 is registered", out["status"])
    ok(calls == 2, "exactly one retry", calls)


def test_lookup_200_that_is_not_json_is_an_error():
    async def go():
        client = FakeClient([FakeResponse(200, None), FakeResponse(200, None)])
        return await rdap.lookup(client, "example.com", {"com": "https://rdap.verisign.com/com/v1"})

    out = with_no_waits(lambda: run(go()))
    ok(out["status"] == rdap.STATUS_ERROR, "a 200 with no JSON is not a registration", out["status"])


def run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                FAILS.append("%s raised %s: %s" % (name, type(exc).__name__, exc))
    print("checks: %d, failed: %d" % (CHECKS[0], len(FAILS)))
    for fail in FAILS:
        print("  FAIL", fail)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(run_all())
