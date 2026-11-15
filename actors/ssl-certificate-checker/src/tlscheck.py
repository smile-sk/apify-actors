"""TLS handshake and dual WebPKI validation for the SSL checker.

The chain is collected with pyOpenSSL (the stdlib ssl module on Python
3.12 does not expose the peer chain). Validation runs twice through the
cryptography verifier: against the current Mozilla root store (certifi)
and against the same store without the roots that Mozilla distrusts
for websites on 15 April 2027.

Two deliberate deviations from the default policy:

1. Chains whose INTERMEDIATE is signed with SHA-1. Browsers still accept
   these (the SHA-1 ban applies to end-entity certs), but the cryptography
   verifier rejects them with "Forbidden signature algorithm" and even
   verify_directly_issued_by refuses SHA-1. For such chains we re-validate
   with OpenSSL through pyOpenSSL X509StoreContext and match the hostname
   ourselves; the row then carries weakSignature=["SHA1"] and an honest
   verdict.

2. Root loading. Self-signed SHA-1 roots raise on self-signature check,
   so we load every certifi cert with subject == issuer without checking
   the self-signature. Otherwise TWCA, Certum and ACCVRAIZ1 would drop out
   and valid chains on those anchors would be called invalid-today.
"""

import hashlib
import ipaddress
import select
import socket
import time
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.verification import (
    DNSName, IPAddress, PolicyBuilder, Store,
)
from OpenSSL import SSL
from OpenSSL import crypto as ocrypto

from src import roots2027


def _sha256_der(cert):
    return hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest().upper()


def _load_mozilla_roots():
    """Root certificates from the Mozilla store shipped in certifi.

    Every self-signed cert counts as a root, no self-signature check:
    SHA-1 self-signed roots raise during verification and would silently
    disappear from the store (TWCA, Certum, ACCVRAIZ1).
    """
    import certifi
    with open(certifi.where(), "rb") as f:
        pem = f.read()
    certs = x509.load_pem_x509_certificates(pem)
    roots = []
    seen = set()
    for c in certs:
        if c.subject != c.issuer:
            continue
        h = _sha256_der(c)
        if h in seen:
            continue
        seen.add(h)
        roots.append(c)
    return roots


def _strip_removed(roots):
    kept = []
    for c in roots:
        if roots2027.removed_name(_sha256_der(c)) is None:
            kept.append(c)
    return kept


_ROOTS_FULL = None
_ROOTS_AFTER = None


def _stores():
    global _ROOTS_FULL, _ROOTS_AFTER
    if _ROOTS_FULL is None:
        roots = _load_mozilla_roots()
        _ROOTS_FULL = roots
        _ROOTS_AFTER = _strip_removed(roots)
    return _ROOTS_FULL, _ROOTS_AFTER


def _subject_alt(host):
    """DNSName for names, IPAddress for IP literals (1.1.1.1 must match
    the IP SAN, not the DNS SAN)."""
    try:
        ip = ipaddress.ip_address(host)
        return IPAddress(ip)
    except ValueError:
        return DNSName(host)


def _path_ok(leaf, intermediates, roots):
    """Chain validation through OpenSSL (pyOpenSSL X509StoreContext).

    OpenSSL accepts SHA-1 intermediate signatures, which cryptography
    refuses even in verify_directly_issued_by. Hostname is NOT checked
    here: the caller matches it separately via _hostname_ok.
    """
    try:
        store = ocrypto.X509Store()
        for r in roots:
            store.add_cert(ocrypto.X509.from_cryptography(r))
        ctx = ocrypto.X509StoreContext(
            store,
            ocrypto.X509.from_cryptography(leaf),
            [ocrypto.X509.from_cryptography(c) for c in intermediates])
        ctx.verify_certificate()
        return True
    except Exception:
        return False


