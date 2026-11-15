"""Personio Jobs API: read the public XML feed of a Personio career site.

The feed is the employer's own public ATS export: no key, no login, no proxy, one
request returns the whole board together with every description section.

One fact shapes this module: for a company that does not exist the feed answers
200 with an empty <workzag-jobs>, which looks exactly like a board with no open
positions. The career page does answer 404 in that case, so a board is only called
empty after the page has confirmed it exists.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

FEED_SUFFIXES = ("jobs.personio.de", "jobs.personio.com")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Names that belong to Personio itself, never to a customer board.
RESERVED = {"jobs", "www", "api", "app", "help", "status", "support", "docs"}
MAX_FEED_BYTES = 40 * 1024 * 1024
USER_AGENT = "personio-jobs-api/0.1"


class BoardError(Exception):
    """A board that was not read. kind is 'not_found' or 'error'."""

    def __init__(self, kind, reason):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def normalize_board(raw):
    """Turn what the buyer typed into a Personio board slug, or None when it is not one.

    Accepts 'ottonova', 'ottonova.jobs.personio.de' and
    'https://ottonova.jobs.personio.de/'. Anything else is a bad identifier, not a
    silent guess: guessing produced rows for the wrong company before.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text or any(char.isspace() for char in text):
        return None
    slug = text
    if "://" in text or "." in text:
        parsed = urlparse(text if "://" in text else "https://" + text)
        host = (parsed.hostname or "").lstrip(".").rstrip(".")
        matched = None
        for suffix in FEED_SUFFIXES:
            if host.endswith("." + suffix):
                matched = suffix
                break
        if matched is None:
            return None
        slug = host[: -(len(matched) + 1)]
    if not SLUG_RE.match(slug) or slug in RESERVED:
        return None
    return slug


def feed_url(slug, language=""):
    url = "https://%s.%s/xml" % (slug, FEED_SUFFIXES[0])
    if language:
        url += "?language=" + language
    return url


def page_url(slug):
    return "https://%s.%s/" % (slug, FEED_SUFFIXES[0])


def _request(url, timeout):
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})


def _read(url, timeout):
    """GET and return the body. HTTP 404 becomes a missing board, everything else an error."""
    try:
        with urllib.request.urlopen(_request(url, timeout), timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_FEED_BYTES:
                raise BoardError(
                    "error",
                    "the feed declares %s bytes, over the %d byte read limit" % (declared, MAX_FEED_BYTES),
                )
            body = response.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BoardError("not_found", "%s answers 404, so this company has no Personio board" % url)
        raise BoardError("error", "%s answers HTTP %d" % (url, exc.code))
    except BoardError:
        raise
    except Exception as exc:
        raise BoardError("error", "%s could not be read: %s" % (url, str(exc)[:160]))
    if len(body) > MAX_FEED_BYTES:
        # A cut feed parses as a truncated board, which is the same lie as an empty one.
        raise BoardError("error", "%s is larger than the %d byte read limit" % (url, MAX_FEED_BYTES))
    if not body.strip():
        raise BoardError("error", "%s answered an empty body" % url)
    return body


def _status(url, timeout):
    """Status code only: used to tell a missing board from a quiet one, no big body read."""
    try:
        with urllib.request.urlopen(_request(url, timeout), timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        raise BoardError("error", "%s could not be read: %s" % (url, str(exc)[:160]))


def _text(node):
    if node is None:
        return None
    value = node.text
    if value is None:
        return None
    value = value.strip()
    return value or None


def parse_feed(body):
    """XML bytes into plain dicts, so parsing can be tested without a network."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise BoardError("error", "the feed is not valid XML: %s" % exc)
    if root.tag != "workzag-jobs":
        raise BoardError("error", "the answer is not a Personio feed (root tag %r)" % root.tag)
    positions = []
    for position in root.findall("position"):
        sections = []
        for section in position.findall("jobDescriptions/jobDescription"):
            name = _text(section.find("name"))
            value = _text(section.find("value"))
            if name or value:
                sections.append({"name": name, "value": value})
        positions.append(
            {
                "id": _text(position.find("id")),
                "subcompany": _text(position.find("subcompany")),
                "office": _text(position.find("office")),
                "additionalOffices": [
                    office for office in (_text(node) for node in position.findall("additionalOffices/office"))
                    if office
                ],
                "department": _text(position.find("department")),
                "recruitingCategory": _text(position.find("recruitingCategory")),
                "name": _text(position.find("name")),
                "employmentType": _text(position.find("employmentType")),
                "seniority": _text(position.find("seniority")),
                "schedule": _text(position.find("schedule")),
                "keywords": _text(position.find("keywords")),
                "occupation": _text(position.find("occupation")),
                "occupationCategory": _text(position.find("occupationCategory")),
                "createdAt": _text(position.find("createdAt")),
                "sections": sections,
            }
        )
    return positions


def fetch_board(slug, language="", timeout=60):
    """Read one board. Returns {'jobs': [...], 'empty': bool}.

    Raises BoardError('not_found') when the company has no Personio board, and
    BoardError('error') when the board exists but could not be read today.
    """
    body = _read(feed_url(slug, language), timeout)
    positions = parse_feed(body)
    if positions:
        return {"jobs": positions, "empty": False}
    # An empty feed is ambiguous by design of the source: confirm with the career page.
    status = _status(page_url(slug), timeout)
    if status == 404:
        raise BoardError(
            "not_found",
            "%s.%s/xml is empty and the career page answers 404, so this company has no board"
            % (slug, FEED_SUFFIXES[0]),
        )
    if status >= 400:
        raise BoardError("error", "%s answers HTTP %d" % (page_url(slug), status))
    return {"jobs": [], "empty": True}


TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")


def strip_html(value):
    """Employer markup into readable text: tags out, entities decoded, whitespace collapsed."""
    if not value:
        return value
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</(p|div|li|ul|ol|h1|h2|h3|h4|tr)>", "\n", text)
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return BLANK_LINES_RE.sub("\n\n", text).strip()


def build_description(sections, description_format):
    """Join the sections the employer wrote, keeping their headings when asked for text."""
    if not sections:
        return None
    pieces = []
    for section in sections:
        name = (section.get("name") or "").strip()
        value = section.get("value") or ""
        if description_format == "text":
            value = strip_html(value)
            if name or value:
                pieces.append((name + "\n" + (value or "")).strip())
        else:
            if name or value:
                pieces.append(("<h3>%s</h3>\n%s" % (name, value)).strip() if name else value)
    joined = "\n\n".join(piece for piece in pieces if piece)
    return joined or None


REMOTE_HINTS = ("remote", "homeoffice", "home office", "home-office", "telearbeit")


def looks_remote(offices, description):
    """True when the board itself says the job can be done remotely.

    The Personio feed has no remote flag, so this reads the office names and the
    description instead of inventing a value.
    """
    for office in offices:
        low = (office or "").lower()
        if any(hint in low for hint in REMOTE_HINTS):
            return True
    head = (description or "")[:4000].lower()
    return any(hint in head for hint in REMOTE_HINTS)


def parse_job(entry, slug, include_details=True, description_format="html"):
    """One feed position into one dataset row, or None when it is not a position."""
    if not isinstance(entry, dict):
        return None
    job_id = str(entry.get("id") or "").strip()
    title = str(entry.get("name") or "").strip()
    if not job_id or not title:
        return None
    primary_office = (entry.get("office") or "").strip()
    offices = []
    if primary_office:
        offices.append(primary_office)
    for extra in entry.get("additionalOffices") or []:
        extra = (extra or "").strip()
        if extra and extra not in offices:
            offices.append(extra)
    raw_keywords = entry.get("keywords") or ""
    keywords = [word.strip() for word in raw_keywords.split(",") if word.strip()]
    sections = entry.get("sections") or []
    description = build_description(sections, description_format) if include_details else None
    # The remote hint lives in the employer's text, so it must not depend on includeDetails:
    # a display option must not change what the Actor decides about the same position.
    remote_text = description
    if remote_text is None and sections:
        remote_text = build_description(sections, "text")
    url = "%sjob/%s" % (page_url(slug), job_id)
    return {
        "status": "DELIVERED",
        "board": slug,
        "jobId": job_id,
        "title": title,
        "subcompany": entry.get("subcompany"),
        "department": entry.get("department"),
        "recruitingCategory": entry.get("recruitingCategory"),
        "employmentType": entry.get("employmentType"),
        "seniority": entry.get("seniority"),
        "schedule": entry.get("schedule"),
        "occupation": entry.get("occupation"),
        "occupationCategory": entry.get("occupationCategory"),
        "keywords": keywords,
        "city": offices[0] if offices else None,
        "locations": [{"city": office} for office in offices],
        "remote": looks_remote(offices, remote_text),
        "createdAt": entry.get("createdAt"),
        "url": url,
        "applyUrl": url,
        "description": description,
        "descriptionSections": (
            [{"name": section.get("name"), "value": section.get("value")} for section in entry.get("sections") or []]
            if include_details
            else None
        ),
        "reason": None,
        "delivered": True,
        "billable": None,
    }


def merge_positions(rows):
    """One posting is one row: a feed that repeats a position id is merged, not charged twice.

    Returns (rows, merged_count). Offices are united, the first row keeps its other fields.
    """
    order, merged = [], {}
    duplicates = 0
    for row in rows:
        job_id = row.get("jobId")
        if job_id in merged:
            kept = merged[job_id]
            cities = {location.get("city") for location in kept.get("locations") or []}
            for location in row.get("locations") or []:
                if location.get("city") not in cities:
                    kept.setdefault("locations", []).append(location)
                    cities.add(location.get("city"))
            duplicates += 1
            continue
        merged[job_id] = row
        order.append(job_id)
    return [merged[job_id] for job_id in order], duplicates


def matches(row, filters):
    """Filters run in the Actor, because the feed carries no server side search."""
    if filters.get("remoteOnly") and not row.get("remote"):
        return False
    for field, key in (
        ("department", "department"),
        ("category", "recruitingCategory"),
        ("employmentType", "employmentType"),
        ("seniority", "seniority"),
        ("occupationCategory", "occupationCategory"),
    ):
        needle = filters.get(field) or ""
        if needle and needle not in (row.get(key) or "").lower():
            return False
    city = filters.get("city") or ""
    if city:
        cities = [row.get("city") or ""] + [location.get("city") or "" for location in row.get("locations") or []]
        if not any(city in value.lower() for value in cities):
            return False
    keywords = filters.get("keywords") or []
    if keywords:
        haystack = " ".join(
            filter(
                None,
                [
                    row.get("title"),
                    row.get("department"),
                    row.get("occupation"),
                    row.get("recruitingCategory"),
                    ", ".join(row.get("keywords") or []),
                ],
            )
        ).lower()
        if not any(word in haystack for word in keywords):
            return False
    created_after = filters.get("createdAfter") or ""
    if created_after and (row.get("createdAt") or "") < created_after:
        return False
    return True


def sort_key(row):
    """Newest first: the feed only carries a creation date, so nothing else can be sorted on."""
    return str(row.get("createdAt") or "")
