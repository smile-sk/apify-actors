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
# `storage/` is the local run output: manifest.py drops it (SKIP_DIRS) and it never
# reaches the platform, so a dash inside a scraped dataset was a false FAIL on
# telegram-channel-scraper (Orestes, 13.11). Scanned folders must match what ships.
JUNK_DIRS = {"__pycache__", ".venv", "node_modules", ".git", ".pytest_cache", "storage"}
# A guard or a cast around the same variable is not a delegation to a helper: isinstance(meta, dict)
# and int(meta["item_count"]) both look like CALLS with meta as the first argument, and treating
# them as a helper turned a real defect into a soft WARN on personio and workable.
BUILTIN_CALLS = {"isinstance", "issubclass", "int", "str", "float", "bool", "len", "getattr",
                 "hasattr", "setattr", "print", "type", "list", "dict", "tuple", "set", "sorted",
                 "min", "max", "abs", "round", "sum", "any", "all", "repr"}

# Calls that read like the SDK but do not exist there. They pass review and pass local tests
# (a hand-written double can carry any method) and fail only inside the container.
# Checked against the installed package, not against memory: Actor.get_value and Actor.set_value
# DO exist in 4.x, and .get_value/.set_value on a key-value store are normal calls. A rule that
# cries wolf on working code is churn, not a guard.
SDK_ABSENT = {
    "Actor.get_pricing_info": "Actor has no get_pricing_info; use get_charging_manager().get_pricing_info()",
    "Dataset.get_info": "Dataset has no get_info in apify 4.x; the metadata getter is get_metadata()",
}

# The same defect in instance form: code calls dataset.get_info() on a variable, so a plain
# substring "Dataset.get_info" never matches. A test double carries any method, so this is
# again a defect that only appears inside the container.
SDK_ABSENT_CALLS = (
    (re.compile(r"\.get_info\s*\("), "no get_info on a dataset in apify 4.x; the metadata getter is get_metadata()", "dataset"),
    (re.compile(r"Actor\.get_pricing_info\s*\("), "Actor has no get_pricing_info; use get_charging_manager().get_pricing_info()", ""),
)

# P10 is a PROXY check, not a judge of style: a Store description must open with the
# user's pain, not with our capability. It catches the "Validate phone numbers ..." opening.
FEATURE_START = {"validate", "validates", "check", "checks", "extract", "extracts",
                 "get", "gets", "find", "finds", "convert", "converts", "scan", "scans",
                 "detect", "detects", "turn", "turns", "parse", "parses", "verify",
                 "verifies", "clean", "cleans", "format", "formats", "generate", "generates"}
PAIN_MARKERS = ("no ", "not ", "never", "without", "instead", "fail", "junk", "garbage",
                "false", "wrong", "tired", "silent", "missing", "empty", "broken",
                "charged", "skip", "lose", "pay ", "complaint", "disappear", "vanish",
                "blank", "made-up", "guess", "die ", "kills", "half", "only read",
                "still report", "still looks")

# local folder name -> actor name on the platform, when they differ
PLATFORM_NAMES = {
    "website-screenshot": "website-screenshot-visual-diff",
    "website-contacts": "website-contacts-finder",
    "workday-jobs": "workday-jobs-api",
    "yt-transcript": "youtube-transcript-api-fast",
}


def platform_name(folder: str) -> str:
    """Name of the actor on the platform.

    .actor/actor.json -> name is the source of truth: it is exactly what the Store
    shows, and the table above lags behind every rename. The table is a fallback.
    """
    try:
        name = json.load(open(os.path.join(folder, ".actor", "actor.json"),
                              encoding="utf-8")).get("name")
    except Exception:  # noqa: BLE001
        name = None
    if name:
        return str(name).split("/")[-1]
    base = os.path.basename(os.path.abspath(folder))
    return PLATFORM_NAMES.get(base, base)


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


def read_doc(name: str) -> str:
    """Text of a file from docs/, empty string when it is missing."""
    try:
        return open(os.path.join(ROOT, "docs", name), encoding="utf-8").read()
    except OSError:
        return ""


def undescribed_props(node, path: str = "") -> list[str]:
    """Every property of an input schema that carries no description, with its path.

    The platform answers 400 on the first such property (`Field
    schema.properties.<name>.description is required`) and the tree never becomes a version.
    The walk is RECURSIVE (Orestes' find H2, 14.11) and the recursion premise is MEASURED, not
    assumed (H3): one throwaway actor pushed three schemas and its builds answered, in order,
    "Field schema.properties.nodesc.description is required" (top level),
    "Field schema.properties.group.inner.description is required" (inside type:object) and
    "Field schema.properties.list.items.deep.description is required" (inside items.properties).
    So all three levels are the same build failure. Paths use dots for objects and [] for array
    items, so the detail names the exact spot instead of just the field name.
    """
    out: list[str] = []
    if not isinstance(node, dict):
        return out
    props = node.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            here = f"{path}.{name}" if path else str(name)
            if isinstance(sub, dict):
                if not str(sub.get("description") or "").strip():
                    out.append(here)
                out.extend(undescribed_props(sub, here))
    items = node.get("items")
    if isinstance(items, dict):
        out.extend(undescribed_props(items, f"{path}[]" if path else "[]"))
    return out


def untyped_props(node, path: str = "") -> list[str]:
    """Every property of an input schema that carries no `type`, with its path.

    Measured 14.11 (Orestes' find H4 turned into a probe): a property described only by `anyOf`
    is rejected by the build with "Field schema.properties.<name> is not matching any input
    schema type definition", and a `oneOf` inside a typed object is validated strictly by their
    own schema checker. Measured over the park before writing the rule: 0 of 50 trees carry a
    property without `type`, so the guard starts clean.
    """
    out: list[str] = []
    if not isinstance(node, dict):
        return out
    props = node.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            here = f"{path}.{name}" if path else str(name)
            if isinstance(sub, dict):
                if "type" not in sub:
                    out.append(here)
                out.extend(untyped_props(sub, here))
    items = node.get("items")
    if isinstance(items, dict):
        out.extend(untyped_props(items, f"{path}[]" if path else "[]"))
    return out


