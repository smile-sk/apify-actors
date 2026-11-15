"""Pure checks of the parser and the CDX reader, no network.

The reader is driven through httpx.MockTransport with the bodies the Internet Archive really
answers, including the empty one: "the archive holds nothing" and "the archive did not
answer" must never collapse into the same thing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import httpx  # noqa: E402

from src import archive  # noqa: E402

PASSED = 0
FAILED: list = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


def transport(bodies):
    """A stand that answers the CDX endpoint from a list of bodies, one per call."""
    state = {"i": 0}

    def handler(request):
        index = min(state["i"], len(bodies) - 1)
        state["i"] += 1
        body = bodies[index]
        if isinstance(body, tuple):
            status, text = body
            return httpx.Response(status, text=text)
        return httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


HEADER = '["timestamp","original","statuscode","mimetype","digest","length"]'
FIRST = ('[["timestamp","original","statuscode","mimetype","digest","length"],\n'
         '["20020120142510","http://example.com:80/","200","text/html","AAA","1792"]]')
LATEST = ('[["timestamp","original","statuscode","mimetype","digest","length"],\n'
          '["20260101120000","https://example.com/","200","text/html","BBB","1088"],\n'
          '["20261113195909","https://example.com/","404","text/html","CCC","100"]]')


def run_all() -> int:
    # the parser
    target, why = archive.parse_target("example.com")
    check("bare host becomes https with a root path", target and target.url == "https://example.com/")
    target, _ = archive.parse_target("apify.com/pricing?x=1")
    check("a path and a query survive", target and target.url == "https://apify.com/pricing?x=1")
    target, _ = archive.parse_target("http://example.com/a")
    check("http is kept as sent", target and target.url == "http://example.com/a")
    target, _ = archive.parse_target("https://example.com:443/x")
    check("the default port is dropped", target and target.url == "https://example.com/x")
    target, _ = archive.parse_target("https://example.com:8443/x")
    check("a custom port is kept", target and target.url == "https://example.com:8443/x")
    target, _ = archive.parse_target("https://example.com/x#top")
    check("the fragment is dropped", target and target.url == "https://example.com/x")
    target, why = archive.parse_target("ftp://example.com/x")
    check("ftp is refused", target is None and "http or https" in why)
    target, why = archive.parse_target("javascript:alert(1)")
    check("javascript is refused", target is None)
    target, why = archive.parse_target("localhost:8080")
    check("localhost is refused", target is None and "local machine" in why)
    target, why = archive.parse_target("127.0.0.1/x")
    check("a loopback literal is refused", target is None and "private address" in why)
    target, why = archive.parse_target("10.0.0.5")
    check("a private literal is refused", target is None and "private address" in why)
    target, why = archive.parse_target("100.64.0.1")
    check("carrier grade NAT is refused too", target is None and "private address" in why)
    target, why = archive.parse_target("https://[::1]/x")
    check("an IPv6 loopback is refused", target is None and "private address" in why)
    target, why = archive.parse_target("https://[::ffff:127.0.0.1]/x")
    check("an IPv4 mapped loopback is refused", target is None and "private address" in why)
    target, why = archive.parse_target("notaurl")
    check("a host without a dot is refused", target is None and "no dot" in why)
    target, why = archive.parse_target("   ")
    check("an empty line is refused", target is None and "empty" in why)
    target, _ = archive.parse_target("\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444/\u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0430")
    check("an IDN host becomes punycode", target and target.host == "xn--e1afmkfd.xn--p1ai")
    target, _ = archive.parse_target("https://user:pass@example.com/x")
    check("credentials in the link do not break the host", target and target.host == "example.com")

    # the date filter
    check("a year filter is accepted", archive.check_date("2025") == "2025")
    check("a full stamp is accepted", archive.check_date("20250102030405") == "20250102030405")
    check("an empty filter means no filter", archive.check_date("") == "")
    for bad in ("2025-01-01", "20", "20250x"):
        try:
            archive.check_date(bad)
            check(f"the filter {bad!r} is refused", False)
        except ValueError:
            check(f"the filter {bad!r} is refused", True)

    # timestamps
    check("a CDX stamp becomes ISO", archive.snapshot_time("20020120142510") == "2002-01-20T14:25:10Z")
    check("a broken stamp gives nothing", archive.snapshot_time("2002") == "")
    check("a non numeric stamp gives nothing", archive.snapshot_time("2002012014251X") == "")

    # the answer parser
    check("an empty body is an empty list", archive._rows("") == [])
    check("[] is an empty list", archive._rows("[]") == [])
    check("the header line is not a row", len(archive._rows(FIRST)) == 1)
    check("the columns are named", archive._rows(FIRST)[0]["timestamp"] == "20020120142510")
    check("a broken body raises instead of reading as empty", _raises(lambda: archive._rows("<html>")))

    # the reader, through the real client and a stand
    client = httpx.Client(transport=transport([FIRST, LATEST]))
    target, _ = archive.parse_target("example.com")
    row, kind, reason = archive.read_history(client, target, max_snapshots=5)
    check("a known URL gives a row", row is not None and kind == "")
    check("the earliest snapshot is named", row and row["firstSnapshotAt"] == "2002-01-20T14:25:10Z")
    check("the newest snapshot is named", row and row["lastSnapshotAt"] == "2026-11-13T19:59:09Z")
    check("the versions are counted", row and row["versionsReturned"] == 2)
    check("the versions are in order", row and row["latestVersions"][0]["at"] < row["latestVersions"][1]["at"])
    check("a complete row is not partial", row and row["partial"] is False)
    check("the row is billable", row and row["billable"] is True)

    client = httpx.Client(transport=transport(["[]"]))
    crashed = ""
    try:
        row, kind, reason = archive.read_history(client, target)
    except Exception as exc:  # noqa: BLE001
        crashed, row, kind, reason = type(exc).__name__, None, "", ""
    check("an empty answer does not crash the reader", crashed == "")
    check("an unknown URL is a verdict, not an error", row is None and kind == "not_found")
    check("the verdict names the archive", "Internet Archive" in reason)
    check("the verdict carries no data", row is None)

    client = httpx.Client(transport=transport(["[]"]))
    try:
        row, kind, reason = archive.read_history(client, target, from_="2025", to="2026")
    except Exception:  # noqa: BLE001
        row, kind, reason = None, "", ""
    check("the window is named in the verdict", kind == "not_found" and "2025" in reason)

    client = httpx.Client(transport=transport([(500, "boom")]))
    row, kind, reason = archive.read_history(client, target)
    check("an HTTP error is an error, not an empty archive", row is None and kind == "error")
    check("the error names the status", "HTTP 500" in reason)

    client = httpx.Client(transport=transport([FIRST, (503, "down")]))
    row, kind, reason = archive.read_history(client, target)
    check("a failed second call still gives the first snapshot", row is not None)
    check("such a row is marked partial", row and row["partial"] is True)
    check("such a row says why", row and "could not be read" in row["versionsNote"])
    check("a partial row still carries the earliest snapshot",
          row and row["firstSnapshotAt"] == "2002-01-20T14:25:10Z")

    # a port the parser refuses is a verdict about the line, never an exception
    for bad in ("example.com:99999", "https://site.com:abc/x", ":65536", "example.com:-1"):
        broken, why = archive.parse_target(bad)
        check(f"bad port {bad!r}: no target", broken is None)
        check(f"bad port {bad!r}: the reason is words", bool(why) and len(why) > 10)
    _, port_why = archive.parse_target("example.com:99999")
    check("bad port: the reason names the port", "port" in port_why.lower())
    good, _ = archive.parse_target("example.com:8443/x")
    check("a real port survives", good is not None and good.url == "https://example.com:8443/x")

    # versions are distinct by digest, not captures of the same digest repeated
    ALT = json.dumps([
        ["timestamp", "original", "statuscode", "mimetype", "digest", "length"],
        ["20240101000000", "http://name@example.com/", "200", "text/html", "AAA", "10"],
        ["20240201000000", "https://example.com/", "200", "text/html", "BBB", "20"],
        ["20240301000000", "http://name@example.com/", "200", "text/html", "AAA", "11"],
        ["20240401000000", "https://example.com/", "200", "text/html", "BBB", "21"],
    ])
    alt_target, _ = archive.parse_target("example.com")
    client = httpx.Client(transport=transport([FIRST, ALT]))
    row, kind, reason = archive.read_history(client, alt_target, max_snapshots=20)
    check("alternating digests: the row is read", row is not None and kind == "")
    check("alternating digests: four captures are named as such", row and row["capturesRead"] == 4)
    check("alternating digests: they are two versions, not four",
          row and row["versionsReturned"] == 2)
    check("alternating digests: the newest capture of each digest wins",
          row and [v["timestamp"] for v in row["latestVersions"]] == ["20240301000000", "20240401000000"])
    check("alternating digests: the newest version is the last snapshot",
          row and row["lastSnapshotAt"] == "2024-04-01T00:00:00Z")

    print(f"pure: {PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print("  FAIL:", name)
        return 1
    return 0


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


if __name__ == "__main__":
    sys.exit(run_all())
