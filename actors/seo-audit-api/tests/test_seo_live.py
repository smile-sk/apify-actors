"""The browser part: a real Chromium reading real pages.

The pages are built here as data: urls, so the test needs no network and cannot
change under us. That makes the numbers deterministic: if the collector stops
reading the title, or reads alt text from the wrong attribute, these checks fail.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.seo import Browser  # noqa: E402

CHECKS_RUN = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS_RUN[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


GOOD_PAGE = (
    "data:text/html;charset=utf-8,"
    "%3Chtml%20lang%3D%22en%22%3E%3Chead%3E"
    "%3Ctitle%3EA%20complete%20page%20for%20the%20audit%20test%3C/title%3E"
    "%3Cmeta%20name%3D%22description%22%20content%3D%22A%20meta%20description%20that%20is%20long%20enough%20to%20sit%20inside%20the%20published%20window%20without%20being%20cut%20by%20a%20snippet%20trimmer%22%3E"
    "%3Cmeta%20name%3D%22viewport%22%20content%3D%22width%3Ddevice-width%2C%20initial-scale%3D1%22%3E"
    "%3Clink%20rel%3D%22canonical%22%20href%3D%22https%3A%2F%2Fa.test%2F%22%3E"
    "%3Cmeta%20property%3D%22og%3Atitle%22%20content%3D%22Example%22%3E"
    "%3Cmeta%20property%3D%22og%3Adescription%22%20content%3D%22Example%22%3E"
    "%3Cmeta%20property%3D%22og%3Aimage%22%20content%3D%22https%3A%2F%2Fa.test%2Fi.png%22%3E"
    "%3Cmeta%20name%3D%22twitter%3Acard%22%20content%3D%22summary%22%3E"
    "%3Cscript%20type%3D%22application%2Fld%2Bjson%22%3E%7B%22%40type%22%3A%22WebPage%22%7D%3C/script%3E"
    "%3C/head%3E%3Cbody%3E%3Ch1%3EThe%20headline%3C/h1%3E%3Ch2%3Eone%3C/h2%3E"
    "%3Cimg%20src%3D%22data%3Aimage%2Fgif%3Bbase64%2CR0lGODlhAQABAAAAACw%3D%3D%22%20alt%3D%22described%22%3E"
    "%3Ca%20href%3D%22https%3A%2F%2Fb.test%2Fpage-two%22%3Enext%20page%3C/a%3E"
    "%3Cp%3E"
    + "%20".join(["word"] * 200)
    + "%3C/p%3E%3C/body%3E%3C/html%3E"
)

BAD_PAGE = (
    "data:text/html;charset=utf-8,"
    "%3Chtml%3E%3Chead%3E%3Cmeta%20name%3D%22robots%22%20content%3D%22noindex%2C%20nofollow%22%3E"
    "%3C/head%3E%3Cbody%3E"
    "%3Cimg%20src%3D%22data%3Aimage%2Fgif%3Bbase64%2CR0lGODlhAQABAAAAACw%3D%3D%22%3E"
    "%3Cimg%20src%3D%22data%3Aimage%2Fgif%3Bbase64%2CR0lGODlhAQABAAAAACw%3D%3D%22%3E"
    "%3C/body%3E%3C/html%3E"
)


async def main() -> int:
    browser = Browser()
    try:
        good = await browser.measure(GOOD_PAGE, settle_ms=0)
        equal(good["title"], "A complete page for the audit test", "the title is read")
        equal(good["titleLength"], 34, "and its length is counted")
        ok(good["metaDescriptionLength"] > 70, "the meta description is read")
        equal(good["h1Count"], 1, "one h1 is counted")
        equal(good["h1"], "The headline", "its text is read")
        equal(good["h2Count"], 1, "h2 is counted")
        equal(good["canonical"], "https://a.test/", "the canonical is read")
        equal(good["htmlLang"], "en", "the language is read")
        ok(good["viewport"], "the viewport tag is read")
        equal(good["ogCount"], 3, "three open graph tags are counted")
        equal(good["twitterCount"], 1, "the twitter card is counted")
        equal(good["jsonLdBlocks"], 1, "one json-ld block is counted")
        equal(good["jsonLdTypes"], ["WebPage"], "its type is read")
        equal(good["jsonLdInvalid"], 0, "and it is valid")
        equal(good["images"], 1, "one image is counted")
        equal(good["imagesMissingAlt"], 0, "with alt text")
        # A data: url has no origin, so a relative href cannot be resolved against one.
        # The link is absolute here on purpose: a link is parsed, never fetched.
        equal(good["externalLinks"], 1, "an absolute link is counted as external")
        ok(good["wordCount"] >= 200, "the readable words are counted")
        ok(good["noindex"] is False, "this page is indexable")
        ok(good["seoScore"] >= 80, f"a complete page scores high, got {good['seoScore']}")
        equal(good["seoGrade"], "good", "and is called good")
        ok(good["httpStatus"] in (None, 200), "a data url carries no http status of its own")
        ok(good["resourceHostsTruncated"] is False, "a page with no hosts is not truncated")

        bad = await browser.measure(BAD_PAGE, settle_ms=0)
        ok(bad["title"] is None, "a page without a title reads as no title")
        ok(bad["noindex"] is True, "noindex is recognised")
        ok(bad["nofollow"] is True, "nofollow is recognised")
        equal(bad["h1Count"], 0, "and it has no h1")
        equal(bad["imagesMissingAlt"], 2, "both images are counted as missing alt")
        bad_codes = [issue["code"] for issue in bad["issues"]]
        ok("noindex" in bad_codes, "noindex is an issue")
        ok("title-missing" in bad_codes, "a missing title is an issue")
        ok(bad["seoScore"] < 50, f"a broken page scores low, got {bad['seoScore']}")
        equal(bad["seoGrade"], "poor", "and is called poor")
    finally:
        await browser.close()

    if FAILS:
        print(f"FAIL {len(FAILS)} of {CHECKS_RUN[0]} checks")
        for line in FAILS:
            print("  -", line)
        return 1
    print(f"OK {CHECKS_RUN[0]} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
