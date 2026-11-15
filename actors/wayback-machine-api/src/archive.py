"""Wayback Machine reader: the public CDX API of the Internet Archive.

No API key, no browser, no HTML parsing: the CDX endpoint answers JSON about what the
archive really holds. Two questions are asked per URL, the earliest snapshot and the newest
distinct versions (collapsed by content digest), which is what "was this page ever archived
and when did it change" actually means.

A URL the archive never saw answers an empty list, and that is a verdict about the URL, not
an error and not data: it comes back as a free row with a plain reason.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

CDX_URL = "https://web.archive.org/cdx/search/cdx"
TIMEOUT = 25.0
USER_AGENT = "apify-wayback-machine-api/0.1 (+https://apify.com/stas_k)"
FIELDS = "timestamp,original,statuscode,mimetype,digest,length"
MAX_SNAPSHOTS = 100
DATE_RE = re.compile(r"^\d{4}(\d{2}){0,5}$")
DEFAULT_PORTS = {"http": "80", "https": "443"}


@dataclass
class Target:
    """One URL the buyer asked about, normalized for the archive."""

    url: str
    host: str
    raw: str


def check_date(value: str) -> str:
    """The CDX date filter is YYYY, YYYYMM, YYYYMMDD or the full stamp.

    A filter the archive silently ignores would sell a whole history as if it were a
    window, so a malformed value is refused before the first request instead.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if not DATE_RE.match(text):
        raise ValueError(
            f"the date filter {text!r} is not YYYY, YYYYMM, YYYYMMDD or YYYYMMDDhhmmss")
    return text


def _public_host(host: str) -> tuple[bool, str]:
    """Whether an IP literal is a public address.

    The private ranges are refused because the actor would otherwise be asked to describe
    our own network: is_global is the check that also catches CGNAT and the IPv4 mapped
    forms that is_private lets through.
    """
    bare = host.strip("[]")
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        return True, ""
    if not address.is_global:
        return False, f"{bare} is a private address, so it is not a public web page"
    return True, ""


def parse_target(entry) -> tuple[Target | None, str]:
    """A URL, a bare host or a host with a path; None and a reason when it is neither."""
    raw = str(entry if entry is not None else "").strip()
    if not raw:
        return None, "the line is empty"
    text = raw if "://" in raw else "https://" + raw
    try:
        parts = urlsplit(text)
    except ValueError:
        return None, f"{raw!r} is not a URL the archive can look up"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None, (f"{raw!r} uses the scheme {scheme or 'none'}: the archive records web "
                      "pages, so send http or https")
    try:
        host = (parts.hostname or "").strip().lower()
    except ValueError:
        return None, f"{raw!r} carries a host name that cannot be read"
    if not host:
        return None, f"{raw!r} carries no host name"
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None, f"the host in {raw!r} cannot be turned into a DNS name"
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return None, f"{raw!r} points at a local machine, not at a public page"
    if "." not in host:
        ok, why = _public_host(host)
        if not ok:
            return None, why
        return None, (f"{raw!r} carries no dot in the host name, so it is not a public web "
                      "page")
    ok, why = _public_host(host)
    if not ok:
        return None, why
    try:
        port = parts.port
    except ValueError:
        # A port the parser refuses used to escape as a ValueError, kill the whole run and
        # throw away rows that were already read: one typo in a list of 200 URLs emptied the
        # dataset. It is a verdict about the line, so it comes back as one.
        return None, (f"{raw!r} carries a port that is not a number from 1 to 65535: "
                      "send the page without a port, or with a real one")
    netloc = host if port in (None, int(DEFAULT_PORTS[scheme])) else f"{host}:{port}"
    path = parts.path or "/"
    url = urlunsplit((scheme, netloc, path, parts.query, ""))
    return Target(url=url, host=host, raw=raw), ""


def snapshot_time(stamp: str) -> str:
    """20020120142510 into 2002-01-20T14:25:10Z, or an empty string when it is not a stamp."""
    text = str(stamp or "").strip()
    if len(text) != 14 or not text.isdigit():
        return ""
    return (f"{text[0:4]}-{text[4:6]}-{text[6:8]}T{text[8:10]}:{text[10:12]}:{text[12:14]}Z")


