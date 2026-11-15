"""Probe engine for the uptime monitor.

Two observer kinds:
- apify: direct probes from the Apify runtime, with one automatic retry on
  transport errors, timeouts and 5xx responses;
- eu-gate: an optional second vantage point (our EU gate) that reports whether
  the target answers at all. Enabled via env UPTIME_GATE_URL and
  UPTIME_GATE_AUTH.
"""
from __future__ import annotations

import base64
import ipaddress
import socket
import ssl as ssl_module
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from cryptography import x509

_PRIVATE = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

UA = "uptime-monitor-api/0.1 (Apify Actor)"
_MAX_HOPS = 10


def _is_tls_error(exc: BaseException) -> bool:
    """Walk the exception chain and detect a TLS failure.

    B3 guard: in httpx 0.28.1 a failed certificate check surfaces as
    httpx.ConnectError whose __cause__ is httpcore.ConnectError, while the
    SSLCertVerificationError sits inside its args. isinstance against
    ssl.SSLError on the first cause never fires, and an expired certificate
    was misreported as connection_error.
    """
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, ssl_module.SSLError):
            return True
        if "SSL" in type(cur).__name__:
            return True
        for attr in ("__cause__", "__context__"):
            nxt = getattr(cur, attr, None)
            if isinstance(nxt, BaseException):
                stack.append(nxt)
        for arg in getattr(cur, "args", ()) or ():
            if isinstance(arg, BaseException):
                stack.append(arg)
            elif isinstance(arg, str) and (
                    "CERTIFICATE_VERIFY_FAILED" in arg or "SSL:" in arg
                    or "ssl3" in arg.lower()):
                return True
    return False


def host_classify(url: str) -> str:
    """Classify the target host of a URL.

    Returns:
    - "ok": every resolved address is public;
    - "private": at least one resolved address is private/loopback/link-local;
    - "unresolvable": DNS does not resolve the host (this is NOT private);
    - "invalid": the URL has no usable host.
    """
    try:
        host = urlparse(url).hostname
    except Exception:
        return "invalid"
    if host is None:
        return "invalid"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "unresolvable"
    if not infos:
        return "unresolvable"
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return "invalid"
        if any(addr in net for net in _PRIVATE):
            return "private"
    return "ok"


def host_is_safe(url: str) -> bool:
    """True when the host resolves and every address is public."""
    return host_classify(url) == "ok"


async def probe_apify(client, url: str, timeout: float):
    """Follow redirects by hand so every hop is checked against private ranges.

    Returns (attempts, status, final_url, error). TLS handshake failures are
    reported as error "tls_error" instead of a generic ConnectError: the
    buyer of the SSL columns must be able to tell a broken certificate from
    a broken network.
    """
    attempts = []
    current = url
    for hop in range(_MAX_HOPS):
        t0 = time.perf_counter()
        try:
            resp = await client.get(current, timeout=timeout, follow_redirects=False)
            ms = int((time.perf_counter() - t0) * 1000)
            attempts.append({"location": "apify", "attempt": hop + 1, "status_code": resp.status_code,
                             "latency_ms": ms, "error": None})
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    return attempts, resp.status_code, str(resp.url), None
                nxt = str(resp.url.join(loc))
                if host_classify(nxt) in ("private", "invalid"):
                    return attempts, None, str(resp.url), "redirect_to_private_blocked"
                current = nxt
                continue
            return attempts, resp.status_code, str(resp.url), None
        except httpx.TimeoutException:
            attempts.append({"location": "apify", "attempt": hop + 1, "status_code": None,
                             "latency_ms": None, "error": "timeout"})
            return attempts, None, current, "timeout"
        except httpx.HTTPError as exc:
            kind = type(exc).__name__
            if kind == "SSLError" or _is_tls_error(exc):
                kind = "tls_error"
            attempts.append({"location": "apify", "attempt": hop + 1, "status_code": None,
                             "latency_ms": None, "error": kind})
            return attempts, None, current, kind
    return attempts, None, current, "too_many_redirects"


def ssl_days_remaining(host: str, port: int, timeout: float):
    """Days until the certificate expires; None when it cannot be read.

    The certificate is read WITHOUT trust validation on purpose: an expired or
    not-yet-valid certificate still yields its notAfter date, which is the
    diagnosis value here. Trust itself is judged by the HTTPS probe.

    The date comes from the raw DER certificate: with CERT_NONE the dict
    getpeercert() returns is empty, so the old notAfter lookup was always
    None (regression 0.1.8).
    """
    try:
        ctx = ssl_module.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl_module.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
        cert = x509.load_der_x509_certificate(der)
        nb = getattr(cert, "not_valid_after_utc", None)
        if nb is None:
            nb = cert.not_valid_after.replace(tzinfo=timezone.utc)
        return (nb - datetime.now(timezone.utc)).days
    except Exception:
        return None


async def probe_gate(client, target_url: str, gate_url: str, gate_auth: str, timeout: float):
    """Ask the EU gate whether the target answers. Returns None when not configured."""
    if not gate_url or not gate_auth:
        return None
    headers = {"Authorization": "Basic " + base64.b64encode(gate_auth.encode()).decode()}
    try:
        resp = await client.get(gate_url, params={"url": target_url}, headers=headers, timeout=timeout)
        data = resp.json()
        return {"location": "eu-gate", "alive": bool(data.get("ok")), "http_code": data.get("http_code"),
                "latency_ms": data.get("total_ms"), "error": data.get("error")}
    except Exception as exc:
        return {"location": "eu-gate", "alive": None, "http_code": None, "latency_ms": None,
                "error": "gate_unreachable: " + type(exc).__name__}
