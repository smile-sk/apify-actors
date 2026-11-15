"""PyPI reader: one JSON answer per project name, no key and no browser.

pypi.org/pypi/<name>/json returns the whole project: the latest release metadata,
every release ever published, the files of each release and its classifiers. A name
that PyPI does not have answers 404 with a 24-byte body, which is a verdict, not a
failure: it comes back as a free "no such package" row.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Optional

TIMEOUT = 20.0
POLITE_SECONDS = 0.05
POOL_SIZE = 10
MAX_NAME = 200
# PyPI asks callers to identify themselves; the README promised a User-Agent that names
# this actor and the code sent none at all (a bare httpx UA).
USER_AGENT = "apify-pypi-package-api/0.1 (+https://apify.com/stas_k/pypi-package-api)"

_JSON = "application/json"

# A project name on PyPI: letters, digits, dash, underscore, dot. Normalised form
# folds runs of -_. into a single dash and lowercases, which is how PyPI compares.
_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,198})[A-Za-z0-9]$|^[A-Za-z0-9]$")
_PROJECT_PATH = re.compile(r"^/project/([^/]+)(?:/([^/]+))?/?$")


@dataclass
class Target:
    """One line of the buyer's input, resolved to a project name (+ optional version)."""

    request: str          # exactly what was looked up, with the version when given
    key: str              # what the run deduplicates on: normalised name, no version
    name: str             # the name to put into the URL
    version: Optional[str] = None


