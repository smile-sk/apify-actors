"""Address checks that need no mailbox: syntax, domain, MX, and what the address looks like.

The DNS lookup goes through a public DNS-over-HTTPS endpoint, so the actor needs no
system resolver and no API key, and it still works in a container whose UDP 53 is
filtered. What this module deliberately does NOT do is probe SMTP: port 25 is closed
both on our side and inside the Apify container, so an honest product can say "this
domain accepts mail", never "this mailbox exists".
"""

from __future__ import annotations

import encodings.idna
import re

DOH_URL = "https://cloudflare-dns.com/dns-query"

_LOCAL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$")
_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")

ROLE_LOCALS = {
    "abuse", "admin", "administrator", "billing", "contact", "enquiries", "help", "hr",
    "info", "inquiries", "jobs", "legal", "mail", "marketing", "noreply", "no-reply",
    "office", "postmaster", "press", "privacy", "sales", "security", "spam", "support",
    "team", "webmaster",
}

FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.de", "gmx.net", "web.de", "mail.com", "zoho.com", "yandex.ru", "yandex.com",
    "mail.ru", "bk.ru", "inbox.ru", "list.ru", "rambler.ru", "ukr.net", "i.ua", "meta.ua",
    "online.ua", "seznam.cz", "wp.pl", "o2.pl", "interia.pl", "qq.com", "163.com", "126.com",
}

DISPOSABLE_DOMAINS = {
    "10minutemail.com", "20minutemail.com", "33mail.com", "anonbox.net", "burnermail.io",
    "dispostable.com", "dropmail.me", "emailondeck.com", "fakeinbox.com", "getairmail.com",
    "getnada.com", "guerrillamail.com", "guerrillamail.info", "guerrillamail.net",
    "inboxkitten.com", "maildrop.cc", "mailinator.com", "mailnesia.com", "mintemail.com",
    "mohmal.com", "mytemp.email", "sharklasers.com", "spam4.me", "temp-mail.io",
    "temp-mail.org", "tempail.com", "tempmail.com", "tempmailo.com", "throwawaymail.com",
    "trashmail.com", "yopmail.com", "yopmail.fr", "yopmail.net",
}

POPULAR_DOMAINS = [
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "protonmail.com", "ukr.net", "i.ua", "meta.ua", "mail.ru", "yandex.ru",
]


def normalise(raw: str) -> str:
    """Trim the noise a spreadsheet adds, without touching the address itself.

    Two passes: `<a@b.com>,` needs the bracket and the comma removed whatever order they
    arrived in, and one pass in one order leaves the bracket inside the address.
    """
    text = (raw or "").strip()
    for _ in range(2):
        before = text
        text = text.strip("<>").strip()
        text = text.rstrip(".,;:").strip()
        if text == before:
            break
    return text


def split_address(text: str) -> tuple[str, str, str]:
    """(local, domain, reason). The reason is empty only when both halves look sane."""
    if not text:
        return "", "", "the address is empty"
    if text.count("@") != 1:
        return "", "", "the address must hold exactly one @"
    local, domain = text.rsplit("@", 1)
    if not local:
        return "", "", "the local part is empty"
    if len(local) > 64:
        return "", "", "the local part is longer than 64 characters"
    if not _LOCAL.match(local):
        return "", "", "the local part has a character an address cannot hold"
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return "", "", "the local part has a leading, trailing or doubled dot"
    if not domain:
        return "", "", "the domain is empty"
    return local, domain, ""


def ascii_domain(domain: str) -> tuple[str, str]:
    """Turn a Unicode domain into its punycode form, which is what DNS understands."""
    text = (domain or "").strip().rstrip(".")
    if not text:
        return "", "the domain is empty"
    if len(text) > 253:
        return "", "the domain is longer than 253 characters"
    labels: list[str] = []
    for label in text.split("."):
        if not label:
            return "", "the domain has an empty label"
        if all(ord(ch) < 128 for ch in label):
            ascii_label = label
        else:
            try:
                ascii_label = encodings.idna.ToASCII(label).decode("ascii")
            except Exception:  # noqa: BLE001
                return "", "the domain label cannot be encoded"
        if not _LABEL.match(ascii_label):
            return "", f"the label {ascii_label[:40]} is not a valid domain label"
        labels.append(ascii_label.lower())
    if len(labels) < 2:
        return "", "the domain needs at least one dot"
    if labels[-1].isdigit():
        return "", "a top level domain cannot be all digits"
    return ".".join(labels), ""


def address_key(raw: str) -> str:
    """The identity of an address, not of the line it arrived on.

    `a@münchen.de` and `A@xn--mnchen-3ya.de` are one address; a key built from the input
    string would charge for it twice. A line we cannot parse falls back to itself.
    """
    text = normalise(raw)
    local, domain, reason = split_address(text)
    if not reason:
        ascii_name, why = ascii_domain(domain)
        if not why:
            return local.lower() + "@" + ascii_name.lower()
    return "raw:" + text.lower()


