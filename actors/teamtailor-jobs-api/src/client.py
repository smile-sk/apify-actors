"""Teamtailor Jobs API: read the public JSON Feed of a Teamtailor career site.

The feed is the employer's own public export (JSON Feed 1.1), no key, no login,
no proxy, and one request returns the whole board together with every description
and a schema.org JobPosting record per role.

Measured facts that shape this module:
  * the board arrives as one answer: `next_url` is null on every live board, so
    nothing can be silently left for "page 2";
  * a board lives either on `<name>.teamtailor.com` or on the employer's own host
    (`jobs.tibber.com`, `career.kry.se`, `jobs.lunar.app` all answer 200), so the
    identifier is a host, not a slug, and a self-hosted board is first class;
  * a company that does not exist answers 404 on the feed itself, so "no such
    board" and "a board with no openings" are told apart by the status;
  * `jobLocation` is an array: 15 of 109 live roles carried more than one office,
    so one role is one row and its offices are united, never split into several
    paid rows;
  * `jobLocationType` is TELECOMMUTE on remote roles (9 of 109), which is a fact
    from the feed, not a guess from the word "remote" in a title.

Because a buyer may type their own host, every address we open is checked: the
host must resolve to public addresses only, and every redirect hop is checked
again. A board is employer-controlled content, so its links are treated as input.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from urllib.parse import urlparse, urlunparse

HOST_SUFFIX = "teamtailor.com"
LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
FEED_PATH = "/jobs.json"
MAX_FEED_BYTES = 40 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT = 60
# Names that belong to Teamtailor itself, never to a customer board.
RESERVED = {"www", "api", "app", "help", "status", "support", "docs", "blog", "go", "info"}


class BoardError(Exception):
    """A board that was not read. kind is 'not_found' or 'error'."""

    def __init__(self, kind, reason):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def _host_is_public(host):
    """True when every address the host resolves to is public, and there is at least one.

    A literal like `127.0.0.1` or `::ffff:169.254.169.254` never reaches DNS but is
    caught here too: in a container the metadata endpoint answers with a service
    account token, so a private target is refused before any request is made.
    """
    if not host:
        return False, "empty host"
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        mapped = getattr(literal, "ipv4_mapped", None) or literal
        if not mapped.is_global:
            return False, "%s is not a public address" % host
        return True, None
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return False, "%s does not resolve (%s)" % (host, exc)
    except Exception as exc:
        return False, "%s could not be resolved (%s)" % (host, exc)
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        return False, "%s resolved to no address" % host
    for address in addresses:
        mapped = getattr(address, "ipv4_mapped", None) or address
        if not mapped.is_global:
            return False, "%s resolves to a private address (%s)" % (host, mapped)
    return True, None


def normalize_board(raw):
    """Turn what the buyer typed into a board host, or None when it is not one.

    Accepts 'oatly', 'oatly.teamtailor.com', 'https://oatly.teamtailor.com/',
    'https://oatly.teamtailor.com/jobs' and a self-hosted board such as
    'jobs.tibber.com' or 'https://career.kry.se/jobs'. A bare word is read as a
    Teamtailor subdomain, because that is what a company name means there.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text or any(char.isspace() for char in text):
        return None
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lstrip(".").rstrip(".")
    if not host or len(host) > 253:
        return None
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if not all(LABEL_RE.match(label) for label in labels):
        return None
    if len(labels) == 1:
        if host in RESERVED:
            return None
        return "%s.%s" % (host, HOST_SUFFIX)
    if host.endswith("." + HOST_SUFFIX):
        slug = host[: -(len(HOST_SUFFIX) + 1)]
        if not slug or "." in slug or slug in RESERVED:
            return None
        return host
    # a self-hosted board: any other public host is allowed, the request guard decides
    if len(labels) < 2:
        return None
    return host


def feed_url(board):
    return urlunparse(("https", board, FEED_PATH, "", "", ""))


def _client(timeout):
    import httpx

    return httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        headers={
            "User-Agent": "teamtailor-jobs-api/0.1 (+https://apify.com)",
            "Accept": "application/feed+json, application/json;q=0.9, */*;q=0.5",
        },
    )


