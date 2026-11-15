"""Parsing checks on real markup: a video must be read from the page, not guessed.

Run: python tests/test_parse.py   (exit code 0 means every check ran)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import channel as ch  # noqa: E402

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
    assert ch.channel_url("@MrBeast") == "https://www.youtube.com/@MrBeast"
    assert ch.channel_url("MrBeast") == "https://www.youtube.com/@MrBeast"
    assert ch.channel_url("https://www.youtube.com/@MrBeast") == "https://www.youtube.com/@MrBeast"
    assert ch.channel_url("UCX6OQ3DkcsbYNE6H8uQQuVA") == (
        "https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA")
    assert ch.channel_url("   ") == ""


@case
def test_views_are_numbers_only_when_the_page_said_so():
    """'92M views' is a fact; an empty string is not zero."""
    assert ch.number_from_text("92M views") == 92_000_000
    assert ch.number_from_text("1,234 views") == 1_234
    assert ch.number_from_text("30.7M subscribers") == 30_700_000
    assert ch.number_from_text("No views") == 0
    assert ch.number_from_text("") is None
    assert ch.number_from_text(None) is None


@case
def test_a_duration_is_seconds_or_nothing():
    assert ch.duration_seconds("22:28") == 1_348
    assert ch.duration_seconds("1:02:03") == 3_723
    assert ch.duration_seconds("LIVE") is None
    assert ch.duration_seconds("") is None


@case
def test_an_age_is_a_lower_bound_in_days():
    assert ch.age_days("11 days ago") == 11
    assert ch.age_days("2 years ago") == 730
    assert ch.age_days("3 weeks ago") == 21
    assert ch.age_days("") is None
    assert ch.age_days("streamed 2 days ago") == 2


@case
def test_the_limit_never_becomes_zero_by_accident():
    assert ch.clamp_max(None) == 50
    assert ch.clamp_max(0) == 1
    assert ch.clamp_max(5000) == 1000
    assert ch.clamp_max("abc") == 50


@case
def test_a_real_resolve_answer_gives_the_channel_id():
    """Saved from the live endpoint on 2026-11-12."""
    payload = fixture("resolve_channel.json")
    assert ch.channel_id(payload) == "UCX6OQ3DkcsbYNE6H8uQQuVA"


@case
def test_a_real_channel_page_gives_title_subscribers_and_tabs():
    """The Videos tab has its own params; hardcoding them would break on a change."""
    payload = fixture("channel_home.json")
    title, subscribers, tabs = ch.channel_info(payload)
    assert title == "MrBeast", title
    assert subscribers.startswith("30.7M subscribers"), subscribers
    assert "videos" in tabs and tabs["videos"].startswith("EgZ2aWRlb3"), tabs
    assert "shorts" in tabs


@case
def test_a_page_without_a_videos_tab_is_an_empty_answer_not_a_guess():
    payload = {"contents": {"tabs": [{"tabRenderer": {"title": "Shorts",
                                                      "endpoint": {"browseEndpoint": {"params": "AAA"}}}}]}}
    _title, _subs, tabs = ch.channel_info(payload)
    assert "videos" not in tabs, tabs


@case
def test_a_real_videos_page_gives_entries_and_a_token():
    """Saved from the live endpoint: five real lockupViewModel entries."""
    payload = fixture("videos_page.json")
    entries, token = ch.parse_page(payload)
    assert len(entries) == 5, len(entries)
    assert token, "the continuation token was not read"


@case
def test_a_real_video_is_read_field_by_field():
    payload = fixture("videos_page.json")
    entries, _ = ch.parse_page(payload)
    row = ch.parse_video(entries[0], "@MrBeast", "MrBeast", "UCX6OQ3DkcsbYNE6H8uQQuVA",
                         "30.7M subscribers", 0)
    assert row["videoId"] == "_c2e1NcI3Sc", row["videoId"]
    assert row["url"] == "https://www.youtube.com/watch?v=_c2e1NcI3Sc", row["url"]
    assert row["title"] == "Beat Professional Athlete, Win $100,000", row["title"]
    assert row["views"] == 92_000_000, row["views"]
    assert row["viewsText"] == "92M views", row["viewsText"]
    assert row["ageText"] == "11 days ago", row["ageText"]
    assert row["ageDays"] == 11, row["ageDays"]
    assert row["durationSeconds"] == 1_348, row["durationSeconds"]
    assert row["channelSubscribersText"] == "30.7M subscribers"
    assert row["thumbnailUrl"].startswith("https://i.ytimg.com/vi/"), row["thumbnailUrl"]


@case
def test_a_lockup_that_is_not_a_video_is_not_a_row():
    assert ch.parse_video({"contentId": "", "contentType": "LOCKUP_CONTENT_TYPE_VIDEO"}, "c", "t", "id", "", 0) is None
    assert ch.parse_video({"contentId": "x", "contentType": "LOCKUP_CONTENT_TYPE_PLAYLIST"}, "c", "t", "id", "", 0) is None


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
