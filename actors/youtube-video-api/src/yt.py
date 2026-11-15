"""One video, read from the public YouTube innertube endpoint.

No API key, no browser, no proxy: the player endpoint answers a plain POST with the
metadata a buyer wants. Its playability status is about playback (DRM, age gate), not
about metadata, so a video that cannot be played here still carries a full row.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

# The web key of youtube.com is public: it ships in the page of every visitor and is
# tied to no account, so it is not a credential. It is not stored in this repository:
# set YT_INNERTUBE_KEY to send it, otherwise the endpoint is called without a key.
_WEB_KEY = os.environ.get("YT_INNERTUBE_KEY", "").strip()
ENDPOINT = "https://www.youtube.com/youtubei/v1/player"
if _WEB_KEY:
    ENDPOINT += "?key=" + _WEB_KEY
CLIENT = {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00",
                     "hl": "en", "gl": "US"}}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_ID = re.compile(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
_SHORT_ID = re.compile(r"^/([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def video_id(raw: str) -> tuple[str | None, str]:
    """Work out the 11 character id from a bare id or any YouTube link."""
    text = (raw or "").strip()
    if not text:
        return None, "the input is empty"
    if _ID.match(text):
        return text, ""
    parsed = urlparse(text if "//" in text else "https://" + text)
    host = (parsed.netloc or "").lower().rsplit("@", 1)[-1].split(":")[0]
    if not any(host == base or host.endswith("." + base) for base in _HOSTS):
        return None, "not a YouTube video id or link"
    found = _PATH_ID.match(parsed.path or "")
    if found:
        return found.group(1), ""
    if host.endswith("youtu.be"):
        short = _SHORT_ID.match(parsed.path or "")
        if short:
            return short.group(1), ""
    values = parse_qs(parsed.query).get("v") or []
    if values and _ID.match(values[0]):
        return values[0], ""
    return None, "the link carries no video id"


def _retry_after(response) -> float:
    """How long the relay asked us to wait, in seconds."""
    raw = ""
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw = str(headers.get("Retry-After") or "").strip()
        except Exception:  # noqa: BLE001
            raw = ""
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 1.0


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _from_response(response) -> tuple[dict | None, str]:
    """Turn a player answer into (payload, reason). Shared by both transports."""
    if response.status_code != 200:
        return None, f"YouTube answered HTTP {response.status_code}"
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return None, "YouTube did not answer JSON"
    if not isinstance(payload, dict):
        return None, "YouTube answered an unexpected shape"
    details = payload.get("videoDetails") or {}
    if not details.get("videoId"):
        block = payload.get("playabilityStatus") or {}
        status = block.get("reason") or "no details"
        # A bot check is not a verdict about the video: the platform network gets one
        # while the same id is answered elsewhere, so calling it "no such video" would
        # sell the buyer a wrong diagnosis.
        if "not a bot" in status.lower() or "confirm you" in status.lower():
            return None, f"YouTube answered a bot check instead of the video ({status})"
        return None, f"YouTube has no video with this id ({status})"
    return payload, ""


@dataclass
class Relay:
    """Our own relay on the VPS.

    The Apify network is refused by this endpoint with a bot check while the same
    request from our VPS is answered, so the actor asks the relay first and only
    falls back to a direct call when the relay itself is the thing that failed.
    """

    url: str
    token: str

    def endpoint(self) -> str:
        return self.url.rstrip("/") + "/player"

    def fetch(self, client, vid: str, timeout: float | None = None,
              retried: bool = False) -> tuple[dict | None, str, str]:
        """Returns (payload, reason, kind); kind is "" when the relay answered about the video."""
        if not self.url or not self.token:
            return None, "the relay is not configured", "relay"
        body = json.dumps({"videoId": vid})
        extra = {} if timeout is None else {"timeout": max(2.0, float(timeout))}
        try:
            response = client.post(self.endpoint(), content=body,
                                   headers={"Content-Type": "application/json",
                                            "X-Relay-Token": self.token,
                                            "User-Agent": USER_AGENT}, **extra)
        except Exception as exc:  # noqa: BLE001
            return None, f"the relay did not answer: {type(exc).__name__}", "relay"
        if response.status_code == 401:
            return None, "the relay refused this token", "relay"
        if response.status_code == 429:
            wait = _retry_after(response)
            if not retried:
                # One repeat, and only once: a busy relay must not push the video onto
                # the road YouTube closes, and a queue of repeats must not eat the run.
                time.sleep(min(3.0, max(0.2, wait)))
                return self.fetch(client, vid, timeout=timeout, retried=True)
            return None, f"the relay is busy (HTTP 429, retry-after {wait:.1f} s)", "relay"
        if response.status_code != 200:
            return None, f"the relay answered HTTP {response.status_code}", "relay"
        payload, reason = _from_response(response)
        return payload, reason, ""


def fetch(client, vid: str, timeout: float | None = None) -> tuple[dict | None, str]:
    """Ask the player endpoint for one video. Returns (payload, reason-when-missing)."""
    body = json.dumps({"context": CLIENT, "videoId": vid})
    extra = {} if timeout is None else {"timeout": max(2.0, float(timeout))}
    try:
        response = client.post(ENDPOINT, content=body,
                               headers={"Content-Type": "application/json",
                                        "User-Agent": USER_AGENT}, **extra)
    except Exception as exc:  # noqa: BLE001
        return None, f"YouTube did not answer: {type(exc).__name__}"
    return _from_response(response)


def row(payload: dict, *, raw: str, index: int, with_description: bool = False) -> dict:
    """One delivered video."""
    details = payload.get("videoDetails") or {}
    micro = (payload.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    thumbs = ((details.get("thumbnail") or {}).get("thumbnails")) or []
    best = thumbs[-1].get("url") if thumbs else None
    vid = details.get("videoId")
    result = {
        "videoId": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": details.get("title"),
        "channelTitle": details.get("author"),
        "channelId": details.get("channelId"),
        "views": _int(details.get("viewCount")),
        "lengthSeconds": _int(details.get("lengthSeconds")),
        "publishedAt": micro.get("publishDate") or micro.get("uploadDate"),
        "category": micro.get("category"),
        "keywords": list(details.get("keywords") or []) or None,
        "isLive": details.get("isLiveContent"),
        "thumbnailUrl": best,
        "playability": (payload.get("playabilityStatus") or {}).get("status"),
        "playabilityReason": (payload.get("playabilityStatus") or {}).get("reason"),
    }
    if with_description:
        result["description"] = details.get("shortDescription")
    result.update({"actorInput": raw, "actorIndex": index, "status": "OK", "reason": ""})
    return result
