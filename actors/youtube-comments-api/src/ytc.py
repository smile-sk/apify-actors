"""Reading YouTube comments over the public innertube endpoint, no browser.

Why no browser. Measured 2026-11-12: the same request answered from a home
connection and from our Frankfurt VPS, which is a datacenter address:

  * POST /youtubei/v1/next with the WEB client and a videoId returned the watch
    payload, 420 086 bytes, and the comment section arrived as an
    itemSectionRenderer with sectionIdentifier "comment-item-section";
  * that section's continuation token returned 21 commentThreadRenderer entries
    and 20 commentEntityPayload records, 284 311 bytes, from both addresses.

So comments do not need the Xvfb trick that Trustpilot's wall needed, and this
actor ships without Chromium: 512 MB instead of 1024 and no display.

What YouTube actually gives, measured on the same payload:

  * a comment's text lives in frameworkUpdates.entityBatchUpdate, keyed by the
    same key the thread points at, not inside the thread renderer;
  * the date is a RELATIVE string ("6 years ago"). There is no absolute date in
    the payload, so the actor sells publishedTimeText and says so on the card
    instead of inventing a timestamp;
  * like and reply counts are strings ("4.8M", "986") and are parsed into
    numbers, with the original text kept beside them;
  * "Comments are turned off." appears in commentsHeaderRenderer even on a video
    with ten million comments, because the reply box is disabled for a guest.
    It must never be used as a signal, or every video would read as closed.

Order of work is a product decision, not an accident: top level comments are
read first and replies come after them, into whatever maxCommentsPerVideo has
left. Otherwise the first busy thread eats the whole budget and a buyer who
asked for a hundred comments gets a hundred replies to one of them.
"""
from __future__ import annotations

import gzip
import json
import re
import urllib.error
import urllib.request

ENDPOINT = "https://www.youtube.com/youtubei/v1/next?prettyPrint=false"
CLIENT_VERSION = "2.20260101.00.00"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

EVENT_NAME = "comment-delivered"

ST_OK = "OK"
ST_INVALID = "INVALID_VIDEO"
ST_DUPLICATE = "DUPLICATE"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_NO_COMMENTS = "NOT_CHECKED_NO_COMMENTS"
ST_UNAVAILABLE = "NOT_CHECKED_UNAVAILABLE"
ST_READ_ERROR = "NOT_CHECKED_READ_ERROR"
ST_TIME = "NOT_CHECKED_TIME"
ST_BILLING = "NOT_CHECKED_BILLING"

NOT_CHECKED = {ST_LIMIT, ST_NO_COMMENTS, ST_UNAVAILABLE, ST_READ_ERROR,
               ST_TIME, ST_BILLING}

MAX_COMMENTS_DEFAULT = 100
MAX_COMMENTS_HARD = 2000
MAX_THREAD_DEFAULT = 20
MAX_THREAD_HARD = 200
MAX_VIDEOS_DEFAULT = 20
MAX_VIDEOS_HARD = 200

# Kept back from the run's remaining time: the summary has to be written before
# the platform's clock kills the container.
PAGE_MARGIN_S = 25.0

# A listing that keeps handing out the same token would loop forever, which is how a
# scraper burns a run's whole clock and comes back with nothing. Measured pages carry
# 20 comments each, so 120 pages are far above the hard row ceiling.
MAX_PAGES_PER_VIDEO = 120

_ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/|/live/|/v/)([A-Za-z0-9_-]{11})")
_BARE_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SUFFIX = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def video_id(text: str) -> str | None:
    """An 11 character video id out of a watch, shorts, embed or bare form."""
    if not text:
        return None
    value = str(text).strip()
    if _BARE_RE.match(value):
        return value
    match = _ID_RE.search(value)
    return match.group(1) if match else None


def parse_count(text: str) -> int | None:
    """"4.8M" -> 4800000, "986" -> 986, "10,644,918" -> 10644918, else None."""
    if text is None:
        return None
    raw = str(text).strip().replace(",", "").replace("\u00a0", " ")
    if not raw:
        return None
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([KMB])?$", raw, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    if suffix:
        number *= _SUFFIX[suffix]
    return int(round(number))


