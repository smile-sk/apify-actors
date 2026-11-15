"""Pure checks on real captured answers: the source, not our belief about it.

Fixtures: tests/fixtures/profile.json and feed_page.json are real answers from
public.api.bsky.app captured on 2026-11-12, truncated to three feed items.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import bsky as bs  # noqa: E402

PASSED = 0
FAILED = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


def fixture(name: str) -> dict:
    return json.loads((HERE / "fixtures" / name).read_text(encoding="utf-8"))


def run_all() -> int:
    profile = fixture("profile.json")
    page = fixture("feed_page.json")

    # the profile answer is real and carries what the actor reports
    info = bs.profile_info(profile)
    check("profile handle is bsky.app", info.get("handle") == "bsky.app")
    check("profile has a did", str(info.get("did", "")).startswith("did:"))
    check("followers is an int", isinstance(info.get("followersCount"), int))
    check("posts count is an int", isinstance(info.get("postsCount"), int))

    # the feed answer is real: three items and a cursor
    entries, cursor = bs.parse_page(page)
    check("three real items parsed", len(entries) == 3)
    check("cursor is present", isinstance(cursor, str) and len(cursor) > 10)

    # one real item becomes one paid row with real numbers
    row = bs.parse_post(entries[0], "bsky.app", info, 0, False, False)
    check("row exists", row is not None)
    row = row or {}
    check("postId is the uri tail", row.get("postId") == str(row.get("uri", "")).rsplit("/", 1)[-1])
    check("text is not empty", bool(str(row.get("text", "")).strip()))
    check("url points at the post", str(row.get("url", "")).startswith("https://bsky.app/profile/"))
    check("createdAt looks like a timestamp", "T" in str(row.get("createdAt", "")))
    check("likes are an int", isinstance(row.get("likeCount"), int))
    check("reposts are an int", isinstance(row.get("repostCount"), int))
    check("author handle present", bool(str(row.get("authorHandle", "")).strip()))
    check("status is OK", row.get("status") == "OK")
    check("row shares something", bs.shares_anything(row))

    # a repost is somebody else's post: off by default, reported when asked
    repost_item = dict(entries[0])
    repost_item["reason"] = {"$type": "app.bsky.feed.defs#reasonRepost"}
    check("repost skipped by default", bs.parse_post(repost_item, "x", info, 0, False, False) is None)
    kept = bs.parse_post(repost_item, "x", info, 0, True, False) or {}
    check("repost kept when asked", kept.get("feedKind") == "repost")

    # a reply is a post to somebody else: off by default too
    reply_item = {"post": json.loads(json.dumps(entries[0]["post"]))}
    reply_item["post"]["record"]["reply"] = {"parent": {"uri": "at://did:plc:x/app.bsky.feed.post/1"}}
    check("reply skipped by default", bs.parse_post(reply_item, "x", info, 0, False, False) is None)
    kept_reply = bs.parse_post(reply_item, "x", info, 0, False, True) or {}
    check("reply kept when asked", kept_reply.get("isReply") is True)

    # a row with neither text nor media is not a product
    check("empty text without media is not a product",
          bs.shares_anything({"text": "   ", "embedType": ""}) is False)
    check("media alone is a product", bs.shares_anything({"text": "", "embedType": "view"}) is True)

    # input handling
    check("handle passes", bs.actor_ref("bsky.app") == "bsky.app")
    check("profile url becomes a handle", bs.actor_ref("https://bsky.app/profile/bsky.app") == "bsky.app")
    check("url with a tab becomes a handle", bs.actor_ref("bsky.app/post/3n22yg6lg2k2f") == "bsky.app")
    check("at sign is dropped", bs.actor_ref("@jay.bsky.team") == "jay.bsky.team")
    check("did passes", bs.actor_ref("did:plc:z72i7hdynmk6r22z27h6tvur") == "did:plc:z72i7hdynmk6r22z27h6tvur")
    check("empty is refused", bs.actor_ref("   ") == "")
    check("plain word is refused", bs.actor_ref("hello") == "")
    check("broken did is refused", bs.actor_ref("did:plc") == "")

    # the ceiling is the measured one: 50 per request, ten requests
    check("default max is 100", bs.clamp_max(None) == 100)
    check("ceiling is 500", bs.clamp_max(10_000) == 500)
    check("zero falls back to default", bs.clamp_max(0) == 100)

    # urls carry the cursor only when there is one
    check("feed url without cursor", "cursor=" not in bs.feed_url("bsky.app"))
    check("feed url with cursor", "cursor=abc" in bs.feed_url("bsky.app", cursor="abc"))
    # B1: the switch must change the QUERY, not only the row filter. The source drops
    # replies itself, so a flag that never reaches the url is a dead switch.
    check("replies off asks the source for posts only",
          "filter=posts_no_replies" in bs.feed_url("bsky.app"))
    check("replies on asks the source for replies too",
          "filter=posts_with_replies" in bs.feed_url("bsky.app", include_replies=True))
    check("the default is still without replies",
          bs.feed_url("bsky.app") == bs.feed_url("bsky.app", include_replies=False))

    print(f"pure checks: {PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print("  FAILED:", name)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(run_all())
