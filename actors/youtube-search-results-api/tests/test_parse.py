"""Parsing checks on the real markup: a result must be read, not guessed.

Run: python tests/test_parse.py   (exit code 0 means every check ran)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import yts  # noqa: E402

CHECKS = []


def case(fn):
    CHECKS.append((fn.__name__, fn))
    return fn


@case
def test_the_filter_token_is_the_one_the_site_uses():
    """The endpoint wants one base64 protobuf; joining tokens would be a different message."""
    assert yts.search_params("relevance", "any", "any") == ""
    assert yts.search_params("views", "any", "any") == "CAM="
    assert yts.search_params("relevance", "any", "short") == "EgIYAQ=="
    assert yts.search_params("relevance", "any", "long") == "EgIYAg=="
    assert yts.search_params("relevance", "today", "any") == "EgIIAg=="
    assert yts.search_params("relevance", "week", "any") == "EgIIAw=="


@case
def test_only_the_sorts_that_really_work_are_offered():
    """Measured 2026-11-12: date and rating sorting return the same page, so they are absent."""
    assert yts.SORT_VALUES == ("relevance", "views")
    assert "date" not in yts.SORT_VALUES
    assert "rating" not in yts.SORT_VALUES


@case
def test_duration_is_read_as_seconds():
    assert yts.duration_seconds("12:00:00") == 43200
    assert yts.duration_seconds("9:41") == 581
    assert yts.duration_seconds("0:13") == 13
    assert yts.duration_seconds("LIVE") is None
    assert yts.duration_seconds(None) is None


@case
def test_view_count_is_read_and_unknown_is_not_zero():
    assert yts.view_count("13,456,363 views") == 13456363
    assert yts.view_count("1.2M views") == 1200000
    assert yts.view_count("804 views") == 804
    assert yts.view_count("No views") == 0
    assert yts.view_count("") is None
    assert yts.view_count(None) is None


@case
def test_age_in_days_is_a_lower_bound_not_a_date():
    assert yts.days_ago("2 years ago") == 730
    assert yts.days_ago("3 weeks ago") == 21
    assert yts.days_ago("19 minutes ago") == 0
    assert yts.days_ago("") is None


@case
def test_a_result_without_an_id_is_not_a_result():
    assert yts.parse_video({"title": {"simpleText": "x"}}, "q") is None
    assert yts.parse_video({}, "q") is None


@case
def test_the_saved_page_parses_into_full_rows():
    here = os.path.dirname(os.path.abspath(__file__))
    payload = json.load(open(os.path.join(here, "fixtures", "search_page.json"), encoding="utf-8"))
    videos, token = yts.parse_page(payload)
    assert len(videos) >= 8, len(videos)
    assert token, "the page carried no continuation token"
    rows = [yts.parse_video(v, "python tutorial") for v in videos]
    rows = [r for r in rows if r]
    assert len(rows) == len(videos)
    for row in rows:
        assert row["videoId"], row
        assert row["url"] == "https://www.youtube.com/watch?v=" + row["videoId"]
        assert row["title"], row
        assert row["channel"], row
        assert isinstance(row["views"], int), row
        assert isinstance(row["durationSeconds"], int), row
        assert row["query"] == "python tutorial"
    assert any(r["publishedDaysAgo"] is not None for r in rows)


@case
def test_clamp_never_goes_above_the_ceiling():
    assert yts.clamp_results(None) == 50
    assert yts.clamp_results(0) == 1
    assert yts.clamp_results(5000) == 500
    assert yts.clamp_results("abc") == 50


@case
def test_the_shorts_shelf_is_counted_not_dropped():
    """Measured 2026-11-12: a search for "cats" carried 40 Shorts beside 9 videos.

    Shorts are rendered as shortsLockupViewModel, not as videoRenderer, so they can
    never become rows. The summary has to count them, otherwise a buyer reads
    "nothing was found" while most of the page was skipped.
    """
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures",
                         "search_page_with_shorts.json")
    payload = json.load(open(path, encoding="utf-8"))
    videos, _token = yts.parse_page(payload)
    assert yts.count_shorts(payload) == 3, yts.count_shorts(payload)
    assert len(videos) == 2, len(videos)
    assert all(v.get("videoId") for v in videos)


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
