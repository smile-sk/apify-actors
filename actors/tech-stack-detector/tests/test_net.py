"""Url parsing and the address guard, checked without a network."""
from __future__ import annotations

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.net import host_is_public, host_of, normalize_url  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def resolver_returning(*addresses):
    def fake(host, port, *a, **k):
        return [(socket.AF_INET6 if ":" in addr else socket.AF_INET, 0, 0, "", (addr, 0))
                for addr in addresses]
    return fake


def test_normalize():
    equal(normalize_url("example.com"), ("https://example.com/", None), "bare host gets https")
    equal(normalize_url("  https://Example.COM/Path  ")[0], "https://example.com/Path",
          "host is lowercased, path kept")
    equal(normalize_url("http://example.com:80/x")[0], "http://example.com/x",
          "default http port dropped")
    equal(normalize_url("https://example.com:8443/x")[0], "https://example.com:8443/x",
          "custom port kept")
    equal(normalize_url("https://example.com/x#frag")[0], "https://example.com/x",
          "fragment dropped")
    ok(normalize_url("")[0] is None, "empty line is not a url")
    ok(normalize_url("   ")[0] is None, "blank line is not a url")
    ok(normalize_url("ftp://example.com/x")[0] is None, "ftp is refused")
    ok(normalize_url("javascript:alert(1)")[0] is None, "javascript: is refused")
    ok(normalize_url("https://user:pass@example.com/")[0] is None, "credentials are refused")
    ok(normalize_url("https://example.com:notaport/")[0] is None, "a malformed port is refused")
    ok(normalize_url("https://" + "a" * 3000 + ".com/")[0] is None, "a huge url is refused")
    equal(normalize_url("https://example.com")[0], "https://example.com/", "path defaults to /")


def test_public_addresses():
    ok(host_is_public("example.com", resolver_returning("93.184.216.34"))[0],
       "a public v4 address passes")
    ok(host_is_public("example.com", resolver_returning("2606:2800:220:1::1"))[0],
       "a public v6 address passes")
    ok(host_is_public("example.com", resolver_returning("93.184.216.34", "2606:2800::1"))[0],
       "both families checked, both public")
    for bad in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.169.254",
                "::1", "fd00::1", "fe80::1"):
        ok(not host_is_public("x", resolver_returning(bad))[0], f"{bad} is refused")
    # CGNAT is the range a flag-by-flag predicate misses and Kubernetes uses.
    ok(not host_is_public("x", resolver_returning("100.64.0.1"))[0], "CGNAT is refused")
    ok(not host_is_public("x", resolver_returning("::ffff:127.0.0.1"))[0],
       "an IPv4-mapped loopback is refused")
    ok(not host_is_public("x", resolver_returning("93.184.216.34", "10.0.0.5"))[0],
       "one private address among public ones is enough to refuse")
    ok(host_is_public("93.184.216.34", resolver_returning())[0],
       "a public literal needs no resolver")
    ok(not host_is_public("127.0.0.1", resolver_returning())[0], "a private literal is refused")


def test_resolver_errors():
    def gaierror(host, port, *a, **k):
        raise socket.gaierror(-2, "Name or service not known")

    def unicode_error(host, port, *a, **k):
        raise UnicodeError("label empty or too long")

    ok(not host_is_public("no-such-host.invalid", gaierror)[0], "a name that does not resolve is refused")
    ok(not host_is_public("a" * 64 + ".com", unicode_error)[0],
       "a label that is too long is refused, not raised")


def test_hosts():
    equal(host_of("https://Example.com/x"), "example.com", "host is lowercased")
    equal(host_of("not a url"), "", "a broken url gives no host")
    equal(host_of("https://[::1]/x"), "::1", "an ipv6 literal is unwrapped")


def run_all() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"test_net: {CHECKS[0]} checks, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL", fail)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run_all())
