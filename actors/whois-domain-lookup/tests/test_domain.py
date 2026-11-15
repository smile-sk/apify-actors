"""Local checks for input normalisation and date maths. Run: python tests/test_domain.py"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import domain

FAILS = []
CHECKS = [0]


def ok(cond, name, extra=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, extra))


def test_plain_and_link_and_email():
    for raw, want in (
        ("example.com", "example.com"),
        ("  EXAMPLE.COM  ", "example.com"),
        ("example.com.", "example.com"),
        ("https://www.example.com/pricing?x=1#top", "example.com"),
        ("http://sub.example.com:8443/a/b", "sub.example.com"),
        ("user@mail.example.org", "mail.example.org"),
        ("www.example.co.uk", "example.co.uk"),
    ):
        got = domain.normalize(raw)
        ok(got["domain"] == want, "normalize %r" % raw, got["domain"])
    ok(domain.normalize("example.co.uk")["tld"] == "uk", "tld is the last label")
    ok(domain.normalize("https://www.example.com/x")["input"] == "https://www.example.com/x",
       "the original line is kept for matching rows back")


def test_international_names():
    got = domain.normalize("\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444")  # Cyrillic written as escapes: the actor ships ASCII only
    ok(got["domain"].startswith("xn--"), "idna to punycode", got["domain"])
    ok(got["tld"].startswith("xn--"), "idna tld", got["tld"])
    ok(domain.normalize("xn--e1afmkfd.xn--p1ai")["domain"] == "xn--e1afmkfd.xn--p1ai",
       "punycode passes through unchanged")


def test_refusals():
    for raw in ("", "   ", "localhost", "192.168.1.1", "http://", "a b.com", "example..com",
                "-bad.com", "bad-.com", "example", "exa_mple.com", "example.c", "example.123"):
        try:
            domain.normalize(raw)
            ok(False, "accepted a bad line %r" % raw)
        except domain.BadDomain:
            ok(True, "refused %r" % raw)


def test_dates():
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    stamp = (now - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ok(domain.age_days(stamp, now) == 400, "age in whole days", domain.age_days(stamp, now))
    fut = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ok(domain.days_until(fut, now) == 30, "days until expiry", domain.days_until(fut, now))
    ok(domain.age_days(None) is None, "unknown date stays None")
    ok(domain.days_until("") is None, "empty date stays None")
    parsed = domain.parse_stamp("2018-03-12T21:44:25+01:00")
    ok(parsed is not None and parsed.utcoffset() == timedelta(hours=1), "offset date is read")
    ok(domain.parse_stamp("not a date") is None, "garbage date is None, not now()")


def test_age_never_negative_for_future_dates():
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    fut = (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ok(domain.age_days(fut, now) == 0, "a future date cannot give a negative age")


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
