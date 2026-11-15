"""Pure checks: parsing of input names and of RDAP answers. No network, no SDK."""

from __future__ import annotations

import sys
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, ".")

import asyncio

import httpx

import src.rdap as rdap_mod
from src.rdap import (VERDICT_AVAILABLE, VERDICT_REGISTERED, VERDICT_UNKNOWN, lookup,
                      parse_domain, parse_rdap, _clean)
from src.main import EVENT, ST_OK, price_state


def test_a_plain_domain_is_accepted():
    assert parse_domain("example.com") == ("example.com", "com", None)


def test_case_and_spaces_are_normalised():
    assert parse_domain("  Example.COM ") == ("example.com", "com", None)


def test_a_url_gives_its_host():
    assert parse_domain("https://apify.com/pricing?x=1")[0] == "apify.com"
    assert parse_domain("http://sub.example.co.uk:8080/path")[0] == "sub.example.co.uk"


def test_an_email_gives_its_trailing_part():
    assert parse_domain("info@example.org")[0] == "example.org"


def test_unicode_is_converted_to_punycode():
    assert parse_domain("münchen.de")[0] == "xn--mnchen-3ya.de"
    assert parse_domain("\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444")[0] == "xn--e1afmkfd.xn--p1ai"


def test_two_forms_of_one_idn_are_the_same_domain():
    assert parse_domain("münchen.de")[0] == parse_domain("xn--mnchen-3ya.de")[0]


def test_a_single_label_is_refused():
    _d, _t, error = parse_domain("localhost")
    assert error and "at least one dot" in error


def test_an_empty_line_is_refused():
    assert parse_domain("   ")[2] == "the line is empty"


def test_a_bad_label_is_refused():
    assert parse_domain("-bad-.com")[2] is not None
    assert parse_domain("exa mple.com")[2] is not None


def test_a_numeric_tld_is_refused():
    assert parse_domain("example.123")[2] is not None


def test_trailing_dot_is_lowered():
    assert parse_domain("example.com.")[0] == "example.com"


def test_clean_keeps_the_host_of_a_messy_line():
    assert _clean("'https://WWW.Example.com/a'") == "www.example.com"


def test_rdap_registered_payload_is_read():
    payload = {
        "status": ["client transfer prohibited"],
        "events": [
            {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
            {"eventAction": "expiration", "eventDate": "2026-08-13T04:00:00Z"},
        ],
        "entities": [{"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", "RESERVED-Internet Assigned Numbers Authority"]]]}],
        "nameservers": [{"ldhName": "A.IANA-SERVERS.NET"}, {"ldhName": "B.IANA-SERVERS.NET"}],
    }
    out = parse_rdap(payload)
    assert out["registeredAt"] == "1995-08-14T04:00:00Z"
    assert out["expiresAt"] == "2026-08-13T04:00:00Z"
    assert out["registrar"].startswith("RESERVED")
    assert out["nameservers"] == ["a.iana-servers.net", "b.iana-servers.net"]
    assert out["statuses"] == ["client transfer prohibited"]


def test_rdap_empty_payload_gives_nothing_invented():
    out = parse_rdap({})
    assert all(value is None for value in out.values())


def test_rdap_garbage_is_not_a_crash():
    assert parse_rdap("not a dict")["registrar"] is None


def test_prices_read_from_a_real_shaped_object():
    class Charging:
        def __init__(self, ppe, prices):
            self.info = SimpleNamespace(is_pay_per_event=ppe, per_event_prices=prices)

        def get_pricing_info(self):
            return self.info

    event = EVENT
    assert price_state(Charging(True, {event: Decimal("0.003")}), event) == (True, None)
    assert price_state(Charging(False, {}), event) == (False, None)
    can, reason = price_state(Charging(True, {}), event)
    assert can is None and "not in the price list" in reason

    class Broken:
        def get_pricing_info(self):
            raise RuntimeError("boom")

    can, reason = price_state(Broken(), event)
    assert can is None and "could not be read" in reason


def test_ok_status_is_the_paid_one():
    assert ST_OK == "OK"


MAP = {"com": "https://rdap.verisign.test/com", "de": "https://rdap.denic.test"}


def _lookup(handler, domain, mapping=None):
    transport = httpx.MockTransport(handler)
    picked = MAP if mapping is None else mapping

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await lookup(client, domain, 5.0, picked)

    return asyncio.run(go())


def test_a_200_with_rdap_json_is_registered():
    out = _lookup(lambda request: httpx.Response(200, json={"ldhName": "EXAMPLE.COM",
                                                            "status": ["active"]}),
                  "example.com")
    assert out["verdict"] == VERDICT_REGISTERED
    assert out["statuses"] == ["active"]


def test_a_404_with_an_empty_body_is_available():
    out = _lookup(lambda request: httpx.Response(404, content=b""), "free.com")
    assert out["verdict"] == VERDICT_AVAILABLE


def test_a_404_notice_about_no_rdap_service_is_not_available():
    """The money bug: a registry without RDAP answers 404, and that is not "free"."""
    body = {"errorCode": 404, "title": "No RDAP service is available for this resource"}
    out = _lookup(lambda request: httpx.Response(404, json=body), "google.de")
    assert out["verdict"] == VERDICT_UNKNOWN
    assert "no RDAP service" in out["reason"]


def test_a_tld_outside_the_map_costs_no_request():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"ldhName": "x"})

    out = _lookup(handler, "example.zz", mapping={"com": "https://rdap.test/com"})
    assert out["verdict"] == VERDICT_UNKNOWN
    assert calls == [], calls
    assert "no RDAP service" in out["reason"]


def test_a_429_is_unknown_and_retried_once():
    calls = []
    original = rdap_mod.RETRY_DELAY_S
    rdap_mod.RETRY_DELAY_S = 0.0
    try:
        def handler(request):
            calls.append(1)
            return httpx.Response(429, headers={"Retry-After": "1"})

        out = _lookup(handler, "example.com")
    finally:
        rdap_mod.RETRY_DELAY_S = original
    assert out["verdict"] == VERDICT_UNKNOWN
    assert len(calls) == 2, calls


def test_a_200_that_is_not_json_is_unknown():
    out = _lookup(lambda request: httpx.Response(200, text="<html>hi</html>"), "example.com")
    assert out["verdict"] == VERDICT_UNKNOWN


def test_the_german_and_io_registries_have_a_base_of_their_own():
    """Not "unsupported": .de, .io, .me and .us answer RDAP, measured twice."""
    assert {"de", "io", "me", "us"} <= set(rdap_mod.FALLBACK_BASES)
    out = _lookup(lambda request: httpx.Response(200, json={"ldhName": "GOOGLE.DE"}), "google.de")
    assert out["verdict"] == VERDICT_REGISTERED


TESTS = [(name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def run_all() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__} {exc}")
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
