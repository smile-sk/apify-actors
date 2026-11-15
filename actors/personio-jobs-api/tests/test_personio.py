"""Checks for the Personio reader: identifiers, the feed, three board states, filters."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from src import client  # noqa: E402

FEED = open(os.path.join(HERE, "feed_ottonova.xml"), encoding="utf-8").read().encode("utf-8")
FEED_EMPTY = open(os.path.join(HERE, "feed_empty.xml"), encoding="utf-8").read().encode("utf-8")


def test_identifiers():
    assert client.normalize_board("ottonova") == "ottonova"
    assert client.normalize_board("  OTTONOVA  ") == "ottonova"
    assert client.normalize_board("https://ottonova.jobs.personio.de/") == "ottonova"
    assert client.normalize_board("https://ottonova.jobs.personio.com/de") == "ottonova"
    assert client.normalize_board("ottonova.jobs.personio.de") == "ottonova"
    # a name with a space or a foreign host is a bad identifier, never a guess
    assert client.normalize_board("ottonova inc") is None
    assert client.normalize_board("https://example.com/ottonova") is None
    assert client.normalize_board("https://www.ottonova.jobs.personio.de/") is None
    assert client.normalize_board("jobs") is None
    assert client.normalize_board(123) is None
    assert client.normalize_board("") is None


def test_feed_parsing():
    positions = client.parse_feed(FEED)
    assert len(positions) == 2, positions
    first = positions[0]
    assert first["id"] and first["name"]
    assert first["office"], "the fixture carries an office"
    assert first["additionalOffices"], "the fixture carries a second office"
    assert first["sections"], "the fixture carries description sections"
    assert first["createdAt"]


def test_parse_job_keeps_every_office():
    entry = client.parse_feed(FEED)[0]
    row = client.parse_job(entry, "ottonova")
    assert row["jobId"] == entry["id"]
    assert row["title"] == entry["name"]
    assert row["board"] == "ottonova"
    assert row["status"] == "DELIVERED" and row["delivered"] is True
    assert row["url"] == "https://ottonova.jobs.personio.de/job/%s" % entry["id"]
    assert row["applyUrl"] == row["url"]
    cities = [location["city"] for location in row["locations"]]
    assert cities[0] == entry["office"], cities
    assert entry["additionalOffices"][0] in cities, cities
    assert row["city"] == entry["office"]
    assert row["keywords"], "the employer keyword list is split, not dropped"
    assert row["description"], "the description is built from the sections"
    assert isinstance(row["remote"], bool)


def test_parse_job_rejects_non_positions():
    assert client.parse_job({}, "ottonova") is None
    assert client.parse_job({"id": "1"}, "ottonova") is None
    assert client.parse_job({"name": "Only a title"}, "ottonova") is None


def test_description_text_format_keeps_headings():
    entry = client.parse_feed(FEED)[0]
    row = client.parse_job(entry, "ottonova", include_details=True, description_format="text")
    text = row["description"]
    assert "<" not in text, "text format must not carry tags"
    assert "&amp;" not in text and "&nbsp;" not in text, "entities are decoded"
    section_name = entry["sections"][0]["name"]
    assert section_name and section_name in text, "the section heading is kept"


def test_description_off_is_null():
    entry = client.parse_feed(FEED)[0]
    row = client.parse_job(entry, "ottonova", include_details=False)
    assert row["description"] is None
    assert row["descriptionSections"] is None
    assert row["title"], "metadata survives without the text"


def test_board_missing_when_feed_empty_and_page_404():
    """The source lies by design: an empty feed is also what a missing company returns."""
    original_read, original_status = client._read, client._status
    client._read = lambda url, timeout: FEED_EMPTY
    client._status = lambda url, timeout: 404
    try:
        try:
            client.fetch_board("holidu")
        except client.BoardError as exc:
            assert exc.kind == "not_found", exc.kind
            assert "404" in exc.reason
        else:
            raise AssertionError("a missing board must not be reported as a quiet one")
    finally:
        client._read, client._status = original_read, original_status


def test_board_empty_when_feed_empty_and_page_alive():
    original_read, original_status = client._read, client._status
    client._read = lambda url, timeout: FEED_EMPTY
    client._status = lambda url, timeout: 200
    try:
        board = client.fetch_board("quiet")
        assert board == {"jobs": [], "empty": True}, board
    finally:
        client._read, client._status = original_read, original_status


def test_board_error_is_not_called_missing():
    original_read = client._read

    def boom(url, timeout):
        raise client.BoardError("error", "HTTP 503")

    client._read = boom
    try:
        try:
            client.fetch_board("ottonova")
        except client.BoardError as exc:
            assert exc.kind == "error", exc.kind
        else:
            raise AssertionError("an unreadable board is an error, not a missing one")
    finally:
        client._read = original_read


def test_broken_xml_is_an_error():
    original_read = client._read
    client._read = lambda url, timeout: b"<html><body>not a feed</body></html>"
    try:
        try:
            client.fetch_board("ottonova")
        except client.BoardError as exc:
            assert exc.kind == "error", exc.kind
        else:
            raise AssertionError("an HTML answer must not pass as a feed")
    finally:
        client._read = original_read


def test_duplicate_position_is_merged_not_charged_twice():
    entry = client.parse_feed(FEED)[0]
    one = client.parse_job(entry, "ottonova")
    two = dict(one)
    two["locations"] = [{"city": "Berlin"}]
    rows, merged = client.merge_positions([one, two])
    assert len(rows) == 1 and merged == 1
    cities = [location["city"] for location in rows[0]["locations"]]
    assert "Berlin" in cities and len(cities) >= 2, cities


def test_filters():
    entry = client.parse_feed(FEED)[0]
    row = client.parse_job(entry, "ottonova")
    assert client.matches(row, {}) is True
    assert client.matches(row, {"department": "no such department"}) is False
    assert client.matches(row, {"department": (row["department"] or "x").lower()[:6]}) is True
    assert client.matches(row, {"city": row["city"].lower()[:5]}) is True
    assert client.matches(row, {"keywords": ["zzzz-not-a-word"]}) is False
    assert client.matches(row, {"createdAfter": "2999-01-01"}) is False
    assert client.matches(row, {"remoteOnly": True}) in (True, False)


def test_remote_is_read_from_text():
    assert client.looks_remote(["Berlin (Remote)"], None) is True
    assert client.looks_remote(["Homeoffice"], None) is True
    assert client.looks_remote(["Munich"], "This role is remote within Germany.") is True
    assert client.looks_remote(["Munich"], "On site, five days a week.") is False
    assert client.looks_remote([], None) is False


def run_all():
    failures = 0
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
            except AssertionError as exc:
                failures += 1
                print("FAIL %s: %s" % (name, exc))
            else:
                print("ok   %s" % name)
    print("%d failed" % failures)
    return failures


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
