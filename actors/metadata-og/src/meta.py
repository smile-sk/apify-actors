"""Page reader for the Open Graph and metadata actor.

Two promises live in this file.

First: every URL a row carries is absolute. An og:image of "/img/cover.png"
comes back as "https://site.com/img/cover.png", resolved against <base href>
or the final URL after redirects, so a preview built from the row renders
instead of showing a blank box.

Second: a page that cannot be read costs nothing. DNS errors, TLS failures,
404, 403, a non-HTML body, a redirect that was not followed and a page with no
metadata at all are each answered with a status and a plain reason, and none of
them is billable.
"""
from __future__ import annotations

import codecs
import ipaddress
import json
import re
import socket
import time
from html import unescape
from urllib.parse import urljoin, urlsplit

import httpx

try:  # selectolax ships in the actor image; the guard keeps imports honest
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:  # pragma: no cover - only when the dependency is missing
    HTMLParser = None  # type: ignore[assignment]

STATUS_EXTRACTED = "EXTRACTED"
STATUS_NO_METADATA = "NO_METADATA"
STATUS_UNREACHABLE = "UNREACHABLE"
STATUS_HTTP_ERROR = "HTTP_ERROR"
STATUS_NOT_HTML = "NOT_HTML"
STATUS_INVALID_URL = "INVALID_URL"
STATUS_PRIVATE = "PRIVATE_TARGET"
STATUS_REDIRECT = "REDIRECT"
STATUS_ERROR = "ERROR"

# Statuses that mean "we never got a page to read": a list made of these is
# typos and dead links, not a finished job.
LINK_ERROR_STATUSES = frozenset((STATUS_INVALID_URL, STATUS_PRIVATE, STATUS_UNREACHABLE,
                                 STATUS_HTTP_ERROR, STATUS_NOT_HTML, STATUS_REDIRECT))
# Statuses that are a finished answer about a page we did open.
HONEST_STATUSES = (STATUS_EXTRACTED, STATUS_NO_METADATA)

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MAX_BYTES = 4 * 1024 * 1024     # metadata lives in <head>; 4 MB is plenty
MAX_JSONLD = 50
MAX_TYPES = 60
MAX_HREFLANG = 50

