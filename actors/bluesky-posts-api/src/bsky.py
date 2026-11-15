"""Bluesky posts: pure functions and the two public XRPC calls this actor needs.

What the source really does (measured 2026-11-12 against public.api.bsky.app, no key):

  * app.bsky.actor.getProfile answers 200 without any auth and carries handle, did,
    displayName, description, followersCount, followsCount and postsCount;
  * app.bsky.feed.getAuthorFeed answers 200 with 50 items and a cursor; the cursor is a
    timestamp, so paging is simple and does not need a token exchange;
  * a feed item is {"post": {...}} and the post carries uri, cid, indexedAt, likeCount,
    repostCount, replyCount, quoteCount, bookmarkCount, embed, labels and author;
  * the text lives in post.record.text, the publish moment in post.record.createdAt, the
    indexed moment in post.indexedAt, the languages in post.record.langs;
  * reposts and pins arrive as item.reason ($type app.bsky.feed.defs#reasonRepost and
    #reasonPin) with the original post inside, so they are told apart from own posts;
  * the public search endpoint needs a session (403 without one), so search is NOT
    offered - only a profile's own feed.

No browser, no API key and no proxy are needed, and it answers from a datacenter.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode

API = "https://public.api.bsky.app/xrpc/"
PROFILE = "app.bsky.actor.getProfile"
FEED = "app.bsky.feed.getAuthorFeed"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
MAX_POSTS_DEFAULT = 100
MAX_POSTS_CEILING = 500
FEED_PAGE = 50


def clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def clamp_max(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return MAX_POSTS_DEFAULT
    if number < 1:
        return MAX_POSTS_DEFAULT
    return min(number, MAX_POSTS_CEILING)


def actor_ref(value: str) -> str:
    """A handle, a did or a profile url becomes the actor argument of the XRPC call.

    Returns "" for anything that clearly is not a profile: the caller reports it free.
    """
    text = clean(value)
    if not text:
        return ""
    lowered = text.lower()
    for prefix in ("https://bsky.app/profile/", "http://bsky.app/profile/",
                   "https://www.bsky.app/profile/", "bsky.app/profile/"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not text:
        return ""
    if text.startswith("@"):
        text = text[1:]
    if text.startswith("did:"):
        parts = text.split(":")
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return ""
        return text
    head = text.split("/", 1)[0]
    if "." not in head or any(ch in head for ch in " \t"):
        return ""
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789.-_")
    if any(ch not in allowed for ch in head.lower()):
        return ""
    if head.startswith(".") or head.endswith("."):
        return ""
    return head


def profile_url(ref: str) -> str:
    return API + PROFILE + "?" + urlencode({"actor": ref})


def feed_url(ref: str, cursor: str | None = None, limit: int = FEED_PAGE,
             include_replies: bool = False) -> str:
    # The feed endpoint drops replies AT THE SOURCE unless it is asked otherwise: the
    # row filter downstream can only remove what arrived, never bring back what the
    # server did not send. Measured 2026-11-12: 0 replies in 50 with posts_no_replies,
    # 22 (jay.bsky.team) and 18 (bsky.app) with posts_with_replies.
    kind = "posts_with_replies" if include_replies else "posts_no_replies"
    query = {"actor": ref, "limit": str(max(1, min(limit, 100))), "filter": kind}
    if cursor:
        query["cursor"] = cursor
    return API + FEED + "?" + urlencode(query)


def build_headers() -> dict[str, str]:
    return {"User-Agent": UA, "Accept": "application/json"}


def post_json(url: str, timeout: float = 20.0) -> dict:
    """One GET with a hard timeout to a fixed public host.

    The url is built here from a validated handle, never taken from the input, so the
    caller cannot point this at a private address. Redirects are followed by urllib,
    which is safe for the same reason: the destination cannot be chosen by the buyer.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=build_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read()[:200].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(f"HTTP {exc.code} {detail}".strip()) from exc
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("the answer was not json") from exc


