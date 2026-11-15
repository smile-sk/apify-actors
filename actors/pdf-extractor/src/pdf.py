"""PDF extraction core: fetch a document safely, read pages, pull tables.

No Apify imports here on purpose: this module is testable without a platform run.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

STATUS_EXTRACTED = "EXTRACTED"
STATUS_NO_TEXT = "NO_TEXT_LAYER"
STATUS_ENCRYPTED = "ENCRYPTED"
STATUS_INVALID = "INVALID_PDF"
STATUS_UNREACHABLE = "UNREACHABLE"
STATUS_TOO_LARGE = "TOO_LARGE"
STATUS_PRIVATE = "PRIVATE_TARGET"
STATUS_ERROR = "ERROR"

MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 500
MAX_REDIRECTS = 5
# The PDF standard allows the %PDF header anywhere in the first 1024 bytes,
# so a file with a short prefix in front of it is still a PDF.
HEADER_WINDOW = 1024
# A row never carries more than this many page numbers: "1-600" would otherwise
# put hundreds of numbers into every document, which is noise, not information.
MAX_LISTED_PAGES = 20


class DocError(Exception):
    """A document that cannot be read, with a status the buyer can understand."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _addrs_public(infos) -> bool:
    """False when any resolved address is private, loopback, link-local or reserved."""
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        ip = getattr(ip, "ipv4_mapped", None) or ip
        # is_global covers CGNAT 100.64.0.0/10 and the mapped IPv4 form.
        if not ip.is_global:
            return False
    return True


