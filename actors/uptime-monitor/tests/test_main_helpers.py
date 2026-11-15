"""Tests for main.py helpers that do not need the platform or the network."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.main import _cache_key, _ERROR_TYPE, CACHE_VERSION, compute_verdict


def test_cache_key_same_params_same_key():
    assert _cache_key("https://e.com/", [200], 5000) == _cache_key("https://e.com/", [200], 5000)


def test_cache_key_changes_with_expected_codes():
    a = _cache_key("https://e.com/", [200], None)
    b = _cache_key("https://e.com/", [500], None)
    assert a != b


def test_cache_key_changes_with_max_ms():
    a = _cache_key("https://e.com/", [200], None)
    b = _cache_key("https://e.com/", [200], 5000)
    assert a != b


def test_cache_key_order_of_codes_does_not_matter():
    assert _cache_key("https://e.com/", [200, 301], None) == _cache_key("https://e.com/", [301, 200], None)


def test_cache_key_differs_per_url():
    assert _cache_key("https://a.com/", [200], None) != _cache_key("https://b.com/", [200], None)


def test_cache_key_carries_version():
    assert CACHE_VERSION in _cache_key("https://e.com/", [200], None)


def test_error_type_mapping_known_names():
    assert _ERROR_TYPE["timeout"] == "timeout"
    assert _ERROR_TYPE["dns_lookup_failed"] == "dns_error"
    assert _ERROR_TYPE["redirect_to_private_blocked"] == "redirect_blocked"
    assert _ERROR_TYPE["ConnectError"] == "connection_error"


def test_error_type_tls_names():
    assert _ERROR_TYPE["tls_error"] == "tls_error"
    assert _ERROR_TYPE["SSLError"] == "tls_error"


def _gate(code):
    return {"location": "eu-gate", "alive": True, "http_code": code, "latency_ms": 30, "error": None}


def _apify(status, latency=100):
    return {"location": "apify", "attempt": 1, "status_code": status,
            "latency_ms": latency, "error": None}


# B1 guards: a 404/500 seen only by the second observer must be unhealthy.
def test_verdict_gate_only_404_is_unhealthy():
    gate = _gate(404)
    v = compute_verdict(None, None, None, "https://example.com/404", [], None, gate, [gate])
    assert v["up"] is True
    assert v["healthy"] is False
    assert "unexpected_status" in v["healthIssues"]
    assert v["statusCode"] == 404


def test_verdict_gate_only_500_is_unhealthy():
    gate = _gate(500)
    v = compute_verdict(None, None, None, "https://e.com/", [], None, gate, [gate])
    assert v["healthy"] is False
    assert "unexpected_status" in v["healthIssues"]


def test_verdict_gate_only_200_is_healthy():
    gate = _gate(200)
    v = compute_verdict(None, None, None, "https://e.com/", [], None, gate, [gate])
    assert v["up"] is True
    assert v["healthy"] is True
    assert v["httpStatusExpected"] is True


def test_verdict_gate_code_respects_custom_expected():
    gate = _gate(404)
    v = compute_verdict(None, None, None, "https://e.com/", [404], None, gate, [gate])
    assert v["httpStatusExpected"] is True
    assert v["healthy"] is True


def test_verdict_apify_404_without_gate_is_down_not_silent():
    v = compute_verdict(404, "https://e.com/404", None, "https://e.com/404", [], None, None,
                        [_apify(404)])
    assert v["up"] is False
    assert v["healthy"] is False
    assert v["statusCode"] == 404
    assert "unexpected_status" in v["healthIssues"]


def test_verdict_apify_500_without_gate_down():
    v = compute_verdict(500, "https://e.com/", None, "https://e.com/", [], None, None,
                        [_apify(500)])
    assert v["up"] is False
    assert v["healthy"] is False
    assert v["statusCode"] == 500


def test_verdict_apify_200_healthy():
    v = compute_verdict(200, "https://e.com/", None, "https://e.com/", [], None, None,
                        [_apify(200, 412)])
    assert v["up"] is True
    assert v["healthy"] is True
    assert v["statusCode"] == 200
    assert v["httpStatusExpected"] is True
    assert v["responseTimeMs"] == 412


def test_verdict_slow_response_issue():
    v = compute_verdict(200, "https://e.com/", None, "https://e.com/", [], 100, None,
                        [_apify(200, 412)])
    assert "slow_response" in v["healthIssues"]
    assert v["healthy"] is False


def test_verdict_latency_is_accepted_final_not_minimum():
    probes = [_apify(500, 100), _apify(200, 900)]
    v = compute_verdict(200, "https://e.com/", None, "https://e.com/", [], None, None, probes)
    assert v["responseTimeMs"] == 900


def test_verdict_custom_expected_codes():
    v = compute_verdict(301, "https://e.com/", None, "https://e.com/", [301], None, None,
                        [_apify(301, 90)])
    assert v["httpStatusExpected"] is True
    assert "unexpected_status" not in v["healthIssues"]


def test_verdict_tls_error_type():
    v = compute_verdict(None, None, "tls_error", "https://e.com/", [], None, None,
                        [{"location": "apify", "attempt": 1, "status_code": None,
                          "latency_ms": None, "error": "tls_error"}])
    assert v["up"] is False
    assert v["errorType"] == "tls_error"
    assert "tls_error" in v["healthIssues"]



# B4 guards: the runtime's own 4xx/5xx answer must not be overridden
# by a healthy second observer.
def test_verdict_apify_500_not_overridden_by_healthy_gate():
    gate = _gate(200)
    probes = [_apify(500, 40), _apify(500, 45), gate]
    v = compute_verdict(500, "https://e.com/", None, "https://e.com/", [], None, gate, probes)
    assert v["up"] is True
    assert v["statusCode"] == 500
    assert "unexpected_status" in v["healthIssues"]
    assert v["healthy"] is False


def test_verdict_apify_404_not_overridden_by_healthy_gate():
    gate = _gate(200)
    probes = [_apify(404, 20), gate]
    v = compute_verdict(404, "https://e.com/404", None, "https://e.com/404", [], None, gate, probes)
    assert v["statusCode"] == 404
    assert v["healthy"] is False


def test_verdict_apify_timeout_gate_200_still_healthy():
    gate = _gate(200)
    probes = [{"location": "apify", "attempt": 1, "status_code": None,
               "latency_ms": None, "error": "timeout"}, gate]
    v = compute_verdict(None, None, "timeout", "https://e.com/", [], None, gate, probes)
    assert v["up"] is True
    assert v["statusCode"] == 200
    assert v["healthy"] is True


def test_verdict_gate_alive_without_code_is_unhealthy():
    gate = {"location": "eu-gate", "alive": True, "http_code": None,
            "latency_ms": None, "error": None}
    v = compute_verdict(None, None, None, "https://e.com/", [], None, gate, [gate])
    assert v["healthy"] is False
    assert "gate_unverified" in v["healthIssues"]



# B2 guards: expectedStatusCodes outside 200-399 must survive a silent gate.
def test_verdict_404_expected_without_gate_is_healthy():
    v = compute_verdict(404, "https://e.com/404", None, "https://e.com/404", [404], None,
                        None, [_apify(404, 60)])
    assert v["up"] is False
    assert v["statusCode"] == 404
    assert v["httpStatusExpected"] is True
    assert v["healthy"] is True


def test_verdict_404_unexpected_without_gate_is_unhealthy():
    v = compute_verdict(404, "https://e.com/404", None, "https://e.com/404", [], None,
                        None, [_apify(404, 60)])
    assert v["httpStatusExpected"] is False
    assert "unexpected_status" in v["healthIssues"]
    assert v["healthy"] is False


def test_verdict_401_expected_without_gate_is_healthy():
    v = compute_verdict(401, "https://e.com/api", None, "https://e.com/api", [401], None,
                        None, [_apify(401, 55)])
    assert v["httpStatusExpected"] is True
    assert v["healthy"] is True

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
