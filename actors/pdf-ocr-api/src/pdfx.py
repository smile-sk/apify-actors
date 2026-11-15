"""The pdf itself: its bytes, its pages, its text layer and its raster.

One rule runs through this module: what the file says about itself decides, not what
the url or the content type claims. A link named .pdf that answers with an html login
page is not a document, and an html page served as application/pdf is not one either.
The first kilobyte is read for the %PDF signature, and a file without it is refused
before any parser touches it. That lesson came from a live run where an html page was
billed as a pdf.
"""
from __future__ import annotations

import io

import pymupdf

ST_ENCRYPTED = "NOT_CHECKED_ENCRYPTED"
ST_BROKEN = "NOT_CHECKED_BROKEN_PDF"

MAX_BYTES = 80 * 1024 * 1024
SIGNATURE_WINDOW = 1024


def looks_like_pdf(data: bytes) -> bool:
    """The signature is looked for in the first kilobyte, not only at offset zero.

    Some generators write a byte order mark or a stray newline before %PDF, and
    MuPDF happily opens those files. Refusing them would be a false negative on a
    document we can read.
    """
    if not data:
        return False
    return b"%PDF-" in data[:SIGNATURE_WINDOW]


def open_document(data: bytes) -> tuple[object | None, str | None, str | None]:
    """(document, status, reason). A refusal is never an exception."""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - a broken file is an answer, not a crash
        return None, ST_BROKEN, f"the file could not be parsed: {type(exc).__name__}"
    try:
        if doc.needs_pass:
            doc.close()
            return None, ST_ENCRYPTED, "the document needs a password, which this actor does not take"
        if doc.page_count <= 0:
            doc.close()
            return None, ST_BROKEN, "the document carries no pages"
    except Exception as exc:  # noqa: BLE001
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
        return None, ST_BROKEN, f"the document could not be inspected: {type(exc).__name__}"
    return doc, None, None


def parse_pages(spec: str, total: int) -> tuple[list[int], int]:
    """Zero based page indexes to read, and how many asked numbers do not exist.

    A spec that cannot be understood raises ValueError: the run then stops before the
    first request and charges nothing. Silently ignoring a broken range would sell the
    buyer pages outside the window he asked for.
    """
    text = "" if spec is None else str(spec).strip()
    if not text:
        return list(range(total)), 0
    chosen: list[int] = []
    seen: set[int] = set()
    missing = 0
    for chunk in text.replace(";", ",").split(","):
        part = chunk.strip()
        if not part:
            continue
        if "-" in part:
            left, _, right = part.partition("-")
            try:
                start = int(left.strip())
                end = int(right.strip())
            except ValueError as exc:
                raise ValueError(f"'{part}' is not a page range like 1-5") from exc
            if start > end:
                start, end = end, start
            numbers = range(start, end + 1)
        else:
            try:
                number = int(part)
            except ValueError as exc:
                raise ValueError(f"'{part}' is not a page number") from exc
            numbers = [number]
        if not numbers:
            continue
        if min(numbers) < 1:
            raise ValueError("page numbers start at 1")
        for number in numbers:
            if number > total:
                missing += 1
                continue
            zero = number - 1
            if zero in seen:
                continue
            seen.add(zero)
            chosen.append(zero)
    if not chosen and missing == 0:
        return [], 0
    return sorted(chosen), missing


def text_layer(page) -> str:
    """The embedded text, exactly as the producer wrote it."""
    try:
        return page.get_text("text") or ""
    except Exception:  # noqa: BLE001 - a page without a layer has no text
        return ""


def rasterize(page, dpi: int) -> bytes:
    """The page as a png, which is what the recogniser reads."""
    pixmap = page.get_pixmap(dpi=int(dpi))
    return pixmap.tobytes("png")


def document_title(doc) -> str | None:
    try:
        meta = doc.metadata or {}
    except Exception:  # noqa: BLE001
        return None
    title = (meta.get("title") or "").strip()
    return title or None


def page_size(page) -> tuple[float, float]:
    try:
        rect = page.rect
        return round(float(rect.width), 1), round(float(rect.height), 1)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0