def dns_query(client, name: str, rtype: str,
              timeout: float | None = None) -> tuple[list[str], str, str]:
    """Ask the public resolver.

    Returns (records, reason, kind) where kind is "ok", "nxdomain" or "error". The
    three are kept apart on purpose: "the domain does not exist" is a verdict a buyer
    pays for, "the resolver did not answer" is our failure and stays free.
    """
    try:
        if timeout is None:
            response = client.get(DOH_URL, params={"name": name, "type": rtype})
        else:
            response = client.get(DOH_URL, params={"name": name, "type": rtype},
                                  timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return [], f"the resolver could not be reached: {type(exc).__name__}", "error"
    if response.status_code == 400:
        return [], "the resolver refused the name as invalid", "error"
    if response.status_code != 200:
        return [], f"the resolver answered HTTP {response.status_code}", "error"
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return [], "the resolver did not answer JSON", "error"
    status = payload.get("Status")
    if status == 3:
        return [], "no such domain", "nxdomain"
    if status not in (0, None):
        return [], f"the resolver answered status {status}", "error"
    records: list[str] = []
    for answer in payload.get("Answer") or []:
        if rtype == "MX" and answer.get("type") == 15:
            records.append(str(answer.get("data") or ""))
        elif rtype == "A" and answer.get("type") == 1:
            records.append(str(answer.get("data") or ""))
        elif rtype == "AAAA" and answer.get("type") == 28:
            records.append(str(answer.get("data") or ""))
    return records, "", "ok"


def _distance(a: str, b: str) -> int:
    """Plain Levenshtein, small enough to keep here instead of a dependency."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def typo_for(domain: str) -> str:
    """The popular domain this one is one edit away from, if any."""
    best = ""
    best_distance = 3
    for popular in POPULAR_DOMAINS:
        if domain == popular:
            return ""
        distance = _distance(domain, popular)
        if distance < best_distance:
            best, best_distance = popular, distance
    return best if best_distance <= 2 else ""


def analyse(client, raw: str, *, check_dns: bool = True,
            timeout: float | None = None) -> dict:
    """Everything we can say about one address without probing a mailbox.

    `valid` is deliberately not reported: it could only repeat `acceptsMail`, and a buyer
    reads `valid: true` as "this mailbox exists", which nothing here can prove.
    """
    text = normalise(raw)
    local, domain, reason = split_address(text)
    row = {
        "email": raw,
        "normalised": text,
        "localPart": local or None,
        "domain": domain or None,
        "domainAscii": None,
        "syntaxValid": False,
        "domainExists": None,
        "hasMx": None,
        "mxHosts": None,
        "acceptsMail": None,
        "isDisposable": None,
        "isRole": None,
        "isFreeProvider": None,
        "suggestion": None,
        "status": "OK",
        "reason": "",
    }
    if reason:
        row["status"] = "INVALID_SYNTAX"
        row["reason"] = reason
        return row
    row["syntaxValid"] = True
    ascii_name, domain_reason = ascii_domain(domain)
    if domain_reason:
        row["status"] = "INVALID_SYNTAX"
        row["reason"] = domain_reason
        return row
    row["domainAscii"] = ascii_name
    lowered = ascii_name.lower()
    row["isDisposable"] = lowered in DISPOSABLE_DOMAINS
    row["isFreeProvider"] = lowered in FREE_DOMAINS
    row["isRole"] = local.lower() in ROLE_LOCALS
    row["suggestion"] = typo_for(lowered) or None
    if not check_dns:
        # Nothing was asked, so nothing can be sold: the row is a format verdict only.
        row["status"] = "SYNTAX_ONLY"
        row["reason"] = "checkDns is off: the format was checked, the domain was not"
        return row

    mx, mx_reason, mx_kind = dns_query(client, ascii_name, "MX", timeout)
    if mx_kind == "nxdomain":
        return _no_domain(row)
    if mx_kind == "error":
        row["status"] = "NOT_CHECKED_DNS_ERROR"
        row["reason"] = mx_reason
        return row
    hosts = []
    for item in mx:
        parts = str(item).split()
        if parts:
            hosts.append(parts[-1].rstrip(".").lower())
    real_hosts = sorted({host for host in hosts if host})
    row["domainExists"] = True
    if mx and not real_hosts:
        # RFC 7505: "0 ." is a published refusal to accept mail, not a mail server.
        row["hasMx"] = False
        row["mxHosts"] = None
        row["acceptsMail"] = False
        row["status"] = "NO_MAIL_SERVER"
        row["reason"] = "the domain publishes a null MX (RFC 7505): it accepts no mail"
        return row
    row["hasMx"] = bool(real_hosts)
    row["mxHosts"] = real_hosts[:10] or None
    if not real_hosts:
        addresses, a_reason, a_kind = dns_query(client, ascii_name, "A", timeout)
        if a_kind == "nxdomain":
            return _no_domain(row)
        if a_kind == "error":
            row["status"] = "NOT_CHECKED_DNS_ERROR"
            row["reason"] = a_reason
            return row
        if not addresses:
            # RFC 5321 5.1: the implicit MX may live on AAAA as well as on A.
            six, six_reason, six_kind = dns_query(client, ascii_name, "AAAA", timeout)
            if six_kind == "nxdomain":
                return _no_domain(row)
            if six_kind == "error":
                row["status"] = "NOT_CHECKED_DNS_ERROR"
                row["reason"] = six_reason
                return row
            addresses = six
        row["domainExists"] = bool(addresses)
    row["acceptsMail"] = bool(real_hosts or row["domainExists"])
    if not row["acceptsMail"]:
        row["status"] = "NO_MAIL_SERVER"
        row["reason"] = "no MX, A or AAAA record was found for this name"
        return row
    return row


def _no_domain(row: dict) -> dict:
    """One place for the verdict "this name is not in DNS", so it is always the same."""
    row["status"] = "NO_SUCH_DOMAIN"
    row["reason"] = "the domain does not exist in DNS"
    row["domainExists"] = False
    row["hasMx"] = False
    row["acceptsMail"] = False
    return row