def profile_info(payload: dict) -> dict:
    """The parts of a profile this actor reports next to every post."""
    if not isinstance(payload, dict):
        return {}
    return {
        "handle": clean(payload.get("handle")),
        "did": clean(payload.get("did")),
        "displayName": clean(payload.get("displayName")),
        "description": clean(payload.get("description")),
        "followersCount": payload.get("followersCount"),
        "followsCount": payload.get("followsCount"),
        "postsCount": payload.get("postsCount"),
    }


def _reason_kind(item: dict) -> str:
    reason = item.get("reason")
    if not isinstance(reason, dict):
        return ""
    kind = clean(reason.get("$type"))
    if kind.endswith("#reasonRepost"):
        return "repost"
    if kind.endswith("#reasonPin"):
        return "pin"
    return kind.split("#")[-1] if kind else ""


def _counts(post: dict) -> dict:
    out = {}
    for field in ("likeCount", "repostCount", "replyCount", "quoteCount", "bookmarkCount"):
        value = post.get(field)
        out[field] = int(value) if isinstance(value, int) else 0
    return out


def _languages(post: dict) -> list[str]:
    record = post.get("record") or {}
    langs = record.get("langs")
    if isinstance(langs, list):
        return [clean(item) for item in langs if clean(item)]
    return []


def _is_reply(post: dict) -> bool:
    record = post.get("record") or {}
    return isinstance(record.get("reply"), dict)


def _post_id(post: dict) -> str:
    uri = clean(post.get("uri"))
    if not uri:
        return ""
    return uri.rsplit("/", 1)[-1]


def parse_page(payload: dict) -> tuple[list[dict], str | None]:
    """Feed entries and the next cursor. Bad entries are skipped, never invented."""
    if not isinstance(payload, dict):
        return [], None
    feed = payload.get("feed")
    if not isinstance(feed, list):
        return [], None
    cursor = clean(payload.get("cursor")) or None
    entries = [item for item in feed if isinstance(item, dict) and isinstance(item.get("post"), dict)]
    return entries, cursor


def parse_post(item: dict, actor_input: str, profile: dict, index: int,
               include_reposts: bool, include_replies: bool) -> dict | None:
    """One feed item becomes one row, or None when the buyer asked not to have it."""
    post = item.get("post") or {}
    post_id = _post_id(post)
    if not post_id:
        return None
    kind = _reason_kind(item)
    if kind == "repost" and not include_reposts:
        return None
    if _is_reply(post) and not include_replies:
        return None
    record = post.get("record") or {}
    author = post.get("author") or {}
    embed = post.get("embed") or {}
    row = {
        "actorInput": actor_input,
        "actorIndex": index,
        "postId": post_id,
        "uri": clean(post.get("uri")),
        "url": "https://bsky.app/profile/" + (clean(author.get("handle")) or clean(author.get("did"))) + "/post/" + post_id,
        "text": clean(record.get("text")),
        "createdAt": clean(record.get("createdAt")),
        "indexedAt": clean(post.get("indexedAt")),
        "langs": _languages(post),
        "isReply": _is_reply(post),
        "feedKind": kind or "post",
        "authorHandle": clean(author.get("handle")),
        "authorDid": clean(author.get("did")),
        "authorName": clean(author.get("displayName")),
        "authorAvatar": clean(author.get("avatar")),
        "profileHandle": profile.get("handle", ""),
        "profileDid": profile.get("did", ""),
        "profileName": profile.get("displayName", ""),
        "followersCount": profile.get("followersCount"),
        "postsCount": profile.get("postsCount"),
        "embedType": clean(embed.get("$type")).split("#")[-1] if embed else "",
        "status": "OK",
        "billable": True,
    }
    row.update(_counts(post))
    return row


def shares_anything(row: dict) -> bool:
    """A row with no text and no embed is not a product: a like-only feed item is not a post."""
    return bool(clean(row.get("text"))) or bool(clean(row.get("embedType")))
