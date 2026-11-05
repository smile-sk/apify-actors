"""Apify Actor: page screenshot plus a comparison with the previous shot of the same URL.

The baseline lives in a NAMED key-value store (STORE_NAME), not in the run store:
the run store dies with the run, so "compare with the previous run" would never
work - every run would see emptiness. A named store lives in the account of the
user who runs the Actor, so its storage is billed to them and baselines of
different customers never mix.

Pricing: the screenshot-page event (one event = one screenshot and its comparison).
The synthetic apify-actor-start (first 5 seconds free) and apify-default-dataset-item
events are emitted by the platform itself - we never touch them manually.
"""
import asyncio
import hashlib
from datetime import datetime, timezone

from apify import Actor

from src.shot import capture, diff_ratio

STORE_NAME = "screenshot-baselines"
CHARGE_EVENT = "screenshot-page"


def baseline_key(url: str, full: bool, width: int, height: int, fmt: str) -> str:
    """Baseline key: a different viewport or format means a different baseline."""
    raw = f"{url}|{int(full)}|{int(width)}|{int(height)}|{fmt}".encode("utf-8")
    return "shot_" + hashlib.sha1(raw).hexdigest()[:16]


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        url = (inp.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        full = bool(inp.get("fullPage", True))
        width = int(inp.get("width", 1440))
        height = int(inp.get("height", 900))
        fmt = inp.get("format", "png")
        sens = int(inp.get("diffPixels", 12))
        wait_ms = int(inp.get("waitFor", 1000))
        reset = bool(inp.get("resetBaseline", False))

        Actor.log.info(f"Screenshot {url} ({width}x{height}, full_page={full}, {fmt})")
        shot = await capture(url, full, width, height, fmt, wait_ms)

        store = await Actor.open_key_value_store(name=STORE_NAME)
        key = baseline_key(url, full, width, height, fmt)
        prev = None if reset else await store.get_value(key)

        ratio = None
        diff_bytes = None
        compare_error = None
        if prev is not None:
            if isinstance(prev, str):
                prev = prev.encode("utf-8", "replace")
            ratio, diff_bytes, compare_error = diff_ratio(prev, shot, sens)
            if ratio is None:
                Actor.log.warning("Changed vs baseline: the two images are not comparable")
            else:
                Actor.log.info(f"Changed vs baseline: {ratio:.4%} of pixels")
            if compare_error:
                Actor.log.warning(f"Images are not comparable: {compare_error}")
        else:
            Actor.log.info("No baseline for this URL - this run sets it")

        await store.set_value(key, shot, content_type=f"image/{fmt}")
        await Actor.set_value(f"screenshot.{fmt}", shot, content_type=f"image/{fmt}")

        result = {
            "url": url,
            "changed": (ratio > 0) if ratio is not None else None,
            "changedRatio": round(ratio, 6) if ratio is not None else None,
            "firstRun": prev is None,
            "compareError": compare_error,
            "sizeBytes": len(shot),
            "width": width,
            "height": height,
            "fullPage": full,
            "screenshotKey": key,
            "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if diff_bytes:
            diff_key = key + "_diff"
            await store.set_value(diff_key, diff_bytes, content_type="image/png")
            await Actor.set_value("diff.png", diff_bytes, content_type="image/png")
            result["diffKey"] = diff_key
        await Actor.push_data(result)

        # Charge for a finished result: the screenshot is stored and available to the user.
        try:
            charge = await Actor.charge(event_name=CHARGE_EVENT)
            Actor.log.info(f"Charged {CHARGE_EVENT}: {getattr(charge, 'charged_count', '?')}")
            if getattr(charge, "event_charge_limit_reached", False):
                Actor.log.warning("User spending limit reached - stopping here")
                return
        except Exception as exc:  # a local run without monetization must not crash
            Actor.log.warning(f"Charging skipped: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
