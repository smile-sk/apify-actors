"""Pure rules of app-reviews-api: what a line means, what a row carries, what costs money.

No network here on purpose: every rule that decides a charge is testable without the
internet, and the fetchers in main.py only translate a live answer into these shapes.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

EVENT_NAME = "review-delivered"

APPLE = "ios"
GOOGLE = "android"
PLATFORMS = (APPLE, GOOGLE)

# Apple answers 10 pages of 50 reviews per country, page 11 is HTTP 400 (measured
# 2026-11-11). Google Play is deep and continues by token.
APPLE_PER_PAGE = 50
APPLE_MAX_PAGES = 10
APPLE_PER_COUNTRY = APPLE_PER_PAGE * APPLE_MAX_PAGES

MAX_REVIEWS_DEFAULT = 200
MAX_REVIEWS_HARD = 5000
MAX_APPS_DEFAULT = 5
MAX_APPS_HARD = 50
MAX_PAGES_DEFAULT = APPLE_MAX_PAGES
MAX_COUNTRIES_DEFAULT = 1
MAX_COUNTRIES_HARD = 10

PAID_BATCH = 20

REQUEST_TIMEOUT_S = 20.0

# A page may take the whole request timeout, and the summary still has to be written
# before the platform kills the container: the margin covers both.
SUMMARY_MARGIN_S = 8.0
PAGE_MARGIN_S = REQUEST_TIMEOUT_S + SUMMARY_MARGIN_S

PRICE_PER_REVIEW = 0.00009

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

BLOCK_WORDS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "access denied",
    "enable javascript and cookies",
    "unusual traffic",
    "verify you are a human",
    "are you a robot",
)

NUMBER_RE = re.compile(r"^\d{6,12}$")
PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,5}$", re.IGNORECASE)
COUNTRY_RE = re.compile(r"^[a-z]{2}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_blocked(text: str) -> bool:
    low = (text or "")[:4000].lower()
    return any(word in low for word in BLOCK_WORDS)


def normalize_app(raw: object) -> tuple[dict | None, str]:
    """A line becomes an app on one platform, or comes back with the reason it cannot.

    Accepted: a bare Apple id (389801252), a bare package (com.instagram.android),
    an App Store link (apps.apple.com/.../id389801252) and a Play link
    (play.google.com/store/apps/details?id=com.instagram.android).
    """
    text = str(raw or "").strip()
    if not text:
        return None, "the line is empty: nothing to read and nothing to charge"
    if len(text) > 400:
        return None, "the line is longer than any app id or store link"
    low = text.lower()

    if "play.google.com" in low or low.startswith("market://"):
        match = re.search(r"[?&]id=([A-Za-z0-9_.]+)", text)
        package = match.group(1) if match else ""
        if not PACKAGE_RE.match(package):
            return None, "the Play link carries no package id in id="
        return {"platform": GOOGLE, "appId": package}, ""

    if "apps.apple.com" in low or "itunes.apple.com" in low:
        match = re.search(r"/id(\d{6,12})", text) or re.search(r"[?&]id=(\d{6,12})", text)
        if not match:
            return None, "the App Store link carries no numeric app id (.../id389801252)"
        return {"platform": APPLE, "appId": match.group(1)}, ""

    if text.startswith("ios:") or text.startswith("apple:"):
        ident = text.split(":", 1)[1].strip()
        if NUMBER_RE.match(ident):
            return {"platform": APPLE, "appId": ident}, ""
        return None, "an ios line needs a numeric App Store id"

    if text.startswith("android:") or text.startswith("google:") or text.startswith("play:"):
        ident = text.split(":", 1)[1].strip()
        if PACKAGE_RE.match(ident):
            return {"platform": GOOGLE, "appId": ident}, ""
        return None, "an android line needs a package name like com.example.app"

    if NUMBER_RE.match(text):
        return {"platform": APPLE, "appId": text}, ""

    if PACKAGE_RE.match(text):
        return {"platform": GOOGLE, "appId": text}, ""

    return None, ("the line is not an App Store id, a Play package or a store link: "
                  "not read and not charged")


def app_key(platform: str, app_id: str) -> str:
    return f"{platform}:{app_id}".lower()


def countries_of(raw: object) -> tuple[list[str], str]:
    """Storefront codes, lower case and unique, or the reason the list is refused."""
    if raw is None or raw == "" or raw == []:
        return ["us"], ""
    if isinstance(raw, str):
        raw = [item for item in re.split(r"[,\s]+", raw) if item]
    items: list[str] = []
    for item in raw:
        code = str(item or "").strip().lower()
        if not code:
            continue
        if not COUNTRY_RE.match(code):
            return [], (f"\"{code}\" is not a two letter storefront code like us, de, cn: "
                        "nothing was read and nothing was charged")
        if code not in items:
            items.append(code)
    return (items or ["us"]), ""


def review_key(platform: str, review_id: str) -> str:
    return f"{platform}:{review_id}"


def apple_feed_url(country: str, app_id: str, page: int) -> str:
    return (f"https://itunes.apple.com/{country}/rss/customerreviews/"
            f"page={page}/id={app_id}/sortby=mostrecent/json")


def apple_lookup_url(country: str, app_id: str) -> str:
    return f"https://itunes.apple.com/lookup?id={app_id}&country={country}"


def play_review_url(app_id: str, review_id: str) -> str:
    return (f"https://play.google.com/store/apps/details?id={app_id}"
            f"&reviewId={review_id}")


def apple_review_url(country: str, app_id: str) -> str:
    """Apple's feed has no per-review permalink (measured), so the row points at the
    app's review page rather than pretending to a link that does not exist."""
    return f"https://apps.apple.com/{country}/app/id{app_id}?see-all=reviews"


