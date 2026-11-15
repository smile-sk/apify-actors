"""On-page SEO audit measured in a real Chromium, without a key and without a crawler.

What this module does: opens one page in its own browser, reads the signals that
search engines actually use (title, meta description, headings, canonical, robots,
structure, images, links, structured data), and turns them into a 0-100 score with
a list of issues. Every issue carries a severity and a fix hint, because "score 62"
alone tells a buyer nothing they can act on.

Why a browser and not an HTTP request: a growing share of sites render their content
with JavaScript, and a plain fetch of those returns an empty shell. Measuring a shell
gives a good score to a page that has nothing on it. The cost of that honesty is run
time, and the run time is bounded in main.py.

What this module does NOT do, said plainly because a promise without a boundary is a
lie: it does not crawl a site, it does not read backlinks, it does not call Google,
and it does not guess search positions. It audits the pages it was given.
"""
from __future__ import annotations

from typing import Any

# Chrome versions used for the two profiles: the audit is a lab measurement, so the
# user agent is fixed and named in the row instead of pretending to be a visitor.
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

PROFILES: dict[str, dict[str, Any]] = {
    "mobile": {
        "viewport": {"width": 412, "height": 823},
        "is_mobile": True,
        "scale": 2.625,
    },
    "desktop": {
        "viewport": {"width": 1440, "height": 900},
        "is_mobile": False,
        "scale": 1,
    },
}

# The host list handed to the private-address guard in main.py is capped: a big page
# with hundreds of third party hosts would otherwise turn every audit into hundreds
# of DNS lookups. A cut list is counted in the row, not hidden.
MAX_RESOURCE_HOSTS = 25

COLLECT_JS = r"""
() => {
  const attr = (el, name) => (el ? (el.getAttribute(name) || "").trim() : null);
  const pick = (sel) => document.querySelector(sel);
  const out = {};
  const titleEl = pick("title");
  out.title = titleEl ? (titleEl.textContent || "").trim() : null;
  out.titleLength = out.title ? out.title.length : 0;
  const md = pick('meta[name="description" i]');
  out.metaDescription = attr(md, "content");
  out.metaDescriptionLength = out.metaDescription ? out.metaDescription.length : 0;
  const h1 = Array.from(document.querySelectorAll("h1"));
  out.h1Count = h1.length;
  out.h1 = h1.length ? (h1[0].textContent || "").trim().slice(0, 200) : null;
  out.h2Count = document.querySelectorAll("h2").length;
  out.h3Count = document.querySelectorAll("h3").length;
  const canon = pick('link[rel="canonical" i]');
  out.canonical = canon ? attr(canon, "href") : null;
  const robots = pick('meta[name="robots" i]');
  out.metaRobots = robots ? (attr(robots, "content") || "") : null;
  const robotsText = (out.metaRobots || "").toLowerCase();
  out.noindex = robotsText.indexOf("noindex") >= 0;
  out.nofollow = robotsText.indexOf("nofollow") >= 0;
  const lang = document.documentElement.getAttribute("lang");
  out.htmlLang = lang ? lang.trim() : null;
  out.viewport = attr(pick('meta[name="viewport" i]'), "content");
  out.charset = attr(pick("meta[charset]"), "charset") || attr(pick('meta[http-equiv="Content-Type" i]'), "content");
  out.ogCount = document.querySelectorAll('meta[property^="og:" i]').length;
  out.ogTitle = attr(pick('meta[property="og:title" i]'), "content");
  out.ogImage = attr(pick('meta[property="og:image" i]'), "content");
  out.twitterCount = document.querySelectorAll('meta[name^="twitter:" i]').length;
  const ld = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
  out.jsonLdBlocks = ld.length;
  const types = [];
  let invalid = 0;
  for (const node of ld) {
    let parsed;
    try {
      parsed = JSON.parse(node.textContent || "null");
    } catch (e) {
      invalid += 1;
      continue;
    }
    const items = Array.isArray(parsed) ? parsed : [parsed];
    for (const item of items) {
      if (!item || typeof item !== "object") continue;
      const t = item["@type"];
      if (Array.isArray(t)) {
        for (const one of t) types.push(String(one));
      } else if (t) {
        types.push(String(t));
      }
    }
  }
  out.jsonLdTypes = Array.from(new Set(types)).slice(0, 25);
  out.jsonLdInvalid = invalid;
  const imgs = Array.from(document.images || []);
  out.images = imgs.length;
  let missing = 0;
  for (const img of imgs) {
    const alt = (img.getAttribute("alt") || "").trim();
    const hidden = img.getAttribute("aria-hidden") === "true" || img.getAttribute("role") === "presentation";
    if (!alt && !hidden) missing += 1;
  }
  out.imagesMissingAlt = missing;
  const links = Array.from(document.querySelectorAll("a[href]"));
  const origin = location.origin;
  let internal = 0, external = 0, nofollowLinks = 0, emptyAnchors = 0;
  const hosts = [], allHosts = [];
  const remember = (host) => {
    if (!host) return;
    if (allHosts.indexOf(host) >= 0) return;
    allHosts.push(host);
    if (hosts.length < 25) hosts.push(host);
  };
  for (const a of links) {
    let u;
    try {
      u = new URL(a.getAttribute("href") || "", location.href);
    } catch (e) {
      continue;
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") continue;
    if (u.origin === origin) internal += 1; else external += 1;
    const text = (a.textContent || "").trim();
    if (!text && !a.querySelector("img,svg")) emptyAnchors += 1;
    const rel = (a.getAttribute("rel") || "").toLowerCase();
    if (rel.indexOf("nofollow") >= 0) nofollowLinks += 1;
    remember(u.hostname);
  }
  const resources = performance.getEntriesByType("resource") || [];
  for (const r of resources) {
    try {
      remember(new URL(r.name).hostname);
    } catch (e) {}
  }
  out.internalLinks = internal;
  out.externalLinks = external;
  out.nofollowLinks = nofollowLinks;
  out.emptyAnchors = emptyAnchors;
  out.hreflangCount = document.querySelectorAll('link[rel="alternate" i][hreflang]').length;
  const body = document.body ? (document.body.innerText || "") : "";
  out.wordCount = body.trim().split(/\s+/).filter((w) => w.length > 1).length;
  out.bodyChars = body.length;
  out.requests = resources.length;
  out.resourceHosts = hosts;
  out.resourceHostsTotal = allHosts.length;
  out.htmlTitleCount = document.querySelectorAll("title").length;
  return out;
}
"""