def _check_target(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise DocError(STATUS_UNREACHABLE, "Only http and https links are supported, got %r." % parsed.scheme)
    if not parsed.hostname:
        raise DocError(STATUS_UNREACHABLE, "The link has no host name.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise DocError(STATUS_UNREACHABLE, "The host name does not resolve: %r. Nothing was charged."
                       % parsed.hostname)
    except OSError as exc:
        raise DocError(STATUS_UNREACHABLE, "The host name could not be resolved: %s" % str(exc)[:120])
    if not _addrs_public(infos):
        raise DocError(STATUS_PRIVATE, "The link points to a private or non-public address. Nothing was charged.")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def fetch(url: str, timeout: float = 45.0) -> bytes:
    """Download a PDF, checking every redirect hop against the private-address guard."""
    opener = urllib.request.build_opener(_NoRedirect)
    current = _check_target(url)
    for _hop in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(current, headers={"User-Agent": "apify-pdf-text-extractor/0.1"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > MAX_BYTES:
                    raise DocError(STATUS_TOO_LARGE, "The file is larger than %d MB." % (MAX_BYTES // 1024 // 1024))
                data = resp.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise DocError(STATUS_TOO_LARGE, "The file is larger than %d MB." % (MAX_BYTES // 1024 // 1024))
                return data
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                target = exc.headers.get("Location") or ""
                current = _check_target(urllib.parse.urljoin(current, target))
                continue
            raise DocError(STATUS_UNREACHABLE, "The server answered HTTP %d." % exc.code)
        except DocError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DocError(STATUS_UNREACHABLE, "The file could not be downloaded: %s: %s"
                           % (type(exc).__name__, str(exc)[:140]))
    raise DocError(STATUS_UNREACHABLE, "Too many redirects (more than %d)." % MAX_REDIRECTS)


def parse_pages(spec: str, page_count: int) -> list[int]:
    """Turn "1-3,7" into zero-based page indexes. Raises ValueError with a plain reason."""
    if not spec or not spec.strip():
        return list(range(min(page_count, MAX_PAGES)))
    picked: list[int] = []
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            parts = chunk.split("-")
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                raise ValueError("Cannot read the page range %r. Use numbers like 1-3,7." % chunk)
            start, end = int(parts[0]), int(parts[1])
            if start < 1 or end < start:
                raise ValueError("The page range %r is backwards or starts below 1." % chunk)
            if end > MAX_PAGES:
                end = MAX_PAGES
            picked.extend(range(start - 1, end))
        else:
            if not chunk.isdigit():
                raise ValueError("Cannot read the page range %r. Use numbers like 1-3,7." % chunk)
            picked.append(int(chunk) - 1)
    if not picked:
        raise ValueError("The page list is empty. Leave it blank for every page.")
    out = sorted({p for p in picked if 0 <= p < page_count})
    if not out:
        raise ValueError("None of the requested pages exist: the document has %d page(s)." % page_count)
    return out


def page_stats(spec: str, page_count: int, max_pages: int = MAX_PAGES) -> tuple[list[int], int, int]:
    """Pages asked for but not read, told apart by why.

    Returns (a short list of page numbers outside the document, how many such
    pages there were, how many asked pages sit above the read limit). A request
    like "1-600" must not put five hundred numbers into a single row, and pages
    cut off by the limit must be counted instead of vanishing.
    """
    if not spec or not spec.strip():
        return [], 0, 0
    outside: list[int] = []
    outside_total = 0
    over_limit = 0
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            parts = chunk.split("-")
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            start, end = int(parts[0]), int(parts[1])
            if start < 1 or end < start:
                continue
            if end > max_pages:
                over_limit += end - max(start - 1, max_pages)
            stop = min(end, max_pages)
            for page in range(start, stop + 1):
                if page > page_count:
                    outside_total += 1
                    if len(outside) < MAX_LISTED_PAGES:
                        outside.append(page)
        elif chunk.isdigit():
            page = int(chunk)
            if page < 1:
                continue
            if page > max_pages:
                over_limit += 1
            elif page > page_count:
                outside_total += 1
                if len(outside) < MAX_LISTED_PAGES:
                    outside.append(page)
    return sorted(outside), outside_total, over_limit


def dropped_pages(spec: str, page_count: int) -> list[int]:
    """Page numbers asked for but outside the document (short list, see page_stats)."""
    return page_stats(spec, page_count)[0]


def extract(data: bytes, pages_spec: str = "", include_tables: bool = True,
            max_chars: int = 200_000, password: str = "") -> dict:
    """Read a PDF from bytes and return a row body plus the status."""
    if b"%PDF-" not in data[:HEADER_WINDOW]:
        raise DocError(STATUS_INVALID, "This link is not a PDF: no %%PDF header in the first %d bytes, so nothing was read and nothing was charged." % HEADER_WINDOW)
    try:
        import pymupdf as fitz  # 1.24 and newer
    except ImportError:  # pragma: no cover - older wheels only
        import fitz  # type: ignore
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise DocError(STATUS_INVALID, "This file is not a readable PDF: %s: %s"
                       % (type(exc).__name__, str(exc)[:140]))
    try:
        if getattr(doc, "needs_pass", False):
            if not password or not doc.authenticate(password):
                raise DocError(STATUS_ENCRYPTED, "The PDF is password protected. Pass the password in the input to read it.")
        page_count = doc.page_count
        if page_count == 0:
            raise DocError(STATUS_INVALID, "The PDF has no pages.")
        indexes = parse_pages(pages_spec, page_count)
        dropped, dropped_total, over_limit = page_stats(pages_spec, page_count)
        if not pages_spec.strip():
            # No page list means every page was wanted; pages above the read limit are reported too.
            over_limit = max(0, page_count - MAX_PAGES)
        pages = []
        used = 0
        truncated = False
        for idx in indexes:
            if used >= max_chars:
                truncated = True
                break
            page = doc.load_page(idx)
            text = page.get_text("text") or ""
            piece = text[: max(0, max_chars - used)]
            if len(piece) < len(text):
                truncated = True
            entry = {"page": idx + 1, "chars": len(piece), "text": piece}
            used += len(piece)
            if include_tables:
                entry["tables"] = _tables(page)
            pages.append(entry)
        text_total = sum(p["chars"] for p in pages)
        return {
            "status": STATUS_EXTRACTED if text_total else STATUS_NO_TEXT,
            "pageCount": page_count,
            "pagesRequested": len(indexes),
            "pagesRead": len(pages),
            "chars": text_total,
            "truncated": truncated,
            "pagesDropped": dropped,
            "pagesDroppedCount": dropped_total,
            "pagesOverLimit": over_limit,
            "pages": pages,
            "reason": "" if text_total else "No text layer found: this PDF is probably a scan. OCR is not part of this actor.",
        }
    finally:
        doc.close()


def _tables(page) -> list:
    try:
        found = page.find_tables()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for table in getattr(found, "tables", []):
        try:
            rows = [[(cell or "").strip() for cell in row] for row in table.extract()]
        except Exception:  # noqa: BLE001
            continue
        rows = [r for r in rows if any(c for c in r)]
        if rows:
            out.append(rows)
    return out
