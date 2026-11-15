"""Core Web Vitals measured in a real Chromium, without a Google key.

Why not the PageSpeed Insights API: without a key it answers HTTP 429, because the
anonymous daily quota belongs to one shared Google project and the whole planet
spends it (measured 2026-11-10 from two different networks). With a key, the buyer
would have to own a Google Cloud project and hand it over. This module runs its own
browser instead: no key, no quota, no third party, and it can audit a staging URL
that Google cannot reach.

The numbers are lab measurements on emulated hardware, the same idea Lighthouse
uses for its lab run: a slower CPU and a slower network than the real one, so the
result is comparable between pages. They are not field data and this module does
not pretend otherwise.

INP is a field metric: it needs a real user interaction, which a lab page load
never has. So there is no INP here at all. Total blocking time is reported as
what it is, a lab proxy, under its own name.
"""
from __future__ import annotations

from typing import Any

# Google's published thresholds: "good" is the first number, "poor" starts after
# the second one. https://web.dev/articles/vitals
THRESHOLDS: dict[str, tuple[float, float]] = {
    "lcpMs": (2500.0, 4000.0),
    "cls": (0.1, 0.25),
    "tbtMs": (200.0, 600.0),
    "ttfbMs": (800.0, 1800.0),
    "fcpMs": (1800.0, 3000.0),
}

RATING_ORDER = {"good": 0, "needs-improvement": 1, "poor": 2}

# Subresource hostnames are collected for the address guard in main.py, which checks
# them after the load. The list is capped: a page with a thousand ad hosts would
# otherwise turn into a thousand DNS lookups per audit.
MAX_RESOURCE_HOSTS = 25

UA = {
    "mobile": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
    ),
    "desktop": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
}

# Lighthouse's own choice for its mobile lab run: 4x CPU slowdown, slow 4G.
# Desktop keeps a light throttle so that a fast page is not measured at zero.
PROFILES: dict[str, dict[str, Any]] = {
    "mobile": {
        "cpu": 4,
        "viewport": {"width": 412, "height": 823},
        "is_mobile": True,
        "latency_ms": 150,
        "download_kbps": 1600,
        "upload_kbps": 750,
        "scale": 2.625,
    },
    "desktop": {
        "cpu": 1,
        "viewport": {"width": 1440, "height": 900},
        "is_mobile": False,
        "latency_ms": 40,
        "download_kbps": 10000,
        "upload_kbps": 5000,
        "scale": 1,
    },
}

# Installed before the page scripts run, otherwise the largest-contentful-paint
# and layout-shift observers arrive after the events they are meant to catch.
INIT_JS = """
(() => {
  const S = window.__cwv = {
    lcp: 0, cls: 0, shifts: 0, lcpElement: null, long: []
  };
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.startTime > S.lcp) {
          S.lcp = e.startTime;
          const el = e.element;
          S.lcpElement = el ? (el.tagName || "").toLowerCase() : null;
        }
      }
    }).observe({ type: "largest-contentful-paint", buffered: true });
  } catch (e) {}
  try {
    let windowValue = 0, firstTs = 0, lastTs = 0;
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.hadRecentInput) continue;
        if (windowValue && (e.startTime - lastTs > 1000 || e.startTime - firstTs > 5000)) {
          windowValue = 0;
        }
        if (!windowValue) { firstTs = e.startTime; }
        windowValue += e.value;
        lastTs = e.startTime;
        if (windowValue > S.cls) { S.cls = windowValue; }
        S.shifts += 1;
      }
    }).observe({ type: "layout-shift", buffered: true });
  } catch (e) {}
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.entryType !== "longtask") { continue; }
        S.long.push({ s: e.startTime, d: e.duration });
      }
    }).observe({ type: "longtask", buffered: true });
  } catch (e) {}
})();
"""