# Title and description length windows: the widely published ranges where a search
# engine shows the whole text instead of cutting it. Being outside them is a warning,
# not an error, and the row says the number so the buyer can decide.
TITLE_MIN, TITLE_MAX = 30, 65
DESCRIPTION_MIN, DESCRIPTION_MAX = 70, 165
THIN_CONTENT_WORDS = 150
ALT_MISSING_RATIO = 0.2

# Every check: code, severity (critical / warning / notice), penalty in score points,
# the sentence the buyer reads, and the sentence that tells them what to do.
CHECKS = (
    ("title-missing", "critical", 20,
     "The page has no title tag.",
     "Add a unique title of 30 to 65 characters; it is the line a search result shows."),
    ("title-length", "warning", 6,
     "The title is outside the 30 to 65 character window.",
     "Rewrite the title so the whole line is shown instead of being cut with an ellipsis."),
    ("description-missing", "critical", 12,
     "The page has no meta description.",
     "Add a meta description of 70 to 165 characters; the search engine may use it as the snippet."),
    ("description-length", "warning", 5,
     "The meta description is outside the 70 to 165 character window.",
     "Shorten or expand it so it is not cut in the snippet."),
    ("h1-missing", "critical", 12,
     "The page has no h1.",
     "Add one h1 that names what the page is about."),
    ("h1-multiple", "warning", 5,
     "The page has more than one h1.",
     "Keep one h1 and move the rest to h2."),
    ("canonical-missing", "warning", 5,
     "The page does not declare a canonical url.",
     "Add rel=canonical with the preferred url so duplicates do not split the signals."),
    ("noindex", "critical", 25,
     "The page tells search engines not to index it.",
     "Remove noindex from the robots meta tag if this page should be found."),
    ("lang-missing", "notice", 4,
     "The html tag does not declare a language.",
     "Add lang to the html tag, for example lang=en."),
    ("viewport-missing", "warning", 8,
     "The page has no viewport meta tag.",
     "Add the viewport meta tag so the page scales on a phone."),
    ("images-alt", "warning", 8,
     "More than a fifth of the images have no alt text.",
     "Describe each meaningful image in alt; leave alt empty only for decoration."),
    ("images-alt-few", "notice", 4,
     "Some images have no alt text.",
     "Describe each meaningful image in alt; leave alt empty only for decoration."),
    ("structured-data-missing", "notice", 3,
     "The page carries no JSON-LD structured data.",
     "Add JSON-LD for the type that fits the page, for example Article or Product."),
    ("structured-data-invalid", "warning", 6,
     "A JSON-LD block on the page is not valid JSON.",
     "Fix the JSON inside the script tag; a broken block is ignored by search engines."),
    ("social-missing", "warning", 5,
     "The page has no Open Graph tags.",
     "Add og:title, og:description and og:image so shared links show a card."),
    ("twitter-missing", "notice", 2,
     "The page has no Twitter card tags.",
     "Add twitter:card and twitter:title if links to this page are shared on social media."),
    ("thin-content", "warning", 8,
     "The page has very little readable text.",
     "Add the text a visitor needs; a page with almost no words has little to rank on."),
    ("no-internal-links", "notice", 4,
     "The page links to no other page of the same site.",
     "Link to related pages so a crawler can reach them from here."),
    ("empty-anchors", "notice", 2,
     "Some links have no text and no image.",
     "Give each link text or an image, otherwise its destination is not described."),
    ("meta-robots-nofollow", "notice", 3,
     "The page tells search engines not to follow its links.",
     "Remove nofollow from the robots meta tag unless you really want the links ignored."),
)

