"""Guards for the SHA-1 intermediate fallback and IP SAN matching.

A live chain whose INTERMEDIATE is signed with SHA-1 must be accepted
(verdict ok, weakSignature=["SHA1"]), never invalid-today. An IP host
must be matched against the IP SAN.

Fixtures live in tests/fixtures/ssl and were generated once with the
openssl CLI (cryptography refuses to CREATE SHA-1 signatures, pyOpenSSL
26 removed the certificate builder): root (SHA-256 self-signed),
intermediate signed with -sha1, leaf with a DNS SAN, an IP-SAN leaf,
and a tampered leaf whose signature byte was flipped (parses, fails).
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography import x509

from src import tlscheck

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ssl")


def _load(name):
    path = os.path.join(FX, name)
    if name.endswith(".der"):
        return x509.load_der_x509_certificate(open(path, "rb").read())
    return x509.load_pem_x509_certificate(open(path, "rb").read())


def _chain():
    leaf = _load("leaf.pem")
    intermediate = _load("int.pem")
    root = _load("root.pem")
    return leaf, intermediate, root


def test_sha1_intermediate_is_not_invalid():
    leaf, intermediate, ca = _chain()
    # the policy verifier refuses SHA-1, our fallback must accept
    assert tlscheck._verify(leaf, [intermediate], [ca], "sha1chain.test")
    assert tlscheck._path_ok(leaf, [intermediate], [ca])
    assert tlscheck._hostname_ok("sha1chain.test", leaf)
    assert tlscheck._weak_sha1([leaf, intermediate, ca]) == ["SHA1"]


def test_sha1_chain_wrong_hostname_rejected():
    leaf, intermediate, ca = _chain()
    try:
        tlscheck._verify(leaf, [intermediate], [ca], "other.test")
        raised = False
    except Exception:
        raised = True
    assert raised, "wrong hostname must fail even on a SHA-1 chain"


def test_sha1_fallback_does_not_miss_broken_path():
    # tampered leaf: parses fine, signature is broken
    leaf_bad = _load("leaf_tampered.der")
    _, intermediate, root = _chain()
    assert not tlscheck._path_ok(leaf_bad, [intermediate], [root])
    try:
        tlscheck._verify(leaf_bad, [intermediate], [root], "sha1chain.test")
        raised = False
    except Exception:
        raised = True
    assert raised, "broken path must not pass the fallback"


def test_ip_san_matching():
    leaf, ca = _load("leaf_ip.pem"), _load("root.pem")
    # IP host matches the IP SAN through the normal policy verifier
    assert tlscheck._verify(leaf, [], [ca], "1.2.3.4")
    assert tlscheck._hostname_ok("1.2.3.4", leaf)
    # a DNS host must not match an IP-only SAN
    try:
        tlscheck._verify(leaf, [], [ca], "example.test")
        raised = False
    except Exception:
        raised = True
    assert raised


def test_subject_alt_kind():
    assert type(tlscheck._subject_alt("1.1.1.1")).__name__ == "IPAddress"
    assert type(tlscheck._subject_alt("example.com")).__name__ == "DNSName"


def test_wildcard_hostname():
    from cryptography.x509 import DNSName
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    now = datetime.datetime.now(datetime.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "*.example.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=90))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("*.example.com")]),
            critical=False)
        .sign(key, hashes.SHA256())
    )
    assert tlscheck._hostname_ok("a.example.com", cert)
    assert not tlscheck._hostname_ok("a.b.example.com", cert)
    assert not tlscheck._hostname_ok("example.com", cert)


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
