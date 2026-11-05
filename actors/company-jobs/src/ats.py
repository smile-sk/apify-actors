"""Detect a company's ATS from its domain and pull every open job.

Why this product exists: the job-postings niche on Apify Store is one of the
largest (146 actors, ~6.1M runs a month), but almost all of it fights sites
that block data-centre IPs - Indeed, LinkedIn, Glassdoor, Zippia. The ATS
boards behind those sites (Greenhouse, Ashby, Lever, Workable, SmartRecruiters,
Recruitee) are open JSON and answer a plain HTTP request.

So this actor does not need proxies, does not get blocked, and costs almost
nothing to run. The work is in the detection: a domain rarely matches the
board slug, so we try the slug, then read the company careers page and follow
whatever ATS link it carries.

Nothing here imports the Apify SDK: this module can be run and measured
locally, which is how the detection was tested.
"""
from __future__ import annotations

import html
import json
import re
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = 15
MAX_BODY = 40 * 1024 * 1024

CAREERS_PATHS = ("/careers", "/jobs", "/company/careers", "/about/careers", "/careers/jobs")

ATS_PATTERNS = (
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9][a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9][a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9][a-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"(?:apply\.workable\.com/|)([a-z0-9][a-z0-9_-]+)\.workable\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([a-z0-9][a-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"([a-z0-9][a-z0-9_-]+)\.recruitee\.com", re.I)),
    ("personio", re.compile(r"([a-z0-9][a-z0-9_-]+)\.jobs\.personio\.(?:de|com)", re.I)),
)


def _get(url: str, timeout: int = TIMEOUT) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(MAX_BODY)


def _json(url: str, timeout: int = TIMEOUT):
    status, body = _get(url, timeout)
    if status != 200:
        raise ValueError(f"HTTP {status}")
    return json.loads(body.decode("utf-8", "replace"))


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


ATS_HOSTS = {
    "jobs.lever.co": "lever",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.eu.greenhouse.io": "greenhouse",
    "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
    "jobs.smartrecruiters.com": "smartrecruiters",
}


def _ats_host_slug(host: str, path: str) -> dict:
    """A board URL names its own slug: jobs.lever.co/acme -> lever / acme."""
    host = host.lower().replace("www.", "")
    if host in ATS_HOSTS:
        parts = [seg for seg in path.split("/") if seg]
        if parts:
            return {"ats": ATS_HOSTS[host], "slug": parts[0].lower()}
        return {}
    if host.endswith(".recruitee.com"):
        return {"ats": "recruitee", "slug": host.split(".")[0]}
    return {}


