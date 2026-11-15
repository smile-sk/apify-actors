"""Shorts reading: resolve a channel, open its Shorts shelf, page through it.

Measured 2026-11-12, not assumed:

  * the public innertube endpoint answers without a key, without a browser and
    from a datacenter;
  * a channel page carries five tabs, and the Shorts tab has its own params, so
    the params are taken from the page instead of being hardcoded;
  * one Shorts page carried 48 entries and the continuation token kept giving
    48 more: three pages gave 144 unique videos;
  * entries are ``shortsLockupViewModel`` with the id inside ``entityId`` and
    the title and view text inside ``overlayMetadata``.
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
MAX_SHORTS_CEILING = 1000
DEFAULT_MAX_SHORTS = 100

_VIEWS = re.compile(r"([\d][\d.,\s]*)\s*([KMB])?", re.I)


def clean(value: object) -> str:
    """A trimmed single-line string, or an empty one."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def clamp_max(value: object) -> int:
    """The per-channel limit, kept honest: junk means the default, not zero."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_SHORTS
    if number < 1:
        return 1
    return min(number, MAX_SHORTS_CEILING)


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
    """Every value stored under ``key`` anywhere in the tree, in page order."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                out.append(value)
            find_all(value, key, out)
    elif isinstance(node, list):
        for value in node:
            find_all(value, key, out)
    return out


def channel_id(payload: dict) -> str:
    """The channel id a resolve_url answer points at, or an empty string."""
    for endpoint in find_all(payload, "browseEndpoint", []):
        if isinstance(endpoint, dict):
            candidate = clean(endpoint.get("browseId"))
            if candidate.startswith("UC"):
                return candidate
    return ""


def channel_info(payload: dict) -> tuple[str, str]:
    """The channel title and the params of its Shorts tab, from the channel page."""
    title = ""
    for meta in find_all(payload, "channelMetadataRenderer", []):
        if isinstance(meta, dict) and clean(meta.get("title")):
            title = clean(meta.get("title"))
            break
    params = ""
    for tab in find_all(payload, "tabRenderer", []):
        if not isinstance(tab, dict):
            continue
        name = clean((tab.get("title") or "") if isinstance(tab.get("title"), str) else "")
        if name.lower() != "shorts":
            continue
        endpoint = tab.get("endpoint") or {}
        browse = endpoint.get("browseEndpoint") or {}
        params = clean(browse.get("params"))
        if params:
            break
    return title, params


def views_from_text(text: str) -> int | None:
    """'79M views' -> 79000000, '1,234 views' -> 1234, 'No views' -> 0.

    None means the page did not say, which is not the same as zero.
    """
    raw = clean(text)
    if not raw:
        return None
    if raw.lower().startswith("no views"):
        return 0
    match = _VIEWS.search(raw)
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


def _title_and_views(node: dict) -> tuple[str, str]:
    overlay = node.get("overlayMetadata")
    if isinstance(overlay, dict):
        title = clean((overlay.get("primaryText") or {}).get("content"))
        views = clean((overlay.get("secondaryText") or {}).get("content"))
        if title:
            return title, views
    text = clean(node.get("accessibilityText"))
    if text:
        head = text.split(" - ")[0]
        parts = [p.strip() for p in head.rsplit(",", 1)]
        if len(parts) == 2:
            return parts[0], parts[1]
        return head, ""
    return "", ""


def _thumbnail(node: dict) -> str:
    for holder in (node.get("thumbnailViewModel"),):
        if not isinstance(holder, dict):
            continue
        images = ((holder.get("image") or {}).get("sources")
                  or (holder.get("image") or {}).get("thumbnails") or [])
        if images:
            return clean(images[-1].get("url"))
    for thumbs in find_all(node, "thumbnails", []):
        if isinstance(thumbs, list) and thumbs and isinstance(thumbs[-1], dict):
            url = clean(thumbs[-1].get("url"))
            if url:
                return url
    return ""


def parse_short(node: object, channel: str, channel_title: str, channel_id_value: str,
                position: int) -> dict | None:
    """One Shorts shelf entry as a row, or None when it carries no video id."""
    if not isinstance(node, dict):
        return None
    entity = clean(node.get("entityId"))
    video_id = ""
    on_tap = node.get("onTap") or {}
    command = on_tap.get("innertubeCommand") or {}
    reel = command.get("reelWatchEndpoint") or {}
    video_id = clean(reel.get("videoId"))
    if not video_id and entity.startswith("shorts-shelf-item-"):
        video_id = entity[len("shorts-shelf-item-"):]
    if not video_id:
        return None
    title, views_text = _title_and_views(node)
    return {
        "channelInput": channel,
        "channelId": channel_id_value,
        "channelTitle": channel_title,
        "videoId": video_id,
        "url": "https://www.youtube.com/shorts/" + video_id,
        "title": title or None,
        "viewsText": views_text or None,
        "views": views_from_text(views_text),
        "position": position,
        "thumbnailUrl": _thumbnail(node) or None,
    }


def channel_url(value: object) -> str:
    """Turn a handle, a channel id or a url into a page the resolve endpoint takes.

    A bare word is read as a handle: that is what a buyer copies from YouTube.
    """
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


def parse_page(payload: dict) -> tuple[list[dict], str | None]:
    """Shorts entries in page order plus the continuation token, if any."""
    entries = find_all(payload, "shortsLockupViewModel", [])
    tokens = find_all(payload, "continuationCommand", [])
    token = None
    for item in tokens:
        if isinstance(item, dict) and clean(item.get("token")):
            token = clean(item.get("token"))
            break
    return entries, token
