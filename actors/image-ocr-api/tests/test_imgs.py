"""The picture reader under test: signatures, rotation, the pixel ceiling."""
from __future__ import annotations

import io
import sys

from PIL import Image

from src import imgs


def _png(size=(40, 30), color=(255, 255, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_signature_decides_the_format_not_the_name():
    pictures = {
        "jpeg": (lambda b: Image.new("RGB", (8, 8)).save(b, format="JPEG")),
        "png": (lambda b: Image.new("RGB", (8, 8)).save(b, format="PNG")),
        "webp": (lambda b: Image.new("RGB", (8, 8)).save(b, format="WEBP")),
        "gif": (lambda b: Image.new("P", (8, 8)).save(b, format="GIF")),
        "bmp": (lambda b: Image.new("RGB", (8, 8)).save(b, format="BMP")),
        "tiff": (lambda b: Image.new("RGB", (8, 8)).save(b, format="TIFF")),
    }
    for expected, writer in pictures.items():
        buffer = io.BytesIO()
        writer(buffer)
        got = imgs.sniff(buffer.getvalue())
        assert got == expected, f"{expected}: got {got}"


def test_html_and_pdf_are_not_pictures():
    assert imgs.sniff(b"<!doctype html><html><body>login</body></html>") is None
    assert imgs.sniff(b"%PDF-1.7\n%....") is None
    assert imgs.sniff(b"") is None


def test_to_png_reports_the_real_size_and_format():
    got = imgs.to_png(_png())
    assert got["format"] == "png"
    assert (got["width"], got["height"]) == (40, 30)
    assert got["png"][:8] == b"\x89PNG\r\n\x1a\n"
    assert got["scaled"] is False and got["rotated"] is False


def test_a_big_picture_is_scaled_down_and_says_so():
    got = imgs.to_png(_png(size=(400, 400)), max_pixels=10_000)
    assert got["scaled"] is True
    assert got["width"] * got["height"] <= 10_000
    assert (got["inputWidth"], got["inputHeight"]) == (400, 400)


def test_exif_rotation_is_applied_to_the_pixels():
    picture = Image.new("RGB", (40, 20), (255, 255, 255))
    exif = picture.getexif()
    exif[274] = 6  # rotate 90 clockwise
    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", exif=exif)
    got = imgs.to_png(buffer.getvalue())
    assert got["rotated"] is True
    assert (got["width"], got["height"]) == (20, 40), got


def test_a_broken_file_is_an_honest_error():
    data = b"\xff\xd8\xff\xe0" + b"\x00" * 40  # jpeg signature, no picture behind it
    try:
        imgs.to_png(data)
    except Exception as exc:  # noqa: BLE001
        assert "could not be decoded" in str(exc) or "not a jpeg" in str(exc), exc
        return
    raise AssertionError("broken jpeg must not pass")


def run_all() -> int:
    failed = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len([n for n in globals() if n.startswith('test_')])} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_all())
