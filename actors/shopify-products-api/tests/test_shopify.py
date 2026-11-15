"""Checks for the storefront reader: parsing, the fallback chain and the honesty paths."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import pathlib

from src import shopify

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class StubHosts:
    """Replace the DNS public-host check for chain tests: they are about paths, not DNS.

    The real check is proven on its own in test_host_is_public, with literals that need no
    network, and in test_sitemap_cannot_reach_private_or_foreign_addresses.
    """

    def __init__(self, allowed=(True,)):
        self.allowed = allowed

    def __enter__(self):
        self.original = shopify.host_is_public
        shopify.host_is_public = lambda host: True
        return self

    def __exit__(self, *exc):
        shopify.host_is_public = self.original
        return False


class FakeLog:
    def __init__(self):
        self.messages = []

    def info(self, *args):
        self.messages.append(("info", args))

    def warning(self, *args):
        self.messages.append(("warning", args))

    def error(self, *args):
        self.messages.append(("error", args))


class FakeResponse:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class FakeClient:
    """A stand-in httpx client: answers by substring, records what was asked."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, headers=None):
        self.calls.append(url)
        for pattern, answer in self.routes.items():
            if pattern in url:
                return answer(url) if callable(answer) else answer
        return FakeResponse(404)


def _fetcher(routes):
    return shopify.Fetcher(FakeClient(routes), FakeLog())


def _products():
    return json.loads((FIXTURES / "allbirds_products.json").read_text(encoding="utf-8"))["products"]


def _ld_html():
    block = (FIXTURES / "gymshark_product_ld.json").read_text(encoding="utf-8")
    return '<html><head><script type="application/ld+json">%s</script></head></html>' % block


# --- input normalisation ---------------------------------------------------------------

def test_normalize_target():
    assert shopify.normalize_target("allbirds.com") == {
        "raw": "allbirds.com", "store": "allbirds.com", "handle": None,
        "base": "https://allbirds.com"}
    got = shopify.normalize_target("https://www.allbirds.com/products/womens-wool-runner?x=1")
    assert got["store"] == "www.allbirds.com" and got["handle"] == "womens-wool-runner", got
    got = shopify.normalize_target("kyliecosmetics.com/products/cosmic-kit.json")
    assert got["handle"] == "cosmic-kit", got
    for bad in ("", "   ", "localhost", "javascript:alert(1)", "ftp://x.com", "no-dot"):
        assert shopify.normalize_target(bad) is None, bad
    return "normalize_target: store, handle, and six rejected lines"


def test_host_is_public():
    assert shopify.host_is_public("127.0.0.1") is False
    assert shopify.host_is_public("10.0.0.5") is False
    assert shopify.host_is_public("localhost") is False
    assert shopify.host_is_public("printer.local") is False
    # an IPv4-mapped IPv6 literal is link-local even though it looks like IPv6
    assert shopify.host_is_public("::ffff:169.254.169.254") is False
    # CGNAT 100.64.0.0/10 is not private by flag, only by is_global.
    assert shopify.host_is_public("100.64.0.1") is False
    assert shopify.host_is_public("100.127.255.254") is False
    assert shopify.host_is_public("::ffff:127.0.0.1") is False
    assert shopify.host_is_public("1.1.1.1") is True
    return "host_is_public: loopback, private, .local, mapped IPv6 blocked, 1.1.1.1 allowed"


# --- parsing ---------------------------------------------------------------------------

def test_normalize_product_real_catalog():
    products = _products()
    first = shopify.normalize_product(products[0], "www.allbirds.com", "json", "USD")
    assert first["price"] == 49.0, first
    assert first["priceMax"] == 49.0, first
    assert first["compareAtPrice"] == 110.0, first
    assert first["currency"] == "USD"
    assert first["availability"] == "in_stock"
    assert first["variantCount"] == len(products[0]["variants"])
    assert first["source"] == "json"
    assert first["productUrl"].endswith(first["handle"]), first["productUrl"]
    assert first["imageUrl"].startswith("https://cdn.shopify.com/"), first["imageUrl"]
    second = shopify.normalize_product(products[1], "www.allbirds.com", "json", "USD")
    assert second["availability"] == "out_of_stock", second
    assert second["compareAtPrice"] == 125.0, second
    return "normalize_product on a real allbirds product: min price, compare-at, stock"


