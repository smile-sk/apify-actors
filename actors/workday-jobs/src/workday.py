"""Workday CXS client - pure logic, no Apify SDK, unit-testable.

Workday powers the career sites of thousands of large employers, and every one
of them serves the same public JSON endpoint:

    POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    GET  https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}

Two facts measured live on 04.11.2026 shape this module:

  * `limit` above 20 is rejected with HTTP 400, so listing is always paginated
    by 20 - a page of 20 is one request, and a 2000-job board is 100 of them;
  * the list endpoint carries no description and no real start date: only
    `postedOn` like "Posted Today" and a path. A description costs one more
    request per job (~0.7 s), which is why it is opt-in and capped.

Nothing here guesses a company's site name: a Workday host without a site
segment is reported back with a hint, never filled with a made-up default.
The same rule holds for the country: a location that never names its country
("Berlin") is not guessed into one.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

LIST_LIMIT = 20          # Workday answers 400 above this - measured, not a choice
LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9][a-z0-9\-]*)\.(?P<wd>wd\d+)\.myworkdayjobs\.com$", re.I)
DAYS_RE = re.compile(r"(\d+)\+?\s+days?", re.I)


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #
def parse_target(raw: str) -> dict:
    """Turn whatever the buyer pasted into tenant / wd / site / host.

    Accepted: full URL, host+path, 'tenant:site'. Anything else is refused with
    a reason instead of a silent empty result.
    """
    text = str(raw or "").strip()
    out = {"input": text, "tenant": None, "wd": None, "site": None,
           "host": None, "error": None, "hint": None}
    if not text:
        out["error"] = "EMPTY_INPUT"
        out["hint"] = "pass a Workday career site URL, e.g. "
        out["hint"] += "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
        return out

    # 'tenant:site' (no host) is checked before a scheme is added - otherwise
    # urlparse reads 'tenant' as the scheme and the pair is lost
    if "://" not in text and "/" not in text and text.count(":") == 1:
        tenant, _, site = text.partition(":")
        tenant, site = tenant.strip().lower(), site.strip()
        if tenant and site and "." not in tenant:
            out["tenant"] = tenant
            out["site"] = site
            out["error"] = "NEEDS_WD_NUMBER"
            out["hint"] = "the wd number is unknown - pass the full URL from the career page"
            return out

    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlparse(text)
    host = (parsed.hostname or "").lower()
    match = HOST_RE.match(host)
    if match:
        out["tenant"] = match.group("tenant").lower()
        out["wd"] = match.group("wd").lower()
        out["host"] = host
        site = _site_from_path(parsed.path)
        if site:
            out["site"] = site
        else:
            out["error"] = "NEEDS_CAREER_URL"
            out["hint"] = ("this host has no site segment - open the company's career page "
                           "and copy the full URL, e.g. "
                           "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
        return out

    out["error"] = "NOT_A_WORKDAY_URL"
    out["hint"] = "expected something like https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    return out


def _site_from_path(path: str) -> str | None:
    """First path segment that is not a locale, not a job link, not the CXS API."""
    parts = [urllib.parse.unquote(p) for p in (path or "").split("/") if p]
    for part in parts:
        if part.lower() in ("job", "jobs", "wday", "cxs", "details"):
            return None
        if LOCALE_RE.match(part):
            continue
        return part
    return None


def board_key(parsed: dict) -> tuple:
    """What a pasted line actually points at, not how it was typed."""
    return ("board", parsed.get("tenant") or "", (parsed.get("wd") or "").lower(),
            (parsed.get("site") or "").lower())


def prepare_targets(raws) -> dict:
    """Dedupe pasted boards by target, not by string.

    The same board is routinely pasted twice: Workday's address bar carries the
    locale (`/en-US/NVIDIAExternalCareerSite`), and people copy it both with and
    without that segment, with a trailing slash, or as host+path. Every spelling
    parses to the same tenant/wd/site, so it is read once and billed once.
    """
    pairs, seen, dropped = [], set(), []
    for raw in raws or []:
        parsed = parse_target(raw)
        key = ("raw", str(raw).strip().lower()) if parsed.get("error") else board_key(parsed)
        if key in seen:
            dropped.append(raw)
            continue
        seen.add(key)
        pairs.append((raw, parsed))
    return {"pairs": pairs, "dropped": dropped}


def job_key(job: dict) -> tuple:
    """Identity of one vacancy: the same job id on the same board, once."""
    return (str(job.get("tenant") or "").lower(), str(job.get("site") or "").lower(),
            str(job.get("jobId") or job.get("externalPath") or ""))


def api_base(tenant: str, wd: str, site: str) -> str:
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
def _request(url: str, payload: dict | None = None, timeout: int = 30) -> tuple[int, bytes]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "User-Agent": UA}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


RETRY_CODES = (429, 500, 502, 503, 504)
MAX_RETRY_WAIT = 30.0


def _retry_after(headers) -> float | None:
    """The wait the server asked for, in seconds, or None when it did not say.

    A fixed 1.5 s backoff against a `Retry-After: 20` is how a polite client
    turns a soft limit into a ban; a header value is the cheapest information
    this module ever gets, so it is read.
    """
    if not headers:
        return None
    value = str(headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, min(float(value), MAX_RETRY_WAIT))
    except ValueError:
        return None


def _request_retry(url: str, payload: dict | None = None, retries: int = 2) -> tuple[int, bytes]:
    last = (0, b"")
    for attempt in range(retries + 1):
        wait = None
        try:
            status, body = _request(url, payload)
            if status == 200:
                return status, body
            last = (status, body)
            if status not in RETRY_CODES:
                return last
        except urllib.error.HTTPError as exc:
            last = (exc.code, exc.read()[:400])
            wait = _retry_after(exc.headers)
            if exc.code not in RETRY_CODES:
                return last
        except Exception as exc:  # network hiccup - report, do not guess
            last = (0, str(exc).encode("utf-8")[:300])
        time.sleep(wait if wait is not None else 1.5 * (attempt + 1))
    return last


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #
def list_page(tenant: str, wd: str, site: str, offset: int, search_text: str = "") -> dict:
    """One page of 20 postings plus the board's own total."""
    url = api_base(tenant, wd, site) + "/jobs"
    status, body = _request_retry(url, {"appliedFacets": {}, "limit": LIST_LIMIT,
                                        "offset": offset, "searchText": search_text})
    if status != 200:
        return {"status": status, "error": f"HTTP_{status}", "jobs": [], "total": None,
                "detail": body.decode("utf-8", "replace")[:200]}
    try:
        data = json.loads(body)
    except Exception:
        return {"status": status, "error": "BAD_JSON", "jobs": [], "total": None,
                "detail": body[:200].decode("utf-8", "replace")}
    return {"status": status, "error": None, "total": int(data.get("total") or 0),
            "jobs": data.get("jobPostings") or []}


