"""Reader for Substack publication archives.

The archive endpoint is public and keyless: https://<publication>/api/v1/archive
It answers {"posts": [...], "nextCursor": "..."} and accepts sort=new, limit and cursor.
The endpoint takes at most 25 posts per page: limit 26 and above is answered with HTTP 400,
so PAGE_SIZE is 25 and a default of 50 posts is read as two pages.

An address copied from a browser is often not the address the archive lives on. A custom
domain can answer 404 while the same publication answers on <label>.substack.com, and an
unknown subdomain answers with a redirect instead of 404. So "there is no publication" is
said only when both roads answered 404, and a redirect is followed, never sold as a verdict.
A publication host is a name, never an address: an IP literal is refused, because a run must
not be able to reach a box inside our own network.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from urllib.parse import urlsplit

import httpx

ARCHIVE_PATH = "/api/v1/archive"
PAGE_SIZE = 25
MAX_REDIRECT_HOPS = 2
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))+$")


def _is_ip_literal(text: str) -> bool:
    """True for anything that is an address rather than a name, IPv6 brackets included."""
    try:
        ipaddress.ip_address(str(text or "").strip().strip("[]"))
        return True
    except ValueError:
        return False


def _private_target(host: str) -> bool:
    """True when the name resolves to an address we must not connect to.

    A name that does not resolve at all is not private: no connection is ever made and the
    request fails on its own, so it must not be reported as a private address.
    """
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return True
        except ValueError:
            return False
    return False


def as_host(value: str) -> str | None:
    """Turn anything a buyer may send into a publication host, or None."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    text = re.sub(r"^[a-z]+://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    text = text.rstrip(".")
    if not text:
        return None
    if _is_ip_literal(text):
        return None
    if "." not in text:
        text = text + ".substack.com"
    if len(text) > 253 or not HOST_RE.match(text):
        return None
    return text


def as_publication(value: str) -> tuple[str, str]:
    """Return (display name, host): the subdomain when the buyer sent a bare slug."""
    host = as_host(value)
    if host is None:
        return "", ""
    name = host[:-len(".substack.com")] if host.endswith(".substack.com") else host
    return name, host


