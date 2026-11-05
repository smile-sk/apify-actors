"""Apify Actor: YouTube transcripts for many videos in one run.

Rules taken from what the leaders in this niche get wrong:

1. Never fail silently. Every video gets a row; a video without a transcript
   carries `error` + `reason` instead of quietly missing from the dataset.
2. Charge only for delivered work: `video-processed` fires after the transcript
   was pushed.
3. Say which language was used and which ones exist.
4. Many videos at once - that is the point of a bulk endpoint.

Cache: a captioned video that one run already fetched is kept in a NAMED
key-value store for `cacheMinutes` (12 hours by default). Two effects matter:
a repeat request is instant, and YouTube sees no second request for the same
video - which is exactly the traffic that gets an address rate-limited. The
store belongs to the account that ran the Actor, so it is that customer's own
storage and never mixes with another customer's.

Network paths, in order: the user's own proxy (if given in the input), our
gateway (YT_GATEWAY env var, exits YouTube's data-centre block), then a direct
connection. A video stops at the first path that returns captions.
"""
import asyncio
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from apify import Actor

from src import yt
from src.yt import fetch, to_srt, to_vtt, video_id

CHARGE_EVENT = "video-processed"
WORKERS = 8
CACHE_STORE = "transcript-cache"
CACHE_KEY_PREFIX = "tr_"
DEFAULT_CACHE_MINUTES = 720


def targets_from_input(inp):
    """(video_id or None, original string) for every distinct request."""
    raw = inp.get("videoUrls") or []
    if isinstance(raw, str):
        raw = [part for part in raw.replace("\n", ",").split(",") if part.strip()]
    seen = set()
    targets = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("url") or item.get("videoId") or ""
        item = str(item).strip()
        if not item:
            continue
        video = video_id(item)
        if video:
            if video in seen:
                continue
            seen.add(video)
        targets.append((video, item))
    return targets


def cache_key(video: str, lang: str | None, prefer_manual: bool) -> str:
    """A different language or caption preference is a different answer."""
    raw = f"{video}|{lang or ''}|{'m' if prefer_manual else 'a'}".encode("utf-8")
    return CACHE_KEY_PREFIX + hashlib.sha1(raw).hexdigest()[:16]