def test_normalize_product_without_price():
    product = {"handle": "gift-card", "title": "Gift card", "variants": [
        {"sku": None, "title": "Default", "price": None, "compare_at_price": None,
         "available": True}]}
    row = shopify.normalize_product(product, "shop.com", "json", None)
    assert row["price"] is None, "a missing price is unknown, never zero"
    assert row["priceMax"] is None
    assert row["currency"] is None
    assert row["availability"] == "in_stock"
    return "normalize_product: no price stays None and is not read as zero"


def test_parse_ld_productgroup_real_page():
    product, currency = shopify.parse_ld_product(_ld_html(), "https://www.gymshark.com/products/x")
    assert product is not None
    assert product["title"] == "Fit Seamless Cropped Leggings", product["title"]
    assert len(product["variants"]) == 3, len(product["variants"])
    assert currency == "USD", currency
    assert product["variants"][0]["price"] == 9.0
    assert product["variants"][1]["available"] is True
    assert product["variants"][0]["available"] is False
    assert product["handle"] == "gymshark-fit-seamless-cropped-leggings-smokey-grey", product["handle"]
    row = shopify.normalize_product(product, "www.gymshark.com", "page", currency)
    assert row["price"] == 9.0 and row["variantCount"] == 3
    return "parse_ld_product: real ProductGroup with hasVariant offers, 3 variants, USD"


def test_parse_ld_plain_product_and_empty():
    html = ('<script type="application/ld+json">{"@type":"Product","name":"Sock",'
            '"offers":[{"price":"12.50","priceCurrency":"EUR",'
            '"availability":"https://schema.org/InStock"}],"sku":"S1"}</script>')
    product, currency = shopify.parse_ld_product(html, "https://shop.com/products/sock")
    assert product["title"] == "Sock" and currency == "EUR"
    assert product["variants"][0]["price"] == 12.5
    assert product["variants"][0]["available"] is True
    assert shopify.parse_ld_product("<html><body>no ld</body></html>", "https://shop.com") == (None, None)
    assert shopify.parse_ld_product("", "https://shop.com") == (None, None)
    return "parse_ld_product: plain Product with offer list, and two pages with nothing"


def test_sitemap_locs_skip_locale_copies():
    index = (FIXTURES / "gymshark_sitemap_index.xml").read_text(encoding="utf-8")
    locs = shopify.LOC_RE.findall(index)
    maps = [url for url in locs if shopify.PRODUCTS_MAP_RE.search(url)]
    assert "https://www.gymshark.com/sitemap_products_1.xml" in maps, maps
    root = [url for url in maps if shopify.urlparse(url).path.count("/") == 1]
    assert root == ["https://www.gymshark.com/sitemap_products_1.xml"], root
    product_map = (FIXTURES / "gymshark_sitemap_products.xml").read_text(encoding="utf-8")
    urls = [url for url in shopify.LOC_RE.findall(product_map) if "/products/" in url]
    assert len(urls) == 5 and urls[0].endswith("/products/gift-card"), urls
    return "sitemap: product maps found, locale copies skipped, 2 product urls extracted"


# --- fetcher honesty -------------------------------------------------------------------

def test_same_site_is_not_two_labels():
    """shop.co.uk and attacker.co.uk share their last two labels, and are not one site."""
    assert shopify._same_site("https://shop.co.uk", "https://attacker.co.uk") is False
    assert shopify._same_site("https://www.shop.co.uk", "https://shop.co.uk") is True
    assert shopify._same_site("https://shop.co.uk", "https://cdn.shop.co.uk") is True
    assert shopify._same_site("https://a.com", "https://b.com") is False
    return "same site: a two-level suffix needs three labels, subdomains still match"


