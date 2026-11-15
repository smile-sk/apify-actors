"""Engine checks for the metadata actor: parsing, absolute URLs, charsets, statuses.

Run: python tests/test_meta.py
No network is used here except the loopback classification, which is local.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import meta

FAILS = []
CHECKS = [0]


def ok(cond, name, extra=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, extra))


FULL = """<!doctype html>
<html lang="en-GB">
<head>
<base href="https://cdn.example.com/assets/">
<title>Tom &amp; Jerry - Cartoons</title>
<meta name="description" content="Classic cat and mouse.">
<meta property="og:title" content="Tom and Jerry">
<meta property="og:description" content="Cat vs mouse.">
<meta property="og:image" content="../img/cover.png">
<meta property="og:type" content="video.other">
<meta property="og:site_name" content="Cartoons">
<meta property="og:url" content="/tom-and-jerry">
<meta property="og:locale" content="en_GB">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="//cdn.example.com/tw.png">
<link rel="canonical" href="/tom-and-jerry">
<link rel="icon" href="/fav.ico">
<link rel="apple-touch-icon" href="touch.png">
<link rel="manifest" href="/site.webmanifest">
<link rel="alternate" hreflang="uk" href="/uk/tom">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"VideoObject","name":"x"}</script>
<script type="application/ld+json">{"@graph":[{"@type":"BreadcrumbList"},{"@type":"WebPage"}]}</script>
</head>
<body>hi</body></html>
"""


def test_full_page():
    row = meta.parse_page(FULL, "https://www.example.com/tom")
    ok(row["status"] == "EXTRACTED", "a full page is extracted", row["status"])
    ok(row["title"] == "Tom and Jerry", "og:title wins for a preview", row["title"])
    ok(row["description"] == "Cat vs mouse.", "og:description wins", row["description"])
    ok(row["lang"] == "en-GB", "language from html tag", row["lang"])
    ok(row["metaCount"] == 10, "every meta tag is counted", row["metaCount"])
    ok(row["openGraph"]["image"] == "https://cdn.example.com/img/cover.png",
       "a relative image is resolved against base href", row["openGraph"]["image"])
    ok(row["openGraph"]["url"] == "https://cdn.example.com/tom-and-jerry",
       "og:url is absolute", row["openGraph"]["url"])
    ok(row["twitterCard"]["image"] == "https://cdn.example.com/tw.png",
       "a protocol relative image gets https", row["twitterCard"]["image"])
    ok(row["canonical"] == "https://cdn.example.com/tom-and-jerry", "canonical is absolute", row["canonical"])
    ok(row["icons"]["favicon"] == "https://cdn.example.com/fav.ico", "favicon is absolute", row["icons"]["favicon"])
    ok(row["icons"]["appleTouchIcon"] == "https://cdn.example.com/assets/touch.png",
       "apple touch icon is absolute", row["icons"]["appleTouchIcon"])
    ok(row["icons"]["manifest"] == "https://cdn.example.com/site.webmanifest", "manifest is absolute")
    ok(row["hreflang"] == [{"lang": "uk", "href": "https://cdn.example.com/uk/tom"}],
       "hreflang is absolute and lowercased", row["hreflang"])
    ok(row["jsonLd"]["count"] == 2, "two json-ld blocks", row["jsonLd"])
    ok(row["jsonLd"]["types"] == ["VideoObject", "BreadcrumbList", "WebPage"],
       "types include @graph children", row["jsonLd"]["types"])
    ok(row["preview"]["ready"] is True, "the preview is ready", row["preview"])
    ok("image_was_relative" in row["preview"]["issues"],
       "a relative image is reported, not hidden", row["preview"]["issues"])
    ok("no_title" not in row["preview"]["issues"], "no false complaint about the title")


def test_title_only_page():
    row = meta.parse_page("<html><head><title>Just a title</title></head><body>x</body></html>",
                          "https://a.test/")
    ok(row["status"] == "EXTRACTED", "a title is metadata", row["status"])
    ok(row["preview"]["ready"] is False, "no description and no image: not ready")
    for issue in ("no_description", "no_image", "no_open_graph"):
        ok(issue in row["preview"]["issues"], "issue %s is named" % issue, row["preview"]["issues"])


def test_js_shell():
    html = ('<html><head><script src="/app.js"></script>'
            '<meta name="viewport" content="width=device-width"></head>'
            '<body><div id="root"></div></body></html>')
    row = meta.parse_page(html, "https://spa.test/")
    ok(row["status"] == "NO_METADATA", "a browser shell has no metadata", row["status"])
    ok("renders in the browser" in row["reason"], "the reason says why", row["reason"])


def test_twitter_fallback():
    html = ('<html><head><meta name="twitter:title" content="Tw title">'
            '<meta name="twitter:description" content="Tw desc">'
            '<meta name="twitter:image" content="https://x.test/i.png"></head></html>')
    row = meta.parse_page(html, "https://x.test/")
    ok(row["title"] == "Tw title", "twitter title is a fallback", row["title"])
    ok(row["description"] == "Tw desc", "twitter description is a fallback", row["description"])
    ok(row["openGraph"]["image"] is None, "no og image when the page has none")
    ok("no_open_graph" in row["preview"]["issues"], "a page without og is reported")
    ok(row["preview"]["ready"] is True, "twitter data still makes a preview")


def test_data_uri_image_is_not_an_image():
    html = '<html><head><title>T</title><meta property="og:image" content="data:image/png;base64,AAAA"></head></html>'
    row = meta.parse_page(html, "https://a.test/")
    ok(row["openGraph"]["image"] is None, "a data: URI is not delivered as an image")
    ok("no_image" in row["preview"]["issues"], "and it is reported")


def test_long_fields_and_entities():
    long_title = "T" * 70
    long_desc = "D" * 200
    html = ('<html><head><title>Tom &amp; Jerry</title>'
            '<meta property="og:title" content="%s">'
            '<meta property="og:description" content="%s">'
            '<meta property="og:image" content="https://a.test/i.png">'
            '</head></html>' % (long_title, long_desc))
    row = meta.parse_page(html, "https://a.test/")
    ok(row["title"] == long_title, "the og title is kept as is")
    ok("title_longer_than_60" in row["preview"]["issues"], "a long title is flagged")
    ok("description_longer_than_160" in row["preview"]["issues"], "a long description is flagged")
    short = meta.parse_page("<html><head><title>Tom &amp; Jerry</title></head></html>", "https://a.test/")
    ok(short["title"] == "Tom & Jerry", "entities in the title are decoded", short["title"])


def test_whitespace_is_collapsed():
    html = '<html><head><title>\n   Many   spaces\t here \n</title></head></html>'
    row = meta.parse_page(html, "https://a.test/")
    ok(row["title"] == "Many spaces here", "the title is cleaned", repr(row["title"]))


def test_empty_page():
    row = meta.parse_page("", "https://a.test/")
    ok(row["status"] == "NO_METADATA", "an empty body is not metadata", row["status"])
    ok("came back empty" in row["reason"], "and the reason says so", row["reason"])


def test_charset_from_meta():
    text = '<html><head><meta charset="windows-1251"><title>\u041f\u0440\u0438\u0432\u0435\u0442</title></head></html>'
    body = text.encode("cp1251")
    decoded, charset = meta.decode_body(body, "text/html")
    ok(charset == "windows-1251", "the meta charset is used", charset)
    ok("\u041f\u0440\u0438\u0432\u0435\u0442" in decoded, "the page is decoded without mojibake")


def test_charset_from_header_wins():
    text = '<html><head><meta charset="windows-1251"><title>ok</title></head></html>'
    body = text.encode("utf-8")
    decoded, charset = meta.decode_body(body, "text/html; charset=utf-8")
    ok(charset == "utf-8", "the header charset is trusted first", charset)
    ok("ok" in decoded, "and decoding works")


def test_bom_is_stripped():
    body = b"\xef\xbb\xbf" + "<html><head><title>Bom</title></head></html>".encode("utf-8")
    decoded, charset = meta.decode_body(body, "text/html")
    ok(charset == "utf-8", "a BOM means utf-8", charset)
    ok(decoded.startswith("<html>"), "the BOM is not left in the text", decoded[:10])


def test_normalize_url():
    cases = [
        ("example.com", "https://example.com"),
        ("//example.com/x", "https://example.com/x"),
        ("http://example.com/a", "http://example.com/a"),
        (" https://example.com ", "https://example.com"),
    ]
    for raw, expected in cases:
        ok(meta.normalize_url(raw) == expected, "normalise %r" % raw, meta.normalize_url(raw))
    for raw in ("", None, "javascript:alert(1)", "ftp://example.com", "hello"):
        ok(meta.normalize_url(raw) is None, "reject %r" % (raw,), meta.normalize_url(raw))


def test_absolute():
    ok(meta.absolute("https://a.test/x/", "/y.png") == "https://a.test/y.png", "root relative")
    ok(meta.absolute("https://a.test/x/", "y.png") == "https://a.test/x/y.png", "path relative")
    ok(meta.absolute("https://a.test/x/", "//cdn.test/i.png") == "https://cdn.test/i.png", "protocol relative")
    ok(meta.absolute("https://a.test/x/", "data:image/png;base64,AA") is None, "data URI rejected")
    ok(meta.absolute("https://a.test/x/", "javascript:void(0)") is None, "javascript rejected")
    ok(meta.absolute("https://a.test/x/", "   ") is None, "blank rejected")


def test_private_networks():
    for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.1.1", "::1", "fd00::1",
               "100.64.0.1", "100.127.255.254"):
        ok(meta.ip_is_private(ip) is True, "%s is private" % ip)
    for ip in ("8.8.8.8", "2606:4700::1111", "not-an-ip"):
        ok(meta.ip_is_private(ip) is False, "%s is public" % ip)


def test_private_target_is_blocked():
    import asyncio
    row = asyncio.run(meta.read_page(None, "http://127.0.0.1/admin"))
    ok(row["status"] == "PRIVATE_TARGET", "loopback is blocked before any request", row["status"])
    ok("private" in row["reason"], "and the reason says why", row["reason"])


def test_invalid_url_row():
    import asyncio
    row = asyncio.run(meta.read_page(None, "javascript:alert(1)"))
    ok(row["status"] == "INVALID_URL", "a non-http line is not fetched", row["status"])


def test_missing_parser_is_reported_not_crashed():
    old = meta.HTMLParser
    meta.HTMLParser = None
    try:
        row = meta.parse_page(FULL, "https://a.test/")
        ok(row["status"] == "ERROR", "a missing parser is a status, not a crash", row["status"])
    finally:
        meta.HTMLParser = old


def test_mapped_ipv6_is_private():
    for ip in ("::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:10.0.0.5", "::FFFF:192.168.1.1"):
        ok(meta.ip_is_private(ip) is True, "%s is private" % ip)
    ok(meta.ip_is_private("::ffff:8.8.8.8") is False, "a mapped public address stays public")


def test_mapped_ipv6_target_is_blocked():
    import asyncio
    row = asyncio.run(meta.read_page(None, "http://[::ffff:127.0.0.1]/admin"))
    ok(row["status"] == "PRIVATE_TARGET", "a mapped loopback is blocked before the request", row["status"])


def test_redirect_answer_is_reported_not_parsed():
    import asyncio
    import httpx

    async def go(handler, max_redirects):
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            return await meta.read_page(client, "https://a.test/start", max_redirects=max_redirects)

    def moved(request):
        return httpx.Response(301, headers={"location": "https://a.test/final"},
                              content=b"<html><head><title>301 Moved Permanently</title></head></html>")

    def no_target(request):
        return httpx.Response(304, content=b"")

    old = meta.host_classify

    async def fake_classify(url):
        return "ok"

    meta.host_classify = fake_classify
    try:
        row = asyncio.run(go(moved, 0))
        ok(row["status"] == "REDIRECT", "a redirect with maxRedirects 0 is a redirect, not a page",
           row["status"])
        ok(row["title"] is None, "the redirect body is not parsed as a page", row["title"])
        ok(row["statusCode"] == 301, "the code is reported", row["statusCode"])
        ok(row["errorType"] == "redirect", "the error type is redirect", row["errorType"])
        ok("maxRedirects is 0" in row["reason"], "the reason names the setting", row["reason"])
        blank = asyncio.run(go(no_target, 5))
        ok(blank["status"] == "REDIRECT", "a 3xx that names no target is a redirect", blank["status"])
        ok("named no target" in blank["reason"], "the reason says the answer named no target", blank["reason"])
    finally:
        meta.host_classify = old


def test_redirect_is_followed_when_allowed():
    import asyncio
    import httpx

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              content=b"<html><head><title>Landing</title></head></html>")

    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            return await meta.read_page(client, "https://a.test/start", max_redirects=5)

    old = meta.host_classify

    async def fake_classify(url):
        return "ok"

    meta.host_classify = fake_classify
    try:
        row = asyncio.run(go())
        ok(row["status"] == "EXTRACTED", "a followed redirect lands on the page", row["status"])
        ok(row["title"] == "Landing", "the landing page is parsed", row["title"])
        ok(row["redirectCount"] == 1, "the hop is counted", row["redirectCount"])
    finally:
        meta.host_classify = old


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
