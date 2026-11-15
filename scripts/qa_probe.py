"""QA probe: will the storefront accept this actor tomorrow morning?

Apify runs every public actor automatically with the input its schema prefills
and expects the run to succeed within 5 minutes. A failed run puts the
"Under maintenance" badge on the card and hides the actor from buyers.

This script repeats that scenario locally, twice per actor:

  empty      no input at all (a bare Run click with nothing filled in)
  prefilled  input built from input_schema.json (prefill values + defaults)

Both must end with exit code 0 inside the 5 minute budget. Anything else is
a FAIL, because either of the two is what the platform may send.

Usage:
    python scripts/qa_probe.py <slug> [<slug> ...]
    python scripts/qa_probe.py --all

Exit code 0 = every probed actor passed both cases. The JSON report lands in
docs/qa/qa_probe_<timestamp>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time

PARK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTORS = os.path.join(PARK, "actors")
REPORT_DIR = os.path.join(PARK, "docs", "qa")

LIMIT_S = 300.0        # the platform's patience
HARD_LIMIT_S = 360.0   # local kill switch, a bit wider so we can measure the overrun
STATUS_RE = re.compile(r"Terminal status message\]?: ([^\r\n]+)")


def actor_slugs() -> list[str]:
    out = []
    for name in sorted(os.listdir(ACTORS)):
        if os.path.isdir(os.path.join(ACTORS, name, ".actor")):
            out.append(name)
    return out


def build_prefilled(slug: str) -> dict:
    """The input a console Run click sends when the schema prefills itself."""
    path = os.path.join(ACTORS, slug, ".actor", "input_schema.json")
    with open(path, encoding="utf-8") as fh:
        schema = json.load(fh)
    props = schema.get("properties") or {}
    value = {}
    for name, spec in props.items():
        if not isinstance(spec, dict):
            continue
        if "prefill" in spec:
            value[name] = spec["prefill"]
        elif "default" in spec:
            value[name] = spec["default"]
    return value


def run_once(slug: str, value: dict) -> dict:
    actor_dir = os.path.join(ACTORS, slug)
    kv = os.path.join(actor_dir, "storage", "key_value_stores", "default")
    os.makedirs(kv, exist_ok=True)
    with open(os.path.join(kv, "INPUT.json"), "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False)

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    started = time.monotonic()
    code = None
    blob = ""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "src.main"],
            cwd=actor_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=HARD_LIMIT_S, env=env,
        )
        code = proc.returncode
        blob = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        code = "TIMEOUT"
        for part in (exc.stdout, exc.stderr):
            if part:
                blob += part if isinstance(part, str) else part.decode("utf-8", "replace")

    seconds = round(time.monotonic() - started, 1)
    found = STATUS_RE.findall(blob)
    return {
        "exit": code,
        "seconds": seconds,
        "ok": code == 0 and seconds <= LIMIT_S,
        "message": found[-1].strip()[:160] if found else "",
    }


def probe(slug: str) -> dict:
    cases = {}
    for name, value in (("empty", {}), ("prefilled", build_prefilled(slug))):
        result = run_once(slug, value)
        result["case"] = name
        cases[name] = result
    return {"actor": slug, "ok": all(c["ok"] for c in cases.values()), "cases": cases}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    slugs = actor_slugs() if "--all" in argv else [a for a in argv if not a.startswith("-")]
    missing = [s for s in slugs if not os.path.isdir(os.path.join(ACTORS, s))]
    if missing:
        print("no such actor: " + ", ".join(missing))
        return 2

    results = []
    print(f"{'actor':40} {'empty':>18}  {'prefilled':>18}  verdict")
    for slug in slugs:
        res = probe(slug)
        results.append(res)
        cells = []
        for case in ("empty", "prefilled"):
            c = res["cases"][case]
            cells.append(f"{c['exit']}/{c['seconds']}s".rjust(18))
        print(f"{slug[:40]:40} {cells[0]}  {cells[1]}  {'PASS' if res['ok'] else 'FAIL'}")

    failed = [r for r in results if not r["ok"]]
    for res in failed:
        for case, c in res["cases"].items():
            if not c["ok"]:
                print(f"\nFAIL {res['actor']} [{case}] exit={c['exit']} {c['seconds']}s")
                print(f"  {c['message'] or 'no terminal status message'}")

    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report = os.path.join(REPORT_DIR, f"qa_probe_{stamp}.json")
    with open(report, "w", encoding="utf-8") as fh:
        json.dump({"at": stamp, "limit_secs": LIMIT_S, "results": results}, fh,
                  ensure_ascii=False, indent=1)

    print(f"\nprobed {len(results)}, failed {len(failed)}  (limit {LIMIT_S:.0f}s, report {os.path.relpath(report, PARK)})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