def _hostname_ok(host, leaf):
    """Match the host against the leaf SAN list (DNS and IP)."""
    try:
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except Exception:
        return False
    try:
        ipaddress.ip_address(host)
        ips = [str(v) for v in san.value.get_values_for_type(x509.IPAddress)]
        return host in ips
    except ValueError:
        names = san.value.get_values_for_type(x509.DNSName)
        for n in names:
            if n.lower() == host.lower():
                return True
            if n.startswith("*."):
                suffix = n[1:].lower()
                if host.lower().endswith(suffix) and "." not in host[:-len(suffix)]:
                    return True
        return False


def _weak_sha1(chain):
    """Distinct signature algorithms that count as weak on this chain."""
    algos = set()
    for c in chain:
        try:
            if c.signature_hash_algorithm is not None:
                algos.add(c.signature_hash_algorithm.name.upper())
        except Exception:
            pass
    weak = [a for a in sorted(algos) if "SHA1" in a or a == "SHA-1"]
    return weak


def _verify(leaf, intermediates, roots, hostname):
    policy = PolicyBuilder().store(Store(roots))
    verifier = policy.build_server_verifier(_subject_alt(hostname))
    try:
        verifier.verify(leaf, list(intermediates))
        return True
    except Exception as e:
        # SHA-1 intermediate: browsers accept it, the policy layer does not.
        if "Forbidden signature algorithm" in str(e) and _path_ok(
                leaf, intermediates, roots) and _hostname_ok(hostname, leaf):
            return True
        raise


def _is_self_signed(cert):
    if cert.subject != cert.issuer:
        return False
    try:
        cert.verify_directly_issued_by(cert)
        return True
    except Exception:
        return False


