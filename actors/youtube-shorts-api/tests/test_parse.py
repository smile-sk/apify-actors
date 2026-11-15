"""Parsing checks on real markup: a Short must be read from the page, not guessed.

Run: python tests/test_parse.py   (exit code 0 means every check ran)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import shorts as sh  # noqa: E402

CHECKS = []
HERE = os.path.dirname(os.path.abspath(__file__))


def case(fn):
    CHECKS.append((fn.__name__, fn))
    return fn


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as handle:
        return json.load(handle)


@case
def test_a_handle_a_url_and_an_id_all_become_a_page():
    """A buyer copies whatever YouTube gave him; the input must not be fussy."""
    assert sh.channel_url("@MrBeast") == "https://www.youtube.com/@MrBeast"
    assert sh.channel_url("MrBeast") == "https://www.youtube.com/@MrBeast"
    assert sh.channel_url("https://www.youtube.com/@MrBeast") == "https://www.youtube.com/@MrBeast"
    assert sh.channel_url("youtube.com/@MrBeast") == "https://youtube.com/@MrBeast"
    assert sh.channel_url("UCX6OQ3DkcsbYNE6H8uQQuVA") == (
        "https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA")
    assert sh.channel_url("   ") == ""


@case
def test_views_are_numbers_only_when_the_page_said_so():
    """'79M views' is a fact; an empty string is not zero."""
    assert sh.views_from_text("79M views") == 79_000_000
    assert sh.views_from_text("17K views") == 17_000
    assert sh.views_from_text("1,234 views") == 1_234
    assert sh.views_from_text("No views") == 0
    assert sh.views_from_text("") is None
    assert sh.views_from_text(None) is None
    assert sh.views_from_text("views") is None


@case
def test_the_limit_never_becomes_zero_by_accident():
    assert sh.clamp_max(None) == 100
    assert sh.clamp_max(0) == 1
    assert sh.clamp_max(-5) == 1
    assert sh.clamp_max(5000) == 1000
    assert sh.clamp_max("abc") == 100


@case
def test_a_real_resolve_answer_gives_the_channel_id():
    """Saved from the live endpoint on 2026-11-12: this is what YouTube really sends."""
    payload = fixture("resolve_channel.json")
    assert sh.channel_id(payload) == "UCX6OQ3DkcsbYNE6H8uQQuVA"


@case
def test_a_real_channel_page_gives_the_title_and_the_shorts_params():
    """The Shorts tab has its own params; hardcoding them would break on a change."""
    payload = fixture("channel_home.json")
    title, params = sh.channel_info(payload)
    assert title == "MrBeast", title
    assert params.startswith("EgZzaG9ydH"), params


@case
def test_no_shorts_tab_is_an_empty_answer_not_a_guess():
    payload = {"contents": {"tabs": [{"tabRenderer": {"title": "Videos",
                                                      "endpoint": {"browseEndpoint": {"params": "AAA"}}}}]}}
    title, params = sh.channel_info(payload)
    assert params == "", params


@case
def test_a_real_shorts_page_gives_entries_and_a_token():
    """Saved from the live endpoint: five real shortsLockupViewModel entries."""
    payload = fixture("shorts_page.json")
    entries, token = sh.parse_page(payload)
    assert len(entries) == 5, len(entries)
    assert token, "the continuation token was not read"


@case
def test_a_real_short_is_read_field_by_field():
    payload = fixture("shorts_page.json")
    entries, _ = sh.parse_page(payload)
    row = sh.parse_short(entries[0], "@MrBeast", "MrBeast", "UCX6OQ3DkcsbYNE6H8uQQuVA", 0)
    assert row["videoId"] == "kPI9Mc35mOg", row["videoId"]
    assert row["url"] == "https://www.youtube.com/shorts/kPI9Mc35mOg", row["url"]
    assert row["title"] == "Trap Door Roulette", row["title"]
    assert row["viewsText"] == "79M views", row["viewsText"]
    assert row["views"] == 79_000_000, row["views"]
    assert row["thumbnailUrl"].startswith("https://i.ytimg.com/vi/"), row["thumbnailUrl"]
    assert row["position"] == 0


@case
def test_an_entry_without_a_video_id_is_not_a_row():
    assert sh.parse_short({"entityId": "shorts-shelf-item-", "overlayMetadata": {}}, "c", "t", "id", 0) is None
    assert sh.parse_short({}, "c", "t", "id", 0) is None


def run_all() -> int:
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
            print("ok   %s" % name)
        except AssertionError as exc:
            failures += 1
            print("FAIL %s -> %s" % (name, exc))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print("ERR  %s -> %s: %s" % (name, type(exc).__name__, exc))
    print("\n%d checks, %d failed" % (len(CHECKS), failures))
    return failures


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
