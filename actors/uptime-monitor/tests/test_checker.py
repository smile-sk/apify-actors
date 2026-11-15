"""Tests for the probe engine parts that do not need the platform."""
import os
import socket
import ssl as ssl_module
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.checker import host_classify, host_is_safe, ssl_days_remaining


def test_loopback_is_private():
    assert host_classify("http://127.0.0.1") == "private"


def test_private_rfc1918_is_private():
    assert host_classify("http://192.168.1.1") == "private"


def test_link_local_is_private():
    assert host_classify("http://169.254.169.254") == "private"


def test_localhost_resolves_private():
    assert host_classify("http://localhost") == "private"


def test_unresolvable_host_is_unresolvable_not_private():
    assert host_classify("http://no-such-host-uptime-test.invalid") == "unresolvable"


def test_public_host_is_ok():
    assert host_classify("https://example.com") == "ok"


def test_host_is_safe_wrapper():
    assert host_is_safe("https://example.com") is True
    assert host_is_safe("http://127.0.0.1") is False
    assert host_is_safe("http://no-such-host-uptime-test.invalid") is False


def test_ssl_days_none_on_bad_host():
    assert ssl_days_remaining("no-such-host-uptime-test.invalid", 443, 3.0) is None


def test_ssl_days_none_on_closed_port():
    assert ssl_days_remaining("127.0.0.1", 1, 2.0) is None


def _self_signed_cert(days: int):
    """Return (cert_path, key_path) for a short-lived self-signed localhost cert."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=days))
            .sign(key, hashes.SHA256()))
    tmp = tempfile.mkdtemp(prefix="uptime_ssl_test_")
    cert_path = os.path.join(tmp, "cert.pem")
    key_path = os.path.join(tmp, "key.pem")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
    return cert_path, key_path


def test_ssl_days_happy_path():
    """Guard for the 0.1.8 regression: sslDaysToExpiry was null for every
    HTTPS host because CERT_NONE made getpeercert() return an empty dict."""
    cert_path, key_path = _self_signed_cert(days=10)
    ctx = ssl_module.SSLContext(ssl_module.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            conn, _ = srv.accept()
            with conn:
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    try:
                        tls.recv(1)
                    except (ConnectionError, OSError):
                        pass  # the probe closed the socket after reading the cert
        except Exception:
            pass
        finally:
            try:
                srv.close()
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    days = ssl_days_remaining("127.0.0.1", port, 5.0)
    thread.join(timeout=5)
    assert days is not None, "expected a real number of days, got None"
    assert abs(days - 10) <= 1, f"expected ~10 days, got {days}"


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
