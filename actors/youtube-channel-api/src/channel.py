"""Channel reading: resolve a channel, open its Videos tab, page through it.

Measured 2026-11-12, not assumed:

  * the channel page carries five tabs (Home, Videos, Shorts, Shows, Posts) and each
    tab has its own params, so the params are read from the page itself;
  * the Videos tab is rendered with ``lockupViewModel`` on the current layout, not with
    ``videoRenderer``: the id sits in ``contentId``, the title in
    ``metadata.lockupMetadataViewModel.title.content``, and views and age in
    ``metadataRows[0].metadataParts``;
  * one page carried 30 videos and a continuation token;
  * the subscriber line is ``subscriberCountText.simpleText`` ("30.7M subscribers");
  * no browser, no API key and no proxy are needed, and it answers from a datacenter.
"""
from __future__ import annotations

import json
import re
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ENDPOINT = "https://www.youtube.com/youtubei/v1/"
CLIENT_VERSION = "2.20241126.01.00"
MAX_VIDEOS_CEILING = 1000
DEFAULT_MAX_VIDEOS = 50

_NUMBER = re.compile(r"([\d][\d.,\s]*)\s*([KMB])?", re.I)
_DURATION = re.compile(r"^(\d+)(?::(\d{1,2}))?(?::(\d{1,2}))?$")
_AGO = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
_UNIT_DAYS = {"second": 0, "minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def clean(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def clamp_max(value: object) -> int:
    """The per-channel limit, kept honest: junk means the default, not zero."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_VIDEOS
    if number < 1:
        return 1
    return min(number, MAX_VIDEOS_CEILING)


def context(region: str, language: str) -> dict:
    return {"client": {"clientName": "WEB", "clientVersion": CLIENT_VERSION,
                       "hl": language, "gl": region}}


def post(body: dict, timeout: float = 30.0) -> dict:
    url = ENDPOINT + body.pop("_endpoint", "browse") + "?prettyPrint=false"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                 "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def build_resolve(url: str, ctx: dict) -> dict:
    return {"_endpoint": "navigation/resolve_url", "context": ctx, "url": url}


def build_browse(browse_id: str, ctx: dict, params: str | None = None,
                 continuation: str | None = None) -> dict:
    body: dict = {"context": ctx}
    if continuation:
        body["continuation"] = continuation
    else:
        body["browseId"] = browse_id
        if params:
            body["params"] = params
    return body


def find_all(node: object, key: str, out: list) -> list:
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                out.append(value)
            find_all(value, key, out)
    elif isinstance(node, list):
        for value in node:
            find_all(value, key, out)
    return out


def channel_url(value: object) -> str:
    """Turn a handle, a channel id or a url into a page the resolve endpoint takes."""
    text = clean(value)
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return text
    if lowered.startswith("youtube.com/") or lowered.startswith("www.youtube.com/"):
        return "https://" + text.split("://")[-1]
    if text.startswith("@"):
        return "https://www.youtube.com/" + text
    if text.startswith("UC") and 20 <= len(text) <= 26:
        return "https://www.youtube.com/channel/" + text
    return "https://www.youtube.com/@" + text


def channel_id(payload: dict) -> str:
    for endpoint in find_all(payload, "browseEndpoint", []):
        if isinstance(endpoint, dict):
            candidate = clean(endpoint.get("browseId"))
            if candidate.startswith("UC"):
                return candidate
    return ""


def channel_info(payload: dict) -> tuple[str, str, dict]:
    """Title, the subscriber line and the params of every tab, read from the page."""
    title = ""
    for meta in find_all(payload, "channelMetadataRenderer", []):
        if isinstance(meta, dict) and clean(meta.get("title")):
            title = clean(meta.get("title"))
            break
    subscribers = ""
    for line in find_all(payload, "subscriberCountText", []):
        if isinstance(line, dict):
            text = clean(line.get("simpleText"))
            if not text:
                runs = line.get("runs") or []
                text = " ".join(clean(r.get("text")) for r in runs if isinstance(r, dict)).strip()
            if text:
                subscribers = text
                break
    tabs: dict[str, str] = {}
    for tab in find_all(payload, "tabRenderer", []):
        if not isinstance(tab, dict):
            continue
        name = clean(tab.get("title")).lower()
        endpoint = tab.get("endpoint") or {}
        browse = endpoint.get("browseEndpoint") or {}
        params = clean(browse.get("params"))
        if name and params:
            tabs[name] = params
    return title, subscribers, tabs


def number_from_text(text: str) -> int | None:
    """'92M views' -> 92000000, '1,234 views' -> 1234, 'No views' -> 0."""
    raw = clean(text)
    if not raw:
        return None
    if raw.lower().startswith("no views"):
        return 0
    match = _NUMBER.search(raw)
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
    return int(value * {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix])


def duration_seconds(text: str | None) -> int | None:
    """'22:28' -> 1348, '1:02:03' -> 3723, 'LIVE' -> None."""
    raw = clean(text)
    if not raw:
        return None
    match = _DURATION.match(raw.strip())
    if not match:
        return None
    numbers = [int(g) for g in match.groups() if g is not None]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0]


def age_days(text: str | None) -> int | None:
    """'11 days ago' -> 11, '2 years ago' -> 730. A lower bound, not a date."""
    raw = clean(text)
    if not raw:
        return None
    match = _AGO.search(raw)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    return amount * _UNIT_DAYS[unit]


def _metadata_parts(lock: dict) -> list[str]:
    metadata = lock.get("metadata") or {}
    holder = metadata.get("lockupMetadataViewModel") or {}
    rows = (((holder.get("metadata") or {}).get("contentMetadataViewModel") or {})
            .get("metadataRows") or [])
    parts: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for part in row.get("metadataParts") or []:
            if isinstance(part, dict):
                text = clean((part.get("text") or {}).get("content"))
                if text:
                    parts.append(text)
    return parts


def _title(lock: dict) -> str:
    metadata = lock.get("metadata") or {}
    holder = metadata.get("lockupMetadataViewModel") or {}
    return clean((holder.get("title") or {}).get("content"))


def _duration_text(lock: dict) -> str:
    for badge in find_all(lock, "thumbnailBadgeViewModel", []):
        if isinstance(badge, dict):
            text = clean(badge.get("text"))
            if text and _DURATION.match(text.strip()):
                return text
    return ""


def _thumbnail(lock: dict) -> str:
    image = ((lock.get("contentImage") or {}).get("thumbnailViewModel") or {}).get("image") or {}
    sources = image.get("sources") or []
    if sources and isinstance(sources[-1], dict):
        return clean(sources[-1].get("url"))
    return ""


def parse_video(node: object, channel: str, channel_title: str, channel_id_value: str,
                subscribers: str, position: int) -> dict | None:
    """One lockup entry as a row, or None when it is not a video."""
    if not isinstance(node, dict):
        return None
    if clean(node.get("contentType")) not in ("", "LOCKUP_CONTENT_TYPE_VIDEO"):
        return None
    video_id = clean(node.get("contentId"))
    if not video_id:
        return None
    title = _title(node)
    parts = _metadata_parts(node)
    views_text = ""
    age_text = ""
    for part in parts:
        if "view" in part.lower() and not views_text:
            views_text = part
        elif not age_text:
            age_text = part
    duration_text = _duration_text(node)
    return {
        "channelInput": channel,
        "channelId": channel_id_value,
        "channelTitle": channel_title,
        "channelSubscribersText": subscribers or None,
        "videoId": video_id,
        "url": "https://www.youtube.com/watch?v=" + video_id,
        "title": title or None,
        "views": number_from_text(views_text),
        "viewsText": views_text or None,
        "ageText": age_text or None,
        "ageDays": age_days(age_text),
        "durationText": duration_text or None,
        "durationSeconds": duration_seconds(duration_text),
        "position": position,
        "thumbnailUrl": _thumbnail(node) or None,
    }


def parse_page(payload: dict) -> tuple[list[dict], str | None]:
    """Video lockups in page order plus the continuation token, if any."""
    entries = [item for item in find_all(payload, "lockupViewModel", [])
               if isinstance(item, dict)
               and clean(item.get("contentType")) in ("", "LOCKUP_CONTENT_TYPE_VIDEO")]
    token = None
    for item in find_all(payload, "continuationCommand", []):
        if isinstance(item, dict) and clean(item.get("token")):
            token = clean(item.get("token"))
            break
    return entries, token
