"""The pdf itself: signature, pages, text layer, raster.

Run: python tests/test_pdfx.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402

from src import pdfx  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def make_pdf(pages: int = 1, text: str = "Hello OCR") -> bytes:
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page()
        if text:
            page.insert_text((72, 100 + number * 20), f"{text} page {number + 1}", fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


def make_blank_pdf(pages: int = 1) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def make_encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner",
                       user_pw="secret")
    doc.close()
    return data


def test_signature():
    ok(pdfx.looks_like_pdf(make_pdf()), "a real pdf is recognised")
    ok(pdfx.looks_like_pdf(b"\xef\xbb\xbf\n%PDF-1.7\nrest"), "a signature after a BOM counts")
    ok(not pdfx.looks_like_pdf(b"<html><body>login</body></html>"), "html is not a pdf")
    ok(not pdfx.looks_like_pdf(b""), "empty bytes are not a pdf")
    ok(not pdfx.looks_like_pdf(b"x" * 2000 + b"%PDF-"), "a signature outside the first kilobyte is not enough")


def test_open_document():
    doc, status, reason = pdfx.open_document(make_pdf())
    ok(doc is not None and status is None, f"a good pdf opens: {status} {reason}")
    equal(doc.page_count, 1, "one page")
    doc.close()

    doc, status, reason = pdfx.open_document(b"not a pdf at all")
    ok(doc is None, "garbage does not open")
    equal(status, "NOT_CHECKED_BROKEN_PDF", "garbage is a broken pdf")
    ok(reason, "and the reason is not empty")

    doc, status, reason = pdfx.open_document(make_encrypted_pdf())
    ok(doc is None, "an encrypted pdf is not opened")
    equal(status, "NOT_CHECKED_ENCRYPTED", "an encrypted pdf says so")


def test_parse_pages():
    equal(pdfx.parse_pages("", 5), ([0, 1, 2, 3, 4], 0), "an empty spec means every page")
    equal(pdfx.parse_pages("1-3,5", 5), ([0, 1, 2, 4], 0), "ranges and single numbers")
    equal(pdfx.parse_pages("5-3", 5), ([2, 3, 4], 0), "a reversed range is read the other way")
    equal(pdfx.parse_pages("2,2,2", 3), ([1], 0), "a repeated page is read once")
    equal(pdfx.parse_pages("7", 3), ([], 1), "a page beyond the document counts as missing")
    try:
        pdfx.parse_pages("abc", 3)
        ok(False, "a junk spec must raise")
    except ValueError:
        ok(True, "a junk spec raises")
    try:
        pdfx.parse_pages("0", 3)
        ok(False, "page zero must raise")
    except ValueError:
        ok(True, "page zero raises")


def test_text_and_raster():
    doc, _, _ = pdfx.open_document(make_pdf())
    page = doc.load_page(0)
    text = pdfx.text_layer(page)
    ok("Hello OCR" in text, f"the text layer is read: {text!r}")
    png = pdfx.rasterize(page, 120)
    ok(png[:8] == b"\x89PNG\r\n\x1a\n", "the raster is a png")
    ok(len(png) > 1000, "the raster has content")
    width, height = pdfx.page_size(page)
    ok(width > 0 and height > 0, "the page has a size")
    doc.close()

    blank, _, _ = pdfx.open_document(make_blank_pdf())
    equal(pdfx.text_layer(blank.load_page(0)).strip(), "", "a blank page has no text layer")
    blank.close()


def test_title():
    doc, _, _ = pdfx.open_document(make_pdf())
    title = pdfx.document_title(doc)
    ok(title is None or isinstance(title, str), "the title is a string or None")
    doc.close()


def run_all() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
            except Exception as exc:  # noqa: BLE001
                FAILS.append(f"{name} raised {type(exc).__name__}: {exc}")
    print(f"test_pdfx: {CHECKS[0]} checks, {len(FAILS)} failed")
    for line in FAILS:
        print("  FAIL", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run_all())
