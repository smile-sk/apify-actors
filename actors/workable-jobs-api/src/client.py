"""Workable board reader.

No Apify SDK and no module level state: every function here is pure, so the same code
can be measured offline against the real API before anything is pushed.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API_TEMPLATE = "https://apply.workable.com/api/v1/widget/accounts/{sub}?details=true"
# details=true is what puts the description into the answer; without it the same postings
# come back without that field. includeDetails only decides whether it lands in the dataset.
MAX_RESPONSE_BYTES = 40 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (compatible; apify-workable-jobs-api/1.0)"

_SUB_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_RESERVED = {"www", "apply", "jobs", "api", "workable"}

_TAGS = re.compile(r"<[^>]+>")
_BLOCKS = re.compile(r"(?i)</(?:p|div|li|ul|ol|h[1-6]|tr|table|section|article)\s*>|<br\s*/?>")


class BoardError(Exception):
    """A board could not be read. kind is 'not_found' or 'error'."""

    def __init__(self, kind, reason):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def normalize_board(raw):
    """Return the Workable subdomain for an identifier or URL, or None if it cannot be one.

    Accepts 'celsius', 'Celsius', 'https://apply.workable.com/celsius/',
    'celsius.workable.com' and 'https://celsius.workable.com/careers'.
    Refuses the Workable host itself and anything that is not a valid subdomain.
    """
    if raw is None:
        return None
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if not text:
        return None
    low = text.lower()

    bare = low.rstrip("/")
    if "://" in low or "/" in bare or low.startswith("www.") or low.endswith(".workable.com"):
        if "://" not in low:
            low = "https://" + low
        try:
            parts = urllib.parse.urlsplit(low)
        except ValueError:
            return None
        host = (parts.hostname or "").lower()
        if host.endswith(".workable.com"):
            sub = host[: -len(".workable.com")]
        elif host in ("workable.com", "www.workable.com"):
            sub = ""
        else:
            return None
        if not sub or sub in _RESERVED:
            segments = [p for p in parts.path.split("/") if p]
            sub = segments[0].lower() if segments else ""
        text = sub
    else:
        text = low

    text = text.strip().strip("/").lower()
    if not text or text in _RESERVED or not _SUB_RE.match(text):
        return None
    return text


def _http_get(url, timeout):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        body = response.read(MAX_RESPONSE_BYTES + 1)
    return status, body


def fetch_board(sub, timeout=60, attempts=2):
    """Read one board. Returns {'name', 'description', 'jobs'}; raises BoardError."""
    url = API_TEMPLATE.format(sub=urllib.parse.quote(sub, safe=""))
    last_reason = "no attempt was made"

    for attempt in range(attempts):
        try:
            status, body = _http_get(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise BoardError("not_found", "Workable has no board named %r (HTTP 404)" % sub)
            last_reason = "HTTP %s %s" % (exc.code, exc.reason)
            if (exc.code == 429 or 500 <= exc.code < 600) and attempt + 1 < attempts:
                time.sleep(2 + attempt * 3)
                continue
            raise BoardError("error", "the board answered %s" % last_reason)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_reason = str(getattr(exc, "reason", exc))
            if attempt + 1 < attempts:
                time.sleep(2 + attempt * 3)
                continue
            raise BoardError("error", "could not reach Workable: %s" % last_reason)

        if len(body) > MAX_RESPONSE_BYTES:
            raise BoardError("error", "the board answered with more than 40 MB, refusing to truncate it")
        if status != 200:
            raise BoardError("error", "the board answered HTTP %s" % status)
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            raise BoardError("error", "the board answered with something that is not JSON")
        if not isinstance(data, dict):
            raise BoardError("error", "the board answered with an unexpected JSON shape")
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            raise BoardError("error", "the board answered without a jobs list")
        return {
            "name": data.get("name") or "",
            "description": data.get("description") or "",
            "jobs": jobs,
        }

    raise BoardError("error", last_reason)


def _clean(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    text = value.strip()
    return text or None


def html_to_text(markup):
    """Strip tags, decode entities, collapse whitespace. Not a Markdown conversion."""
    if not markup:
        return None
    text = _BLOCKS.sub("\n", str(markup))
    text = _TAGS.sub(" ", text)
    text = html_mod.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


def parse_locations(job):
    locations = []
    for entry in job.get("locations") or []:
        if not isinstance(entry, dict):
            continue
        locations.append(
            {
                "country": _clean(entry.get("country")),
                "countryCode": _clean(entry.get("countryCode")),
                "city": _clean(entry.get("city")),
                "region": _clean(entry.get("region")),
            }
        )
    return locations


def parse_job(job, board, board_name=None, include_details=True, description_format="html"):
    """Turn one board entry into a row. Returns None when the entry is not a posting."""
    if not isinstance(job, dict):
        return None
    shortcode = _clean(job.get("shortcode")) or _clean(job.get("code"))
    title = _clean(job.get("title"))
    if not shortcode and not title:
        return None

    locations = parse_locations(job)
    primary = locations[0] if locations else {}

    description = _clean(job.get("description"))
    if description is not None:
        if not include_details:
            description = None
        elif description_format == "text":
            description = html_to_text(description)

    remote = job.get("telecommuting")
    return {
        "board": board,
        "boardName": _clean(board_name),
        "jobId": shortcode,
        "title": title,
        "department": _clean(job.get("department")),
        "function": _clean(job.get("function")),
        "industry": _clean(job.get("industry")),
        "education": _clean(job.get("education")),
        "experience": _clean(job.get("experience")),
        "employmentType": _clean(job.get("employment_type")),
        "remote": bool(remote) if isinstance(remote, bool) else None,
        "country": _clean(job.get("country")) or primary.get("country"),
        "city": _clean(job.get("city")) or primary.get("city"),
        "state": _clean(job.get("state")) or primary.get("region"),
        "locations": locations,
        "publishedOn": _clean(job.get("published_on")),
        "createdAt": _clean(job.get("created_at")),
        "url": _clean(job.get("url")),
        "applyUrl": _clean(job.get("application_url")),
        "description": description,
    }


_COUNTRY_ALIASES = {
    "uk": "gb", "usa": "us", "u.s.": "us", "u.s.a.": "us", "america": "us",
    "great britain": "gb", "england": "gb", "scotland": "gb", "wales": "gb",
    "holland": "nl", "uae": "ae", "south korea": "kr",
}


def country_matches(entry, wanted):
    """True when this location lies in the requested country.

    The ISO code is compared by equality only: 'in' is India's code but also sits inside
    Singapore, Indonesia and United Kingdom, so a substring test used to deliver and charge
    postings from other countries. A country name is compared on a word boundary and only
    when it is longer than a code, so 'india' does not match 'British Indian Ocean Territory'.
    """
    if not wanted:
        return True
    code = (entry.get("countryCode") or "").strip().lower()
    name = (entry.get("country") or "").strip().lower()
    alias = _COUNTRY_ALIASES.get(wanted, wanted)
    if code:
        # Both sides go through the alias table: a feed may send 'UK' where the ISO
        # code is 'GB', and the caller may ask for either spelling.
        codes = {code, _COUNTRY_ALIASES.get(code, code)}
        if wanted in codes or alias in codes:
            return True
    if len(wanted) <= 2 or not name:
        return False
    return re.search(r"(?<![a-z])" + re.escape(wanted) + r"(?![a-z])", name) is not None


def merge_postings(rows):
    """One row per posting: Workable splits a multi-city posting into one entry per city.

    The split entries carry the same shortcode and one location each, so without this merge
    the same posting is delivered (and charged) once per city. Merging happens before any
    filter or limit, otherwise maxJobsPerBoard counts cities instead of postings.
    Returns (rows, how many entries were merged away).
    """
    merged, order, duplicates = {}, [], 0
    for index, row in enumerate(rows):
        key = row.get("jobId") or "__row_%d" % index
        first = merged.get(key)
        if first is None:
            merged[key] = row
            order.append(key)
            continue
        duplicates += 1
        seen, combined = set(), []
        for entry in list(first.get("locations") or []) + list(row.get("locations") or []):
            signature = (
                entry.get("country"), entry.get("countryCode"),
                entry.get("city"), entry.get("region"),
            )
            if signature in seen:
                continue
            seen.add(signature)
            combined.append(entry)
        first["locations"] = combined
        if combined:
            primary = combined[0]
            first["country"] = first.get("country") or primary.get("country")
            first["city"] = first.get("city") or primary.get("city")
            first["state"] = first.get("state") or primary.get("region")
    return [merged[key] for key in order], duplicates


def sort_key(job, sort_by="published"):
    """Newest first; the job id makes the order stable when dates are equal."""
    field = "createdAt" if sort_by == "created" else "publishedOn"
    return (job.get(field) or "", job.get("jobId") or "")


def matches(job, filters):
    """Apply every filter in code: Workable has no server side filtering on the board call."""
    if filters.get("remoteOnly") and job.get("remote") is not True:
        return False
    for key, field in (
        ("department", "department"),
        ("function", "function"),
        ("industry", "industry"),
        ("employmentType", "employmentType"),
    ):
        wanted = filters.get(key) or ""
        if wanted and wanted not in (job.get(field) or "").lower():
            return False

    keywords = filters.get("keywords") or []
    if keywords:
        haystack = " ".join(
            str(job.get(field) or "") for field in ("title", "department", "function")
        ).lower()
        if not any(word in haystack for word in keywords):
            return False

    country = filters.get("country") or ""
    if country and not any(country_matches(entry, country) for entry in job.get("locations") or []):
        return False

    after = filters.get("publishedAfter") or ""
    if after and (job.get("publishedOn") or "") < after:
        return False
    return True