def test_fetcher_never_parses_a_redirect():
    async def run():
        client = FakeClient({"/products.json": FakeResponse(301, "<html>Moved</html>")})
        fetcher = shopify.Fetcher(client, FakeLog())
        status, text, error = await fetcher.get("https://shop.com/products.json",
                                                shopify.JSON_ACCEPT)
        assert status == 301, status
        assert text == "", "a redirect body must never come back as a page"
        assert "redirect not followed" in error, error

        client = FakeClient({"/a": FakeResponse(302, "", {"location": "https://shop.com/b"}),
                             "/b": FakeResponse(200, '{"ok":true}')})
        fetcher = shopify.Fetcher(client, FakeLog())
        status, text, error = await fetcher.get("https://shop.com/a", shopify.JSON_ACCEPT)
        assert status == 200 and text == '{"ok":true}', (status, text, error)
        assert client.calls == ["https://shop.com/a", "https://shop.com/b"], client.calls

        client = FakeClient({"/a": FakeResponse(302, "", {"location": "https://evil.example/x"})})
        fetcher = shopify.Fetcher(client, FakeLog())
        status, text, error = await fetcher.get("https://shop.com/a", shopify.JSON_ACCEPT)
        assert status == 302 and text == "", (status, text)
        assert "off the store" in error, error

    asyncio.run(run())
    return "fetcher: 301 body is never content, same-site redirect followed once, off-site refused"


# --- the fallback chain ----------------------------------------------------------------

def test_read_store_from_catalog_json():
    async def run():
        products = _products()
        fetcher = _fetcher({
            "/meta.json": FakeResponse(200, json.dumps({"name": "Allbirds", "currency": "USD"})),
            "/products.json": FakeResponse(200, json.dumps({"products": products})),
        })
        result = await shopify.read_store(fetcher, shopify.normalize_target("allbirds.com"),
                                          250, FakeLog())
        assert result["status"] == "ok" and result["source"] == "json", result
        assert len(result["products"]) == 2, len(result["products"])
        assert result["currency"] == "USD" and result["shop"] == "Allbirds"
        return result

    result = asyncio.run(run())
    assert result["products"][0]["price"] == 49.0
    return "read_store: catalog json path, 2 products, currency from meta.json"


def test_read_store_falls_back_to_sitemap_and_pages():
    async def run():
        page = _ld_html()
        product_map = (FIXTURES / "gymshark_sitemap_products.xml").read_text(encoding="utf-8")
        index = (FIXTURES / "gymshark_sitemap_index.xml").read_text(encoding="utf-8")

        def products_json(url):
            # first page refuses, which is what gymshark.com does to a datacenter address
            return FakeResponse(403)

        def page_answer(url):
            # one product page answers, the other one is dead: both must be counted
            if url.endswith("/products/gift-card"):
                return FakeResponse(404)
            if url.endswith("gymshark-fit-seamless-cropped-leggings-smokey-grey"):
                return FakeResponse(200, page)
            return FakeResponse(404)

        fetcher = _fetcher({
            "/meta.json": FakeResponse(200, json.dumps({"name": "Gymshark", "currency": "USD"})),
            "/products.json": products_json,
            "/sitemap.xml": FakeResponse(200, index),
            "/sitemap_products_1.xml": FakeResponse(200, product_map),
            "/products/": page_answer,
        })
        with StubHosts():
            result = await shopify.read_store(fetcher, shopify.normalize_target("gymshark.com"),
                                              10, FakeLog())
        assert result["status"] == "ok", result
        assert result["source"] == "page", result
        assert len(result["products"]) == 1, result["products"]
        assert result["pagesFailed"] == 4, result
        assert result["products"][0]["price"] == 9.0
        return result

    asyncio.run(run())
    return "read_store: catalog 403, sitemap found, 1 page read, 4 dead pages counted"


