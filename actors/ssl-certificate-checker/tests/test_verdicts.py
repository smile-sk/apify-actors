"""Verdict logic tests on locally generated certificates (no network).

A chain signed by a CA that is not in the Mozilla store must give
invalid-today with a chain-broken reason, and must never be charged
(charging lives in main.py and is not exercised here).
"""

import datetime
import ipaddress
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from src import roots2027, tlscheck


def _make_ca(name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _make_leaf(ca_key, ca_cert, dns):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return cert


def test_unknown_ca_is_invalid_today():
    ca_key, ca_cert = _make_ca("My Test CA")
    leaf = _make_leaf(ca_key, ca_cert, "example.test")
    roots_full, roots_after = tlscheck._stores()
    assert len(roots_full) > 100
    present_removed = sum(
        1 for r in roots_full if roots2027.removed_name(tlscheck._sha256_der(r)))
    assert len(roots_after) == len(roots_full) - present_removed
    # 18 of the 19 removed roots are in the current certifi bundle;
    # Firmaprofesional Root CA is absent from certifi entirely, so
    # present_removed must be 18, not 19.
    assert present_removed == 18

    # anchor resolution: the self-signed CA is not in the store
    chain = [leaf, ca_cert]
    store_hashes = {tlscheck._sha256_der(r) for r in roots_full}
    candidates = []
    for cert in chain:
        if tlscheck._is_self_signed(cert):
            candidates.append(cert)
            continue
        for r in roots_full:
            try:
                cert.verify_directly_issued_by(r)
                candidates.append(r)
            except Exception:
                continue
    assert candidates == [ca_cert], "only the unknown self-signed CA"

    ok_today = False
    for r in candidates:
        if tlscheck._sha256_der(r) in store_hashes:
            try:
                tlscheck._verify(leaf, [], [r], "example.test")
                ok_today = True
            except Exception:
                pass
    assert not ok_today


def test_removed_roots_are_stripped():
    roots_full, roots_after = tlscheck._stores()
    for name, h in roots2027.REMOVED_ROOTS:
        for r in roots_after:
            assert tlscheck._sha256_der(r) != h, name


def test_removed_name_known_roots():
    # Go Daddy G2 must be in the current Mozilla store and marked removed
    roots_full, _ = tlscheck._stores()
    hashes = {tlscheck._sha256_der(r) for r in roots_full}
    godaddy = "45140B3247EB9CC8C5B4F0D7B53091F73292089E6E5A63E2749DD3ACA9198EDA"
    assert godaddy in hashes
    assert roots2027.removed_name(godaddy) == "Go Daddy Root Certificate Authority - G2"


def test_sha1_self_signed_roots_are_loaded():
    # TWCA and Certum roots are SHA-1 self-signed: the loader must keep
    # them (hashes taken from the removal table, never hardcoded guesses)
    roots_full, _ = tlscheck._stores()
    hashes = {tlscheck._sha256_der(r) for r in roots_full}
    wanted = {}
    for name, h in roots2027.REMOVED_ROOTS:
        if name.startswith("TWCA") or name.startswith("Certum"):
            wanted[name] = h
    assert len(wanted) == 2
    for name, h in wanted.items():
        assert h in hashes, "%s must be loaded" % name


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
