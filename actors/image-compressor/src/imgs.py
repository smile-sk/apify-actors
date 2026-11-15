"""Image bytes in, a smaller image out, with the format decided by the bytes.

The rule of this park is that a buyer never pays for something that was not
delivered, and this actor adds a second rule on top: a buyer does not pay for a file
that did not get smaller. If the image cannot be squeezed under its input size it is
still delivered, byte for byte as it arrived, and the row says so and costs nothing.

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
    "jpeg": ("JPEG", "image/jpeg", ".jpg"),
    "png": ("PNG", "image/png", ".png"),
    "webp": ("WEBP", "image/webp", ".webp"),
    "avif": ("AVIF", "image/avif", ".avif"),
}

# A source format this actor can write back in the same format. Anything else (gif,
# heif, tiff, bmp) is delivered as jpeg, which every browser and every office opens.
KEEPABLE = ("jpeg", "png", "webp", "avif")
FALLBACK_TARGET = "jpeg"

MIN_QUALITY = 30
MIN_SIDE = 64

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


def resolve_target(requested: str, source_format: str) -> tuple[str, str]:
    """The format to write, and the word for it when it is not what was asked.

    'keep' means the source format when this actor can write it, else jpeg. A named
    format is honoured as asked; only an unknown name falls back to 'keep' logic.
    """
    asked = (requested or "keep").strip().lower()
    if asked == "jpg":
        asked = "jpeg"
    if asked in TARGETS:
        return asked, ""
    if asked != "keep":
        return (source_format if source_format in KEEPABLE else FALLBACK_TARGET), "unknown-format"
    return (source_format if source_format in KEEPABLE else FALLBACK_TARGET), ""


def parse_colour(text: str) -> tuple[int, int, int]:
    """A hex colour, or white when the answer is not one."""
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


def _encode(image: Image.Image, pil_name: str, quality: int, carry: dict) -> bytes:
    out = io.BytesIO()
    if pil_name == "JPEG":
        image.save(out, "JPEG", quality=int(quality), optimize=True, progressive=True, **carry)
    elif pil_name == "PNG":
        # PNG is lossless: the only honest lever is the deflate level, and the
        # quality number does not apply. Saying otherwise would be a lie in the row.
        image.save(out, "PNG", compress_level=9, **carry)
    elif pil_name == "WEBP":
        image.save(out, "WEBP", quality=int(quality), method=6, **carry)
    else:
        image.save(out, "AVIF", quality=int(quality), speed=8, **carry)
    return out.getvalue()


def compress(data: bytes, requested_format: str = "keep", quality: int = 80,
             max_w: int = 0, max_h: int = 0, target_bytes: int = 0,
             strip: bool = True, background: str = "#FFFFFF") -> dict:
    """One image in, one smaller image out, or an ImageError with a status."""
    if not data:
        raise ImageError(ST_DECODE, "the file is empty")

    source_format = sniff(data) or "unknown"
    target, format_note = resolve_target(requested_format, source_format)
    fmt = TARGETS.get(target)
    if fmt is None:
        raise ImageError("NOT_CHECKED_UNSUPPORTED_TARGET",
                         f"the format '{target}' is not one of {', '.join(sorted(TARGETS))}")
    pil_name, content_type, extension = fmt
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
                    "megapixels this container can compress: not delivered and not charged")
            opened.load()
            # A phone photo is stored sideways with an orientation tag. Applying it to
            # the pixels is the only honest way to resize: otherwise a portrait photo
            # comes out rotated and the buyer blames the compressor.
            image = ImageOps.exif_transpose(opened.copy())
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

    carry: dict = {}
    if not strip:
        # Pillow writes exif only when it is handed to save(), never from info.
        if image.info.get("exif") and pil_name != "AVIF":
            carry["exif"] = image.info["exif"]
        if image.info.get("icc_profile"):
            carry["icc_profile"] = image.info["icc_profile"]

    quality = max(1, min(int(quality), 100))
    try:
        if target_bytes > 0 and pil_name != "PNG":
            # Highest quality that still fits the budget, by halving the range. If even
            # the lowest quality is too big, the picture is scaled down and the search
            # repeats: that is what "under 200 KB" means for a 12 megapixel photo.
            best = b""
            used_quality = quality
            attempt = image
            for _ in range(4):
                low, high = MIN_QUALITY, quality
                best_local = b""
                best_q = MIN_QUALITY
                current = _encode(attempt, pil_name, high, carry)
                if len(current) <= target_bytes:
                    best_local, best_q = current, high
                else:
                    for _step in range(7):
                        if low > high:
                            break
                        mid = (low + high) // 2
                        blob = _encode(attempt, pil_name, mid, carry)
                        if len(blob) <= target_bytes:
                            best_local, best_q = blob, mid
                            low = mid + 1
                        else:
                            high = mid - 1
                    if not best_local:
                        best_local = _encode(attempt, pil_name, MIN_QUALITY, carry)
                        best_q = MIN_QUALITY
                if not best or len(best_local) < len(best):
                    best, used_quality = best_local, best_q
                if len(best_local) <= target_bytes:
                    break
                scale = (target_bytes / float(len(best_local))) ** 0.5
                new_w = max(MIN_SIDE, int(attempt.size[0] * scale * 0.98))
                new_h = max(MIN_SIDE, int(attempt.size[1] * scale * 0.98))
                if (new_w, new_h) == attempt.size:
                    break
                attempt = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
            payload = best
            final_image = attempt
            width_out, height_out = attempt.size
            quality_used = used_quality
        else:
            quality_used = quality
            payload = _encode(image, pil_name, quality, carry)
            final_image = image
    except ImageError:
        raise
    except Exception as exc:  # noqa: BLE001 - an encoder failure is a free row
        raise ImageError(ST_DECODE,
                         f"{type(exc).__name__}: the image could not be written as "
                         f"{target}: not delivered and not charged") from exc

    target_reached = True if target_bytes <= 0 else len(payload) <= target_bytes
    return {
        "data": payload,
        "width": width_out,
        "height": height_out,
        "widthIn": width_in,
        "heightIn": height_in,
        "sourceFormat": source_format,
        "format": target,
        "contentType": content_type,
        "extension": extension,
        "bytesIn": len(data),
        "bytesOut": len(payload),
        "qualityUsed": quality_used,
        "targetBytes": target_bytes if target_bytes > 0 else None,
        "targetReached": target_reached,
        "formatNote": format_note,
        "resized": (width_out, height_out) != (width_in, height_in),
    }
