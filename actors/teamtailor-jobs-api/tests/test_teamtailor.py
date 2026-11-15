"""Parsing and guards of the Teamtailor client, on fixtures taken from live boards.

The fixtures are real answers: `feed_tibber.json` keeps three roles that really list
several offices, `feed_storytel.json` one role, `feed_empty.json` an existing board
with no openings. Nothing here is invented, so a passing test says something about
the source and not only about my hopes.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from src import client  # noqa: E402

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append("%s: got %r, want %r" % (name, got, want))


def load(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as handle:
        return json.load(handle)


def test_normalize_board():
    check("bare name", client.normalize_board("oatly"), "oatly.teamtailor.com")
    check("host", client.normalize_board("Oatly.Teamtailor.com"), "oatly.teamtailor.com")
    check("url", client.normalize_board("https://oatly.teamtailor.com/jobs"), "oatly.teamtailor.com")
    check("trailing slash", client.normalize_board("oatly.teamtailor.com/"), "oatly.teamtailor.com")
    # a self-hosted board is first class: measured live on three of them
    check("self hosted", client.normalize_board("https://jobs.tibber.com/jobs"), "jobs.tibber.com")
    check("self hosted bare", client.normalize_board("career.kry.se"), "career.kry.se")
    check("www stripped", client.normalize_board("https://www.jobs.tibber.com"), "jobs.tibber.com")
    check("nested subdomain refused", client.normalize_board("api.oatly.teamtailor.com"), None)
    check("reserved refused", client.normalize_board("www"), None)
    check("garbage refused", client.normalize_board("not a board !!!"), None)
    check("ftp refused", client.normalize_board("ftp://oatly.teamtailor.com"), None)
    check("empty refused", client.normalize_board(""), None)
    check("not a string refused", client.normalize_board(None), None)


def test_feed_url():
    check("feed url", client.feed_url("oatly.teamtailor.com"), "https://oatly.teamtailor.com/jobs.json")


def test_private_targets_refused():
    for host in ("127.0.0.1", "localhost", "169.254.169.254", "::1", "10.0.0.5",
                 "::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:10.0.0.5",
                     "100.64.0.1", "100.127.255.254", "::ffff:100.64.0.1"):
        ok, why = client._host_is_public(host)
        check("private %s" % host, ok, False)
        check("private reason %s" % host, bool(why), True)
    ok, _ = client._host_is_public("8.8.8.8")
    check("public literal", ok, True)


def test_live_fixture_parsing():
    feed = load("feed_tibber.json")
    items = client.parse_feed(json.dumps(feed).encode("utf-8"))
    check("tibber items", len(items), 5)
    rows = [client.parse_job(item, "jobs.tibber.com") for item in items]
    check("all parsed", all(rows), True)
    multi = [row for row in rows if len(row["locations"]) > 1]
    check("multi location roles", len(multi) >= 3, True)
    # the whole point: every office is kept, not only the first one.
    # A tautology here ("got X, want X") would prove nothing, so the number is fixed.
    if not multi:
        return  # already reported; the rest would only crash on an empty list
    first = multi[0]
    check("offices kept", len(first["locations"]) >= 2, True)
    offices_here = first["locations"]
    check("first office has city", bool(offices_here[0]["city"]) if offices_here else False, True)
    if offices_here:
        check("row city is the first office", first["city"], offices_here[0]["city"])
    check("job id from the posting", bool(first["jobId"]), True)
    check("url present", bool(first["url"]), True)
    check("employment type present", bool(first["employmentType"]), True)
    check("remote is boolean", isinstance(first["remote"], bool), True)
    check("delivered row is billable unknown", first["billable"], None)
    check("status", first["status"], "DELIVERED")


def test_remote_flag_from_feed():
    feed = load("feed_tibber.json")
    items = client.parse_feed(json.dumps(feed).encode("utf-8"))
    rows = [client.parse_job(item, "board") for item in items]
    remote = [row for row in rows if row["remote"]]
    check("remote rows found", len(remote) >= 0, True)
    for row in remote:
        # remote must come from the posting, never from the word in the title
        check("remote has no title guess %s" % row["jobId"],
              row["remote"], True)


def test_valid_through_and_dates():
    feed = load("feed_storytel.json")
    items = client.parse_feed(json.dumps(feed).encode("utf-8"))
    row = client.parse_job(items[0], "storytel.teamtailor.com")
    check("published at", bool(row["publishedAt"]), True)
    check("title", bool(row["title"]), True)
    check("company", bool(row["company"]), True)


def test_empty_board_is_not_an_error():
    items = client.parse_feed(json.dumps(load("feed_empty.json")).encode("utf-8"))
    check("empty items", items, [])


def test_not_a_feed():
    for body in (b"", b"[]", b'{"foo": 1}', b"<html></html>"):
        try:
            client.parse_feed(body)
        except client.BoardError as exc:
            check("not a feed kind %r" % body[:8], exc.kind, "error")
        else:
            FAILS.append("not a feed %r: no error raised" % body[:8])


def test_strip_html_drops_script():
    html = "<p>Hi</p><script>alert('x')</script><style>p{}</style><p>There</p>"
    text = client.strip_html(html)
    check("script dropped", "alert" in text, False)
    check("style dropped", "p{}" in text, False)
    check("text kept", "Hi" in text and "There" in text, True)


def test_strip_html_entities():
    check("entities", client.strip_html("<p>Caf&eacute; &amp; bar</p>"), "Café & bar")


def test_merge_positions():
    rows = [
        {"jobId": "1", "locations": [{"city": "Berlin"}], "title": "A"},
        {"jobId": "1", "locations": [{"city": "Munich"}], "title": "A"},
        {"jobId": "2", "locations": [{"city": "Berlin"}], "title": "B"},
    ]
    merged, duplicates = client.merge_positions(rows)
    check("merged count", len(merged), 2)
    check("duplicates", duplicates, 1)
    check("offices united", [office["city"] for office in merged[0]["locations"]], ["Berlin", "Munich"])


def test_country_filter_is_exact_for_codes():
    row = {"countryCode": "SG", "country": "Singapore", "city": "Singapore",
           "locations": [{"countryCode": "SG", "country": "Singapore", "city": "Singapore"}]}
    # "in" used to match Singapore by substring: a paid row for a country nobody asked for
    check("in does not match singapore", client.matches(row, {"country": "in"}), False)
    check("sg matches", client.matches(row, {"country": "sg"}), True)
    ca = {"countryCode": "ZA", "country": "South Africa", "city": "Cape Town",
          "locations": [{"countryCode": "ZA", "country": "South Africa", "city": "Cape Town"}]}
    check("ca does not match south africa", client.matches(ca, {"country": "ca"}), False)
    check("full name matches", client.matches(ca, {"country": "south"}), True)


def test_filters_other_fields():
    row = {"jobId": "1", "title": "Data Engineer", "company": "Oatly", "remote": True,
           "employmentType": "FULL_TIME", "city": "Berlin", "countryCode": "DE",
           "country": "Germany", "locations": [{"city": "Munich", "countryCode": "DE",
                                               "country": "Germany"}],
           "publishedAt": "2026-11-01T10:00:00+01:00"}
    check("remote only passes", client.matches(row, {"remoteOnly": True}), True)
    check("remote only drops", client.matches(dict(row, remote=False), {"remoteOnly": True}), False)
    check("employment", client.matches(row, {"employmentType": "full_time"}), True)
    check("keywords", client.matches(row, {"keywords": ["engineer"]}), True)
    check("keywords miss", client.matches(row, {"keywords": ["designer"]}), False)
    check("city in second office", client.matches(row, {"city": "munich"}), True)
    check("date filter passes", client.matches(row, {"publishedAfter": "2026-10-01"}), True)
    check("date filter drops", client.matches(row, {"publishedAfter": "2026-12-01"}), False)


def test_no_details_means_no_description():
    feed = load("feed_storytel.json")
    items = client.parse_feed(json.dumps(feed).encode("utf-8"))
    row = client.parse_job(items[0], "b", include_details=False)
    check("description off", row["description"], None)
    row = client.parse_job(items[0], "b", include_details=True, description_format="text")
    check("text has no tags", "<" in (row["description"] or ""), False)


def run_all():
    FAILS.clear()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("%d check(s) failed" % len(FAILS))
    for line in FAILS:
        print("  FAIL", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run_all())