def list_jobs(tenant: str, wd: str, site: str, max_jobs: int = 200, page_delay: float = 0.15,
              search_text: str = "") -> dict:
    """Walk the board page by page until the cap or the end of the list.

    `search_text` is Workday's own server-side search: without it a keyword
    match deep in a 2000-job board would never be reached, because only the
    first `max_jobs` postings are ever listed.
    """
    first = list_page(tenant, wd, site, 0, search_text)
    if first["error"]:
        return {"error": first["error"], "detail": first.get("detail"),
                "total": None, "jobs": []}
    total = first["total"]
    jobs = list(first["jobs"])
    offset = len(jobs)
    while offset < min(total, max_jobs):
        page = list_page(tenant, wd, site, offset, search_text)
        if page["error"]:
            return {"error": page["error"], "detail": page.get("detail"),
                    "total": total, "jobs": jobs[:max_jobs], "partial": True}
        if not page["jobs"]:
            break
        jobs.extend(page["jobs"])
        offset += len(page["jobs"])
        time.sleep(page_delay)
    return {"error": None, "total": total, "jobs": jobs[:max_jobs],
            "truncated": total > min(total, max_jobs)}


# --------------------------------------------------------------------------- #
# detail
# --------------------------------------------------------------------------- #
def job_detail(tenant: str, wd: str, site: str, external_path: str) -> dict:
    url = api_base(tenant, wd, site) + (external_path or "")
    status, body = _request_retry(url)
    if status != 200:
        return {"error": f"HTTP_{status}"}
    try:
        data = json.loads(body)
    except Exception:
        return {"error": "BAD_JSON"}
    info = data.get("jobPostingInfo") or {}
    return {
        "error": None,
        "jobDescription": info.get("jobDescription"),
        "startDate": info.get("startDate"),
        "timeType": info.get("timeType"),
        "jobReqId": info.get("jobReqId"),
        "externalUrl": info.get("externalUrl"),
        "country": info.get("country"),
        "location": info.get("location"),
        "additionalLocations": info.get("additionalLocations"),
    }


