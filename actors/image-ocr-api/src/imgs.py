"""An image is a picture until its signature says otherwise.

The signature decides what we treat as an image, not the content type and not the
file name: a link answering with an html login page or a pdf is not a picture, and
that exact case was billed as a document once in this park before the check existed.
Everything the recogniser sees goes through one door here: bytes in, honest png out,
with the picture already turned the right way up and shrunk only when it is too big
for the memory this run really has.
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps

try:  # heic is a phone photo, not an exotic format, and it is promised in the schema
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001 - the build gate proves this, not a silent hole
    pass

MAX_BYTES = 25 * 1024 * 1024
# A 60 megapixel photo opens as ~240 MB of RGBA before the recogniser ever sees it,
# which is why the ceiling is a number and not a hope.
MAX_PIXELS = 40_000_000

_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif", b"avis")


class ImageError(RuntimeError):
    """The bytes are not a picture this actor can read."""


def sniff(data: bytes) -> str | None:
    """The real format by signature, or None when these bytes are not an image."""
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "heic"
    return None


def to_png(data: bytes, max_pixels: int = MAX_PIXELS) -> dict:
    """Png bytes the recogniser can read, plus what was changed on the way.

    Rotation comes from the exif tag the phone wrote, and it is applied to the
    pixels: a sideways photo is a scan the engine reads badly, and the buyer would
    blame the engine for our laziness.
    """
    kind = sniff(data)
    if kind is None:
        raise ImageError("the bytes are not a jpeg, png, webp, gif, bmp, tiff or heic image")
    try:
        picture = Image.open(io.BytesIO(data))
        picture.load()
    except Exception as exc:  # noqa: BLE001 - a broken file is a status, not a crash
        raise ImageError(f"the image could not be decoded: {type(exc).__name__}") from exc

    width, height = picture.size
    rotated = False
    try:
        turned = ImageOps.exif_transpose(picture)
        if turned is not None and turned.size != picture.size:
            picture = turned
            rotated = True
    except Exception:  # noqa: BLE001 - a broken exif block must not lose the picture
        pass

    pixels = picture.size[0] * picture.size[1]
    scaled = False
    if max_pixels and pixels > max_pixels:
        ratio = (max_pixels / float(pixels)) ** 0.5
        size = (max(1, int(picture.size[0] * ratio)), max(1, int(picture.size[1] * ratio)))
        picture = picture.resize(size, Image.LANCZOS)
        scaled = True

    if picture.mode not in ("RGB", "L"):
        picture = picture.convert("RGB")
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG", optimize=False)
    return {
        "png": buffer.getvalue(),
        "format": kind,
        "width": picture.size[0],
        "height": picture.size[1],
        "inputWidth": width,
        "inputHeight": height,
        "rotated": rotated,
        "scaled": scaled,
    }