def normalise(name: str) -> str:
    """PyPI's own comparison form: lowercase, runs of -_. collapse to one dash."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_target(raw) -> tuple[Optional[Target], str]:
    """Turn one input line into a Target, or explain why it is not a package name."""
    if not isinstance(raw, str):
        return None, ("the line is not a string: send a PyPI project name such as "
                      '"requests" or "requests==2.31.0"')
    text = raw.strip()
    if not text:
        return None, ("the line is empty: send a PyPI project name such as "
                      '"requests" or "requests==2.31.0"')
    if len(text) > MAX_NAME:
        return None, (f"the line is longer than {MAX_NAME} characters, which is longer "
                      "than a PyPI project name can be")
    if "://" in text:
        low = text.lower()
        if "pypi.org" not in low:
            return None, ("this link does not point at pypi.org: only "
                          "https://pypi.org/project/<name>/ links are read")
        path = text.split("pypi.org", 1)[1]
        path = path.split("?", 1)[0].split("#", 1)[0]
        found = _PROJECT_PATH.match(path if path.startswith("/") else "/" + path)
        if not found:
            return None, ("this pypi.org link is not a project page: expected "
                          "https://pypi.org/project/<name>/")
        name, version = found.group(1), found.group(2)
    else:
        name, version = text, None
        if "==" in text:
            name, _, version = text.partition("==")
            name = name.strip()
            version = version.strip() or None
        elif text.count("=") == 1 and not text.startswith("="):
            # "requests=" is a typo for "requests==", not a version selector
            name = text.split("=", 1)[0].strip()
    if not name or not _NAME_RE.match(name):
        return None, (f'"{raw.strip()[:80]}" is not a PyPI project name: use letters, '
                      'digits, dashes, underscores and dots, for example "requests"')
    key = normalise(name)
    request = f"{name}=={version}" if version else name
    return Target(request=request, key=key, name=name, version=version), ""


def _iso(value) -> Optional[str]:
    if isinstance(value, str) and len(value) >= 10:
        return value
    return None


def _row(payload: dict, target: Target, with_dependencies: bool) -> dict:
    info = payload.get("info") or {}
    releases = payload.get("releases") or {}
    urls = payload.get("urls") or []

    # The version that was asked for wins; otherwise the project's latest. A version PyPI
    # never published is NOT a row: selling it would invent the version field the README
    # says stays null, hand the buyer no metadata for that release, and eat the honest line
    # for the real version sent in the same batch.
    latest = info.get("version")
    if target.version and target.version not in releases:
        return None, (f"PyPI has no release {target.version} of "
                      f"{info.get('name') or target.name} (latest is {latest}): "
                      "this line was not sold and is not charged")
    version = target.version or latest
    files = releases.get(version) if version else None
    if files is None and version == latest:
        files = urls
    files = files or []

    uploads = []
    for files_of_version in releases.values():
        for item in files_of_version or ():
            stamp = _iso(item.get("upload_time_iso_8601"))
            if stamp:
                uploads.append(stamp)
    uploads.sort()

    classifiers = [c for c in (info.get("classifiers") or []) if isinstance(c, str)]
    python_versions = []
    for c in classifiers:
        if not c.startswith("Programming Language :: Python :: "):
            continue
        tail = c.split("::")[-1].strip()
        # "3 :: Only" and "2.7 :: Beta" are not versions; keep numbers only
        if re.fullmatch(r"\d+(?:\.\d+)?", tail):
            python_versions.append(tail)
    dependencies = [d for d in (info.get("requires_dist") or []) if isinstance(d, str)]

    project_urls = info.get("project_urls") or {}
    if not isinstance(project_urls, dict):
        project_urls = {}

    return {
        "name": info.get("name") or target.name,
        "version": version,
        "summary": info.get("summary") or None,
        "license": (info.get("license") or None) if isinstance(info.get("license"), str) else None,
        "requiresPython": info.get("requires_python") or None,
        "author": info.get("author") or info.get("author_email") or None,
        "homepage": info.get("home_page") or project_urls.get("Homepage") or None,
        "repository": project_urls.get("Repository") or project_urls.get("Source") or None,
        "pythonVersions": python_versions or None,
        "dependencies": (dependencies[:50] if with_dependencies else None),
        "dependenciesCount": len(dependencies),
        "dependenciesTruncated": bool(with_dependencies and len(dependencies) > 50),
        "releaseCount": len(releases),
        "firstRelease": uploads[0] if uploads else None,
        "latestRelease": uploads[-1] if uploads else None,
        "filesInRelease": len(files),
        "releaseSizeBytes": sum(f.get("size") or 0 for f in files) or None,
        "classifiers": classifiers or None,
        "source": "PyPI JSON API (keyless)",
    }, ""


def read_packages(client, targets: Iterable[Target], *, with_dependencies: bool = True,
                  timeout: float = TIMEOUT, results: dict | None = None
                  ) -> tuple[dict, dict, dict]:
    """Read every target one by one.

    Returns (rows, refused, missing), all keyed by the string the buyer sent.
    Keying by the request and not by the canonical name matters: two versions of one project
    are two different questions, and keying by the name let the unreadable version overwrite
    the readable one inside a single batch.
    A 404 is not a failure: the line stays absent from rows and main() reports it as a free
    verdict. A 5xx or a broken socket is a failed read of that one line and lands in refused.
    A version PyPI never published lands in missing and is free too.
    """
    rows: dict = results if results is not None else {}
    refused: dict = {}
    missing: dict = {}
    for target in targets:
        url = f"https://pypi.org/pypi/{target.name}/json"
        try:
            answer = client.get(url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            refused[target.request] = (f"PyPI could not be reached for this name "
                                       f"({type(exc).__name__}): not read")
            continue
        if answer.status_code == 404:
            continue
        if answer.status_code != 200:
            refused[target.request] = (f"PyPI answered HTTP {answer.status_code}: this name "
                                       "was not read, send it again later")
            continue
        try:
            payload = answer.json()
        except (json.JSONDecodeError, ValueError):
            refused[target.request] = ("PyPI answered with something that is not JSON: this "
                                       "name was not read")
            continue
        if not isinstance(payload, dict):
            refused[target.request] = "PyPI answered without a project object: this name was not read"
            continue
        row, missing_reason = _row(payload, target, with_dependencies)
        if row is None:
            missing[target.request] = missing_reason
            continue
        rows[target.request] = row
    return rows, refused, missing
