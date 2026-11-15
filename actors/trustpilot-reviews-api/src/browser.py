"""A real Chromium window on a virtual display, and how a listing is read.

Why headful: measured on 2026-11-11 from our Frankfurt VPS, the same address with
the same Chrome answered differently by mode:

  * curl with full Chrome headers -> 403, a 991 byte "Verifying Connection" wall;
  * Chromium `--headless=new`    -> the wall passes, then Trustpilot answers its own
    430 byte block: "identified as coming from a bot";
  * Chromium headful under Xvfb  -> the real page, 878 981 bytes, 50 289 reviews.

So the thing that decides is not the address and not the wall, it is whether the
browser is a real window. Xvfb gives that inside the container for free, with no
residential proxy and no third-party key.

One browser per run, one tab, one navigation per company: the wall is passed once,
and every further page of that listing is a same-origin fetch inside the tab, which
keeps the cookies the wall gave us and costs a fraction of a navigation.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time

# Extracted inside the page, so only the fields we sell cross the CDP bridge: a
# whole listing page is ~900 KB, twenty slim reviews are ~15 KB.
EXTRACT_JS = r"""
async (url) => {
  let response, text;
  try {
    response = await fetch(url, {headers: {'accept': 'text/html'}, credentials: 'include'});
    text = await response.text();
  } catch (error) {
    return {fetchError: String(error && error.message || error)};
  }
  const status = response.status;
  const match = text.match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
  if (!match) {
    return {status, noJson: true, bytes: text.length, head: text.slice(0, 400)};
  }
  let payload;
  try {
    payload = JSON.parse(match[1]);
  } catch (error) {
    return {status, badJson: true, bytes: text.length};
  }
  const props = (payload && payload.props && payload.props.pageProps) || {};
  const unit = props.businessUnit || {};
  const reviews = props.reviews || [];
  const categories = (unit.categories || []).map(c => c && c.categoryId).filter(Boolean);
  // A login page is what Trustpilot answers past the anonymous cap (measured
  // 2026-11-11: page 11 lands on /users/connect). It must be told apart from a
  // listing with no reviews, otherwise a wall is sold as an empty company.
  return {
    status,
    finalUrl: response.url,
    login: (response.url || '').indexOf('/users/connect') !== -1,
    bytes: text.length,
    meta: {
      name: unit.identifyingName || null,
      display: unit.displayName || null,
      score: (unit.trustScore === undefined ? null : unit.trustScore),
      total: (unit.numberOfReviews === undefined ? null : unit.numberOfReviews),
      claimed: (unit.isClaimed === undefined ? null : unit.isClaimed),
      country: unit.countryCode || null,
      categories,
      website: unit.websiteUrl || null,
    },
    reviews: reviews.map(x => ({
      id: x.id,
      rating: (x.rating === undefined ? null : x.rating),
      title: x.title || '',
      text: x.text || '',
      language: x.language || null,
      likes: x.likes || 0,
      published: (x.dates && x.dates.publishedDate) || null,
      experienced: (x.dates && x.dates.experiencedDate) || null,
      verified: !!(x.labels && x.labels.verification && x.labels.verification.isVerified),
      consumer: {
        displayName: (x.consumer && x.consumer.displayName) || null,
        countryCode: (x.consumer && x.consumer.countryCode) || null,
        numberOfReviews: (x.consumer && x.consumer.numberOfReviews === undefined ? null : x.consumer.numberOfReviews)
      },
      dates: {publishedDate: (x.dates && x.dates.publishedDate) || null,
              experiencedDate: (x.dates && x.dates.experiencedDate) || null},
      labels: {verification: {isVerified: !!(x.labels && x.labels.verification && x.labels.verification.isVerified)}},
      reply: x.reply ? {message: x.reply.message || '', publishedDate: x.reply.publishedDate || null} : null
    }))
  };
}
"""

def _free_display() -> int:
    for number in range(90, 120):
        if not os.path.exists(f"/tmp/.X11-unix/X{number}"):
            return number
    return 99


class Display:
    """Xvfb, started only when the container has no display of its own."""

    def __init__(self) -> None:
        self._process = None
        self._started = False
        self.reason = ""

    async def ensure(self) -> str | None:
        """A DISPLAY to draw into, or None and a reason a human can act on."""
        existing = str(os.environ.get("DISPLAY") or "").strip()
        if existing:
            self.reason = f"display from the environment: {existing}"
            return existing
        binary = shutil.which("Xvfb")
        if not binary:
            self.reason = ("no display and no Xvfb in the image: a headful browser "
                           "cannot start")
            return None
        number = _free_display()
        display = f":{number}"
        try:
            self._process = subprocess.Popen(
                [binary, display, "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            self.reason = f"Xvfb could not start: {type(exc).__name__}"
            return None
        for _ in range(60):
            if os.path.exists(f"/tmp/.X11-unix/X{number}"):
                os.environ["DISPLAY"] = display
                self._started = True
                self.reason = f"Xvfb started on {display}"
                return display
            if self._process.poll() is not None:
                self.reason = "Xvfb exited before the display appeared"
                return None
            await asyncio.sleep(0.1)
        self.reason = "Xvfb did not create the display in 6 seconds"
        return None

    def close(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:  # noqa: BLE001 - a leftover Xvfb must not fail the run
                pass
            self._process = None


class Browser:
    """One real Chromium window for the whole run."""

    def __init__(self) -> None:
        self.display = Display()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.mode = "unknown"
        self.reason = ""

    async def start(self) -> str | None:
        """Open the window. Returns None on success, or a reason to stop."""
        from playwright.async_api import async_playwright

        display = await self.display.ensure()
        headful = display is not None
        try:
            self._pw = await async_playwright().start()
        except Exception as exc:  # noqa: BLE001
            self.reason = f"playwright could not start: {type(exc).__name__}"
            return self.reason
        args = ["--no-sandbox", "--disable-dev-shm-usage",
                "--disable-background-timer-throttling",
                "--disable-blink-features=AutomationControlled"]
        try:
            self._browser = await self._pw.chromium.launch(headless=not headful, args=args)
        except Exception as exc:  # noqa: BLE001
            self.reason = f"chromium could not start: {type(exc).__name__}: {exc}"
            return self.reason
        try:
            self._context = await self._browser.new_context(
                locale="en-US", viewport={"width": 1366, "height": 900},
                timezone_id="Europe/Berlin",
            )
            self._page = await self._context.new_page()
            await self._page.goto("about:blank")
        except Exception as exc:  # noqa: BLE001
            self.reason = f"a tab could not be opened: {type(exc).__name__}"
            return self.reason
        self.mode = "headful" if headful else "headless"
        self.reason = self.display.reason
        return None

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._browser = None
            self._context = None
            self._page = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None
            self.display.close()

    async def _wait_for_listing(self, seconds: float) -> bool:
        """Wait until the wall has passed and the listing is in the document."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                state = await self._page.evaluate(
                    "() => ({has: !!document.getElementById('__NEXT_DATA__'),"
                    " title: (document.title || '').slice(0, 80),"
                    " url: location.href})")
            except Exception:  # noqa: BLE001 - a page mid-navigation has nothing yet
                state = None
            if state and state.get("has"):
                return True
            await asyncio.sleep(0.4)
        return False

    async def open_listing(self, url: str, wait_s: float = 25.0,
                           nav_timeout_s: float = 45.0) -> dict:
        """Navigate once, so the wall hands us its cookies. Never raises.

        The timeout comes from the run's remaining time, not from a constant: a
        fixed 45 s navigation inside a run with 34 s left is how a run is killed
        by the platform's clock before it can write a summary.
        """
        budget_ms = max(5_000, int(max(5.0, float(nav_timeout_s)) * 1000))
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=budget_ms)
        except Exception as exc:  # noqa: BLE001 - a bad listing is data, not a crash
            name = type(exc).__name__
            return {"ok": False, "timeout": "timeout" in name.lower(),
                    "error": f"{name}: {str(exc)[:160]}"}
        ready = await self._wait_for_listing(max(1.0, float(wait_s)))
        return {"ok": True, "ready": ready}

    async def page_data(self, url: str, timeout_s: float = 25.0) -> dict:
        """Read one listing page inside the tab; fields only, not the whole html."""
        try:
            data = await asyncio.wait_for(self._page.evaluate(EXTRACT_JS, url),
                                          timeout=max(5.0, timeout_s))
        except asyncio.TimeoutError:
            return {"timeout": True}
        except Exception as exc:  # noqa: BLE001 - reported as a data problem
            return {"fetchError": f"{type(exc).__name__}: {exc}"}
        return data if isinstance(data, dict) else {"fetchError": "unexpected answer"}
