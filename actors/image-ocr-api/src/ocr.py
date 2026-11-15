"""Tesseract inside the image, and the honest answer when it is not there.

The actor promises text from a scan, so the recogniser is part of the image, not a
key the buyer has to bring. This module is the only place that talks to it: one call
per page and one TSV answer that carries the words, their boxes and their confidence,
so the text and the quality estimate never come from two different runs of the engine.
"""
from __future__ import annotations

import shutil
import subprocess

BIN = "tesseract"
PSM = "3"  # fully automatic page segmentation, the sane default for a document


class OcrError(RuntimeError):
    """The engine failed, as opposed to a page that has nothing to read."""


def engine() -> str | None:
    """The tesseract binary, or None when the image was built without it."""
    return shutil.which(BIN)


def available() -> tuple[bool, str]:
    path = engine()
    if not path:
        return False, "the tesseract binary is not installed in this image"
    return True, f"tesseract at {path}"


def languages() -> set[str]:
    """Language packs the image really has, read from the engine itself."""
    try:
        done = subprocess.run([BIN, "--list-langs"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    if done.returncode != 0:
        return set()
    text = (done.stdout or b"").decode("utf-8", "replace")
    found = set()
    for line in text.splitlines():
        name = line.strip()
        if not name or " " in name or "." in name:
            continue
        if name.lower() == "list":
            continue
        found.add(name)
    return found


def parse_tsv(tsv: str, want_words: bool = False) -> dict:
    """Tesseract's table into text lines, mean confidence and optional boxes.

    Rows of level 5 are words; everything above them is a page, a block, a paragraph
    or a line and carries no text of its own. Lines are rebuilt from the block,
    paragraph and line numbers, because joining every word with a space turns a two
    column page into one long run of nonsense.
    """
    if not tsv.strip():
        return {"text": "", "confidence": None, "words": []}
    rows = [line.split("\t") for line in tsv.splitlines() if line.strip()]
    if not rows:
        return {"text": "", "confidence": None, "words": []}
    header = rows[0]
    index = {name: position for position, name in enumerate(header)}
    need = ("level", "block_num", "par_num", "line_num", "conf", "text")
    if any(name not in index for name in need):
        raise OcrError("tesseract answered in a format this actor does not understand")

    words: list[dict] = []
    confidences: list[float] = []
    lines: list[str] = []
    current_key = None
    current_words: list[str] = []

    for cells in rows[1:]:
        if len(cells) <= index["text"]:
            continue
        try:
            level = int(cells[index["level"]])
        except (TypeError, ValueError):
            continue
        if level != 5:
            continue
        text = cells[index["text"]].strip()
        if not text:
            continue
        try:
            conf = float(cells[index["conf"]])
        except (TypeError, ValueError):
            conf = -1.0
        key = (cells[index["block_num"]], cells[index["par_num"]], cells[index["line_num"]])
        if current_key is not None and key != current_key:
            if current_words:
                lines.append(" ".join(current_words))
            current_words = []
        current_key = key
        current_words.append(text)
        if conf >= 0:
            confidences.append(conf)
        if want_words:
            try:
                words.append({
                    "text": text,
                    "x": int(cells[index["left"]]),
                    "y": int(cells[index["top"]]),
                    "w": int(cells[index["width"]]),
                    "h": int(cells[index["height"]]),
                    "confidence": round(conf, 1) if conf >= 0 else None,
                })
            except (KeyError, TypeError, ValueError):
                continue
    if current_words:
        lines.append(" ".join(current_words))

    mean = round(sum(confidences) / len(confidences), 1) if confidences else None
    return {"text": "\n".join(lines), "confidence": mean, "words": words}


def recognize(image_png: bytes, lang: str, want_words: bool, timeout: float) -> dict:
    """Text, mean word confidence and optionally every word with its box.

    Raises OcrError with a plain reason instead of returning empty text: an engine
    that crashed and a page that is really blank are two different things to the
    buyer, and only one of them is free.
    """
    if not image_png:
        raise OcrError("the page rendered to an empty image")
    try:
        done = subprocess.run(
            [BIN, "stdin", "stdout", "-l", lang, "--psm", PSM, "tsv"],
            input=image_png,
            capture_output=True,
            timeout=max(5.0, float(timeout)),
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"tesseract did not finish in {int(timeout)} s on this page") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise OcrError(f"tesseract could not be started: {type(exc).__name__}") from exc
    if done.returncode != 0:
        reason = (done.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise OcrError(f"tesseract answered {done.returncode}: {reason or 'no reason given'}")
    return parse_tsv((done.stdout or b"").decode("utf-8", "replace"), want_words)