async def probe(inp):
    """Diagnostic run: per video, what each client says. Costs a cent."""
    targets = targets_from_input(inp)[:5]
    for video, item in targets:
        if not video:
            await Actor.push_data({"url": item, "error": "BAD_URL"})
            continue
        row = {"videoId": video, "url": item}
        for client in yt.CLIENTS:
            name = client[0]
            try:
                player = yt._player(video, client)
                status = player.get("playabilityStatus") or {}
                tracks = (((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
                          .get("captionTracks") or [])
                entry = {"status": status.get("status"), "tracks": len(tracks)}
                if tracks:
                    entry["captionBytes"] = len(yt._download(tracks[0]["baseUrl"] + "&fmt=json3", client[4]))
                row[name] = entry
            except Exception as exc:
                row[name] = {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
        await Actor.push_data(row)


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}

        if inp.get("probe"):
            await probe(inp)
            Actor.log.info("Probe finished")
            return

        targets = targets_from_input(inp)
        if not targets:
            raise ValueError("videoUrls is empty - pass at least one YouTube link")

        targets = targets[:int(inp.get("maxVideos") or 100)]
        lang = (str(inp.get("language") or "").strip() or None)
        prefer_manual = bool(inp.get("preferManualCaptions", True))
        include_segments = bool(inp.get("includeSegments", True))
        cache_minutes = int(inp.get("cacheMinutes", DEFAULT_CACHE_MINUTES) or 0)
        cache_seconds = max(0, cache_minutes) * 60

        user_proxy = None
        if inp.get("proxyConfiguration"):
            configuration = await Actor.create_proxy_configuration(
                actor_proxy_input=inp["proxyConfiguration"])
            user_proxy = await configuration.new_url() if configuration else None

        # Our own egress gateway: YouTube refuses Google Cloud addresses once
        # they are dirty, and the plain VPS answers 20 of 20 videos in 0.2 s.
        # The env var wins, so the gateway can be rotated without a rebuild...
        # and when the env var does not arrive, this constant keeps the Actor alive.
        gateway = ((os.environ.get("YT_GATEWAY") or "").strip()
                   or "http://user:pass@example-vps:8899")
        paths = [p for p in [user_proxy, gateway, None] if p or p is None]
        ordered_paths = []
        for path in paths:
            if path not in ordered_paths:
                ordered_paths.append(path)

        cache = None
        if cache_seconds:
            try:
                cache = await Actor.open_key_value_store(name=CACHE_STORE)
            except Exception as exc:  # a local run without the platform must not crash
                Actor.log.warning(f"Cache store unavailable, running without cache: {exc}")

        loop = asyncio.get_running_loop()
        valid = [(video, item) for video, item in targets if video]

        cached_by_item, to_fetch = {}, []
        now = time.time()
        for video, item in valid:
            entry = None
            if cache is not None:
                try:
                    entry = await cache.get_value(cache_key(video, lang, prefer_manual))
                except Exception as exc:
                    Actor.log.warning(f"Cache read failed for {video}: {exc}")
            if (isinstance(entry, dict) and isinstance(entry.get("result"), dict)
                    and entry["result"].get("ok")
                    and now - float(entry.get("fetchedAt") or 0) <= cache_seconds):
                cached_by_item[item] = entry["result"]
            else:
                to_fetch.append((video, item))

        Actor.log.info(f"Fetching transcripts for {len(targets)} videos "
                       f"(language={lang or 'auto'}, manual_first={prefer_manual}, "
                       f"paths={len(ordered_paths)}, cache={cache_minutes}m: "
                       f"{len(cached_by_item)} fresh, {len(to_fetch)} to fetch, "
                       f"gateway_env={bool(os.environ.get('YT_GATEWAY'))})")

        delivered = failed = 0
        stopped = False

        async def deliver(video, item, result, from_cache):
            """Push one row the moment its answer exists.

            Rows leave in the order they finish, not in the order they were
            asked for: a run that times out late still hands over everything
            it already fetched, instead of an empty dataset.
            """
            nonlocal delivered, failed, stopped
            if stopped:
                return
            if not video:
                await Actor.push_data({"url": item, "videoId": None, "status": "FAILED",
                                       "error": "BAD_URL",
                                       "reason": "not a YouTube link or 11-character video id"})
                failed += 1
                return
            if not result or not result.get("ok"):
                result = result or {}
                await Actor.push_data({
                    "url": item,
                    "videoId": result.get("videoId", video),
                    "videoTitle": result.get("title"),
                    "availableLanguages": result.get("availableLanguages"),
                    "status": "FAILED",
                    "error": result.get("error") or "UNKNOWN",
                    "reason": result.get("reason") or "no answer from any client",
                })
                failed += 1
                Actor.log.warning(f"{video}: {result.get('error')} - {result.get('reason')}")
                return

            row = {
                "url": result["url"],
                "videoId": result["videoId"],
                "videoTitle": result.get("title"),
                "channel": result.get("channel"),
                "channelId": result.get("channelId"),
                "durationSeconds": result.get("durationSeconds"),
                "viewCount": result.get("viewCount"),
                "language": result.get("language"),
                "languageFallback": bool(result.get("languageFallback")),
                "isAutoGenerated": result.get("isAutoGenerated"),
                "availableLanguages": result.get("availableLanguages"),
                "segmentCount": len(result["segments"]),
                "transcript": result["transcript"],
                "srt": to_srt(result["segments"]),
                "vtt": to_vtt(result["segments"]),
                "status": "SUCCESS",
                "fromCache": from_cache,
                "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if include_segments:
                row["segments"] = result["segments"]
            await Actor.push_data(row)
            delivered += 1

            try:
                charge = await Actor.charge(event_name=CHARGE_EVENT)
                if getattr(charge, "event_charge_limit_reached", False):
                    Actor.log.warning("User spending limit reached - stopping")
                    stopped = True
            except Exception as exc:  # local run without monetization must not crash
                Actor.log.warning(f"Charging skipped: {exc}")

        # 1) cached answers and unusable links go out at once
        for video, item in targets:
            if not video:
                await deliver(video, item, None, False)
            elif item in cached_by_item:
                await deliver(video, item, cached_by_item[item], True)

        # 2) the rest - one by one, as each fetch lands
        if to_fetch and not stopped:
            pool = ThreadPoolExecutor(max_workers=min(WORKERS, max(1, len(to_fetch))))
            try:
                running = {}
                for video, item in to_fetch:
                    future = loop.run_in_executor(pool, fetch, item, lang, prefer_manual, ordered_paths)
                    running[future] = (video, item)
                pending = set(running)
                while pending and not stopped:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for future in done:
                        video, item = running[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = {"ok": False, "error": type(exc).__name__,
                                      "reason": str(exc)[:200]}
                        await deliver(video, item, result, False)
                        if cache is not None and isinstance(result, dict) and result.get("ok"):
                            try:
                                await cache.set_value(cache_key(video, lang, prefer_manual),
                                                      {"fetchedAt": now, "result": result})
                            except Exception as exc:
                                Actor.log.warning(f"Cache write failed for {video}: {exc}")
            finally:
                pool.shutdown(wait=False)

        Actor.log.info(f"Done: {delivered} transcripts delivered, {failed} failed "
                       f"(cache hits: {len(cached_by_item)})")


if __name__ == "__main__":
    asyncio.run(main())