COLLECT_JS = """
() => {
  const S = window.__cwv || {};
  const nav = (performance.getEntriesByType("navigation") || [])[0] || {};
  const paints = performance.getEntriesByType("paint") || [];
  const fcp = (paints.find((p) => p.name === "first-contentful-paint") || {}).startTime || 0;
  const res = performance.getEntriesByType("resource") || [];
  let bytes = 0, blocking = 0, opaque = 0;
  for (const r of res) {
    if (typeof r.transferSize === "number") {
      if (r.transferSize === 0 && r.duration > 0) { opaque += 1; }
      else { bytes += r.transferSize; }
    }
    if (r.renderBlockingStatus === "blocking") { blocking += 1; }
  }
  const origin = location.origin;
  let thirdBytes = 0, thirdRequests = 0;
  const hosts = [];
  const allHosts = [];
  for (const r of res) {
    try {
      const u = new URL(r.name);
      if (u.origin !== origin) { thirdRequests += 1; thirdBytes += (r.transferSize || 0); }
      if (allHosts.indexOf(u.hostname) < 0) {
        allHosts.push(u.hostname);
        if (hosts.length < 25) { hosts.push(u.hostname); }
      }
    } catch (e) {}
  }
  const nodes = document.getElementsByTagName("*").length;
  // Total blocking time is counted from first contentful paint up to the moment the
  // numbers are read, which is the window Lighthouse calls the lab TBT window.
  // Long tasks that started while the page was still blanking out are not blocking
  // anything the user could see, so they are not counted.
  let tbt = 0, longTasks = 0;
  const observeEnd = performance.now();
  for (const t of (S.long || [])) {
    if (t.s < fcp) { continue; }
    if (t.s > observeEnd) { continue; }
    longTasks += 1;
    if (t.d > 50) { tbt += (t.d - 50); }
  }
  const navBytes = typeof nav.transferSize === "number" ? nav.transferSize : 0;
  return {
    ttfbMs: nav.responseStart || 0,
    fcpMs: fcp,
    lcpMs: S.lcp || 0,
    lcpElement: S.lcpElement || null,
    cls: S.cls || 0,
    tbtMs: Math.round(tbt),
    longTasks: longTasks,
    layoutShifts: S.shifts || 0,
    domNodes: nodes,
    requests: res.length,
    transferBytes: bytes + navBytes,
    documentBytes: navBytes,
    thirdPartyRequests: thirdRequests,
    thirdPartyBytes: thirdBytes,
    renderBlockingResources: blocking,
    opaqueResponses: opaque,
    resourceHosts: hosts,
    resourceHostsTotal: allHosts.length,
    loadMs: Math.round(nav.loadEventEnd || 0),
    domContentLoadedMs: Math.round(nav.domContentLoadedEventEnd || 0),
  };
}
"""


class PageError(Exception):
    """A page that could not be measured, with a reason a buyer can act on."""

    def __init__(self, reason: str, kind: str = "probe") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


def rate(name: str, value: float | None) -> str | None:
    """good / needs-improvement / poor by Google's published thresholds."""
    if value is None:
        return None
    bounds = THRESHOLDS.get(name)
    if not bounds:
        return None
    good, poor = bounds
    if value <= good:
        return "good"
    if value <= poor:
        return "needs-improvement"
    return "poor"


def ratings(metrics: dict) -> dict:
    return {name: rate(name, metrics.get(name)) for name in THRESHOLDS}


def verdict(metrics: dict) -> str | None:
    """The worst rating of the metrics measured, so one bad number is not hidden."""
    values = [r for r in ratings(metrics).values() if r]
    if not values:
        return None
    return max(values, key=lambda r: RATING_ORDER.get(r, 0))


def profile_for(strategy: str) -> dict:
    return PROFILES.get(str(strategy or "mobile").lower(), PROFILES["mobile"])


def host_list_truncated(hosts: list[str], total: int | None) -> bool:
    """True when the host list handed to the address guard is a prefix of a longer one."""
    return int(total or 0) > len(hosts)


def redirect_chain(response) -> list[str]:
    """The urls the browser really went through, oldest first."""
    chain: list[str] = []
    request = getattr(response, "request", None) if response is not None else None
    while request is not None:
        url = getattr(request, "url", None)
        if url:
            chain.append(url)
        request = getattr(request, "redirected_from", None)
    chain.reverse()
    return chain