def parse_company(raw: str) -> dict:
    """'Acme Inc', 'acme.com', 'https://jobs.lever.co/acme' -> display, domain, slugs."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    display, domain = raw, ""
    candidate = raw
    if "://" in candidate:
        candidate = candidate.split("://", 1)[1]
    host = candidate.split("/", 1)[0].strip()
    path = candidate.split("/", 1)[1] if "/" in candidate else ""
    hint = {}
    if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host.lower()):
        domain = host.lower().removeprefix("www.")
        display = domain.split(".")[0].replace("-", " ").title()
        hint = _ats_host_slug(host, path)
    slugs = []
    if hint:
        slugs = [hint["slug"]]
        display = hint["slug"].replace("-", " ").title()
    elif domain:
        label = domain.split(".")[0]
        slugs = [label]
        if "-" in label:
            slugs.append(label.replace("-", ""))
        slugs.append(domain.replace(".", ""))
    else:
        guess = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
        if guess:
            slugs = [guess, guess.replace("-", "")]
        display = raw
    out = {"display": display, "domain": domain,
           "slugs": [s for s in dict.fromkeys(slugs) if s]}
    if hint:
        out["hint"] = hint
    return out


def _parse_salary(text: str | None) -> dict:
    """'$180K - $250K' / 'EUR 60.000 - 80.000 a year' -> numbers."""
    out = {"min": None, "max": None, "currency": None, "period": None}
    if not text or not isinstance(text, str):
        return out
    currency = None
    if "$" in text or "USD" in text.upper():
        currency = "USD"
    elif "€" in text or "EUR" in text.upper():
        currency = "EUR"
    elif "£" in text or "GBP" in text.upper():
        currency = "GBP"
    if re.search(r"(?i)per\s+(year|annum)|/\s*(year|yr)|annual|yearly|a\s+year", text):
        out["period"] = "YEAR"
    elif re.search(r"(?i)per\s+month|/\s*month|monthly|a\s+month", text):
        out["period"] = "MONTH"
    elif re.search(r"(?i)per\s+hour|/\s*hour|hourly", text):
        out["period"] = "HOUR"
    numbers = []
    for match in re.finditer(r"(\d[\d\s.,]*)\s*([kK])?", text):
        digits = re.sub(r"[\s.,]", "", match.group(1))
        if not digits:
            continue
        try:
            value = float(digits)
        except ValueError:
            continue
        if match.group(2):
            value *= 1000
        if value >= 1000:
            numbers.append(int(value))
    if numbers:
        out["min"] = min(numbers)
        out["max"] = max(numbers) if len(numbers) > 1 else None
    out["currency"] = currency
    return out


# --------------------------------------------------------------------------- #
# Per-ATS fetchers. Each returns a list of dicts in our unified schema, or
# raises. Detection calls them with detail=False (cheap), then the winner is
# called once more with the detail the user asked for.
# --------------------------------------------------------------------------- #

def fetch_greenhouse(slug: str, detail: bool = False) -> list[dict]:
    # content=true is not optional: it is also what carries `departments` and
    # `offices`. With content=false those fields are absent, so every row would
    # lose its department - a data-quality bug, not a saving.
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = _json(url)
    jobs = []
    for job in data.get("jobs") or []:
        loc = (job.get("location") or {}).get("name") or ""
        depts = ", ".join(d.get("name", "") for d in (job.get("departments") or []) if d.get("name"))
        if not depts:
            depts = ", ".join(d.get("name", "") for d in (job.get("offices") or []) if d.get("name"))
        jobs.append({
            "jobId": str(job.get("id")),
            "jobTitle": job.get("title"),
            "department": depts or None,
            "location": loc or None,
            "remote": bool(re.search(r"(?i)\bremote\b|anywhere", loc)),
            "employmentType": None,
            "salaryMin": None, "salaryMax": None, "salaryCurrency": None, "salaryPeriod": None,
            "postedAt": job.get("updated_at") or job.get("first_published"),
            "url": job.get("absolute_url"),
            "jobDescription": _strip_html(job.get("content")) if detail else None,
        })
    return jobs


def fetch_ashby(slug: str, detail: bool = False) -> list[dict]:
    data = _json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    jobs = []
    for job in data.get("jobs") or []:
        loc = job.get("location") or ""
        secondary = ", ".join(x.get("location", "") for x in (job.get("secondaryLocations") or []) if x.get("location"))
        comp = job.get("compensation") or {}
        salary = _parse_salary(comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary"))
        jobs.append({
            "jobId": job.get("id"),
            "jobTitle": job.get("title"),
            "department": job.get("department") or job.get("team"),
            "location": ", ".join([p for p in [loc, secondary] if p]) or None,
            "remote": bool(job.get("isRemote")) or (job.get("workplaceType") or "").lower() == "remote",
            "employmentType": job.get("employmentType"),
            "salaryMin": salary["min"], "salaryMax": salary["max"],
            "salaryCurrency": salary["currency"], "salaryPeriod": salary["period"],
            "postedAt": job.get("publishedAt") or job.get("updatedAt"),
            "url": job.get("jobUrl") or job.get("applyUrl"),
            "jobDescription": (_strip_html(job.get("descriptionHtml")) or job.get("descriptionPlain")) if detail else None,
        })
    return jobs


def fetch_lever(slug: str, detail: bool = False) -> list[dict]:
    data = _json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for job in data or []:
        cat = job.get("categories") or {}
        loc = cat.get("location") or ""
        all_loc = ", ".join(cat.get("allLocations") or [])
        rng = job.get("salaryRange") or {}
        jobs.append({
            "jobId": job.get("id"),
            "jobTitle": job.get("text"),
            "department": cat.get("team") or cat.get("department"),
            "location": all_loc or loc or None,
            "remote": bool(re.search(r"(?i)\bremote\b", f"{loc} {job.get('workplaceType') or ''}")),
            "employmentType": cat.get("commitment"),
            "salaryMin": int(rng["min"]) if isinstance(rng.get("min"), (int, float)) else None,
            "salaryMax": int(rng["max"]) if isinstance(rng.get("max"), (int, float)) else None,
            "salaryCurrency": rng.get("currency"),
            "salaryPeriod": (rng.get("interval") or "").upper() or None,
            "postedAt": job.get("createdAt"),
            "url": job.get("hostedUrl") or job.get("applyUrl"),
            "jobDescription": _strip_html(job.get("descriptionPlain") or job.get("description")) if detail else None,
        })
    return jobs


def fetch_workable(slug: str, detail: bool = False) -> list[dict]:
    data = _json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    jobs = []
    for job in data.get("jobs") or []:
        loc = job.get("location") or {}
        loc_text = loc.get("location_str") or ", ".join(
            p for p in [loc.get("city"), loc.get("region"), loc.get("country")] if p)
        jobs.append({
            "jobId": job.get("shortcode") or job.get("id"),
            "jobTitle": job.get("title"),
            "department": ", ".join(job.get("department") or []) or None,
            "location": loc_text or None,
            "remote": bool(loc.get("telecommuting")) or bool(re.search(r"(?i)\bremote\b", loc_text)),
            "employmentType": job.get("type"),
            "salaryMin": None, "salaryMax": None, "salaryCurrency": None, "salaryPeriod": None,
            "postedAt": job.get("created_at") or job.get("published_on"),
            "url": job.get("url") or job.get("shortlink"),
            "jobDescription": _strip_html(job.get("description")) if detail else None,
        })
    return jobs


def fetch_smartrecruiters(slug: str, detail: bool = False) -> list[dict]:
    jobs = []
    offset = 0
    while True:
        data = _json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
        content = data.get("content") or []
        for job in content:
            loc = job.get("location") or {}
            loc_text = ", ".join(p for p in [loc.get("city"), loc.get("region"), loc.get("country")] if p)
            jobs.append({
                "jobId": job.get("id") or job.get("uuid"),
                "jobTitle": job.get("name"),
                "department": (job.get("department") or {}).get("label"),
                "location": loc_text or None,
                "remote": bool(loc.get("remote")) or bool(re.search(r"(?i)\bremote\b", loc_text)),
                "employmentType": (job.get("typeOfEmployment") or {}).get("label"),
                "salaryMin": None, "salaryMax": None, "salaryCurrency": None, "salaryPeriod": None,
                "postedAt": job.get("releasedDate"),
                "url": f"https://jobs.smartrecruiters.com/{slug}/{job.get('id')}",
                "jobDescription": None,
            })
        total = data.get("totalFound") or 0
        offset += 100
        if not content or offset >= total or offset >= 500:
            break
    return jobs


def fetch_recruitee(slug: str, detail: bool = False) -> list[dict]:
    data = _json(f"https://{slug}.recruitee.com/api/offers/")
    jobs = []
    for job in data.get("offers") or []:
        loc = ", ".join(p for p in [job.get("city"), job.get("country")] if p)
        salary = job.get("salary") or {}
        jobs.append({
            "jobId": str(job.get("id")),
            "jobTitle": job.get("title"),
            "department": job.get("department"),
            "location": loc or None,
            "remote": bool(job.get("remote")),
            "employmentType": job.get("employment_type_code"),
            "salaryMin": salary.get("min"), "salaryMax": salary.get("max"),
            "salaryCurrency": salary.get("currency"), "salaryPeriod": None,
            "postedAt": job.get("published_at"),
            "url": job.get("careers_url") or job.get("careers_apply_url"),
            "jobDescription": _strip_html(job.get("description")) if detail else None,
        })
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "lever": fetch_lever,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
}

PROBE_ORDER = ("greenhouse", "ashby", "lever", "workable", "smartrecruiters", "recruitee")


def find_ats_links(domain: str) -> list[tuple[str, str]]:
    """Read the company careers page and pull (ats, slug) out of its links."""
    found = []
    if not domain:
        return found
    for path in CAREERS_PATHS:
        try:
            status, body = _get(f"https://{domain}{path}", timeout=12)
        except Exception:
            continue
        if status != 200:
            continue
        text = body.decode("utf-8", "replace")
        for ats, pattern in ATS_PATTERNS:
            for match in pattern.finditer(text):
                pair = (ats, match.group(1).lower())
                if pair not in found:
                    found.append(pair)
        if found:
            break
    return found


def probe(company: str) -> dict:
    """Detect the ATS of one company. Returns provider, slug and job count."""
    parsed = parse_company(company)
    if not parsed:
        return {"input": company, "error": "EMPTY"}

    tries: list[tuple[str, str]] = []
    hint = parsed.get("hint") or {}
    if hint.get("ats") and hint.get("slug"):
        tries.append((hint["ats"], hint["slug"]))
    for ats, slug in find_ats_links(parsed["domain"]):
        tries.append((ats, slug))
    for slug in parsed["slugs"]:
        for ats in PROBE_ORDER:
            if (ats, slug) not in tries:
                tries.append((ats, slug))
    if not tries:
        return {"input": company, **parsed, "error": "NO_SLUG"}

    def attempt(pair):
        ats, slug = pair
        try:
            jobs = FETCHERS[ats](slug, detail=False)
            if jobs:
                return {"ats": ats, "slug": slug, "count": len(jobs)}
        except Exception:
            return None
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(attempt, tries[:12]):
            if result:
                return {"input": company, **parsed, **result, "error": None}
    return {"input": company, **parsed, "ats": None, "slug": None, "count": 0,
            "error": "NO_ATS_FOUND",
            "hint": "no open ATS board answered for this domain; pass the board slug or the careers URL"}


def pull(company: str, detail: bool = False) -> dict:
    """Detect, then fetch the full board with the detail level the user asked for."""
    found = probe(company)
    if found.get("error"):
        return found
    try:
        jobs = FETCHERS[found["ats"]](found["slug"], detail=detail)
    except Exception as exc:
        return {**found, "error": f"FETCH_FAILED: {type(exc).__name__}", "count": 0}
    return {**found, "jobs": jobs, "count": len(jobs)}


def _loc_match(needle: str, haystack) -> bool:
    """Country codes must be whole words: 'US' must not match 'Australia'."""
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
    if needle == "uk" and "united" in tokens and "kingdom" in tokens:
        return True
    if len(needle) > 3:
        return needle in hay
    return False


def filter_jobs(jobs: list[dict], keywords: str = "", location: str = "", only_remote: bool = False,
                limit: int = 0) -> list[dict]:
    """Only the offers that match - the buyer pays for what they asked for."""
    words = [w for w in re.split(r"[,\s]+", (keywords or "").lower()) if w]
    loc_needle = (location or "").lower().strip()
    out = []
    for job in jobs:
        title = (job.get("jobTitle") or "").lower()
        if words and not any(w in title for w in words):
            continue
        if loc_needle and not _loc_match(loc_needle, job.get("location")):
            continue
        if only_remote and not job.get("remote"):
            continue
        out.append(job)
        if limit and len(out) >= limit:
            break
    return out