def _read(url, timeout):
    """GET the feed, checking every hop. 404 becomes a missing board."""
    import httpx

    current = url
    with _client(timeout) as client:
        for hop in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme != "https":
                raise BoardError("error", "%s is not https" % current)
            ok, why = _host_is_public(parsed.hostname or "")
            if not ok:
                raise BoardError("error", "%s was refused: %s" % (current, why))
            try:
                with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise BoardError("error", "%s answers %d without Location" % (current, response.status_code))
                        current = str(response.url.join(location))
                        continue
                    if response.status_code == 404:
                        raise BoardError(
                            "not_found",
                            "%s answers 404, so this company has no Teamtailor board at this address" % current,
                        )
                    if response.status_code != 200:
                        raise BoardError("error", "%s answers HTTP %d" % (current, response.status_code))
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_FEED_BYTES:
                        raise BoardError(
                            "error",
                            "the feed declares %s bytes, over the %d byte read limit" % (declared, MAX_FEED_BYTES),
                        )
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_FEED_BYTES:
                            raise BoardError("error", "%s is larger than the %d byte read limit" % (current, MAX_FEED_BYTES))
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if not body.strip():
                        raise BoardError("error", "%s answered an empty body" % current)
                    return body
            except BoardError:
                raise
            except httpx.HTTPError as exc:
                raise BoardError("error", "%s could not be read: %s" % (current, str(exc)[:160]))
            except Exception as exc:
                raise BoardError("error", "%s could not be read: %s" % (current, str(exc)[:160]))
    raise BoardError("error", "%s redirects more than %d times" % (url, MAX_REDIRECTS))


