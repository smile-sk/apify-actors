"""Recruitee Jobs API: read the public JSON of a Recruitee career site.

The feed is the employer's own public ATS export: no key, no login, no proxy, one
request returns the whole board together with every description and requirement.

Three measured facts shape this module:
  * the list endpoint ignores ?page= and ?limit= (same bytes, same first id), so the
    board arrives in one answer and nothing can be silently left for "page 2";
  * 27% of live offers carry more than one office (ggzbreburg 34 of 124), and the feed
    already returns them as one record with a locations array - so one offer is one
    row and nobody can be charged twice for the same vacancy;
  * a company that does not exist answers 404 on the very endpoint, so "no such board"
    and "a board with no openings" are told apart by the status, not by guesswork.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

HOST_SUFFIX = "recruitee.com"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Names that belong to Recruitee itself, never to a customer board.
RESERVED = {"www", "api", "app", "help", "status", "support", "docs", "careers", "blog"}
MAX_FEED_BYTES = 40 * 1024 * 1024
USER_AGENT = "recruitee-jobs-api/0.1"


class BoardError(Exception):
    """A board that was not read. kind is 'not_found' or 'error'."""

    def __init__(self, kind, reason):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def normalize_board(raw):
    """Turn what the buyer typed into a Recruitee board slug, or None when it is not one.

    Accepts 'bunq', 'bunq.recruitee.com', 'https://bunq.recruitee.com/',
    'https://bunq.recruitee.com/api/offers/' and 'careers.bunq.com' style hosts are
    rejected: a host we do not know is a bad identifier, not a guess.
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
        if not host.endswith("." + HOST_SUFFIX):
            return None
        slug = host[: -(len(HOST_SUFFIX) + 1)]
        if slug.startswith("www."):
            slug = slug[4:]
        if "." in slug:
            # api.bunq.recruitee.com is not a tenant we read
            return None
    if not SLUG_RE.match(slug) or slug in RESERVED:
        return None
    return slug


def feed_url(slug):
    return "https://%s.%s/api/offers/" % (slug, HOST_SUFFIX)


def _request(url):
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})


def _read(url, timeout):
    """GET and return the body. HTTP 404 becomes a missing board, everything else an error."""
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_FEED_BYTES:
                raise BoardError(
                    "error",
                    "the feed declares %s bytes, over the %d byte read limit" % (declared, MAX_FEED_BYTES),
                )
            body = response.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BoardError("not_found", "%s answers 404, so this company has no Recruitee board" % url)
        raise BoardError("error", "%s answers HTTP %d" % (url, exc.code))
    except BoardError:
        raise
    except Exception as exc:
        raise BoardError("error", "%s could not be read: %s" % (url, str(exc)[:160]))
    if len(body) > MAX_FEED_BYTES:
        # A cut answer parses as a shorter board, which is the same lie as an empty one.
        raise BoardError("error", "%s is larger than the %d byte read limit" % (url, MAX_FEED_BYTES))
    if not body.strip():
        raise BoardError("error", "%s answered an empty body" % url)
    return body


