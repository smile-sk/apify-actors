"""Checks for the LinkedIn rules that cost money or tell the truth.

Run: python tests/test_linkedin.py   (exit code 0 means every check ran)
These are pure: no browser, no network, no run.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import linkedin as li          # noqa: E402
from src import main as M               # noqa: E402

CHECKS: list[tuple[str, callable]] = []


def case(fn):
    CHECKS.append((fn.__name__, fn))
    return fn


# --- addresses -------------------------------------------------------------

@case
def test_search_url_without_filters():
    url = li.search_url("python developer")
    assert url.startswith("https://www.linkedin.com/jobs/search/?keywords="), url
    assert "%20" in url and "&location=" not in url
    assert "f_TPR" not in url and "f_E" not in url and "f_WT" not in url


@case
def test_search_url_carries_every_filter_it_was_given():
    url = li.search_url("python developer", "United States", date_posted="pastWeek",
                        experience="mid-senior", remote="remote")
    assert "&location=United%20States" in url
    assert "&f_TPR=r604800" in url
    assert "&f_E=4" in url
    assert "&f_WT=2" in url


@case
def test_search_url_encodes_non_ascii():
    url = li.search_url("developpeur confirme")
    assert "%20" in url and " " not in url


@case
def test_job_id_is_taken_from_the_link():
    link = "https://www.linkedin.com/jobs/view/senior-software-engineer-python-at-venmo-4465379983?position=1"
    assert li.job_id_from_url(link) == "senior-software-engineer-python-at-venmo-4465379983"


@case
def test_garbage_link_gives_no_id():
    assert li.job_id_from_url("https://www.linkedin.com/jobs/search/") is None
    assert li.job_id_from_url(None) is None


@case
def test_canonical_url_drops_tracking():
    assert li.canonical_url("abc-123") == "https://www.linkedin.com/jobs/view/abc-123"


# --- lenses, the depth lever that was measured ------------------------------

@case
def test_default_lenses_are_three_and_ordered():
    assert li.lenses("any") == ["any", "pastWeek", "past24h"]


@case
def test_a_chosen_window_is_honoured_alone():
    assert li.lenses("pastMonth") == ["pastMonth"]


@case
def test_an_unknown_window_does_not_silently_become_one_lens():
    assert li.lenses("last tuesday") == ["any", "pastWeek", "past24h"]


@case
def test_depth_clamp_is_three_lenses_of_sixty():
    assert li.MAX_JOBS_CLAMP == 180


# --- values ----------------------------------------------------------------

@case
def test_clean_collapses_whitespace_and_empties():
    assert li.clean("  Senior   Engineer \n") == "Senior Engineer"
    assert li.clean("") is None
    assert li.clean(None) is None


@case
def test_posted_hours_ago_becomes_a_moment():
    value = li.parse_posted("2 hours ago")
    assert value and value.endswith("Z")
    moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - moment).total_seconds() / 3600.0
    assert 1.9 <= age <= 2.1, age


@case
def test_posted_minutes_and_reposted_days():
    assert li.parse_posted("30 minutes ago")
    assert li.parse_posted("Reposted 3 days ago")


@case
def test_posted_garbage_is_not_invented():
    assert li.parse_posted("Posted today") is None
    assert li.parse_posted(None) is None


@case
def test_the_time_element_is_a_date_not_a_moment():
    assert li.posted_from_date("2026-11-12") == "2026-11-12"
    assert li.posted_from_date("12.11.2026") is None


# --- the sold row ----------------------------------------------------------

@case
def test_job_row_merges_card_and_detail():
    card = {"id": "job-1", "title": "Card title", "company": "Venmo",
            "location": "Austin, TX", "posted": "2 hours ago", "date": "2026-11-12"}
    detail = {"title": "Senior Software Engineer - Python", "company": "Venmo",
              "location": "Venmo  Austin, TX", "description": "x" * 500,
              "seniorityLevel": "Mid-Senior level", "employmentType": "Full-time",
              "jobFunction": "Engineering", "industries": "Financial Services",
              "applicants": "Over 200 applicants", "companyUrl": "https://www.linkedin.com/company/venmo",
              "applyUrl": "https://www.linkedin.com/jobs/view/job-1/apply"}
    row = li.job_row(card, detail)
    assert row["jobId"] == "job-1"
    assert row["title"] == "Senior Software Engineer - Python"  # detail wins over the card
    assert row["descriptionCharacters"] == 500
    assert row["employmentType"] == "Full-time"
    assert row["applicants"] == "Over 200 applicants"
    assert row["postedAt"] and row["postedAt"].endswith("Z")


@case
def test_job_row_without_detail_still_carries_the_card():
    card = {"id": "job-2", "title": "Backend Developer", "company": "Acme",
            "location": "Remote", "posted": "1 day ago", "date": None}
    row = li.job_row(card, None)
    assert row["title"] == "Backend Developer"
    assert row["description"] is None
    assert row["descriptionCharacters"] == 0
    assert row["seniorityLevel"] is None


# --- walls -----------------------------------------------------------------

@case
def test_a_banner_on_a_page_with_cards_is_not_a_wall():
    # Measured: every guest search carries a sign-in banner, and still has 60 cards.
    assert li.text_is_wall("Python Jobs | LinkedIn", "Sign in to view more", has_cards=True) is None


@case
def test_no_cards_and_a_sign_in_wall_is_a_wall():
    assert li.text_is_wall("Sign In | LinkedIn", "join now to see all the jobs",
                           has_cards=False) is not None


@case
def test_captcha_is_a_wall_even_with_cards():
    assert li.text_is_wall("LinkedIn", "please solve this captcha", has_cards=True) is not None


@case
def test_a_clean_page_is_not_a_wall():
    assert li.text_is_wall("Python Jobs", "60 jobs found", has_cards=True) is None


# --- price -----------------------------------------------------------------

class FakeCharging:
    def __init__(self, info=None, boom=None):
        self._info = info
        self._boom = boom

    def get_pricing_info(self):
        if self._boom:
            raise self._boom
        return self._info


class Info:
    def __init__(self, is_ppe, prices):
        self.is_pay_per_event = is_ppe
        self.per_event_prices = prices


@case
def test_price_state_true_when_the_event_is_priced():
    result, note = M.price_state(FakeCharging(Info(True, {M.EVENT_NAME: object()})))
    assert result is True, (result, note)


@case
def test_price_state_false_when_not_sold_per_event():
    result, note = M.price_state(FakeCharging(Info(False, {})))
    assert result is False and "not sold per event" in note, (result, note)


@case
def test_price_state_false_when_another_event_is_priced():
    result, note = M.price_state(FakeCharging(Info(True, {"other-event": object()})))
    assert result is False and M.EVENT_NAME in note, (result, note)


@case
def test_price_state_unknown_when_the_read_fails():
    result, note = M.price_state(FakeCharging(boom=RuntimeError("nope")))
    assert result is None and "pricing read failed" in note, (result, note)


@case
def test_price_state_unknown_when_the_answer_is_empty():
    result, note = M.price_state(FakeCharging(None))
    assert result is None, (result, note)


# --- time budget -----------------------------------------------------------

@case
def test_nav_budget_never_exceeds_the_constant():
    assert M.nav_budget(None) == M.NAV_TIMEOUT_S


@case
def test_nav_budget_shrinks_with_the_run():
    import time
    deadline = time.time() + 12.0
    assert M.nav_budget(deadline) < M.NAV_TIMEOUT_S
    assert M.nav_budget(deadline) >= 5.0


@case
def test_a_place_is_only_a_place():
    # The job subline carries company, place, age and applicants in one string.
    assert li.clean_place("Tata Consultancy Services Morrisville, NC 1 hour ago 68 applicants "
                          "See who Tata Consultancy Services has hired for this role") == \
        "Tata Consultancy Services Morrisville, NC"
    assert li.clean_place("Austin, TX") == "Austin, TX"
    assert li.clean_place("Berlin, Germany 3 days ago Over 200 applicants") == "Berlin, Germany"
    assert li.clean_place("   ") is None


@case
def test_a_company_name_does_not_stay_inside_the_place():
    row = li.job_row({"id": "9", "title": "Python Dev", "company": "X", "location": "Austin, TX"},
                     {"title": "Python Dev", "company": "Tata Consultancy Services",
                      "location": "Tata Consultancy Services Morrisville, NC 1 hour ago",
                      "description": "x" * 600})
    assert row["company"] == "Tata Consultancy Services", row["company"]
    assert row["location"] == "Morrisville, NC", row["location"]


@case
def test_a_job_page_that_turns_into_the_sign_in_page_is_a_wall():
    # Live: a job page answered with the "Join LinkedIn" page and an empty description,
    # and it was sold as a row. A sign-up page is not a job.
    assert li.text_is_wall("Join LinkedIn", "Join now") == "sign-in wall"
    assert li.text_is_wall("Sign Up | LinkedIn", "text") == "sign-in wall"
    assert li.text_is_wall("Jobs | LinkedIn", "60 jobs found", True) is None


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