def _label(node) -> str:
    if isinstance(node, dict):
        return str(node.get("label") or "")
    if node is None:
        return ""
    return str(node)


def _attrs(node) -> dict:
    if isinstance(node, dict):
        value = node.get("attributes")
        if isinstance(value, dict):
            return value
    return {}


def apple_rows(payload: dict, platform: str, app_id: str, country: str,
               meta: dict, skipped: dict | None = None) -> list[dict]:
    """Reviews of one Apple feed page, as rows this actor sells."""
    feed = payload.get("feed") if isinstance(payload, dict) else None
    if not isinstance(feed, dict):
        return []
    entries = feed.get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    rows: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        review_id = _label(entry.get("id")).strip()
        rating = _label(entry.get("im:rating")).strip()
        if not review_id or not rating.isdigit():
            if skipped is not None:
                skipped["unparsable"] = skipped.get("unparsable", 0) + 1
            continue
        rows.append({
            "platform": platform,
            "appId": app_id,
            "appTitle": meta.get("title") or None,
            "appRating": meta.get("rating"),
            "appRatingsTotal": meta.get("ratingsTotal"),
            "country": country,
            "reviewId": review_id,
            "rating": int(rating),
            "title": _label(entry.get("title")) or None,
            "text": _label(entry.get("content")) or None,
            "author": _label((entry.get("author") or {}).get("name")
                             if isinstance(entry.get("author"), dict) else ""),
            "version": _label(entry.get("im:version")) or None,
            "publishedDate": _label(entry.get("updated")) or None,
            "thumbsUp": int(_label(entry.get("im:voteCount")) or 0),
            "developerReply": None,
            "developerReplyDate": None,
            "url": apple_review_url(country, app_id),
            "source": "apple-rss",
        })
    return rows


