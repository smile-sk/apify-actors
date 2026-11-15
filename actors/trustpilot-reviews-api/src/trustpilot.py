"""Pure rules of the Trustpilot actor: addresses, pages, filters, money, deadline.

Nothing here touches the network or the browser, so every rule below can be proven
by a test. The browser lives in browser.py, the orchestration in main.py.

Measured on live Trustpilot 2026-11-11 (headful Chromium, Frankfurt VPS):
  * an anonymous visitor gets at most 10 pages of 20 reviews per listing: page 11
    answers 200 with an empty reviews array, page 50 the same;
  * the first page of a star listing depends on how the address is written:
    `?stars=1` answers 20 reviews of rating 1 with `hasActiveFilters: true`, while
    `?stars=1&page=1` answers the same mixed set as the plain page 1 with
    `hasActiveFilters: false`. From page 2 the filter is confirmed by two independent
    measurements, so this actor never relies on the quirk and starts every bucket at
    page 2;
  * so the honest ceiling is one newest page read without a filter (20 reviews) plus
    five buckets of nine filtered pages (900), which is 920 reviews per company, not
    a thousand: a promise of 1000 would be a promise the listing does not serve;
  * `?sort=recency` works and the default order is already newest first;
  * an unknown domain answers 404, a known company with no reviews answers 200
    with an empty array, so "no such company" and "no reviews" are told apart by
    status, not by guesswork.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

EVENT_NAME = "review-delivered"
SUMMARY_STORE = "trustpilot-reviews-api-summary"

REVIEWS_PER_PAGE = 20
STAR_BUCKETS = (1, 2, 3, 4, 5)
# The anonymous listing ends at page 10 whatever the company size. This is the
# platform's own wall, measured, not our choice.
MAX_PAGES_HARD = 10
MAX_PAGES_DEFAULT = 10
# 20 newest reviews plus five buckets of nine filtered pages each.
MAX_REVIEWS_HARD = REVIEWS_PER_PAGE * (1 + len(STAR_BUCKETS) * (MAX_PAGES_HARD - 1))
MAX_REVIEWS_DEFAULT = 200
MAX_COMPANIES_DEFAULT = 5
MAX_COMPANIES_HARD = 50
# Rows per paid call. The SDK charges per item inside the batch and writes exactly
# what it charged, so the batch size is a comfort knob, not a money knob.
PAID_BATCH = 20
# One page costs a fetch plus a parse inside the same browser tab. The margin is
# computed per page, so a slow listing stops the run with a summary instead of
# being killed by the platform with none.
PAGE_MARGIN_EXTRA_S = 8.0
PAGE_TIME_BUDGET_S = 25.0
# A first navigation has to pass the wall and render the listing, so it is given
# more room than a same-origin page fetch, but never more than the run has left.
NAV_TIMEOUT_S = 45.0

ST_OK = "OK"
ST_INVALID = "NOT_CHECKED_INVALID"
ST_DUPLICATE = "NOT_CHECKED_DUPLICATE"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_BLOCKED = "NOT_CHECKED_BLOCKED_PAGE"
ST_NOT_FOUND = "NOT_CHECKED_NOT_FOUND"
ST_HTTP = "NOT_CHECKED_HTTP_ERROR"
ST_NO_REVIEWS = "NO_REVIEWS"
ST_FILTERED = "FILTERED_OUT"

# Trustpilot's own interim page, measured word for word: the primary WAF says
# "Verifying Connection", the bot answer says "blocked as it has been identified
# as coming from a bot", Cloudflare's says "Just a moment".
BLOCK_WORDS = re.compile(
    r"just a moment|verifying connection|checking your browser|"
    r"identified as coming from a bot|client challenge|attention required|"
    r"verify\s+(?:that\s+)?you(?:['\u2019]?re|\s+are)\s+not\s+a\s+robot", re.I)

IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                       r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def deadline_seconds() -> float | None:
    """Seconds left before the platform kills the run, or None when unknown."""
    raw = os.environ.get("ACTOR_TIMEOUT_AT")
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - datetime.now(timezone.utc)).total_seconds()


def page_margin_seconds() -> float:
    """The worst case for one page: one fetch, one parse, one body in memory."""
    return PAGE_TIME_BUDGET_S + PAGE_MARGIN_EXTRA_S


def nav_budget_seconds(left: float | None) -> tuple[float, float]:
    """How long the first navigation may take, worked out from what is left.

    A fixed 45 s navigation inside a run that has 34 s left is how a run dies by the
    platform's clock with no summary at all: the gate says "go on" and the navigation
    eats twice the reserve. So the two timeouts are cut from the remaining time and
    always add up to less than it.
    """
    if left is None:
        return NAV_TIMEOUT_S, PAGE_TIME_BUDGET_S
    room = max(6.0, float(left) - PAGE_MARGIN_EXTRA_S)
    nav = min(NAV_TIMEOUT_S, room * 0.65)
    wait = max(2.0, room - nav)
    return nav, wait


def normalize_company(raw: object) -> tuple[str | None, str]:
    """A domain to read, or None and a reason a buyer can act on.

    Accepts `amazon.com`, `www.amazon.com`, `https://www.amazon.com/review/x?y=1`
    and a Trustpilot profile url. Anything without a real domain is refused before
    a single request, so a broken input never costs the buyer a charge.
    """
    text = str(raw or "").strip()
    if not text:
        return None, "an empty line: nothing to look up, not charged"
    if " " in text:
        return None, "the line contains a space: expected a domain like example.com"
    candidate = text
    path = ""
    if "://" in candidate:
        parts = urlsplit(candidate)
        host = parts.hostname or ""
        path = parts.path or ""
    else:
        head, _, rest = candidate.partition("/")
        host = head.split("?", 1)[0].split("#", 1)[0]
        path = "/" + rest if rest else ""
    host = host.strip().strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None, "the line has no host: expected a domain like example.com"
    if ":" in host:
        host = host.split(":", 1)[0]
    if IPV4_RE.match(host):
        return None, ("the line is an ip address, not a company domain: Trustpilot "
                      "lists companies by domain")
    if not DOMAIN_RE.match(host):
        return None, "the line is not a domain: expected something like example.com"
    if host in {"trustpilot.com"}:
        # A profile address is a legitimate input: /review/amazon.com names the company.
        profile = re.match(r"^/review/([^/?#]+)", "/" + path.lstrip("/"))
        if profile:
            host = profile.group(1).strip().lower()
            if host.startswith("www."):
                host = host[4:]
        else:
            return None, "this is Trustpilot itself, not a company to read"
        if not DOMAIN_RE.match(host) or IPV4_RE.match(host):
            return None, "the profile address does not name a company domain"
    return host, ""


def review_url(domain: str, page: int = 1, stars: int | None = None) -> str:
    """The public listing url for one page, optionally inside one star bucket."""
    query = "?sort=recency"
    if stars is not None:
        query += f"&stars={int(stars)}"
    if page and int(page) > 1:
        query += f"&page={int(page)}"
    return f"https://www.trustpilot.com/review/{domain}{query}"


def star_buckets(wanted: object) -> list[int]:
    """Which star listings to read. Empty input means all five.

    Refused values are dropped instead of silently read as "all", because a typo
    must not cost five times the pages.
    """
    if wanted is None or wanted == [] or wanted == "":
        return list(STAR_BUCKETS)
    items = wanted if isinstance(wanted, list) else [wanted]
    out: list[int] = []
    for item in items:
        try:
            value = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 5 and value not in out:
            out.append(value)
    return out


def stars_problem(wanted: object, buckets: list[int]) -> str:
    """A reason to refuse the whole run before one request, or an empty string.

    `stars: [9]` used to produce an empty bucket list, then zero pages, then a row
    saying the company "has no reviews": a lie about a company nobody even opened.
    """
    given = not (wanted is None or wanted == [] or wanted == "")
    if given and not buckets:
        return ("the stars filter names no rating from 1 to 5: nothing was read and "
                "nothing was charged")
    return ""


def star_plan(buckets: list[int], max_pages: int) -> list[tuple[int | None, int, int]]:
    """Which (bucket, first page, last page) to read, measured, not guessed.

    The first page of a star listing answers unfiltered when the address carries
    `page=1` (see the module doc), so it is never read as a bucket: the newest page is
    read once without any filter and named honestly, and every bucket starts at page 2,
    where the filter is confirmed twice.

    All five buckets: 1 + 5 x 9 = 46 pages, 920 reviews.
    One bucket: 9 pages, 180 reviews, every one of them rating 1.
    """
    pages = max(1, int(max_pages))
    plan: list[tuple[int | None, int, int]] = []
    if len(buckets) >= len(STAR_BUCKETS):
        plan.append((None, 1, 1))
    if pages >= 2:
        for bucket in buckets:
            plan.append((bucket, 2, pages))
    return plan


def is_blocked(html: str, status: int | None = None) -> bool:
    """Is this body a wall instead of a listing?"""
    if status in (403, 429):
        return True
    if not html:
        return False
    head = html[:4000]
    return bool(BLOCK_WORDS.search(head))


def slim_review(raw: dict, domain: str, bucket: int | None, meta: dict,
                source: str | None = None) -> dict:
    """One review as the buyer sees it: flat, dated, with the company's answer."""
    consumer = raw.get("consumer") or {}
    dates = raw.get("dates") or {}
    labels = raw.get("labels") or {}
    verification = labels.get("verification") or {}
    reply = raw.get("reply") or None
    return {
        "company": meta.get("name") or domain,
        "companyDisplayName": meta.get("display") or None,
        "companyTrustScore": meta.get("score"),
        "companyReviewsTotal": meta.get("total"),
        "reviewId": raw.get("id"),
        "url": f"https://www.trustpilot.com/reviews/{raw.get('id')}" if raw.get("id") else None,
        "rating": raw.get("rating"),
        "title": raw.get("title") or "",
        "text": raw.get("text") or "",
        "language": raw.get("language") or None,
        "author": consumer.get("displayName") or None,
        "authorCountry": consumer.get("countryCode") or None,
        "authorReviews": consumer.get("numberOfReviews"),
        "verified": bool(verification.get("isVerified")),
        "publishedDate": dates.get("publishedDate"),
        "experiencedDate": dates.get("experiencedDate"),
        "companyReply": (reply or {}).get("message") or None,
        "companyReplyDate": (reply or {}).get("publishedDate") or None,
        "starBucket": bucket,
        "starSource": source or ("star-filter" if bucket is not None else "newest"),
    }


