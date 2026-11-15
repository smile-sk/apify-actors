"""Checks for the detector itself: marks, versions, categories and the money filters.

Run: python tests/test_tech.py

Every case here is a page shape that really happens: a generator meta tag, a platform
cookie, a versioned asset path, a script src, a header nobody can remove while the
product still works. The point of the file is to prove that a signature fires on its own
mark and, just as important, that a plain page fires nothing at all.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import tech  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def found(items) -> list[str]:
    return [item["name"] for item in items]


def signals_of(html="", headers=None, cookies=None, scripts=None, metas=None):
    """None means parse it from the html; an empty list means there is none."""
    return tech.page_signals(html, headers or {}, cookies or [], scripts, metas)


def test_wordpress_generator_and_version():
    html = ('<html><head><meta name="generator" content="WordPress 6.4.2" />'
            '<link rel="stylesheet" href="/wp-content/themes/twentytwenty/style.css">'
            '</head><body>hello</body></html>')
    items = tech.detect(signals_of(html))
    names = found(items)
    ok("WordPress" in names, f"WordPress is recognised from its markup: {names}")
    wordpress = [item for item in items if item["name"] == "WordPress"][0]
    equal(wordpress["version"], "6.4.2", "the generator meta gives the version")
    equal(wordpress["confidence"], "high", "a meta tag is a high confidence mark")
    ok(wordpress["categories"] == ["cms"], f"WordPress is a cms: {wordpress['categories']}")
    ok(any("generator" in item for item in wordpress["evidence"]),
       f"the evidence names the matched mark: {wordpress['evidence']}")
    ok(len(wordpress["evidence"]) <= tech.MAX_EVIDENCE,
       "evidence is capped, one proof is enough to sell a name")


def test_html_only_mark_gives_a_markup_snippet():
    """A page whose only mark is a path in the markup still shows what matched."""
    items = tech.detect(signals_of('<html><body><img src="/wp-content/uploads/a.png">'
                                   '</body></html>'))
    wordpress = [item for item in items if item["name"] == "WordPress"][0]
    equal(wordpress["confidence"], "medium", "markup alone is a medium confidence mark")
    ok(any(item.startswith("html:") for item in wordpress["evidence"]),
       f"the evidence is a snippet of the markup: {wordpress['evidence']}")
    ok("wp-content" in " ".join(wordpress["evidence"]), "and it contains the matched path")


def test_shopify_from_the_platform_header():
    items = tech.detect(signals_of('<html><body>a shop</body></html>',
                                   headers={"X-ShopId": "77123", "Server": "cloudflare"},
                                   cookies=["_shopify_s", "_ga"]))
    names = found(items)
    ok("Shopify" in names, f"Shopify comes from the x-shopid header: {names}")
    ok("Cloudflare" in names, f"and the server header is read too: {names}")
    shopify = [item for item in items if item["name"] == "Shopify"][0]
    equal(shopify["categories"], ["ecommerce"], "Shopify is an ecommerce platform")
    equal(shopify["confidence"], "high", "a header is a high confidence mark")
    ok(any("x-shopid" in item for item in shopify["evidence"]),
       f"the evidence names the header: {shopify['evidence']}")


def test_nextjs_from_the_script_path():
    html = ('<html><body><script src="/_next/static/chunks/main-9d8c.js"></script>'
            '<script id="__NEXT_DATA__" type="application/json">{"page":"/"}</script>'
            '</body></html>')
    items = tech.detect(signals_of(html))
    names = found(items)
    ok("Next.js" in names, f"Next.js is recognised from its own asset path: {names}")
    nextjs = [item for item in items if item["name"] == "Next.js"][0]
    equal(nextjs["confidence"], "medium", "a markup mark is medium confidence")
    equal(nextjs["version"], None, "no version is invented when the vendor prints none")


def test_cookie_marks():
    items = tech.detect(signals_of('<html></html>', cookies=["PHPSESSID", "laravel_session"]))
    names = found(items)
    ok("PHP" in names, f"a PHPSESSID cookie names PHP: {names}")
    ok("Laravel" in names, f"a laravel_session cookie names Laravel: {names}")


def test_a_plain_page_fires_nothing():
    """The most important case: a site with no product must not be charged for a guess."""
    html = ('<html><head><title>Notes</title></head><body><h1>Hello</h1>'
            '<p>A page with no platform, no analytics and no framework.</p></body></html>')
    items = tech.detect(signals_of(html, headers={"server": "some-proxy/1.0"}))
    equal(found(items), [], "a plain page detects nothing")
    equal(tech.categories_present(items), [], "and has no categories")


def test_filters_match_a_part_of_a_name():
    items = tech.detect(signals_of('<html><script src="https://cdn.shopify.com/x.js">'
                                   '</script></html>'))
    equal(found(items), ["Shopify"], "the page is a Shopify shop")
    matched, reason = tech.matches(items, ["shopify"], [])
    ok(matched, "a lowercase filter finds the name")
    equal(reason, None, "and there is no reason to give")
    matched, reason = tech.matches(items, ["next"], [])
    ok(not matched, "a filter for another platform does not match")
    ok("none of the wanted technologies" in (reason or ""),
       f"the reason says it plainly: {reason}")
    matched, reason = tech.matches(items, [], ["shopify"])
    ok(not matched, "an exclusion matches the same way")
    ok("excluded" in (reason or ""), f"the exclusion reason is plain: {reason}")
    matched, reason = tech.matches(items, [], [])
    ok(matched and reason is None, "no filter means everything passes")


def test_wanted_list_is_cleaned():
    equal(tech.normalise_wanted([" Shopify ", "", "shopify", None, "NEXT"]),
          ["shopify", "next"], "the filter list is lowercased and deduplicated")
    equal(tech.normalise_wanted(None), [], "no filter is an empty list, not a crash")


def test_meta_tag_detection_and_case_insensitivity():
    html = '<html><head><meta name="GENERATOR" content="Ghost 5.11"></head></html>'
    items = tech.detect(signals_of(html))
    equal(found(items), ["Ghost"], "a meta tag is found whatever its case")
    equal(items[0]["version"], "5.11", "and its version is read")


def test_analytics_from_script_src():
    html = ('<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABC123">'
            '</script><script src="https://www.googletagmanager.com/gtm.js?id=GTM-XX99">'
            '</script>')
    items = tech.detect(signals_of(html))
    names = found(items)
    ok("Google Analytics 4" in names, f"GA4 is read from its script: {names}")
    ok("Google Tag Manager" in names, f"and GTM from its own: {names}")


def test_scan_is_capped_but_the_head_is_read():
    """A huge body must not cost memory: the marks live in the head."""
    filler = "<p>" + ("x" * 900) + "</p>"
    body = filler * 700  # about 630 KB, past the cap
    html = '<html><head><meta name="generator" content="WordPress 6.4" /></head>' + body
    items = tech.detect(signals_of(html))
    ok("WordPress" in found(items), "a mark inside the read window is still found")
    equal(len((tech.page_signals(html, {}, [], [], {})["p"])), tech.MAX_SCAN_BYTES,
          "the scanned text is cut at the cap")


def test_technology_names_are_stable_and_sorted():
    html = ('<html><head><meta name="generator" content="WordPress 6.1"></head>'
            '<body><script src="/_next/static/x.js"></script>'
            '<script src="https://cdn.shopify.com/y.js"></script></body></html>')
    first = tech.detect(signals_of(html))
    second = tech.detect(signals_of(html))
    equal(found(first), found(second), "the same page gives the same list twice")
    equal(found(first), sorted(found(first), key=lambda name: (
        tech.categories_of(name), name)), "the list is ordered by category then name")


def test_every_signature_is_complete():
    names = [sig["n"] for sig in tech.SIGNATURES]
    equal(len(names), len(set(names)), "no signature is declared twice")
    for sig in tech.SIGNATURES:
        ok(bool(sig.get("c")), f"{sig['n']} has at least one category")
        ok(any(sig.get(key) for key in ("h", "k", "p", "s", "m")),
           f"{sig['n']} has at least one mark")
    ok(len(names) >= 100, f"the table is big enough to be a product: {len(names)}")


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    """A real tag, copied from the live page it was found on. Nothing is invented."""
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def test_a_hash_of_somebody_else_is_not_a_technology():
    """Live blocker: about.gitlab.com carries "modx" inside a Cloudinary hash.

    The tag also carries `data-v-f9a5b6b2`, and GitLab's frontend really is Vue, so the
    fixture does not have to answer "nothing": the point is that MODX, which the hash
    spelled, is not sold.
    """
    items = tech.detect(signals_of(fixture("gitlab_cloudinary_hash.html")))
    names = found(items)
    ok("MODX" not in names, f"a name inside a hash is not that product: {names}")


def test_a_foreign_file_name_is_not_the_product():
    """Live blocker: the BBC loads its own `dotcom-bootstrap.js`, and it was charged."""
    items = tech.detect(signals_of(fixture("bbc_dotcom_bootstrap.html")))
    equal(found(items), [], "a hyphen is a word boundary, but that file is not Bootstrap")


def test_a_real_asset_still_fires():
    """The other half of the same rule: the real asset must still be recognised."""
    items = tech.detect(signals_of(fixture("getbootstrap_asset.html")))
    ok("Bootstrap" in found(items), f"a real bootstrap.min.css is still sold: {found(items)}")


def test_a_quoted_modx_asset_still_fires():
    html = '<html><body><script src="/assets/components/modx.min.js"></script></body></html>'
    ok("MODX" in found(tech.detect(signals_of(html))),
       "a quoted modx asset names MODX")


def test_no_mark_fires_inside_a_longer_token():
    """The class behind both blockers, proved on every signature, not on two of them.

    Each product name is buried the way a hash or somebody else's file name carries it:
    glued to letters on both sides, spaces included. Not one name may be recognised.
    """
    hits = []
    for sig in tech.SIGNATURES:
        token = sig["n"].lower()
        html = (f'<html><body><img src="https://res.example/x/a1b2{token}c3d4.svg">'
                f'</body></html>')
        names = found(tech.detect(signals_of(html)))
        if names:
            hits.append(f"{sig['n']}: {names}")
    equal(hits, [], "a name inside a longer token is not the product")


def test_prose_about_a_framework_is_not_the_framework():
    html = ('<html><body><h1>Why we moved off Symfony last year</h1>'
            '<p>A long story about a php framework and the people who left it.</p>'
            '</body></html>')
    equal(found(tech.detect(signals_of(html))), [],
          "a page that only talks about a framework is free")


def run_all() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"test_tech: {CHECKS[0]} checks, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL", fail)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run_all())