# --------------------------------------------------------------------------- #
# normalisation and filters
# --------------------------------------------------------------------------- #
def _country_name(value) -> str | None:
    """Workday reports a country as a bare string or as {descriptor, id}."""
    if isinstance(value, dict):
        return value.get("descriptor") or value.get("id")
    return value or None


def normalize(posting: dict, tenant: str, wd: str, site: str, detail: dict | None = None) -> dict:
    path = posting.get("externalPath") or ""
    bullets = posting.get("bulletFields") or []
    job_id = (detail or {}).get("jobReqId") or (bullets[0] if bullets else None)
    if not job_id and path:
        job_id = path.rstrip("/").split("_")[-1]
    host = f"{tenant}.{wd}.myworkdayjobs.com"
    url = (detail or {}).get("externalUrl") or f"https://{host}/{site}{path}"
    posted_on = posting.get("postedOn")
    return {
        "jobId": job_id,
        "jobTitle": posting.get("title"),
        "location": (detail or {}).get("location") or posting.get("locationsText"),
        "additionalLocations": (detail or {}).get("additionalLocations"),
        "country": (canonical_country((detail or {}).get("country"))
                    or iso_country(location_country((detail or {}).get("location")
                                                    or posting.get("locationsText")))),
        "postedOn": posted_on,
        "postedDaysAgo": days_ago(posted_on),
        "startDate": (detail or {}).get("startDate"),
        "timeType": (detail or {}).get("timeType") or posting.get("timeType"),
        "remote": is_remote(posting.get("locationsText") or (detail or {}).get("location") or ""),
        "url": url,
        "externalPath": path,
        "tenant": tenant,
        "site": site,
        "jobDescription": None,
    }


def apply_detail(job: dict, detail: dict | None, with_description: bool = True) -> dict:
    """Fold a fetched job detail into an already normalised row.

    The description is written only when the caller asked for it: a country probe
    fetches the very same record, and a run with `includeDescription=false` must
    not deliver descriptions nobody ordered - nor count them as fetched.
    """
    if not detail or detail.get("error"):
        return job
    if with_description and detail.get("jobDescription") is not None:
        job["jobDescription"] = detail["jobDescription"]
    for field in ("startDate", "timeType", "additionalLocations", "location"):
        if detail.get(field):
            job[field] = detail[field]
    if detail.get("externalUrl"):
        job["url"] = detail["externalUrl"]
    # the probe is also what fills `country` for a row whose location named none
    country = (canonical_country(detail.get("country"))
               or iso_country(location_country(detail.get("location") or job.get("location"))))
    if country:
        job["country"] = country
    return job


def days_ago(posted_on) -> int | None:
    """'Posted Today' -> 0, 'Posted 10 Days Ago' -> 10, 'Posted 30+ Days Ago' -> 30."""
    if not posted_on:
        return None
    text = str(posted_on)
    if re.search(r"\btoday\b", text, re.I):
        return 0
    if re.search(r"\byesterday\b", text, re.I):
        return 1
    match = DAYS_RE.search(text)
    return int(match.group(1)) if match else None


def is_remote(location: str) -> bool:
    return bool(re.search(r"\bremote\b|\btelecommut|\bwork from home\b|\banywhere\b", location or "", re.I))


UNINFORMATIVE_LOCATION_RE = re.compile(
    r"^\s*\d+\s+locations?\b"                      # "5 Locations"
    r"|^\s*(?:multiple|various|several)\s+locations?\b"
    r"|^\s*(?:remote|anywhere)\s*$",                 # a bare "Remote"
    re.I)


def location_is_informative(job: dict) -> bool:
    """False when the listing says where the job is only vaguely ("5 Locations").

    Such a row may sit anywhere, so only its own detail record can tell - it must
    still be probed. A row that names a real city is decided by its own text.
    """
    text = str(job.get("location") or "").strip()
    if not text:
        return False
    return not UNINFORMATIVE_LOCATION_RE.match(text)


