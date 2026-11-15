"""Tech stack detector: what a site is built with, and the proof for every answer.

Money rule of this park: a row is delivered with a charge, or it is delivered free with
a status that says in plain words why the buyer got no scan. Nothing is charged for an
address we could not use, for a page that answered 4xx or 5xx, for a file that is not a
page, for a site the buyer's own filter excluded, or for a run that stopped early.

The paid push goes through Actor.push_data(..., charged_event_name=...), the only SDK
entry point that writes the row and charges for it under one charge lock, and it answers
with a ChargeResult. The budget is checked before that call as well: on an exhausted
budget the SDK deliberately lets one more row through and the platform then kills the
run, which would be the buyer paying for a run that dies.

The site is read with plain http requests, not with a browser: every technology we name
is visible in the bytes the server sends to anyone, and a headless browser would multiply
the cost of a scan by a hundred without changing the answer.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from apify import Actor

from . import tech
from .net import CODE_PRIVATE, host_of, host_verdict, normalize_url

EVENT_NAME = "site-scanned"
SUMMARY_STORE = "tech-stack-detector-summary"
MAX_SITES_DEFAULT = 100
MAX_REDIRECTS = 3
MAX_HTML_BYTES = 3 * 1024 * 1024
MAX_REFUSALS_IN_A_ROW = 3
# One site costs its own request timeout, up to three hops and one body read. The
# margin is computed from the setting instead of being a constant, because a constant
# smaller than the work it guards lets the platform kill the run mid site, and a killed
# run leaves no summary at all.
SITE_MARGIN_EXTRA_S = 6.0

ST_OK = "OK"
ST_INVALID = "NOT_CHECKED_INVALID"
ST_DUPLICATE = "NOT_CHECKED_DUPLICATE"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_PRIVATE = "NOT_CHECKED_PRIVATE_ADDRESS"
ST_DNS = "NOT_CHECKED_DNS_ERROR"
ST_TIMEOUT = "NOT_CHECKED_TIMEOUT"
ST_HTTP = "NOT_CHECKED_HTTP_ERROR"
ST_NOT_HTML = "NOT_CHECKED_NOT_HTML"
ST_FETCH = "NOT_CHECKED_FETCH_ERROR"
ST_TOO_LARGE = "NOT_CHECKED_TOO_LARGE"
ST_FILTERED = "FILTERED_OUT"
ST_EMPTY = "NO_TECHNOLOGY_FOUND"
ST_BLOCKED = "NOT_CHECKED_BLOCKED_PAGE"
ST_NO_CONTENT = "NOT_CHECKED_NO_CONTENT"
ST_EMPTY_BODY = "NOT_CHECKED_EMPTY_PAGE"

# A challenge page says so in its own words AND has almost nothing to read. Both halves
# came from live mistakes: single words ("captcha", "ddos protection") flagged a normal
# page with a captcha in its form and a shop with a Cloudflare badge, and a size limit
# alone passed a 16 KB challenge as a scanned site that was then charged for.
# The vendor words below are the ones that really arrive: measured live 2026-11-11, the
# Cloudflare/Fastly interim page says "Client Challenge" (drupal.org), the Amazon one says
# "we need to verify that you're not a robot" (booking.com, imdb.com), and plain "captcha"
# stays out on purpose because a sign-in form prints "I'm not a robot" on a normal page.
BLOCK_WORDS = re.compile(
    r"just a moment|client challenge|checking your browser|"
    r"enable javascript and cookies|please enable javascript|attention required|"
    r"verify\s+(?:that\s+)?you(?:['\u2019]?re|\s+are)\s+not\s+a\s+robot", re.I)
# The technical marks sit in urls and attributes, not in the words a person reads, so they
# are a second, quieter signal: drupal.org names itself "Client Challenge" in the body and
# carries "_fs-ch-" in a script path. The words come first because that is what the buyer
# can be told in plain language.
BLOCK_TOKENS = re.compile(r"challenge-platform|cf-browser-verification|cf-chl|_fs-ch-", re.I)
BLOCK_MARKS = BLOCK_WORDS
BLOCK_VISIBLE_TEXT_MAX = 1024
# The second half of the same class: a page that ships kilobytes of markup and almost no
# text is a wall, not a site, and the words may be missing entirely (imdb.com answers 202
# with 1997 bytes and nothing to read). Measured live 2026-11-11: drupal.org 3038 b / 303
# chars, booking.com 8859 / 157 block; example.com 577 / 171 is too small to judge,
# getbootstrap.com 45732 / 1378 and tailwindcss.com 878001 / 7457 have a real page inside.
BLOCK_TEXT_MAX = 400
BLOCK_TEXT_SHARE_MAX = 0.15
BLOCK_BODY_MIN_BYTES = 2048
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
TAG_RE = re.compile(r"<[^>]+>", re.S)
SPACES_RE = re.compile(r"\s+")


def visible_text_len(html: str) -> int:
    """How many characters a person really sees: no scripts, styles, comments or tags."""
    text = SCRIPT_STYLE_RE.sub(" ", html or "")
    text = COMMENT_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    return len(SPACES_RE.sub(" ", text).strip())


def run_id() -> str:
    return (
        os.environ.get("ACTOR_RUN_ID")
        or (Actor.get_env() or {}).get("run_id")
        or "run"
    )


def deadline_seconds() -> float | None:
    """Seconds left before the platform kills the run, or None when unknown."""
    raw = os.environ.get("ACTOR_TIMEOUT_AT")
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - datetime.now(timezone.utc)).total_seconds()


def site_margin_seconds(timeout_s: float) -> float:
    """The worst case for one site: its own request, three hops, one body read."""
    return timeout_s * (MAX_REDIRECTS + 1) + SITE_MARGIN_EXTRA_S


def status_row(line: str, index: int, status: str, reason: str) -> dict:
    return {
        "site": None,
        "url": None,
        "input": line,
        "inputIndex": index,
        "status": status,
        "reason": reason,
        "billable": False,
    }


def cookie_names(headers) -> list[str]:
    names: list[str] = []
    for raw in headers.get_list("set-cookie") if hasattr(headers, "get_list") else []:
        name = str(raw).split("=", 1)[0].strip()
        if name:
            names.append(name)
    return names


def decode_body(body: bytes, content_type: str | None) -> str:
    """Text of the page, with the charset the server declared, or utf-8, or latin-1."""
    declared = ""
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if match:
        declared = match.group(1)
    if not declared:
        head = body[:4096].decode("ascii", errors="ignore")
        found = re.search(r"charset=[\"']?([\w-]+)", head, re.I)
        if found:
            declared = found.group(1)
    for charset in (declared, "utf-8"):
        if not charset:
            continue
        try:
            return body.decode(charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("latin-1", errors="replace")


def build_tasks(lines: list, max_sites: int) -> tuple[list[dict], dict]:
    """Work in input order, with the free refusals decided before any request."""
    tasks: list[dict] = []
    seen: set[str] = set()
    counts = {"asked": len(lines), "unique": 0, "duplicates": 0, "invalid": 0,
              "blank": 0, "limit": 0}
    for index, raw in enumerate(lines):
        url, why = normalize_url(raw)
        if url is None:
            if not str(raw or "").strip():
                counts["blank"] += 1
            else:
                counts["invalid"] += 1
            tasks.append({"kind": "row", "row": status_row(
                "" if raw is None else str(raw), index, ST_INVALID,
                f"this line is not a url: {why}")})
            continue
        if url in seen:
            counts["duplicates"] += 1
            tasks.append({"kind": "row", "row": status_row(
                str(raw), index, ST_DUPLICATE,
                "the same url came earlier in the list, so it is read once and this "
                "line is free")})
            continue
        seen.add(url)
        counts["unique"] += 1
        if max_sites and counts["unique"] > max_sites:
            counts["limit"] += 1
            tasks.append({"kind": "row", "row": status_row(
                str(raw), index, ST_LIMIT,
                f"the list is cut at maxSites={max_sites}: not read and not charged")})
            continue
        tasks.append({"kind": "work", "line": str(raw), "index": index, "url": url})
    return tasks, counts


def price_state(charging) -> tuple[bool | None, str]:
    """Can this run charge? True / False / None (unknown) and a plain reason."""
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001 - any failure means "price unknown"
        return None, f"pricing read failed: {type(exc).__name__}"
    if info is None:
        return None, "pricing info is not readable"
    is_ppe = getattr(info, "is_pay_per_event", None)
    if is_ppe is None and isinstance(info, dict):
        is_ppe = info.get("isPayPerEvent", info.get("is_pay_per_event"))
    if is_ppe is False:
        return False, "the actor is not sold per event"
    prices = getattr(info, "per_event_prices", None)
    if prices is None and isinstance(info, dict):
        prices = info.get("perEventPrices") or info.get("per_event_prices") or {}
    names: list[str] = []
    if isinstance(prices, dict):
        names = [str(key) for key in prices]
    elif isinstance(prices, list):
        for item in prices:
            name = getattr(item, "event_name", None)
            if name is None and isinstance(item, dict):
                name = item.get("eventName") or item.get("event_name")
            if name:
                names.append(str(name))
    if is_ppe is None:
        if names:
            return (EVENT_NAME in names), (
                f"{EVENT_NAME} is priced" if EVENT_NAME in names
                else f"{EVENT_NAME} is not in the price list")
        return None, "the pricing answer is not understood"
    if EVENT_NAME not in names:
        return False, f"{EVENT_NAME} is not priced, rows go out free"
    return True, f"{EVENT_NAME} is priced"


async def dataset_count(dataset) -> int | None:
    try:
        meta = await dataset.get_metadata()
    except Exception:  # noqa: BLE001 - a missing counter is not a lost row
        return None
    value = getattr(meta, "item_count", None)
    if value is None and isinstance(meta, dict):
        value = meta.get("itemCount", meta.get("item_count"))
    return value if isinstance(value, int) else None


def stopped_status(stop_reason: str | None) -> tuple[str, str]:
    mapping = {
        "spending-cap": (ST_CAP, "the spending cap stopped the run earlier"),
        "billing-price-unknown": (
            ST_BILLING, "the run stopped earlier: the price was unknown"),
        "deadline": (ST_TIME, "the run stopped earlier: the deadline was close"),
        "charge-error": (ST_ERROR, "the run stopped earlier: a charge failed"),
        "dataset-write-error": (
            ST_ERROR, "the run stopped earlier: the dataset refused rows"),
        "unexpected-error": (
            ST_ERROR, "the run stopped earlier on an unexpected error"),
    }
    return mapping.get(stop_reason or "", (ST_ERROR, "the run stopped earlier"))


def is_page(content_type: str | None) -> bool:
    """A page, not an image and not a pdf. An empty type is treated as a page,
    because some servers do not send one and refusing those would be a guess."""
    text = (content_type or "").lower()
    if not text:
        return True
    return "html" in text or "xml" in text


def blocked_page(html: str, headers: dict, body_size: int | None = None) -> str | None:
    """Why this answer is a challenge page rather than the site itself.

    Both halves are needed: the words alone fire on a shop that prints "DDoS protection
    by Cloudflare" in its footer, and a short answer alone says nothing, because a small
    page can simply be a small page. A challenge names itself and shows almost no text,
    since everything it has is script.

    A page can also be a wall without saying so: kilobytes of markup with almost nothing
    to read is not the site, whatever it calls itself. That rule is what catches the
    interim pages that answer 200 and carry no vendor words at all.
    """
    if (headers or {}).get("cf-mitigated", "").lower() == "challenge":
        return "the site answered a Cloudflare challenge instead of the page"
    text = html or ""
    visible = visible_text_len(text)
    size = body_size if body_size is not None else len(text.encode("utf-8", errors="ignore"))
    mark = BLOCK_WORDS.search(text) or BLOCK_TOKENS.search(text)
    if mark and visible < BLOCK_VISIBLE_TEXT_MAX:
        return (f"the site answered a challenge page ({mark.group(0)}) with {visible} "
                "characters of visible text instead of the page itself")
    if (size > BLOCK_BODY_MIN_BYTES and visible < BLOCK_TEXT_MAX
            and visible / max(size, 1) < BLOCK_TEXT_SHARE_MAX):
        return (f"the answer carries {size} bytes of markup but only {visible} characters "
                "of visible text: a script wall or a challenge, not the site itself")
    return None


async def _fetch_page(client: httpx.AsyncClient, url: str) -> dict:
    """One page, with every redirect hop checked before its request goes out.

    follow_redirects=True made the address guard blind in four actors of this park: the
    client walked a public url into the private network on its own, and the chain was
    read only after the bytes had arrived. Here each hop is resolved and refused before
    it is requested, and the chain is at most MAX_REDIRECTS long.
    """
    result: dict = {"body": None, "html": None, "finalUrl": None, "hops": [],
                    "status": None, "reason": None, "contentType": None,
                    "httpStatus": None, "headers": {}, "cookies": []}
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        ok, code, why = host_verdict(host_of(current) or "")
        if not ok:
            result["status"] = ST_PRIVATE if code == CODE_PRIVATE else ST_DNS
            result["finalUrl"] = current
            result["hops"] = [*result["hops"], current]
            if len(result["hops"]) > 1:
                result["reason"] = (
                    f"a redirect led to an address that cannot be used ({current}: "
                    f"{why}): that hop was never requested, nothing was delivered and "
                    "nothing was charged")
            else:
                result["reason"] = (
                    f"the address cannot be used, the page was not opened: {why}. "
                    "Nothing was delivered and nothing was charged")
            return result
        result["hops"] = [*result["hops"], current]
        try:
            async with client.stream("GET", current) as response:
                result["finalUrl"] = str(response.url)
                result["contentType"] = response.headers.get("content-type")
                result["headers"] = {key.lower(): value
                                     for key, value in response.headers.items()}
                result["cookies"] = cookie_names(response.headers)
                location = response.headers.get("location")
                if response.is_redirect and location:
                    current = urljoin(str(response.url), location)
                    continue
                result["httpStatus"] = response.status_code
                if response.status_code >= 400:
                    result["status"] = ST_HTTP
                    result["reason"] = (
                        f"the server answered HTTP {response.status_code}: not scanned "
                        "and not charged")
                    return result
                # Only 200 and 201 carry a page. 202 and 203 mean "accepted, no content"
                # and are exactly what an anti-bot edge answers while it decides whether
                # to show a challenge: booking.com and imdb.com both answered 202 with
                # 8.8 KB and 2 KB of nothing, and that was sold as a scanned site.
                if response.status_code not in (200, 201):
                    result["status"] = ST_NO_CONTENT
                    result["reason"] = (
                        f"the server answered HTTP {response.status_code} with no page in "
                        "it: not scanned and not charged")
                    return result
                if not is_page(result["contentType"]):
                    result["status"] = ST_NOT_HTML
                    result["reason"] = (
                        f"the response is {result['contentType']}, not a page: not "
                        "scanned and not charged")
                    return result
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_HTML_BYTES:
                        result["status"] = ST_TOO_LARGE
                        result["reason"] = (
                            f"the page is larger than {MAX_HTML_BYTES // (1024 * 1024)} "
                            "MB, the part that carries the marks was not reached: not "
                            "scanned and not charged")
                        return result
                    chunks.append(chunk)
                body = b"".join(chunks)
                result["body"] = body
                if not body.strip():
                    result["status"] = ST_EMPTY_BODY
                    result["reason"] = ("the server answered with an empty body: not a "
                                        "page, not scanned and not charged")
                    return result
                result["html"] = decode_body(body, result["contentType"])
                return result
        except httpx.TimeoutException:
            result["status"] = ST_TIMEOUT
            result["reason"] = ("the page did not answer in time: not scanned and not "
                                "charged")
            return result
        except httpx.HTTPError as exc:
            result["status"] = ST_FETCH
            result["reason"] = (f"{type(exc).__name__}: the page could not be read, not "
                                "scanned and not charged")
            return result
    result["status"] = ST_FETCH
    result["reason"] = (f"the page redirected more than {MAX_REDIRECTS} times: not "
                        "scanned and not charged")
    return result


async def fetch_page(client: httpx.AsyncClient, url: str,
                     budget_s: float | None = None) -> dict:
    """One site, on a wall clock for the whole site and not for one request.

    A server that drips 100 bytes every 3 seconds passes a 20 s per-request timeout and
    still eats a minute: with 40 s left and a 26 s margin that is the platform killing
    the run before the summary is written, which loses every row of the run. The site
    gets `left - margin` seconds instead, and a site that overruns it is one free row
    with the time reason.
    """
    if not budget_s or budget_s <= 0:
        return await _fetch_page(client, url)
    try:
        async with asyncio.timeout(budget_s):
            return await _fetch_page(client, url)
    except TimeoutError:
        return {"body": None, "html": None, "finalUrl": url, "hops": [url],
                "status": ST_TIME,
                "reason": (f"the site did not finish within {budget_s:.0f} s left before "
                           "the platform deadline: not scanned and not charged"),
                "contentType": None, "httpStatus": None, "headers": {}, "cookies": []}


def make_row(url: str, line: str, index: int, page: dict, items: list[dict],
             wanted_ok: bool, filter_reason: str | None) -> dict:
    names = [str(item.get("name")) for item in items]
    return {
        "site": host_of(page.get("finalUrl") or url),
        "url": url,
        "finalUrl": page.get("finalUrl"),
        "input": line,
        "inputIndex": index,
        "status": ST_OK if wanted_ok else ST_FILTERED,
        "reason": filter_reason,
        "billable": False,
        "httpStatus": page.get("httpStatus"),
        "contentType": page.get("contentType"),
        "redirects": max(0, len(page.get("hops") or []) - 1),
        "server": (page.get("headers") or {}).get("server"),
        "poweredBy": (page.get("headers") or {}).get("x-powered-by"),
        "detected": len(items),
        "technologies": items,
        "technologyNames": names,
        "categories": tech.categories_present(items),
        "versionsFound": {item["name"]: item["version"] for item in items
                          if item.get("version")},
    }


async def main() -> None:
    async with Actor:
        raw_input = await Actor.get_input() or {}
        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()
        can_charge, charge_reason = price_state(charging)
        # billable is a claim about the price, not a receipt: one paid push writes the
        # row first and charges second, so a row whose charge fails is already in the
        # dataset. The money really taken is chargedEvents in the summary.
        billable_claim = None if can_charge is None else bool(can_charge)

        lines = raw_input.get("urls") or []
        if isinstance(lines, str):
            lines = [lines]
        lines = [str(item) for item in lines]

        try:
            max_sites = int(raw_input.get("maxSites") or 0)
        except (TypeError, ValueError):
            max_sites = 0
        if max_sites <= 0:
            max_sites = MAX_SITES_DEFAULT
        try:
            timeout_s = float(raw_input.get("timeoutSecs") or 20)
        except (TypeError, ValueError):
            timeout_s = 20.0
        timeout_s = max(5.0, min(timeout_s, 90.0))
        wanted = tech.normalise_wanted(raw_input.get("onlyIf"))
        forbidden = tech.normalise_wanted(raw_input.get("excludeIf"))
        user_agent = str(raw_input.get("userAgent") or "").strip()
        if not user_agent:
            user_agent = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        include_evidence = bool(raw_input.get("includeEvidence", True))

        margin_s = site_margin_seconds(timeout_s)
        tasks, counts = build_tasks(lines, max_sites)

        by_status: dict[str, int] = {}
        by_category: dict[str, int] = {}
        technology_seen: dict[str, int] = {}
        rows_written = 0
        delivered = 0
        charged_total = 0
        delivered_uncharged = 0
        charge_errors = 0
        write_errors = 0
        private_blocked = 0
        dns_errors = 0
        http_errors = 0
        not_html = 0
        timeouts = 0
        fetch_errors = 0
        too_large = 0
        filtered_out = 0
        empty_scan = 0
        blocked = 0
        no_content = 0
        empty_pages = 0
        stop_reason: str | None = None
        deadline_capped = False
        unexpected_error: str | None = None
        dataset_api_count: int | None = None
        dataset_note: str | None = None
        billing_active = can_charge is True
        stopping = False
        refusals_in_a_row = 0
        failed_at = 0

        def note(status: str) -> None:
            by_status[status] = by_status.get(status, 0) + 1

        async def write_free(row: dict) -> bool:
            nonlocal rows_written, write_errors, dataset_note
            nonlocal stopping, stop_reason, refusals_in_a_row
            row["billable"] = False
            try:
                await dataset.push_data(row)
            except Exception:  # noqa: BLE001
                write_errors += 1
                refusals_in_a_row += 1
                dataset_note = "a free status row was refused by the dataset"
                if refusals_in_a_row >= MAX_REFUSALS_IN_A_ROW:
                    stopping = True
                    stop_reason = stop_reason or "dataset-write-error"
                return False
            rows_written += 1
            refusals_in_a_row = 0
            return True

        async def stop_row(line: str, index: int) -> None:
            status, reason = stopped_status(stop_reason)
            note(status)
            if stop_reason == "dataset-write-error":
                return
            await write_free(status_row(line, index, status, reason))

        client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_s),
            headers={
                "user-agent": user_agent,
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
            },
        )
        try:
            for position, task in enumerate(tasks):
                failed_at = position
                if task["kind"] == "row":
                    row = task["row"]
                    written = await write_free(row)
                    note(row["status"] if written else ST_ERROR)
                    continue

                if stopping:
                    await stop_row(task["line"], task["index"])
                    continue

                left = deadline_seconds()
                if left is not None and left < margin_s:
                    deadline_capped = True
                    stopping = True
                    stop_reason = stop_reason or "deadline"
                    note(ST_TIME)
                    await write_free(status_row(
                        task["line"], task["index"], ST_TIME,
                        "the platform deadline was close, nothing was delivered and "
                        "nothing was charged"))
                    continue

                if can_charge is None:
                    stopping = True
                    stop_reason = "billing-price-unknown"
                    note(ST_BILLING)
                    await write_free(status_row(
                        task["line"], task["index"], ST_BILLING,
                        f"the price could not be read ({charge_reason}): not delivered "
                        "and not charged"))
                    continue

                if can_charge and charging.is_event_charge_limit_reached(EVENT_NAME):
                    stopping = True
                    stop_reason = "spending-cap"
                    note(ST_CAP)
                    await write_free(status_row(
                        task["line"], task["index"], ST_CAP,
                        "the spending cap was reached: not delivered and not charged"))
                    continue

                # The first hop is checked here, in the open, so the buyer sees the
                # exact answer: a name that resolved inside the private network is not
                # the same as a typo, and neither is a scan.
                guard_ok, guard_code, guard_why = host_verdict(host_of(task["url"]) or "")
                if not guard_ok:
                    status = ST_PRIVATE if guard_code == CODE_PRIVATE else ST_DNS
                    if status == ST_PRIVATE:
                        private_blocked += 1
                    else:
                        dns_errors += 1
                    note(status)
                    await write_free(status_row(
                        task["line"], task["index"], status,
                        f"the address cannot be used, the page was not opened: "
                        f"{guard_why}. Nothing was delivered and nothing was charged"))
                    continue

                left_now = deadline_seconds()
                budget_s = None if left_now is None else max(5.0, left_now - margin_s)
                try:
                    page = await fetch_page(client, task["url"], budget_s)
                except Exception as exc:  # noqa: BLE001 - one site never kills the run
                    fetch_errors += 1
                    note(ST_FETCH)
                    await write_free(status_row(
                        task["line"], task["index"], ST_FETCH,
                        f"{type(exc).__name__}: the page could not be read, nothing was "
                        "delivered and nothing was charged"))
                    continue

                if page["status"]:
                    status = page["status"]
                    if status == ST_PRIVATE:
                        private_blocked += 1
                    elif status == ST_DNS:
                        dns_errors += 1
                    elif status == ST_HTTP:
                        http_errors += 1
                    elif status == ST_NOT_HTML:
                        not_html += 1
                    elif status == ST_TIMEOUT:
                        timeouts += 1
                    elif status == ST_TOO_LARGE:
                        too_large += 1
                    elif status == ST_NO_CONTENT:
                        no_content += 1
                    elif status == ST_EMPTY_BODY:
                        empty_pages += 1
                    else:
                        fetch_errors += 1
                    note(status)
                    await write_free(status_row(task["line"], task["index"], status,
                                                page["reason"]))
                    continue

                html = page.get("html") or ""
                challenge = blocked_page(html, page.get("headers") or {},
                                         len(page.get("body") or b""))
                if challenge:
                    blocked += 1
                    note(ST_BLOCKED)
                    await write_free(status_row(
                        task["line"], task["index"], ST_BLOCKED,
                        f"{challenge}: no technologies were read and nothing was "
                        "charged. Try userAgent or another address for this site"))
                    continue
                if not visible_text_len(html):
                    # Nothing to read: a script-only stub, not a site. Selling the
                    # absence of text as "no technology found" hides what happened.
                    empty_pages += 1
                    note(ST_EMPTY_BODY)
                    await write_free(status_row(
                        task["line"], task["index"], ST_EMPTY_BODY,
                        "the answer carries no visible text at all, so there is no page "
                        "to read: not scanned and not charged"))
                    continue

                scripts, metas = tech.parse_page(html)
                signals = tech.page_signals(html, page.get("headers") or {},
                                            page.get("cookies") or [], scripts, metas)
                items = tech.detect(signals)
                if not include_evidence:
                    items = [{key: value for key, value in item.items()
                              if key != "evidence"} for item in items]
                matched, filter_reason = tech.matches(items, wanted, forbidden)
                row = make_row(task["url"], task["line"], task["index"], page, items,
                               matched, filter_reason)
                for category in row["categories"]:
                    by_category[category] = by_category.get(category, 0) + 1
                for name in row["technologyNames"]:
                    technology_seen[name] = technology_seen.get(name, 0) + 1

                if not matched:
                    # A filter is a promise about the money: a site the buyer excluded
                    # costs nothing, and the row is still there so the list adds up.
                    filtered_out += 1
                    note(ST_FILTERED)
                    await write_free(row)
                    continue

                if not items:
                    # Nothing matched a signature, so there is nothing to sell: the site
                    # was read, and the answer "none of our signatures is here" is free.
                    # Charging for it would be money for a blank line, and a page whose
                    # technologies only exist after javascript would be exactly that.
                    empty_scan += 1
                    row["status"] = ST_EMPTY
                    row["reason"] = ("the page was read and none of the "
                                     f"{len(tech.SIGNATURES)} signatures matched: not "
                                     "charged")
                    note(ST_EMPTY)
                    await write_free(row)
                    continue

                if can_charge:
                    if charging.is_event_charge_limit_reached(EVENT_NAME):
                        note(ST_CAP)
                        await write_free(status_row(
                            task["line"], task["index"], ST_CAP,
                            "the spending cap was reached: not delivered and not "
                            "charged"))
                        stopping = True
                        stop_reason = "spending-cap"
                        continue
                    try:
                        result = await Actor.push_data(
                            {**row, "billable": billable_claim},
                            charged_event_name=EVENT_NAME,
                        )
                    except Exception as exc:  # noqa: BLE001
                        # One SDK call writes the row and then charges for it, so an
                        # exception here does not say which half failed. A second status
                        # row for the same input would hand the buyer two rows for one
                        # site, and calling it a dataset refusal would be a guess.
                        charge_errors += 1
                        refusals_in_a_row = 0
                        delivered_uncharged += 1
                        rows_written += 1
                        note(ST_OK)
                        stopping = True
                        stop_reason = "charge-error"
                        dataset_note = (
                            f"a paid row could not be confirmed after "
                            f"{type(exc).__name__}: it may be in the dataset and it was "
                            "not charged")
                        continue
                    charged = int(getattr(result, "charged_count", 0) or 0)
                    limit_reached = bool(getattr(result, "event_charge_limit_reached",
                                                 False))
                    if charged <= 0:
                        status = ST_CAP if limit_reached else ST_ERROR
                        note(status)
                        await write_free(status_row(
                            task["line"], task["index"], status,
                            "the spending cap was reached: not delivered and not "
                            "charged" if limit_reached
                            else "nothing was written and nothing was charged"))
                        stopping = True
                        stop_reason = ("spending-cap" if limit_reached
                                       else "charge-error")
                        continue
                    rows_written += 1
                    delivered += 1
                    charged_total += charged
                    refusals_in_a_row = 0
                    note(ST_OK)
                    if limit_reached:
                        stopping = True
                        stop_reason = "spending-cap"
                    continue

                row["billable"] = False
                written = await write_free(row)
                note(ST_OK if written else ST_ERROR)
        except Exception as exc:  # noqa: BLE001 - a crash must not lose the summary
            unexpected_error = f"{type(exc).__name__}: {exc}"[:300]
            stop_reason = stop_reason or "unexpected-error"
            # The tail gets its own free row with the reason, otherwise the counters do
            # not add up and the buyer cannot tell a site that was never tried from one
            # that was scanned and lost.
            for rest in tasks[failed_at:]:
                if rest.get("kind") == "row":
                    row = rest.get("row") or {}
                    line = row.get("input") or ""
                    index = row.get("inputIndex") or 0
                else:
                    line = rest.get("line") or ""
                    index = rest.get("index") or 0
                note(ST_ERROR)
                await write_free(status_row(
                    line, index, ST_ERROR,
                    "the run stopped on an unexpected error before this site: nothing "
                    "was charged"))
        finally:
            await client.aclose()
            dataset_api_count = await dataset_count(dataset)

        processed = by_status.get(ST_OK, 0)
        filtered = by_status.get(ST_FILTERED, 0)
        empty = by_status.get(ST_EMPTY, 0)
        asked = counts["asked"]
        # Not a tautology any more: notChecked is read from the statuses that were
        # actually written, so a row that is neither delivered, filtered, empty nor
        # marked NOT_CHECKED has to be explained instead of being absorbed by the
        # subtraction that defined it.
        not_checked = sum(count for status, count in by_status.items()
                          if status.startswith("NOT_CHECKED"))
        expected_not_checked = asked - processed - filtered - empty
        if not_checked != expected_not_checked:
            by_status["counterMismatch"] = expected_not_checked - not_checked

        top = sorted(technology_seen.items(), key=lambda item: (-item[1], item[0]))[:20]
        summary = {
            "requested": asked,
            "uniqueSites": counts["unique"],
            "sitesAttempted": counts["unique"] - counts["limit"],
            "duplicates": counts["duplicates"],
            "invalidLines": counts["invalid"],
            "blankLines": counts["blank"],
            "processed": processed,
            "notChecked": not_checked,
            "filteredOut": filtered,
            "noTechnologyFound": empty,
            "blockedPages": blocked,
            "noContentPages": no_content,
            "emptyPages": empty_pages,
            "delivered": processed,
            "rowsWritten": rows_written,
            "chargedEvents": charged_total if billing_active else 0,
            "billingActive": billing_active,
            "billingReason": charge_reason,
            "deliveredUncharged": delivered_uncharged,
            "chargeErrors": charge_errors,
            "writeErrors": write_errors,
            "stopReason": stop_reason,
            "unexpectedError": unexpected_error,
            "byStatus": by_status,
            "byCategory": dict(sorted(by_category.items())),
            "topTechnologies": {name: count for name, count in top},
            "onlyIf": wanted,
            "excludeIf": forbidden,
            "billableOnlyWhenMatched": bool(wanted or forbidden),
            "maxSites": max_sites,
            "cutByMaxSites": counts["limit"],
            "deadlineCapped": deadline_capped,
            "siteMarginSecs": round(margin_s, 1),
            "privateBlocked": private_blocked,
            "dnsErrors": dns_errors,
            "httpErrors": http_errors,
            "notHtml": not_html,
            "tooLarge": too_large,
            "timeouts": timeouts,
            "fetchErrors": fetch_errors,
            "signatures": len(tech.SIGNATURES),
            "datasetItemCount": rows_written,
            "datasetItemCountApi": dataset_api_count,
            "datasetItemCountNote": dataset_note,
            "priceEvent": EVENT_NAME,
            "runId": run_id(),
            "finishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if dataset_api_count is not None and dataset_api_count < rows_written:
            lag = f"metadata lag: expected {rows_written}, reports {dataset_api_count}"
            summary["datasetItemCountNote"] = (
                f"{dataset_note}; {lag}" if dataset_note else lag)

        store = await Actor.open_key_value_store(name=SUMMARY_STORE)
        await store.set_value(f"RUN_SUMMARY-{run_id()}", summary)
        default_store = await Actor.open_key_value_store()
        await default_store.set_value("RUN_SUMMARY", summary)

        # The header is a receipt, so it is built from the money that was really taken:
        # "scanned and charged" on a run whose charge failed was a lie the buyer could
        # not see through. The number of sites is the number this run worked on, not
        # every distinct url, because maxSites cutting the list is not a scan.
        billed = ("scanned and charged" if charged_total > 0
                  else "scanned, nothing charged")
        attempted = counts["unique"] - counts["limit"]
        text = (f"{attempted} site{'' if attempted == 1 else 's'} from "
                f"{asked} line{'' if asked == 1 else 's'}, {processed} {billed}")
        if filtered:
            text += (f", {filtered} site{'' if filtered == 1 else 's'} did not match the "
                     f"filter and {'was' if filtered == 1 else 'were'} free")
        if empty:
            text += (f", {empty} site{'' if empty == 1 else 's'} had no known technology "
                     f"and {'was' if empty == 1 else 'were'} free")
        if not_checked:
            text += f", {not_checked} not checked"
        if counts["limit"]:
            text += f", {counts['limit']} cut by maxSites and free"
        if not billing_active:
            text += f" (billing is off: {charge_reason})"
        if stop_reason:
            text += f", stopped: {stop_reason}"
        await Actor.set_status_message(text)

        # A green run with nothing delivered is the same defect as a traceback: the
        # buyer sees SUCCEEDED and an empty dataset and cannot tell why.
        if asked == 0:
            await Actor.fail(
                status_message=(
                    "No urls were sent: nothing to scan, nothing charged. Put one site "
                    "url per line into the urls field."))
        if unexpected_error:
            await Actor.fail(
                status_message=(
                    f"The run stopped on an unexpected error ({unexpected_error}). "
                    f"{processed} of {asked} site(s) delivered, {not_checked} not "
                    f"checked, {charged_total if billing_active else 0} charged."))
        if asked > 0 and processed == 0 and not filtered and not empty:
            breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
            await Actor.fail(
                status_message=(
                    f"Nothing could be scanned: {breakdown or 'no rows written'}. "
                    "Nothing was charged."))


if __name__ == "__main__":
    asyncio.run(main())