def parse_feed(body):
    """JSON bytes into the list of raw feed items."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BoardError("error", "the answer is not JSON: %s" % exc)
    if not isinstance(payload, dict):
        raise BoardError("error", "the answer is not a JSON Feed object (got %s)" % type(payload).__name__)
    items = payload.get("items")
    if not isinstance(items, list):
        raise BoardError("error", "the answer carries no items list (keys: %s)" % ",".join(sorted(payload)[:6]))
    return items


def fetch_board(board, timeout=TIMEOUT):
    """Read one board. Returns {'jobs': [...], 'empty': bool}.

    Raises BoardError('not_found') when no board answers at that address, and
    BoardError('error') when the board exists but could not be read today.
    """
    items = parse_feed(_read(feed_url(board), timeout))
    return {"jobs": items, "empty": not items, "title": None}


TAG_RE = re.compile(r"<[^>]+>")
DROP_BLOCK_RE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")


def strip_html(value):
    """Employer markup into readable text: scripts dropped, tags out, entities decoded."""
    if not value:
        return value
    text = DROP_BLOCK_RE.sub(" ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|ul|ol|h1|h2|h3|h4|tr)>", "\n", text)
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return BLANK_LINES_RE.sub("\n\n", text).strip()


def _clean(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _first(value):
    if isinstance(value, list):
        for entry in value:
            text = _clean(entry if isinstance(entry, str) else None)
            if text:
                return text
        return None
    return _clean(value)


def _all(value):
    if isinstance(value, list):
        return [_clean(entry) for entry in value if _clean(entry)]
    text = _clean(value)
    return [text] if text else []


def offices_of(jobposting):
    """Every office of one role, in the order the employer listed them."""
    offices, seen = [], set()
    for place in jobposting.get("jobLocation") or []:
        if not isinstance(place, dict):
            continue
        address = place.get("address") if isinstance(place.get("address"), dict) else {}
        city = _clean(address.get("addressLocality"))
        state = _clean(address.get("addressRegion"))
        country = _clean(address.get("addressCountry"))
        postal = _clean(address.get("postalCode"))
        street = _clean(address.get("streetAddress"))
        key = (city, state, country, postal)
        if not any(key) or key in seen:
            continue
        seen.add(key)
        offices.append(
            {
                "city": city,
                "state": state,
                "country": country,
                "countryCode": country if country and len(country) == 2 else None,
                "postalCode": postal,
                "street": street,
            }
        )
    return offices


def parse_job(item, board, include_details=True, description_format="html"):
    """One feed item into one dataset row, or None when it is not a job posting."""
    if not isinstance(item, dict):
        return None
    jobposting = item.get("_jobposting") if isinstance(item.get("_jobposting"), dict) else {}
    identifier = jobposting.get("identifier") if isinstance(jobposting.get("identifier"), dict) else {}
    job_id = _clean(str(identifier.get("value") or "")) or _clean(item.get("id"))
    title = _clean(jobposting.get("title")) or _clean(item.get("title"))
    if not job_id or not title:
        return None
    offices = offices_of(jobposting)
    primary = offices[0] if offices else {}
    organization = jobposting.get("hiringOrganization") if isinstance(jobposting.get("hiringOrganization"), dict) else {}
    location_type = (_clean(jobposting.get("jobLocationType")) or "").upper()
    description = None
    if include_details:
        description = _clean(item.get("content_html")) or _clean(jobposting.get("description"))
        if description and description_format == "text":
            description = strip_html(description)
    return {
        "status": "DELIVERED",
        "board": board,
        "jobId": job_id,
        "title": title,
        "company": _clean(organization.get("name")),
        "employmentType": ", ".join(_all(jobposting.get("employmentType"))) or None,
        "directApply": jobposting.get("directApply") is True,
        "remote": location_type == "TELECOMMUTE" or bool(jobposting.get("applicantLocationRequirements")),
        "city": primary.get("city"),
        "state": primary.get("state"),
        "country": primary.get("country"),
        "countryCode": primary.get("countryCode"),
        "postalCode": primary.get("postalCode"),
        "locations": offices,
        "publishedAt": _clean(jobposting.get("datePosted")) or _clean(item.get("date_published")),
        "validThrough": _clean(jobposting.get("validThrough")),
        "url": _clean(item.get("url")),
        "description": description,
        "reason": None,
        "delivered": True,
        "billable": None,
    }


def merge_positions(rows):
    """One role is one row: a job id that repeats is merged, never charged twice."""
    order, merged = [], {}
    duplicates = 0
    for row in rows:
        job_id = row.get("jobId")
        if job_id in merged:
            kept = merged[job_id]
            seen = {
                (office.get("city"), office.get("state"), office.get("country"), office.get("postalCode"))
                for office in kept.get("locations") or []
            }
            for office in row.get("locations") or []:
                key = (office.get("city"), office.get("state"), office.get("country"), office.get("postalCode"))
                if key not in seen:
                    kept.setdefault("locations", []).append(office)
                    seen.add(key)
            duplicates += 1
            continue
        merged[job_id] = row
        order.append(job_id)
    return [merged[job_id] for job_id in order], duplicates


def matches(row, filters):
    """Filters run in the Actor: the feed is a static export with no server side search."""
    if filters.get("remoteOnly") and row.get("remote") is not True:
        return False
    employment = filters.get("employmentType") or ""
    if employment and employment not in (row.get("employmentType") or "").lower():
        return False
    city = filters.get("city") or ""
    if city:
        cities = [row.get("city") or ""] + [office.get("city") or "" for office in row.get("locations") or []]
        if not any(city in value.lower() for value in cities):
            return False
    country = filters.get("country") or ""
    if country:
        # a two letter code is compared for equality: "in" inside Singapore is a paid row
        # for a country the buyer never asked for
        code = (row.get("countryCode") or "").lower()
        name = (row.get("country") or "").lower()
        others = [((office.get("countryCode") or "").lower(), (office.get("country") or "").lower())
                  for office in row.get("locations") or []]
        if len(country) == 2:
            if code != country and not any(oc == country for oc, _ in others):
                return False
        else:
            if country not in name and not any(country in on for _, on in others):
                return False
    keywords = filters.get("keywords") or []
    if keywords:
        haystack = " ".join(filter(None, [row.get("title"), row.get("company")])).lower()
        if not any(word in haystack for word in keywords):
            return False
    published_after = filters.get("publishedAfter") or ""
    if published_after and str(row.get("publishedAt") or "") < published_after:
        return False
    return True


def sort_key(row):
    """Newest first: the feed carries a publish date, so that is what we sort on."""
    return str(row.get("publishedAt") or "")
