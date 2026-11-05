"""Regression tests for the four billing blockers found at QA 0.1.4.

Every test here exists because a live run charged a buyer for something that
should never have been charged: another country's jobs, the same job twice, a
description that never arrived, a board silently dropped.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import workday


# --------------------------------------------------------------------------- #
# B1: a US state code is not a country
# --------------------------------------------------------------------------- #
def test_california_is_not_canada():
    job = {"location": "US, CA, Santa Clara", "country": None}
    assert not workday.country_matches(job, ["Canada"])
    assert not workday.country_matches(job, ["CA"])
    assert workday.country_matches(job, ["US"])


def test_delaware_is_not_germany():
    job = {"location": "Wilmington, DE, United States", "country": None}
    assert not workday.country_matches(job, ["Germany"])
    assert not workday.country_matches(job, ["DE"])
    assert workday.country_matches(job, ["United States"])


def test_colorado_is_not_colombia():
    job = {"location": "Denver, CO, United States", "country": None}
    assert not workday.country_matches(job, ["Colombia"])
    assert not workday.country_matches(job, ["CO"])


def test_indiana_is_not_india():
    job = {"location": "US, IN, Indianapolis", "country": None}
    assert not workday.country_matches(job, ["India"])


def test_country_name_at_the_end_still_works():
    job = {"location": "Sao Paulo, Brazil", "country": None}
    assert workday.country_matches(job, ["Brazil"])
    assert workday.country_matches(job, ["BR"])


def test_unknown_country_name_at_the_end_works():
    job = {"location": "Almaty, Kazakhstan", "country": None}
    assert workday.country_matches(job, ["Kazakhstan"])


def test_country_without_a_name_is_not_guessed():
    # a bare city never becomes a country
    assert not workday.country_matches({"location": "Berlin", "country": None}, ["Germany"])


def test_descriptor_matches_a_code_and_a_name():
    job = {"location": "5 Locations", "country": "United States of America"}
    assert workday.country_matches(job, ["US"])
    assert workday.country_matches(job, ["United States"])


# --------------------------------------------------------------------------- #
# B2: one board pasted twice is read once and billed once
# --------------------------------------------------------------------------- #
def test_same_board_two_urls_collapses():
    prepared = workday.prepare_targets([
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite",
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/",
    ])
    assert len(prepared["pairs"]) == 1
    assert len(prepared["dropped"]) == 2


def test_different_sites_on_the_same_host_are_kept():
    prepared = workday.prepare_targets([
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAUniversity",
    ])
    assert len(prepared["pairs"]) == 2


def test_job_key_is_board_plus_job_id():
    a = {"tenant": "NVIDIA", "site": "Site", "jobId": "JR1", "externalPath": "/job/x"}
    b = {"tenant": "nvidia", "site": "site", "jobId": "JR1", "externalPath": "/job/y"}
    c = {"tenant": "nvidia", "site": "site", "jobId": "JR2", "externalPath": "/job/x"}
    assert workday.job_key(a) == workday.job_key(b)
    assert workday.job_key(a) != workday.job_key(c)


# --------------------------------------------------------------------------- #
# B3: includeDescription actually delivers a description
# --------------------------------------------------------------------------- #
def test_normalize_always_has_the_description_field():
    posting = {"title": "Architect", "externalPath": "/job/X/A_JR1", "bulletFields": ["JR1"]}
    job = workday.normalize(posting, "nvidia", "wd5", "Site")
    assert "jobDescription" in job
    assert job["jobDescription"] is None


def test_apply_detail_fills_the_description():
    posting = {"title": "Architect", "externalPath": "/job/X/A_JR1", "bulletFields": ["JR1"]}
    job = workday.normalize(posting, "nvidia", "wd5", "Site")
    detail = {"error": None, "jobDescription": "<p>Build things.</p>", "startDate": "2026-11-04",
              "timeType": "Full time", "country": {"descriptor": "United States of America"}}
    workday.apply_detail(job, detail)
    assert job["jobDescription"] == "<p>Build things.</p>"
    assert job["startDate"] == "2026-11-04"
    assert job["country"] == "US"  # a code, not a name: one spelling for one country


def test_apply_detail_ignores_a_failed_detail():
    posting = {"title": "Architect", "externalPath": "/job/X/A_JR1", "bulletFields": ["JR1"]}
    job = workday.normalize(posting, "nvidia", "wd5", "Site")
    workday.apply_detail(job, {"error": "HTTP_404"})
    assert job["jobDescription"] is None


# --------------------------------------------------------------------------- #
# keywords go to Workday's own search, so a match deep in a big board is found
# --------------------------------------------------------------------------- #
def test_keywords_are_passed_to_the_listing_request():
    seen = {}

    def fake_list_page(tenant, wd, site, offset, search_text=""):
        seen["search_text"] = search_text
        return {"status": 200, "error": None, "total": 1,
                "jobs": [{"externalPath": "/job/1"}]}

    original = workday.list_page
    workday.list_page = fake_list_page
    try:
        workday.list_jobs("t", "wd1", "s", max_jobs=20, page_delay=0, search_text="engineer")
    finally:
        workday.list_page = original
    assert seen["search_text"] == "engineer"


def test_list_jobs_keeps_partial_jobs_when_a_later_page_fails():
    calls = {"n": 0}

    def flaky(tenant, wd, site, offset, search_text=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": 200, "error": None, "total": 100,
                    "jobs": [{"externalPath": f"/job/{i}"} for i in range(20)]}
        return {"status": 500, "error": "HTTP_500", "jobs": [], "total": None, "detail": "boom"}

    original = workday.list_page
    workday.list_page = flaky
    try:
        result = workday.list_jobs("t", "wd1", "s", max_jobs=100, page_delay=0)
    finally:
        workday.list_page = original
    assert result["error"] == "HTTP_500"
    assert result.get("partial") is True
    assert len(result["jobs"]) == 20


# --------------------------------------------------------------------------- #
# the cache key knows the cluster and the payload version
# --------------------------------------------------------------------------- #
def test_cache_key_changes_with_wd_and_version():
    a = workday.cache_key({"tenant": "nvidia", "wd": "wd5", "site": "S"}, False, 200, "", "")
    b = workday.cache_key({"tenant": "nvidia", "wd": "wd1", "site": "S"}, False, 200, "", "")
    assert a != b
    assert a.startswith("wd_")
    assert workday.CACHE_VERSION == "6"


# --------------------------------------------------------------------------- #
# 0.1.6: every location shape Workday actually writes
#
# At 0.1.5 a "Canada" filter returned California and a "US" filter returned
# nothing on a board whose locations are written "Plano, TX". Both are the same
# root: only one shape was parsed. One guard per shape now.
# --------------------------------------------------------------------------- #
def test_city_state_code_is_the_united_states():
    job = {"location": "Plano, TX", "country": None}
    assert workday.country_matches(job, ["US"])
    assert workday.country_matches(job, ["United States"])
    assert not workday.country_matches(job, ["Canada"])


def test_city_california_is_not_canada():
    # "CA" is California and Canada both: the string alone proves nothing, so
    # the row carries no country until its own detail record says which one.
    job = {"location": "San Francisco, CA", "country": None}
    assert workday.needs_country_probe(job)
    workday.apply_detail(job, {"error": None, "country": {"descriptor": "United States of America"}})
    assert workday.country_matches(job, ["US"])
    assert not workday.country_matches(job, ["Canada"])
    assert not workday.country_matches(job, ["CA"])


def test_city_full_state_name_is_the_united_states():
    assert workday.country_matches({"location": "San Francisco, California"}, ["US"])
    assert workday.country_matches({"location": "Austin, Texas"}, ["United States"])


def test_canadian_province_is_canada():
    assert workday.country_matches({"location": "Toronto, ON"}, ["Canada"])
    assert workday.country_matches({"location": "Vancouver, British Columbia"}, ["Canada"])
    assert not workday.country_matches({"location": "Toronto, ON"}, ["US"])


def test_country_then_dash_then_city():
    assert workday.country_matches({"location": "United Kingdom - London"}, ["UK"])
    assert workday.country_matches({"location": "United Kingdom - London"}, ["United Kingdom"])
    assert not workday.country_matches({"location": "United Kingdom - London"}, ["US"])


def test_city_then_dash_then_country():
    assert workday.country_matches({"location": "London - United Kingdom"}, ["UK"])


def test_country_name_at_the_tail():
    assert workday.country_matches({"location": "Sao Paulo, Brazil"}, ["Brazil"])
    assert workday.country_matches({"location": "Almaty, Kazakhstan"}, ["Kazakhstan"])


def test_remote_marker_does_not_hide_the_country():
    assert workday.country_matches({"location": "Remote - US"}, ["US"])
    assert workday.country_matches({"location": "US - Remote"}, ["US"])


def test_delaware_city_state_is_not_germany():
    job = {"location": "Wilmington, DE", "country": None}
    assert workday.needs_country_probe(job)
    workday.apply_detail(job, {"error": None, "country": {"descriptor": "United States of America"}})
    assert workday.country_matches(job, ["US"])
    assert not workday.country_matches(job, ["Germany"])
    assert not workday.country_matches(job, ["DE"])


def test_a_bare_city_names_no_country():
    assert not workday.country_matches({"location": "Berlin"}, ["Germany"])
    assert workday.needs_country_probe({"location": "Berlin"})
    assert not workday.country_matches({"location": "5 Locations"}, ["US"])
    assert workday.needs_country_probe({"location": "5 Locations"})


def test_a_named_location_is_never_probed():
    assert not workday.needs_country_probe({"location": "Plano, TX"})
    assert not workday.needs_country_probe({"location": "Sao Paulo, Brazil"})
    assert not workday.needs_country_probe({"location": "5 Locations",
                                            "country": "United States of America"})


def test_the_probe_resolves_the_country_from_the_detail():
    job = {"location": "22 Locations", "country": None}
    assert not workday.country_matches(job, ["US"])
    workday.apply_detail(job, {"error": None, "country": {"descriptor": "United States of America"}})
    assert workday.country_matches(job, ["US"])
    assert not workday.country_matches(job, ["Canada"])


def test_additional_locations_count_as_well():
    job = {"location": "5 Locations", "country": None,
           "additionalLocations": ["Toronto, ON", "Remote - US"]}
    assert workday.country_matches(job, ["Canada"])
    assert workday.country_matches(job, ["US"])
    assert not workday.needs_country_probe(job)


# --------------------------------------------------------------------------- #
# 0.1.7: a tail that reads two ways is settled by the job's own detail record,
# never by table order. Live at 0.1.6: "Bangalore, In" was delivered as Indiana
# on a US filter (and billed), while the same rows vanished on an India filter.
# --------------------------------------------------------------------------- #
def _with_detail(location, descriptor):
    job = {"location": location, "country": None}
    workday.apply_detail(job, {"error": None, "country": {"descriptor": descriptor}})
    return job


def test_ambiguous_tails_are_probed_not_guessed():
    for location in ("Bangalore, In", "San Francisco, CA", "Wilmington, DE",
                     "Denver, CO", "Tel Aviv, IL", "Bratislava, SK", "Amsterdam, NL",
                     "Buenos Aires, AR", "Fort Wayne, IN"):
        assert workday.location_country(location) is None, location
        assert workday.needs_country_probe({"location": location}), location


def test_bangalore_india_is_not_indiana():
    job = _with_detail("Bangalore, In", "India")
    assert workday.country_matches(job, ["India"])
    assert not workday.country_matches(job, ["US"])


def test_california_from_detail_is_the_united_states():
    job = _with_detail("San Francisco, CA", "United States of America")
    assert workday.country_matches(job, ["US"])
    assert not workday.country_matches(job, ["Canada"])


def test_bratislava_is_slovakia():
    job = _with_detail("Bratislava, SK", "Slovakia")
    assert workday.country_matches(job, ["Slovakia"])
    assert not workday.country_matches(job, ["Canada"])


def test_bogota_is_colombia():
    job = _with_detail("Bogota, CO", "Colombia")
    assert workday.country_matches(job, ["Colombia"])
    assert not workday.country_matches(job, ["US"])


def test_unambiguous_tails_still_decide_alone():
    assert workday.location_country("Plano, TX") == "united states"
    assert workday.location_country("Toronto, ON") == "canada"
    assert workday.location_country("Sao Paulo, Brazil") == "brazil"
    assert not workday.needs_country_probe({"location": "Plano, TX"})


def test_an_unproven_row_matches_nothing():
    job = {"location": "Bangalore, In", "country": None}
    assert not workday.country_matches(job, ["US"])
    assert not workday.country_matches(job, ["India"])


def test_the_country_field_is_filled_from_the_location_when_it_is_clear():
    row = workday.normalize({"title": "Engineer", "locationsText": "Plano, TX",
                             "externalPath": "/job/Plano-TX/1"}, "acme", "wd1", "Site")
    assert row["country"] == "US"
    row = workday.normalize({"title": "Engineer", "locationsText": "5 Locations",
                             "externalPath": "/job/5-Locations/2"}, "acme", "wd1", "Site")
    assert row["country"] is None