def _location_text(job: dict) -> str:
    """Every place the row names: its main location and any additional ones."""
    parts = [str(job.get("location") or "")]
    for value in _additional_locations(job):
        if isinstance(value, dict):
            value = value.get("descriptor") or value.get("name") or ""
        parts.append(str(value or ""))
    return " | ".join(p for p in parts if p)


def matches(job: dict, keywords: str, location: str) -> bool:
    """Every keyword must appear in the title or the place; place filter is a substring.

    The place filter reads additional locations too: a job advertised in three
    cities is open in every one of them, and reading only the first one hid those
    two thirds of the board from a city filter.
    """
    if keywords:
        haystack = f"{job.get('jobTitle') or ''} {_location_text(job)}".lower()
        for word in [w for w in re.split(r"[,\s]+", keywords.lower()) if w]:
            if word not in haystack:
                return False
    if location and location.lower() not in _location_text(job).lower():
        return False
    return True


# --------------------------------------------------------------------------- #
# cache identity
# --------------------------------------------------------------------------- #
CACHE_VERSION = "6"          # bump when the shape of a cached result changes


def cache_key(parsed: dict, include_desc: bool, max_jobs: int, keywords: str, location: str,
              countries=None) -> str:
    """Identity of a cached board answer.

    The cluster (`wd`) is part of the key: a tenant that moves from wd5 to wd1
    must not be served the old cluster's answer.
    """
    import hashlib
    raw = "|".join([CACHE_VERSION, parsed.get("tenant") or "", parsed.get("wd") or "",
                    parsed.get("site") or "",
                    "d" if include_desc else "l", str(max_jobs), keywords.lower(), location.lower(),
                    ",".join(sorted(str(c).lower() for c in (countries or [])))])
    return "wd_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# country matching
# --------------------------------------------------------------------------- #
# Workday writes a location in a handful of shapes, all of them measured on
# live boards:
#   "US, CA, Santa Clara"        country code first, then state, then city
#   "Plano, TX"                  city, then a US state code
#   "San Francisco, California"  city, then the state name
#   "Toronto, ON"                city, then a Canadian province
#   "Sao Paulo, Brazil"          city, then the country name
#   "United Kingdom - London"    country, dash, city
#   "5 Locations"                an aggregate: the country lives only in the
#                                detail record, so the row must be probed, not
#                                guessed and not silently dropped
#
# A trailing two-letter segment is a US state or a Canadian province, NOT a
# country: "CA" in "San Francisco, CA" is California. Reading it as Canada
# charged a buyer for California jobs on a "Canada" filter, and the mirror case
# ("Plano, TX" under a "US" filter) returned nothing at all.
US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
    "pr": "puerto rico",
}

CA_PROVINCES = {
    "ab": "alberta", "bc": "british columbia", "mb": "manitoba",
    "nb": "new brunswick", "nl": "newfoundland and labrador", "ns": "nova scotia",
    "nt": "northwest territories", "nu": "nunavut", "on": "ontario",
    "pe": "prince edward island", "qc": "quebec", "sk": "saskatchewan",
    "yt": "yukon",
}

# a tail segment may carry the code ("Plano, TX") or the full name
# ("San Francisco, California") - both mean the same country
US_STATE_TOKENS = set(US_STATES) | set(US_STATES.values())
CA_PROVINCE_TOKENS = set(CA_PROVINCES) | set(CA_PROVINCES.values())

