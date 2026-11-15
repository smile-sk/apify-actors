"""Checks for the Recruitee reader: identifiers, parsing, offices, money, filters."""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from src import client as client_mod  # noqa: E402
from src.client import (  # noqa: E402
    BoardError, feed_url, fetch_board, matches, merge_positions, normalize_board, parse_job,
    parse_money, parse_offers, sort_key,
)

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append("%s: got %r, want %r" % (name, got, want))


def load(name):
    with open(os.path.join(HERE, name), "rb") as handle:
        return handle.read()


def test_identifiers():
    good = {
        "bunq": "bunq",
        "BUNQ": "bunq",
        " bunq ": "bunq",
        "bunq.recruitee.com": "bunq",
        "https://bunq.recruitee.com/": "bunq",
        "https://bunq.recruitee.com/api/offers/": "bunq",
        "https://www.bunq.recruitee.com/api/offers/": "bunq",
        "ggzbreburg": "ggzbreburg",
    }
    for raw, want in good.items():
        check("normalize %r" % raw, normalize_board(raw), want)
    bad = ["", "   ", "bunq rec", "careers.bunq.com", "api.bunq.recruitee.com",
           "recruitee.com", "www", "bunq/", None, 12, "https://bunq.com/"]
    for raw in bad:
        check("reject %r" % (raw,), normalize_board(raw), None)
    check("feed url", feed_url("bunq"), "https://bunq.recruitee.com/api/offers/")


def test_parse_offers():
    offers = parse_offers(load("offers_bunq.json"))
    check("real fixture offers", len(offers), 3)
    check("empty fixture", parse_offers(load("offers_empty.json")), [])
    for body, why in ((b"<html>no</html>", "not JSON"), (b'{"x": 1}', "no offers"),
                      (b'[1,2]', "not an object")):
        try:
            parse_offers(body)
        except BoardError as exc:
            check("bad body %s kind" % why, exc.kind, "error")
        else:
            FAILS.append("bad body %s did not raise" % why)


def test_parse_job():
    offers = parse_offers(load("offers_bunq.json"))
    multi = next(o for o in offers if len(o.get("locations") or []) > 1)
    row = parse_job(multi, "bunq", True, "html")
    check("jobId", row["jobId"], str(multi["id"]))
    check("offices kept", len(row["locations"]), len(multi["locations"]))
    check("first city", row["city"], multi["locations"][0]["city"])
    check("delivered", row["delivered"], True)
    check("status", row["status"], "DELIVERED")
    check("description kept", bool(row["description"]), True)
    check("requirements kept", bool(row["requirements"]), True)

    plain = parse_job(multi, "bunq", True, "text")
    check("text has no tags", "<p>" in (plain["description"] or ""), False)
    off = parse_job(multi, "bunq", False, "html")
    check("details off", (off["description"], off["requirements"]), (None, None))
    check("not an offer", parse_job({"id": 1}, "bunq"), None)
    check("not a dict", parse_job("x", "bunq"), None)


def test_money():
    cases = {
        "1,299": 1299.0, "1,299.00": 1299.0, "1.299,00": 1299.0,
        "\u20ac 2 500,50": 2500.5, "45000": 45000.0, 3200: 3200.0,
        "": None, None: None, "-": None, "not a number": None,
    }
    for raw, want in cases.items():
        check("money %r" % (raw,), parse_money(raw), want)


def test_merge():
    rows = [
        {"jobId": "1", "locations": [{"city": "A", "country": "AT"}]},
        {"jobId": "1", "locations": [{"city": "B", "country": "AT"}, {"city": "A", "country": "AT"}]},
        {"jobId": "2", "locations": [{"city": "C", "country": "DE"}]},
    ]
    merged, duplicates = merge_positions(rows)
    check("merged rows", len(merged), 2)
    check("duplicates", duplicates, 1)
    check("offices united", [x["city"] for x in merged[0]["locations"]], ["A", "B"])


def test_filters():
    row = {"title": "Senior Engineer", "department": "Platform", "category": "banking",
           "employmentType": "fulltime_permanent", "experience": "mid_level",
           "city": "Vienna", "locations": [{"city": "Vienna"}, {"city": "Cologne"}],
           "remote": False, "hybrid": True, "tags": ["Backend"], "publishedAt": "2026-10-30 10:00:00 UTC"}
    base = {"remoteOnly": False, "department": "", "employmentType": "", "experience": "",
            "category": "", "city": "", "keywords": [], "publishedAfter": ""}
    check("no filter", matches(row, base), True)
    check("keywords hit", matches(row, dict(base, keywords=["engineer"])), True)
    check("keywords miss", matches(row, dict(base, keywords=["nurse"])), False)
    check("second office city", matches(row, dict(base, city="cologne")), True)
    check("other city", matches(row, dict(base, city="berlin")), False)
    check("hybrid is not remote", matches(row, dict(base, remoteOnly=True)), False)
    check("remote true", matches(dict(row, remote=True), dict(base, remoteOnly=True)), True)
    check("date after", matches(row, dict(base, publishedAfter="2026-11-01")), False)
    check("date before", matches(row, dict(base, publishedAfter="2026-10-01")), True)
    check("department miss", matches(row, dict(base, department="sales")), False)
    check("sort key", sort_key(row), "2026-10-30 10:00:00 UTC")


def test_fetch_board():
    """fetch_board decides what a missing board is and what an empty board is."""
    original = client_mod._read
    try:
        client_mod._read = lambda url, timeout: load("offers_bunq.json")
        board = fetch_board("bunq", 5)
        check("fetch real jobs", len(board["jobs"]), 3)
        check("fetch real not empty", board["empty"], False)

        client_mod._read = lambda url, timeout: load("offers_empty.json")
        board = fetch_board("bunq", 5)
        check("fetch empty jobs", board["jobs"], [])
        check("fetch empty flag", board["empty"], True)

        def missing(url, timeout):
            raise BoardError("not_found", "404")
        client_mod._read = missing
        try:
            fetch_board("zzzz", 5)
        except BoardError as exc:
            check("missing board kind", exc.kind, "not_found")
        else:
            FAILS.append("a missing board did not raise not_found")
    finally:
        client_mod._read = original


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
