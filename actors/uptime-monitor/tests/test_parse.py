"""Tests for URL normalization and input parsing."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.parse import normalize_url, parse_rows


def test_bare_domain_gets_https():
    assert normalize_url("example.com") == "https://example.com/"


def test_trailing_slash_normalized():
    assert normalize_url("https://EXAMPLE.com/path/") == "https://example.com/path"


def test_host_lowercased():
    assert normalize_url("HTTPS://Apify.com") == "https://apify.com/"


def test_javascript_rejected():
    assert normalize_url("javascript:alert(1)") == ""


def test_no_host_rejected():
    assert normalize_url("hello world") == ""


def test_ftp_rejected():
    assert normalize_url("ftp://example.com/file") == ""


def test_query_string_is_kept():
    assert normalize_url("https://e.com/a?token=1") == "https://e.com/a?token=1"


def test_query_strings_are_different_targets():
    rows = parse_rows(["https://e.com/a?token=1", "https://e.com/a?token=2"])
    assert len(rows) == 2
    assert rows[0]["norm"] == "https://e.com/a?token=1"
    assert rows[1]["norm"] == "https://e.com/a?token=2"


def test_path_query_and_fragment():
    assert normalize_url("http://x.io/heartbeat?deep=1") == "http://x.io/heartbeat?deep=1"


def test_ipv6_literal_allowed():
    assert normalize_url("https://[2606:4700:4700::1111]/") == "https://[2606:4700:4700::1111]/"


def test_bad_ipv6_rejected():
    assert normalize_url("https://[zzzz::1]/") == ""


def test_idn_host_encoded():
    assert normalize_url("https://münchen.de/") == "https://xn--mnchen-3ya.de/"


def test_duplicates_kept_once():
    rows = parse_rows(["example.com", "https://example.com/", "https://example.com/path"])
    assert len(rows) == 2
    assert rows[0]["norm"] == "https://example.com/"
    assert rows[1]["norm"] == "https://example.com/path"


def test_invalid_row_kind():
    rows = parse_rows(["javascript:alert(1)"])
    assert rows[0]["kind"] == "invalid"
    assert rows[0]["raw"] == "javascript:alert(1)"


def test_empty_input():
    assert parse_rows([]) == []


def test_port_kept():
    assert normalize_url("http://example.com:8080/x") == "http://example.com:8080/x"


def test_userinfo_url_rejected():
    assert normalize_url("https://user:pass@example.com/") == ""

def test_userinfo_url_is_invalid_row():
    rows = parse_rows(["https://user:pass@example.com/"])
    assert rows[0]["kind"] == "invalid"


def run_all():
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS", name)
        except Exception as e:
            failed += 1
            print("FAIL", name, "->", type(e).__name__, str(e)[:200])
    print(f"{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