GRADE_GOOD = 80
GRADE_FAIR = 50


class PageError(Exception):
    """A page that could not be read, with a reason a buyer can act on."""

    def __init__(self, reason: str, kind: str = "probe") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


def grade_for(score: int) -> str:
    if score >= GRADE_GOOD:
        return "good"
    if score >= GRADE_FAIR:
        return "needs-improvement"
    return "poor"


def _hit(code: str, facts: dict) -> bool:
    """Is this check triggered by these facts? Each branch reads the fact it names."""
    title = facts.get("title") or ""
    description = facts.get("metaDescription") or ""
    images = int(facts.get("images") or 0)
    missing_alt = int(facts.get("imagesMissingAlt") or 0)
    if code == "title-missing":
        return not title.strip()
    if code == "title-length":
        return bool(title.strip()) and not (TITLE_MIN <= len(title) <= TITLE_MAX)
    if code == "description-missing":
        return not description.strip()
    if code == "description-length":
        return bool(description.strip()) and not (
            DESCRIPTION_MIN <= len(description) <= DESCRIPTION_MAX)
    if code == "h1-missing":
        return int(facts.get("h1Count") or 0) == 0
    if code == "h1-multiple":
        return int(facts.get("h1Count") or 0) > 1
    if code == "canonical-missing":
        return not (facts.get("canonical") or "").strip()
    if code == "noindex":
        return bool(facts.get("noindex"))
    if code == "lang-missing":
        return not (facts.get("htmlLang") or "").strip()
    if code == "viewport-missing":
        return not (facts.get("viewport") or "").strip()
    if code == "images-alt":
        return images > 0 and missing_alt / images > ALT_MISSING_RATIO
    if code == "images-alt-few":
        return images > 0 and 0 < missing_alt and missing_alt / images <= ALT_MISSING_RATIO
    if code == "structured-data-missing":
        return int(facts.get("jsonLdBlocks") or 0) == 0
    if code == "structured-data-invalid":
        return int(facts.get("jsonLdInvalid") or 0) > 0
    if code == "social-missing":
        return int(facts.get("ogCount") or 0) == 0
    if code == "twitter-missing":
        return int(facts.get("twitterCount") or 0) == 0
    if code == "thin-content":
        return int(facts.get("wordCount") or 0) < THIN_CONTENT_WORDS
    if code == "no-internal-links":
        return int(facts.get("internalLinks") or 0) == 0
    if code == "empty-anchors":
        return int(facts.get("emptyAnchors") or 0) > 0
    if code == "meta-robots-nofollow":
        return bool(facts.get("nofollow"))
    return False


def audit(facts: dict) -> dict:
    """Issues, score and grade for one page.

    The score starts at 100 and each triggered check takes its own penalty, so the
    arithmetic is inspectable: the buyer can add the penalties back up and get the
    same number. A missing title and a missing Twitter card are not the same problem,
    and the weights say so.
    """
    issues = []
    penalty = 0
    for code, severity, points, message, fix in CHECKS:
        if _hit(code, facts):
            issues.append({"code": code, "severity": severity, "points": points,
                           "message": message, "fix": fix})
            penalty += points
    score = max(0, min(100, 100 - penalty))
    counts = {"critical": 0, "warning": 0, "notice": 0}
    for issue in issues:
        counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
    order = {"critical": 0, "warning": 1, "notice": 2}
    issues.sort(key=lambda i: (order.get(i["severity"], 3), -i["points"]))
    return {
        "issues": issues,
        "issueCount": len(issues),
        "criticalIssues": counts["critical"],
        "warningIssues": counts["warning"],
        "noticeIssues": counts["notice"],
        "penalty": penalty,
        "seoScore": score,
        "seoGrade": grade_for(score),
    }


