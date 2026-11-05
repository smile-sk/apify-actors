"""Preflight checklist for an Apify actor before publishing.

Usage:
    python scripts/preflight.py                 # all actors in actors/
    python scripts/preflight.py vat-validator   # one actor
    python scripts/preflight.py --json          # machine output

Two layers:
  LOCAL    - what is in the folder and what would travel to the platform
  PLATFORM - what the card in the Store actually shows

Exit code 0 = no FAIL, 1 = at least one FAIL.
Rules and their history live in docs/PUBLISH-CHECKLIST.md.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTORS_DIR = os.path.join(ROOT, "actors")
SECRETS = r"C:\Documents\Projects\ai_ui\data\secrets.json"
API = "https://api.apify.com/v2"

TEXT_EXT = {".py", ".md", ".json", ".txt", ".yaml", ".yml", ".toml",
            ".cfg", ".ini", ".dockerfile", ".sh", ".js", ".ts"}
CYRILLIC = re.compile(r"[\u0400-\u04FF]")
EM_DASH = re.compile(r"[\u2014\u2013]")
JUNK_DIRS = {"__pycache__", ".venv", "node_modules", ".git", ".pytest_cache"}

# local folder name -> actor name on the platform, when they differ
PLATFORM_NAMES = {
    "website-screenshot": "website-screenshot-visual-diff",
    "website-contacts": "website-contacts-finder",
    "workday-jobs": "workday-jobs-api",
    "yt-transcript": "youtube-transcript-api-fast",
}


def token() -> str:
    return json.load(open(SECRETS, encoding="utf-8"))["apify_token"]


def api_get(path: str, tok: str) -> dict:
    url = f"{API}{path}"
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(url + sep + urllib.parse.urlencode({"token": tok}))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def walk(folder: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in JUNK_DIRS]
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), folder).replace("\\", "/"))
    return sorted(out)


def local_checks(folder: str) -> list[dict]:
    checks = []

    def add(code, ok, text, detail=""):
        checks.append({"code": code, "status": "PASS" if ok else "FAIL",
                       "text": text, "detail": detail})

    files = walk(folder)

    junk = [f for f in files if f.endswith((".bak", ".pyc", ".pkl", ".log", ".orig", ".rej"))
            or ".bak_" in f]
    add("L1", not junk, "no .bak/.pyc/.log/.orig in the folder", ", ".join(junk[:6]))

    structure = [p for p in (".actor/actor.json", ".actor/input_schema.json",
                             "Dockerfile", "README.md", "requirements.txt", "src/main.py")
                 if p not in files]
    add("L2", not structure, "required files present", "missing: " + ", ".join(structure))

    cyr, dashes = [], []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in TEXT_EXT and os.path.basename(f) != "Dockerfile":
            continue
        try:
            text = open(os.path.join(folder, f), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if CYRILLIC.search(text):
            cyr.append(f)
        if EM_DASH.search(text):
            dashes.append(f)
    add("L3", not cyr, "no Cyrillic anywhere", ", ".join(cyr[:6]))
    add("L4", not dashes, "no em/en dash (U+2014/U+2013)", ", ".join(dashes[:6]))

    tests = [f for f in files if f.startswith("tests/") and f.endswith(".py")]
    checks.append({"code": "L5", "status": "PASS" if tests else "WARN",
                   "text": "tests present", "detail": f"{len(tests)} file(s)"})

    reqs = []
    rp = os.path.join(folder, "requirements.txt")
    if os.path.exists(rp):
        for line in open(rp, encoding="utf-8", errors="replace"):
            line = line.split("#")[0].strip()
            if line and "==" not in line:
                reqs.append(line)
    checks.append({"code": "L6", "status": "PASS" if not reqs else "WARN",
                   "text": "requirements pinned with ==", "detail": ", ".join(reqs[:6])})

    ignore = [f for f in (".actorignore", ".apifyignore") if f in files]
    checks.append({"code": "L7", "status": "PASS" if ignore else "WARN",
                   "text": "ignore file so tests never ship", "detail": ", ".join(ignore)})

    try:
        actor_json = json.load(open(os.path.join(folder, ".actor", "actor.json"),
                                    encoding="utf-8"))
        cats = actor_json.get("categories") or []
        checks.append({"code": "L8", "status": "PASS" if cats else "WARN",
                       "text": "categories in actor.json", "detail": ", ".join(cats)})
        mem = actor_json.get("defaultMemoryMbytes")
        checks.append({"code": "L9", "status": "PASS" if mem and mem <= 1024 else "WARN",
                       "text": "defaultMemoryMbytes sane (<=1024)", "detail": str(mem)})
    except Exception as exc:  # noqa: BLE001
        add("L8", False, "actor.json readable", str(exc))

    return checks


def platform_checks(actor: dict) -> list[dict]:
    checks = []

    def add(code, ok, text, detail="", level="FAIL"):
        checks.append({"code": code, "status": "PASS" if ok else level,
                       "text": text, "detail": detail})

    seo_t = actor.get("seoTitle") or ""
    add("P1", seo_t and len(seo_t) <= 60, "seoTitle set, <=60 chars",
        f"{len(seo_t)} chars: {seo_t[:70]}")

    seo_d = actor.get("seoDescription") or ""
    add("P2", seo_d and len(seo_d) <= 200, "seoDescription set, <=200 chars",
        f"{len(seo_d)} chars")

    cats = actor.get("categories") or []
    add("P3", bool(cats), "categories visible on the Store card", ", ".join(cats))

    example = actor.get("exampleRunInput")
    body = (example or {}).get("body") if isinstance(example, dict) else None
    is_hello = isinstance(body, str) and "helloWorld" in body
    add("P4", bool(body) and not is_hello, "exampleRunInput is a real input",
        "platform default helloWorld" if is_hello else ("empty" if not body else str(body)[:90]))

    mem = (actor.get("defaultRunOptions") or {}).get("memoryMbytes")
    add("P5", bool(mem) and mem <= 1024, "run memory <=1024 MB", str(mem))

    pricing = actor.get("pricingInfos") or []
    ppe = [p for p in pricing if p.get("pricingModel") == "PAY_PER_EVENT"]
    events = []
    for p in ppe:
        for e in ((p.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}):
            events.append(e)
    add("P6", bool(ppe), "pay-per-event enabled", ", ".join(events[:8]))

    started = [p.get("startedAt") for p in ppe if p.get("startedAt")]
    checks.append({"code": "P7", "status": "PASS" if started else "WARN",
                   "text": "price already in effect", "detail": str(started[:1])})

    public = actor.get("isPublic")
    checks.append({"code": "P8", "status": "PASS" if public else "INFO",
                   "text": "published (isPublic)", "detail": str(public)})

    pic = actor.get("pictureUrl")
    checks.append({"code": "P9", "status": "PASS" if pic else "WARN",
                   "text": "icon set", "detail": "none" if not pic else "ok"})

    return checks


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    argv = [a for a in argv if not a.startswith("--")]
    only = argv[0] if argv else None

    tok = token()
    listing = api_get("/acts?my=true&limit=1000", tok)["data"]["items"]
    by_short = {it["name"].split("/")[-1]: it for it in listing}

    folders = sorted(d for d in os.listdir(ACTORS_DIR)
                     if os.path.isdir(os.path.join(ACTORS_DIR, d)))
    if only:
        folders = [only]

    results, failures = {}, 0
    for folder in folders:
        path = os.path.join(ACTORS_DIR, folder)
        checks = local_checks(path)
        platform_name = PLATFORM_NAMES.get(folder, folder)
        item = by_short.get(platform_name)
        if item:
            full = api_get(f"/acts/{item['id']}", tok)["data"]
            checks += platform_checks(full)
        else:
            checks.append({"code": "P0", "status": "INFO", "text": "not on the platform yet",
                           "detail": "local only"})
        bad = [c for c in checks if c["status"] == "FAIL"]
        failures += len(bad)
        results[folder] = checks
        if not as_json:
            print(f"\n=== {folder}: {'OK' if not bad else str(len(bad)) + ' FAIL'} ===")
            for c in checks:
                mark = {"PASS": "ok  ", "FAIL": "FAIL", "WARN": "warn", "INFO": "info"}[c["status"]]
                detail = f"  [{c['detail']}]" if c.get("detail") else ""
                print(f"  {mark} {c['code']} {c['text']}{detail}")

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"\nTotal FAIL: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