def local_checks(folder: str) -> list[dict]:
    checks = []

    def add(code, ok, text, detail="", level="FAIL"):
        checks.append({"code": code, "status": "PASS" if ok else level,
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
            # A fixture keeps the employer's own words, dashes included: rewriting real
            # source text would be a lie about the data. Shipped files and our own code
            # stay under the rule.
            if f.startswith("tests/") and ext != ".py":
                pass
            else:
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

    slug = platform_name(folder)
    targets = read_doc("SEARCH-TARGETS.md")
    add("L10", slug in targets,
        "search-query table for this actor (docs/SEARCH-TARGETS.md)",
        "" if slug in targets else f"no line mentions {slug}")

    icon_rows = [ln for ln in read_doc("ICONS.md").splitlines() if slug in ln]
    icon_ready = any(re.search(r"[0-9a-f]{32}", ln) for ln in icon_rows)
    checks.append({"code": "L11",
                   "status": "PASS" if icon_ready else ("WARN" if icon_rows else "FAIL"),
                   "text": "icon accounted for in docs/ICONS.md",
                   "detail": "file + md5" if icon_ready else ("waiting for logo" if icon_rows else "no row")})

    # L12: SDK misuse that only shows up at runtime on the platform, never in tests
    api_hits, raise_hits = [], []
    for rel in sorted(files):
        if not (rel.startswith("src/") and rel.endswith(".py")):
            continue
        try:
            body = open(os.path.join(folder, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for n, ln in enumerate(body.splitlines(), 1):
            if re.search(r"await\s+Actor\.log\.", ln):
                api_hits.append("%s:%d await Actor.log (log is sync)" % (rel, n))
            if re.search(r"Actor\.fail\(\s*(?!status_message=|exit_code=|exception=)[\"\'f]", ln):
                api_hits.append("%s:%d Actor.fail positional (keyword-only)" % (rel, n))
            if "create_if_missing" in ln:
                api_hits.append("%s:%d create_if_missing (absent in SDK 4.x)" % (rel, n))
            for bad, why in SDK_ABSENT.items():
                if bad in ln:
                    api_hits.append("%s:%d %s" % (rel, n, why))
            # Instance form, and only on lines that actually run code: a docstring sentence
            # about a missing method is not a call and must not turn into a FAIL.
            if "await " in ln or "= " in ln or ln.lstrip().startswith("return "):
                for pattern, why, needs in SDK_ABSENT_CALLS:
                    if needs and needs not in ln.lower():
                        continue
                    if pattern.search(ln):
                        api_hits.append("%s:%d %s" % (rel, n, why))
                        break
            m = re.search(r"Actor\.push_data\((.*)\)\s*$", ln)
            if m:
                inner, depth = m.group(1), 0
                for i, ch in enumerate(inner):
                    if ch in "([{":
                        depth += 1
                    elif ch in ")]}":
                        depth -= 1
                    elif ch == "," and depth == 0:
                        tail = inner[i + 1:].strip()
                        if tail and not tail.startswith("charged_event_name="):
                            api_hits.append("%s:%d push_data extra positional arg" % (rel, n))
                        break
            if rel == "src/main.py" and re.search(r"raise\s+(ValueError|SystemExit)\(", ln):
                raise_hits.append("%s:%d raises instead of Actor.fail" % (rel, n))
    checks.append({"code": "L12", "status": "FAIL" if api_hits else "PASS",
                   "text": "SDK calls match the 4.x signatures",
                   "detail": "; ".join(api_hits[:6]) or "clean"})
    checks.append({"code": "L13", "status": "FAIL" if raise_hits else "PASS",
                   "text": "empty input stops with a readable message, not a traceback",
                   "detail": "; ".join(raise_hits[:6]) or "clean"})

    silent_tests = []
    for rel in sorted(files):
        if not (rel.startswith("tests/") and rel.endswith(".py")):
            continue
        try:
            body = open(os.path.join(folder, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "def run_all" not in body and "__main__" not in body:
            silent_tests.append("%s runs no checks (no run_all/__main__)" % rel)
    checks.append({"code": "L14", "status": "FAIL" if silent_tests else "PASS",
                   "text": "every test file actually runs its checks",
                   "detail": "; ".join(silent_tests[:6]) or "clean"})

    # L15: the output schema is a v1 Store schema, not the legacy dataset shape. The platform
    # rejected exactly this on metadata-og 0.1: "must have required property
    # 'actorOutputSchemaVersion'", then "must have required property 'template'". A local test
    # cannot see it, a build can, and a build costs a push.
    # The file is resolved from actor.json -> output first: ssl-certificate-checker keeps its
    # v1 schema in dataset_schema.json and names it there, so a hard-coded filename reported
    # "no output schema" on an actor whose outputs are described. A guard that cries on
    # working code is the same defect as a missed check.
    schema_rel = ".actor/output_schema.json"
    try:
        named = json.load(open(os.path.join(folder, ".actor/actor.json"), encoding="utf-8")).get("output")
        if named:
            schema_rel = os.path.normpath(os.path.join(".actor", named)).replace(os.sep, "/")
    except Exception:  # noqa: BLE001
        pass
    if schema_rel in files:
        problems = []
        try:
            schema = json.load(open(os.path.join(folder, schema_rel), encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            schema = None
            problems.append("unreadable: %s" % exc)
        if schema is not None:
            if schema.get("actorOutputSchemaVersion") != 1:
                problems.append("actorOutputSchemaVersion missing or not 1")
            if "fields" in schema or "actorSpecification" in schema:
                problems.append("legacy fields/actorSpecification shape")
            props = schema.get("properties")
            if not isinstance(props, dict) or not props:
                problems.append("no properties object")
            else:
                for name, prop in props.items():
                    if not isinstance(prop, dict) or not prop.get("template"):
                        problems.append("%s has no template" % name)
        add("L15", not problems, "output schema is v1 with a template on every entry",
            "; ".join(problems[:6]) or "clean")
    else:
        checks.append({"code": "L15", "status": "WARN", "text": "output schema present",
                       "detail": "no output schema: actor.json names %s" % schema_rel})

    # L16: the run id must be read under the key the SDK really has. get_env() returns the
    # env var names in lower case (run_id, act_id, actor_task_id): there is no "actor_run_id"
    # key, and ActorEnvVars 4.2.0 has no APIFY_ACTOR_RUN_ID at all. A dead fallback does not
    # fail loudly, it silently yields RUN_SUMMARY-run, and then the named store stops telling
    # runs apart. Found live on the published pdf-extractor 0.9.1.
    # Two levels, because the two leftovers differ in cost: a wrong get_env key is the live
    # defect (sweep it before the next build), a dead env name next to a working env-first
    # read changes nothing and is just litter. A guard that screams FAIL on working code is
    # the same defect as a missed check.
    bad_key, dead_name = [], []
    for rel in sorted(files):
        if not (rel.startswith("src/") and rel.endswith(".py")):
            continue
        try:
            body = open(os.path.join(folder, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for n, ln in enumerate(body.splitlines(), 1):
            if ln.lstrip().startswith("#"):
                continue
            if "actor_run_id" in ln:
                bad_key.append("%s:%d %s" % (rel, n, ln.strip()[:80]))
            elif "APIFY_ACTOR_RUN_ID" in ln:
                dead_name.append("%s:%d %s" % (rel, n, ln.strip()[:80]))
    add("L16", not bad_key,
        "run id read under the real key (get_env run_id), no dead actor_run_id",
        "; ".join(bad_key[:6]) or "clean")
    checks.append({"code": "L17",
                   "status": "PASS" if not dead_name else "WARN",
                   "text": "no dead APIFY_ACTOR_RUN_ID fallback (absent from SDK 4.2.0)",
                   "detail": "; ".join(dead_name[:6]) or "clean"})

    # L18: a coroutine from the SDK called without await. Measured in apify 4.2.0 by reading
    # the installed source (apify/_actor.py), not from memory: push_data, fail, get_input,
    # open_dataset, open_key_value_store, set_value, get_value, set_status_message, charge,
    # start, exit, metamorph are `async def`, while get_env, get_charging_manager and log are
    # plain. Called without await they build a coroutine, send nothing and leave one
    # RuntimeWarning: the Store card keeps a promise the code never performs, and every local
    # test stays green because a hand-written double can be synchronous. Found live on
    # pump-fun-new-token-monitor 0.1, where the run header was never set.
    actor_coroutines = (
        "abort", "add_webhook", "call", "call_task", "charge", "create_proxy_configuration",
        "exit", "fail", "get_input", "get_value", "init", "metamorph", "open_dataset",
        "open_key_value_store", "open_request_queue", "push_data", "reboot", "resurrect",
        "set_status_message", "set_value", "start", "start_task", "use_state",
    )
    await_pattern = re.compile(r"Actor\.(%s)\s*\(" % "|".join(actor_coroutines))
    missing_await = []
    for rel in sorted(files):
        if not (rel.startswith("src/") and rel.endswith(".py")):
            continue
        try:
            body = open(os.path.join(folder, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        in_doc = False
        for n, ln in enumerate(body.splitlines(), 1):
            code = ln.split("#")[0]
            # A docstring is prose, not code: a call quoted in it is not a call. Triple
            # quoted blocks are tracked so the guard reads code only, otherwise a guard
            # cries FAIL on working code (cve-lookup-api 0.1).
            marks = code.count('"""') + code.count("'''")
            if in_doc:
                if marks % 2:
                    in_doc = False
                continue
            if marks % 2:
                in_doc = True
                continue
            hit = await_pattern.search(code)
            if not hit:
                continue
            if "await" in code[:hit.start()]:
                continue
            missing_await.append("%s:%d %s called without await"
                                 % (rel, n, hit.group(0).rstrip("( ").strip()))
    add("L18", not missing_await,
        "SDK coroutines are awaited (push_data, fail, set_status_message, open_*, ...)",
        "; ".join(missing_await[:6]) or "clean")

    # L19: the container starts `python -m src.main`. A module that only defines main() and
    # never runs it exits with code 0, writes no dataset and no summary: a green run that
    # reads as "no data" instead of "the code never ran". Found live on workable-jobs-api 0.1
    # (the first local run returned rc=0 with an empty storage).
    entry_rel = "src/main.py"
    entry_ok, entry_detail = False, "%s is missing" % entry_rel
    if entry_rel in files:
        try:
            entry_body = open(os.path.join(folder, entry_rel), encoding="utf-8", errors="replace").read()
        except OSError:
            entry_body = ""
        has_guard = re.search(r"if\s+__name__\s*==\s*[\"']__main__[\"']\s*:", entry_body)
        runs_main = re.search(r"asyncio\.run\(\s*main\s*\(\s*\)\s*\)", entry_body)
        entry_ok = bool(has_guard and runs_main)
        entry_detail = ("asyncio.run(main()) under __main__" if entry_ok
                        else "no asyncio.run(main()) under if __name__ == __main__: the container would exit 0 doing nothing")
    add("L19", entry_ok, "the entry module really runs when the container starts it", entry_detail)

    # L20: the price list is an ActorPricingInfo dataclass in apify 4.2.0, not a dict.
    # Reading only pricing["pricingPerEvent"] makes the guard return None, so every paid row
    # ships with billable=null while the charge itself went through. Found live on
    # workable-jobs-api 0.1.1 (run ICQx80lOFPBBEqE5S: chargedEventCounts 5, five DELIVERED
    # rows, billable null on all of them). A dict stays allowed as a legacy stand-in, but
    # only next to a dataclass path.
    dict_only, legacy_only = [], []
    for rel in sorted(files):
        if not (rel.startswith("src/") and rel.endswith(".py")):
            continue
        try:
            body = open(os.path.join(folder, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        has_dict = "pricingPerEvent" in body
        has_dataclass = "per_event_prices" in body
        if not has_dict:
            continue
        if has_dataclass:
            legacy_only.append(rel)
            continue
        for n, ln in enumerate(body.splitlines(), 1):
            if "pricingPerEvent" in ln:
                dict_only.append("%s:%d %s" % (rel, n, ln.strip()[:80]))
    add("L20", not dict_only,
        "price list read from the SDK dataclass (per_event_prices), not from a dict only",
        "; ".join(dict_only[:6]) or ("clean" if not legacy_only else "dict path kept next to dataclass path: " + ", ".join(legacy_only)))

    # L21: the dataset size is read through a dict guard, while get_metadata() in apify 4.2.0
    # returns a pydantic model (crawlee.storage_clients.models.DatasetMetadata: id, name,
    # item_count, accessed_at, created_at, modified_at). `if not isinstance(meta, dict): return
    # None` is therefore always None on a real run: datasetItemCount ships null and the "the row
    # landed but the charge failed" branch can never fire. Proven by running the installed SDK
    # locally (two rows -> metadata.item_count == 2, isinstance(meta, dict) is False), not by
    # memory. Found by Orestes in personio main.py:59 and workable main.py:56.
    #
    # A working shape keeps a dict read next to an attribute read of the SAME variable
    # (smartrecruiters: isinstance guard that returns, then getattr(info, attr, None)), or reads
    # the value inside a helper that uses the attribute path.
    # Three shapes are caught, each proven on a copy of a working tree:
    #   a) a subscript read info["item_count"] with no attribute read of the SAME variable;
    #   b) a .get("item_count") read with no attribute read of the same variable (DatasetMetadata
    #      has no .get at all, so the AttributeError is swallowed and the counter stays None);
    #   c) the value is read in one function and the attribute path lives in a helper: that is a
    #      lead, not proof, so it is WARN (L22) and a human looks at it.
    # The subject is the variable of the READ, never the variable of an isinstance guard: a guard
    # on settings/config next to a correct getattr(info, ...) is working code, not a defect.
    dict_only_count = []
    helper_suspects = []
    for rel in sorted(files):
        if not (rel.startswith("src/") and rel.endswith(".py")):
            continue
        try:
            body = open(os.path.join(folder, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "item_count" not in body and "itemCount" not in body:
            continue
        lines = body.splitlines()
        starts = [i for i, ln in enumerate(lines) if re.match(r"(async\s+)?def\s+\w+", ln)]
        for idx, first in enumerate(starts):
            last = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
            block = "\n".join(lines[first:last])
            if "item_count" not in block and "itemCount" not in block:
                continue
            attr_vars = set(re.findall(r"getattr\s*\(\s*(\w+)\s*,", block))
            attr_vars |= set(re.findall(r"\b(\w+)\s*\.\s*item_?[cC]ount\b", block))
            reads = []
            for m in re.finditer(r"(\w+)\s*\[\s*[\"']item_?[cC]ount[\"']\s*\]", block):
                reads.append((m.group(1), m))
            for m in re.finditer(r"(\w+)\s*\.\s*get\s*\(\s*[\"']item_?[cC]ount[\"']", block):
                reads.append((m.group(1), m))
            for var, m in reads:
                if var in attr_vars:
                    continue
                line_no = first + block[:m.start()].count("\n") + 1
                calls = re.findall(r"(\w+)\s*\(\s*%s\b(?!\s*\[)" % re.escape(var), block)
                if any(c not in BUILTIN_CALLS for c in calls):
                    helper_suspects.append("%s:%d %s" % (rel, line_no, lines[first].strip()[:60]))
                    continue
                dict_only_count.append("%s:%d %s reads the dataset size as a dict only"
                                       % (rel, line_no, lines[first].strip()[:60]))
    add("L21", not dict_only_count,
        "dataset size read from the DatasetMetadata attribute, not from a dict only",
        "; ".join(dict_only_count[:6]) or "clean")
    add("L22", not helper_suspects,
        "dataset size read through a helper: attribute path assumed, verify by hand",
        "; ".join(helper_suspects[:6]), level="WARN")
    # L23: get_charging_manager() and get_pricing_info() are SYNCHRONOUS in apify 4.2.0.
    #      `await` on them raises TypeError, a silent except turns the price into "unknown"
    #      and every row goes out FREE. Measured on iban 0.1.6 and phone 0.2.2: 3 valid rows,
    #      0 charged, billable null. The test fake was async, so the tests proved nothing.
    # Correct shape awaits the bare VARIABLE, never the call:
    #     manager = Actor.get_charging_manager()
    #     if inspect.isawaitable(manager): manager = await manager
    #     pricing = manager.get_pricing_info()
    #     if inspect.isawaitable(pricing): pricing = await pricing
    # Two holes were closed here: skipping the whole FILE when the word isawaitable appeared
    # (correct shape next to the defect hid it), and then honouring an isawaitable guard line
    # above the call (a one-line guard made the real defect read as "clean"). Now any
    # "await <obj>.get_pricing_info()" is a FAIL, no exceptions.
    awaited = []
    for f in files:
        if not (f.startswith("src/") and f.endswith(".py")):
            continue
        try:
            text = open(os.path.join(folder, f), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        lines = text.splitlines()
        # Hole 3: `_call = manager.get_pricing_info` followed by `await _call()` is the same
        # defect under another name. Aliases are collected first, then any await of them fails.
        aliases = {}
        for n, ln in enumerate(lines, 1):
            code = ln.split("#")[0].rstrip()
            m = re.match(r"\s*(\w+)\s*=\s*[\w\.]*\.get_(?:pricing_info|charging_manager)\s*$", code)
            if m:
                aliases[m.group(1)] = n
            g = re.match(r"\s*(\w+)\s*=\s*getattr\s*\(\s*[\w\.]+(?:\s*\(\s*\))?\s*,\s*[\"'](?:get_pricing_info|get_charging_manager)[\"']\s*\)\s*$",
                         code)
            if g:
                aliases[g.group(1)] = n
        for n, ln in enumerate(lines, 1):
            code_line = ln.split("#")[0]
            hit = re.search(r"await\s+([\w\.]+)\.get_(?:pricing_info|charging_manager)\s*\(", code_line)
            if hit:
                # No exceptions: SDK 4.2.0 has no correct shape with await on these calls. The
                # compatible shape awaits the bare VARIABLE and never matches this pattern.
                awaited.append("%s:%d await on a synchronous SDK method" % (f, n))
                continue
            got = re.search(r"await\s+getattr\s*\(\s*[\w\.]+(?:\s*\(\s*\))?\s*,\s*[\"'](?:get_pricing_info|get_charging_manager)[\"']",
                            code_line)
            if got:
                # getattr(manager, "get_pricing_info")() is the same defect under a disguise
                awaited.append("%s:%d await on a getattr-wrapped synchronous SDK method" % (f, n))
                continue
            alias = re.search(r"await\s+(\w+)\s*\(", code_line)
            if alias and alias.group(1) in aliases:
                awaited.append("%s:%d await on an alias of a synchronous SDK method (bound on line %d)"
                               % (f, n, aliases[alias.group(1)]))
    add("L23", not awaited,
        "price list read without await (get_pricing_info is synchronous in SDK 4.2.0)",
        "; ".join(awaited[:6]) or "clean")


    # L24: the platform's automatic quality check runs a public Actor with the
    # PREFILLED input schema. If no field has prefill and no field has default, that run
    # gets an empty {} input; an Actor that honestly refuses an empty input is then
    # flagged "Under maintenance". Cost us three flags on 2026-11-09 (personio,
    # recruitee, teamtailor).
    qa_input = []
    schema_path = os.path.join(folder, ".actor", "input_schema.json")
    if os.path.exists(schema_path):
        try:
            with open(schema_path, encoding="utf-8") as fh:
                schema = json.load(fh)
        except Exception as exc:
            schema = None
            qa_input.append("input_schema.json is not readable: %s" % exc)
        if isinstance(schema, dict):
            props = schema.get("properties") or {}
            arrays = {k: v for k, v in props.items() if isinstance(v, dict) and v.get("type") == "array"}
            has_prefill = any("prefill" in v for v in props.values() if isinstance(v, dict))
            array_default = any(v.get("default") for v in arrays.values())
            if arrays and not has_prefill and not array_default:
                qa_input.append("array field(s) %s have no prefill and no non-empty default: "
                                "the platform QA run sends an empty input and the Actor refuses it"
                                % ", ".join(sorted(arrays)[:3]))
    add("L24", not qa_input,
        "input can be filled by the platform QA run (prefill or default exists)",
        "; ".join(qa_input[:4]) or "clean")

    # L25: the platform validates input_schema at BUILD time and refuses a build when a
    # string or array field has no "editor" ("Field schema.properties.<name>.editor is
    # required"). Cost us one failed build on #22 telegram-channel-scraper on 2026-11-09,
    # after the tree had already passed the gate: the verdict was about behaviour, the
    # platform was about the schema file. A string field with enum gets a select editor
    # for free, which is why website-screenshot's "format" passes without one.
    editor_missing = []
    if os.path.exists(schema_path):
        try:
            with open(schema_path, encoding="utf-8") as fh:
                eschema = json.load(fh)
        except Exception:
            eschema = None
        if isinstance(eschema, dict):
            for name, spec in (eschema.get("properties") or {}).items():
                if not isinstance(spec, dict):
                    continue
                kind = spec.get("type")
                if kind not in ("string", "array"):
                    continue
                if "editor" in spec:
                    continue
                if kind == "string" and spec.get("enum"):
                    continue
                editor_missing.append("%s (%s)" % (name, kind))
    add("L25", not editor_missing,
        "every string/array input field declares editor (the platform refuses the build otherwise)",
        ("no editor: " + ", ".join(sorted(editor_missing)[:4])) if editor_missing else "clean")

    # L26: a privacy check that walks a redirect list but never looks at the address
    # that was really loaded. A chain without its last hop says nothing about where
    # the browser ended up: a public url answering 302 into 127.0.0.1 was opened,
    # measured and CHARGED that way (core-web-vitals-audit 0.1, QA gate 2026-11-10).
    # The window is deliberate: the list and the guard sit a few lines apart, and the
    # rule only speaks when the same window really runs an address guard.
    guard_gap = []
    guard_call = re.compile(r"host_is_public\s*\(|ip_is_private\s*\(|is_global\b")
    redirect_read = re.compile(
        r"\.get\(\s*[\"']redirects[\"']|\[\s*[\"']redirects[\"']\s*\]"
        r"|response\.history|\.history\b|[\"']hops[\"']")
    for rel in files:
        if not rel.endswith(".py"):
            continue
        try:
            text = open(os.path.join(folder, rel), encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        body_lines = text.splitlines()
        for n, line in enumerate(body_lines):
            if not redirect_read.search(line) or line.lstrip().startswith("#"):
                continue
            # The window reaches a few lines back as well: the address that was really
            # loaded is often read one line before the chain it belongs to
            # (pdf-ocr-api reads final_url on the line above hops).
            joined = "\n".join(body_lines[max(0, n - 5):n + 25])
            if not guard_call.search(joined):
                continue
            if "finalUrl" in joined or "final_url" in joined:
                continue
            guard_gap.append("%s:%d redirect list guarded without the final url" % (rel, n + 1))
    add("L26", not guard_gap,
        "a redirect chain is checked together with the address that was really loaded",
        ", ".join(guard_gap[:4]) if guard_gap else "clean")

    # L27: the platform refuses an actor whose description is longer than 300
    # characters (400 schema-validation at create and at build). Cost us a blocked
    # push on #27 image-converter 2026-11-10: the tree had passed the gate at 312
    # characters, so the verdict could not be executed at all.
    desc_len = 0
    actor_json = os.path.join(folder, ".actor", "actor.json")
    if os.path.exists(actor_json):
        try:
            with open(actor_json, encoding="utf-8") as fh:
                desc_len = len((json.load(fh).get("description") or ""))
        except Exception:
            desc_len = 0
    add("L27", desc_len <= 300,
        "actor.json description fits the platform limit (300 chars)",
        "%d chars" % desc_len)




# L29: the platform refuses a title longer than 63 characters (400
    # schema-validation on card create and on build). Cost us a card on
    # #40 bluesky-posts-api 2026-11-12: the tree passed the gate at 64.
    title_len = 0
    if os.path.exists(actor_json):
        try:
            with open(actor_json, encoding="utf-8") as fh:
                title_len = len((json.load(fh).get("title") or ""))
        except Exception:
            title_len = 0
    add("L29", title_len <= 63,
        "actor.json title fits the platform limit (63 chars)",
        "%d chars" % title_len)

    # L30: the platform refuses an input schema that carries `actorSpecification`
    # (400 "Property schema.actorSpecification is not allowed"). That key belongs to
    # the OUTPUT schema. Cost us a failed build on #40 bluesky-posts-api 2026-11-12:
    # the upload succeeded and the build died, so nothing could be tested on the platform.
    input_schema_path = os.path.join(folder, ".actor", "input_schema.json")
    schema_bad = ""
    if os.path.exists(input_schema_path):
        try:
            with open(input_schema_path, encoding="utf-8") as fh:
                sch = json.load(fh)
            if "actorSpecification" in sch:
                schema_bad = "actorSpecification is not allowed in an input schema"
            elif sch.get("schemaVersion") != 1 or sch.get("type") != "object":
                schema_bad = "input schema needs schemaVersion 1 and type object"
        except Exception as exc:
            schema_bad = "input schema does not parse: %s" % exc
    add("L30", not schema_bad,
        "input schema carries no key the platform refuses",
        schema_bad or "clean")

    # L28: a key-value record key allows only a-zA-Z0-9!-_.'() . The platform answered
    # 400 invalid-record-key for `images/<name>.<ext>` on #27 image-converter and every
    # delivered image became NOT_CHECKED_STORE_ERROR, while the same code wrote files to
    # the local disk without a complaint. A key built in a variable is followed back to
    # its assignment, and that assignment has to pass through the filter.
    key_gap = []
    for f in files:
        if not f.endswith(".py") or f.startswith("tests/"):
            continue
        try:
            text = open(os.path.join(folder, f), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        src_lines = text.splitlines()
        names = set()
        for line in src_lines:
            if ".set_value(" not in line:
                continue
            after = line.split(".set_value(", 1)[1].lstrip()
            ident = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", after)
            if ident:
                names.add(ident.group(1))
        for n, line in enumerate(src_lines, 1):
            stripped = line.strip()
            for name in names:
                if not stripped.startswith(name + " ="):
                    continue
                right = stripped.split("=", 1)[1]
                if "/" in right and "store_key(" not in right:
                    key_gap.append("%s:%d" % (f, n))
    add("L28", not key_gap,
        "a store record key carries no slash (platform allows only a-zA-Z0-9!-_.'())",
        ", ".join(key_gap[:4]) if key_gap else "clean")
    # L31: `minimalMaxTotalChargeUsd` is the FLOOR OF THE BUYER'S SPENDING LIMIT, not the
    # minimum a run costs (Apify docs, "Set a minimum for the user limit"). A README line
    # "minimum spend per run $0.05" makes a $0.0004 product read like a $0.05 one. It sat
    # in three trees unnoticed (#37, #38, #39) until Orestes read the money section on
    # 2026-11-12; a run that delivered 100 videos for $0.04 is legal. WARN, not FAIL: the
    # trees that already carry the line have to reach the store first.
    # Three phrasings were measured in the park on 2026-11-13, not one: "minimum spend per
    # run", "minimum spend cap", "minimum charge per run". And the phrase can be wrapped
    # over two lines ("minimum\ncharge per run" on recruitee-jobs-api:80), so the scan runs
    # over the whole text: a line-by-line search missed exactly that tree while Orestes
    # counted six and I counted five. Level was WARN only while the debt was open: channel
    # and bluesky carried the line into their slots on purpose (Orestes, 13.11), recruitee
    # sat frozen inside the gate. The last four trees were cleaned on 13.11 and Orestes ran
    # the switched variant over all 40: not one FAIL by this class, so WARN would have let
    # the line ship silently. FAIL now, like every other money lie on the card.
    wording_gap = []
    readme_path = os.path.join(folder, "README.md")
    if os.path.exists(readme_path):
        try:
            readme_text = open(readme_path, encoding="utf-8", errors="replace").read()
        except OSError:
            readme_text = ""
        for m in re.finditer(r"minimum\s+(?:spend|charge)", readme_text, re.I):
            wording_gap.append("README.md:%d" % (readme_text[:m.start()].count("\n") + 1))
    add("L31", not wording_gap,
        "README does not call the buyer's spending-limit floor a minimum spend/charge per run",
        ", ".join(wording_gap[:3]) if wording_gap else "clean")

    # L32-L35, added 2026-11-14 after pypi-package-api was REJECTED BY THE BUILD three times
    # while my own checklist stayed green. Each of the three shapes below is a 400 from the
    # platform on `apify push`, so the tree never becomes a version at all:
    #   views.<name> without `transformation`   -> "Field input.views.overview is not allowed"
    #   a root-level `actorId` in input_schema  -> "Field input.actorId is not allowed"
    #   defaultRunOptions without memoryMbytes  -> platform stores 1024 MB, the card and the
    #                                              tree then disagree (P11, memory check).
    # I had "prefill is present" as a REASON for that failed push and it was FALSE: the
    # property-prefill was there all along, only the third free example differed. A
    # a plausible reason instead of an observed one is the class this gate exists to catch, so
    # the check below is machine, not memory. Authority stays with the build: a green L32-L34
    # does not promise the build succeeds, it only rules out the three known shapes.
    # L35 stays WARN until the park is clean (Orestes, 14.11): pump-fun, shopify and
    # smartrecruiters have no property-prefill and fall over on an empty input.
    actor_json, schema = {}, {}
    for name, target in ((".actor/actor.json", actor_json), (".actor/input_schema.json", schema)):
        fp = os.path.join(folder, name)
        if os.path.exists(fp):
            try:
                target.update(json.loads(open(fp, encoding="utf-8").read()))
            except Exception:
                pass

    bad_views = []
    storages = actor_json.get("storages")
    dataset = storages.get("dataset") if isinstance(storages, dict) else None
    views = dataset.get("views") if isinstance(dataset, dict) else None
    if isinstance(views, dict):
        for vname, view in views.items():
            if not isinstance(view, dict) or "transformation" not in view:
                bad_views.append("views.%s" % vname)
    add("L32", not bad_views,
        "every dataset view carries `transformation` (a view without it is a 400 on push)",
        ", ".join(bad_views) if bad_views else "clean")

    root_actor_id = "actorId" in schema
    add("L33", not root_actor_id,
        "input_schema has no root-level `actorId` (the platform answers 400)",
        "found at the top level" if root_actor_id else "clean")

    # L37, found the hard way on 2026-11-14 while probing the memory form: my throwaway actor
    # failed its FIRST build with "Input schema is not valid (Field
    # schema.properties.urls.description is required)". A property without a description is a
    # 400 from the build, so the tree never becomes a version - same class as L32/L33. Measured
    # over the park before writing: 0 of 50 trees carry an undescribed property.
    # Recursive since 14.11 (Orestes' find H2): the top-level walk let a property inside
    # type:object and inside items.properties pass as clean, and both are the same 400.
    # The same day I measured the neighbouring case on two throwaway actors (both deleted in the
    # same run, count back to 50): an undescribed property in output_schema does NOT break the
    # build (SUCCEEDED), while the same trick on input_schema fails it with "Field
    # schema.properties.<name>.description is required". So output_schema is a different class and
    # L37 rightly does not cover it; storages.dataset is not measured at all.
    undescribed = sorted(undescribed_props(schema))
    add("L37", not undescribed,
        "every input_schema property carries a description (a property without one is a 400 on push)",
        ", ".join(undescribed[:5]) if undescribed else "clean")

    untyped = sorted(untyped_props(schema))
    add("L38", not untyped,
        "every input_schema property declares a `type` (a property with only anyOf/oneOf is a 400 on push)",
        ", ".join(untyped[:5]) if untyped else "clean")

    # L34 re-pointed 2026-11-14 (Orestes asked whether L36 "memory declared somewhere" is
    # needed). Measured BEFORE writing: 0 of 50 trees carry `defaultRunOptions` at all, so
    # the old condition `bool(dro) and ...` could never fire: the guard had never checked
    # anything, the decorative-check class. Memory actually lives at the root of actor.json
    # in all 50 trees, so L34 now reads both shapes; no separate L36, one live guard instead
    # of one dead plus one new.
    # Measured 2026-11-14 on a throwaway actor (probe-memory-form-tmp, deleted in the same run):
    # push ACCEPTS defaultRunOptions.memoryMbytes and root defaultMemoryMbytes without a 400, and
    # the actor OBJECT kept memoryMbytes 1024 after both pushes; only PUT /acts/{id} moved the
    # object field to 256. That is the object, not the run: Orestes (14.11, find H1) measured the
    # runs themselves and they start with the version's defaultMemoryMbytes (pypi 256,
    # image-converter 1024, core-web-vitals 4096; the card matched the tree on all 50). The docs
    # say the same: "the default amount of memory to be used when the Actor is started", and 1024
    # is the platform default only for an actor that declares no memory at all - 0 such trees
    # here. So the tree value IS what a run gets; I had measured the wrong thing and my "1024"
    # (and the "4096" before it) were never the run's memory.
    dro = actor_json.get("defaultRunOptions")
    dro = dro if isinstance(dro, dict) else {}
    mem_forms = []
    # Presence is counted by KEY, not by truthiness: `defaultMemoryMbytes: 0` is declared,
    # and 0 is falsy (Orestes' find G4, 14.11). Same rule as _mem above.
    if actor_json.get("defaultMemoryMbytes") is not None:
        mem_forms.append("root defaultMemoryMbytes=%s" % actor_json.get("defaultMemoryMbytes"))
    dro_default = dro.get("defaultMemoryMbytes", dro.get("memoryMbytes"))
    if dro_default is not None:
        mem_forms.append("defaultRunOptions memory=%s" % dro_default)
    if mem_forms:
        l34_detail = ", ".join(mem_forms)
    elif any(k in actor_json or k in dro for k in ("minMemoryMbytes", "maxMemoryMbytes")):
        # min/max are declared, only the default is missing: "declared nowhere" would be
        # a false statement about the tree (Orestes' find G3, 14.11).
        l34_detail = "defaultMemoryMbytes not declared (min/max are)"
    else:
        l34_detail = "memory declared nowhere"
    add("L34", bool(mem_forms),
        "actor.json declares its memory somewhere (the platform starts the run with the version's defaultMemoryMbytes; the card repeats it today on all 50)",
        l34_detail)

    # L36 added 2026-11-14 after Orestes corrected my park measurement: minMemoryMbytes and
    # maxMemoryMbytes ARE declared at the root of all 50 actor.json (I had looked only at
    # `memoryMbytes`, which lives inside defaultRunOptions and is declared by nobody). So
    # min <= default <= max is checkable today on every tree; violations 0 when written.
    # Measured before writing: 50/50 carry all three, 0 trees keep memory in two places at once.
    def _mem(obj):
        # Presence is counted by KEY, not by type: Apify documents all three fields as
        # optional and independent, and defaultMemoryMbytes may be an expression string
        # ("get(input, 'startUrls.length', 1) * 1024"). Requiring int here made an
        # expression look "not declared" (Orestes' find G2, 2026-11-14).
        out = {}
        if isinstance(obj, dict):
            for k in ("minMemoryMbytes", "defaultMemoryMbytes", "maxMemoryMbytes"):
                if k in obj and obj[k] is not None:
                    out[k] = obj[k]
            # The platform stores the runner memory inside defaultRunOptions under
            # `memoryMbytes` (proof: her own actor object 8YLFmRvPwY7iPSPGb returns
            # defaultRunOptions {build, timeoutSecs, memoryMbytes: 256}). Reading only
            # `defaultMemoryMbytes` there made G34 say "declared nowhere" on a tree written in
            # the documented shape (Orestes' find G5, 14.11).
            if "defaultMemoryMbytes" not in out and obj.get("memoryMbytes") is not None:
                out["defaultMemoryMbytes"] = obj["memoryMbytes"]
        return out

    root_mem = _mem(actor_json)
    dro_mem = _mem(dro)
    both = bool(root_mem) and bool(dro_mem)
    merged = dict(root_mem)
    merged.update(dro_mem)
    mn, dm, mx = merged.get("minMemoryMbytes"), merged.get("defaultMemoryMbytes"), merged.get("maxMemoryMbytes")
    problems = []
    warnings = []
    if both:
        problems.append("memory declared in two places at once (root and defaultRunOptions)")
    # Bounds completeness. Added 2026-11-14 on Orestes' find (a green line read as "the order
    # was verified" while nothing was verified), refined the same day on his next two finds:
    # G1 - a tree with a lone defaultMemoryMbytes is LEGAL (all three fields are optional and
    #      independent in Apify's docs), so a partial set is a WARN, not a FAIL on a working
    #      tree; a FAIL there is the class worse than a missed check.
    # G2 - presence is judged by key, and the order is checked only when all three are plain
    #      numbers; an expression is declared, it just cannot be ordered here.
    names3 = ("minMemoryMbytes", "defaultMemoryMbytes", "maxMemoryMbytes")
    missing = [n for n in names3 if n not in merged]
    # A real number vs an expression vs a boolean are three different things:
    # 256.5 is a number (the order can be checked), "get(input, ...) * 1024" is an
    # expression (declared, not orderable here), True is neither (isinstance(int) lies
    # about it). Found by Orestes, 14.11, as the tail of G3.
    def _isnum(v):
        return type(v) in (int, float)
    dynamic = [n for n in names3 if n in merged and not isinstance(merged.get(n), bool)
               and not _isnum(merged.get(n))]
    boolish = [n for n in names3 if isinstance(merged.get(n), bool)]
    if boolish:
        warnings.append("not a memory size: %s (a boolean is not a number)" % ", ".join(
            "%s=%r" % (n, merged.get(n)) for n in boolish))
    if merged and missing:
        warnings.append("bounds incomplete: %s not declared (all three fields are optional on "
                        "Apify, so this is a warning, not a failure)" % ", ".join(missing))
    if dynamic:
        warnings.append("bounds are dynamic, order not checked: %s" % ", ".join(
            "%s=%r" % (n, merged.get(n)) for n in dynamic))
    # G3 (Orestes, 14.11): an empty range is broken by meaning even when default is
    # absent, so the two known bounds are ordered on their own. Before this a legal-by-
    # declaration but empty range (min=2048 max=1024, no default) passed as a warning.
    if _isnum(mn) and _isnum(mx) and mn > mx:
        problems.append("min=%s max=%s is an empty range: min > max" % (mn, mx))
    numeric = not missing and not dynamic and not boolish
    if numeric and not (mn <= dm <= mx):
        problems.append("min=%s default=%s max=%s is not min <= default <= max" % (mn, dm, mx))
    detail = "; ".join(problems + warnings)
    if not detail:
        detail = ("min=%s default=%s max=%s" % (mn, dm, mx) if numeric
                  else "no bounds declared at all, L34 covers that case")
    add("L36", not problems and not warnings,
        "memory bounds declared in one place, and the order is verified whenever all three "
        "are plain numbers",
        detail,
        level="FAIL" if problems else "WARN")

    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    has_prefill = any(isinstance(v, dict) and "prefill" in v for v in props.values())
    add("L35", has_prefill,
        "input_schema carries a property-prefill (the platform runs the actor with it)",
        "no property-prefill: an empty run is what the platform QA sends" if not has_prefill else "clean",
        level="WARN")
    return checks


def platform_checks(actor: dict, tree: dict | None = None) -> list[dict]:
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
    # A bigger machine is allowed only when the tree says in plain words why: a silent
    # exception would hide the cost, and a hard ceiling would fail an honest actor that
    # cannot obey it (Whisper needs one 4096 MB thread on purpose).
    justification = ""
    if isinstance(tree, dict):
        justification = str(((tree.get("meta") or {}).get("memoryJustification") or "")).strip()
    add("P5", bool(mem) and (mem <= 1024 or bool(justification)),
        "run memory <=1024 MB or justified in actor.json",
        (("%s MB, justified: %s" % (mem, justification[:90])) if justification else str(mem)))

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
    checks.append({"code": "P9", "status": "PASS" if pic else ("FAIL" if public else "WARN"),
                   "text": "icon set", "detail": "none" if not pic else "ok"})

    desc = actor.get("description") or ""
    words = desc.split()
    first = words[0].strip("\"'(-,.:").lower() if words else ""
    starts_feature = first in FEATURE_START
    # The pain must be in the FIRST SENTENCE, not somewhere in the first 150 chars:
    # "Real mod-97 checksum, ... the reason a number fails" opened with a capability
    # and still passed, because "fail" sat at char 120.
    sent = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0][:220]
    sent_l = sent.lower()
    cap_open = bool(re.match(r"(bulk|real|fast|easy|simple|powerful|advanced|extract|scrape|"
                             r"validate|check|get|find|parse|convert|generate)\b", sent_l)) \
               or bool(re.search(r"\bapi for\b", sent_l[:60]))
    has_pain = any(m in sent_l for m in PAIN_MARKERS)
    if not desc or len(desc) > 300:
        p10 = "FAIL"
    elif starts_feature or cap_open or not has_pain:
        p10 = "FAIL"
    else:
        p10 = "PASS"
    checks.append({"code": "P10", "status": p10,
                   "text": "description opens with the pain, <=300 chars (proxy)",
                   "detail": f"{len(desc)} chars, first word '{first}', capability opener {cap_open}, first-sentence pain {'yes' if has_pain else 'NO'}"})

    # Card vs tree: memory and categories are PROMISED on the Store card, but the build
    # takes them from .actor/actor.json. A mismatch shows the buyer a number the run will
    # never get (phone: card 512, tree 256) and the next build silently reverts the tags.
    if tree:
        card_mem = (actor.get("defaultRunOptions") or {}).get("memoryMbytes")
        tree_mem = tree.get("defaultMemoryMbytes")
        add("P11", card_mem == tree_mem, "run memory on the card equals .actor/actor.json",
            f"card {card_mem} vs tree {tree_mem}")
        card_cats = set(actor.get("categories") or [])
        tree_cats = set(tree.get("categories") or [])
        add("P12", card_cats == tree_cats, "categories on the card equal .actor/actor.json",
            f"card {sorted(card_cats)} vs tree {sorted(tree_cats)}")
        # P13: the description is a PROMISE, and it lives in two places. Editing the card
        # through the API does not touch the tree, and the next push rewrites the card from
        # actor.json: a text fixed on the Store quietly reverts on the next build. Found on
        # whois-domain-lookup 0.1.1 (card 216 chars, tree 271) and then measured across the
        # park: 14 of 21 actors differed. The tree now equals the card, and this guard keeps
        # the two from drifting apart again.
        card_desc = (actor.get("description") or "").strip()
        tree_desc = (tree.get("description") or "").strip()
        add("P13", card_desc == tree_desc, "description on the card equals .actor/actor.json",
            "%d vs %d chars%s" % (len(card_desc), len(tree_desc),
                                  "" if card_desc == tree_desc else ": the next build rewrites the card text"))

    # P14: the Store prints "$X / 1,000 <eventTitle lowercased>s", so a past-participle
    # title reads as garbage on the buyer's card ("job delivereds", "iban checkeds").
    # Measured 13.11: 33 of 44 charge events in the park ended in -ed. Fixed by adding one
    # pricing record per actor (all existing records as-is + one new), price and event key
    # untouched; billing proved live on youtube-shorts-api (2 rows -> 2 charged events).
    pis = actor.get("pricingInfos") or []
    evs = ((pis[-1].get("pricingPerEvent") or {}).get("actorChargeEvents") or {}) if pis else {}
    bad_titles = []
    for key, ev in evs.items():
        if ev.get("isOneTimeEvent"):
            continue
        title = (ev.get("eventTitle") or "").strip()
        if not title:
            bad_titles.append("%s: empty title" % key)
            continue
        last_word = title.lower().split()[-1]
        if last_word.endswith(("ed", "s")) or last_word in ("up", "out", "in", "on", "with", "of", "to", "for"):
            bad_titles.append("%s: '%s'" % (key, title))
    add("P14", not bad_titles,
        "event titles read as a clean plural on the Store (the Store appends 's')",
        ", ".join(bad_titles[:4]) if bad_titles else "clean")
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
        pname = platform_name(path)
        item = by_short.get(pname)
        if item:
            full = api_get(f"/acts/{item['id']}", tok)["data"]
            tree = {}
            try:
                with open(os.path.join(path, ".actor", "actor.json"), encoding="utf-8") as fh:
                    tree = json.load(fh)
            except Exception:
                tree = {}
            checks += platform_checks(full, tree)
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