def check_host(host, port=443, timeout=10):
    """Run one TLS check. Returns a dict, never raises."""
    row = {
        "host": host, "port": port, "verdict": "unreachable",
        "validToday": None, "validAfter2027RootRemovals": None,
        "anchorRootName": None, "anchorRootSha256": None,
        "anchorRootRemovedOn": None, "newerRootNotYetTrusted": [],
        "leafIssuer": None, "leafNotBefore": None, "leafNotAfter": None,
        "daysToExpiry": None, "lifetimeDays": None,
        "subjectAltNames": [], "subjectAltIPs": [],
        "weakSignature": [], "tlsVersion": None, "cipherName": None,
        "reason": None, "billable": False, "fromCache": False,
    }
    tls_ver = None
    cipher = None
    try:
        ctx = SSL.Context(SSL.TLS_CLIENT_METHOD)
        ctx.set_verify(SSL.VERIFY_NONE, lambda *a: True)
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.setblocking(False)
        conn = SSL.Connection(ctx, sock)
        conn.set_tlsext_host_name(host.encode("ascii", "ignore"))
        conn.set_connect_state()
        deadline = time.monotonic() + timeout
        while True:
            try:
                conn.do_handshake()
                break
            except SSL.WantReadError:
                select.select([sock], [], [], max(0.0, deadline - time.monotonic()))
            except SSL.WantWriteError:
                select.select([], [sock], [], max(0.0, deadline - time.monotonic()))
            if time.monotonic() > deadline:
                raise socket.timeout("TLS handshake timeout")
        tls_ver = conn.get_protocol_version_name()
        cipher = conn.get_cipher_name()
        peer = conn.get_peer_cert_chain()
        try:
            conn.shutdown()
        except Exception:
            pass
        conn.close()
        if not peer:
            row["reason"] = "TLS handshake succeeded but no certificate chain was sent"
            row["tlsVersion"] = tls_ver
            row["cipherName"] = cipher
            return row
        chain = []
        for c in peer:
            raw = c.to_cryptography()
            if isinstance(raw, bytes):
                chain.append(x509.load_der_x509_certificate(raw))
            else:
                chain.append(raw)
    except socket.timeout:
        row["reason"] = "connection timeout"
        return row
    except (socket.gaierror, OSError) as e:
        row["reason"] = "connection failed: %s" % (str(e)[:200])
        return row
    except SSL.Error as e:
        row["reason"] = "TLS handshake failed: %s" % (str(e)[:200])
        return row
    except Exception as e:
        row["reason"] = "check failed: %s" % (type(e).__name__ + ": " + str(e)[:200])
        return row

    leaf = chain[0]
    intermediates = chain[1:]
    row["leafIssuer"] = leaf.issuer.rfc4514_string()
    row["leafNotBefore"] = leaf.not_valid_before_utc.isoformat()
    row["leafNotAfter"] = leaf.not_valid_after_utc.isoformat()
    row["daysToExpiry"] = (leaf.not_valid_after_utc - datetime.now(timezone.utc)).days
    row["lifetimeDays"] = (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days
    row["tlsVersion"] = tls_ver
    row["cipherName"] = cipher
    row["weakSignature"] = _weak_sha1(chain)
    try:
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        row["subjectAltNames"] = list(san.value.get_values_for_type(x509.DNSName))
        row["subjectAltIPs"] = [str(v) for v in san.value.get_values_for_type(x509.IPAddress)]
    except Exception:
        row["subjectAltNames"] = []
        row["subjectAltIPs"] = []

    roots_full, roots_after = _stores()
    store_hashes = {_sha256_der(r) for r in roots_full}

    # Root candidates: roots that actually sign ANY cert the server sent.
    # Cross-signed intermediates are the norm, so check every link.
    candidates = []
    for cert in chain:
        if _is_self_signed(cert):
            if _sha256_der(cert) not in store_hashes:
                row["newerRootNotYetTrusted"].append(cert.subject.rfc4514_string())
            candidates.append(cert)
            continue
        for r in roots_full:
            try:
                cert.verify_directly_issued_by(r)
                candidates.append(r)
            except Exception:
                continue
    seen = set()
    candidates = [c for c in candidates
                  if not (_sha256_der(c) in seen or seen.add(_sha256_der(c)))]

    ok_today_roots = []
    ok_after_roots = []
    for r in candidates:
        in_store = _sha256_der(r) in store_hashes
        if not in_store:
            continue
        if roots2027.removed_name(_sha256_der(r)):
            continue
        try:
            _verify(leaf, intermediates, [r], host)
            ok_after_roots.append(r)
        except Exception:
            pass
    for r in candidates:
        in_store = _sha256_der(r) in store_hashes
        if not in_store:
            continue
        try:
            _verify(leaf, intermediates, [r], host)
            ok_today_roots.append(r)
        except Exception:
            pass

    ok_today = bool(ok_today_roots)
    ok_after = bool(ok_after_roots)
    anchor = ok_after_roots[0] if ok_after_roots else (
        ok_today_roots[0] if ok_today_roots else None)
    if anchor is not None:
        row["anchorRootName"] = anchor.subject.rfc4514_string()
        row["anchorRootSha256"] = _sha256_der(anchor)
        removed = roots2027.removed_name(row["anchorRootSha256"])
        if removed:
            row["anchorRootRemovedOn"] = roots2027.REMOVAL_DATE

    reason_today = None
    if not ok_today:
        try:
            _verify(leaf, intermediates, roots_full, host)
        except Exception as e:
            reason_today = str(e)[:240]

    row["validToday"] = ok_today
    row["validAfter2027RootRemovals"] = ok_after
    if ok_today and ok_after:
        row["verdict"] = "ok"
        row["reason"] = None
    elif ok_today and not ok_after:
        row["verdict"] = "breaks-2027-04-15"
        if row["anchorRootRemovedOn"]:
            row["reason"] = "chain only reaches %s, removed on %s" % (
                row["anchorRootName"], row["anchorRootRemovedOn"])
        else:
            row["reason"] = "validation fails after the 2027 removals"
    else:
        row["verdict"] = "invalid-today"
        row["reason"] = reason_today or (
            "chain is broken: no trusted root signs the last certificate"
            if not candidates else "validation failed")
    return row