_PRIVATE = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.]+)""", re.I)
_HEADER_CHARSET = re.compile(r"charset\s*=\s*([^;\s]+)", re.I)


def _fail(status: str, reason: str, *, final: str | None = None, http: int | None = None,
          redirects: int = 0, ms: int | None = None, error: str | None = None) -> dict:
    """One shape for every row that carries no metadata."""
    return {
        "status": status,
        "reason": reason,
        "statusCode": http,
        "finalUrl": final,
        "redirectCount": redirects,
        "responseTimeMs": ms,
        "errorType": error,
        "title": None,
        "description": None,
        "canonical": None,
        "lang": None,
        "charset": None,
        "openGraph": {},
        "twitterCard": {},
        "icons": {},
        "jsonLd": {"count": 0, "types": []},
        "hreflang": [],
        "preview": {"ready": False, "issues": []},
        "metaCount": 0,
    }


def ip_is_private(text: str) -> bool:
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    # An IPv4 address can arrive written as IPv6 (::ffff:127.0.0.1). It is the same
    # address, so it is unwrapped before the range check: otherwise a loopback or a
    # cloud metadata endpoint hides behind the mapped form and the request goes out.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    # is_global is False for every non-routable range, CGNAT 100.64.0.0/10
    # included: the explicit list below missed it and let the request out.
    return not addr.is_global


async def host_classify(url: str) -> str:
    """Classify the host: ok, private, unresolvable or invalid.

    A name that does not resolve is not private: it is its own answer, because
    it is the buyer's typo and not an attempt to reach our network.
    """
    try:
        host = urlsplit(url).hostname
    except Exception:  # noqa: BLE001 - a malformed URL has no host
        return "invalid"
    if not host:
        return "invalid"
    import asyncio
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except socket.gaierror:
        return "unresolvable"
    if not infos:
        return "unresolvable"
    for info in infos:
        if ip_is_private(info[4][0]):
            return "private"
    return "ok"


def normalize_url(raw) -> str | None:
    """Turn a line of input into an http(s) URL, or None when it cannot be one."""
    text = unescape(str(raw or "")).strip().strip('"').strip("'")
    if not text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    if not _SCHEME_RE.match(text):
        if _SCHEME_RE.match("//" + text) or text.startswith("/"):
            return None
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if not parts.hostname:
        return None
    host = parts.hostname
    if "." not in host and host.lower() != "localhost" and not host.startswith("["):
        return None
    return text


def _safe_codec(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = name.strip().strip('"').strip("'").lower()
    try:
        codecs.lookup(cleaned)
    except LookupError:
        return None
    return cleaned


def decode_body(content: bytes, content_type: str | None = None) -> tuple[str, str]:
    """Decode a page without turning an unknown charset into mojibake.

    Order: BOM, then the charset the response header declares, then the one the
    page declares inside <meta>, then UTF-8. A wrong declaration is replaced,
    never raised, so a broken page still yields its metadata.
    """
    if content.startswith(codecs.BOM_UTF8):
        return content[len(codecs.BOM_UTF8):].decode("utf-8", "replace"), "utf-8"
    if content.startswith(codecs.BOM_UTF16_LE):
        return content.decode("utf-16-le", "replace"), "utf-16-le"
    if content.startswith(codecs.BOM_UTF16_BE):
        return content.decode("utf-16-be", "replace"), "utf-16-be"

    declared = None
    if content_type:
        match = _HEADER_CHARSET.search(content_type)
        if match:
            declared = _safe_codec(match.group(1))
    if declared is None:
        match = _META_CHARSET.search(content[:4096])
        if match:
            declared = _safe_codec(match.group(1).decode("ascii", "ignore"))

    codec = declared or "utf-8"
    try:
        return content.decode(codec, "replace"), codec
    except LookupError:
        return content.decode("utf-8", "replace"), "utf-8"


def _clean(value) -> str:
    if value is None:
        return ""
    text = unescape(str(value))
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return re.sub(r"\s+", " ", text).strip()


def absolute(base: str, value) -> str | None:
    """Resolve a meta value into an absolute http(s) URL, or None."""
    text = _clean(value)
    if not text:
        return None
    low = text.lower()
    if low.startswith(("javascript:", "data:", "mailto:", "tel:", "about:", "blob:")):
        return None
    if low.startswith("//"):
        text = "https:" + text
    try:
        merged = urljoin(base, text)
    except Exception:  # noqa: BLE001 - a value that cannot be joined is not a URL
        return None
    if not merged.lower().startswith(("http://", "https://")):
        return None
    return merged


def _jsonld_types(node, out: list[str]) -> None:
    if isinstance(node, dict):
        kind = node.get("@type")
        if isinstance(kind, str) and kind not in out:
            out.append(kind)
        elif isinstance(kind, list):
            for item in kind:
                if isinstance(item, str) and item not in out:
                    out.append(item)
        for key, value in node.items():
            if key in ("@type",):
                continue
            if isinstance(value, (dict, list)):
                _jsonld_types(value, out)
    elif isinstance(node, list):
        for item in node:
            _jsonld_types(item, out)


def _preview_issues(title: str | None, description: str | None, image: str | None,
                    image_raw: str, has_og: bool) -> list[str]:
    issues: list[str] = []
    if not title:
        issues.append("no_title")
    if not description:
        issues.append("no_description")
    if not image:
        issues.append("no_image")
    if not has_og:
        issues.append("no_open_graph")
    if image_raw and image and image != image_raw:
        issues.append("image_was_relative")
    if title and len(title) > 60:
        issues.append("title_longer_than_60")
    if description and len(description) > 160:
        issues.append("description_longer_than_160")
    return issues


def parse_page(html: str, final_url: str) -> dict:
    """Pull the metadata out of one page. Pure: no network, no billing."""
    if HTMLParser is None:
        row = _fail(STATUS_ERROR, "The HTML parser is missing from this build, so nothing was read.")
        row["finalUrl"] = final_url
        return row

    row = _fail(STATUS_NO_METADATA,
                "No title, description or Open Graph tags on the page: nothing to show in a preview. "
                "Nothing was charged for it.")
    row["finalUrl"] = final_url
    raw = (html or "").strip()
    if not raw:
        row["reason"] = "The page came back empty: nothing to read, nothing was charged."
        return row

    try:
        tree = HTMLParser(raw)
    except Exception as exc:  # noqa: BLE001 - a broken page is an answer, not a crash
        row["status"] = STATUS_ERROR
        row["reason"] = "The page could not be parsed (%s): nothing was charged." % type(exc).__name__
        return row

    base = final_url
    base_node = tree.css_first("base")
    if base_node is not None:
        href = _clean((base_node.attributes or {}).get("href"))
        if href:
            base = urljoin(final_url, href)

    meta_map: dict[str, str] = {}
    og: dict[str, str] = {}
    twitter: dict[str, str] = {}
    metas = tree.css("meta")
    row["metaCount"] = len(metas)
    for node in metas:
        attrs = node.attributes or {}
        key = _clean(attrs.get("property") or attrs.get("name") or attrs.get("itemprop")).lower()
        content = attrs.get("content")
        if not key or content is None:
            continue
        value = _clean(content)
        if not value:
            continue
        meta_map.setdefault(key, value)
        if key.startswith("og:"):
            og.setdefault(key[3:], value)
        elif key.startswith("twitter:"):
            twitter.setdefault(key[8:], value)

    page_title = None
    title_node = tree.css_first("title")
    if title_node is not None:
        page_title = _clean(title_node.text(deep=True, strip=True)) or None
    page_title = page_title or meta_map.get("title") or None
    title = og.get("title") or page_title or twitter.get("title") or None

    page_description = meta_map.get("description") or None
    description = og.get("description") or page_description or twitter.get("description") or None

    canonical = None
    for node in tree.css("link"):
        attrs = node.attributes or {}
        rel = _clean(attrs.get("rel")).lower()
        if "canonical" in rel:
            canonical = absolute(base, attrs.get("href"))
            if canonical:
                break

    lang = None
    html_node = tree.css_first("html")
    if html_node is not None:
        lang = _clean((html_node.attributes or {}).get("lang")) or None
    lang = lang or meta_map.get("content-language") or meta_map.get("language") or None

    favicon = None
    apple_icon = None
    manifest = None
    hreflang: list[dict] = []
    for node in tree.css("link"):
        attrs = node.attributes or {}
        rel = _clean(attrs.get("rel")).lower()
        href = attrs.get("href")
        if not href:
            continue
        resolved = absolute(base, href)
        if not resolved:
            continue
        if "apple-touch-icon" in rel:
            apple_icon = apple_icon or resolved
        elif "manifest" in rel:
            manifest = manifest or resolved
        elif "icon" in rel:
            favicon = favicon or resolved
        if "alternate" in rel and attrs.get("hreflang"):
            if len(hreflang) < MAX_HREFLANG:
                hreflang.append({"lang": _clean(attrs.get("hreflang")).lower(), "href": resolved})

    types: list[str] = []
    jsonld_count = 0
    for node in tree.css('script[type="application/ld+json"]'):
        text = (node.text() or "").strip()
        if not text:
            continue
        jsonld_count += 1
        if jsonld_count > MAX_JSONLD:
            break
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001 - invalid JSON-LD is skipped, not fatal
            continue
        _jsonld_types(data, types)

    og_image_raw = og.get("image") or og.get("image:url") or ""
    og_image = absolute(base, og_image_raw)
    tw_image_raw = twitter.get("image") or ""
    tw_image = absolute(base, tw_image_raw)
    # A preview falls back to the Twitter card image, but openGraph.image must stay the
    # Open Graph value: selling a twitter:image as an og:image would be an invention.
    preview_image = og_image or tw_image
    preview_image_raw = og_image_raw if og_image else tw_image_raw
    preview_title = og.get("title") or title
    preview_description = og.get("description") or description
    issues = _preview_issues(preview_title, preview_description, preview_image,
                            preview_image_raw, bool(og))

    row.update({
        "title": title,
        "description": description,
        "canonical": canonical,
        "lang": lang,
        "openGraph": {
            "title": og.get("title"),
            "description": og.get("description"),
            "image": og_image,
            "imageAlt": og.get("image:alt"),
            "type": og.get("type"),
            "siteName": og.get("site_name"),
            "url": absolute(base, og.get("url")),
            "locale": og.get("locale"),
            "video": absolute(base, og.get("video")),
        },
        "twitterCard": {
            "card": twitter.get("card"),
            "title": twitter.get("title"),
            "description": twitter.get("description"),
            "image": tw_image,
            "site": twitter.get("site"),
        },
        "icons": {"favicon": favicon, "appleTouchIcon": apple_icon, "manifest": manifest},
        "jsonLd": {"count": jsonld_count, "types": types[:MAX_TYPES]},
        "hreflang": hreflang,
        "preview": {"ready": not any(i in issues for i in ("no_title", "no_description", "no_image")),
                    "issues": issues},
    })

    if title or description or og or jsonld_count:
        row["status"] = STATUS_EXTRACTED
        row["reason"] = ""
    else:
        row["status"] = STATUS_NO_METADATA
        row["reason"] = ("The page opened but carries no title, description, Open Graph or JSON-LD: "
                         "most likely it renders in the browser. Nothing was charged for it.")
    return row


def _is_tls_error(exc: BaseException) -> bool:
    """Detect a TLS failure anywhere in the exception chain.

    httpx 0.28.1 wraps a certificate error as ConnectError, keeping the
    SSLCertVerificationError inside args, so a plain isinstance miss reports an
    expired certificate as a network failure.
    """
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, Exception) and "SSL" in type(cur).__name__:
            return True
        for attr in ("__cause__", "__context__"):
            nxt = getattr(cur, attr, None)
            if isinstance(nxt, BaseException):
                stack.append(nxt)
        for arg in getattr(cur, "args", ()) or ():
            if isinstance(arg, BaseException):
                stack.append(arg)
            elif isinstance(arg, str) and ("CERTIFICATE_VERIFY_FAILED" in arg or "SSL:" in arg):
                return True
    return False


def _is_dns_error(exc: BaseException) -> bool:
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, socket.gaierror):
            return True
        for attr in ("__cause__", "__context__"):
            nxt = getattr(cur, attr, None)
            if isinstance(nxt, BaseException):
                stack.append(nxt)
        for arg in getattr(cur, "args", ()) or ():
            if isinstance(arg, BaseException):
                stack.append(arg)
    return False


def _classify_exc(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if _is_tls_error(exc):
        return "tls_error"
    if _is_dns_error(exc):
        return "dns_error"
    return "network_error"


_ERROR_TEXT = {
    "timeout": "No answer within the timeout: nothing was charged.",
    "tls_error": "The TLS certificate could not be verified: nothing was charged.",
    "dns_error": "The domain does not resolve: nothing was charged.",
    "network_error": "The connection failed: nothing was charged.",
}


def make_client(user_agent: str = "", timeout: float = 20.0) -> "httpx.AsyncClient":
    """One HTTP client for the whole run, with redirects followed by hand."""
    return httpx.AsyncClient(
        headers={
            "User-Agent": (user_agent or DEFAULT_UA),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.8",
        },
        timeout=timeout,
        follow_redirects=False,
    )


async def read_page(client, url: str, *, timeout: float = 20.0, max_redirects: int = 5) -> dict:
    """Read one page and return a row. Never raises for a bad page."""
    norm = normalize_url(url)
    if norm is None:
        return _fail(STATUS_INVALID_URL,
                     "This is not an http or https address: nothing was requested and nothing was charged.")
    current = norm
    redirects = 0
    started = time.perf_counter()
    while True:
        kind = await host_classify(current)
        if kind == "private":
            return _fail(STATUS_PRIVATE,
                         "The host resolves to a private or loopback address: the request was blocked "
                         "and nothing was charged.", final=current, redirects=redirects)
        if kind == "invalid":
            return _fail(STATUS_INVALID_URL,
                         "The address has no usable host: nothing was requested.", final=current)
        if kind == "unresolvable":
            return _fail(STATUS_UNREACHABLE, _ERROR_TEXT["dns_error"], final=current,
                         redirects=redirects, error="dns_error")
        try:
            async with client.stream("GET", current, timeout=timeout) as response:
                code = response.status_code
                final_url = str(response.url)
                if code in (301, 302, 303, 307, 308) and max_redirects > 0:
                    location = response.headers.get("location")
                    if location:
                        nxt = urljoin(current, location.strip())
                        if redirects >= max_redirects:
                            return _fail(STATUS_UNREACHABLE,
                                         "Too many redirects (%d): nothing was charged." % redirects,
                                         final=current, redirects=redirects, error="redirect_loop")
                        current = nxt
                        redirects += 1
                        continue
                if 300 <= code < 400:
                    # A redirect that was not followed: maxRedirects is 0, or the answer
                    # names no target. Either way there is no page to read, so the redirect
                    # itself is the answer and it costs nothing: parsing it as a page used
                    # to sell a "301 Moved Permanently" title to the buyer.
                    elapsed = int((time.perf_counter() - started) * 1000)
                    if max_redirects <= 0:
                        reason = ("The page answered HTTP %d and maxRedirects is 0, so the redirect was "
                                  "not followed: there is no page to read and nothing was charged. "
                                  "Raise maxRedirects to follow it." % code)
                    else:
                        reason = ("The page answered HTTP %d with a redirect but named no target, so "
                                  "there was no page to read: nothing was charged." % code)
                    return _fail(STATUS_REDIRECT, reason, final=final_url, http=code,
                                 redirects=redirects, ms=elapsed, error="redirect")
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_BYTES:
                        break
                body = b"".join(chunks)
                content_type = response.headers.get("content-type") or ""
                elapsed = int((time.perf_counter() - started) * 1000)
        except httpx.TimeoutException:
            return _fail(STATUS_UNREACHABLE, _ERROR_TEXT["timeout"], final=current,
                         redirects=redirects, error="timeout")
        except httpx.HTTPError as exc:
            kind_exc = _classify_exc(exc)
            return _fail(STATUS_UNREACHABLE, _ERROR_TEXT.get(kind_exc, _ERROR_TEXT["network_error"]),
                         final=current, redirects=redirects, error=kind_exc)

        if code >= 400:
            return _fail(STATUS_HTTP_ERROR,
                         "The page answered HTTP %d: there was nothing to read and nothing was charged." % code,
                         final=final_url, http=code, redirects=redirects, ms=elapsed)
        lowered = content_type.lower()
        if content_type and "html" not in lowered and "xml" not in lowered:
            return _fail(STATUS_NOT_HTML,
                         "The link returned %s, not an HTML page: nothing was parsed and nothing was charged."
                         % (content_type.split(";")[0].strip() or "an unknown type"),
                         final=final_url, http=code, redirects=redirects, ms=elapsed)

        text, charset = decode_body(body, content_type)
        row = parse_page(text, final_url)
        row.update({
            "source": url,
            "url": url,
            "finalUrl": final_url,
            "statusCode": code,
            "redirectCount": redirects,
            "responseTimeMs": elapsed,
            "charset": charset,
        })
        return row
