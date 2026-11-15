"""Checks for the board reader: identifiers, pagination, money-free parsing and filters.

The fixtures in tests/fixtures are real answers copied from the live API on 2026-11-07
(companies/Ubisoft2, 298 open postings), not shapes I typed from memory. A fake that
repeats my belief about an API is what hid a dead key in another Actor, so the shapes
here are pinned to what the service really sent.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import sc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as handle:
        return json.load(handle)


class FakeClient:
    """Answers the same three methods the real client has, in the order given."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = 0

    def get_json(self, url):
        self.requests += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, url):
        self.requests += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


# --- identifiers -----------------------------------------------------------------------

def test_parse_company_accepts_identifiers_and_urls():
    assert sc.parse_company("SGS")["identifier"] == "SGS"
    assert sc.parse_company("  SGS  ")["identifier"] == "SGS"
    assert sc.parse_company("https://jobs.smartrecruiters.com/SGS")["identifier"] == "SGS"
    assert sc.parse_company("https://jobs.smartrecruiters.com/SGS/744000160445305")["identifier"] == "SGS"
    assert sc.parse_company("jobs.smartrecruiters.com/Ubisoft2")["identifier"] == "Ubisoft2"
    # the identifier is not the company name: measured 07.11.2026, "ubisoft" has no board
    # and "Ubisoft2" has 298 postings, so a typed identifier is never lowercased past its
    # real spelling
    assert sc.parse_company("Ubisoft2")["identifier"] == "Ubisoft2"
    assert sc.parse_company("")["error"] == "EMPTY"
    assert sc.parse_company("   ")["error"] == "EMPTY"
    assert sc.parse_company("bad name!")["error"] == "INVALID_IDENTIFIER"
    assert sc.parse_company("../../etc/passwd")["error"] == "INVALID_IDENTIFIER"
    # measured 07.11.2026: the platform host typed alone was reported as a board that does
    # not exist, which blames the buyer's company instead of the missing identifier
    for host in ("smartrecruiters.com", "www.smartrecruiters.com", "jobs.smartrecruiters.com"):
        assert sc.parse_company(host)["error"] == "INVALID_IDENTIFIER", host
    return "parse_company: identifier, careers URL, posting URL, empty and junk refused"


# --- what a posting becomes ------------------------------------------------------------

def test_normalize_reads_the_real_answer():
    page = _fixture("postings_ubisoft.json")
    post = page["content"][0]
    row = sc.normalize(post, "Ubisoft2", "Ubisoft")
    assert row["status"] == "DELIVERED"
    assert row["jobId"] == str(post["id"])
    assert row["company"] == "Ubisoft2" and row["companyName"] == "Ubisoft"
    assert row["jobTitle"] == post["name"]
    assert row["department"] == post["department"]["label"]
    assert row["experienceLevel"] == post["experienceLevel"]["label"]
    assert row["employmentType"] == post["typeOfEmployment"]["label"]
    assert row["countryCode"] == post["location"]["country"].upper()
    assert row["url"] == "https://jobs.smartrecruiters.com/Ubisoft2/%s" % post["id"]
    assert row["detailFetched"] is False and row["jobDescription"] is None
    # every posting in the fixture is a real one and every one must survive normalize
    for post in page["content"]:
        assert sc.normalize(post, "Ubisoft2")["jobTitle"], post.get("id")
    return "normalize: real Ubisoft postings keep title, department, level, type, country, url"


def test_normalize_keeps_unknown_salary_null():
    row = sc.normalize({"id": "1", "name": "X"}, "SGS")
    assert row["salaryMin"] is None and row["salaryMax"] is None
    assert row["salaryCurrency"] is None and row["salaryPeriod"] is None
    assert row["location"] is None and row["remote"] is False
    return "normalize: a posting without a salary or a location stays null, never invented"


def test_compensation_reads_a_nested_block():
    assert sc._compensation(None)["salaryMin"] is None
    assert sc._compensation({"baseSalary": None})["salaryMin"] is None
    got = sc._compensation({"baseSalary": {"amount": {"minAmount": 50000, "maxAmount": 70000,
                                                      "currency": "eur", "period": "year"}}})
    assert (got["salaryMin"], got["salaryMax"]) == (50000.0, 70000.0), got
    assert got["salaryCurrency"] == "EUR" and got["salaryPeriod"] == "YEAR", got
    return "_compensation: null stays null, a nested amount block is read"