def _clean(value):
    """Text without the blanks the source sometimes keeps around a title."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _authors(post: dict) -> list:
    bylines = post.get("publishedBylines")
    if not isinstance(bylines, list):
        # The source sends a number here for some posts: that is not a list of people.
        return []
    out = []
    for byline in bylines:
        if isinstance(byline, dict):
            name = byline.get("name")
            if name:
                out.append(str(name))
    return out


def post_row(post: dict, publication: str, include_body: bool, host: str = "") -> dict | None:
    """One paid row, or None when the post has no id to sell."""
    if not isinstance(post, dict):
        return None
    post_id = post.get("id")
    if post_id in (None, ""):
        return None
    slug = post.get("slug")
    url = post.get("canonical_url")
    if not url and slug and host:
        url = "https://%s/p/%s" % (host, slug)
    row = {
        "publication": publication,
        "postId": str(post_id),
        "title": _clean(post.get("title")),
        "subtitle": post.get("subtitle"),
        "url": url,
        "publishedAt": post.get("post_date"),
        "authors": _authors(post),
        "section": post.get("section_name"),
        "type": post.get("type"),
        "audience": post.get("audience"),
        "likes": post.get("reaction_count"),
        "comments": post.get("comment_count"),
        "wordCount": post.get("wordcount"),
        "language": post.get("language"),
        "coverImage": post.get("cover_image"),
        "description": post.get("description"),
        "status": "OK",
        "billable": True,
    }
    if include_body:
        row["previewText"] = post.get("truncated_body_text")
    return row


def _location(answer) -> str:
    headers = getattr(answer, "headers", None) or {}
    try:
        return str(headers.get("location") or "")
    except Exception:  # noqa: BLE001
        return ""


def _safe_target(location: str) -> str | None:
    """A redirect is followed only inside https and only to a public name."""
    text = str(location or "").strip()
    if not text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    split = urlsplit(text)
    if split.scheme and split.scheme.lower() != "https":
        return None
    target = (split.hostname or "").lower().rstrip(".")
    if not target or len(target) > 253 or not HOST_RE.match(target):
        return None
    if _is_ip_literal(target) or _private_target(target):
        return None
    return target


def _subdomain_road(host: str) -> str | None:
    """The address a publication usually lives on when its own site answers 404.

    A "www." in front is dropped first: www.platformer.news lives on platformer.substack.com,
    not on www.substack.com.
    """
    bare = host[4:] if host.startswith("www.") else host
    label = bare.split(".")[0]
    if not label or len(label) > 63:
        return None
    candidate = label + ".substack.com"
    return None if candidate == host else candidate


def _page(client, host: str, limit: int, cursor, timeout):
    """One request. Returns (posts, next_cursor, kind, reason, location)."""
    url = "https://" + host + ARCHIVE_PATH
    params = {"sort": "new", "limit": str(max(1, min(int(limit), PAGE_SIZE)))}
    if cursor:
        params["cursor"] = cursor
    for attempt in (0, 1):
        try:
            if timeout is None:
                answer = client.get(url, params=params)
            else:
                answer = client.get(url, params=params, timeout=timeout)
        except httpx.TimeoutException:
            return [], None, "error", "the publication did not answer in time", ""
        except httpx.HTTPError as exc:
            return [], None, "error", "the publication could not be reached (%s)" % type(exc).__name__, ""
        if answer.status_code != 429 or attempt == 1:
            break
        time.sleep(2.0)
    code = answer.status_code
    if 300 <= code < 400:
        return [], None, "redirect", (
            "the host is not a publication: the archive endpoint answered with a redirect "
            "(HTTP %d)" % code), _location(answer)
    if code == 404:
        return [], None, "not_found", "no publication answers on this host", ""
    if code in (401, 403):
        return [], None, "blocked", "the publication refused the archive request (HTTP %d)" % code, ""
    if code == 429:
        return [], None, "blocked", "the publication asked to slow down (HTTP 429)", ""
    if code != 200:
        return [], None, "error", "the publication answered HTTP %d" % code, ""
    try:
        data = answer.json()
    except Exception:  # noqa: BLE001
        return [], None, "error", "the publication answered with something that is not JSON", ""
    posts = data.get("posts")
    if not isinstance(posts, list):
        return [], None, "error", "the archive answer has no posts list", ""
    return posts, data.get("nextCursor"), "", "", ""


def open_publication(client, host: str, limit: int, timeout=None):
    """Resolve a copied address and read its first page.

    Returns (host_used, posts, next_cursor, kind, reason). A 3xx is followed up to
    MAX_REDIRECT_HOPS while it stays https and public. A 404 on the address the buyer sent
    is tried once as <label>.substack.com, because that is where the archive of a custom
    domain usually lives. NOT_FOUND is said only when both roads answered 404.
    """
    if _private_target(host):
        return host, [], None, "invalid", (
            "the host resolves to an address inside a private network, which is never read")
    current = host
    tried_subdomain = False
    hops = 0
    while True:
        posts, cursor, kind, reason, location = _page(client, current, limit, None, timeout)
        if kind == "redirect":
            target = _safe_target(location)
            hops += 1
            if target is not None and hops <= MAX_REDIRECT_HOPS:
                current = target
                continue
            # A redirect we will not follow (a path only, plain http, an address, or too many
            # hops) means the archive is not served here. That is a road closed, not a verdict
            # about the publication: the substack road still gets its chance below.
            kind, reason = "redirected", (
                "the archive endpoint on %s redirected to %s, which is not followed"
                % (current, location or "nowhere"))
        if kind in ("not_found", "redirected") and not tried_subdomain:
            tried_subdomain = True
            second = _subdomain_road(host)
            if second and second != current:
                posts2, cursor2, kind2, reason2, _ = _page(client, second, limit, None, timeout)
                if not kind2:
                    return second, posts2, cursor2, "", ""
                if kind2 in ("not_found", "redirected"):
                    return current, [], None, "not_found", (
                        "no publication answers on %s and none on %s (%s)"
                        % (host, second, reason2 or reason))
                return second, [], None, kind2, reason2
        if kind == "redirected":
            return current, [], None, "not_found", reason
        return current, posts, cursor, kind, reason


def fetch_page(client, host: str, limit: int, cursor, timeout=None):
    """Return (posts, next_cursor, error_kind, reason) for one page."""
    posts, nxt, kind, reason, _ = _page(client, host, limit, cursor, timeout)
    if kind == "redirect":
        return [], None, "not_found", reason
    return posts, nxt, kind, reason