def host_list_truncated(hosts: list, total: int | None) -> bool:
    """True when the host list handed to the address guard is a prefix of a longer one."""
    return int(total or 0) > len(hosts)


def redirect_chain(response) -> list:
    """The urls the browser really went through, oldest first."""
    chain: list = []
    request = getattr(response, "request", None) if response is not None else None
    while request is not None:
        url = getattr(request, "url", None)
        if url:
            chain.append(url)
        request = getattr(request, "redirected_from", None)
    chain.reverse()
    return chain


def profile_for(strategy: str) -> dict:
    return PROFILES.get(str(strategy or "mobile").lower(), PROFILES["mobile"])


class Browser:
    """One Chromium for the whole run, warmed once.

    A browser per page pays a cold start on every page: the first navigation of a
    fresh process was measured at 2.9 s against 0.1 s warm, and that lands in the
    audit as a slow page. So the run opens one browser and audits every page in it.
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


async def measure(url: str, *, strategy: str = "mobile", timeout_ms: int = 45_000,
                  settle_ms: int = 1500, ignore_https_errors: bool = False) -> dict:
    """Audit one page in a browser of its own. For tests and one-off checks."""
    browser = Browser()
    await browser.start()
    try:
        return await browser.measure(url, strategy=strategy, timeout_ms=timeout_ms,
                                     settle_ms=settle_ms,
                                     ignore_https_errors=ignore_https_errors)
    finally:
        await browser.close()


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
        page = await context.new_page()
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
        # A javascript rendered page fills in after the load event, so the numbers are
        # read after the network goes quiet, with a short settle window on top for
        # content that arrives from a timer rather than a request.
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

    facts = {
        "title": raw.get("title"),
        "titleLength": _to_int(raw.get("titleLength")),
        "metaDescription": raw.get("metaDescription"),
        "metaDescriptionLength": _to_int(raw.get("metaDescriptionLength")),
        "h1": raw.get("h1"),
        "h1Count": _to_int(raw.get("h1Count")),
        "h2Count": _to_int(raw.get("h2Count")),
        "h3Count": _to_int(raw.get("h3Count")),
        "canonical": raw.get("canonical"),
        "metaRobots": raw.get("metaRobots"),
        "noindex": bool(raw.get("noindex")),
        "nofollow": bool(raw.get("nofollow")),
        "htmlLang": raw.get("htmlLang"),
        "viewport": raw.get("viewport"),
        "charset": raw.get("charset"),
        "ogCount": _to_int(raw.get("ogCount")),
        "ogTitle": raw.get("ogTitle"),
        "ogImage": raw.get("ogImage"),
        "twitterCount": _to_int(raw.get("twitterCount")),
        "jsonLdBlocks": _to_int(raw.get("jsonLdBlocks")),
        "jsonLdTypes": list(raw.get("jsonLdTypes") or []),
        "jsonLdInvalid": _to_int(raw.get("jsonLdInvalid")),
        "images": _to_int(raw.get("images")),
        "imagesMissingAlt": _to_int(raw.get("imagesMissingAlt")),
        "internalLinks": _to_int(raw.get("internalLinks")),
        "externalLinks": _to_int(raw.get("externalLinks")),
        "nofollowLinks": _to_int(raw.get("nofollowLinks")),
        "emptyAnchors": _to_int(raw.get("emptyAnchors")),
        "hreflangCount": _to_int(raw.get("hreflangCount")),
        "wordCount": _to_int(raw.get("wordCount")),
        "bodyChars": _to_int(raw.get("bodyChars")),
        "requests": _to_int(raw.get("requests")),
        "resourceHosts": list(raw.get("resourceHosts") or [])[:MAX_RESOURCE_HOSTS],
        "resourceHostsTotal": _to_int(raw.get("resourceHostsTotal")),
        "httpStatus": status,
        "contentType": content_type,
        "finalUrl": final_url,
        "redirects": chain[:-1] if chain else [],
        "strategy": str(strategy or "mobile").lower(),
        "userAgent": UA.get(str(strategy).lower(), UA["mobile"]),
    }
    facts.update(audit(facts))
    facts["resourceHostsTruncated"] = host_list_truncated(
        facts["resourceHosts"], facts["resourceHostsTotal"])
    return facts