def test_read_store_not_shopify_and_blocked():
    async def run():
        fetcher = _fetcher({"/meta.json": FakeResponse(404),
                            "/products.json": FakeResponse(404),
                            "/sitemap.xml": FakeResponse(404)})
        result = await shopify.read_store(fetcher, shopify.normalize_target("example.com"),
                                          10, FakeLog())
        assert result["status"] == "not-shopify", result
        assert result["error"], "a refusal must carry a reason"

        fetcher = _fetcher({"/products.json": FakeResponse(403),
                            "/sitemap.xml": FakeResponse(200, "<?xml version='1.0'?><urlset>"
                                                             "<url><loc>https://x.com/a</loc></url>"
                                                             "</urlset>")})
        result = await shopify.read_store(fetcher, shopify.normalize_target("x.com"), 10, FakeLog())
        assert result["status"] == "blocked", result
        assert "catalog" in result["error"] and "sitemap" in result["error"], result["error"]
        return True

    asyncio.run(run())
    return "read_store: non-Shopify named, blocked store keeps both HTTP reasons"


def test_read_store_single_product_url():
    async def run():
        product = _products()[0]
        fetcher = _fetcher({
            "/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "/products/womens-wool-runners-natural-grey.json": FakeResponse(
                200, json.dumps({"product": product})),
        })
        target = shopify.normalize_target(
            "https://www.allbirds.com/products/womens-wool-runners-natural-grey")
        result = await shopify.read_store(fetcher, target, 250, FakeLog())
        assert result["status"] == "ok" and result["source"] == "product-json", result
        assert len(result["products"]) == 1 and result["products"][0]["price"] == 49.0
        return True

    asyncio.run(run())
    return "read_store: a product URL reads one product through /products/<handle>.json"


def test_unparsable_catalog_is_not_an_empty_shop():
    """A 200 answer that is not JSON must not read as "the shop has no products".

    Measured 06.11.2026: a 250-product catalog is 1.8 MB, and a byte cap turned it into
    truncated text that answered "empty catalog" while the shop was full.
    """
    async def run():
        fetcher = _fetcher({"/products.json": FakeResponse(200, '{"products":[{"id":1'),
                            "/sitemap.xml": FakeResponse(404)})
        result = await shopify.read_store(fetcher, shopify.normalize_target("shop.com"),
                                          10, FakeLog())
        assert result["status"] == "blocked", result
        assert "not JSON" in (result["error"] or ""), result["error"]

        client = FakeClient({"/big": FakeResponse(200, "x" * 50)})
        fetcher = shopify.Fetcher(client, FakeLog())
        status, text, error = await fetcher.get("https://shop.com/big", shopify.JSON_ACCEPT, 10)
        assert status == 200 and text == "", (status, text)
        assert "larger than" in error, error

    asyncio.run(run())
    return "a truncated or broken catalog is a refusal with a reason, never an empty shop"


def test_two_sitemap_urls_pointing_at_one_product_are_one_row():
    """A duplicate product is a double charge, so the page path dedupes by handle."""
    async def run():
        page = _ld_html()
        index = (FIXTURES / "gymshark_sitemap_index.xml").read_text(encoding="utf-8")
        two_urls = ("<?xml version='1.0'?><urlset>"
                    "<url><loc>https://www.gymshark.com/products/leggings-a</loc></url>"
                    "<url><loc>https://www.gymshark.com/products/leggings-b</loc></url>"
                    "</urlset>")
        fetcher = _fetcher({
            "/products.json": FakeResponse(403),
            "/sitemap.xml": FakeResponse(200, index),
            "/sitemap_products_1.xml": FakeResponse(200, two_urls),
            "/products/": FakeResponse(200, page),
        })
        with StubHosts():
            result = await shopify.read_store(fetcher, shopify.normalize_target("gymshark.com"),
                                              10, FakeLog())
        assert result["status"] == "ok", result
        assert len(result["products"]) == 1, result["products"]
        assert result["duplicates"] == 1, result

    asyncio.run(run())
    return "two sitemap urls for one product give one row and one duplicate counter"


