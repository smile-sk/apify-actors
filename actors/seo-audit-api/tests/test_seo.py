"""Score checks for the SEO audit, with no browser involved.

The score is arithmetic over facts, so it is tested as arithmetic: every check is
triggered on purpose, the penalty is compared with the published weight, and the
sum of penalties is compared with 100 minus the score. A test that only asserts
"score is a number" would pass on any bug.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.seo import (  # noqa: E402
    CHECKS, GRADE_FAIR, GRADE_GOOD, audit, grade_for, host_list_truncated,
    redirect_chain,
)

CHECKS_RUN = [0]
FAILS: list[str] = []

WEIGHTS = {code: points for code, _severity, points, _m, _f in CHECKS}
SEVERITIES = {code: severity for code, severity, _p, _m, _f in CHECKS}


def ok(condition, message: str) -> None:
    CHECKS_RUN[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def page(**overrides) -> dict:
    base = {
        "title": "Example domain page for the audit", "titleLength": 33,
        "metaDescription": (
            "A meta description that sits inside the published window and is neither "
            "cut short nor padded until a search engine trims it."),
        "metaDescriptionLength": 120,
        "h1": "Example", "h1Count": 1, "h2Count": 2, "h3Count": 1,
        "canonical": "https://a.test/", "metaRobots": None, "noindex": False,
        "nofollow": False, "htmlLang": "en",
        "viewport": "width=device-width, initial-scale=1", "charset": "utf-8",
        "ogCount": 3, "ogTitle": "Example", "ogImage": "https://a.test/i.png",
        "twitterCount": 2, "jsonLdBlocks": 1, "jsonLdTypes": ["WebPage"],
        "jsonLdInvalid": 0, "images": 4, "imagesMissingAlt": 0, "internalLinks": 5,
        "externalLinks": 2, "nofollowLinks": 0, "emptyAnchors": 0, "hreflangCount": 0,
        "wordCount": 400,
    }
    base["metaDescriptionLength"] = len(base["metaDescription"])
    base.update(overrides)
    return base


def codes(result) -> list:
    return [issue["code"] for issue in result["issues"]]


def test_a_clean_page_scores_one_hundred():
    result = audit(page())
    equal(result["seoScore"], 100, "a page with every signal scores 100")
    equal(result["seoGrade"], "good", "and it is called good")
    equal(result["issueCount"], 0, "with no issues at all")
    equal(result["penalty"], 0, "and no penalty")


def test_a_missing_title_costs_its_published_weight():
    result = audit(page(title=None, titleLength=0))
    ok("title-missing" in codes(result), "a missing title is named")
    equal(result["seoScore"], 100 - WEIGHTS["title-missing"], "the score drops by the weight")
    equal(SEVERITIES["title-missing"], "critical", "and it is critical, not cosmetic")


def test_a_short_title_is_a_warning_not_a_critical():
    result = audit(page(title="Hi", titleLength=2))
    ok("title-length" in codes(result), "a two character title is outside the window")
    ok("title-missing" not in codes(result), "but the title does exist")
    equal(result["criticalIssues"], 0, "so nothing is critical here")


def test_a_long_description_is_a_warning():
    long_text = "x" * 200
    result = audit(page(metaDescription=long_text, metaDescriptionLength=200))
    ok("description-length" in codes(result), "a 200 character description is trimmed")
    ok("description-missing" not in codes(result), "it is not missing")


def test_two_h1_are_reported():
    result = audit(page(h1Count=2))
    ok("h1-multiple" in codes(result), "two h1 are named")
    ok("h1-missing" not in codes(result), "and no h1 is not claimed at the same time")


def test_noindex_is_the_heaviest_single_check():
    result = audit(page(noindex=True, metaRobots="noindex, follow"))
    ok("noindex" in codes(result), "noindex is named")
    equal(result["seoScore"], 100 - WEIGHTS["noindex"], "and it costs the most")
    ok(WEIGHTS["noindex"] == max(WEIGHTS.values()), "no index weight is higher")


def test_images_without_alt_are_split_into_two_severities():
    heavy = audit(page(images=10, imagesMissingAlt=5))
    ok("images-alt" in codes(heavy), "half the images without alt is a warning")
    ok("images-alt-few" not in codes(heavy), "and not also the mild one")
    mild = audit(page(images=10, imagesMissingAlt=1))
    ok("images-alt-few" in codes(mild), "one image in ten is a notice")
    ok("images-alt" not in codes(mild), "and not the warning")
    none = audit(page(images=0, imagesMissingAlt=0))
    ok("images-alt" not in codes(none) and "images-alt-few" not in codes(none),
       "a page without images is not punished for alt text")


def test_broken_structured_data_is_named_separately():
    broken = audit(page(jsonLdInvalid=1))
    ok("structured-data-invalid" in codes(broken), "a broken json-ld block is named")
    ok("structured-data-missing" not in codes(broken), "and not reported as missing")
    missing = audit(page(jsonLdBlocks=0))
    ok("structured-data-missing" in codes(missing), "no blocks at all is a notice")


def test_thin_content_is_a_warning():
    result = audit(page(wordCount=40))
    ok("thin-content" in codes(result), "forty words is thin")


def test_every_missing_signal_is_caught():
    cases = {
        "canonical-missing": page(canonical=None),
        "viewport-missing": page(viewport=None),
        "lang-missing": page(htmlLang=None),
        "social-missing": page(ogCount=0),
        "twitter-missing": page(twitterCount=0),
        "no-internal-links": page(internalLinks=0),
        "empty-anchors": page(emptyAnchors=2),
        "meta-robots-nofollow": page(nofollow=True),
        "description-missing": page(metaDescription=None, metaDescriptionLength=0),
        "h1-missing": page(h1Count=0),
    }
    for code, facts in cases.items():
        result = audit(facts)
        ok(code in codes(result), f"{code} is triggered by its own fact")


def test_the_penalties_add_up_to_the_score():
    result = audit(page(title=None, titleLength=0, h1Count=0, canonical=None))
    total = sum(issue["points"] for issue in result["issues"])
    equal(result["penalty"], total, "the penalty is the sum of the issue weights")
    equal(result["seoScore"], max(0, 100 - total), "and the score is 100 minus that sum")


def test_the_score_never_goes_below_zero():
    everything = audit(page(
        title=None, titleLength=0, metaDescription=None, metaDescriptionLength=0,
        h1Count=0, canonical=None, noindex=True, htmlLang=None, viewport=None,
        images=4, imagesMissingAlt=4, jsonLdBlocks=0, jsonLdInvalid=0, ogCount=0,
        twitterCount=0, wordCount=0, internalLinks=0, emptyAnchors=3, nofollow=True))
    equal(everything["seoScore"], 0, "a page with everything wrong scores 0")
    equal(everything["seoGrade"], "poor", "and it is called poor")
    ok(everything["penalty"] > 100, "the penalty can exceed 100 and the score still floors")


def test_issues_are_sorted_by_severity():
    result = audit(page(title=None, titleLength=0, twitterCount=0, images=10,
                        imagesMissingAlt=5))
    order = {"critical": 0, "warning": 1, "notice": 2}
    ranks = [order[issue["severity"]] for issue in result["issues"]]
    equal(ranks, sorted(ranks), "critical issues come first, notices last")
    equal(result["issues"][0]["severity"], "critical", "the first issue is critical")


def test_every_issue_carries_a_fix():
    everything = audit(page(title=None, titleLength=0, h1Count=0, jsonLdBlocks=0))
    ok(all(issue["fix"] for issue in everything["issues"]), "every issue says what to do")
    ok(all(issue["message"] for issue in everything["issues"]),
       "and what is wrong, in a sentence")


def test_grades_have_exact_boundaries():
    equal(grade_for(GRADE_GOOD), "good", "80 is good")
    equal(grade_for(GRADE_GOOD - 1), "needs-improvement", "79 is not good")
    equal(grade_for(GRADE_FAIR), "needs-improvement", "50 is still improvable")
    equal(grade_for(GRADE_FAIR - 1), "poor", "49 is poor")
    equal(grade_for(0), "poor", "zero is poor")


def test_audit_does_not_change_the_facts():
    facts = page(title=None, titleLength=0)
    before = dict(facts)
    audit(facts)
    equal(facts, before, "the facts handed in are not rewritten")


def test_the_host_list_cut_is_visible():
    ok(host_list_truncated(["a", "b"], 5) is True, "a longer total than the list is a cut")
    ok(host_list_truncated(["a", "b"], 2) is False, "the same size is not a cut")
    ok(host_list_truncated(["a"], 0) is False, "an unknown total is not a cut")


def test_the_redirect_chain_is_built_oldest_first():
    class Response:
        pass

    third = Response()
    second = Response()
    first = Response()
    third.request = type("R", (), {"url": "https://c.test/", "redirected_from": second.request
                                   if hasattr(second, "request") else None})()
    second.request = type("R", (), {"url": "https://b.test/", "redirected_from": None})()
    third.request.redirected_from = second.request
    second.request.redirected_from = first.request = type(
        "R", (), {"url": "https://a.test/", "redirected_from": None})()
    chain = redirect_chain(third)
    equal(chain, ["https://a.test/", "https://b.test/", "https://c.test/"],
          "the chain reads from the first hop to the last")


def run_all() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILS:
        print(f"FAIL {len(FAILS)} of {CHECKS_RUN[0]} checks")
        for line in FAILS:
            print("  -", line)
        return 1
    print(f"OK {CHECKS_RUN[0]} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_all())