def review_meta_key(review: dict) -> str:
    """A stable key for de-duplication: the id, or a fingerprint when it is missing.

    A review without an id used to be counted as a duplicate and dropped, which lost
    real reviews silently. The fingerprint is built from fields that do not change
    between pages, so a review repeated across pages still collapses into one row.
    """
    review_id = review.get("reviewId")
    if review_id:
        return str(review_id)
    parts = [str(review.get(field) or "") for field in
             ("author", "publishedDate", "rating", "title", "text")]
    raw = "|".join(parts)
    if not raw.strip("|"):
        return ""
    return "fp-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def after_published(review: dict, cutoff: str | None) -> bool:
    """Keep reviews published at or after the cutoff (ISO date or datetime)."""
    if not cutoff:
        return True
    published = str(review.get("publishedDate") or "")
    if not published:
        return False
    return published[:10] >= str(cutoff)[:10]


def language_ok(review: dict, wanted: str | None) -> bool:
    if not wanted:
        return True
    return str(review.get("language") or "").lower() == str(wanted).lower()


def status_row(line: str, index: int, status: str, reason: str) -> dict:
    return {
        "company": None,
        "companyDisplayName": None,
        "reviewId": None,
        "input": line,
        "inputIndex": index,
        "status": status,
        "reason": reason,
        "billable": False,
    }