def play_rows(payload: list[dict], platform: str, app_id: str, country: str,
              meta: dict, skipped: dict | None = None) -> list[dict]:
    rows: list[dict] = []
    for review in payload or []:
        if not isinstance(review, dict):
            continue
        review_id = str(review.get("reviewId") or "").strip()
        if not review_id:
            if skipped is not None:
                skipped["unparsable"] = skipped.get("unparsable", 0) + 1
            continue
        score = review.get("score")
        moment = review.get("at")
        if isinstance(moment, datetime):
            published = moment.replace(tzinfo=moment.tzinfo or timezone.utc).isoformat()
        else:
            published = str(moment) if moment else None
        reply_moment = review.get("repliedAt")
        if isinstance(reply_moment, datetime):
            replied = reply_moment.replace(tzinfo=reply_moment.tzinfo or timezone.utc).isoformat()
        else:
            replied = str(reply_moment) if reply_moment else None
        try:
            rating = int(score)
        except (TypeError, ValueError):
            if skipped is not None:
                skipped["unparsable"] = skipped.get("unparsable", 0) + 1
            continue
        rows.append({
            "platform": platform,
            "appId": app_id,
            "appTitle": meta.get("title") or None,
            "appRating": meta.get("rating"),
            "appRatingsTotal": meta.get("ratingsTotal"),
            "country": country,
            "reviewId": review_id,
            "rating": rating,
            "title": None,
            "text": review.get("content") or None,
            "author": review.get("userName") or None,
            "version": review.get("appVersion") or review.get("reviewCreatedVersion") or None,
            "publishedDate": published,
            "thumbsUp": int(review.get("thumbsUpCount") or 0),
            "developerReply": review.get("replyContent") or None,
            "developerReplyDate": replied,
            "url": play_review_url(app_id, review_id),
            "source": "google-play",
        })
    return rows


def keep_review(row: dict, min_rating: int | None, max_rating: int | None,
                cutoff: str | None) -> tuple[bool, str]:
    """The buyer's own filters. A dropped review is free and counted, never charged."""
    rating = row.get("rating")
    if min_rating is not None and isinstance(rating, int) and rating < min_rating:
        return False, f"rating {rating} is below minRating {min_rating}"
    if max_rating is not None and isinstance(rating, int) and rating > max_rating:
        return False, f"rating {rating} is above maxRating {max_rating}"
    if cutoff:
        published = str(row.get("publishedDate") or "")[:10]
        if not published:
            # No date means the window cannot be checked: a paid row would be a promise
            # the buyer cannot verify, so it goes out free with the reason.
            return False, f"no publication date to compare with publishedAfter {cutoff}"
        if published < cutoff:
            return False, f"published {published} is older than publishedAfter {cutoff}"
    return True, ""


def status_row(line: str, index: int, status: str, reason: str) -> dict:
    return {
        "platform": None,
        "appId": None,
        "appTitle": None,
        "reviewId": None,
        "input": line,
        "inputIndex": index,
        "status": status,
        "reason": reason,
        "billable": False,
    }


def price_state(charging) -> tuple[bool | None, str]:
    """Can this run charge? True / False / None (unknown) and a plain reason.

    get_pricing_info() and get_charging_manager() are synchronous in the SDK: awaiting
    them silently zeroes the whole money path (this park has paid for that lesson).
    """
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
    """What an app that was never read gets when the run stops."""
    if stop_reason == "spending-cap":
        return ST_CAP, "the spending cap was reached: not read and not charged"
    if stop_reason == "billing-price-unknown":
        return ST_BILLING, ("the price of this actor could not be read: not read and "
                            "not charged")
    if stop_reason == "deadline":
        return ST_TIME, "the run ran out of its time budget: not read and not charged"
    if stop_reason == "charge-error":
        return ST_ERROR, "the run stopped after a charge error: not read and not charged"
    return ST_ERROR, "the run stopped before this app: not read and not charged"


def undelivered_status(stop_reason: str | None) -> tuple[str, str]:
    """What a review that was read but not sold gets. Words matter: read is not unread."""
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
        return ST_ERROR, ("the run stopped after a charge error: read but not delivered, "
                          "not charged")
    return ST_ERROR, ("the run stopped before this review was delivered: read but not "
                      "delivered, not charged")
