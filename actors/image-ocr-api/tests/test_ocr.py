"""Tesseract output parsing, and the reason it gives when the engine is gone.

Run: python tests/test_ocr.py

The engine itself is not installed on the machine that runs these checks, so the
binary is not called here. What IS checked is the table tesseract really answers with
(a sample of its own TSV format, header included) and the refusal path: a missing
engine must raise a plain reason, not return empty text that looks like a blank page.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import ocr  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []

HEADER = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth"
          "\theight\tconf\ttext")


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def word(block, par, line, word_no, text, conf, left=10, top=20):
    return (f"5\t1\t{block}\t{par}\t{line}\t{word_no}\t{left}\t{top}\t40\t12\t{conf}\t{text}")


def sample() -> str:
    rows = [
        "1\t1\t0\t0\t0\t0\t0\t0\t500\t700\t-1\t",
        "2\t1\t1\t0\t0\t0\t10\t20\t300\t100\t-1\t",
        "3\t1\t1\t1\t0\t0\t10\t20\t300\t100\t-1\t",
        "4\t1\t1\t1\t1\t0\t10\t20\t300\t14\t-1\t",
        word(1, 1, 1, 1, "Invoice", "96.5"),
        word(1, 1, 1, 2, "number", "91.0"),
        word(1, 1, 1, 3, "42", "88.0"),
        "4\t1\t1\t1\t2\t0\t10\t40\t300\t14\t-1\t",
        word(1, 1, 2, 1, "Total", "70.0"),
        word(1, 1, 2, 2, "100.00", "60.0"),
        "4\t1\t1\t2\t1\t0\t10\t60\t300\t14\t-1\t",
        word(1, 2, 1, 1, "Thank", "80.0"),
        word(1, 2, 1, 2, "you", "80.0"),
    ]
    return HEADER + "\n" + "\n".join(rows) + "\n"


def test_lines_are_rebuilt():
    got = ocr.parse_tsv(sample())
    equal(got["text"], "Invoice number 42\nTotal 100.00\nThank you",
          "words are joined per line, lines per paragraph")
    equal(len(got["text"].splitlines()), 3, "three lines")


def test_confidence():
    got = ocr.parse_tsv(sample())
    # mean of 96.5 91 88 70 60 80 80
    equal(got["confidence"], round((96.5 + 91 + 88 + 70 + 60 + 80 + 80) / 7, 1),
          "confidence is the mean of the words")


def test_words():
    got = ocr.parse_tsv(sample(), want_words=True)
    equal(len(got["words"]), 7, "seven words with boxes")
    equal(got["words"][0]["text"], "Invoice", "the first word is read")
    equal(got["words"][0]["confidence"], 96.5, "its confidence is kept")
    equal((got["words"][0]["x"], got["words"][0]["y"]), (10, 20), "its box is kept")
    ok(ocr.parse_tsv(sample(), want_words=False)["words"] == [], "boxes are off by default")


def test_junk_rows():
    equal(ocr.parse_tsv("")["text"], "", "an empty table is empty text")
    equal(ocr.parse_tsv("   \n\n")["confidence"], None, "no confidence without words")
    junk = HEADER + "\n" + "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\tnan\t\n"
    equal(ocr.parse_tsv(junk)["text"], "", "a word row with no text is skipped")
    negative = HEADER + "\n" + word(1, 1, 1, 1, "Hi", "-1")
    equal(ocr.parse_tsv(negative)["confidence"], None, "a confidence of -1 is not a number")
    no_header = "1\t2\t3\n4\t5\t6\n"
    try:
        ocr.parse_tsv(no_header)
        ok(False, "a table without the expected columns must raise")
    except ocr.OcrError:
        ok(True, "a table without the expected columns raises")


def test_missing_engine_is_a_reason():
    original = ocr.BIN
    ocr.BIN = "tesseract-xyz-not-installed"
    try:
        try:
            ocr.recognize(b"\x89PNG\r\n\x1a\n", "eng", False, 5.0)
            ok(False, "a missing engine must raise")
        except ocr.OcrError as exc:
            ok("could not be started" in str(exc), f"the reason names the engine: {exc}")
        try:
            ocr.recognize(b"", "eng", False, 5.0)
            ok(False, "an empty image must raise")
        except ocr.OcrError:
            ok(True, "an empty image raises")
    finally:
        ocr.BIN = original


def test_available_reports_the_binary():
    present, reason = ocr.available()
    if present:
        ok("tesseract at" in reason, "the reason names the binary")
    else:
        ok("not installed" in reason, "the absence is said plainly")


def run_all() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
            except Exception as exc:  # noqa: BLE001
                FAILS.append(f"{name} raised {type(exc).__name__}: {exc}")
    print(f"test_ocr: {CHECKS[0]} checks, {len(FAILS)} failed")
    for line in FAILS:
        print("  FAIL", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run_all())