def test_strip_html_gives_text():
    assert sc._strip_html("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"
    assert sc._strip_html("<script>bad()</script><p>ok</p>") == "ok"
    assert sc._strip_html(None) is None
    assert sc._strip_html("   ") is None
    return "_strip_html: tags, entities, scripts and blanks"


# --- filters ---------------------------------------------------------------------------

def _settings(**over):
    base = {"keywords": [], "exclude_keywords": [], "location": "", "only_remote": False,
            "employment_type": "", "experience_level": "", "department": ""}
    base.update(over)
    return base


def test_filter_reason_by_field():
    row = {"jobTitle": "Senior Game Designer", "location": "Paris, Ile-de-France, France",
           "remote": False, "employmentType": "Full-time", "experienceLevel": "Mid-Senior Level",
           "department": "Game Design"}
    assert sc.filter_reason(row, _settings()) is None
    assert sc.filter_reason(row, _settings(keywords=["designer"])) is None
    assert sc.filter_reason(row, _settings(keywords=["engineer"])) == "keywords"
    assert sc.filter_reason(row, _settings(exclude_keywords=["senior"])) == "excludeKeywords"
    assert sc.filter_reason(row, _settings(employment_type="full-time")) is None
    assert sc.filter_reason(row, _settings(employment_type="part-time")) == "employmentType"
    assert sc.filter_reason(row, _settings(experience_level="senior")) is None
    assert sc.filter_reason(row, _settings(experience_level="entry")) == "experienceLevel"
    assert sc.filter_reason(row, _settings(department="design")) is None
    assert sc.filter_reason(row, _settings(department="finance")) == "department"
    assert sc.filter_reason(row, _settings(only_remote=True)) == "remoteOnly"
    assert sc.filter_reason(dict(row, remote=True), _settings(only_remote=True)) is None
    return "filter_reason: keywords, excludes, type, level, department, remote"


def test_keywords_are_phrases_not_single_words():
    # measured 07.11.2026: splitting on spaces turned "game designer" into "game OR
    # designer", so a buyer asking for one role was charged for every "game" title
    assert sc.split_words("game designer, level artist") == ["game designer", "level artist"]
    assert sc.split_words(" Designer ,  Level  Designer ") == ["designer", "level designer"]
    assert sc.split_words("") == [] and sc.split_words("  ,  ") == []
    row = {"jobTitle": "Game Artist", "location": "Paris, France", "remote": False,
           "employmentType": "Full-time", "experienceLevel": "Mid-Senior Level",
           "department": "Game Design"}
    assert sc.filter_reason(row, _settings(keywords=["game designer"])) == "keywords"
    assert sc.filter_reason(dict(row, jobTitle="Senior Game Designer"),
                            _settings(keywords=["game designer"])) is None
    return "split_words: comma-separated phrases, a space inside stays part of the phrase"


def test_location_matches_whole_words_only():
    # measured: 'us' inside 'Australia' used to pass, so a US filter delivered Australian rows
    assert sc._loc_match("us", "Austin, Texas, United States") is True
    assert sc._loc_match("us", "Sydney, Australia") is False
    assert sc._loc_match("us", "Remote, USA") is True
    assert sc._loc_match("france", "Paris, France") is True
    assert sc._loc_match("", "anywhere") is True
    assert sc._loc_match("remote", "Remote - Europe") is True
    return "_loc_match: whole word country codes, usa alias, substring for long names"


# --- pagination ------------------------------------------------------------------------

def _page_with_ids(ids, total=250):
    """A page of real postings with the ids a board would give them."""
    page = _fixture("postings_ubisoft.json")
    posts = []
    for index, job_id in enumerate(ids):
        post = dict(page["content"][index % len(page["content"])])
        post["id"] = job_id
        posts.append(post)
    return {"totalFound": total, "content": posts}


def test_list_postings_walks_every_page():
    client = FakeClient([_page_with_ids(["a", "b", "c"]),
                         _page_with_ids(["d", "e", "f"]),
                         _page_with_ids(["g"]),
                         _page_with_ids([])])
    got = sc.list_postings(client, "Ubisoft2", max_jobs=0)
    assert got["total"] == 250 and got["pages"] == 3, got
    assert [post["id"] for post in got["postings"]] == ["a", "b", "c", "d", "e", "f", "g"], got
    assert got["truncated"] is False and got["duplicates"] == 0
    return "list_postings: every page until the board runs out, nothing truncated"


def test_a_posting_answered_twice_is_dropped_and_counted():
    # measured 07.11.2026: offset paging answered the same posting twice. A duplicate row
    # is a duplicate charge, so the reader drops it and says how many it dropped.
    client = FakeClient([_page_with_ids(["a", "b"]),
                         _page_with_ids(["b", "c"]),
                         _page_with_ids([])])
    got = sc.list_postings(client, "Ubisoft2", max_jobs=0)
    assert [post["id"] for post in got["postings"]] == ["a", "b", "c"], got
    assert got["duplicates"] == 1, got
    assert got["truncated"] is False
    return "list_postings: a posting answered twice is delivered once and counted"


def test_list_postings_stops_at_max_jobs_with_a_reason():
    page = _fixture("postings_ubisoft.json")
    client = FakeClient([{"totalFound": 298, "content": page["content"]}])
    got = sc.list_postings(client, "Ubisoft2", max_jobs=2)
    assert got["truncated"] is True and got["truncatedReason"] == "maxJobsPerCompany", got
    # measured 07.11.2026: the API answers a page of 100 whatever we ask for, so the list
    # must be cut to the limit or the buyer pays for 100 rows after asking for 2
    assert len(got["postings"]) == 2, len(got["postings"])
    return "list_postings: maxJobsPerCompany truncates with its own reason"


def test_a_failed_page_after_the_first_is_truncation_not_a_short_board():
    page = _fixture("postings_ubisoft.json")
    client = FakeClient([{"totalFound": 298, "content": page["content"]},
                         sc.ScError("HTTP 500", 500)])
    got = sc.list_postings(client, "Ubisoft2", max_jobs=0)
    assert got["truncated"] is True and "page 2 failed" in got["truncatedReason"], got
    assert got["pagesFailed"] == 1 and len(got["postings"]) == 3
    return "list_postings: a page that fails mid-board is truncated with the reason"


def test_a_board_that_never_answered_raises():
    client = FakeClient([sc.ScError("HTTP 500", 500)])
    try:
        sc.list_postings(client, "Ubisoft2")
    except sc.ScError:
        return "list_postings: a board that never answered raises instead of reading as empty"
    raise AssertionError("a board that never answered must raise, not read as empty")


def test_the_limit_is_one_hundred_not_more():
    page = _fixture("postings_ubisoft.json")
    client = FakeClient([{"totalFound": 298, "content": page["content"]},
                         {"totalFound": 298, "content": page["content"][:1]},
                         {"totalFound": 298, "content": []}])
    sc.list_postings(client, "Ubisoft2")
    # measured 07.11.2026: limit=200 is answered with 100 rows, so asking for more is mute
    assert sc.PAGE == 100
    return "list_postings: the page size is the measured ceiling of 100"


# --- details ---------------------------------------------------------------------------

def test_fetch_detail_reads_the_real_answer():
    detail = _fixture("detail_ubisoft.json")
    client = FakeClient([detail])
    got = sc.fetch_detail(client, "Ubisoft2", detail["id"])
    assert got["jobDescription"] and "<" not in got["jobDescription"], got["jobDescription"][:120]
    assert got["qualifications"] and got["companyDescription"]
    assert got["applyUrl"] == detail.get("applyUrl")
    sections = detail["jobAd"]["sections"]
    assert len(got["jobDescription"]) <= len(sc._strip_html(sections["jobDescription"]["text"]))
    return "fetch_detail: real Ubisoft description, qualifications and company blurb as text"


def test_fetch_detail_failure_raises_for_the_caller():
    client = FakeClient([sc.ScError("HTTP 404", 404)])
    try:
        sc.fetch_detail(client, "Ubisoft2", "1")
    except sc.ScError:
        return "fetch_detail: a refused description raises and the caller keeps the row"
    raise AssertionError("a refused description must raise")


def test_board_name_separates_a_real_board_from_a_typo():
    real = (200, b"<html><title>Careers at Ubisoft</title></html>")
    typo = (200, b"<html><title>SmartRecruiters Job Search</title></html>")
    assert sc.board_name(FakeClient([real]), "Ubisoft2") == "Ubisoft"
    assert sc.board_name(FakeClient([typo]), "zzzz-not-a-company") is None
    assert sc.board_name(FakeClient([(500, b"")]), "x") is None
    return "board_name: 'Careers at X' means the board exists, the generic title does not"


def run_all():
    checks = [
        test_parse_company_accepts_identifiers_and_urls,
        test_normalize_reads_the_real_answer,
        test_normalize_keeps_unknown_salary_null,
        test_compensation_reads_a_nested_block,
        test_strip_html_gives_text,
        test_filter_reason_by_field,
        test_keywords_are_phrases_not_single_words,
        test_location_matches_whole_words_only,
        test_list_postings_walks_every_page,
        test_a_posting_answered_twice_is_dropped_and_counted,
        test_list_postings_stops_at_max_jobs_with_a_reason,
        test_a_failed_page_after_the_first_is_truncation_not_a_short_board,
        test_a_board_that_never_answered_raises,
        test_the_limit_is_one_hundred_not_more,
        test_fetch_detail_reads_the_real_answer,
        test_fetch_detail_failure_raises_for_the_caller,
        test_board_name_separates_a_real_board_from_a_typo,
    ]
    failed = 0
    for check in checks:
        try:
            note = check()
            print("ok   %-58s %s" % (check.__name__, note or ""))
        except AssertionError as exc:
            failed += 1
            print("FAIL %-58s %s" % (check.__name__, exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERR  %-58s %s: %s" % (check.__name__, type(exc).__name__, exc))
    print("\n%d checks, %d failed" % (len(checks), failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
