"""arXiv reader: id parsing and the Atom API.

The API is keyless but asks for politeness: one request at a time, a pause between them,
and a User-Agent that names the caller. Ids therefore go out in chunks, because one call
per id would spend the whole run on sleeping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

TIMEOUT = 25.0
POLITE_SECONDS = 3.0
MAX_IDS_PER_CALL = 50
API = "https://export.arxiv.org/api/query"
USER_AGENT = "apify-arxiv-paper-api/1.0 (https://apify.com/stas_k/arxiv-paper-api)"

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"

_NEW_ID = re.compile(r"^\d{4}\.\d{4,5}$")
_OLD_ID = re.compile(r"^[a-z]+(?:[-.][A-Za-z]+)*/\d{7}$")
_VERSION = re.compile(r"v(\d+)$")
_HOSTS = ("arxiv.org", "www.arxiv.org", "export.arxiv.org")


@dataclass(frozen=True)
class Paper:
    """One id as the buyer sent it, reduced to what the API is asked for."""

    key: str
    paper: str
    version: int | None
    request: str


def _squeeze(text) -> str:
    return " ".join(str(text).split())


def parse_paper(value: str) -> tuple[Paper | None, str]:
    """An arXiv id or a link on arxiv.org -> the id to ask for. A bad line is a verdict."""
    raw = _squeeze(value)
    if not raw:
        return None, "the line is empty: send an arXiv id such as 1706.03762"
    candidate = raw
    if "://" in raw or raw.lower().startswith(_HOSTS):
        link = raw if "://" in raw else "https://" + raw
        try:
            parts = urlsplit(link)
        except ValueError:
            return None, f"{raw!r} is not a link an arXiv id can be taken from"
        if parts.scheme not in ("http", "https"):
            return None, f"only http and https links are read, not {parts.scheme!r}"
        host = (parts.hostname or "").lower()
        if host not in _HOSTS:
            return None, (f"{host or raw!r} is not arxiv.org: send an arXiv id such as "
                          "1706.03762 or a link on arxiv.org")
        bits = [bit for bit in unquote(parts.path or "").split("/") if bit]
        if len(bits) < 2 or bits[0] not in ("abs", "pdf", "format", "ps"):
            return None, (f"the link {raw!r} does not point at a paper: expected /abs/<id> "
                          "or /pdf/<id>")
        candidate = bits[1]
        # An old-style id carries its archive in its own path segment: /abs/hep-th/9901001
        # and /abs/cond-mat.mes-hall/0211022 are real links, and taking only the first
        # segment turned every pre-2007 paper sent as a link into an invalid line.
        if len(bits) >= 3 and bits[2][:7].isdigit():
            candidate = bits[1] + "/" + bits[2]
        if candidate.lower().endswith(".pdf"):
            candidate = candidate[:-4]
    version = None
    match = _VERSION.search(candidate)
    if match:
        version = int(match.group(1))
        candidate = candidate[:match.start()]
    if not (_NEW_ID.match(candidate) or _OLD_ID.match(candidate)):
        return None, (f"{raw!r} is not an arXiv id: expected 1706.03762, hep-th/9901001 or "
                      "a link on arxiv.org")
    # The Atom API answers only the canonical form of an old id: cond-mat.mes-hall/0211022
    # comes back as an empty feed (totalResults 0), which the run would sell as "no such
    # paper" for a paper that exists. The sub-archive is a display name, not part of the key.
    key = candidate
    if "/" in key:
        archive, number = key.split("/", 1)
        if "." in archive:
            key = archive.split(".", 1)[0] + "/" + number
    request = f"{key}v{version}" if version is not None else key
    return Paper(key=key, paper=key, version=version, request=request), ""


def _text(node, tag: str) -> str:
    found = node.find(tag)
    return _squeeze(found.text) if found is not None and found.text else ""


def row_from_entry(entry) -> dict | None:
    """One Atom entry -> one paid row. None means the feed carried an error, not a paper."""
    raw_id = _text(entry, _ATOM + "id")
    if "/api/errors" in raw_id:
        return None
    tail = raw_id.rsplit("/abs/", 1)[-1]
    version = None
    match = _VERSION.search(tail)
    if match:
        version = int(match.group(1))
        tail = tail[:match.start()]
    if not tail:
        return None
    categories = [node.get("term") for node in entry.findall(_ATOM + "category")
                  if node.get("term")]
    primary = entry.find(_ARXIV + "primary_category")
    authors = []
    for node in entry.findall(_ATOM + "author"):
        name = node.find(_ATOM + "name")
        if name is not None and name.text:
            authors.append(_squeeze(name.text))
    pdf_url = ""
    for link in entry.findall(_ATOM + "link"):
        if link.get("title") == "pdf" and link.get("href"):
            pdf_url = link.get("href")
    return {
        "arxivId": tail,
        "version": version,
        "title": _text(entry, _ATOM + "title"),
        "authors": authors,
        "publishedAt": _text(entry, _ATOM + "published"),
        "updatedAt": _text(entry, _ATOM + "updated"),
        "primaryCategory": primary.get("term") if primary is not None else None,
        "categories": categories,
        "comment": _text(entry, _ARXIV + "comment") or None,
        "journalRef": _text(entry, _ARXIV + "journal_ref") or None,
        "doi": _text(entry, _ARXIV + "doi") or None,
        "abstract": _text(entry, _ATOM + "summary"),
        "absUrl": f"https://arxiv.org/abs/{tail}",
        "pdfUrl": pdf_url or f"https://arxiv.org/pdf/{tail}",
        "billable": True,
    }


def _request(client, ids: list, with_abstract: bool, timeout) -> tuple:
    """One physical call. Returns (rows by canonical id, error text or None, HTTP status)."""
    params = {"id_list": ",".join(ids), "max_results": str(len(ids))}
    try:
        response = client.get(API, params=params, timeout=timeout,
                              headers={"User-Agent": USER_AGENT})
    except Exception as exc:  # noqa: BLE001
        return {}, f"arXiv could not be reached ({type(exc).__name__})", None
    if response.status_code != 200:
        return {}, f"arXiv answered HTTP {response.status_code}", response.status_code
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError:
        return {}, "arXiv answered with a body that is not a feed", 200
    rows = {}
    for entry in root.findall(_ATOM + "entry"):
        row = row_from_entry(entry)
        if row is None:
            continue
        if not with_abstract:
            row.pop("abstract", None)
        rows[row["arxivId"]] = row
    return rows, None, 200


def read_papers(client, ids: list, with_abstract: bool = True,
                timeout: float | None = None, max_probes: int = 8) -> tuple:
    """One call for a chunk of ids. Returns (rows by id, error or None, refused by request).

    A missing id is not an error: the feed simply has no entry for it, and the caller turns
    that into a free NOT_FOUND row. An unreadable answer is an error, because an unreachable
    arXiv says nothing about the paper.

    One refused id is a third case, and it is the reason this function returns three things.
    arXiv answers HTTP 500 for the WHOLE id_list when a single id in it is bad, so before the
    fix one such id left the buyer with nothing on fifty good papers, and the run called the
    source unreachable. A server-side refusal is therefore split in halves until the offender
    stands alone: that line comes back in `refused` keyed by the string that was SENT (the only
    name the API knows), and the rest of the chunk is still read and sold. A network failure is
    never split: it says nothing about any single paper.
    """
    if not ids:
        return {}, None, {}
    ids = list(ids)
    rows, error, status = _request(client, ids, with_abstract, timeout)
    if error is None:
        return rows, None, {}
    server_side = status is not None and status >= 500
    if not server_side or len(ids) < 2:
        refused = {ids[0]: error} if server_side else {}
        return rows, error, refused
    found: dict = {}
    refused: dict = {}
    queue = [ids]
    probes = 0
    while queue:
        chunk = queue.pop(0)
        if len(chunk) < 2 or probes >= max_probes:
            # Cannot or may not split further: these lines keep the block error, which is
            # honest ("not read") and never turns into a free NOT_FOUND for a paper that exists.
            for item in chunk:
                refused[item] = error
            continue
        probes += 1
        half = len(chunk) // 2
        for part in (chunk[:half], chunk[half:]):
            part_rows, part_error, part_status = _request(client, part, with_abstract, timeout)
            if part_error is None:
                found.update(part_rows)
                continue
            if part_status is not None and part_status >= 500 and len(part) > 1:
                queue.append(part)
                continue
            for item in part:
                refused[item] = part_error
    return found, error, refused