def walk(obj, key: str) -> list:
    """Every value stored under `key`, at any depth."""
    found: list = []
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for name, value in node.items():
                if name == key:
                    found.append(value)
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _first_token(obj) -> str | None:
    for node in walk(obj, "continuationCommand"):
        if isinstance(node, dict) and node.get("token"):
            return node["token"]
    return None


def _comment_section(data: dict) -> dict | None:
    for section in walk(data, "itemSectionRenderer"):
        if isinstance(section, dict) and section.get("sectionIdentifier") == "comment-item-section":
            return section
    return None


def _sort_tokens(data: dict) -> dict[str, str]:
    """The watch payload carries the sort menu: Top and Newest, each a token."""
    tokens: dict[str, str] = {}
    for renderer in walk(data, "sortFilterSubMenuRenderer"):
        for item in (renderer or {}).get("subMenuItems", []) or []:
            title = str(item.get("title") or "").strip().lower()
            token = _first_token(item)
            if title and token:
                tokens[title] = token
    return tokens


def _next_page_token(data: dict) -> str | None:
    """The token for the next page of the listing, skipping per-thread replies."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for name, value in node.items():
                if name == "commentThreadRenderer":
                    continue
                if name == "continuationItemRenderer" and isinstance(value, dict):
                    token = _first_token(value)
                    if token:
                        return token
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(value for value in node if not _is_thread(value))
    return None


def _is_thread(node) -> bool:
    return isinstance(node, dict) and "commentThreadRenderer" in node


def _text_of(node) -> str:
    """Innertube text is either {"simpleText": ...} or {"runs": [{"text": ...}]}."""
    if not isinstance(node, dict):
        return ""
    if node.get("simpleText") is not None:
        return str(node["simpleText"])
    return "".join(str(run.get("text") or "") for run in node.get("runs", []) or [])


def comment_payloads(data: dict) -> list[dict]:
    """Every comment record in the response, in the order YouTube stored it."""
    records = []
    mutations = (data.get("frameworkUpdates", {}) or {}).get("entityBatchUpdate", {}) or {}
    for mutation in mutations.get("mutations", []) or []:
        payload = (mutation or {}).get("payload", {}).get("commentEntityPayload")
        if isinstance(payload, dict):
            records.append(payload)
    return records


def pinned_of(data: dict) -> dict[str, str]:
    """Thread keys whose comment carries "Pinned by ..." as a visible label."""
    pinned: dict[str, str] = {}
    for view in walk(data, "commentViewModel"):
        inner = (view or {}).get("commentViewModel") if isinstance(view, dict) else None
        if isinstance(inner, dict) and inner.get("pinnedText") and inner.get("commentKey"):
            pinned[inner["commentKey"]] = str(inner["pinnedText"])
    return pinned


def slim(payload: dict, video: str, parent_id: str | None, pinned: dict[str, str]) -> dict:
    """One comment as a row: only the fields we sell, plus the honesty flags."""
    properties = payload.get("properties", {}) or {}
    author = payload.get("author", {}) or {}
    toolbar = payload.get("toolbar", {}) or {}
    content = properties.get("content", {}) or {}
    comment_id = properties.get("commentId") or None
    likes_text = toolbar.get("likeCountNotliked") or toolbar.get("likeCountLiked") or ""
    replies_text = toolbar.get("replyCount") or ""
    # heartActiveTooltip is a state template, not a fact: it reads "by @channel" on
    # every record, measured 180 of 180 on three videos on 2026-11-12, including videos
    # where the uploader hearted nothing. The mark only a hearted comment carries is the
    # creator thumbnail in the same toolbar.
    hearted = bool(toolbar.get("creatorThumbnailUrl"))
    return {
        "videoId": video,
        "commentId": comment_id,
        "parentCommentId": parent_id,
        "isReply": parent_id is not None,
        "author": author.get("displayName"),
        "authorChannelId": author.get("channelId"),
        "authorIsVerified": bool(author.get("isVerified")),
        "authorIsCreator": bool(author.get("isCreator")),
        "text": content.get("content") or "",
        "likes": parse_count(likes_text),
        "likesText": likes_text or None,
        "replyCount": parse_count(replies_text),
        "replyCountText": replies_text or None,
        "heartedByCreator": bool(hearted),
        "pinnedText": pinned.get(payload.get("key", "")),
        "publishedTimeText": properties.get("publishedTime") or None,
        "replyLevel": properties.get("replyLevel"),
        "url": (f"https://www.youtube.com/watch?v={video}&lc={comment_id}"
                if comment_id else None),
    }


def status_row(value: str, index: int, status: str, reason: str) -> dict:
    """A free row: it names the reason and carries no comment, so nothing leaks."""
    return {"input": value, "inputIndex": index, "videoId": video_id(value),
            "status": status, "billable": False, "reason": reason, "text": None,
            "commentId": None}


def undelivered_status(reason: str) -> tuple[str, str]:
    if reason == "deadline":
        return ST_TIME, ("the run ran out of time before this comment was delivered: "
                         "not delivered and not charged")
    if reason == "spending-cap":
        return ST_LIMIT, ("the spending cap was reached: this comment was not delivered "
                          "and not charged")
    if reason == "limit":
        return ST_LIMIT, ("the run reached maxCommentsPerVideo: this comment was not "
                          "delivered and not charged")
    if reason == "billing-price-unknown":
        return ST_BILLING, ("the price could not be read, so nothing is sold and nothing "
                            "is charged: this comment was not delivered")
    return ST_READ_ERROR, ("the comment was read but could not be delivered: not "
                           "charged")


def price_state(charging) -> tuple[bool | None, str]:
    """Can we charge at all? None means "unknown", never a silent yes."""
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001 - a price we cannot read is not a price
        return None, f"the price could not be read: {type(exc).__name__}"
    if info is None:
        return None, "the platform returned no price list"
    if getattr(info, "is_pay_per_event", None) is False:
        return False, "this actor is not sold per event right now"
    prices = getattr(info, "per_event_prices", None)
    if isinstance(prices, dict) and prices and EVENT_NAME not in prices:
        return None, f"the price list has no {EVENT_NAME} event"
    return True, "sold per delivered comment"


class Innertube:
    """A thin client: one POST, JSON back, no cookies, no key."""

    def __init__(self, timeout_s: float = 25.0) -> None:
        self.timeout_s = timeout_s
        self.calls = 0

    def call(self, body: dict) -> dict:
        payload = {"context": {"client": {"clientName": "WEB",
                                          "clientVersion": CLIENT_VERSION,
                                          "hl": "en", "gl": "US"}}}
        payload.update(body)
        request = urllib.request.Request(
            ENDPOINT, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                     "X-Youtube-Client-Name": "1",
                     "X-Youtube-Client-Version": CLIENT_VERSION,
                     "Accept-Language": "en-US,en;q=0.9"})
        self.calls += 1
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            raw = response.read()
        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: BLE001 - an uncompressed answer is still an answer
            pass
        return json.loads(raw)


def read_video(client, video: str, max_comments: int, sort: str = "top",
               include_replies: bool = True, max_per_thread: int = 0,
               known: set | None = None, time_left: float | None = None) -> dict:
    """Read one video's comments. Returns rows plus what happened, never raises.

    `known` is the set of comment ids already delivered in this run: one comment
    is sold once, even when YouTube shows it twice (pinned plus in the listing).

    The budget is split: a quarter of it is held back for replies, because a title that
    promises replies must deliver them, and a busy video otherwise fills the whole budget
    with top level comments and never opens a single thread. Leftover space goes back to
    the listing.
    """
    known = known if known is not None else set()
    rows: list[dict] = []
    reply_tokens: list[tuple[str, str]] = []
    pages = 0
    reason = ""
    meta: dict = {}
    seen = 0
    skipped_empty = 0
    duplicate = 0
    hit_limit = False

    try:
        first = client.call({"videoId": video})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": ST_UNAVAILABLE, "rows": [], "pages": 0, "meta": meta,
                    "reason": "YouTube answered 404 for this video: not delivered "
                              "and not charged"}
        return {"status": ST_READ_ERROR, "rows": [], "pages": 0, "meta": meta,
                "reason": f"YouTube answered HTTP {exc.code}: not delivered and not charged"}
    except Exception as exc:  # noqa: BLE001 - a bad answer is data, not a crash
        return {"status": ST_READ_ERROR, "rows": [], "pages": 0, "meta": meta,
                "reason": f"the video could not be read ({type(exc).__name__}): "
                          "not delivered and not charged"}

    meta["videoTitle"] = _video_title(first)
    meta["channel"] = _video_channel(first)

    section = _comment_section(first)
    if section is None:
        return {"status": ST_NO_COMMENTS, "rows": [], "pages": 0, "meta": meta,
                "reason": ("the watch page exposes no comment section: the video may "
                           "have comments turned off or none at all, nothing was "
                           "charged")}

    token = _first_token(section)
    sorts = _sort_tokens(first)
    wanted = sorts.get("newest" if sort == "newest" else "top")
    meta["sortApplied"] = bool(wanted)
    if wanted:
        token = wanted

    budget = max(1, int(max_comments))
    replies_on = bool(include_replies) and max_per_thread > 0
    quota = max(1, budget // 4) if replies_on and budget >= 4 else 0
    listing_target = max(1, budget - quota)
    state = {"seen": 0, "skipped": 0, "duplicate": 0, "pages": 0}

    listing = _read_listing(client, video, token, listing_target, known, state, time_left)
    rows.extend(listing["rows"])
    reply_tokens.extend(listing["replyTokens"])
    pages += listing["pages"]
    reason = listing["reason"] or reason
    if listing.get("meta"):
        meta.update(listing["meta"])
    hit_limit = listing["hitLimit"]

    replies_sold = 0
    if replies_on and rows and len(rows) < budget:
        for index, (parent_id, reply_token) in enumerate(reply_tokens):
            if index >= quota:
                break
            left = budget - len(rows)
            if left <= 0:
                break
            if time_left is not None and time_left <= PAGE_MARGIN_S:
                reason = reason or "deadline"
                break
            extra = _read_replies(client, video, reply_token, parent_id,
                                  min(max_per_thread, left), known, left, state)
            rows.extend(extra)
            replies_sold += len(extra)

    if listing["nextToken"] and len(rows) < budget and not reason:
        more = _read_listing(client, video, listing["nextToken"], budget - len(rows),
                             known, state, time_left)
        rows.extend(more["rows"])
        pages += more["pages"]
        reason = reason or more["reason"]
        hit_limit = hit_limit or more["hitLimit"]

    seen = state["seen"]
    skipped_empty = state["skipped"]
    duplicate = state["duplicate"]
    status = ST_OK if rows else ST_NO_COMMENTS
    if not reason:
        if not rows:
            reason = ("the listing answered with no comments: nothing was delivered "
                      "and nothing was charged")
        elif len(rows) >= budget:
            hit_limit = True
    return {"status": status, "rows": rows[:budget], "pages": pages, "reason": reason,
            "meta": meta, "replies": replies_sold, "repliesOffered": len(reply_tokens),
            "seen": seen, "skippedEmpty": skipped_empty, "duplicate": duplicate,
            "hitLimit": hit_limit}


def _read_listing(client, video: str, token: str, want: int, known: set,
                  state: dict, time_left: float | None) -> dict:
    """One pass over the listing: rows, reply tokens, the token of the next page."""
    rows: list[dict] = []
    reply_tokens: list[tuple[str, str]] = []
    pages = 0
    reason = ""
    meta: dict = {}
    pinned: dict[str, str] = {}
    visited: set[str] = set()
    hit_limit = False
    while token and len(rows) < want:
        if token in visited:
            # The same continuation twice means the listing is not advancing. Going on
            # would read the same page until the platform kills the run.
            reason = reason or "the listing repeated a page instead of advancing"
            break
        if len(visited) >= MAX_PAGES_PER_VIDEO:
            reason = reason or f"the listing did not end within {MAX_PAGES_PER_VIDEO} pages"
            break
        visited.add(token)
        if time_left is not None and time_left <= PAGE_MARGIN_S:
            reason = "deadline"
            break
        try:
            page = client.call({"continuation": token})
        except Exception as exc:  # noqa: BLE001 - reported as a data problem
            reason = f"a comments page could not be read ({type(exc).__name__})"
            break
        pages += 1
        pinned.update(pinned_of(page))
        if meta.get("commentsCountText") is None:
            meta["commentsCountText"] = _comments_count(page)
        threads = _reply_tokens_by_comment(page)
        for payload in comment_payloads(page):
            properties = payload.get("properties", {}) or {}
            if properties.get("replyLevel") not in (0, None):
                continue
            state["seen"] += 1
            row = slim(payload, video, None, pinned)
            if not (row.get("text") or "").strip():
                # An emoji-only comment has no text to sell, and a row with nothing
                # in it is not a product: it is counted and never charged.
                state["skipped"] += 1
                continue
            if not row["commentId"] or row["commentId"] in known:
                state["duplicate"] += 1
                continue
            known.add(row["commentId"])
            rows.append(row)
            if row["commentId"] in threads:
                reply_tokens.append((row["commentId"], threads[row["commentId"]]))
            if len(rows) >= want:
                break
        token = _next_page_token(page)
    if token:
        hit_limit = True
    return {"rows": rows, "replyTokens": reply_tokens, "nextToken": token, "pages": pages,
            "reason": reason, "meta": meta, "hitLimit": hit_limit}


def _read_replies(client, video: str, token: str, parent_id: str, max_per_thread: int,
                  known: set, budget: int, state: dict) -> list[dict]:
    """Replies of one thread, capped, and never more than the run can sell."""
    rows: list[dict] = []
    page_token = token
    pages = 0
    while page_token and len(rows) < max_per_thread and len(rows) < budget and pages < 5:
        try:
            page = client.call({"continuation": page_token})
        except Exception:  # noqa: BLE001 - a reply page that fails is a reason, not a crash
            break
        pages += 1
        for payload in comment_payloads(page):
            state["seen"] += 1
            row = slim(payload, video, parent_id, {})
            if not (row.get("text") or "").strip():
                state["skipped"] += 1
                continue
            if not row["commentId"] or row["commentId"] in known:
                state["duplicate"] += 1
                continue
            known.add(row["commentId"])
            rows.append(row)
            if len(rows) >= max_per_thread or len(rows) >= budget:
                break
        page_token = _next_page_token(page)
    return rows


def _reply_tokens_by_comment(page: dict) -> dict[str, str]:
    """The replies continuation that belongs to each comment in this page."""
    tokens: dict[str, str] = {}
    for thread in walk(page, "commentThreadRenderer"):
        if not isinstance(thread, dict):
            continue
        view = thread.get("commentViewModel") or {}
        inner = view.get("commentViewModel") or view
        comment_id = inner.get("commentId")
        if not comment_id:
            continue
        token = _first_token(thread.get("replies") or {})
        if token:
            tokens[comment_id] = token
    return tokens


def _video_title(data: dict) -> str | None:
    for renderer in walk(data, "videoPrimaryInfoRenderer"):
        title = _text_of((renderer or {}).get("title") or {})
        if title:
            return title
    return None


def _video_channel(data: dict) -> str | None:
    for renderer in walk(data, "videoSecondaryInfoRenderer"):
        owner = (renderer or {}).get("owner", {}).get("videoOwnerRenderer", {}) or {}
        title = _text_of(owner.get("title") or {})
        if title:
            return title
    return None


def _comments_count(data: dict) -> str | None:
    for renderer in walk(data, "commentsHeaderRenderer"):
        text = _text_of((renderer or {}).get("countText") or {})
        if text:
            return text.replace("Comments", "").strip() or None
    return None