def price_state(charging) -> tuple[bool | None, str]:
    """Can this run charge? True / False / None (unknown) and a plain reason."""
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001 - any failure means "price unknown"
        return None, f"pricing read failed: {type(exc).__name__}"
    if info is None:
        return None, "pricing info is not readable"
    is_ppe = getattr(info, "is_pay_per_event", None)
    if is_ppe is None and isinstance(info, dict):
        is_ppe = info.get("isPayPerEvent", info.get("is_pay_per_event"))
    prices = getattr(info, "per_event_prices", None)
    if prices is None and isinstance(info, dict):
        prices = info.get("perEventPrices") or info.get("per_event_prices") or {}
    names: list[str] = []
    if isinstance(prices, dict):
        names = [str(key) for key in prices]
    elif isinstance(prices, list):
        for item in prices:
            name = getattr(item, "event_name", None)
            if name is None and isinstance(item, dict):
                name = item.get("eventName") or item.get("event_name")
            if name:
                names.append(str(name))
    if is_ppe is False:
        return False, "the actor is not sold per event"
    if EVENT_NAME not in names:
        if is_ppe is None and not names:
            return None, "the pricing answer is not understood"
        return False, f"{EVENT_NAME} is not priced, rows go out free"
    return True, f"{EVENT_NAME} is priced"


def stopped_status(stop_reason: str | None) -> tuple[str, str]:
    """The status a company gets when the run stopped before opening it."""
    if stop_reason == "spending-cap":
        return ST_CAP, "the spending cap was reached: not read and not charged"
    if stop_reason == "billing-price-unknown":
        return ST_BILLING, ("the price of this actor could not be read: not read and "
                            "not charged")
    if stop_reason == "deadline":
        return ST_TIME, "the run ran out of its time budget: not read and not charged"
    if stop_reason == "charge-error":
        return ST_ERROR, "the run stopped after a charge error: not read and not charged"
    return ST_ERROR, "the run stopped before this line: not read and not charged"


def undelivered_status(stop_reason: str | None) -> tuple[str, str]:
    """The status a review gets when it was read but the run stopped before selling it.

    The words matter: a review that was read is not "not read", and telling the buyer
    otherwise is the same lie as a made-up number.
    """
    if stop_reason == "spending-cap":
        return ST_CAP, ("the spending cap was reached: read but not delivered, "
                        "not charged")
    if stop_reason == "billing-price-unknown":
        return ST_BILLING, ("the price of this actor could not be read: read but not "
                            "delivered, not charged")
    if stop_reason == "deadline":
        return ST_TIME, ("the run ran out of its time budget: read but not delivered, "
                         "not charged")
    if stop_reason == "charge-error":
        return ST_ERROR, ("the run stopped after a charge error: read but not "
                          "delivered, not charged")
    return ST_ERROR, ("the run stopped before this review was delivered: read but not "
                      "delivered, not charged")
