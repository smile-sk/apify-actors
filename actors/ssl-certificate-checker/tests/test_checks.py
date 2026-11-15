"""Unit tests for the SSL checker. Every test file must be runnable
directly (run_all + __main__), otherwise a silently failing file exits
with rc=0 and red tests go unnoticed.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import roots2027
from src.parse import HOST_RE, is_private, parse_host


def test_roots_table():
    assert len(roots2027.REMOVED_ROOTS) == 19
    for name, h in roots2027.REMOVED_ROOTS:
        assert len(h) == 64, name
        int(h, 16)  # valid hex
        assert roots2027.removed_name(h.lower()) == name
    assert roots2027.removed_name("0" * 64) is None
    assert roots2027.REMOVAL_DATE == "2027-04-15"


def test_parse_host_forms():
    assert parse_host("example.com") == ("example.com", 443)
    assert parse_host("https://www.example.com/path?x=1") == ("www.example.com", 443)
    assert parse_host("api.example.com:8443") == ("api.example.com", 8443)
    assert parse_host("EXAMPLE.COM.") == ("example.com", 443)
    assert parse_host("  example.com  ") == ("example.com", 443)
    assert parse_host("[::1]:443")[0] is None
    assert parse_host("user@example.com") == ("example.com", 443)
    assert parse_host("example.com/x") == ("example.com", 443)


def test_parse_host_invalid():
    for bad in ["", "javascript:alert(1)", "example.com:abc", "example.com:99999",
                "exa mple.com", "http://", "example.com:443:80", "example..com",
                "localhost", "-bad.com"]:
        host, port = parse_host(bad)
        assert host is None, bad
    assert parse_host("localhost") == (None, None)


def test_private_addresses():
    assert is_private("127.0.0.1")
    assert is_private("192.168.1.1")
    assert is_private("10.0.0.5")
    assert is_private("172.16.0.2")
    assert is_private("169.254.10.10")
    assert is_private("100.64.0.1")
    assert is_private("::ffff:100.64.0.1")
    assert is_private("0.0.0.0")
    assert is_private("localtest.me")
    assert not is_private("example.com")


def test_host_regex():
    assert HOST_RE.match("example.com")
    assert HOST_RE.match("a-b-c.example.co.uk")
    assert not HOST_RE.match("example")
    assert not HOST_RE.match("exa_mple.com")


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except Exception as e:
            failed += 1
            print("FAIL", t.__name__, "->", repr(e))
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
