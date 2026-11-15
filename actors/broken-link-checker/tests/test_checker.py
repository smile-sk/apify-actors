import os
import ssl as ssl_module
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import checker


def _chain_naturally(outer_exc, inner_exc):
    """Build a real __cause__ chain through natural exception handling."""
    try:
        try:
            raise inner_exc
        except Exception as exc:
            raise outer_exc from exc
    except Exception as top:
        return top


def test_tls_error_in_cause_of_connecterror():
    # httpx 0.28.1 real shape: ConnectError(cause=ConnectError(cause=SSLCertVerificationError))
    leaf = ssl_module.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")
    inner = OSError("wrap error")
    outer = OSError("connect error")
    chain = _chain_naturally(outer, _chain_naturally(inner, leaf))
    assert checker._is_tls_error(chain) is True


def test_plain_connect_is_not_tls():
    inner = OSError("connect error")
    chain = _chain_naturally(inner, inner)
    assert checker._is_tls_error(chain) is False


def test_dns_error_in_chain():
    inner = OSError("connect error")
    leaf = socket.gaierror("Name or service not known")
    chain = _chain_naturally(inner, leaf)
    assert checker._is_dns_error(chain) is True


def test_classify_timeout():
    import httpx
    assert checker._classify_exc(httpx.TimeoutException("t")) == "timeout"


def test_retry_after_seconds_parsed_and_capped():
    assert checker._retry_after_seconds({"retry-after": "3"}) == 3.0
    assert checker._retry_after_seconds({"retry-after": "99"}) == 10.0
    assert checker._retry_after_seconds({}) == 0.0
    assert checker._retry_after_seconds({"retry-after": "soon"}) == 0.0


def test_one_attempt_returns_retry_after():
    import asyncio as aio

    class Resp:
        status_code = 503
        url = "https://e.com/"

        def __init__(self):
            self.headers = {"retry-after": "4"}

    class FakeClient:
        async def get(self, url, timeout=None, follow_redirects=False):
            return Resp()

    status, final, lat, err, detail, retry_after = aio.run(
        checker._one_attempt(FakeClient(), "https://e.com/", 15.0, 5))
    assert status == 503
    assert err is None
    assert retry_after == 4.0
    assert len(detail) == 1


def test_host_classify_is_async_and_unresolvable():
    import asyncio as aio
    assert aio.iscoroutinefunction(checker.host_classify)
    # 999.999.999.999 is not a valid address: getaddrinfo fails instantly,
    # no network involved
    assert aio.run(checker.host_classify("https://999.999.999.999/")) == "unresolvable"


def test_verdict_healthy_200():
    att = {"status": 200, "error": None, "latency_ms": 120, "final_url": "https://e.com/",
           "redirect_count": 0}
    row = checker.verdict_from_attempt(att, list(range(200, 400)), 2000, 0, "https://e.com/", "list")
    assert row["broken"] is False
    assert row["errorType"] is None
    assert row["slow"] is False


def test_verdict_404_broken():
    att = {"status": 404, "error": None, "latency_ms": 90, "final_url": "https://e.com/x",
           "redirect_count": 0}
    row = checker.verdict_from_attempt(att, list(range(200, 400)), 2000, 0, "https://e.com/x", "list")
    assert row["broken"] is True
    assert row["errorType"] == "http_error"
    assert row["statusCode"] == 404


def test_verdict_expected_404_is_healthy():
    att = {"status": 404, "error": None, "latency_ms": 90, "final_url": "https://e.com/x",
           "redirect_count": 0}
    row = checker.verdict_from_attempt(att, [404], 2000, 0, "https://e.com/x", "list")
    assert row["broken"] is False
    assert row["errorType"] is None


def test_verdict_timeout_broken_with_type():
    att = {"status": None, "error": "timeout", "latency_ms": None, "final_url": "https://e.com/",
           "redirect_count": 0}
    row = checker.verdict_from_attempt(att, list(range(200, 400)), 2000, 3, "https://e.com/", "list")
    assert row["broken"] is True
    assert row["errorType"] == "timeout"
    assert row["retriesUsed"] == 3


def test_verdict_tls_broken():
    att = {"status": None, "error": "tls_error", "latency_ms": None, "final_url": "https://e.com/",
           "redirect_count": 0}
    row = checker.verdict_from_attempt(att, list(range(200, 400)), 2000, 0, "https://e.com/", "list")
    assert row["errorType"] == "tls_error"
    assert row["broken"] is True


def test_verdict_redirect_to_private():
    att = {"status": None, "error": "redirect_to_private_blocked", "latency_ms": None,
           "final_url": "https://e.com/", "redirect_count": 1}
    row = checker.verdict_from_attempt(att, list(range(200, 400)), 2000, 0, "https://e.com/", "list")
    assert row["errorType"] == "private_target"
    assert row["broken"] is True


def test_verdict_slow_flag():
    att = {"status": 200, "error": None, "latency_ms": 3500, "final_url": "https://e.com/",
           "redirect_count": 0}
    row = checker.verdict_from_attempt(att, list(range(200, 400)), 2000, 0, "https://e.com/", "list")
    assert row["slow"] is True
    assert row["broken"] is False


def test_should_retry_rules():
    assert checker._should_retry("network_error", None) is True
    assert checker._should_retry("timeout", None) is True
    assert checker._should_retry("dns_error", None) is True
    assert checker._should_retry(None, 429) is True
    assert checker._should_retry(None, 503) is True
    assert checker._should_retry(None, 404) is False
    assert checker._should_retry(None, 200) is False
    assert checker._should_retry("tls_error", None) is False


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
