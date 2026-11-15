"""Tests for ATS detection and filtering.

No Apify SDK and no network here: everything below is a pure function, so the
suite runs in a second and pins down the behaviour the listing promises.

Run directly:  python tests/test_ats.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from ats import (  # noqa: E402
    ATS_PATTERNS,
    FETCHERS,
    PROBE_ORDER,
    _ats_host_slug,
    _loc_match,
    _parse_salary,
    _strip_html,
    filter_jobs,
    parse_company,
    unreadable_providers,
)


def test_parse_company_bare_name():
    parsed = parse_company("Acme Inc")
    assert parsed["display"] == "Acme Inc"
    assert parsed["domain"] == ""
    assert parsed["slugs"] == ["acme-inc", "acmeinc"], parsed


def test_parse_company_domain():
    parsed = parse_company("acme.com")
    assert parsed["domain"] == "acme.com"
    assert parsed["display"] == "Acme"
    assert parsed["slugs"][0] == "acme"
    assert "acmecom" in parsed["slugs"]


def test_parse_company_strips_www_and_scheme():
    parsed = parse_company("https://www.acme.com/careers")
    assert parsed["domain"] == "acme.com"
    assert parsed["slugs"][0] == "acme"


def test_parse_company_board_url_names_its_slug():
    parsed = parse_company("https://jobs.lever.co/leverdemo")
    assert parsed["hint"] == {"ats": "lever", "slug": "leverdemo"}
    assert parsed["slugs"] == ["leverdemo"]
    assert parsed["display"] == "Leverdemo"


def test_parse_company_greenhouse_board_url():
    parsed = parse_company("https://boards.greenhouse.io/acme")
    assert parsed["hint"]["ats"] == "greenhouse"
    assert parsed["hint"]["slug"] == "acme"


def test_parse_company_recruitee_subdomain():
    parsed = parse_company("https://acme.recruitee.com/")
    assert parsed["hint"] == {"ats": "recruitee", "slug": "acme"}


def test_parse_company_empty():
    assert parse_company("") == {}
    assert parse_company("   ") == {}


def test_parse_company_hyphen_adds_joined_slug():
    # Third form strips the dots only, so the dash stays: <label>com.
    parsed = parse_company("my-company.com")
    assert parsed["slugs"] == ["my-company", "mycompany", "my-companycom"], parsed


def test_strip_html_unescapes_and_collapses():
    assert _strip_html("<p>Hello &amp; <b>you</b></p>") == "Hello & you"
    assert _strip_html(None) == ""
    assert _strip_html("   ") == ""


def test_strip_html_drops_script_and_style():
    text = "<style>.a{color:red}</style><script>var x=1</script><p>Real text</p>"
    assert _strip_html(text) == "Real text"


def test_salary_range_with_k_suffix():
    salary = _parse_salary("$180K - $250K a year")
    assert salary["min"] == 180000
    assert salary["max"] == 250000
    assert salary["currency"] == "USD"
    assert salary["period"] == "YEAR"


def test_salary_euro_with_dots():
    salary = _parse_salary("EUR 60.000 - 80.000 per year")
    assert salary["min"] == 60000
    assert salary["max"] == 80000
    assert salary["currency"] == "EUR"
    assert salary["period"] == "YEAR"


def test_salary_single_value_has_no_max():
    salary = _parse_salary("GBP 55,000 per year")
    assert salary["min"] == 55000
    assert salary["max"] is None
    assert salary["currency"] == "GBP"


def test_salary_empty():
    assert _parse_salary(None) == {"min": None, "max": None, "currency": None, "period": None}
    assert _parse_salary("") == {"min": None, "max": None, "currency": None, "period": None}
    assert _parse_salary("Competitive")["min"] is None


def test_loc_match_us_does_not_match_australia():
    # The regression: 'US' used to pass 'Australia' as a substring.
    assert _loc_match("us", "Australia") is False
    assert _loc_match("us", "Austria") is False


def test_loc_match_whole_words():
    assert _loc_match("us", "New York, US") is True
    assert _loc_match("uk", "London, United Kingdom") is True
    assert _loc_match("germany", "Berlin, Germany") is True
    assert _loc_match("london", "London, UK") is True


def test_loc_match_short_code_needs_the_code_in_the_text():
    # Honest limit of the documented "substring match": a two-letter ISO code
    # matches the location only when the text carries the code ("Berlin, DE"),
    # not the country name. A full country name works.
    assert _loc_match("de", "Berlin, DE") is True
    assert _loc_match("de", "Berlin, Germany") is False


def test_loc_match_remote():
    assert _loc_match("remote", "Remote - Europe") is True
    assert _loc_match("remote", "Berlin, Germany") is False


def test_loc_match_empty_needle_matches_everything():
    assert _loc_match("", "anything") is True
    assert _loc_match("", None) is True


def test_filter_jobs_by_keyword_location_remote():
    jobs = [
        {"jobTitle": "Senior Python Engineer", "location": "Berlin, Germany", "remote": False},
        {"jobTitle": "Python Developer", "location": "Remote - Europe", "remote": True},
        {"jobTitle": "Designer", "location": "Berlin, Germany", "remote": False},
    ]
    assert len(filter_jobs(jobs, keywords="python")) == 2
    assert len(filter_jobs(jobs, keywords="python", only_remote=True)) == 1
    assert len(filter_jobs(jobs, location="germany")) == 2
    assert len(filter_jobs(jobs, keywords="python,designer")) == 3


def test_filter_jobs_limit_stops_early():
    jobs = [{"jobTitle": f"Engineer {i}", "location": "US", "remote": False} for i in range(10)]
    assert len(filter_jobs(jobs, keywords="engineer", limit=3)) == 3
    assert filter_jobs(jobs, keywords="nothing-matches") == []


def test_every_probe_order_provider_has_a_fetcher():
    assert set(FETCHERS) == set(PROBE_ORDER), (set(FETCHERS), set(PROBE_ORDER))
    assert set(PROBE_ORDER) <= {name for name, _ in ATS_PATTERNS}


def test_unreadable_provider_is_reported_not_dropped():
    # personio is recognised in page links but has no fetcher: saying
    # NO_ATS_FOUND there would send the buyer hunting for a board we just found.
    assert "personio" in {name for name, _ in ATS_PATTERNS}
    assert unreadable_providers([("personio", "acme")]) == ["personio"]
    assert unreadable_providers([("lever", "acme"), ("personio", "acme")]) == ["personio"]
    assert unreadable_providers([("lever", "acme")]) == []


def test_ats_host_slug_unknown_host_is_empty():
    assert _ats_host_slug("acme.com", "/careers") == {}
    assert _ats_host_slug("jobs.lever.co", "/") == {}
    assert _ats_host_slug("jobs.lever.co", "/acme/abc") == {"ats": "lever", "slug": "acme"}


def run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:
            failed.append((name, exc))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
