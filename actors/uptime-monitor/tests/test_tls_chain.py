"""Guards for the TLS error chain walker (B3): an expired certificate must be
a tls_error, never a generic connection_error."""
import os
import ssl as ssl_module
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.checker import _is_tls_error


def _chain(inner):
    import httpcore
    outer = httpcore.ConnectError("handshake failed")
    outer.__cause__ = inner
    return outer


def test_sslcertverification_error_detected_through_httpcore():
    inner = ssl_module.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    assert _is_tls_error(_chain(inner)) is True


def test_plain_oserror_is_not_tls():
    assert _is_tls_error(_chain(OSError("connection refused"))) is False


def test_direct_ssl_error_detected():
    assert _is_tls_error(ssl_module.SSLError("bad handshake")) is True


def test_ssl_text_in_args_detected():
    import httpcore
    exc = httpcore.ConnectError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
    assert _is_tls_error(exc) is True


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
