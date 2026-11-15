"""Pure checks: the reader and the input parser, no SDK and no real network."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from src import pypi


# ------------------------------------------------------------------ parse_target

def test_bare_name():
    t, why = pypi.parse_target("requests")
    return t is not None and t.name == "requests" and t.key == "requests" and t.version is None and why == ""


def test_pinned_version():
    t, why = pypi.parse_target("requests==2.31.0")
    return t is not None and t.version == "2.31.0" and t.name == "requests" and t.key == "requests"


def test_case_folds():
    # PyPI compares names case-insensitively: Requests IS requests, and must not be sold twice.
    a, _ = pypi.parse_target("Requests")
    b, _ = pypi.parse_target("requests")
    return a is not None and b is not None and a.key == b.key == "requests"


def test_separators_fold():
    a, _ = pypi.parse_target("zope.interface")
    b, _ = pypi.parse_target("zope-interface")
    c, _ = pypi.parse_target("zope_interface")
    return a is not None and a.key == b.key == c.key == "zope-interface"


def test_project_link():
    t, _ = pypi.parse_target("https://pypi.org/project/requests/")
    return t is not None and t.key == "requests" and t.version is None


def test_project_link_with_version():
    t, _ = pypi.parse_target("https://pypi.org/project/requests/2.31.0/")
    return t is not None and t.version == "2.31.0"


def test_foreign_host_is_invalid():
    t, why = pypi.parse_target("https://example.com/project/requests/")
    return t is None and "pypi.org" in why


def test_junk_is_invalid():
    t, why = pypi.parse_target("hello world")
    return t is None and why


def test_empty_line_is_invalid():
    t, why = pypi.parse_target("   ")
    return t is None and why


def test_too_long_is_invalid():
    t, why = pypi.parse_target("a" * 300)
    return t is None and why


def test_not_a_string_is_invalid():
    t, why = pypi.parse_target(42)
    return t is None and why


def test_strips_whitespace():
    t, _ = pypi.parse_target("  requests  ")
    return t is not None and t.name == "requests"


# ------------------------------------------------------------------ read_packages

def payload(name="requests", version="2.31.0", deps=("urllib3<3", "certifi>=2017")):
    return {
        "info": {
            "name": name, "version": version, "summary": "Python HTTP for Humans.",
            "license": "Apache 2.0", "requires_python": ">=3.7", "author": "Kenneth Reitz",
            "home_page": "https://requests.readthedocs.io",
            "project_urls": {"Repository": "https://github.com/psf/requests"},
            "classifiers": ["Programming Language :: Python :: 3.11", "Programming Language :: Python :: 3"],
            "requires_dist": list(deps),
        },
        "releases": {
            version: [{"upload_time_iso_8601": "2023-05-22T00:00:00Z", "size": 1000}],
            "2.30.0": [{"upload_time_iso_8601": "2023-05-03T00:00:00Z", "size": 900}],
        },
        "urls": [{"upload_time_iso_8601": "2023-05-22T00:00:00Z", "size": 1000}],
    }


def client_answering(table):
    def handler(request):
        url = str(request.url)
        name = url.split("/pypi/", 1)[1].rsplit("/json", 1)[0]
        answer = table.get(name)
        if answer is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if isinstance(answer, int):
            return httpx.Response(answer, text="oops")
        return httpx.Response(200, json=answer)
    return httpx.Client(transport=httpx.MockTransport(handler))


def read_one(raw, table):
    target, why = pypi.parse_target(raw)
    assert target is not None, why
    with client_answering(table) as client:
        return pypi.read_packages(client, [target])


def test_row_has_the_metadata():
    rows, refused, missing = read_one("requests", {"requests": payload()})
    row = rows["requests"]
    return (refused == {} and missing == {} and row["version"] == "2.31.0"
            and row["license"] == "Apache 2.0" and row["requiresPython"] == ">=3.7"
            and row["releaseCount"] == 2 and row["firstRelease"] == "2023-05-03T00:00:00Z"
            and row["dependenciesCount"] == 2 and row["repository"] == "https://github.com/psf/requests"
            and "3.11" in row["pythonVersions"])


def test_missing_name_is_a_verdict_not_a_failure():
    rows, refused, missing = read_one("does-not-exist", {})
    return rows == {} and refused == {} and missing == {}


def test_no_such_release_is_missing_not_a_row():
    # PyPI has no 9.9.9 of requests: selling it would invent the version field and charge
    # for a release that does not exist.
    rows, refused, missing = read_one("requests==9.9.9", {"requests": payload()})
    return (rows == {} and refused == {} and "requests==9.9.9" in missing
            and "latest is 2.31.0" in missing["requests==9.9.9"])


def test_two_versions_of_one_project_do_not_overwrite_each_other():
    # The reader keys rows by the string that was sent: keying by the project name let an
    # unreadable version overwrite the readable one inside one batch.
    table = {"requests": payload(version="2.31.0")}
    with client_answering(table) as client:
        good, _ = pypi.parse_target("requests==2.30.0")
        bad, _ = pypi.parse_target("requests==9.9.9")
        rows, refused, missing = pypi.read_packages(client, [good, bad])
    return (list(rows) == ["requests==2.30.0"] and rows["requests==2.30.0"]["version"] == "2.30.0"
            and list(missing) == ["requests==9.9.9"])


def test_user_agent_names_the_actor():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, json=payload())

    target, _ = pypi.parse_target("requests")
    with httpx.Client(transport=httpx.MockTransport(handler),
                      headers={"User-Agent": pypi.USER_AGENT}) as client:
        pypi.read_packages(client, [target])
    return seen["ua"].startswith("apify-pypi-package-api")


def test_server_error_is_refused_by_the_text_that_was_sent():
    # Keyed by the string the buyer sent, with its version: a refused line must come back
    # as "not read", never as "no such package".
    rows, refused, missing = read_one("requests==2.31.0", {"requests": 500})
    return rows == {} and "requests==2.31.0" in refused and missing == {}


def test_broken_json_is_refused():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")
    target, _ = pypi.parse_target("requests")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, refused, missing = pypi.read_packages(client, [target])
    return rows == {} and "requests" in refused and missing == {}


def test_dependencies_can_be_left_out():
    def handler(request):
        return httpx.Response(200, json=payload())
    target, _ = pypi.parse_target("requests")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, _, _ = pypi.read_packages(client, [target], with_dependencies=False)
    row = rows["requests"]
    return row["dependencies"] is None and row["dependenciesCount"] == 2


def test_pinned_version_wins_over_latest():
    # The pinned version exists (2.30.0 is in releases), so it wins over the latest 2.31.0,
    # and the row is keyed by the request, not by the project name.
    table = {"requests": payload(version="2.31.0")}
    def handler(request):
        return httpx.Response(200, json=table["requests"])
    target, _ = pypi.parse_target("requests==2.30.0")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows, _, _ = pypi.read_packages(client, [target])
    row = rows["requests==2.30.0"]
    return row["version"] == "2.30.0" and row["filesInRelease"] == 1 and row["releaseSizeBytes"] == 900


def test_classifiers_are_kept():
    rows, _, _ = read_one("requests", {"requests": payload()})
    return rows["requests"]["classifiers"] and len(rows["requests"]["classifiers"]) == 2


def test_source_is_named():
    rows, _, _ = read_one("requests", {"requests": payload()})
    return rows["requests"]["source"].startswith("PyPI")


# ------------------------------------------------------------------ runner

CHECKS = [
    test_bare_name, test_pinned_version, test_case_folds, test_separators_fold,
    test_project_link, test_project_link_with_version, test_foreign_host_is_invalid,
    test_junk_is_invalid, test_empty_line_is_invalid, test_too_long_is_invalid,
    test_not_a_string_is_invalid, test_strips_whitespace,
    test_row_has_the_metadata, test_missing_name_is_a_verdict_not_a_failure,
    test_no_such_release_is_missing_not_a_row,
    test_two_versions_of_one_project_do_not_overwrite_each_other,
    test_user_agent_names_the_actor,
    test_server_error_is_refused_by_the_text_that_was_sent, test_broken_json_is_refused,
    test_dependencies_can_be_left_out, test_pinned_version_wins_over_latest,
    test_classifiers_are_kept, test_source_is_named,
]


def run_all():
    failed = []
    for check in CHECKS:
        try:
            if not check():
                failed.append((check.__name__, "returned False"))
        except Exception as exc:  # noqa: BLE001
            failed.append((check.__name__, f"{type(exc).__name__}: {exc}"))
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
    for name, why in failed:
        print(f"FAIL {name}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_all())