ALIASES = {
    "united states": ["us", "usa", "u.s.", "united states of america", "america"],
    "united kingdom": ["uk", "gb", "great britain", "england", "scotland", "wales"],
    "germany": ["de", "deutschland"],
    "netherlands": ["nl", "the netherlands"],
    "france": ["fr"],
    "spain": ["es", "espana"],
    "italy": ["it", "italia"],
    "poland": ["pl", "polska"],
    "ukraine": ["ua", "ukraina"],
    "canada": ["ca"],
    "australia": ["au"],
    "india": ["in"],
    "china": ["cn", "prc"],
    "japan": ["jp"],
    "korea": ["kr", "south korea", "republic of korea"],
    "brazil": ["br", "brasil"],
    "mexico": ["mx"],
    "ireland": ["ie"],
    "israel": ["il"],
    "singapore": ["sg"],
    "switzerland": ["ch", "schweiz", "suisse"],
    "sweden": ["se", "sverige"],
    "norway": ["no", "norge"],
    "denmark": ["dk", "danmark"],
    "finland": ["fi", "suomi"],
    "austria": ["at", "osterreich"],
    "belgium": ["be", "belgie", "belgique"],
    "portugal": ["pt"],
    "czechia": ["cz", "czech republic"],
    "romania": ["ro"],
    "turkey": ["tr", "turkiye"],
    "uae": ["ae", "united arab emirates", "dubai"],
    "south africa": ["za"],
    "argentina": ["ar"],
    "colombia": ["co"],
    "chile": ["cl"],
    "new zealand": ["nz"],
    "kazakhstan": ["kz"],
    "serbia": ["rs"],
    "bulgaria": ["bg"],
    "hungary": ["hu"],
    "greece": ["gr"],
    "slovakia": ["sk"],
    "lithuania": ["lt"],
    "latvia": ["lv"],
    "estonia": ["ee"],
}


def _alias_index() -> dict:
    """alias -> every canonical country name it can stand for."""
    index: dict[str, set] = {}
    for name, aliases in ALIASES.items():
        for alias in [name] + list(aliases):
            index.setdefault(alias, set()).add(name)
    return index


_ALIAS_INDEX = _alias_index()

# ISO 3166-1 alpha-2 for every country this module can name. The `country`
# output field is a CODE, not a name: the same country used to appear as
# "United States of America" on one row and "united states" on the next, which
# makes the column worthless to join on - and the README promised a code all
# along. A name we do not recognise is passed through unchanged, never dropped.
ISO_BY_COUNTRY = {
    "united states": "US", "united kingdom": "GB", "germany": "DE",
    "netherlands": "NL", "france": "FR", "spain": "ES", "italy": "IT",
    "poland": "PL", "ukraine": "UA", "canada": "CA", "australia": "AU",
    "india": "IN", "china": "CN", "japan": "JP", "korea": "KR",
    "brazil": "BR", "mexico": "MX", "ireland": "IE", "israel": "IL",
    "singapore": "SG", "switzerland": "CH", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "austria": "AT", "belgium": "BE",
    "portugal": "PT", "czechia": "CZ", "romania": "RO", "turkey": "TR",
    "uae": "AE", "south africa": "ZA", "argentina": "AR", "colombia": "CO",
    "chile": "CL", "new zealand": "NZ", "kazakhstan": "KZ", "serbia": "RS",
    "bulgaria": "BG", "hungary": "HU", "greece": "GR", "slovakia": "SK",
    "lithuania": "LT", "latvia": "LV", "estonia": "EE",
}
ISO_CODES = set(ISO_BY_COUNTRY.values())


def iso_country(value) -> str | None:
    """A country written any way at all -> its ISO code, or None when unknown.

    'United States of America', 'united states', 'USA' and 'US' all give 'US'.
    """
    text = str(_country_name(value) or "").strip().lower()
    if not text:
        return None
    if len(text) == 2 and text.upper() in ISO_CODES:
        return text.upper()
    names = _ALIAS_INDEX.get(text)
    if names and len(names) == 1:
        return ISO_BY_COUNTRY.get(next(iter(names)))
    for name, code in ISO_BY_COUNTRY.items():
        if name == text:
            return code
    for name, code in ISO_BY_COUNTRY.items():
        if re.search(rf"\b{re.escape(name)}\b", text):
            return code
    return None


def canonical_country(value) -> str | None:
    """One spelling for the output: a code when we know the country, else its own text."""
    text = _country_name(value)
    if not text or not str(text).strip():
        return None
    return iso_country(text) or str(text).strip()


# a token that stands for exactly one country can decide a location on its own;
# a token shared by two countries is left to the state/province rules
COUNTRY_BY_TOKEN = {token: next(iter(names)) for token, names in _ALIAS_INDEX.items()
                    if len(names) == 1}

# Codes that are BOTH a state/province code and a country code. Workday writes
# them either way and the string alone cannot tell which: "Bangalore, In" is
# India, "Fort Wayne, IN" is Indiana, "San Francisco, CA" is California, and
# "Bratislava, SK" is Slovakia. Reading them in table order charged a buyer for
# California jobs on a "Canada" filter and for Indiana jobs on a "US" filter.
# A tail like this names NO country by itself: the row is probed in its own
# detail record, which states the country, and is dropped - never guessed - if
# that record cannot be read.
AMBIGUOUS_TAILS = (set(US_STATE_TOKENS) | set(CA_PROVINCE_TOKENS)) & set(_ALIAS_INDEX)


