"""Local checks for the DNS layer and the DMARC reader. Run: python tests/test_dnsrec.py

No network: a stub resolver plays the part of the three real ones, and the
formatting helpers are checked on objects shaped like dnspython's answers.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dns.exception
import dns.resolver

from src import dnsrec

FAILS = []
CHECKS = [0]


def ok(cond, name, extra=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, extra))


class Txt:
    def __init__(self, text):
        self.strings = [text.encode("utf-8")]


class Mx:
    def __init__(self, preference, exchange):
        self.preference = preference
        self.exchange = exchange


class Stub:
    """Plays one resolver. `answers` maps rdtype to a list, or raises when told to."""

    def __init__(self, answers=None, raises=None, label="system"):
        self.answers = answers or {}
        self.raises = raises
        self.label = label
        self.asked = []

    async def resolve(self, name, rdtype, lifetime=None, raise_on_no_answer=False):
        self.asked.append((name, rdtype))
        if self.raises is not None:
            raise self.raises
        key = rdtype if name.count("_dmarc") == 0 else "_dmarc." + rdtype
        return self.answers.get(key, self.answers.get(rdtype, []))


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def with_resolvers(resolvers):
    old = dnsrec._resolver
    it = iter(resolvers)

    def fake(servers):
        try:
            return next(it)
        except StopIteration:
            return resolvers[-1]

    dnsrec._resolver = fake
    return old


def test_records_are_read_from_the_first_resolver_that_answers():
    stub = Stub({
        "NS": [Txt("ns1.example.net"), Txt("ns2.example.net")],
        "MX": [Mx(10, "mail.example.net.")],
        "TXT": [Txt("v=spf1 include:_spf.example.net -all"), Txt("google-site-verification=abc")],
        "_dmarc.TXT": [Txt("v=DMARC1; p=quarantine; rua=mailto:d@example.net")],
        "A": ["93.184.216.34"],
        "DS": [],
    })
    stub.answers["NS"] = [_Name("ns1.example.net"), _Name("ns2.example.net")]
    old = with_resolvers([stub])
    try:
        out = run(dnsrec.lookup("example.net"))
    finally:
        dnsrec._resolver = old
    ok(out["exists"] is True, "domain exists", out["exists"])
    ok(out["resolver"] == "system", "the answering resolver is named", out["resolver"])
    ok(out["nameservers"] == ["ns1.example.net", "ns2.example.net"], "ns parsed", out["nameservers"])
    ok(out["mx"] == [{"preference": 10, "exchange": "mail.example.net"}], "mx parsed", out["mx"])
    ok(out["spf"].startswith("v=spf1"), "spf found", out["spf"])
    ok(out["dmarcPolicy"] == "quarantine", "dmarc policy", out["dmarcPolicy"])
    ok(out["a"] == ["93.184.216.34"], "a records", out["a"])
    ok(out["dnssecSigned"] is False, "no DS means not signed", out["dnssecSigned"])


class _Name:
    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text


def test_nxdomain_is_a_real_answer():
    old = with_resolvers([Stub(raises=dns.resolver.NXDOMAIN())])
    try:
        out = run(dnsrec.lookup("gone.example"))
    finally:
        dnsrec._resolver = old
    ok(out["exists"] is False, "NXDOMAIN means the name does not exist", out["exists"])
    ok(out["error"] is None, "an NXDOMAIN is not an error", out["error"])


def test_every_resolver_closed_is_not_an_empty_answer():
    old = with_resolvers([Stub(raises=dns.exception.Timeout()),
                          Stub(raises=dns.exception.Timeout()),
                          Stub(raises=dns.exception.Timeout())])
    try:
        out = run(dnsrec.lookup("example.net"))
    finally:
        dnsrec._resolver = old
    ok(out["exists"] is None, "closed resolvers leave the answer unknown", out["exists"])
    ok(out["error"] and "Timeout" in out["error"], "the reason names the failure", out["error"])


def test_the_second_resolver_is_used_when_the_first_is_closed():
    closed = Stub(raises=dns.exception.Timeout(), label="8.8.8.8")
    alive = Stub({"NS": [_Name("ns1.example.net")], "A": [], "DS": []}, label="1.1.1.1")
    old = with_resolvers([closed, alive])
    try:
        out = run(dnsrec.lookup("example.net"))
    finally:
        dnsrec._resolver = old
    ok(out["exists"] is True, "the second resolver answered", out["exists"])
    ok(out["resolver"] == "8.8.8.8", "the label comes from the resolver that answered", out["resolver"])


def test_spf_is_found_past_the_twentieth_txt_record():
    records = [Txt("verify-%02d=abc" % i) for i in range(25)]
    records.append(Txt("v=spf1 -all"))
    stub = Stub({"NS": [_Name("ns1.example.net")], "TXT": records, "A": [], "DS": []})
    old = with_resolvers([stub])
    try:
        out = run(dnsrec.lookup("example.net"))
    finally:
        dnsrec._resolver = old
    ok(len(out["txt"]) == dnsrec.TXT_LIMIT, "the visible list is capped", len(out["txt"]))
    ok(out["spf"] == "v=spf1 -all", "spf is still found past the cap", out["spf"])


def test_dmarc_reader():
    record, policy = dnsrec.dmarc_policy(["v=DMARC1; p=reject; rua=mailto:x@y"])
    ok(policy == "reject", "policy read", policy)
    record, policy = dnsrec.dmarc_policy(["V=DMARC1; P=NONE"])
    ok(policy == "none", "case does not matter", policy)
    record, policy = dnsrec.dmarc_policy(["v=spf1 -all"])
    ok(record is None and policy is None, "no dmarc record gives None")
    record, policy = dnsrec.dmarc_policy(["v=DMARC1; rua=mailto:x@y"])
    ok(record is not None and policy is None, "a record without p= keeps the record", record)


def test_mail_can_be_switched_off():
    stub = Stub({"NS": [_Name("ns1.example.net")], "A": [], "DS": []})
    old = with_resolvers([stub])
    try:
        out = run(dnsrec.lookup("example.net", want_mail=False))
    finally:
        dnsrec._resolver = old
    ok(out["mx"] == [] and out["txt"] == [], "no mail queries were kept")
    ok(("example.net", "MX") not in stub.asked, "MX was not even asked", stub.asked)


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
