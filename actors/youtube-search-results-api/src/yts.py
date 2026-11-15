"""YouTube search through the public innertube endpoint, parsed into plain rows.

No browser, no API key, no proxy: one POST per page, and the continuation token the
page itself hands back. Measured on 2026-11-12 from a datacenter address: a search
page returns 9-13 videos and a token, and three pages give 36 unique videos, so the
depth is real and not a promise.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.request

ENDPOINT = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
CLIENT_VERSION = "2.20240726.00.00"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_RESULTS_DEFAULT = 50
MAX_RESULTS_CLAMP = 500

# Measured 2026-11-12: the endpoint ignores the date and rating sort tokens
# (identical first page, overlap 13 of 13 and 11 of 13), so only the two that
# really reorder the page are offered. Depth comes from the date filter instead.
SORT_VALUES = ("relevance", "views")
DATE_VALUES = ("any", "hour", "today", "week", "month", "year")
DURATION_VALUES = ("any", "short", "medium", "long")

# Protobuf search filters, the same ones the site puts in the URL. Measured, not guessed:
# the date bucket changes the first page, see tests/test_params.py.
_SORT_PARAMS = {"relevance": "", "date": "CAI=", "views": "CAM=", "rating": "CAE="}
_DURATION_PARAMS = {"any": "", "short": "EgIYAQ==", "medium": "EgIYAw==", "long": "EgIYAg=="}
_DATE_PARAMS = {"any": "", "hour": "EgIIAQ==", "today": "EgIIAg==", "week": "EgIIAw==",
                "month": "EgIIBA==", "year": "EgIIBQ=="}


def _text(node) -> str | None:
    """innertube carries text either as simpleText or as runs; both are text."""
    if not isinstance(node, dict):
        return None
    if "simpleText" in node:
        return node["simpleText"]
    runs = node.get("runs")
    if isinstance(runs, list):
        joined = "".join(str(r.get("text") or "") for r in runs if isinstance(r, dict))
        return joined or None
    return None


def clean(value) -> str:
    return str(value or "").strip()


def clamp_results(value, default: int = MAX_RESULTS_DEFAULT) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, MAX_RESULTS_CLAMP))


def _varint(value: int) -> bytes:
    out = b""
    while True:
        chunk = value & 0x7F
        value >>= 7
        out += bytes([chunk | (0x80 if value else 0)])
        if not value:
            return out


def _field_varint(number: int, value: int) -> bytes:
    return _varint((number << 3) | 0) + _varint(value)


def _field_bytes(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def search_params(sort_by: str, upload_date: str, duration: str) -> str:
    """Combine the filters into the single protobuf token the endpoint expects.

    The site puts one base64 protobuf in `params`; joining tokens would be a different
    message and the server would ignore it. Field layout: 1 = sort, 2 = filters,
    filters.1 = upload date, filters.3 = duration.
    """
    sort_map = {"relevance": 0, "rating": 1, "date": 2, "views": 3}
    date_map = {"any": 0, "hour": 1, "today": 2, "week": 3, "month": 4, "year": 5}
    duration_map = {"any": 0, "short": 1, "long": 2, "medium": 3}

    filters = b""
    if date_map.get(upload_date, 0):
        filters += _field_varint(1, date_map[upload_date])
    if duration_map.get(duration, 0):
        filters += _field_varint(3, duration_map[duration])

    payload = b""
    if sort_map.get(sort_by, 0):
        payload += _field_varint(1, sort_map[sort_by])
    if filters:
        payload += _field_bytes(2, filters)
    return base64.b64encode(payload).decode("ascii") if payload else ""


def build_body(query: str, region: str, language: str, params: str = "",
               continuation: str | None = None) -> dict:
    context = {"client": {"clientName": "WEB", "clientVersion": CLIENT_VERSION,
                          "hl": language or "en", "gl": region or "US"}}
    if continuation:
        return {"context": context, "continuation": continuation}
    body = {"context": context, "query": query}
    if params:
        body["params"] = params
    return body


def post(body: dict, timeout: float = 30.0) -> dict:
    """One request. Raises on network trouble; the caller turns that into a status."""
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                 "X-YouTube-Client-Name": "1", "X-YouTube-Client-Version": CLIENT_VERSION,
                 "Origin": "https://www.youtube.com"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def walk(node, videos: list, tokens: list) -> None:
    """Collect videoRenderer nodes and continuation tokens, in page order."""
    if isinstance(node, dict):
        renderer = node.get("videoRenderer")
        if isinstance(renderer, dict):
            videos.append(renderer)
        item = node.get("continuationItemRenderer")
        if isinstance(item, dict):
            token = ((item.get("continuationEndpoint") or {}).get("continuationCommand")
                     or {}).get("token")
            if token:
                tokens.append(token)
        for value in node.values():
            walk(value, videos, tokens)
    elif isinstance(node, list):
        for value in node:
            walk(value, videos, tokens)


def count_shorts(node: object) -> int:
    """Count Shorts shelf entries on a search page.

    YouTube renders Shorts as shortsLockupViewModel, not as videoRenderer, so a
    page can carry dozens of videos that never become rows. The count is reported
    in the summary as shortsSkipped instead of vanishing without a trace.
    """
    if isinstance(node, dict):
        total = 1 if isinstance(node.get("shortsLockupViewModel"), dict) else 0
        return total + sum(count_shorts(value) for value in node.values())
    if isinstance(node, list):
        return sum(count_shorts(value) for value in node)
    return 0


def parse_page(payload: dict) -> tuple[list[dict], str | None]:
    videos: list[dict] = []
    tokens: list[str] = []
    walk(payload, videos, tokens)
    return videos, (tokens[0] if tokens else None)


_DURATION = re.compile(r"^(\d+)(?::(\d{1,2}))?(?::(\d{1,2}))?$")


def duration_seconds(text: str | None) -> int | None:
    """'12:00:00' -> 43200, '9:41' -> 581, 'LIVE' -> None."""
    if not text:
        return None
    match = _DURATION.match(text.strip())
    if not match:
        return None
    groups = [g for g in match.groups() if g is not None]
    numbers = [int(g) for g in groups]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0]


def view_count(text: str | None) -> int | None:
    """'13,456,363 views' -> 13456363, '1.2M views' -> 1200000, 'No views' -> 0.

    None means the page did not say, which is not the same as zero.
    """
    if not text:
        return None
    raw = text.strip().replace("\xa0", " ")
    if raw.lower().startswith("no views"):
        return 0
    match = re.search(r"([\d][\d.,\s]*)\s*([KMB])?", raw, re.I)
    if not match:
        return None
    number_text = match.group(1).replace(" ", "").replace(",", "")
    suffix = (match.group(2) or "").upper()
    if suffix and "." in number_text:
        try:
            value = float(number_text)
        except ValueError:
            return None
    else:
        digits = re.sub(r"[^\d]", "", number_text)
        if not digits:
            return None
        value = float(digits)
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
    return int(value * multiplier)


_AGO = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
_DAYS = {"second": 0, "minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def days_ago(text: str | None) -> int | None:
    """'2 years ago' -> 730. This is a lower bound in days, not an exact date.

    The page never carries the upload timestamp, so the row says "about this many days"
    and never pretends to know the exact date.
    """
    if not text:
        return None
    match = _AGO.search(text)
    if not match:
        return None
    return int(match.group(1)) * _DAYS[match.group(2).lower()]


def parse_video(renderer: dict, query: str) -> dict | None:
    """One search result as a row. A result without an id is not a result."""
    if not isinstance(renderer, dict):
        return None
    video_id = clean(renderer.get("videoId"))
    if not video_id:
        return None
    title = _text(renderer.get("title")) or ""
    channel_node = renderer.get("ownerText") or renderer.get("longBylineText") or {}
    channel = _text(channel_node) or ""
    channel_id = ""
    runs = (channel_node.get("runs") or []) if isinstance(channel_node, dict) else []
    for run in runs:
        endpoint = ((run.get("navigationEndpoint") or {}).get("browseEndpoint") or {})
        if endpoint.get("browseId"):
            channel_id = str(endpoint["browseId"])
            break
    views_text = _text(renderer.get("viewCountText")) or ""
    published_text = _text(renderer.get("publishedTimeText")) or ""
    duration_text = _text(renderer.get("lengthText")) or ""
    description = ""
    snippets = renderer.get("detailedMetadataSnippets") or []
    if snippets and isinstance(snippets[0], dict):
        description = (_text(snippets[0].get("snippetText")) or "").strip()
    thumbs = ((renderer.get("thumbnail") or {}).get("thumbnails") or [])
    thumbnail = thumbs[-1].get("url") if thumbs and isinstance(thumbs[-1], dict) else None
    badges = []
    for badge in renderer.get("badges") or []:
        label = ((badge or {}).get("metadataBadgeRenderer") or {}).get("label")
        if label:
            badges.append(str(label))
    row = {
        "query": query,
        "videoId": video_id,
        "url": "https://www.youtube.com/watch?v=" + video_id,
        "title": title,
        "channel": channel,
        "channelId": channel_id or None,
        "channelUrl": ("https://www.youtube.com/channel/" + channel_id) if channel_id else None,
        "views": view_count(views_text),
        "viewsText": views_text or None,
        "publishedText": published_text or None,
        "publishedDaysAgo": days_ago(published_text),
        "durationText": duration_text or None,
        "durationSeconds": duration_seconds(duration_text),
        "isLive": bool(renderer.get("isLiveNow")) or None,
        "badges": badges or None,
        "description": description or None,
        "thumbnailUrl": thumbnail,
    }
    return row
