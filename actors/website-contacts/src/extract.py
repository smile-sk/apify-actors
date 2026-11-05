"""Contact extraction from a website's HTML. Pure functions, no Apify SDK.

Honest by construction: every value carries the page it was found on, nothing is
generated or guessed. A field that is absent stays absent and the row says why.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from html import unescape
from urllib.parse import unquote, urljoin, urlparse, urlsplit

try:                                                   # parser is optional for pure tests
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:                                    # pragma: no cover - tests run without it
    HTMLParser = None                                  # type: ignore[assignment]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
BAD_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js",
              ".ico", ".woff", ".woff2", ".avif", ".bmp", ".mp4")
BAD_PARTS = ("example.", "sentry.io", "wixpress.com", "sentry-next", "@2x",
             "domain.com", "email.com", "yourdomain", "youremail", "test.com")
ROLE_LOCAL = {"info", "sales", "contact", "hello", "support", "office", "admin",
              "hr", "jobs", "careers", "press", "marketing", "team", "enquiries",
              "inquiries", "billing", "help", "service", "mail", "reception",
              "dpo", "privacy", "legal", "security", "compliance", "abuse", "gdpr"}
THIRD_PARTY = ("intercom.io", "sentry.io", "wixpress.com", "hubspot.com", "zendesk.com",
               "drift.com", "typeform.com", "mailchimp.com")
NOISE_LOCAL = ("noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
               "webmaster@localhost", "bounce", "notifications")

PHONE_RE = re.compile(
    r"(?<![\w.])"
    r"(\+?\(?\d{1,5}\)?(?:[\s.\-/]?\(?\d{1,8}\)?){0,8})"
    r"(?![\w.])"
)
DATE_LIKE = re.compile(r"(?<!\d)(?:19|20)\d{2}[-./]\d{1,2}[-./]\d{1,2}(?!\d)")
DATE_LIKE_DMY = re.compile(r"(?<!\d)\d{1,2}[-./]\d{1,2}[-./](?:19|20)\d{2}(?!\d)")
DATE_LIKE_MY = re.compile(r"(?<!\d)\d{1,2}[-./](?:19|20)\d{2}(?!\d)")
TIME_LIKE = re.compile(r"\d{1,2}:\d{2}")
COORD_PAIR = re.compile(r"(?<![\d.])\d{1,3}\.\d{3,}\s*[,;/]?\s*\d{1,3}\.\d{3,}(?![\d.])")
FRACTION = re.compile(r"(?<![\d.])\d+\.\d{4,}(?![\d.])")
E164_RE = re.compile(r"\+\d{9,15}(?!\d)")
TAX_ID = re.compile(r"(?<!\d)(?:\d{2}\.\d{3}\.\d{3}/\d{4}-?\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})(?!\d)")
YEAR_ROW = re.compile(r"^\d{4}(?:[\s.\-/]\d{4}){2,}$")
YEAR_OR_ROUND = re.compile(r"^(?:19|20)\d{2}$|^\d0{3}$")
PHONE_WORD = re.compile(
    r"\b(?:tel|telephone|phone|call|mobile|mobil|mob|handy|fon|telefonnummer|tlf|ph|gsm"
    r"|cell|whatsapp|viber|hotline|fax|office"
    r"|contact|reach|dial|support|help|sales|info|enquiries|inquiries|reception|desk|service|booking"
    r"|\u0442\u0435\u043b|\u0442\u0435\u043b\u0435\u0444\u043e\u043d|\u043c\u043e\u0431|\u0437\u0432\u043e\u043d"
    r"|\u043a\u043e\u043d\u0442\u0430\u043a\u0442|\u0441\u0432\u044f\u0437|\u043e\u0431\u0440\u0430\u0442\u043d"
    r"|tel\u00e9phone|t\u00e9l|tel\u00e9fono|tel\u00e9fono|telefono|movil|m\u00f3vil"
    r"|\u260e|\u2706)",
    re.I,
)

COUNT_NOUN_AFTER = re.compile(
    r"^(?:customers|clients|users|visitors|people|members|employees|companies|"
    r"businesses|stores|shops|downloads|installs|installs|followers|subscribers|"
    r"items|results|products|projects|orders|transactions|records|reviews|"
    r"citations|patents|partners|countries|pages|sites|experts|professionals|"
    r"tickets|requests|visits|views|impressions|transactions)\b",
    re.I,
)

SOCIALS = {
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in|school)/[A-Za-z0-9\-_%.]+"),
    "x": re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/(?!intent|share|home|hashtag|i/)[A-Za-z0-9_]{2,30}"),
    "facebook": re.compile(r"https?://(?:[a-z]{2,3}\.)?facebook\.com/(?!sharer|dialog|plugins|tr\?)[A-Za-z0-9._\-]{2,60}"),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/(?!p/|reel/|explore/)[A-Za-z0-9._]{2,40}"),
    "youtube": re.compile(r"https?://(?:www\.)?youtube\.com/(?:@|c/|channel/|user/)[A-Za-z0-9._\-]{2,60}"),
    "github": re.compile(r"https?://(?:www\.)?github\.com/(?!features|about|pricing|login|join|orgs)[A-Za-z0-9._\-]{2,40}"),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9._]{2,40}"),
    "telegram": re.compile(r"https?://(?:www\.)?(?:t\.me|telegram\.me)/[A-Za-z0-9_]{3,40}"),
    "threads": re.compile(r"https?://(?:www\.)?threads\.net/@[A-Za-z0-9._]{2,40}"),
}

DROP_TAG = re.compile(r"<(script|style|noscript|template)\b", re.I)

CONTACT_WORDS = re.compile(
    r"(contact|kontakt|about|impressum|imprint|support|help|company|team|legal|"
    r"privacy|datenschutz|mentions|contacto|contatti|contactez)", re.I)


def registrable(host: str) -> str:
    host = (host or "").lower().strip(".")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    two = ".".join(parts[-2:])
    if parts[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"} and len(parts) >= 3:
        return ".".join(parts[-3:])
    return two


def same_site(host: str, domain: str) -> bool:
    """A mailbox on a subdomain (info@mail.brand.com) belongs to brand.com.

    Comparing registrable domains instead of string suffixes: the old suffix check
    filed a company's own mailbox under 'foreign' and the row said NO_CONTACTS.
    """
    return registrable(host) == registrable(domain)

def site_key(site: str) -> str:
    """The host part, lowercase, without www: the unit of 'one site, one event'.

    The old key kept the path and the query, so simplify.hr and
    simplify.hr/contact were crawled twice and billed twice, while the README
    promises one event per site. Different subdomains stay different sites.
    """
    raw = (site or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    host = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host.strip(".")


def unique_sites(raw_sites: list[str]) -> tuple[list[str], int]:
    """Drop repeated sites by host, keeping the first spelling. Returns (sites, dropped)."""
    sites: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for raw in raw_sites:
        text = (raw or "").strip()
        key = site_key(text)
        if not key:
            continue
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        sites.append(text)
    return sites, dropped


def parse_sites(value) -> list[str]:
    """A comma/newline separated string is a list, not 33 separate characters."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;\n\r]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        return []
    return [str(part).strip() for part in parts if str(part).strip()]


