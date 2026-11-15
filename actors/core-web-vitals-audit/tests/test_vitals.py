"""Thresholds, the redirect chain, and a real Chromium measurement.

Run: python tests/test_vitals.py
The measurement block opens a real browser against real sites on purpose: a lab
number that was never produced by a browser is not a number, it is a hope.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.vitals import (  # noqa: E402
    Browser,
    PageError,
    host_list_truncated,
    PROFILES,
    THRESHOLDS,
    profile_for,
    rate,
    ratings,
    redirect_chain,
    verdict,
)

CHECKS = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def test_thresholds():
    equal(rate("lcpMs", 2000), "good", "2.0 s LCP is good")
    equal(rate("lcpMs", 2500), "good", "the boundary itself is good")
    equal(rate("lcpMs", 2501), "needs-improvement", "just over the boundary")
    equal(rate("lcpMs", 4000), "needs-improvement", "4.0 s is still needs-improvement")
    equal(rate("lcpMs", 4001), "poor", "over 4.0 s is poor")
    equal(rate("cls", 0.1), "good", "0.1 CLS is good")
    equal(rate("cls", 0.11), "needs-improvement", "0.11 CLS is needs-improvement")
    equal(rate("cls", 0.3), "poor", "0.3 CLS is poor")
    equal(rate("tbtMs", 200), "good", "200 ms TBT is good")
    equal(rate("ttfbMs", 800), "good", "800 ms TTFB is good")
    equal(rate("fcpMs", 3000), "needs-improvement", "3.0 s FCP is needs-improvement")
    equal(rate("nope", 5), None, "an unknown metric has no rating")
    equal(rate("lcpMs", None), None, "no value, no rating")
    ok(set(THRESHOLDS) == {"lcpMs", "cls", "tbtMs", "ttfbMs", "fcpMs"},
       "the five measured metrics are exactly the five with thresholds")


def test_verdict():
    good = {"lcpMs": 1000, "cls": 0.0, "tbtMs": 10, "ttfbMs": 100, "fcpMs": 500}
    equal(verdict(good), "good", "all good is good")
    mixed = dict(good, lcpMs=5000)
    equal(verdict(mixed), "poor", "one poor metric makes the page poor")
    mixed2 = dict(good, cls=0.15)
    equal(verdict(mixed2), "needs-improvement", "one needs-improvement is reported")
    equal(verdict({}), None, "no metrics, no verdict")
    equal(ratings(good)["lcpMs"], "good", "ratings carry per metric names")


def test_profiles():
    equal(profile_for("mobile")["cpu"], 4, "mobile throttles the cpu four times")
    equal(profile_for("desktop")["cpu"], 1, "desktop does not throttle the cpu")
    equal(profile_for("MOBILE")["cpu"], 4, "the profile name is case blind")
    equal(profile_for("tablet")["cpu"], PROFILES["mobile"]["cpu"],
          "an unknown profile falls back to mobile, not to a free pass")


class FakeRequest:
    def __init__(self, url, previous=None):
        self.url = url
        self.redirected_from = previous


class FakeResponse:
    def __init__(self, request, status=200, headers=None):
        self.request = request
        self.status = status
        self.headers = headers or {}


def test_redirect_chain():
    first = FakeRequest("http://example.com/")
    second = FakeRequest("https://example.com/", previous=first)
    third = FakeRequest("https://example.com/final", previous=second)
    equal(redirect_chain(FakeResponse(third)),
          ["http://example.com/", "https://example.com/", "https://example.com/final"],
          "the chain is oldest first")
    equal(redirect_chain(None), [], "no response, no chain")
    equal(redirect_chain(FakeResponse(FakeRequest("https://a.test/x"))),
          ["https://a.test/x"], "a single hop is a chain of one")


def test_truncated_host_list_flag():
    """A capped list that nobody flags is indistinguishable from a complete one."""
    equal(host_list_truncated(["a", "b"], 2), False, "a complete list is not truncated")
    equal(host_list_truncated(["a", "b"], 30), True, "a cut list is flagged")
    equal(host_list_truncated(["a"], None), False, "an unknown total claims nothing")


def test_live_measurement():
    async def run():
        browser = Browser()
        await browser.start()
        try:
            first = await browser.measure("https://example.com", settle_ms=500,
                                          timeout_ms=30_000)
            second = await browser.measure("https://example.com", settle_ms=500,
                                           timeout_ms=30_000)
            missing = await browser.measure("https://httpbin.org/status/404",
                                            settle_ms=200, timeout_ms=30_000)
        finally:
            await browser.close()
        return first, second, missing

    first, second, missing = asyncio.run(run())

    equal(first["httpStatus"], 200, "a live page answers 200")
    ok("html" in (first["contentType"] or ""), "the content type is html")
    ok(first["fcpMs"] > 0, f"first contentful paint was measured ({first['fcpMs']})")
    ok(first["lcpMs"] >= first["fcpMs"],
       f"LCP is never earlier than FCP ({first['lcpMs']} vs {first['fcpMs']})")
    ok(first["ttfbMs"] > 0, "time to first byte was measured")
    ok(first["verdict"] in ("good", "needs-improvement", "poor"), "a verdict is produced")
    ok(first["transferBytes"] > 0, "the document bytes are counted")
    ok(isinstance(first["ratings"], dict) and "lcpMs" in first["ratings"],
       "the row carries per metric ratings")
    # A cold browser used to add seconds to the first navigation. Reusing one warm
    # browser must keep both pages in the same order of magnitude.
    ok(second["ttfbMs"] < 2000,
       f"the second page in a warm browser has a sane TTFB ({second['ttfbMs']})")
    equal(missing["httpStatus"], 404, "a 404 page reports its status, not a crash")
    ok(missing["verdict"] is not None, "a 404 still yields numbers, the caller decides")


def test_error_is_named():
    async def run():
        browser = Browser()
        await browser.start()
        try:
            await browser.measure("https://example.com", timeout_ms=1, settle_ms=0)
        finally:
            await browser.close()

    try:
        asyncio.run(run())
    except PageError as exc:
        equal(exc.kind, "timeout", "a page that never loads is a timeout, by kind")
        ok("did not finish loading" in exc.reason, "the reason is in plain words")
    else:
        FAILS.append("a 1 ms timeout did not raise PageError")
        CHECKS[0] += 1


def run_all() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"test_vitals: {CHECKS[0]} checks, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL", fail)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run_all())
