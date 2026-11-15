"""A real Chromium window on a virtual display, and how a LinkedIn page is read.

Why headful: measured on 2026-11-12 from our Frankfurt VPS, a plain HTTP request
to the guest search page came back 403 with Cloudflare and reCAPTCHA markers,
while the same page in a headful Chromium window under Xvfb answered with 60 job
cards, and a job page answered with an 8181 character description. Xvfb gives a
real window inside the container for free, with no residential proxy and no
third-party key.

One browser per run, one tab: the guest session keeps its cookies between pages,
which is what lets the walk go deeper than the first page.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time

LIST_JS = r"""
(function(){
  var anchors = Array.prototype.slice.call(document.querySelectorAll('a[href*="/jobs/view/"]'));
  var jobs = [], seen = {};
  anchors.forEach(function(a){
    var m = (a.href || '').match(/jobs\/view\/([^\/?#]+)/);
    if (!m || seen[m[1]]) return;
    var card = a.closest('li') || a.closest('div.base-card') || a.parentElement;
    if (!card) return;
    function t(sel){ var e = card.querySelector(sel); return e ? e.innerText.trim() : null; }
    var time = card.querySelector('time');
    seen[m[1]] = 1;
    jobs.push({
      id: m[1],
      title: t('.base-search-card__title') || t('h3'),
      company: t('.base-search-card__subtitle') || t('h4'),
      location: t('.job-search-card__location'),
      posted: t('time') || (time ? time.innerText : null),
      date: time ? time.getAttribute('datetime') : null,
      link: a.href
    });
  });
  return {jobs: jobs, title: document.title || '',
          body: (document.body ? document.body.innerText : '').slice(0, 1200)};
})()
"""

JOB_JS = r"""
(function(){
  function t(sel){ var e = document.querySelector(sel); return e ? e.innerText.trim() : null; }
  var desc = '';
  var node = document.querySelector('.show-more-less-html__markup')
          || document.querySelector('.description__text')
          || document.querySelector('article');
  if (node) desc = node.innerText.trim();
  var criteria = {};
  var heads = Array.prototype.slice.call(document.querySelectorAll('.description__job-criteria-subheader'));
  heads.forEach(function(h){
    var v = h.nextElementSibling;
    if (v) criteria[h.innerText.trim().toLowerCase()] = v.innerText.trim();
  });
  var apply = document.querySelector('a[href*="/apply"], a.apply-button, a[data-tracking-control-name*="apply"]');
  return {
    title: t('h1') || t('.top-card-layout__title'),
    company: t('.topcard__org-name-link') || t('.top-card-layout__second-subline a'),
    location: t('.topcard__flocation') || t('.top-card-layout__second-subline'),
    applicants: t('.num-applicants__caption') || t('.num-applicants__count'),
    companyUrl: (document.querySelector('.topcard__org-name-link') || {}).href || null,
    applyUrl: apply ? apply.href : null,
    employmentType: criteria['employment type'] || null,
    seniorityLevel: criteria['seniority level'] || null,
    jobFunction: criteria['job function'] || null,
    industries: criteria['industries'] || null,
    description: desc,
    pageTitle: document.title || '',
    body: (document.body ? document.body.innerText : '').slice(0, 1200)
  };
})()
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
        self.reason = ""

    async def ensure(self) -> str | None:
        existing = str(os.environ.get("DISPLAY") or "").strip()
        if existing:
            self.reason = f"display from the environment: {existing}"
            return existing
        binary = shutil.which("Xvfb")
        if not binary:
            self.reason = "no display and no Xvfb in the image: a headful browser cannot start"
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

    async def start(self, proxy_url: str | None = None) -> str | None:
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
        launch = {"headless": not headful, "args": args}
        if proxy_url:
            launch["proxy"] = {"server": proxy_url}
        try:
            self._browser = await self._pw.chromium.launch(**launch)
        except Exception as exc:  # noqa: BLE001
            self.reason = f"chromium could not start: {type(exc).__name__}: {str(exc)[:120]}"
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

    async def read(self, url: str, js: str, timeout_s: float) -> dict:
        """Navigate and extract. Never raises: a wall is data, not a crash.

        The navigation timeout comes from the run's remaining time, not from a
        constant: a fixed 45 s inside a run with 34 s left is how a run is killed
        by the platform clock before it can write a summary.
        """
        budget_ms = max(5_000, int(max(5.0, float(timeout_s)) * 1000))
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=budget_ms)
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            return {"ok": False, "timeout": "timeout" in name.lower(),
                    "error": f"{name}: {str(exc)[:160]}"}
        # Cards are rendered after the document; a short settle beats a fixed sleep.
        end = time.monotonic() + min(8.0, max(1.0, float(timeout_s) * 0.3))
        while time.monotonic() < end:
            try:
                has = await self._page.evaluate(
                    "() => !!document.querySelector('a[href*=\"/jobs/view/\"], h1')")
            except Exception:  # noqa: BLE001 - a page mid-navigation has nothing yet
                has = False
            if has:
                break
            await asyncio.sleep(0.3)
        try:
            data = await asyncio.wait_for(self._page.evaluate(js), timeout=max(5.0, float(timeout_s)))
        except asyncio.TimeoutError:
            return {"ok": False, "timeout": True, "error": "extraction timed out"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "unexpected answer from the page"}
        data["ok"] = True
        return data

    async def read_cards(self, url: str, timeout_s: float) -> dict:
        return await self.read(url, LIST_JS, timeout_s)

    async def read_job(self, url: str, timeout_s: float) -> dict:
        return await self.read(url, JOB_JS, timeout_s)
