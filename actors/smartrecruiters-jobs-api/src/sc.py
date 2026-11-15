"""SmartRecruiters job board reader: open postings, filters and job descriptions.

Why a separate Actor. The SmartRecruiters posting list is open JSON that answers a
plain HTTP request from a data centre, and one large board carries thousands of open
roles. The shared company-jobs Actor speaks this API too, but it stops at 500
postings and never reads a job description; this module reads the whole board, uses
only the filters the API really applies, and can fetch the description of jobs it
delivers.

Measured on 2026-11-07 against companies/SGS (4 632 open postings):
* limit=100 is the ceiling: limit=200 is answered with 100 rows;
* offset works far beyond 500 (offset=4500 answered 100 rows), so the 500 cap that
  other Actors carry is artificial;
* q=, country= (lowercase ISO-2) and releasedAfter= are applied by the server
  (q=geologo gave 6 rows out of 4 632, country=pe gave 611, releasedAfter gave 296);
* function=, typeOfEmployment=, experienceLevel= and department= are IGNORED
  silently: totals do not change, department= even answers HTTP 400. This module
  therefore never sends them and filters in code, because a filter that is silently
  dropped makes the buyer pay for rows he excluded;
* a board nobody owns answers HTTP 200 with totalFound=0, exactly like a board with
  no open roles. The public page title separates the two: "Careers at SGS" for a
  real board, "SmartRecruiters Job Search" for an identifier nobody owns;
* the posting detail carries jobAd.sections with jobDescription, qualifications,
  additionalInformation and companyDescription, about 5 KB, 0.2 s per request.

Nothing here imports the Apify SDK: the module can be measured locally.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
API = "https://api.smartrecruiters.com"
SITE = "https://jobs.smartrecruiters.com"
PAGE = 100          # measured: the API answers at most 100 rows per page
MAX_PAGES = 80      # 8 000 postings per company, a guard, not a product limit
MAX_BODY = 40 * 1024 * 1024
TIMEOUT = 25
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ScError(Exception):
    """A request that did not produce JSON. `status` is the HTTP code when there was one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Client:
    """HTTP with a polite pace, a request counter and one retry on 429 and 5xx."""

    def __init__(self, pace: float = 0.3, timeout: int = TIMEOUT):
        self.pace = max(0.0, float(pace))
        self.timeout = timeout
        self.requests = 0
        self._last = 0.0

    def _wait(self) -> None:
        if self.pace:
            gap = time.monotonic() - self._last
            if gap < self.pace:
                time.sleep(self.pace - gap)
        self._last = time.monotonic()

    def get(self, url: str) -> tuple[int, bytes]:
        self._wait()
        self.requests += 1
        request = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(MAX_BODY)
        except urllib.error.HTTPError as exc:
            return exc.code, b""
        except urllib.error.URLError as exc:
            raise ScError("connection failed: %s" % (exc.reason,)) from exc
        except Exception as exc:  # noqa: BLE001 - a timeout or a socket error
            raise ScError("%s: %s" % (type(exc).__name__, exc)) from exc

    def get_json(self, url: str):
        last = "no answer"
        for attempt in (1, 2):
            status, body = self.get(url)
            if status == 429 or 500 <= status < 600:
                last = "HTTP %s" % status
                if attempt == 1:
                    time.sleep(3)
                    continue
            if status != 200:
                raise ScError("HTTP %s" % status, status)
            try:
                return json.loads(body.decode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001
                raise ScError("the answer is not JSON: %s" % exc, status) from exc
        raise ScError(last)


def parse_company(raw: str) -> dict:
    """'SGS', 'jobs.smartrecruiters.com/SGS', 'https://jobs.smartrecruiters.com/SGS/744' -> SGS.

    An identifier is what the API calls the company in its own URLs. A full careers
    URL is accepted because that is what a buyer has in his hands; the first path
    segment is the identifier and the rest is a posting id we do not need.
    """
    text = (raw or "").strip()
    if not text:
        return {"raw": raw, "identifier": None, "error": "EMPTY"}
    candidate = text
    if "://" in candidate:
        parts = urllib.parse.urlsplit(candidate)
        path = [seg for seg in parts.path.split("/") if seg]
        candidate = path[0] if path else ""
    elif "/" in candidate:
        segments = [seg for seg in candidate.split("/") if seg]
        # "jobs.smartrecruiters.com/Ubisoft2" is a host and a path: the identifier is
        # the segment after the host, not the host itself
        if len(segments) > 1 and "." in segments[0]:
            candidate = segments[1]
        else:
            candidate = segments[0]
    candidate = urllib.parse.unquote(candidate).strip()
    if candidate.lower().rstrip("/") in ("smartrecruiters.com", "www.smartrecruiters.com",
                                         "jobs.smartrecruiters.com"):
        # measured 07.11.2026: the host alone was read as an identifier and came back as
        # "board does not exist", which blames the wrong thing
        return {"raw": raw, "identifier": None, "error": "INVALID_IDENTIFIER"}
    if not candidate or not IDENTIFIER.match(candidate):
        return {"raw": raw, "identifier": None, "error": "INVALID_IDENTIFIER"}
    return {"raw": raw, "identifier": candidate, "error": None}


def board_name(client: Client, identifier: str) -> str | None:
    """The company name when the board exists, None when nobody owns that identifier.

    Measured 2026-11-07: both a real board and an unknown identifier answer HTTP 200
    with totalFound=0, so the empty answer alone cannot tell "this company has no
    open roles" from "this company does not exist". The page title can: a real board
    renders "Careers at <name>", an unknown identifier renders the generic
    "SmartRecruiters Job Search".
    """
    try:
        status, body = client.get("%s/%s" % (SITE, urllib.parse.quote(identifier, safe="")))
    except ScError:
        return None
    if status != 200:
        return None
    text = body.decode("utf-8", "replace")
    match = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
    if not match:
        return None
    title = html.unescape(match.group(1)).strip()
    if not title.lower().startswith("careers at "):
        return None
    name = title[len("careers at "):].strip()
    return name or identifier


def list_postings(client: Client, identifier: str, *, query: str = "", country: str = "",
                  released_after: str = "", max_jobs: int = 0,
                  max_pages: int = MAX_PAGES) -> dict:
    """Every open posting of one board, newest first as the API returns them.

    `truncated` is set with a reason when the list is not the whole board: a page
    that failed after the first one succeeded, maxJobsPerCompany or the page guard.
    A page that fails before anything was read raises, so the caller reports a board
    it could not read instead of an empty board.
    """
    postings: list[dict] = []
    seen_ids: set[str] = set()
    duplicates = 0
    pages = 0
    failed = 0
    offset = 0
    total = None
    truncated = False
    reason = None
    error = None
    while True:
        params = {"limit": PAGE, "offset": offset}
        if query:
            params["q"] = query
        if country:
            params["country"] = country
        if released_after:
            params["releasedAfter"] = released_after
        url = "%s/v1/companies/%s/postings?%s" % (
            API, urllib.parse.quote(identifier, safe=""), urllib.parse.urlencode(params))
        try:
            data = client.get_json(url)
        except ScError as exc:
            failed += 1
            if not postings:
                raise
            truncated = True
            reason = "page %d failed: %s" % (pages + 1, exc)
            error = str(exc)
            break
        pages += 1
        if total is None:
            total = int(data.get("totalFound") or 0)
        content = data.get("content") or []
        if not content:
            break
        for post in content:
            job_id = str(post.get("id") or post.get("uuid") or "")
            if job_id and job_id in seen_ids:
                # Measured 07.11.2026: offset paging answered the same posting twice.
                # A duplicate row is a duplicate charge, so it is dropped and counted
                # instead of being delivered as a second posting.
                duplicates += 1
                continue
            if job_id:
                seen_ids.add(job_id)
            postings.append(post)
        offset += PAGE
        if max_jobs and len(postings) >= max_jobs:
            # The API answers a page of 100 whatever we ask for, so a limit below a page
            # used to deliver the whole page: asked for 5, got 100 and paid for 100.
            # Measured 07.11.2026 on the local run before this line existed.
            if len(postings) > max_jobs:
                postings = postings[:max_jobs]
            truncated = len(postings) < (total or 0)
            if truncated:
                reason = "maxJobsPerCompany"
            break
        if offset >= (total or 0):
            break
        if pages >= max_pages:
            truncated = True
            reason = "maxPages"
            break
    return {"postings": postings, "total": total if total is not None else len(postings),
            "pages": pages, "pagesFailed": failed, "truncated": truncated,
            "truncatedReason": reason, "error": error, "duplicates": duplicates}


def fetch_detail(client: Client, identifier: str, job_id: str) -> dict:
    """The job description block of one posting. Raises ScError when it cannot be read."""
    url = "%s/v1/companies/%s/postings/%s" % (
        API, urllib.parse.quote(identifier, safe=""), urllib.parse.quote(str(job_id), safe=""))
    data = client.get_json(url)
    sections = ((data.get("jobAd") or {}).get("sections") or {})
    return {
        "jobDescription": _strip_html((sections.get("jobDescription") or {}).get("text")),
        "qualifications": _strip_html((sections.get("qualifications") or {}).get("text")),
        "additionalInformation": _strip_html((sections.get("additionalInformation") or {}).get("text")),
        "companyDescription": _strip_html((sections.get("companyDescription") or {}).get("text")),
        "applyUrl": data.get("applyUrl"),
        "postingUrl": data.get("postingUrl"),
    }


def _strip_html(text) -> str | None:
    if not text or not isinstance(text, str):
        return None
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _label(node) -> str | None:
    """SmartRecruiters wraps almost every vocabulary field as {'id': ..., 'label': ...}."""
    if isinstance(node, dict):
        label = node.get("label") or node.get("name")
        return str(label) if label else None
    if isinstance(node, str) and node.strip():
        return node.strip()
    return None


def _compensation(node, depth: int = 0) -> dict:
    """Salary when the employer publishes one, nulls when he does not.

    Measured 2026-11-07: most boards answer `compensation: null`. Guessing a salary
    from nothing would be the same lie as inventing a number, so an absent block
    stays null and the README says so.
    """
    out = {"salaryMin": None, "salaryMax": None, "salaryCurrency": None, "salaryPeriod": None}
    if depth > 4:
        return out
    if isinstance(node, dict):
        low = _float(node.get("minAmount", node.get("minimum")))
        high = _float(node.get("maxAmount", node.get("maximum")))
        if low is not None or high is not None:
            out["salaryMin"] = low
            out["salaryMax"] = high
            currency = node.get("currency") or node.get("currencyCode")
            period = node.get("period") or node.get("interval") or node.get("frequency")
            out["salaryCurrency"] = str(currency).upper() if currency else None
            out["salaryPeriod"] = str(period).upper() if period else None
            return out
        for value in node.values():
            found = _compensation(value, depth + 1)
            if found["salaryMin"] is not None or found["salaryMax"] is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _compensation(value, depth + 1)
            if found["salaryMin"] is not None or found["salaryMax"] is not None:
                return found
    return out


def normalize(post: dict, identifier: str, company_name: str | None = None) -> dict:
    """One posting from the list endpoint -> one flat row in our schema."""
    location = post.get("location") or {}
    city = location.get("city")
    region = location.get("region")
    country = (location.get("country") or "").upper() or None
    full = location.get("fullLocation") or ", ".join(
        part for part in (city, region) if part) or None
    salary = _compensation(post.get("compensation"))
    language = post.get("language")
    if isinstance(language, dict):
        language = language.get("code") or language.get("label")
    remote = bool(location.get("remote"))
    hybrid = bool(location.get("hybrid"))
    if not remote and re.search(r"(?i)\bremote\b|anywhere", full or ""):
        remote = True
    return {
        "status": "DELIVERED",
        "jobId": str(post.get("id") or post.get("uuid") or ""),
        "company": identifier,
        "companyName": company_name or _label(post.get("company")) or identifier,
        "jobTitle": post.get("name"),
        "department": _label(post.get("department")),
        "function": _label(post.get("function")),
        "industry": _label(post.get("industry")),
        "experienceLevel": _label(post.get("experienceLevel")),
        "employmentType": _label(post.get("typeOfEmployment")),
        "location": full,
        "city": city,
        "region": region,
        "countryCode": country,
        "address": location.get("address"),
        "postalCode": location.get("postalCode"),
        "latitude": _float(location.get("latitude")),
        "longitude": _float(location.get("longitude")),
        "remote": remote,
        "hybrid": hybrid,
        "refNumber": post.get("refNumber"),
        "releasedDate": post.get("releasedDate"),
        "language": language,
        "visibility": post.get("visibility"),
        "url": "%s/%s/%s" % (SITE, identifier, post.get("id")),
        **salary,
        "jobDescription": None,
        "qualifications": None,
        "additionalInformation": None,
        "companyDescription": None,
        "detailFetched": False,
        "detailError": None,
        "reason": None,
    }


def _loc_match(needle: str, haystack: str | None) -> bool:
    """'US' must not match 'Australia': the country code is compared as a whole word."""
    if not needle:
        return True
    hay = (haystack or "").lower()
    if needle in ("remote", "anywhere"):
        return needle in hay
    tokens = re.findall(r"[a-z]+", hay)
    if needle in tokens:
        return True
    if needle == "us" and "usa" in tokens:
        return True
    if needle == "us" and "united" in tokens and "states" in tokens:
        return True
    if needle == "uk" and "united" in tokens and "kingdom" in tokens:
        return True
    if len(needle) > 3:
        return needle in hay
    return False


def filter_reason(row: dict, settings: dict) -> str | None:
    """Why this posting is not the buyer's, or None when it is.

    Every filter here is applied in code. The API silently ignores function,
    typeOfEmployment and experienceLevel, so trusting it would deliver rows the
    buyer excluded and charge for them.
    """
    words = settings.get("keywords") or []
    excluded = settings.get("exclude_keywords") or []
    title = (row.get("jobTitle") or "").lower()
    if words and not any(word in title for word in words):
        return "keywords"
    if excluded and any(word in title for word in excluded):
        return "excludeKeywords"
    if settings.get("location") and not _loc_match(settings["location"], row.get("location")):
        return "location"
    if settings.get("only_remote") and not row.get("remote"):
        return "remoteOnly"
    for key, field in (("employment_type", "employmentType"),
                       ("experience_level", "experienceLevel"),
                       ("department", "department")):
        needle = (settings.get(key) or "").strip().lower()
        if needle and needle not in str(row.get(field) or "").lower():
            return {"employment_type": "employmentType", "experience_level": "experienceLevel",
                    "department": "department"}[key]
    return None


def split_words(text: str) -> list[str]:
    """Comma-separated phrases, spaces kept inside a phrase.

    Measured 07.11.2026: splitting on spaces as well turned "game designer" into
    "game OR designer", so a buyer asking for one role was charged for every posting
    with "game" in the title. A phrase with a space is now matched as a phrase.
    """
    parts = [re.sub(r"\s+", " ", part.strip().lower())
             for part in re.split(r"[,;\n]+", (text or ""))]
    return [part for part in parts if part]
