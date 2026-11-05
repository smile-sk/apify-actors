"""Unit tests for src/workday.py - pure logic, no network.

Run: python -m pytest tests -q   (or any runner that calls test_* functions)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import workday


# --------------------------------------------------------------------------- #
# parsing what the buyer pasted
# --------------------------------------------------------------------------- #
def test_full_url():
    t = workday.parse_target("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
    assert t["error"] is None
    assert (t["tenant"], t["wd"], t["site"]) == ("nvidia", "wd5", "NVIDIAExternalCareerSite")
    assert t["host"] == "nvidia.wd5.myworkdayjobs.com"


def test_url_with_locale_prefix():
    t = workday.parse_target("https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site")
    assert t["error"] is None
    assert (t["tenant"], t["wd"], t["site"]) == ("salesforce", "wd12", "External_Career_Site")


def test_url_with_job_path_keeps_site():
    t = workday.parse_target(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/China-Remote/Architect_JR1")
    assert t["error"] is None
    assert t["site"] == "NVIDIAExternalCareerSite"


def test_url_with_query_and_trailing_slash():
    t = workday.parse_target("https://cisco.wd5.myworkdayjobs.com/Cisco_Careers/?locale=en_US")
    assert t["error"] is None
    assert t["site"] == "Cisco_Careers"


def test_host_without_site_is_reported_not_guessed():
    t = workday.parse_target("https://nvidia.wd5.myworkdayjobs.com")
    assert t["error"] == "NEEDS_CAREER_URL"
    assert t["site"] is None
    assert "career" in t["hint"].lower()


def test_not_workday():
    t = workday.parse_target("https://jobs.lever.co/leverdemo")
    assert t["error"] == "NOT_A_WORKDAY_URL"


def test_empty():
    t = workday.parse_target("   ")
    assert t["error"] == "EMPTY_INPUT"


def test_tenant_site_without_wd_is_refused():
    t = workday.parse_target("nvidia:NVIDIAExternalCareerSite")
    assert t["error"] == "NEEDS_WD_NUMBER"
    assert t["tenant"] == "nvidia"


def test_bare_domain_is_not_workday():
    assert workday.parse_target("nvidia.com")["error"] == "NOT_A_WORKDAY_URL"


# --------------------------------------------------------------------------- #
# posting age
# --------------------------------------------------------------------------- #
def test_days_ago_today():
    assert workday.days_ago("Posted Today") == 0


def test_days_ago_yesterday():
    assert workday.days_ago("Posted Yesterday") == 1


def test_days_ago_number():
    assert workday.days_ago("Posted 10 Days Ago") == 10


def test_days_ago_plus():
    assert workday.days_ago("Posted 30+ Days Ago") == 30


def test_days_ago_none():
    assert workday.days_ago(None) is None
    assert workday.days_ago("Posted Recently") is None


# --------------------------------------------------------------------------- #
# remote detection
# --------------------------------------------------------------------------- #
def test_remote_words():
    assert workday.is_remote("Remote - US")
    assert workday.is_remote("Work from home")
    assert workday.is_remote("Anywhere")


def test_not_remote():
    assert not workday.is_remote("Berlin, Germany")
    assert not workday.is_remote("")


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
def _job(title="Data Engineer", location="Berlin, Germany", remote=False):
    return {"jobTitle": title, "location": location, "remote": remote}


def test_keywords_all_words_must_match():
    assert workday.matches(_job(), "data, engineer", "")
    assert not workday.matches(_job(), "data, nurse", "")


def test_keyword_case_insensitive():
    assert workday.matches(_job(title="SENIOR ENGINEER"), "senior", "")


def test_location_filter_is_substring():
    assert workday.matches(_job(), "", "berlin")
    assert not workday.matches(_job(), "", "Munich")


def test_empty_filters_keep_everything():
    assert workday.matches(_job(), "", "")


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def test_normalize_uses_bullet_id_and_builds_url():
    posting = {"title": "Architect", "locationsText": "China, Remote",
               "postedOn": "Posted Today", "externalPath": "/job/China-Remote/Architect_JR2022712",
               "bulletFields": ["JR2022712"]}
    job = workday.normalize(posting, "nvidia", "wd5", "NVIDIAExternalCareerSite")
    assert job["jobId"] == "JR2022712"
    assert job["remote"] is True
    assert job["postedDaysAgo"] == 0
    assert job["url"] == ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
                          "/job/China-Remote/Architect_JR2022712")


def test_normalize_falls_back_to_path_suffix():
    posting = {"title": "Architect", "externalPath": "/job/X/Architect_R-1190258"}
    job = workday.normalize(posting, "unilever", "wd3", "Unilever_Experienced_Professionals")
    assert job["jobId"] == "R-1190258"


def test_normalize_with_detail_prefers_real_fields():
    posting = {"title": "Architect", "locationsText": "2 Locations",
               "externalPath": "/job/X/Architect_JR1", "bulletFields": ["JR1"]}
    detail = {"error": None, "jobDescription": "Build things.", "startDate": "2026-11-04",
              "timeType": "Full time", "jobReqId": "JR1", "country": "CN",
              "externalUrl": "https://nvidia.wd5.myworkdayjobs.com/en-US/Site/job/X/Architect_JR1",
              "location": "China, Remote", "additionalLocations": ["Shanghai"]}
    job = workday.normalize(posting, "nvidia", "wd5", "NVIDIAExternalCareerSite", detail)
    assert job["country"] == "CN"
    assert job["startDate"] == "2026-11-04"
    assert job["location"] == "China, Remote"
    assert job["url"].startswith("https://nvidia.wd5.myworkdayjobs.com/en-US/Site/job/")


# --------------------------------------------------------------------------- #
# listing pagination, with the network replaced
# --------------------------------------------------------------------------- #
def test_list_jobs_paginates_until_cap(monkeypatch=None):
    pages = {
        0: {"status": 200, "error": None, "total": 45, "jobs": [{"externalPath": f"/job/{i}"} for i in range(20)]},
        20: {"status": 200, "error": None, "total": 45, "jobs": [{"externalPath": f"/job/{i}"} for i in range(20, 40)]},
        40: {"status": 200, "error": None, "total": 45, "jobs": [{"externalPath": f"/job/{i}"} for i in range(40, 45)]},
    }
    original = workday.list_page
    workday.list_page = lambda tenant, wd, site, offset, search_text="": pages.get(
        offset, {"status": 200, "error": None, "total": 45, "jobs": []})
    try:
        result = workday.list_jobs("t", "wd1", "s", max_jobs=100, page_delay=0)
    finally:
        workday.list_page = original
    assert result["error"] is None
    assert result["total"] == 45
    assert len(result["jobs"]) == 45
    assert result["truncated"] is False


def test_list_jobs_marks_truncated():
    page = {"status": 200, "error": None, "total": 500, "jobs": [{"externalPath": f"/job/{i}"} for i in range(20)]}
    original = workday.list_page
    workday.list_page = lambda *a, **k: page
    try:
        result = workday.list_jobs("t", "wd1", "s", max_jobs=40, page_delay=0)
    finally:
        workday.list_page = original
    assert len(result["jobs"]) == 40
    assert result["truncated"] is True


def test_list_jobs_reports_http_error():
    original = workday.list_page
    workday.list_page = lambda *a, **k: {"status": 404, "error": "HTTP_404", "jobs": [], "total": None, "detail": "nf"}
    try:
        result = workday.list_jobs("t", "wd1", "s", max_jobs=10, page_delay=0)
    finally:
        workday.list_page = original
    assert result["error"] == "HTTP_404"
    assert result["jobs"] == []

def test_normalize_flattens_country_object():
    posting = {"title": "Architect", "externalPath": "/job/X/A_JR2", "bulletFields": ["JR2"]}
    detail = {"error": None, "country": {"descriptor": "United States of America", "id": "bc33"}}
    job = workday.normalize(posting, "nvidia", "wd5", "Site", detail)
    assert job["country"] == "US"  # object or string, one spelling


def test_normalize_country_string_becomes_a_code():
    posting = {"title": "Architect", "externalPath": "/job/X/A_JR3"}
    detail = {"error": None, "country": "Germany"}
    job = workday.normalize(posting, "nvidia", "wd5", "Site", detail)
    assert job["country"] == "DE"  # a code, not the board's wording

# --------------------------------------------------------------------------- #
# country matching: three Workday spellings, one input
# --------------------------------------------------------------------------- #
def test_country_matches_iso_prefix_in_location():
    job = {"location": "US, CA, Santa Clara", "country": None}
    assert workday.country_matches(job, ["US"])
    assert workday.country_matches(job, ["United States"])


def test_country_matches_name_at_the_end():
    job = {"location": "Sao Paulo, Brazil", "country": None}
    assert workday.country_matches(job, ["Brazil"])
    assert workday.country_matches(job, ["BR"])


def test_country_matches_detail_descriptor():
    job = {"location": "5 Locations", "country": "United States of America"}
    assert workday.country_matches(job, ["United States"])
    assert workday.country_matches(job, ["US"])


def test_country_rejects_other_countries():
    job = {"location": "Munich, Germany", "country": None}
    assert not workday.country_matches(job, ["US"])
    assert not workday.country_matches(job, ["Brazil"])


def test_country_empty_keeps_everything():
    assert workday.country_matches({"location": "Anywhere"}, [])
    assert workday.country_matches({"location": "Anywhere"}, None)


def test_country_code_does_not_match_inside_a_word():
    # 'in' is India, but must not match 'Berlin' or 'Singapore'
    assert not workday.country_matches({"location": "Berlin, Germany"}, ["IN"])
    assert workday.country_matches({"location": "Bengaluru, India"}, ["IN"])