def test_sitemap_cannot_reach_private_or_foreign_addresses():
    """B1 of the 06.11 gate: a sitemap entry must never become a request of its own.

    Measured by the reviewer on 0.1: a shop with a closed catalog and a sitemap naming
    http://169.254.169.254/latest/meta-data/... and http://127.0.0.1:8080/products/admin made
    the page path fetch both. Inside the Apify container the first is the metadata service with
    a service account token, so this is a real door, not a style question.
    """
    async def run():
        index = (FIXTURES / "gymshark_sitemap_index.xml").read_text(encoding="utf-8")
        hostile = ("<?xml version='1.0'?><urlset>"
                   "<url><loc>http://169.254.169.254/latest/meta-data/products/x</loc></url>"
                   "<url><loc>http://127.0.0.1:8080/products/admin</loc></url>"
                   "<url><loc>https://attacker.example/products/x</loc></url>"
                   "<url><loc>https://www.gymshark.com/products/leggings-a</loc></url>"
                   "</urlset>")
        client = FakeClient({
            "/meta.json": FakeResponse(200, json.dumps({"currency": "USD"})),
            "/products.json": FakeResponse(403),
            "/sitemap.xml": FakeResponse(200, index),
            "/sitemap_products_1.xml": FakeResponse(200, hostile),
            "/products/": FakeResponse(200, _ld_html()),
        })
        fetcher = shopify.Fetcher(client, FakeLog())
        with StubHosts():
            result = await shopify.read_store(fetcher, shopify.normalize_target("gymshark.com"),
                                              10, FakeLog())
        asked = " ".join(client.calls)
        for bad in ("169.254", "127.0.0.1", "attacker.example"):
            assert bad not in asked, ("a sitemap address was fetched: %s" % asked)
        assert result["status"] == "ok", result
        assert len(result["products"]) == 1, result["products"]
        assert result["skippedOffsite"] == 3, result["skippedOffsite"]

        # a store whose own host resolves into loopback is refused by the address check, not
        # by the name: the sitemap lives on the same host, so only host_is_public can stop it
        loop_back = ("<?xml version='1.0'?><urlset>"
                     "<url><loc>http://127.0.0.1:8080/products/admin</loc></url></urlset>")
        client = FakeClient({
            "/products.json": FakeResponse(403),
            "/sitemap.xml": FakeResponse(200, loop_back),
        })
        fetcher = shopify.Fetcher(client, FakeLog())
        result = await shopify.read_store(fetcher, shopify.normalize_target("127.0.0.1:8080"),
                                          10, FakeLog())
        assert not any("/products/" in url for url in client.calls), client.calls
        assert result["skippedOffsite"] >= 1, result["skippedOffsite"]

    asyncio.run(run())
    return "sitemap: metadata service, loopback and a foreign host are refused and counted"


def test_half_read_catalog_is_flagged():
    """B2 of the 06.11 gate: page 2 fails, page 1 has 250 products, and the run said "ok".

    A buyer reading stopReason completed saw 250 of 368 products as the whole catalog.
    """
    async def run():
        full = [{"handle": "h%d" % i, "title": "t%d" % i,
                 "variants": [{"price": "10.00", "available": True}]} for i in range(250)]

        def answer(url):
            if "page=2" in url:
                return FakeResponse(200, '{"products":[{"id":1')
            return FakeResponse(200, json.dumps({"products": full}))

        fetcher = _fetcher({"page=2": answer, "products.json": answer})
        result = await shopify.read_store(fetcher, shopify.normalize_target("shop.com"),
                                          0, FakeLog())
        assert result["status"] == "ok", result
        assert len(result["products"]) == 250, len(result["products"])
        assert result["truncated"] is True, result
        assert "not JSON" in (result["truncatedReason"] or ""), result["truncatedReason"]

    asyncio.run(run())
    return "a catalog whose second page failed is flagged truncated, not reported complete"


