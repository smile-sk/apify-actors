"""YouTube caption fetcher: no API key, no browser.

Two hard facts measured on 2026-11-04, and the code follows them:

* The normal WEB player endpoint answers UNPLAYABLE to server-side requests, and
  the caption URLs it hands out come back with an empty 200 body.
* The iOS client answers OK and returns real caption JSON - from a home IP and
  from a plain VPS. From Google Cloud (where this Actor runs) the same request is
  answered with "Sign in to confirm you're not a bot" once that IP is dirty, so
  the caller can pass a proxy and every client is tried in turn instead of
  betting the run on one.

Each attempt is cheap: one player request, one caption download.
"""
import json
import re
import urllib.error
import urllib.request

_UA_IOS = "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)"
_UA_ANDROID = "com.google.android.youtube/20.10.38 (Linux; U; Android 14) gzip"
_UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_UA_MWEB = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1")

# name, client context, X-YouTube-Client-Name, X-YouTube-Client-Version, user agent
CLIENTS = [
    ("IOS", {"clientName": "IOS", "clientVersion": "20.10.4", "deviceModel": "iPhone16,2",
             "osName": "iPhone", "osVersion": "18.3.2", "hl": "en", "gl": "US"},
     "5", "20.10.4", _UA_IOS),
    ("ANDROID", {"clientName": "ANDROID", "clientVersion": "20.10.38", "androidSdkVersion": 34,
                 "hl": "en", "gl": "US"},
     "3", "20.10.38", _UA_ANDROID),
    ("MWEB", {"clientName": "MWEB", "clientVersion": "2.20250312.04.00", "hl": "en", "gl": "US"},
     "2", "2.20250312.04.00", _UA_MWEB),
    ("WEB", {"clientName": "WEB", "clientVersion": "2.20250312.04.00", "hl": "en", "gl": "US"},
     "1", "2.20250312.04.00", _UA_WEB),
    ("TVEMBED", {"clientName": "TVHTML5_SIMPLY_EMBEDDED_PLAYER", "clientVersion": "2.0",
                 "hl": "en", "gl": "US"},
     "85", "2.0", _UA_WEB),
]

_ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/|/live/|/v/)([A-Za-z0-9_-]{11})")


def video_id(url_or_id):
    """11-character video id from any YouTube link shape, or None."""
    text = (url_or_id or "").strip()
    if not text:
        return None
    found = _ID_RE.search(text)
    if found:
        return found.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    return None


def _opener(proxy_url=None):
    if not proxy_url:
        return urllib.request.build_opener()
    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    return urllib.request.build_opener(handler)


def _player(video, client, proxy_url=None, timeout=25):
    name, context, header_name, header_version, user_agent = client
    payload = {"context": {"client": context}, "videoId": video,
               "contentCheckOk": True, "racyCheckOk": True}
    request = urllib.request.Request(
        "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": user_agent,
            "X-YouTube-Client-Name": header_name,
            "X-YouTube-Client-Version": header_version,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with _opener(proxy_url).open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _download(url, user_agent, proxy_url=None, timeout=25):
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"}
    )
    with _opener(proxy_url).open(request, timeout=timeout) as response:
        return response.read()


def _pick_track(tracks, lang, prefer_manual, default_index):
    """Which caption track to return.

    1. the language the caller asked for;
    2. otherwise the track YouTube itself would show (`defaultCaptionTrackIndex`);
    3. otherwise human captions, then auto-generated, preferring English;
    4. otherwise the first track.

    A language this video does not carry never kills the run: the original track
    comes back flagged `_language_fallback`, because a transcript in another
    language is worth more to the caller than an error row.
    """
    fallback = False
    if lang:
        wanted = [t for t in tracks if (t.get("languageCode") or "").lower().startswith(lang.lower())]
        if wanted:
            tracks = wanted
        else:
            fallback = True

    if not lang and isinstance(default_index, int) and 0 <= default_index < len(tracks):
        return tracks[default_index]

    def manual(items):
        return [t for t in items if t.get("kind") != "asr"]

    def auto(items):
        return [t for t in items if t.get("kind") == "asr"]

    pool = tracks
    if not lang:
        english = [t for t in pool if (t.get("languageCode") or "").lower() == "en"]
        if english:
            pool = english
    ordered = (manual(pool) + auto(pool)) if prefer_manual else (auto(pool) + manual(pool))
    if not ordered:
        ordered = manual(tracks) + auto(tracks)
    chosen = ordered[0] if ordered else (tracks[0] if tracks else None)
    if chosen is not None and fallback:
        chosen = dict(chosen)
        chosen["_language_fallback"] = True
    return chosen


def _segments_from_json(raw):
    data = json.loads(raw)
    segments = []
    for event in data.get("events") or []:
        pieces = event.get("segs")
        if not pieces:
            continue
        text = "".join(p.get("utf8", "") for p in pieces).replace("\n", " ").strip()
        if not text:
            continue
        segments.append({
            "start": round(event.get("tStartMs", 0) / 1000.0, 2),
            "duration": round(event.get("dDurationMs", 0) / 1000.0, 2),
            "text": text,
        })
    return segments


