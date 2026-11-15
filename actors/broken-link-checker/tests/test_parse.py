import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import parse


def test_bare_domain_gets_https():
    assert parse.normalize_url("example.com") == "https://example.com/"


def test_scheme_and_case_normalized():
    assert parse.normalize_url("HTTPS://EXAMPLE.COM/A/") == "https://example.com/A"


def test_query_string_kept():
    assert parse.normalize_url("https://e.com/a?token=1") == "https://e.com/a?token=1"
    assert parse.normalize_url("https://e.com/a?token=2") == "https://e.com/a?token=2"
    assert parse.normalize_url("https://e.com/a?token=1") != parse.normalize_url("https://e.com/a?token=2")


def test_fragment_dropped():
    assert parse.normalize_url("https://e.com/a#top") == "https://e.com/a"


def test_userinfo_rejected():
    assert parse.normalize_url("https://user:pass@e.com/") == ""


def test_bad_scheme_rejected():
    assert parse.normalize_url("javascript:alert(1)") == ""
    assert parse.normalize_url("data:text/plain,hi") == ""
    assert parse.normalize_url("ftp://e.com/") == ""


def test_bad_port_rejected():
    assert parse.normalize_url("https://e.com:99999/") == ""


def test_non_default_port_kept():
    assert parse.normalize_url("https://e.com:8443/a") == "https://e.com:8443/a"
    assert parse.normalize_url("http://e.com:8080/") == "http://e.com:8080/"


def test_ipv6_bracketed():
    assert parse.normalize_url("https://[2606:4700:4700::1111]/x") == "https://[2606:4700:4700::1111]/x"


def test_idn_punycode():
    assert parse.normalize_url("https://m\u00fcnchen.de/") == "https://xn--mnchen-3ya.de/"


def test_localhost_rejected_by_parse_but_flag_comes_from_host_classify():
    # normalize keeps it; main.py classifies it private before any request
    assert parse.normalize_url("http://localhost:8080/") == "http://localhost:8080/"


def test_parse_rows_dedupes_and_marks_invalid():
    rows = parse.parse_rows(["example.com", "https://example.com/", "javascript:x", ""])
    assert len(rows) == 2
    assert rows[0]["kind"] == "ok"
    assert rows[1]["kind"] == "invalid"
    assert rows[1]["raw"] == "javascript:x"


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"PASS {fn.__name__}")
    print(f"{passed}/{len(fns)} passed")


if __name__ == "__main__":
    run_all()