class Browser:
    """One Chromium for the whole run, warmed once.

    A browser launched per page pays its own cold start, and on a fresh process the
    very first navigation was measured at 2.9 s against 0.1 s warm, which would land
    straight in TTFB and turn every verdict into "poor". So the run opens one
    browser, warms it, and measures every page inside it.
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-background-timer-throttling"]
        )
        # Warm up the browser internals without touching the measured network: no
        # real request is made, but the network service and the renderer are up.
        context = await self._browser.new_context()
        try:
            page = await context.new_page()
            await page.goto("about:blank")
            await page.goto("data:text/html,<html><body>warm</body></html>")
        finally:
            await context.close()

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            self._browser = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None

    async def measure(self, url: str, *, strategy: str = "mobile",
                      timeout_ms: int = 45_000, settle_ms: int = 1500,
                      ignore_https_errors: bool = False) -> dict:
        if self._browser is None:
            await self.start()
        return await _measure_in(self._browser, url, strategy=strategy,
                                 timeout_ms=timeout_ms, settle_ms=settle_ms,
                                 ignore_https_errors=ignore_https_errors)


async def measure(
    url: str,
    *,
    strategy: str = "mobile",
    timeout_ms: int = 45_000,
    settle_ms: int = 1500,
    ignore_https_errors: bool = False,
) -> dict:
    """Measure one page in a browser of its own. For tests and one-off checks."""
    browser = Browser()
    await browser.start()
    try:
        return await browser.measure(url, strategy=strategy, timeout_ms=timeout_ms,
                                     settle_ms=settle_ms,
                                     ignore_https_errors=ignore_https_errors)
    finally:
        await browser.close()


async def _measure_in(browser, url: str, *, strategy: str, timeout_ms: int,
                      settle_ms: int, ignore_https_errors: bool) -> dict:
    profile = profile_for(strategy)
    context = await browser.new_context(
        viewport=profile["viewport"],
        user_agent=UA.get(str(strategy).lower(), UA["mobile"]),
        locale="en-US",
        is_mobile=profile["is_mobile"],
        has_touch=profile["is_mobile"],
        device_scale_factor=profile["scale"],
        ignore_https_errors=bool(ignore_https_errors),
    )
    try:
        await context.add_init_script(INIT_JS)
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)
        await cdp.send("Network.enable")
        await cdp.send("Network.emulateNetworkConditions", {
            "offline": False,
            "latency": profile["latency_ms"],
            "downloadThroughput": profile["download_kbps"] * 1024 / 8,
            "uploadThroughput": profile["upload_kbps"] * 1024 / 8,
            "connectionType": "cellular4g" if profile["is_mobile"] else "wifi",
        })
        await cdp.send("Emulation.setCPUThrottlingRate", {"rate": profile["cpu"]})
        try:
            response = await page.goto(url, wait_until="load", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - named by the caller as a status
            name = type(exc).__name__
            if "Timeout" in name:
                raise PageError(
                    f"the page did not finish loading in {timeout_ms // 1000} s",
                    kind="timeout",
                ) from exc
            raise PageError(f"{name}: the page could not be opened") from exc
        status = response.status if response is not None else None
        headers = (response.headers or {}) if response is not None else {}
        content_type = headers.get("content-type")
        try:
            await page.wait_for_load_state("networkidle", timeout=min(5000, timeout_ms))
        except Exception:  # noqa: BLE001 - a page with an endless stream is fine
            pass
        if settle_ms:
            await page.wait_for_timeout(int(settle_ms))
        raw = await page.evaluate(COLLECT_JS)
        final_url = page.url
        chain = redirect_chain(response)
    finally:
        await context.close()

    metrics = {
        "ttfbMs": round(float(raw.get("ttfbMs") or 0), 1),
        "fcpMs": round(float(raw.get("fcpMs") or 0), 1),
        "lcpMs": round(float(raw.get("lcpMs") or 0), 1),
        "cls": round(float(raw.get("cls") or 0), 4),
        "tbtMs": int(raw.get("tbtMs") or 0),
    }
    out = dict(metrics)
    out.update({
        "lcpElement": raw.get("lcpElement"),
        "longTasks": int(raw.get("longTasks") or 0),
        "layoutShifts": int(raw.get("layoutShifts") or 0),
        "domNodes": int(raw.get("domNodes") or 0),
        "requests": int(raw.get("requests") or 0),
        "transferBytes": int(raw.get("transferBytes") or 0),
        "documentBytes": int(raw.get("documentBytes") or 0),
        "thirdPartyRequests": int(raw.get("thirdPartyRequests") or 0),
        "thirdPartyBytes": int(raw.get("thirdPartyBytes") or 0),
        "renderBlockingResources": int(raw.get("renderBlockingResources") or 0),
        "opaqueResponses": int(raw.get("opaqueResponses") or 0),
        "resourceHosts": list(raw.get("resourceHosts") or [])[:MAX_RESOURCE_HOSTS],
        "resourceHostsTotal": int(raw.get("resourceHostsTotal") or 0),
        "loadMs": int(raw.get("loadMs") or 0),
        "domContentLoadedMs": int(raw.get("domContentLoadedMs") or 0),
        "httpStatus": status,
        "contentType": content_type,
        "finalUrl": final_url,
        "redirects": chain[:-1] if chain else [],
        "strategy": str(strategy or "mobile").lower(),
        "cpuThrottling": profile["cpu"],
        "networkProfile": (
            f"{profile['download_kbps']} kbps down, {profile['latency_ms']} ms latency"
        ),
        "ratings": ratings(metrics),
        "verdict": verdict(metrics),
        # transferSize is 0 for cross-origin resources without Timing-Allow-Origin,
        # so the byte count is a floor, and the row says so instead of implying
        # the page is lighter than it is.
        "bytesPartial": bool(raw.get("opaqueResponses")),
    })
    # The guard in main.py only ever sees the capped list, so the row says when the
    # list was cut instead of letting a complete looking list hide the rest.
    out["resourceHostsTruncated"] = host_list_truncated(
        out["resourceHosts"], out["resourceHostsTotal"])
    return out
