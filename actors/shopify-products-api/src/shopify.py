"""Read a Shopify storefront catalog from public endpoints, with a fallback chain.

Attempt order, cheapest first:
1. /meta.json                 shop name, country and currency, one call per store;
2. /products.json             the catalog with variants, prices and stock, 250 per page;
3. /products/<handle>.json    one product, when the input names a product URL;
4. /sitemap.xml then sitemap_products_N.xml   product addresses, when the JSON catalog is closed;
5. the product page           JSON-LD Product or ProductGroup, one page per product.

Measured 06.11.2026: allbirds.com and kyliecosmetics.com answer /products.json with 200
(250 products on page 1, 118 on page 2 for allbirds), /meta.json gives the currency;
gymshark.com answers 403 on /products.json, but /sitemap_products_1.xml lists 605 product
URLs and each product page answers 200 with JSON-LD ProductGroup whose hasVariant[].offers
carries price, currency and availability. That is why the chain exists: the first endpoint
says no, and the shop is not empty.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
JSON_ACCEPT = "application/json, text/plain, */*"
HTML_ACCEPT = "text/html,application/xhtml+xml"
XML_ACCEPT = "application/xml, text/xml, */*"
PAGE_SIZE = 250
MAX_PAGES = 40
MAX_SITEMAPS = 20
# a 250-product catalog measured 1.8 MB on allbirds.com, and body_html makes it bigger:
# cutting a JSON answer at a byte limit turns it into unparsable text that used to read as
# an empty shop, so JSON and HTML get very different limits.
READ_LIMIT_JSON = 40_000_000
READ_LIMIT_HTML = 2_000_000
CONCURRENCY = 4
PAGE_CHUNK = 8
RETRY_STATUSES = {429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
LOCAL_SUFFIXES = (".local", ".internal", ".lan", ".home", ".corp", ".localhost")
BLOCK_STATUSES = {401, 403, 404, 410, 430, 451}

LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
PRODUCTS_MAP_RE = re.compile(r"/sitemap_products_\d+\.xml$", re.I)


def _money(value):
    """A price as a float, or None: a missing price is unknown, never zero."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, 2)


