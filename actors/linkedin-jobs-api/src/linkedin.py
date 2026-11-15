"""LinkedIn public job search: addresses, extraction rules and plain data rules.

Measured on 2026-11-12 from a Frankfurt datacenter address, headful Chromium under
Xvfb, guest session (no account):

  * the search page answered with 60 unique job cards and no sign-in wall;
  * `start=` does not paginate: start=0, 25, 50, 200 and 250 returned the same first
    cards, so page offsets are not a depth lever for a guest;
  * `sortBy=DD` (newest) returned exactly the same 60 ids as relevance, so sorting is
    not a lever either;
  * the date filter is a lever: `f_TPR=r604800` (past week) returned 60 ids with only
    14 shared with the unfiltered set, union 106;
  * a job page answered with an 8181 character description and no sign-in wall.

So depth comes from lenses (date windows), not from pages. Nothing here touches the
network: this module is pure, so the rules are tested without a browser and without
spending a run.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

JOBS_PER_LENS = 60
MAX_LENSES = 3
MAX_JOBS_CLAMP = JOBS_PER_LENS * MAX_LENSES

DATE_FILTERS = {
    "any": "",
    "past24h": "&f_TPR=r86400",
    "pastWeek": "&f_TPR=r604800",
    "pastMonth": "&f_TPR=r2592000",
}

LENS_ORDER = ("any", "pastWeek", "past24h")

EXPERIENCE_FILTERS = {
    "any": "",
    "internship": "&f_E=1",
    "entry": "&f_E=2",
    "associate": "&f_E=3",
    "mid-senior": "&f_E=4",
    "director": "&f_E=5",
    "executive": "&f_E=6",
}

REMOTE_FILTERS = {"any": "", "remote": "&f_WT=2", "hybrid": "&f_WT=3", "onsite": "&f_WT=1"}


def lenses(date_posted: str = "any") -> list[str]:
    """Date windows to walk. A chosen window is honoured alone; 'any' means all three.

    The measured reason: each lens carries its own 60 cards, and the past week lens
    shared only 14 ids with the unfiltered one, so the walk is not a repeat.
    """
    chosen = str(date_posted or "any").strip()
    if chosen in DATE_FILTERS and chosen != "any":
        return [chosen]
    return list(LENS_ORDER)


def search_url(keywords: str, location: str = "", date_posted: str = "any",
               experience: str = "any", remote: str = "any") -> str:
    """A guest search address. A filter that is not sent is not silently applied."""
    url = "https://www.linkedin.com/jobs/search/?keywords=" + _quote(keywords)
    if location:
        url += "&location=" + _quote(location)
    url += DATE_FILTERS.get(date_posted, "")
    url += EXPERIENCE_FILTERS.get(experience, "")
    url += REMOTE_FILTERS.get(remote, "")
    return url


def _quote(text: str) -> str:
    """Percent-encode everything outside the URL-safe set.

    A plain isalnum() is not enough: it is True for Cyrillic letters too, so a
    Russian search phrase used to travel unencoded and the search answered nothing.
    """
    from urllib.parse import quote
    return quote(str(text), safe="-_.~")


def job_id_from_url(url: str) -> str | None:
    """The id LinkedIn uses, e.g. senior-software-engineer-python-at-venmo-4465379983."""
    match = re.search(r"/jobs/view/([^/?#]+)", str(url or ""))
    return match.group(1) if match else None


def canonical_url(job_id: str) -> str:
    return "https://www.linkedin.com/jobs/view/" + str(job_id)


def clean(text) -> str | None:
    """One line, no runs of spaces, None for empty. Sold as-is beyond that."""
    if text is None:
        return None
    value = re.sub(r"\s+", " ", str(text)).strip()
    return value or None


def parse_posted(text) -> str | None:
    """LinkedIn prints '2 hours ago', '30 minutes ago', 'Reposted 3 days ago'."""
    value = clean(text)
    if not value:
        return None
    match = re.search(r"(\d+)\s*(minute|hour|day|week|month)", value.lower())
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2)
    delta = {
        "minute": timedelta(minutes=amount),
        "hour": timedelta(hours=amount),
        "day": timedelta(days=amount),
        "week": timedelta(weeks=amount),
        "month": timedelta(days=30 * amount),
    }[unit]
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def posted_from_date(value) -> str | None:
    """The <time datetime="2026-11-12"> form is a date, not a moment."""
    text = clean(value)
    if not text:
        return None
    return text if re.match(r"^\d{4}-\d{2}-\d{2}$", text) else None


TIME_AGO = re.compile(r"\b\d+\s*(?:minute|hour|day|week|month|year)s?\s*ago\b", re.I)


def clean_place(text) -> str | None:
    """A place name and nothing else: the job subline also carries age and applicants."""
    value = clean(text)
    if not value:
        return None
    cut = len(value)
    match = TIME_AGO.search(value)
    if match and match.start() > 0:
        cut = match.start()
    for marker in (" ago", " applicant", " see who", " easy apply", " promoted", " reposted",
                   " be an early applicant", " actively recruiting"):
        pos = value.lower().find(marker)
        if pos > 0:
            cut = min(cut, pos)
    out = value[:cut].strip(" ,;:-")
    return out or None


def job_row(card: dict, detail: dict | None) -> dict:
    """One sold row: card fields first, detail fields where they were read."""
    detail = detail or {}
    job_id = clean(card.get("id")) or job_id_from_url(card.get("link"))
    description = detail.get("description") or ""
    row = {
        "jobId": job_id,
        "url": canonical_url(job_id) if job_id else None,
        "title": clean(detail.get("title")) or clean(card.get("title")),
        "company": clean(detail.get("company")) or clean(card.get("company")),
        "location": clean_place(detail.get("location")) or clean_place(card.get("location")),
        "postedAt": parse_posted(card.get("posted")) or posted_from_date(card.get("date")),
        "postedText": clean(card.get("posted")),
        "employmentType": clean(detail.get("employmentType")),
        "seniorityLevel": clean(detail.get("seniorityLevel")),
        "jobFunction": clean(detail.get("jobFunction")),
        "industries": clean(detail.get("industries")),
        "applyUrl": clean(detail.get("applyUrl")),
        "applicants": clean(detail.get("applicants")),
        "description": description or None,
        "descriptionCharacters": len(description),
        "companyUrl": clean(detail.get("companyUrl")),
    }
    if row["company"] and row["location"] and row["location"].lower().startswith(row["company"].lower()):
        rest = row["location"][len(row["company"]):].strip(" ,-")
        row["location"] = rest or None
    return row


def text_is_wall(title: str | None, body: str | None, has_cards: bool = False) -> str | None:
    """A sign-in wall or a challenge, told apart from a search with no jobs.

    A guest search always shows a sign-in banner somewhere, so the banner alone is
    not a wall: it counts only when the page carried no job cards at all.
    """
    low_title = str(title or "").lower()
    low_body = str(body or "").lower()
    for marker in ("authwall", "join linkedin", "linkedin login", "sign up | linkedin",
                   "sign in | linkedin"):
        if marker in low_title:
            return "sign-in wall"
    # A challenge is a wall even when a few cards rendered behind it.
    for marker in ("captcha", "verify you are human", "unusual activity"):
        if marker in low_body[:1500]:
            return marker
    # The sign-in banner is on every guest page: it counts only when the page
    # carried no job cards at all.
    for marker in ("sign in to view", "join now to see"):
        if marker in low_body[:1500]:
            return None if has_cards else marker
    return None