def test_limit_on_a_page_boundary_is_probed_not_guessed():
    """250 products with maxProductsPerStore 250 is exact, and 251 is cut: one probe tells."""
    async def run():
        full = [{"handle": "h%d" % i, "title": "t%d" % i,
                 "variants": [{"price": "10.00", "available": True}]} for i in range(250)]

        def answer_exact(url):
            if "limit=1" in url:
                return FakeResponse(200, '{"products": []}')
            return FakeResponse(200, json.dumps({"products": full}))

        fetcher = _fetcher({"limit=1": answer_exact, "products.json": answer_exact})
        result = await shopify.read_store(fetcher, shopify.normalize_target("shop.com"),
                                          250, FakeLog())
        assert len(result["products"]) == 250, len(result["products"])
        assert result["truncated"] is False, result["truncatedReason"]

        def answer_more(url):
            if "limit=1" in url:
                return FakeResponse(200, json.dumps({"products": full[:1]}))
            return FakeResponse(200, json.dumps({"products": full}))

        fetcher = _fetcher({"limit=1": answer_more, "products.json": answer_more})
        result = await shopify.read_store(fetcher, shopify.normalize_target("shop.com"),
                                          250, FakeLog())
        assert result["truncated"] is True, "a 251st product means the catalog was cut"

    asyncio.run(run())
    return "maxProductsPerStore on a page boundary: probed, so neither a false cut nor a miss"


def test_the_cut_reason_names_the_real_cause():
    """Orestes, 07.11: a catalog ended by the page ceiling was reported as "maxProductsPerStore
    stopped it", so the reason blamed a setting that was not the cause."""
    async def run():
        full = [{"handle": "h%d" % i, "title": "t%d" % i,
                 "variants": [{"price": "10.00", "available": True}]} for i in range(250)]
        fetcher = _fetcher({"products.json": FakeResponse(200, json.dumps({"products": full}))})
        original_pages = shopify.MAX_PAGES
        try:
            shopify.MAX_PAGES = 2
            result = await shopify.read_store(fetcher, shopify.normalize_target("shop.com"),
                                              1000, FakeLog())
        finally:
            shopify.MAX_PAGES = original_pages
        assert result["truncated"] is True, result
        reason = result["truncatedReason"] or ""
        assert "page ceiling" in reason, reason
        assert not reason.startswith("maxProductsPerStore"), \
            "the reason blames a setting that did not stop the read: %s" % reason

    asyncio.run(run())
    return "the truncation reason names the page ceiling, not a setting that was not the cause"


def run_all():
    checks = [
        test_normalize_target,
        test_host_is_public,
        test_normalize_product_real_catalog,
        test_normalize_product_without_price,
        test_parse_ld_productgroup_real_page,
        test_parse_ld_plain_product_and_empty,
        test_sitemap_locs_skip_locale_copies,
        test_same_site_is_not_two_labels,
        test_fetcher_never_parses_a_redirect,
        test_read_store_from_catalog_json,
        test_read_store_falls_back_to_sitemap_and_pages,
        test_read_store_not_shopify_and_blocked,
        test_read_store_single_product_url,
        test_unparsable_catalog_is_not_an_empty_shop,
        test_two_sitemap_urls_pointing_at_one_product_are_one_row,
        test_sitemap_cannot_reach_private_or_foreign_addresses,
        test_half_read_catalog_is_flagged,
        test_limit_on_a_page_boundary_is_probed_not_guessed,
        test_the_cut_reason_names_the_real_cause,
    ]
    failed = 0
    for check in checks:
        try:
            note = check()
            print("ok   %-48s %s" % (check.__name__, note or ""))
        except AssertionError as exc:
            failed += 1
            print("FAIL %-48s %s" % (check.__name__, exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERR  %-48s %s: %s" % (check.__name__, type(exc).__name__, exc))
    print("\n%d checks, %d failed" % (len(checks), failed))
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if run_all() else 0)