def _tags(value):
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def host_is_public(host: str) -> bool:
    """False for a name that resolves into a private, loopback or link-local address.

    The store is a hostname the buyer types, so a run must not become a probe of the
    account's own network. Every address getaddrinfo returns is checked, not the first one,
    and an IPv4-mapped IPv6 address is unwrapped first: ::ffff:169.254.169.254 is link-local
    even though the literal looks like IPv6.
    """
    name = (host or "").strip().lower().rstrip(".")
    if not name or " " in name or "/" in name:
        return False
    if name == "localhost" or name.endswith(LOCAL_SUFFIXES):
        return False
    try:
        infos = socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        raw = str(info[4][0]).split("%")[0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return False
        addr = getattr(addr, "ipv4_mapped", None) or addr
        # is_global also refuses CGNAT 100.64.0.0/10, which the explicit
        # list of flags above did not cover.
        if not addr.is_global:
            return False
    return True


def normalize_target(raw: str):
    """Turn an input line into a store host plus an optional product handle, or None."""
    text = str(raw or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or "." not in host:
        return None
    handle = None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() == "products":
        handle = parts[1]
        if handle.lower().endswith(".json"):
            handle = handle[:-5]
    return {"raw": str(raw), "store": host, "handle": handle, "base": "https://" + host}


# Suffixes met in the wild that need three labels: shop.co.uk and attacker.co.uk share the
# same last two labels, and a two-label rule called them one site.
TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "ne.jp", "or.jp",
    "com.au", "net.au", "org.au", "co.nz", "net.nz", "co.za", "org.za",
    "com.br", "com.mx", "com.ar", "com.tr", "com.sg", "com.hk", "com.tw",
    "co.in", "co.kr", "co.il", "com.ua", "co.id", "com.my", "com.ph",
}


def _registrable(host: str) -> str:
    """The domain a host belongs to, with the public suffixes we actually meet."""
    parts = [part for part in (host or "").lower().split(".") if part]
    if len(parts) < 2:
        return ".".join(parts)
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _same_site(first: str, second: str) -> bool:
    """Whether two URLs live on the same registrable domain, so one redirect may be followed."""
    host_a = (urlparse(first).hostname or "").lower()
    host_b = (urlparse(second).hostname or "").lower()
    if not host_a or not host_b:
        return False
    return _registrable(host_a) == _registrable(host_b)


class SiteGuard:
    """Which addresses found inside a store's own pages may be fetched.

    The input host is checked once in main, and a redirect is checked in the fetcher, but a
    sitemap is content the store writes: it can name any address at all. Measured 06.11.2026:
    a sitemap carrying http://169.254.169.254/latest/meta-data/... and
    http://127.0.0.1:8080/... made the page path issue real requests to both, and in an Apify
    container the first one is the metadata service with a service account token. Same site and
    a public address are both required; the DNS answer is cached per host, so 605 product
    addresses do not become 605 lookups.
    """

    def __init__(self):
        self._cache = {}
        self.skipped = 0

    async def allowed(self, base: str, url: str) -> bool:
        parsed = urlparse(str(url or ""))
        if parsed.scheme not in ("http", "https") or not _same_site(base, url):
            self.skipped += 1
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            self.skipped += 1
            return False
        if host not in self._cache:
            self._cache[host] = await asyncio.to_thread(host_is_public, host)
        if not self._cache[host]:
            self.skipped += 1
            return False
        return True

    async def keep(self, base: str, urls):
        """The addresses that may be fetched, in the order they came."""
        ready = []
        for url in urls:
            if await self.allowed(base, url):
                ready.append(url)
        return ready


class Fetcher:
    """One HTTP surface for a run: a polite semaphore, honest statuses, one guarded redirect.

    A 3xx answer is never parsed as a page. The same mistake in an earlier Actor charged
    $0.002 for a page whose title was "301 Moved Permanently", so a redirect that cannot be
    followed here comes back as a status with the Location it wanted, not as content.
    """

    def __init__(self, client: httpx.AsyncClient, log, concurrency: int = CONCURRENCY):
        self.client = client
        self.log = log
        self.semaphore = asyncio.Semaphore(concurrency)
        self.requests = 0

    async def get(self, url: str, accept: str, limit: int = READ_LIMIT_HTML):
        """Return (status, text, error)."""
        async with self.semaphore:
            return await self._get(url, accept, hops=1, limit=limit)

    async def _get(self, url: str, accept: str, hops: int, limit: int = READ_LIMIT_HTML):
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        for attempt in (1, 2):
            self.requests += 1
            try:
                response = await self.client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                if attempt == 1:
                    await asyncio.sleep(1.0)
                    continue
                return None, "", "%s: %s" % (type(exc).__name__, exc)
            status = response.status_code
            if status in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location or hops <= 0:
                    return status, "", "redirect not followed: %s" % (location or "no Location")
                target = urljoin(url, location)
                if not _same_site(url, target):
                    return status, "", "redirected off the store: %s" % target
                host = (urlparse(target).hostname or "")
                if not await asyncio.to_thread(host_is_public, host):
                    return status, "", "redirected to a non-public host: %s" % host
                return await self._get(target, accept, hops - 1, limit)
            if status in RETRY_STATUSES and attempt == 1:
                await asyncio.sleep(1.0)
                continue
            if status != 200:
                return status, "", "HTTP %s" % status
            text = response.text
            if len(text) > limit:
                return status, "", "the answer is larger than %d characters and was not read" % limit
            return status, text, None
        return None, "", "gave up after two attempts"


def normalize_product(product: dict, store: str, source: str, currency):
    """One product in the shape this Actor sells, from a /products.json product."""
    variants = []
    for variant in product.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        variants.append({
            "sku": variant.get("sku") or None,
            "title": variant.get("title") or None,
            "price": _money(variant.get("price")),
            "compareAtPrice": _money(variant.get("compare_at_price")),
            "available": bool(variant.get("available")),
        })
    prices = [item["price"] for item in variants if item["price"] is not None]
    compares = [item["compareAtPrice"] for item in variants if item["compareAtPrice"] is not None]
    handle = product.get("handle") or None
    images = product.get("images") or []
    image = None
    if images and isinstance(images[0], dict):
        image = images[0].get("src")
    if not image and isinstance(product.get("featured_image"), str):
        image = product.get("featured_image")
    return {
        "store": store,
        "handle": handle,
        "title": product.get("title") or "",
        "price": min(prices) if prices else None,
        "priceMax": max(prices) if prices else None,
        "compareAtPrice": min(compares) if compares else None,
        "currency": currency,
        "availability": "in_stock" if any(item["available"] for item in variants) else "out_of_stock",
        "variantCount": len(variants),
        "variants": variants,
        "productType": product.get("product_type") or None,
        "vendor": product.get("vendor") or None,
        "tags": _tags(product.get("tags")),
        "productUrl": product.get("url") or ("https://%s/products/%s" % (store, handle) if handle else None),
        "imageUrl": image or None,
        "updatedAt": product.get("updated_at") or None,
        "source": source,
    }


def _walk_ld(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk_ld(item)
        return
    if isinstance(node, dict):
        graph = node.get("@graph")
        if isinstance(graph, list):
            yield from _walk_ld(graph)
        yield node


def _ld_types(node: dict):
    raw = node.get("@type")
    if isinstance(raw, list):
        return {str(item) for item in raw}
    return {str(raw)} if raw else set()


def _ld_image(raw):
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url")
    if isinstance(raw, dict):
        return raw.get("url")
    return None


def _ld_offers(node: dict):
    offers = node.get("offers")
    if isinstance(offers, dict):
        return [offers]
    if isinstance(offers, list):
        return [item for item in offers if isinstance(item, dict)]
    return []


def _ld_variants(node: dict):
    sku = node.get("sku") or node.get("mpn") or node.get("productID")
    label = node.get("size") or node.get("color") or node.get("name")
    out = []
    for offer in _ld_offers(node):
        price = _money(offer.get("price"))
        if price is None:
            spec = offer.get("priceSpecification")
            if isinstance(spec, dict):
                price = _money(spec.get("price"))
        availability = str(offer.get("availability") or "")
        out.append({
            "sku": sku or None,
            "title": label or None,
            "price": price,
            "compareAtPrice": None,
            "available": "InStock" in availability,
            "currency": offer.get("priceCurrency") or None,
        })
    if not out:
        out.append({"sku": sku or None, "title": label or None, "price": None,
                    "compareAtPrice": None, "available": False, "currency": None})
    return out


def _handle_from_url(url: str):
    parts = [part for part in urlparse(str(url or "")).path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() == "products":
        handle = parts[1]
        return handle[:-5] if handle.lower().endswith(".json") else handle
    return None


def parse_ld_product(html: str, url: str):
    """Read a product page's JSON-LD into the same shape as /products.json, or return None.

    Two shapes are seen in the wild: a Product with offers, and a ProductGroup whose
    hasVariant[] entries each carry their own offers (gymshark.com, measured 06.11.2026).
    Returns (product, currency); currency is taken from the offer that had one.
    """
    if not html:
        return None, None
    title = None
    image = None
    handle = None
    variants = []
    currency = None
    for block in LD_RE.findall(html):
        text = block.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        for node in _walk_ld(data):
            types = _ld_types(node)
            if "ProductGroup" in types:
                title = title or node.get("name")
                image = image or _ld_image(node.get("image"))
                handle = handle or _handle_from_url(node.get("url") or url)
                for child in node.get("hasVariant") or []:
                    if isinstance(child, dict):
                        variants.extend(_ld_variants(child))
            elif "Product" in types:
                title = title or node.get("name")
                image = image or _ld_image(node.get("image"))
                handle = handle or _handle_from_url(node.get("url") or url)
                variants.extend(_ld_variants(node))
    if not variants and not title:
        return None, None
    for variant in variants:
        if variant.get("currency"):
            currency = variant["currency"]
            break
    product = {
        "handle": handle or _handle_from_url(url),
        "title": title or "",
        "variants": [{"sku": item["sku"], "title": item["title"],
                      "price": item["price"], "compare_at_price": item["compareAtPrice"],
                      "available": item["available"]} for item in variants],
        "images": [{"src": image}] if image else [],
        "url": url,
    }
    return product, currency


async def read_products_json(fetcher: Fetcher, base: str, max_products: int):
    """The catalog from /products.json, page by page.

    Returns (products, status, error, pages, truncated, cut) where `cut` names what stopped the
    read, so the summary cannot blame maxProductsPerStore for a page ceiling. `truncated` is True when the catalog
    was cut short: by maxProductsPerStore, by a page that failed after earlier pages answered,
    or by the page ceiling. Measured 06.11.2026: page 1 gave 250 products and page 2 answered
    with text that is not JSON; without the flag the run reported "completed" on a shop it had
    read halfway, and the buyer saw 250 of 368 products as the whole catalog.
    """
    products = []
    page = 1
    first_status = None
    first_error = None
    pages = 0
    truncated = False
    cut = None
    full_page = False
    while page <= MAX_PAGES:
        if max_products and len(products) >= max_products:
            break
        url = "%s/products.json?limit=%d&page=%d" % (base, PAGE_SIZE, page)
        status, text, error = await fetcher.get(url, JSON_ACCEPT, READ_LIMIT_JSON)
        if first_status is None:
            first_status = status
            first_error = error
        if status != 200:
            # earlier pages answered, so this one is a hole in the catalog, not a refusal
            truncated = bool(products)
            if truncated:
                cut = "a later catalog page failed: %s" % (error or ("HTTP %s" % status))
            break
        pages += 1
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            first_error = first_error or "products.json is not JSON"
            truncated = bool(products)
            if truncated:
                cut = "a later catalog page failed: products.json is not JSON"
            break
        batch = data.get("products") or []
        if not batch:
            break
        products.extend(batch)
        full_page = len(batch) >= PAGE_SIZE
        if not full_page:
            break
        page += 1
    else:
        truncated = True  # the page ceiling ended the catalog, not the shop
        cut = ("the page ceiling of %d catalog pages stopped the read, not maxProductsPerStore"
               % MAX_PAGES)
    if max_products and len(products) > max_products:
        truncated = True
        cut = cut or "maxProductsPerStore stopped the catalog before its end"
    elif max_products and full_page and not truncated:
        # the limit landed exactly on a page boundary: ask for a single product instead of
        # guessing in either direction whether the shop has more
        probe_status, probe_text, _ = await fetcher.get(
            "%s/products.json?limit=1&page=%d" % (base, page), JSON_ACCEPT, READ_LIMIT_JSON)
        if probe_status == 200:
            try:
                more = json.loads(probe_text).get("products") or []
            except (ValueError, TypeError):
                more = []
            truncated = bool(more)
            if truncated:
                cut = "maxProductsPerStore stopped the catalog before its end"
    if max_products:
        products = products[:max_products]
    return products, first_status, first_error, pages, truncated, cut


async def sitemap_product_urls(fetcher: Fetcher, base: str, limit: int, guard: SiteGuard):
    """Product addresses from the store sitemap.

    Returns (urls, status, error, shopify, cut). Every address goes through the guard before it
    is used or counted, because a sitemap is content the store writes, not an address the buyer
    typed: an entry pointing at the container metadata service is a request this run must never
    make. `cut` says the list stopped at `limit` or at MAX_SITEMAPS, not at the end of the shop.
    """
    status, text, error = await fetcher.get(base + "/sitemap.xml", XML_ACCEPT)
    if status != 200:
        return [], status, error, False, False
    locs = LOC_RE.findall(text)
    maps = await guard.keep(
        base, [url for url in locs if PRODUCTS_MAP_RE.search(urlparse(url).path)])
    if not maps:
        direct = await guard.keep(base, [url for url in locs if "/products/" in url])
        if direct:
            cut = bool(limit and len(direct) > limit)
            return (direct[:limit] if limit else direct), status, None, True, cut
        return [], status, "the sitemap lists no product sitemap", False, False
    # locale copies look like /es-US/sitemap_products_1.xml: the root one is enough
    root = [url for url in maps if urlparse(url).path.count("/") == 1]
    urls = []
    for map_url in (root or maps)[:MAX_SITEMAPS]:
        map_status, body, map_error = await fetcher.get(map_url, XML_ACCEPT)
        if map_status != 200:
            error = error or map_error
            continue
        for loc in LOC_RE.findall(body):
            if "/products/" not in loc or loc in urls:
                continue
            if not await guard.allowed(base, loc):
                continue
            urls.append(loc)
            if limit and len(urls) >= limit:
                return urls, map_status, None, True, True
    return urls, 200, None, True, False


async def read_product_pages(fetcher: Fetcher, base: str, urls, log, guard: SiteGuard):
    """Read product pages in chunks so memory stays flat.

    Returns (products, failed, errors). The guard runs again here, at the request point: a list
    that reached this function from anywhere else is still not allowed to name a foreign or
    private address.
    """
    products = []
    failed = 0
    errors = []
    for start in range(0, len(urls), PAGE_CHUNK):
        chunk = await guard.keep(base, urls[start:start + PAGE_CHUNK])
        if not chunk:
            continue
        answers = await asyncio.gather(*[fetcher.get(url, HTML_ACCEPT) for url in chunk])
        for url, (status, text, error) in zip(chunk, answers):
            if status != 200:
                failed += 1
                if len(errors) < 5:
                    errors.append("%s: %s" % (url, error or status))
                continue
            product, page_currency = parse_ld_product(text, url)
            if product is None:
                failed += 1
                if len(errors) < 5:
                    errors.append("%s: no Product in JSON-LD" % url)
                continue
            products.append((product, page_currency))
    return products, failed, errors


def _done(result: dict, guard: SiteGuard) -> dict:
    """The store result with the count of addresses the guard refused."""
    result["skippedOffsite"] = guard.skipped
    return result


async def read_store(fetcher: Fetcher, target: dict, max_products: int, log) -> dict:
    """Read one store through the whole chain and say honestly what happened."""
    store = target["store"]
    base = target["base"]
    result = {
        "store": store,
        "handle": target.get("handle"),
        "status": "blocked",
        "source": None,
        "products": [],
        "currency": None,
        "shop": None,
        "pages": 0,
        "pagesFailed": 0,
        "pageErrors": [],
        "httpStatus": None,
        "error": None,
        "note": None,
        "duplicates": 0,
        "skippedOffsite": 0,
        "truncated": False,
        "truncatedReason": None,
    }
    guard = SiteGuard()
    meta_status, meta_text, meta_error = await fetcher.get(
        base + "/meta.json", JSON_ACCEPT, 1_000_000)
    if meta_status == 200:
        try:
            meta = json.loads(meta_text)
        except (ValueError, TypeError):
            meta = {}
        if isinstance(meta, dict):
            result["currency"] = meta.get("currency") or None
            result["shop"] = meta.get("name") or None

    if target.get("handle"):
        handle = target["handle"]
        url = "%s/products/%s.json" % (base, handle)
        status, text, error = await fetcher.get(url, JSON_ACCEPT, READ_LIMIT_JSON)
        if status == 200:
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                data = None
            product = (data or {}).get("product") if isinstance(data, dict) else None
            if product:
                result["products"] = [normalize_product(product, store, "product-json",
                                                       result["currency"])]
                result["status"] = "ok"
                result["source"] = "product-json"
                return _done(result, guard)
        page_url = "%s/products/%s" % (base, handle)
        page_status, page_text, page_error = await fetcher.get(page_url, HTML_ACCEPT)
        if page_status == 200:
            product, page_currency = parse_ld_product(page_text, page_url)
            if product:
                result["products"] = [normalize_product(product, store, "page",
                                                       result["currency"] or page_currency)]
                result["status"] = "ok"
                result["source"] = "page"
                return _done(result, guard)
        result["status"] = "blocked"
        result["httpStatus"] = status
        result["error"] = "product JSON: %s; product page: %s" % (error or status,
                                                                 page_error or page_status)
        return _done(result, guard)

    products, status, error, pages, truncated, cut = await read_products_json(
        fetcher, base, max_products)
    result["pages"] = pages
    if products:
        result["products"] = [normalize_product(item, store, "json", result["currency"])
                              for item in products]
        result["status"] = "ok"
        result["source"] = "json"
        if truncated:
            result["truncated"] = True
            # the chain says what stopped it, so the reason cannot blame a setting that was
            # not the cause: measured 07.11.2026, a catalog ended by the page ceiling was
            # called "maxProductsPerStore stopped it" whenever maxProducts was set
            result["truncatedReason"] = cut or ("the catalog was cut short: %s"
                                                % (error or "unknown cause"))
        return _done(result, guard)
    if status == 200 and not products and not error:
        result["status"] = "empty"
        result["httpStatus"] = status
        result["note"] = "the store answered with an empty catalog"
        return _done(result, guard)

    sitemap_status, sitemap_error = status, error
    urls, sitemap_status, sitemap_error, is_shopify, cut = await sitemap_product_urls(
        fetcher, base, max_products, guard)
    if urls:
        parsed, failed, errors = await read_product_pages(fetcher, base, urls, log, guard)
        result["pages"] = len(urls)
        result["pagesFailed"] = failed
        result["pageErrors"] = errors
        if parsed:
            rows = []
            seen_handles = set()
            duplicates = 0
            for item, page_currency in parsed:
                row = normalize_product(item, store, "page",
                                        result["currency"] or page_currency)
                handle = row.get("handle")
                if handle and handle in seen_handles:
                    duplicates += 1
                    continue
                if handle:
                    seen_handles.add(handle)
                rows.append(row)
            result["products"] = rows
            result["duplicates"] = duplicates
            result["status"] = "ok"
            result["source"] = "page"
            if failed:
                result["truncated"] = True
                result["truncatedReason"] = ("%d product page(s) answered but could not be read"
                                             % failed)
            elif cut:
                result["truncated"] = True
                result["truncatedReason"] = (
                    "maxProductsPerStore cut the list of product pages" if max_products
                    else "the page limit cut the list of product pages, not maxProductsPerStore")
            return _done(result, guard)
        result["status"] = "blocked"
        result["httpStatus"] = status
        result["error"] = ("catalog: %s; %d product pages, none readable: %s"
                           % (error or status, failed, "; ".join(errors[:2]) or "no reason"))
        return _done(result, guard)

    result["httpStatus"] = status
    # "not Shopify" is a 404 on the catalog endpoint and nothing in the sitemap: a 403 or a
    # rate limit is a refusal, and calling it "not Shopify" would send the buyer to the wrong
    # answer. Measured: a 403 catalog with a sitemap that has no product map is blocked.
    if (is_shopify is False and status in (404, 410)
            and sitemap_status in (404, 410, None)):
        result["status"] = "not-shopify"
        result["error"] = ("no Shopify catalog and no product sitemap: %s; sitemap: %s"
                           % (error or status, sitemap_error or sitemap_status))
        return _done(result, guard)
    result["status"] = "blocked"
    result["error"] = "catalog: %s; sitemap: %s" % (error or status,
                                                   sitemap_error or sitemap_status)
    return _done(result, guard)