def _tail_readings(token: str) -> set[str]:
    """Every country a tail token can legitimately stand for."""
    readings: set[str] = set()
    if token in US_STATE_TOKENS:
        readings.add("united states")
    if token in CA_PROVINCE_TOKENS:
        readings.add("canada")
    readings.update(_ALIAS_INDEX.get(token, ()))
    return readings

DASH_SEPARATORS = (" - ", " \u2013 ", " \u2014 ", " \u2013", " \u2014", " -", "- ")
REMOTE_RE = re.compile(r"\b(remote|home ?office|work from home|anywhere|hybrid)\b", re.I)


def _segments(text) -> list[str]:
    return [part.strip().lower() for part in str(text or "").split(",") if part.strip()]


def _clean(segment: str) -> str:
    return REMOTE_RE.sub(" ", segment).strip(" -\u2013\u2014()[]").strip()


def _token_country(token: str) -> str | None:
    """One token -> one canonical country, or None when it names none."""
    return COUNTRY_BY_TOKEN.get(token) if token else None


def location_country(location) -> str | None:
    """The country a location string actually names, or None - never a guess.

    Order of evidence: a country at the head ("US, CA, ..."), then a US state or
    Canadian province at the tail ("Plano, TX", "Toronto, ON"), then a country
    name at the tail ("Sao Paulo, Brazil"), then "Country - City" in any segment.

    A tail that reads both ways ("Bangalore, In", "San Francisco, CA") returns
    None on purpose: the row's own detail record decides, not the table order.
    """
    segments = _segments(location)
    if not segments:
        return None

    first = _clean(segments[0])
    country = _token_country(first)
    if country:
        return country

    if len(segments) >= 2:
        last = _clean(segments[-1])
        if last in AMBIGUOUS_TAILS:
            return None
        readings = _tail_readings(last)
        if readings:
            return next(iter(readings))

    for segment in segments:
        cleaned = _clean(segment)
        for separator in DASH_SEPARATORS:
            if separator in cleaned:
                left, _, right = cleaned.partition(separator)
                for part in (left.strip(), right.strip()):
                    country = _token_country(part)
                    if country:
                        return country

    for segment in segments[1:]:
        country = _token_country(_clean(segment))
        if country:
            return country
    return None


def _additional_locations(job: dict) -> list:
    extra = job.get("additionalLocations") or []
    if isinstance(extra, str):
        return [extra]
    return list(extra)


def implied_countries(job: dict) -> set[str]:
    """Canonical countries this row's own text states - never a guess."""
    names: set[str] = set()
    for value in [job.get("location")] + _additional_locations(job):
        if isinstance(value, dict):
            value = value.get("descriptor") or value.get("name") or ""
        country = location_country(value)
        if country:
            names.add(country)
    descriptor = str(_country_name(job.get("country")) or "").strip().lower()
    if descriptor:
        names.add(descriptor)
        names.update(_ALIAS_INDEX.get(descriptor, ()))
        for name in ALIASES:
            if name in descriptor:
                names.add(name)
    return names


def needs_country_probe(job: dict) -> bool:
    """True when the row names no country, so only its detail record can tell.

    "5 Locations" and a bare city are the two live shapes. Such a row is probed
    (one extra request) before a country filter decides its fate - dropped only
    if the detail names no country either.
    """
    return not implied_countries(job)


def wanted_countries(wanted) -> set[str]:
    want: set[str] = set()
    for token in [str(w).strip().lower() for w in (wanted or []) if str(w).strip()]:
        want.add(token)
        want.update(_ALIAS_INDEX.get(token, ()))
    return want


def country_matches(job: dict, wanted) -> bool:
    """True when the job sits in any of the wanted countries (empty = all).

    A wanted country expands to every spelling we know, and the job offers only
    the countries its own location or detail actually names. No name in the
    location means no match - a row is never charged on a guess.
    """
    want = wanted_countries(wanted)
    if not want:
        return True
    return bool(want & implied_countries(job))