def parse_offers(body):
    """JSON bytes into a list of raw offer dicts, so parsing can be tested without a network."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BoardError("error", "the answer is not JSON: %s" % exc)
    if not isinstance(payload, dict):
        raise BoardError("error", "the answer is not a Recruitee object (got %s)" % type(payload).__name__)
    offers = payload.get("offers")
    if not isinstance(offers, list):
        raise BoardError("error", "the answer carries no offers list (keys: %s)" % ",".join(sorted(payload)[:6]))
    return offers


def fetch_board(slug, timeout=60):
    """Read one board. Returns {'jobs': [...]}.

    Raises BoardError('not_found') when the company has no Recruitee board, and
    BoardError('error') when the board exists but could not be read today. An empty
    offers list is a real board with no open positions: the source already answered
    200 for a company that exists.
    """
    offers = parse_offers(_read(feed_url(slug), timeout))
    return {"jobs": offers, "empty": not offers}


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


MONEY_RE = re.compile(r"[^0-9,.\-]")


def parse_money(value):
    """Salary as a number, or None. '1,299.00' is 1299.0, '1.299,00' is 1299.0, '1,299' is 1299.

    A naive float() turned '1,299' into 1.299 and '1,299.00' into nothing at all, and a
    price field that quietly changes by a thousand times is worse than an empty field.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = MONEY_RE.sub("", str(value)).strip()
    if not text or text in ("-", ".", ","):
        return None
    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        head, _, tail = text.rpartition(",")
        # '1,299' and '1,299,500' are thousands; '1,29' is a decimal comma
        if len(tail) == 3 and head and head.replace("-", "").isdigit():
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _clean(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def locations_of(entry):
    """Every office of one offer, in the order the employer listed them.

    The feed already sends several locations as one array inside one offer, which is why
    this never splits an offer into several rows.
    """
    offices, seen = [], set()
    raw = entry.get("locations")
    if isinstance(raw, list):
        for location in raw:
            if not isinstance(location, dict):
                continue
            city = _clean(location.get("city")) or _clean(location.get("name"))
            country = _clean(location.get("country"))
            code = _clean(location.get("country_code"))
            state = _clean(location.get("state"))
            key = (city, state, country, code)
            if key in seen or not any(key):
                continue
            seen.add(key)
            offices.append(
                {"city": city, "state": state, "country": country, "countryCode": code,
                 "name": _clean(location.get("name")), "postalCode": _clean(location.get("postal_code"))}
            )
    if not offices:
        city = _clean(entry.get("city"))
        country = _clean(entry.get("country"))
        code = _clean(entry.get("country_code"))
        state = _clean(entry.get("state_name"))
        if city or country or code:
            offices.append({"city": city, "state": state, "country": country, "countryCode": code,
                            "name": _clean(entry.get("location")), "postalCode": _clean(entry.get("postal_code"))})
    return offices


def parse_job(entry, slug, include_details=True, description_format="html"):
    """One offer into one dataset row, or None when it is not an offer."""
    if not isinstance(entry, dict):
        return None
    job_id = str(entry.get("id") or "").strip()
    title = _clean(entry.get("title"))
    if not job_id or not title:
        return None
    offices = locations_of(entry)
    primary = offices[0] if offices else {}
    tags = entry.get("tags")
    tags = [str(tag).strip() for tag in tags if str(tag).strip()] if isinstance(tags, list) else []
    description = requirements = None
    if include_details:
        description = _clean(entry.get("description"))
        requirements = _clean(entry.get("requirements"))
        if description_format == "text":
            description = strip_html(description) if description else None
            requirements = strip_html(requirements) if requirements else None
    salary = entry.get("salary") if isinstance(entry.get("salary"), dict) else {}
    return {
        "status": "DELIVERED",
        "board": slug,
        "jobId": job_id,
        "title": title,
        "department": _clean(entry.get("department")),
        "location": _clean(entry.get("location")),
        "city": primary.get("city"),
        "state": primary.get("state"),
        "country": primary.get("country"),
        "countryCode": primary.get("countryCode"),
        "locations": offices,
        "remote": entry.get("remote") is True,
        "hybrid": entry.get("hybrid") is True,
        "onSite": entry.get("on_site") is True,
        "employmentType": _clean(entry.get("employment_type_code")),
        "experience": _clean(entry.get("experience_code")),
        "education": _clean(entry.get("education_code")),
        "category": _clean(entry.get("category_code")),
        "salaryMin": parse_money(salary.get("min")),
        "salaryMax": parse_money(salary.get("max")),
        "salaryCurrency": _clean(salary.get("currency")),
        "salaryPeriod": _clean(salary.get("period")),
        "tags": tags,
        "publishedAt": _clean(entry.get("published_at")),
        "url": _clean(entry.get("careers_url")),
        "applyUrl": _clean(entry.get("careers_apply_url")),
        "description": description,
        "requirements": requirements,
        "reason": None,
        "delivered": True,
        "billable": None,
    }


def merge_positions(rows):
    """One offer is one row: an offer id that repeats is merged, never charged twice.

    Returns (rows, merged_count). Offices are united, the first row keeps its other fields.
    """
    order, merged = [], {}
    duplicates = 0
    for row in rows:
        job_id = row.get("jobId")
        if job_id in merged:
            kept = merged[job_id]
            seen = {
                (location.get("city"), location.get("state"), location.get("country"), location.get("countryCode"))
                for location in kept.get("locations") or []
            }
            for location in row.get("locations") or []:
                key = (location.get("city"), location.get("state"), location.get("country"), location.get("countryCode"))
                if key not in seen:
                    kept.setdefault("locations", []).append(location)
                    seen.add(key)
            duplicates += 1
            continue
        merged[job_id] = row
        order.append(job_id)
    return [merged[job_id] for job_id in order], duplicates


def matches(row, filters):
    """Filters run in the Actor, because the offers endpoint carries no server side search."""
    if filters.get("remoteOnly") and row.get("remote") is not True:
        return False
    for field, key in (
        ("department", "department"),
        ("employmentType", "employmentType"),
        ("experience", "experience"),
        ("category", "category"),
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
            filter(None, [row.get("title"), row.get("department"), row.get("category"),
                          ", ".join(row.get("tags") or [])])
        ).lower()
        if not any(word in haystack for word in keywords):
            return False
    published_after = filters.get("publishedAfter") or ""
    if published_after and str(row.get("publishedAt") or "") < published_after:
        return False
    return True


def sort_key(row):
    """Newest first: the feed carries a publish date, so that is what we sort on."""
    return str(row.get("publishedAt") or "")
