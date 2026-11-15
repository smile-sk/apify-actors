"""Local checks for the PDF engine. Run: python tests/test_pdf.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf

from src import pdf

FAILS = []
CHECKS = [0]


def ok(cond, name, extra=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, extra))


def make_pdf(pages=("Hello world page one", "Second page with words")):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 100), text, fontsize=14)
    return doc.tobytes()


def test_extract_all_pages():
    data = make_pdf()
    out = pdf.extract(data)
    ok(out["status"] == pdf.STATUS_EXTRACTED, "all pages status", out["status"])
    ok(out["pageCount"] == 2, "page count", out["pageCount"])
    ok("Hello world" in out["pages"][0]["text"], "page one text")
    ok(out["chars"] > 0 and out["truncated"] is False, "chars and truncation")


def test_page_selection():
    data = make_pdf()
    out = pdf.extract(data, pages_spec="2")
    ok(len(out["pages"]) == 1 and out["pages"][0]["page"] == 2, "single page picked")
    out2 = pdf.extract(data, pages_spec="1-2,2")
    ok(len(out2["pages"]) == 2, "range with duplicate collapses", len(out2["pages"]))


def test_bad_page_spec():
    data = make_pdf()
    for spec in ["9", "2-1", "abc", "1-"]:
        try:
            pdf.extract(data, pages_spec=spec)
            ok(False, "bad spec accepted: %s" % spec)
        except ValueError:
            ok(True, "bad spec rejected")


def test_no_text_layer():
    data = make_pdf(pages=("",))
    out = pdf.extract(data)
    ok(out["status"] == pdf.STATUS_NO_TEXT, "blank page is a scan", out["status"])
    ok("scan" in out["reason"].lower(), "scan reason explains", out["reason"])


def test_invalid_pdf():
    try:
        pdf.extract(b"not a pdf at all")
        ok(False, "junk accepted as pdf")
    except pdf.DocError as exc:
        ok(exc.status == pdf.STATUS_INVALID, "junk is INVALID_PDF", exc.status)


def test_truncation():
    data = make_pdf(pages=("x" * 500,))
    out = pdf.extract(data, max_chars=50)
    ok(out["truncated"] is True, "truncated flag")
    ok(out["chars"] <= 50, "chars capped", out["chars"])


def test_private_targets():
    for url in ["http://127.0.0.1/a.pdf", "http://localhost/a.pdf", "http://192.168.1.5/a.pdf", "http://10.0.0.1/a.pdf"]:
        try:
            pdf.fetch(url)
            ok(False, "private target fetched: %s" % url)
        except pdf.DocError as exc:
            ok(exc.status == pdf.STATUS_PRIVATE, "private blocked %s" % url, exc.status)
    try:
        pdf.fetch("file:///etc/passwd")
        ok(False, "file scheme allowed")
    except pdf.DocError as exc:
        ok(exc.status == pdf.STATUS_UNREACHABLE, "file scheme blocked", exc.status)


def test_tables_key_present():
    data = make_pdf()
    out = pdf.extract(data, include_tables=True)
    ok(all("tables" in p for p in out["pages"]), "tables key on every page")
    out2 = pdf.extract(data, include_tables=False)
    ok(all("tables" not in p for p in out2["pages"]), "tables skipped when off")


def test_rows_from():
    from src.main import _rows_from
    ok(_rows_from(["a.pdf", "b.pdf"]) == ["a.pdf", "b.pdf"], "list input")
    ok(_rows_from("a.pdf\nb.pdf") == ["a.pdf", "b.pdf"], "newline string")
    ok(_rows_from(["a.pdf,b.pdf"]) == ["a.pdf", "b.pdf"], "comma string")
    ok(_rows_from(None) == [], "empty input")


def test_not_a_pdf_is_rejected():
    for blob in [b"<html><body>hi</body></html>", b"just text", b""]:
        try:
            pdf.extract(blob)
            ok(False, "non-pdf accepted: %r" % blob[:20])
        except pdf.DocError as exc:
            ok(exc.status == pdf.STATUS_INVALID and "not a PDF" in exc.reason, "plain rejection", exc.reason)


def test_pages_outside_the_document_are_reported():
    data = make_pdf()
    out = pdf.extract(data, pages_spec="1,99")
    ok(out["pagesDropped"] == [99], "page 99 is reported, not lost", out.get("pagesDropped"))


def test_truncation_is_honest():
    data = make_pdf(("short",))
    out = pdf.extract(data, max_chars=100000)
    ok(out["truncated"] is False, "nothing cut, nothing marked")
    tiny = pdf.extract(data, max_chars=2)
    ok(tiny["truncated"] is True, "cut is marked")



def test_pdf_header_after_a_short_prefix():
    data = b"JUNK-PREFIX-" * 20 + make_pdf()
    out = pdf.extract(data)
    ok(out["status"] == pdf.STATUS_EXTRACTED, "a PDF with a prefix is still a PDF", out["status"])


def test_pages_above_the_read_limit_are_counted():
    out = pdf.extract(make_pdf(), pages_spec="1-600")
    ok(out["pagesOverLimit"] == 100, "pages above the read limit are counted", out.get("pagesOverLimit"))
    ok(out["pagesDroppedCount"] == 498, "pages outside the document are counted", out.get("pagesDroppedCount"))
    ok(len(out["pagesDropped"]) == 20, "the listed page numbers are capped", len(out["pagesDropped"]))


def test_long_document_reports_pages_it_did_not_read():
    doc = pymupdf.open()
    for _ in range(505):
        page = doc.new_page()
        page.insert_text((72, 100), "row", fontsize=10)
    out = pdf.extract(doc.tobytes(), include_tables=False, max_chars=10 ** 6)
    ok(out["pagesRead"] == 500, "only 500 pages are read", out["pagesRead"])
    ok(out["pagesOverLimit"] == 5, "the five unread pages are reported", out.get("pagesOverLimit"))

def test_cgnat_and_mapped_addresses_are_not_public():
    ok(pdf._addrs_public([(2, 1, 6, "", ("8.8.8.8", 0))]) is True, "public stays public")
    for bad in ("100.64.0.1", "100.127.255.254", "::ffff:100.64.0.1",
                "::ffff:127.0.0.1", "169.254.169.254"):
        ok(pdf._addrs_public([(2, 1, 6, "", (bad, 0))]) is False, "refused %s" % bad)

def run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                FAILS.append("%s raised %s: %s" % (name, type(exc).__name__, exc))
    print("checks: %d, failed: %d" % (CHECKS[0], len(FAILS)))
    for f in FAILS:
        print("  FAIL", f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(run_all())