SCHEME_LIKE = re.compile(r"^([a-z][a-z0-9+.\-]*):(.*)$", re.I)


def to_url(site: str) -> str:
    """'' when the text is not a http(s) website ('javascript:alert(1)', 'hello world')."""
    text = (site or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    if "://" in text:
        scheme = text.split("://", 1)[0].lower()
        candidate = text if scheme in {"http", "https"} else ""
    else:
        match = SCHEME_LIKE.match(text)
        if match and not match.group(2).isdigit():     # site.com:8080 is a port, not a scheme
            candidate = ""
        else:
            candidate = "https://" + text
    if not candidate:
        return ""
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        return ""
    return candidate if "." in host else ""



def host_of(url: str) -> str:
    """The hostname of a URL, or '' instead of an exception on junk input."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def is_private_host(host: str) -> bool:
    """Loopback, LAN and link-local hosts are never a buyer's website.

    Without this, a list containing 'localhost' or 'x.x.x.x' would make the
    Actor scan the network of whoever runs it.
    """
    name = (host or "").strip().lower().strip("[]")
    if not name:
        return True
    if name in {"localhost", "localhost.localdomain", "ip6-localhost", "0.0.0.0", "::1"}:
        return True
    if name.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return (address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast or address.is_unspecified)


def parse(html: str) -> "HTMLParser":
    if HTMLParser is None:
        raise RuntimeError("selectolax is not installed: HTML parsing is unavailable")
    return HTMLParser(html or "")


def page_text(tree: HTMLParser) -> str:
    body = tree.body
    return body.text(separator=" ", strip=True) if body else ""


def _ok_email(addr: str, domain: str) -> bool:
    addr = addr.strip().strip(".,;:<>()[]'\"")
    if not addr or addr.count("@") != 1:
        return False
    local, _, host = addr.partition("@")
    if not local or not host or "." not in host:
        return False
    if len(local) > 64 or len(addr) > 254:
        return False
    if addr.endswith(BAD_SUFFIX) or any(b in addr for b in BAD_PARTS):
        return False
    if not re.fullmatch(r"[a-z]{2,24}", host.rsplit(".", 1)[-1]):   # tld must be letters
        return False
    if any(local.startswith(n) for n in NOISE_LOCAL):
        return False
    if any(host.endswith(t) for t in THIRD_PARTY):
        return False
    if re.fullmatch(r"[0-9a-f]{16,}", local):          # asset hash, not a person
        return False
    if local.startswith("u003") or "%" in local:
        return False
    return True


def _code_text(tree: HTMLParser) -> str:
    """Text of code samples: a documentation snippet is not a contact."""
    chunks = []
    for css in ("pre", "code", "samp"):
        for node in tree.css(css):
            text = node.text() or ""
            if "@" in text:
                chunks.append(text)
    return " ".join(chunks)


DEMO_ATTR = re.compile(
    r"(demo|example|sample|snippet|mock|placeholder|preview|formfield|graphicform"
    r"|illustration|mockup)", re.I
)


def _demo_text(tree: HTMLParser) -> str:
    """Text inside demo/example blocks: a marketing mock-up is not a contact."""
    chunks = []
    for node in tree.css("[class],[id]"):
        marker = " ".join(str(v) for k, v in (node.attributes or {}).items()
                          if k in ("class", "id", "data-testid", "aria-label"))
        if not marker or not DEMO_ATTR.search(marker):
            continue
        text = node.text() or ""
        if "@" in text and len(text) < 4000:
            chunks.append(text)
    return " ".join(chunks)


def without_scripts(html: str) -> str:
    """Drop script/style bodies: a UI mock-up is not a contact.

    Live case: stripe.com ships jane.diaz@stripe.com inside a translation JSON
    payload for a checkout illustration, and the row reported it as a found email.
    Linear scan, not a lazy-dot regex: a broken page with thousands of unclosed
    <script> tags made the old pattern backtrack for five seconds.
    """
    source = html or ""
    lower = source.lower()
    kept: list[str] = []
    pos = 0
    while True:
        match = DROP_TAG.search(source, pos)
        if not match:
            kept.append(source[pos:])
            break
        kept.append(source[pos:match.start()])
        open_end = source.find(">", match.end())
        if open_end < 0:
            break                                    # unclosed opening tag: drop the rest
        close = lower.find("</" + match.group(1).lower(), open_end)
        if close < 0:
            break                                    # unclosed block: drop the rest
        close_end = source.find(">", close)
        pos = len(source) if close_end < 0 else close_end + 1
    return " ".join(kept)


def email_from_jsonld(value: str, page_url: str, domain: str) -> dict | None:
    """The site's own schema.org email, when it publishes one."""
    addr = str(value or "").strip().lower()
    if addr.startswith("mailto:"):
        addr = addr[7:]
    addr = addr.split("?")[0].strip()
    if not _ok_email(addr, domain):
        return None
    return {"value": addr, "foundOn": page_url, "kind": "jsonld",
            "role": addr.split("@")[0] in ROLE_LOCAL,
            "foreign": not same_site(addr.split("@", 1)[1], domain)}


def emails_from(tree: HTMLParser, html: str, page_url: str, domain: str) -> list[dict]:
    found: dict[str, dict] = {}
    code = _code_text(tree)
    demo = _demo_text(tree)
    for node in tree.css('a[href^="mailto:"]'):
        raw = unquote(node.attributes.get("href") or "")[7:]
        addr = raw.split("?")[0].strip().lower()
        if _ok_email(addr, domain):
            found[addr] = {"value": addr, "foundOn": page_url, "kind": "mailto",
                           "role": addr.split("@")[0] in ROLE_LOCAL,
                           "foreign": not same_site(addr.split("@", 1)[1], domain)}
    for match in EMAIL_RE.findall(without_scripts(html)):
        addr = unescape(match).strip().lower()
        if addr in found or not _ok_email(addr, domain):
            continue
        if addr in code.lower():                    # inside a code sample -> not a contact
            continue
        if addr in demo.lower():                    # inside a demo mock-up -> not a contact
            continue
        found[addr] = {"value": addr, "foundOn": page_url, "kind": "text",
                       "role": addr.split("@")[0] in ROLE_LOCAL,
                       "foreign": not same_site(addr.split("@", 1)[1], domain)}
    return list(found.values())


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _looks_like_year_row(raw: str) -> bool:
    """A row of years or round thousands is not a phone number.

    Live QA 0.1.20: 'Tel. 2026 2027 2028' and 'Support 1000 2000 3000' came
    back as phones. A phone word right next to the number made the shape check
    pass, and the site was billed as having contacts. Three or more groups of
    exactly four digits, every group a year or a round thousand, without a plus
    sign - that is a table of numbers, not a contact.
    """
    text = " ".join((raw or "").split())
    if not text or text.startswith("+") or not YEAR_ROW.match(text):
        return False
    return all(YEAR_OR_ROUND.match(part) for part in re.split(r"[\s.\-/]", text))


def _looks_like_not_phone(raw: str) -> bool:
    """True when the matched text is a date, a time or a coordinate pair.

    Live cases that billed a site without contacts:
    'Published 2026-11-04 19:48' -> phones=['+2026110419'],
    'Stand: 04.11.2026 19:48'    -> phones=['+0411202619'],
    'Find us at 50.4501 30.5234' -> phones=['+504501305234'].
    """
    if not raw:
        return True
    if _looks_like_year_row(raw):              # a row of years is not a phone
        return True
    if DATE_LIKE.search(raw) or DATE_LIKE_DMY.search(raw) or DATE_LIKE_MY.search(raw):
        return True
    if TIME_LIKE.search(raw):
        return True
    if COORD_PAIR.search(raw) or FRACTION.search(raw):
        return True
    if TAX_ID.search(raw):                     # CNPJ / CPF - a registry number, not a phone
        return True
    if "/" in raw and "-" in raw:              # '22.121.209/0001-46': two separators, one id
        return True
    digits = _digits(raw)
    if len(digits) == 12 and digits[:2] in {"19", "20"}:
        month, day = int(digits[4:6]), int(digits[6:8])
        if 1 <= month <= 12 and 1 <= day <= 31:
            return True
    return False


NOT_PHONE_BEFORE = re.compile(
    r"(?:tracking|track|order|invoice|parcel|shipment|reference|ticket|account|"
    r"customer|sku|isbn|iban|bic|swift|vat|ust|reg|nr|price|total|amount|sum|\u043d\u043e\u043c\u0435\u0440|\u0437\u0430\u043a\u0430\u0437[\u0430\u0443]?"
    r"|\u043e\u0442\u0441\u043b\u0435\u0436\u0438\u0432\u0430\u043d\u0438[\u0435\u044f]"
    r"|\u043d\u0430\u043a\u043b\u0430\u0434\u043d\w*)\s*"
    r"(?:number|no\.?|num|#|\u2116)?\s*[:#\u2116]?\s*$",
    re.I,
)


def _near_not_phone(text: str, start: int) -> bool:
    """'Tracking number 1234 5678 9012' is a parcel id, not a phone."""
    return bool(NOT_PHONE_BEFORE.search((text or "")[max(0, start - 48):start]))


def _near_phone_word(text: str, start: int, length: int) -> bool:
    """Is a phone word right next to the number? A bare number is not a phone.

    QA 0.1.16: a 48-character window billed 'Contact us - 1 350 000 000 customers'
    as a phone. So the label must stand immediately before the number (only
    punctuation between), a count noun right after the number vetoes it, and
    a form with '/' or parentheses is a phone by shape ('030/12345678').
    """
    if start is None:
        return False
    source = text or ""
    end = start + length
    if COUNT_NOUN_AFTER.match(source[end:end + 24].strip()):
        return False
    raw = source[start:end]
    if "/" in raw or "(" in raw:
        return True
    before = source[max(0, start - 24):start]
    for match in PHONE_WORD.finditer(before):
        tail = before[match.end():]
        if len(tail) <= 8 and not any(ch.isdigit() for ch in tail):
            return True
    return bool(PHONE_WORD.search(source[end:end + 24]))


def phones_from(tree: HTMLParser, text: str, page_url: str) -> list[dict]:
    """Phones exactly as the page writes them. Nothing is generated.

    Two rules keep numeric noise out of the field the buyer pays for: a '+' is
    never glued to a number that did not have one ('8 (495) 123-45-67' stayed
    Moscow on the page and became Vietnam in an earlier build), and a number
    written without '+' must stand next to a phone word or a tel: link -
    python.org shows the Fibonacci sample '8 13 21 34 55 89 144' inside <code>.
    """
    found: dict[str, dict] = {}
    source = text or ""

    def add(raw: str, kind: str, start: int | None = None) -> None:
        value = " ".join(re.split(r"[;&,]", raw or "", 1)[0].split())   # drop ext=123
        if not value or _looks_like_not_phone(value):
            return
        if start is not None and _near_not_phone(source, start):
            return
        digits = _digits(value)
        if re.search(r"\(\s*0\s*\)", value):        # '+44 (0)7911 123456': trunk prefix in brackets
            digits = _digits(re.sub(r"\(\s*0\s*\)", "", value, count=1))
        labelled = (kind != "tel-link" and start is not None
                    and _near_phone_word(source, start, len(raw)))
        floor = 9 if (kind == "tel-link" or labelled) else 10   # 9 digits: ES/IT numbers
        if not (floor <= len(digits) <= 15):
            return
        if digits.startswith("000") or len(set(digits)) <= 3:
            return
        if not value.startswith("+") and kind != "tel-link":
            if not labelled:
                trunk = digits.startswith("0") and 9 <= len(digits) <= 11
                international = value.startswith("00") and 11 <= len(digits) <= 15
                if not (trunk or international) or not re.search(r"[\s.\-/]", value):
                    return
        key = digits[-9:]
        if key in found:
            return
        entry: dict = {"value": value, "foundOn": page_url, "kind": kind}
        if value.startswith("+"):
            entry["e164"] = "+" + digits
        found[key] = entry

    for node in tree.css('a[href^="tel:"]'):
        add(unquote(node.attributes.get("href") or "")[4:].split("?")[0], "tel-link")
    for match in E164_RE.finditer(source):
        add(match.group(0), "text", match.start())
    for match in PHONE_RE.finditer(source):
        add(match.group(0), "text", match.start())
    return list(found.values())


def phone_text(tree: HTMLParser) -> str:
    """Page text with code samples removed: a documentation sample is not a contact.

    Live case: python.org's front page carries the Fibonacci sequence
    '8 13 21 34 55 89 144' inside <pre><code>, and an earlier build reported it
    as two phone numbers and billed the site as having contacts.
    """
    raw_html = getattr(tree, "html", "") or ""
    if not raw_html:
        return page_text(tree)
    try:
        clean = parse(raw_html)
    except Exception:                                   # pragma: no cover - broken markup
        return page_text(tree)
    for css in ("pre", "code", "samp"):
        for node in clean.css(css):
            for method in ("decompose", "remove"):
                action = getattr(node, method, None)
                if action is None:
                    continue
                try:
                    action()
                    break
                except Exception:                       # pragma: no cover - parser quirks
                    continue
    return page_text(clean)


def phone_from_jsonld(value: str, page_url: str) -> dict | None:
    """The site's own schema.org telephone, returned as published."""
    text = str(value or "").strip()
    if text.lower().startswith("tel:"):
        text = text[4:]
    text = text.split(";")[0].strip()
    digits = _digits(text)
    if not (7 <= len(digits) <= 15) or len(set(digits)) <= 3:
        return None
    entry: dict = {"value": " ".join(text.split()), "foundOn": page_url, "kind": "jsonld"}
    if text.startswith("+"):
        entry["e164"] = "+" + digits
    return entry


def resolves_private(host: str) -> bool:
    """True when a name points at a loopback/LAN address (localtest.me -> 127.0.0.1).

    A literal address was refused before, a DNS name that resolves to one was not.
    """
    name = (host or "").strip().lower().strip("[]")
    if not name:
        return True
    if is_private_host(name):
        return True
    try:
        infos = socket.getaddrinfo(name, None)
    except Exception:
        return False
    for info in infos:
        address = info[4][0] if len(info) > 4 else ""
        if address and is_private_host(address):
            return True
    return False



def socials_from(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, rx in SOCIALS.items():
        match = rx.search(html or "")
        if match:
            out[name] = match.group(0).rstrip("/\"'),;")
    return out


def _walk_ld(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk_ld(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _walk_ld(value)


def jsonld_org(tree: HTMLParser) -> dict:
    org: dict = {}
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except Exception:
            continue
        for item in _walk_ld(data):
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            types = [str(t).lower() for t in types if t]
            if not any(t in {"organization", "localbusiness", "corporation", "store",
                             "professionalservice", "onlinestore"} for t in types):
                continue
            if not org.get("name") and isinstance(item.get("name"), str):
                org["name"] = item["name"].strip()
            addr = item.get("address")
            if addr and not org.get("address"):
                if isinstance(addr, str):
                    org["address"] = {"text": addr.strip()}
                elif isinstance(addr, dict):
                    mapped = {
                        "street": addr.get("streetAddress"),
                        "city": addr.get("addressLocality"),
                        "region": addr.get("addressRegion"),
                        "postalCode": addr.get("postalCode"),
                        "country": addr.get("addressCountry") if isinstance(addr.get("addressCountry"), str)
                        else (addr.get("addressCountry") or {}).get("name") if isinstance(addr.get("addressCountry"), dict) else None,
                    }
                    mapped = {k: (v.strip() if isinstance(v, str) else v) for k, v in mapped.items() if v}
                    if mapped:
                        org["address"] = mapped
            for key in ("email", "telephone"):
                if item.get(key) and key not in org:
                    org[key] = str(item[key]).strip()
    return org


def company_name(tree: HTMLParser, org: dict, domain: str) -> str:
    if org.get("name"):
        return org["name"][:120]
    node = tree.css_first('meta[property="og:site_name"]')
    if node and (node.attributes.get("content") or "").strip():
        return node.attributes["content"].strip()[:120]
    node = tree.css_first("title")
    if node and (node.text() or "").strip():
        title = re.sub(r"\s+", " ", node.text()).strip()
        return re.split(r"\s+[|\-\u2013\u2014:·]\s+", title)[0][:120]
    return domain


def contact_links(tree: HTMLParser, page_url: str, domain: str, limit: int) -> list[str]:
    seen: dict[str, None] = {}
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        absolute = urljoin(page_url, href).split("#")[0]
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if registrable(parsed.hostname or "") != domain:
            continue
        if absolute.rstrip("/") == page_url.rstrip("/"):
            continue
        label = ((node.text() or "") + " " + href)
        if CONTACT_WORDS.search(label):
            seen.setdefault(absolute, None)
        if len(seen) >= limit:
            break
    return list(seen)


BLOCK_MARKERS = ("just a moment", "attention required", "cf-chl", "challenge-platform",
                 "enable javascript and cookies", "ddos protection", "access denied")


def is_blocked(html: str, tree: HTMLParser) -> bool:
    head = (html or "")[:4000].lower()
    title = (tree.css_first("title").text() if tree.css_first("title") else "") or ""
    haystack = head + " " + title.lower()
    return any(marker in haystack for marker in BLOCK_MARKERS)
