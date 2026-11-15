"""Image bytes in, a converted image out, with the format decided by the bytes.

The rule of this park is that a buyer never pays for something that was not
delivered, and for an image actor the first half of that rule is the signature: a
link that answers with an html page, a video or a pdf is not an image, whatever its
name or its content type says. That exact mistake billed an html page as a document
in another actor of this park before the check existed.

Everything here is local: Pillow does the work inside the container, so there is no
key, no image service and no per image cost besides the platform itself.
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps, features

try:  # HEIC reading needs libheif, shipped with the wheel
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # noqa: BLE001 - the build gate proves it is there; a missing one is a free refusal
    pass

MAX_BYTES = 25 * 1024 * 1024
# Measured peak memory of one 60 MP RGBA image: 783 MB on a 253 MB base, and the
# container has 1024 MB with the archive read into memory on top. 40 MP keeps the
# promise inside the memory the buyer pays for; a bigger image comes back free with
# its own status instead of an out of memory kill that loses the whole run.
MAX_PIXELS = 40_000_000

ST_TOO_MANY_PIXELS = "NOT_CHECKED_TOO_MANY_PIXELS"
ST_DECODE = "NOT_CHECKED_DECODE_ERROR"

TARGETS = {
    "jpeg": ("JPEG", "image/jpeg"),
    "png": ("PNG", "image/png"),
    "webp": ("WEBP", "image/webp"),
    "avif": ("AVIF", "image/avif"),
}

_HEIF_BRANDS = {
    b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1",
}
_AVIF_BRANDS = {b"avif", b"avis"}


class ImageError(Exception):
    """A file that cannot be delivered, carrying the status the buyer needs."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def sniff(data: bytes) -> str | None:
    """The real format from the magic bytes, never from the name or content type."""
    head = data[:16] if data else b""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:2] == b"BM":
        return "bmp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _AVIF_BRANDS:
            return "avif"
        if brand in _HEIF_BRANDS:
            return "heif"
    return None


def target_supported(name: str) -> bool:
    fmt = TARGETS.get(name)
    if fmt is None:
        return False
    pil_name = fmt[0]
    if pil_name in ("JPEG", "PNG"):
        return True
    return bool(features.check(pil_name.lower()))


def parse_colour(text: str) -> tuple[int, int, int]:
    """A hex colour, or white when the answer is not one.

    A wrong colour is not worth failing a run over: the buyer sees the delivered
    pixels and the summary says what the background was.
    """
    raw = (text or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        return (255, 255, 255)
    try:
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except ValueError:
        return (255, 255, 255)


def _fit(size: tuple[int, int], max_w: int, max_h: int) -> tuple[int, int]:
    """The delivered size: never upscaled, aspect ratio always kept."""
    width, height = size
    if width <= 0 or height <= 0:
        return width, height
    scale = 1.0
    if max_w and width > max_w:
        scale = min(scale, max_w / float(width))
    if max_h and height > max_h:
        scale = min(scale, max_h / float(height))
    if scale >= 1.0:
        return width, height
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def convert(data: bytes, target: str, quality: int = 82, max_w: int = 0, max_h: int = 0,
            strip: bool = True, background: str = "#FFFFFF") -> dict:
    """One image in, one converted image out, or an ImageError with a status."""
    fmt = TARGETS.get(target)
    if fmt is None:
        raise ImageError("NOT_CHECKED_UNSUPPORTED_TARGET",
                         f"the format '{target}' is not one of {', '.join(sorted(TARGETS))}")
    pil_name, content_type = fmt
    if not target_supported(target):
        raise ImageError("NOT_CHECKED_UNSUPPORTED_TARGET",
                         f"this image has no encoder for {target}")

    try:
        with Image.open(io.BytesIO(data)) as opened:
            width_in, height_in = opened.size
            if width_in * height_in > MAX_PIXELS:
                raise ImageError(
                    ST_TOO_MANY_PIXELS,
                    f"{width_in}x{height_in} is more than the {MAX_PIXELS // 1_000_000} "
                    "megapixels this container can convert: not delivered and not charged")
            opened.load()
            source_format = (opened.format or "unknown").lower()
            # A phone photo is stored sideways with an orientation tag. Applying it to
            # the pixels is the only honest way to resize: otherwise a portrait photo
            # comes out rotated and the buyer blames the converter.
            image = ImageOps.exif_transpose(opened.copy())
            # The reported original size is the upright one: a photo stored sideways
            # is delivered rotated, and a row that says 40x20 for a 20x40 file would be
            # a lie about the very file the buyer downloads.
            width_in, height_in = image.size
    except ImageError:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageError(ST_TOO_MANY_PIXELS,
                         f"the image is too large to decode safely: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - one bad file is a free row, not a dead run
        raise ImageError(ST_DECODE,
                         f"{type(exc).__name__}: the bytes carry an image signature but "
                         "cannot be read: not delivered and not charged") from exc

    width_out, height_out = _fit((width_in, height_in), max_w, max_h)
    if (width_out, height_out) != (width_in, height_in):
        image = image.resize((width_out, height_out), Image.Resampling.LANCZOS)

    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info)
    if pil_name == "JPEG" and has_alpha:
        flat = Image.new("RGB", image.size, parse_colour(background))
        rgba = image.convert("RGBA")
        flat.paste(rgba, mask=rgba.split()[-1])
        image = flat
    elif pil_name == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    if strip:
        # Drops EXIF, GPS, camera tags and the colour profile with them.
        image.info = {}

    out = io.BytesIO()
    carry: dict = {}
    if not strip:
        # Pillow writes exif only when it is handed to save(), never from info, so the
        # unstrpped path has to pass it on purpose. Without this the camera tags
        # vanished exactly when the buyer asked to keep them.
        if image.info.get("exif"):
            carry["exif"] = image.info["exif"]
        if image.info.get("icc_profile"):
            carry["icc_profile"] = image.info["icc_profile"]
    if pil_name == "AVIF":
        carry.pop("exif", None)  # the avif encoder takes the profile, not the exif block
    try:
        if pil_name == "JPEG":
            image.save(out, "JPEG", quality=int(quality), optimize=True, progressive=True,
                       **carry)
        elif pil_name == "PNG":
            image.save(out, "PNG", compress_level=6, **carry)
        elif pil_name == "WEBP":
            image.save(out, "WEBP", quality=int(quality), method=4, **carry)
        else:
            image.save(out, "AVIF", quality=int(quality), speed=8, **carry)
    except Exception as exc:  # noqa: BLE001 - an encoder failure is a free row
        raise ImageError(ST_DECODE,
                         f"{type(exc).__name__}: the image could not be written as "
                         f"{target}: not delivered and not charged") from exc

    payload = out.getvalue()
    return {
        "data": payload,
        "width": width_out,
        "height": height_out,
        "widthIn": width_in,
        "heightIn": height_in,
        "sourceFormat": source_format,
        "format": target,
        "contentType": content_type,
        "bytesIn": len(data),
        "bytesOut": len(payload),
    }
