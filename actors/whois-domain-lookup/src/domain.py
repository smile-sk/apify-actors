"""Turn messy input lines into clean domain names.

A buyer pastes links, emails, bare names, upper case, a trailing dot or an
international name. All of it goes through one place, so the rest of the actor
only ever sees a lower case ASCII (punycode) domain and its TLD. Anything that
is not a domain is refused here, with a plain reason, before any request is
made and before anything could be charged.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

MAX_DOMAIN_LEN = 253
MAX_LABEL_LEN = 63

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


class BadDomain(ValueError):
    """The line is not a domain. The message is shown to the buyer as is."""


def normalize(raw: str) -> dict:
    """Return {domain, tld, input} for one input line.

    Raises BadDomain with a human reason when the line cannot be a domain.
    """
    original = (raw or "").strip()
    if not original:
        raise BadDomain("empty line")

    text = original

    # A link or an email carries the host somewhere inside: take the host only.
    if "://" in text:
        try:
            host = urlsplit(text).hostname or ""
        except ValueError:
            raise BadDomain("this is not a link that can be read") from None
        if not host:
            raise BadDomain("the link has no host in it")
        text = host
    elif "@" in text:
        text = text.rsplit("@", 1)[1]

    # Strip a path, a query and a port that survived a copy-paste.
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if text.startswith("[") and text.endswith("]"):
        raise BadDomain("this is an IP address, not a domain")
    text = text.split(":", 1)[0]
    text = text.strip().strip(".").lower()
    if not text:
        raise BadDomain("empty line")

    if _IPV4.match(text):
        raise BadDomain("this is an IP address, not a domain")

    # www.example.com is the same registration as example.com: look up the zone,
    # not the host, and keep the original line so the buyer can match rows back.
    if text.startswith("www.") and text.count(".") >= 2:
        text = text[4:]

    if any(ch.isspace() for ch in text):
        raise BadDomain("a domain cannot contain spaces")

    if not text.isascii():
        try:
            text = text.encode("idna").decode("ascii")
        except UnicodeError:
            raise BadDomain("this international name cannot be converted to punycode") from None

    if len(text) > MAX_DOMAIN_LEN:
        raise BadDomain("longer than %d characters" % MAX_DOMAIN_LEN)

    labels = text.split(".")
    if len(labels) < 2:
        raise BadDomain("a domain needs a name and a TLD, for example example.com")
    for label in labels:
        if not label:
            raise BadDomain("a domain cannot have an empty part between dots")
        if len(label) > MAX_LABEL_LEN:
            raise BadDomain("one part of the name is longer than %d characters" % MAX_LABEL_LEN)
        if not _LABEL.match(label):
            raise BadDomain("this is not a valid domain name")
    tld = labels[-1]
    if tld.startswith("xn--"):
        if len(tld) < 8:
            raise BadDomain("this is not a valid domain name")
    elif len(tld) < 2 or not tld.isalpha():
        raise BadDomain("the TLD %r is not a known form" % tld)

    return {"domain": text, "tld": tld, "input": original}


def age_days(created: str | None, now: datetime | None = None) -> int | None:
    """Whole days since the registration date, or None when it is not known."""
    stamp = parse_stamp(created)
    if stamp is None:
        return None
    now = now or datetime.now(stamp.tzinfo)
    return max(0, (now - stamp).days)


def days_until(stamp_text: str | None, now: datetime | None = None) -> int | None:
    """Whole days until a date from an RDAP event, or None when it is not known."""
    stamp = parse_stamp(stamp_text)
    if stamp is None:
        return None
    now = now or datetime.now(stamp.tzinfo)
    return (stamp - now).days


def parse_stamp(value: str | None) -> datetime | None:
    """RDAP dates come as RFC 3339 with a Z or an offset: read them, never guess."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp
