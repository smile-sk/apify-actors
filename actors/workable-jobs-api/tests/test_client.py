"""Checks for the Workable board reader. Run with PYTHONPATH set to the actor folder."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from src import client  # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        RESULTS.append((name, "FAIL", str(exc)))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, "ERROR", "%s: %s" % (type(exc).__name__, exc)))
    else:
        RESULTS.append((name, "ok", ""))


LIVE_JOB = {
    "application_url": "https://apply.workable.com/j/54B541D775/apply",
    "city": "Boca Raton",
    "code": "",
    "country": "United States",
    "created_at": "2025-03-28",
    "department": "Finance",
    "description": "<p>If&nbsp;you are reading this</p><ul><li>one</li></ul>",
    "education": "Bachelor's Degree",
    "employment_type": "Full-time",
    "experience": "Mid-Senior level",
    "function": "Accounting/Auditing",
    "industry": "Food & Beverages",
    "locations": [
        {"country": "United States", "countryCode": "US", "city": "Boca Raton", "region": "Florida", "hidden": False},
        {"country": "Canada", "countryCode": "CA", "city": "Toronto", "region": None, "hidden": False},
    ],
    "published_on": "2026-10-01",
    "shortcode": "54B541D775",
    "shortlink": "https://apply.workable.com/j/54B541D775",
    "state": "Florida",
    "telecommuting": False,
    "title": "Accounting Manager",
    "url": "https://apply.workable.com/j/54B541D775",
}


def test_normalize():
    assert client.normalize_board("celsius") == "celsius"
    assert client.normalize_board("  Celsius ") == "celsius"
    assert client.normalize_board("https://apply.workable.com/fuku/") == "fuku"
    assert client.normalize_board("fuku.workable.com") == "fuku"
    assert client.normalize_board("https://fuku.workable.com/careers") == "fuku"
    assert client.normalize_board("apply.workable.com/prx") == "prx"
    assert client.normalize_board("www.workable.com") is None
    assert client.normalize_board("https://apply.workable.com/") is None
    assert client.normalize_board("https://workable.com") is None
    assert client.normalize_board("not a board !!!") is None
    assert client.normalize_board("") is None
    assert client.normalize_board(None) is None
    assert client.normalize_board("https://example.com/celsius") is None


def test_parse_all_locations():
    row = client.parse_job(LIVE_JOB, "celsius", "Celsius")
    assert row["jobId"] == "54B541D775"
    assert row["title"] == "Accounting Manager"
    assert row["department"] == "Finance"
    assert row["employmentType"] == "Full-time"
    assert row["remote"] is False
    assert row["publishedOn"] == "2026-10-01"
    assert row["createdAt"] == "2025-03-28"
    assert row["applyUrl"].endswith("/apply")
    assert len(row["locations"]) == 2, row["locations"]
    assert row["locations"][1]["region"] is None
    assert row["country"] == "United States"


def test_empty_fields_are_null():
    row = client.parse_job({"title": "X", "shortcode": "A1", "department": "", "education": "  "}, "b")
    assert row["department"] is None
    assert row["education"] is None
    assert row["industry"] is None
    assert row["remote"] is None


def test_description_flags():
    full = client.parse_job(LIVE_JOB, "b", "B", True, "html")
    assert full["description"].startswith("<p>")
    off = client.parse_job(LIVE_JOB, "b", "B", False, "html")
    assert off["description"] is None
    plain = client.parse_job(LIVE_JOB, "b", "B", True, "text")
    assert "<" not in plain["description"]
    assert "If you are reading this" in plain["description"].replace("\xa0", " ")
    assert "one" in plain["description"]


def test_not_a_posting():
    assert client.parse_job({}, "b") is None
    assert client.parse_job("nope", "b") is None
    assert client.parse_job({"title": "", "shortcode": ""}, "b") is None


def test_filters():
    row = client.parse_job(LIVE_JOB, "b", "B")
    assert client.matches(row, {}) is True
    assert client.matches(row, {"department": "finan"}) is True
    assert client.matches(row, {"department": "sales"}) is False
    assert client.matches(row, {"keywords": ["accounting"]}) is True
    assert client.matches(row, {"keywords": ["nurse", "accounting"]}) is True
    assert client.matches(row, {"keywords": ["game designer"]}) is False
    assert client.matches(row, {"country": "ca"}) is True
    assert client.matches(row, {"country": "canada"}) is True
    assert client.matches(row, {"country": "pl"}) is False
    assert client.matches(row, {"remoteOnly": True}) is False
    assert client.matches(row, {"publishedAfter": "2026-01-01"}) is True
    assert client.matches(row, {"publishedAfter": "2026-11-01"}) is False


def test_sort_newest_first():
    rows = [
        {"publishedOn": "2026-01-01", "jobId": "a"},
        {"publishedOn": "2026-10-01", "jobId": "b"},
        {"publishedOn": None, "jobId": "c"},
    ]
    ordered = sorted(rows, key=lambda r: client.sort_key(r, "published"), reverse=True)
    assert [r["jobId"] for r in ordered] == ["b", "a", "c"]


def test_fetch_not_found():
    original = client._http_get

    def fake(url, timeout):
        raise client.urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    client._http_get = fake
    try:
        client.fetch_board("zzzz")
    except client.BoardError as exc:
        assert exc.kind == "not_found", exc.kind
    else:
        raise AssertionError("a 404 must be reported as not_found")
    finally:
        client._http_get = original


def test_fetch_error_is_not_empty():
    original = client._http_get

    def fake(url, timeout):
        raise client.BoardError("error", "boom") if False else (_ for _ in ()).throw(
            client.urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        )

    client._http_get = fake
    try:
        client.fetch_board("celsius")
    except client.BoardError as exc:
        assert exc.kind == "error", exc.kind
    else:
        raise AssertionError("a 5xx must be an error, not an empty board")
    finally:
        client._http_get = original


def test_fetch_not_json():
    original = client._http_get

    def fake(url, timeout):
        return 200, b"<html>nope</html>"

    client._http_get = fake
    try:
        client.fetch_board("celsius")
    except client.BoardError as exc:
        assert exc.kind == "error" and "JSON" in exc.reason, exc.reason
    else:
        raise AssertionError("non JSON must be an error")
    finally:
        client._http_get = original


def test_fetch_ok_and_empty():
    original = client._http_get
    import json as _json

    def fake(url, timeout):
        assert url.endswith("/accounts/celsius?details=true"), url
        return 200, _json.dumps({"name": "Celsius", "description": "", "jobs": [LIVE_JOB]}).encode()

    client._http_get = fake
    try:
        board = client.fetch_board("celsius")
        assert board["name"] == "Celsius"
        assert len(board["jobs"]) == 1
    finally:
        client._http_get = original

    def empty(url, timeout):
        return 200, b'{"name":"Toggl","description":"","jobs":[]}'

    client._http_get = empty
    try:
        board = client.fetch_board("toggl")
        assert board["jobs"] == []
    finally:
        client._http_get = original


def test_huge_response_refused():
    original = client._http_get

    def fake(url, timeout):
        return 200, b"x" * 10

    client._http_get = fake
    old_limit = client.MAX_RESPONSE_BYTES
    client.MAX_RESPONSE_BYTES = 5
    try:
        client.fetch_board("celsius")
    except client.BoardError as exc:
        assert exc.kind == "error" and "40 MB" in exc.reason, exc.reason
    else:
        raise AssertionError("an oversized answer must be an error, not a truncated board")
    finally:
        client.MAX_RESPONSE_BYTES = old_limit
        client._http_get = original


def test_country_filter_is_not_a_substring_match():
    def row_in(country, code):
        doc = dict(LIVE_JOB)
        doc["locations"] = [{"country": country, "countryCode": code, "city": "X", "region": None}]
        doc["country"] = country
        return client.parse_job(doc, "b", "B")

    assert client.matches(row_in("India", "IN"), {"country": "in"}) is True
    assert client.matches(row_in("Singapore", "SG"), {"country": "in"}) is False, "'in' inside Singapore"
    assert client.matches(row_in("Indonesia", "ID"), {"country": "in"}) is False, "'in' inside Indonesia"
    assert client.matches(row_in("United Kingdom", "GB"), {"country": "in"}) is False, "'in' inside United Kingdom"
    assert client.matches(row_in("South Africa", "ZA"), {"country": "ca"}) is False, "'ca' inside Africa"
    assert client.matches(row_in("Cambodia", "KH"), {"country": "ca"}) is False, "'ca' inside Cambodia"
    assert client.matches(row_in("Australia", "AU"), {"country": "us"}) is False
    assert client.matches(row_in("United Kingdom", "GB"), {"country": "uk"}) is True
    assert client.matches(row_in("United States", "US"), {"country": "usa"}) is True
    assert client.matches(row_in("United States", "US"), {"country": "united states"}) is True
    assert client.matches(row_in("British Indian Ocean Territory", "IO"), {"country": "india"}) is False


def test_merge_postings_keeps_every_location_once():
    first = client.parse_job(LIVE_JOB, "b", "B")
    second = client.parse_job(LIVE_JOB, "b", "B")
    rows, merged = client.merge_postings([first, second])
    assert len(rows) == 1, len(rows)
    assert merged == 1, merged
    assert len(rows[0]["locations"]) == 2, rows[0]["locations"]
    assert [l["city"] for l in rows[0]["locations"]] == ["Boca Raton", "Toronto"]

    other = dict(LIVE_JOB)
    other["shortcode"] = "OTHER"
    rows2, merged2 = client.merge_postings([client.parse_job(LIVE_JOB, "b", "B"), client.parse_job(other, "b", "B")])
    assert len(rows2) == 2, len(rows2)
    assert merged2 == 0, merged2


CHECKS = [
    ("normalize accepts identifiers and URLs, refuses the host", test_normalize),
    ("parse keeps every location, not only the first", test_parse_all_locations),
    ("empty board fields become null, not empty strings", test_empty_fields_are_null),
    ("includeDetails and descriptionFormat behave", test_description_flags),
    ("an entry that is not a posting is refused", test_not_a_posting),
    ("filters match in code, phrases are not split by spaces", test_filters),
    ("sorting puts the newest first", test_sort_newest_first),
    ("a 404 is not_found, not an empty board", test_fetch_not_found),
    ("a 5xx is an error, not an empty board", test_fetch_error_is_not_empty),
    ("a non JSON answer is an error", test_fetch_not_json),
    ("a good answer and an empty board parse", test_fetch_ok_and_empty),
    ("an oversized answer is refused, not truncated", test_huge_response_refused),
    ("the country filter is equality on the code, not a substring", test_country_filter_is_not_a_substring_match),
    ("merging by jobId keeps every location exactly once", test_merge_postings_keeps_every_location_once),
]


def run_all():
    for name, fn in CHECKS:
        check(name, fn)
    failed = [r for r in RESULTS if r[1] != "ok"]
    for name, status, detail in RESULTS:
        if status != "ok":
            print("%-6s %s :: %s" % (status, name, detail))
    print("%d/%d checks passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_all())
