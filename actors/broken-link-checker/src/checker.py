"""Probe engine for the broken link checker.

The core promise: a link is marked broken only after retries. One failed
request must not turn a working link into a broken one - that is the exact
complaint the niche leader carries (its 2-star review says: a lot of links
scanned as broken are actually still working).

Retry policy: network errors, timeouts, DNS failures, 429 and 5xx responses
are retried up to `retries` times with a short backoff and respect the
Retry-After header. A 4xx answer is a real answer and is not retried.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl as ssl_module
import time

import httpx

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

_MAX_HOPS = 30
_RETRY_BACKOFF_S = [0.5, 1.0, 2.0, 4.0, 8.0]


def _is_tls_error(exc: BaseException) -> bool:
    """Walk the exception chain and detect a TLS failure.

    In httpx 0.28.1 a failed certificate check surfaces as httpx.ConnectError
    whose __cause__ is httpcore.ConnectError, while the SSLCertVerificationError
    sits inside its args. isinstance against ssl.SSLError on the first cause
    never fires, and an expired certificate was misreported as connection_error.
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


def _is_dns_error(exc: BaseException) -> bool:
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, socket.gaierror):
            return True
        for attr in ("__cause__", "__context__"):
            nxt = getattr(cur, attr, None)
            if isinstance(nxt, BaseException):
                stack.append(nxt)
        for arg in getattr(cur, "args", ()) or ():
            if isinstance(arg, BaseException):
                stack.append(arg)
    return False


async def host_classify(url: str) -> str:
    """Classify the target host of a URL.

    Returns:
    - "ok": every resolved address is public;
    - "private": at least one resolved address is private/loopback/link-local;
    - "unresolvable": DNS does not resolve the host (this is NOT private);
    - "invalid": the URL has no usable host.

    DNS runs on the event loop, so concurrent checks never serialize on a
    blocking resolver call.
    """
    try:
        host = urlparse_host(url)
    except Exception:
        return "invalid"
    if host is None:
        return "invalid"
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
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


def urlparse_host(url: str):
    """Host of a normalized URL; None when there is none."""
    from urllib.parse import urlparse as _urlparse
    try:
        return _urlparse(url).hostname
    except Exception:
        return None


def _classify_exc(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if _is_tls_error(exc):
        return "tls_error"
    if _is_dns_error(exc):
        return "dns_error"
    return "network_error"


def _retry_after_seconds(headers) -> float:
    raw = (headers or {}).get("retry-after")
    if not raw:
        return 0.0
    try:
        return min(float(raw), 10.0)
    except ValueError:
        return 0.0


async def _one_attempt(client, url: str, timeout: float, max_redirects: int):
    """One probe with redirects followed by hand so every hop is checked
    against private ranges. Returns (status, final_url, latency_ms, error,
    attempts_detail, retry_after_seconds)."""
    detail = []
    current = url
    hop = 0
    while True:
        t0 = time.perf_counter()
        try:
            resp = await client.get(current, timeout=timeout, follow_redirects=False)
            ms = int((time.perf_counter() - t0) * 1000)
            detail.append({"hop": hop + 1, "status": resp.status_code,
                           "latency_ms": ms, "error": None})
            retry_after = _retry_after_seconds(resp.headers)
            if resp.status_code in (301, 302, 303, 307, 308) and max_redirects > 0:
                if hop >= max_redirects:
                    return None, str(resp.url), None, "redirect_loop", detail, 0.0
                loc = resp.headers.get("location")
                if not loc:
                    return resp.status_code, str(resp.url), ms, None, detail, retry_after
                nxt = str(resp.url.join(loc))
                if await host_classify(nxt) in ("private", "invalid"):
                    return None, str(resp.url), None, "redirect_to_private_blocked", detail, 0.0
                current = nxt
                hop += 1
                continue
            return resp.status_code, str(resp.url), ms, None, detail, retry_after
        except httpx.TimeoutException:
            detail.append({"hop": hop + 1, "status": None, "latency_ms": None, "error": "timeout"})
            return None, current, None, "timeout", detail, 0.0
        except httpx.HTTPError as exc:
            kind = _classify_exc(exc)
            detail.append({"hop": hop + 1, "status": None, "latency_ms": None, "error": kind})
            return None, current, None, kind, detail, 0.0


def _should_retry(error: str, status: int) -> bool:
    if error in ("network_error", "timeout", "dns_error"):
        return True
    if status is not None and (status == 429 or status >= 500):
        return True
    return False


def verdict_from_attempt(attempt, expected, slow_threshold_ms, retries_used, url, source):
    """Pure builder from the final attempt - the same code the live run uses,
    so the test suite covers the real verdict logic."""
    status = attempt["status"]
    error = attempt["error"]
    lat = attempt["latency_ms"]
    final_url = attempt["final_url"]
    redirect_count = attempt["redirect_count"]
    healthy = status is not None and status in expected
    if error == "redirect_to_private_blocked":
        broken = True
        error_type = "private_target"
        message = "redirect chain leads to a private address and was blocked"
    elif error:
        broken = True
        error_type = error
        message = {
            "timeout": "no response within the timeout after retries",
            "tls_error": "TLS handshake or certificate verification failed",
            "dns_error": "domain does not resolve",
            "network_error": "connection failed after retries",
            "redirect_loop": "too many redirects",
        }.get(error, error)
    else:
        broken = not healthy
        error_type = None if healthy else "http_error"
        message = None if healthy else f"final status {status} is not in the expected list"
    slow = lat is not None and slow_threshold_ms is not None and lat > slow_threshold_ms
    return {
        "url": url,
        "statusCode": status,
        "broken": broken,
        "errorType": error_type,
        "error": message,
        "finalUrl": final_url,
        "redirectCount": redirect_count,
        "responseTimeMs": lat,
        "slow": slow,
        "retriesUsed": retries_used,
        "expectedStatusCodes": expected,
        "source": source,
        "fromCache": False,
    }


async def check_url(client, url: str, *, timeout: float, max_redirects: int,
                    retries: int, expected, slow_threshold_ms, source) -> dict:
    """Check one URL with retries. Returns the verdict row (without billing
    fields). The pause before a retry is the larger of the fixed backoff and
    the Retry-After header the response carried."""
    attempts_left = max(0, min(retries, 5))
    attempt_no = 0
    last = None
    while True:
        status, final_url, lat, error, detail, retry_after = await _one_attempt(
            client, url, timeout, max_redirects)
        redirect_count = max(0, len([d for d in detail if d.get("status") in (301, 302, 303, 307, 308)]))
        last = {"status": status, "final_url": final_url, "latency_ms": lat,
                "error": error, "redirect_count": redirect_count}
        if not _should_retry(error, status) or attempts_left <= 0:
            break
        attempts_left -= 1
        attempt_no += 1
        backoff = _RETRY_BACKOFF_S[min(attempt_no - 1, len(_RETRY_BACKOFF_S) - 1)]
        await asyncio.sleep(max(backoff, retry_after))
    return verdict_from_attempt(last, expected, slow_threshold_ms, attempt_no, url, source)