def _clock(seconds, comma=False):
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{',' if comma else '.'}{millis:03d}"


def to_srt(segments):
    lines = []
    for index, segment in enumerate(segments, 1):
        start = _clock(segment["start"], comma=True)
        end = _clock(segment["start"] + segment.get("duration", 0), comma=True)
        lines.append(f"{index}\n{start} --> {end}\n{segment['text']}\n")
    return "\n".join(lines)


def to_vtt(segments):
    lines = ["WEBVTT\n"]
    for segment in segments:
        start = _clock(segment["start"])
        end = _clock(segment["start"] + segment.get("duration", 0))
        lines.append(f"{start} --> {end}\n{segment['text']}\n")
    return "\n".join(lines)


def _attempt(video, url_or_id, lang, prefer_manual, proxy_url):
    """One pass over all clients with one network path. Returns (result, attempts)."""
    attempts = []
    for client in CLIENTS:
        name, _context, _hn, _hv, user_agent = client
        try:
            player = _player(video, client, proxy_url)
        except Exception as exc:
            attempts.append(f"{name}: request failed ({type(exc).__name__})")
            continue

        status = player.get("playabilityStatus") or {}
        details = player.get("videoDetails") or {}
        if status.get("status") != "OK":
            attempts.append(f"{name}: {status.get('status')} - {status.get('reason')}")
            continue

        renderer = ((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
        tracks = renderer.get("captionTracks") or []
        if not tracks:
            return ({"ok": False, "videoId": video, "url": url_or_id, "error": "NO_CAPTIONS",
                     "reason": "this video has no captions (disabled by the uploader or none published)",
                     "title": details.get("title")}, attempts)

        audio_tracks = renderer.get("audioTracks") or []
        default_index = audio_tracks[0].get("defaultCaptionTrackIndex") if audio_tracks else None
        track = _pick_track(tracks, lang, prefer_manual, default_index)
        if track is None:
            return ({"ok": False, "videoId": video, "url": url_or_id, "error": "LANGUAGE_NOT_AVAILABLE",
                     "reason": f"no captions in '{lang}'",
                     "availableLanguages": sorted({t.get("languageCode") for t in tracks if t.get("languageCode")}),
                     "title": details.get("title")}, attempts)

        try:
            raw = _download(track["baseUrl"] + "&fmt=json3", user_agent, proxy_url)
        except Exception as exc:
            attempts.append(f"{name}: caption download failed ({type(exc).__name__})")
            continue
        if not raw:
            attempts.append(f"{name}: empty caption body")
            continue

        try:
            segments = _segments_from_json(raw)
        except Exception as exc:
            attempts.append(f"{name}: bad caption format ({type(exc).__name__})")
            continue
        if not segments:
            attempts.append(f"{name}: caption track carries no text")
            continue

        return ({
            "ok": True,
            "videoId": video,
            "url": f"https://www.youtube.com/watch?v={video}",
            "title": details.get("title"),
            "channel": details.get("author"),
            "channelId": details.get("channelId"),
            "durationSeconds": int(details.get("lengthSeconds") or 0),
            "viewCount": details.get("viewCount"),
            "language": track.get("languageCode"),
            "languageFallback": bool(track.get("_language_fallback")),
            "isAutoGenerated": track.get("kind") == "asr",
            "availableLanguages": sorted({t.get("languageCode") for t in tracks if t.get("languageCode")}),
            "client": name,
            "segments": segments,
            "transcript": " ".join(s["text"] for s in segments),
        }, attempts)
    return None, attempts


def fetch(url_or_id, lang=None, prefer_manual=True, proxy_urls=None):
    """Try every network path and every client until captions come back.

    `proxy_urls` is an ordered list: the first that works wins. `None` entries
    mean a direct connection.

    Failures are data, not exceptions: the caller must be able to tell the user
    WHY a video produced nothing.
    """
    video = video_id(url_or_id)
    if not video:
        return {"ok": False, "url": url_or_id, "error": "BAD_URL",
                "reason": "not a YouTube link or 11-character video id"}

    paths = list(proxy_urls or [None])
    if not paths:
        paths = [None]
    all_attempts = []
    for path in paths:
        result, attempts = _attempt(video, url_or_id, lang, prefer_manual, path)
        all_attempts.extend(attempts)
        if result is not None:
            return result

    error = "LOGIN_REQUIRED" if any("LOGIN_REQUIRED" in a for a in all_attempts) else "FETCH_FAILED"
    return {"ok": False, "videoId": video, "url": url_or_id, "error": error,
            "reason": "every client was refused; last answers: " + " | ".join(all_attempts[-3:]),
            "attempts": all_attempts}
