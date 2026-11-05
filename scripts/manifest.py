#!/usr/bin/env python3
"""Build a source manifest from the BYTES on disk (never from decoded text).

Why this file exists: a manifest computed on text with CRLF->LF normalisation
matches the platform only by accident. The platform stores the bytes you pushed,
so the hashes must be taken from those same bytes.

Usage:  python scripts/manifest.py <actor_dir> <build_label> [out_dir]
Writes manifest.json and MANIFEST.txt, prints TOTAL.
"""
import hashlib
import json
import os
import sys

SKIP_DIRS = {"tests", "__pycache__", ".pytest_cache", ".git", ".venv"}
SKIP_SUFFIX = (".pyc", ".pyo", ".bak", ".orig")


def collect(root):
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(SKIP_SUFFIX) or ".bak_" in name:
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append(rel)
    return sorted(out)


def total_of(pairs):
    lines = "\n".join("%s  %s" % (rel, hx) for rel, hx in pairs)
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def main():
    root = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else "unlabelled"
    out_dir = sys.argv[3] if len(sys.argv) > 3 else os.path.join(root, "..", "..", "docs", "manifests", label)
    pairs = []
    for rel in collect(root):
        with open(os.path.join(root, rel), "rb") as fh:
            pairs.append((rel, hashlib.sha256(fh.read()).hexdigest()))
    total = total_of(pairs)
    os.makedirs(out_dir, exist_ok=True)
    payload = {
        "build": label,
        "algorithm": "per-file sha256 of the file BYTES; TOTAL = sha256 of '<path>  <hex>' lines sorted by path, joined by \\n",
        "files": dict(pairs),
        "TOTAL_SHA256": total,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=1, sort_keys=False)
        fh.write("\n")
    with open(os.path.join(out_dir, "MANIFEST.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("%s\n" % label)
        fh.write("algorithm: per-file sha256 of the file BYTES; TOTAL = sha256 of '<path>  <hex>' joined by \\n, sorted by path\n")
        for rel, hx in pairs:
            fh.write("%s  %s\n" % (rel, hx))
        fh.write("TOTAL  %s\n" % total)
    print("files:", len(pairs), "TOTAL:", total, "out:", out_dir)


if __name__ == "__main__":
    main()
