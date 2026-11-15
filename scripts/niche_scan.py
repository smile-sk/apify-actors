"""Niche scanner for Apify Store.

Purpose: choose niches where a NEW actor can realistically get a slot.
The deciding number is not total demand but DEPTH: how many actors in the
niche earn >= 1000 runs / 30 days. Depth 1 means "one winner takes all",
depth 20+ means "there are twenty paying positions, go stand next to them".

Second signal: the leader's rating. A leader at 3.7 with 49 reviews is a
leader with a known flaw - that is the gap we copy the product into.

Run:  python scripts/niche_scan.py [--queries q1,q2] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.apify.com/v2/store"

DEFAULT_QUERIES = [
    "tiktok scraper", "instagram scraper", "google maps scraper", "website screenshot",
    "email finder", "contact finder", "linkedin scraper", "youtube transcript",
    "pdf text extraction", "seo audit", "price monitor", "google search results",
    "amazon product", "web page to markdown", "ocr image to text", "whois domain check",
    "sitemap crawl", "currency exchange rates", "job board scraper", "real estate listings",
    "company data", "data enrichment", "mcp server", "review scraper", "ai agent tool",
]


def fetch(query: str, limit: int = 50, retries: int = 2) -> dict:
    url = BASE + "?" + urllib.parse.urlencode({"search": query, "limit": limit})
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "apify-niche-scan/1.0"})
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.load(resp)
        except Exception as exc:  # network hiccup - one more try, then report
            last = exc
            time.sleep(1.5)
    raise RuntimeError(f"{query}: {last}")


def runs30(actor: dict) -> int:
    stats = actor.get("stats") or {}
    return int((stats.get("publicActorRunStats30Days") or {}).get("TOTAL") or 0)


def newcomers30(actor: dict) -> int:
    return int((actor.get("stats") or {}).get("totalUsers30Days") or 0)


def unit_price(actor: dict) -> str:
    """Human price of one charged event, e.g. '0.05/page' - or the model name."""
    info = actor.get("currentPricingInfo") or {}
    events = (info.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}
    parts = []
    for key, ev in events.items():
        price = ev.get("eventPriceUsd")
        if isinstance(price, (int, float)):
            parts.append(f"{price:g}/{ev.get('eventTitle') or key}")
    if parts:
        return ", ".join(parts[:2])
    return str(info.get("pricingModel") or "?")


def scan(query: str, limit: int = 50) -> dict:
    data = (fetch(query, limit).get("data") or {})
    items = data.get("items") or []
    actors = []
    for it in items:
        actors.append({
            "runs30": runs30(it),
            "new_users30": newcomers30(it),
            "rating": it.get("actorReviewRating") or 0.0,
            "reviews": it.get("actorReviewCount") or 0,
            "users_total": (it.get("stats") or {}).get("totalUsers") or 0,
            "username": it.get("username"),
            "name": it.get("name"),
            "title": it.get("title"),
            "price": unit_price(it),
            "url": it.get("url"),
        })
    actors.sort(key=lambda a: -a["runs30"])
    return {
        "query": query,
        "actors_total": data.get("total") or len(items),
        "runs_top": sum(a["runs30"] for a in actors),
        "depth_1k": sum(1 for a in actors if a["runs30"] >= 1000),
        "depth_5k": sum(1 for a in actors if a["runs30"] >= 5000),
        "leader_rating": actors[0]["rating"] if actors else 0.0,
        "leader_share": (actors[0]["runs30"] / max(1, sum(a["runs30"] for a in actors))) if actors else 0.0,
        "top": actors[:6],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default="", help="comma separated; default = built-in list")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--json", default="", help="write full result here")
    args = ap.parse_args()

    queries = [q.strip() for q in args.queries.split(",") if q.strip()] or DEFAULT_QUERIES
    rows = []
    for q in queries:
        try:
            rows.append(scan(q, args.limit))
        except Exception as exc:
            print(f"{q}: ERROR {exc}", file=sys.stderr)

    # Rank by depth first (how many paid positions exist), then by volume.
    rows.sort(key=lambda r: (-r["depth_1k"], -r["runs_top"]))
    print(f"{'niche':24s} {'actors':>6s} {'runs30d':>10s} {'>=1k':>5s} {'>=5k':>5s} "
          f"{'leader%':>7s} {'lead★':>6s}")
    for r in rows:
        print(f"{r['query']:24s} {r['actors_total']:6d} {r['runs_top']:10d} {r['depth_1k']:5d} "
              f"{r['depth_5k']:5d} {r['leader_share']*100:6.0f}% {r['leader_rating']:6.2f}")
        for a in r["top"][:3]:
            print(f"    {a['runs30']:9d}r  +{a['new_users30']:5d}u  "
                  f"{a['rating']:.2f}({a['reviews']})  {a['username']}/{a['name']}  {a['price']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        print(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
