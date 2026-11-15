"""Who wears the "Under maintenance" badge right now, and what changed?

Apify puts notice=UNDER_MAINTENANCE on a public actor when its own automated
run fails. The badge hides the card from buyers, so it is worth checking by
machine instead of waiting for the next email.

The scan pulls every actor's card one by one (the list endpoint returns
notice=None for all of them and isPublic=None, so it lies), keeps a snapshot
in docs/qa/notice_<timestamp>.json and diff against the newest previous one.

Usage:
    python scripts/notice_scan.py [--quiet]

Token: APIFY_TOKEN env var, else the apify_token key of the ai_ui secrets
file next door. Exit code 0 = no actor is flagged.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys
import urllib.request

PARK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(PARK, "docs", "qa")
SECRETS = os.path.join(os.path.dirname(PARK), "ai_ui", "data", "secrets.json")
API = "https://api.apify.com/v2"


def token() -> str:
    value = os.environ.get("APIFY_TOKEN")
    if value:
        return value.strip()
    with open(SECRETS, encoding="utf-8") as fh:
        return json.load(fh)["apify_token"]


def get(path: str, tok: str) -> dict:
    req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def scan() -> list[dict]:
    tok = token()
    listed = get("/acts?limit=1000", tok)["data"]["items"]
    out = []
    for item in listed:
        card = get(f"/acts/{item['id']}", tok)["data"]
        out.append({
            "id": item["id"],
            "name": card["name"],
            "notice": card.get("notice"),
            "public": bool(card.get("isPublic")),
            "runs": (card.get("stats") or {}).get("totalRuns"),
        })
    return sorted(out, key=lambda r: r["name"])


def load_snapshot(path: str) -> dict | None:
    """Only files written by this script count: a stray probe file broke the diff once."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("actors"), list):
        return None
    return {r["name"]: r.get("notice") for r in data["actors"]}


def previous_snapshot(current: str) -> str | None:
    snaps = [p for p in glob.glob(os.path.join(REPORT_DIR, "notice_*.json")) if current not in p]
    snaps = [p for p in snaps if load_snapshot(p) is not None]
    return max(snaps, key=os.path.getmtime) if snaps else None


def confirm_flagged(rows: list[dict], tok: str) -> list[str]:
    """Read a flagged card a second time: the badge field flaps (all 40 flagged for a minute)."""
    flaps = []
    for row in rows:
        try:
            again = get("/acts/" + row["id"], tok)["data"]
        except Exception:
            continue
        if not again.get("notice"):
            flaps.append(row["name"])
    return flaps


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    rows = scan()
    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(REPORT_DIR, f"notice_{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"at": stamp, "actors": rows}, fh, ensure_ascii=False, indent=1)

    tok = token()
    flagged = [r for r in rows if r["notice"]]
    flaps = confirm_flagged(flagged, tok) if flagged else []
    if flaps:
        print(f"card flapped on a second read (not counted as flagged): {flaps}")
        flagged = [r for r in flagged if r["name"] not in flaps]
    if not quiet:
        print(f"{len(rows)} actors, {len(flagged)} flagged  ({stamp})")
        for r in flagged:
            print(f"  UNDER MAINTENANCE  {r['name']}  runs={r['runs']}")

    prev = previous_snapshot(path)
    if prev:
        old = load_snapshot(prev) or {}
        new = {r["name"]: r["notice"] for r in rows}
        cleared = [n for n in new if old.get(n) and not new[n]]
        appeared = [n for n in new if new[n] and not old.get(n)]
        print(f"diff vs {os.path.basename(prev)}: cleared {cleared or '-'}, new {appeared or '-'}")
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