def _rows(text: str) -> list:
    """The CDX answer is a list of lists whose first line names the columns.

    An empty archive answers [] and a broken answer is not an empty archive: the caller
    has to tell "no snapshot" from "the archive did not answer".
    """
    body = (text or "").strip()
    if not body:
        return []
    if body == "[]":
        return []
    data = json.loads(body)
    if not isinstance(data, list) or not data:
        return []
    header = data[0]
    if not isinstance(header, list):
        return []
    out = []
    for line in data[1:]:
        if isinstance(line, list) and len(line) == len(header):
            out.append(dict(zip([str(name) for name in header], line)))
    return out


def _call(client, target: Target, *, limit, collapse: bool, from_: str, to: str,
          timeout: float | None):
    params = {"url": target.url, "output": "json", "fl": FIELDS, "limit": str(limit)}
    if collapse:
        params["collapse"] = "digest"
    if from_:
        params["from"] = from_
    if to:
        params["to"] = to
    headers = {"User-Agent": USER_AGENT}
    answer = client.get(CDX_URL, params=params, timeout=timeout, headers=headers)
    status = getattr(answer, "status_code", 200)
    if status != 200:
        raise RuntimeError(f"the archive answered HTTP {status}")
    return _rows(answer.text)


def read_history(client, target: Target, *, max_snapshots: int = 20, from_: str = "",
                 to: str = "", timeout: float | None = None):
    """One row of history for one URL, or a verdict with a reason.

    The first snapshot and the newest distinct versions are two questions, so they are two
    cheap calls. When the second one fails the row still carries the first snapshot, is
    marked partial and says so: a page that existed in 2004 is data even if the newest list
    could not be read.
    """
    max_snapshots = max(1, min(MAX_SNAPSHOTS, int(max_snapshots)))
    try:
        earliest = _call(client, target, limit=1, collapse=False, from_=from_, to=to,
                          timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return None, "error", f"the archive could not be read ({type(exc).__name__}): {exc}"
    if not earliest:
        window = ""
        if from_ or to:
            window = f" between {from_ or 'the beginning'} and {to or 'now'}"
        return None, "not_found", (
            f"the Internet Archive holds no snapshot of this URL{window}")
    first = earliest[0]
    partial = False
    note = ""
    snapshots = []
    # Columns collapse only neighbours, and two variants of one page alternate, so a limit of
    # max_snapshots came back as max_snapshots captures of two versions. Ask for more captures
    # and collapse by digest ourselves, otherwise "Versions" sells repeats as versions.
    wanted = min(MAX_SNAPSHOTS, max_snapshots * 3)
    try:
        newest = _call(client, target, limit=-wanted, collapse=True, from_=from_,
                       to=to, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        newest = []
        partial = True
        note = (f"the newest versions could not be read ({type(exc).__name__}): the row "
                "carries the earliest snapshot only")
    for item in newest:
        at = snapshot_time(item.get("timestamp", ""))
        if not at:
            continue
        try:
            length = int(item.get("length") or 0)
        except (TypeError, ValueError):
            length = 0
        snapshots.append({
            "at": at,
            "timestamp": str(item.get("timestamp", "")),
            "url": str(item.get("original", "")),
            "statusCode": str(item.get("statuscode", "")),
            "mimeType": str(item.get("mimetype", "")),
            "digest": str(item.get("digest", "")),
            "lengthBytes": length,
        })
    snaps = sorted(snapshots, key=lambda row: row["at"])
    captures = len(snaps)
    unique: dict = {}
    for item in snaps:
        # The newest capture of each digest wins: it is the version the page ended up in.
        unique[item["digest"] or f"at:{item['at']}"] = item
    versions = sorted(unique.values(), key=lambda row: row["at"])[-max_snapshots:]
    first_at = snapshot_time(first.get("timestamp", ""))
    last_at = versions[-1]["at"] if versions else first_at
    row = {
        "status": "OK",
        "billable": True,
        "url": target.url,
        "host": target.host,
        "firstSnapshotAt": first_at,
        "firstSnapshotUrl": str(first.get("original", "")),
        "firstSnapshotStatus": str(first.get("statuscode", "")),
        "lastSnapshotAt": last_at,
        "versionsReturned": len(versions),
        "capturesRead": captures,
        "windowFrom": from_,
        "windowTo": to,
        "latestVersions": versions,
        "partial": partial,
        "versionsNote": note,
        "source": "Internet Archive CDX API (keyless)",
    }
    return row, "", ""
