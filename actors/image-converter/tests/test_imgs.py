"""The image engine: the signature decides, the size is kept, the metadata is dropped.

Run: python tests/test_imgs.py

Nothing here touches the network: the bytes are made with Pillow itself, so the checks
prove the engine and the refusals, not the internet.
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from src import imgs  # noqa: E402

CHECKS = [0]
FAILS: list[str] = []


def ok(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        FAILS.append(message)


def equal(got, want, message: str) -> None:
    ok(got == want, f"{message}: got {got!r}, want {want!r}")


def png_bytes(size=(40, 20), mode="RGB", colour=(10, 120, 200)) -> bytes:
    image = Image.new(mode, size, colour)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def jpeg_bytes(size=(40, 20), colour=(200, 30, 30), exif=None) -> bytes:
    image = Image.new("RGB", size, colour)
    out = io.BytesIO()
    kwargs = {"quality": 90}
    if exif is not None:
        kwargs["exif"] = exif
    image.save(out, "JPEG", **kwargs)
    return out.getvalue()


def test_signature_decides():
    equal(imgs.sniff(png_bytes()), "png", "a png is recognised by its bytes")
    equal(imgs.sniff(jpeg_bytes()), "jpeg", "a jpeg is recognised by its bytes")
    gif = io.BytesIO()
    Image.new("P", (4, 4)).save(gif, "GIF")
    equal(imgs.sniff(gif.getvalue()), "gif", "a gif is recognised by its bytes")
    webp = io.BytesIO()
    Image.new("RGB", (4, 4)).save(webp, "WEBP")
    equal(imgs.sniff(webp.getvalue()), "webp", "a webp is recognised by its bytes")
    avif = io.BytesIO()
    Image.new("RGB", (4, 4)).save(avif, "AVIF")
    equal(imgs.sniff(avif.getvalue()), "avif", "an avif is recognised by its bytes")
    equal(imgs.sniff(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"),
          "heif", "a heic brand is recognised even without a decoder")
    equal(imgs.sniff(b"<!doctype html><html><body>login</body></html>"), None,
          "an html page is not an image, whatever the url says")
    equal(imgs.sniff(b"%PDF-1.7\n1 0 obj"), None, "a pdf is not an image")
    equal(imgs.sniff(b""), None, "empty bytes are not an image")
    equal(imgs.sniff(b"\xff\xd8"), None, "two bytes are not a jpeg either")


def test_targets():
    for name in ("jpeg", "png", "webp"):
        ok(imgs.target_supported(name), f"{name} must be writable by this build")
    ok(imgs.target_supported("avif") == bool(imgs.features.check("avif")),
       "avif support follows the build, not a promise")
    ok(not imgs.target_supported("gif"), "gif is not a target we sell")
    ok(not imgs.target_supported("bmp"), "bmp is not a target we sell")


def test_png_to_webp_keeps_size():
    got = imgs.convert(png_bytes((120, 60)), "webp", quality=80)
    equal((got["width"], got["height"]), (120, 60), "no resize limit keeps the size")
    equal(got["sourceFormat"], "png", "the row says what the file really was")
    equal(got["format"], "webp", "the row says what was delivered")
    equal(got["contentType"], "image/webp", "and the content type follows the format")
    equal(imgs.sniff(got["data"]), "webp", "the delivered bytes really are a webp")
    ok(got["bytesOut"] < got["bytesIn"], "a flat png becomes a smaller webp")


def test_resize_keeps_aspect_and_never_upscales():
    got = imgs.convert(png_bytes((400, 200)), "png", max_w=200, max_h=200)
    equal((got["width"], got["height"]), (200, 100), "the width limits and the aspect follows")
    equal((got["widthIn"], got["heightIn"]), (400, 200), "the row keeps the original size")
    tall = imgs.convert(png_bytes((100, 500)), "png", max_w=200, max_h=100)
    equal((tall["width"], tall["height"]), (20, 100), "the height limits too")
    small = imgs.convert(png_bytes((40, 30)), "png", max_w=1000, max_h=1000)
    equal((small["width"], small["height"]), (40, 30), "a small image is never upscaled")
    free = imgs.convert(png_bytes((300, 300)), "png", max_w=0, max_h=0)
    equal((free["width"], free["height"]), (300, 300), "zero means no limit")


def test_transparency_flattens_on_the_background_for_jpeg():
    got = imgs.convert(png_bytes((10, 10), "RGBA", (0, 0, 0, 0)), "jpeg",
                       background="#FF0000")
    with Image.open(io.BytesIO(got["data"])) as out:
        red, green, blue = out.convert("RGB").getpixel((5, 5))
    ok(red > 240 and green < 15 and blue < 15,
       f"a transparent area takes the asked colour, got {(red, green, blue)}")
    wrong = imgs.parse_colour("not a colour")
    equal(wrong, (255, 255, 255), "a colour that cannot be read falls back to white")
    equal(imgs.parse_colour("#000"), (0, 0, 0), "a short hex colour is understood")


def test_metadata_is_stripped_and_orientation_applied():
    exif = Image.Exif()
    exif[0x0112] = 6          # orientation: rotate 90
    exif[0x010F] = "TestCam"  # make
    sideways = jpeg_bytes((40, 20), exif=exif.tobytes())
    got = imgs.convert(sideways, "jpeg", strip=True)
    equal((got["width"], got["height"]), (20, 40),
          "the orientation from exif is applied to the pixels, so the photo is upright")
    with Image.open(io.BytesIO(got["data"])) as out:
        equal(dict(out.getexif()), {}, "stripMetadata drops exif from the delivered file")
    kept = imgs.convert(sideways, "jpeg", strip=False)
    with Image.open(io.BytesIO(kept["data"])) as out:
        ok(dict(out.getexif()).get(0x010F) == "TestCam",
           "without stripping the camera stays in the file")


def test_refusals_are_free_and_named():
    try:
        imgs.convert(b"\x89PNG\r\n\x1a\ntruncated garbage", "webp")
        ok(False, "a broken file with a png signature must be refused")
    except imgs.ImageError as exc:
        equal(exc.status, imgs.ST_DECODE, "a broken png is a decode error, not a crash")
        ok("not charged" in exc.reason, "the reason says nothing was charged")
    try:
        imgs.convert(png_bytes(), "gif")
        ok(False, "gif is not a target")
    except imgs.ImageError as exc:
        equal(exc.status, "NOT_CHECKED_UNSUPPORTED_TARGET",
              "an unsupported target has its own status")
    saved = imgs.MAX_PIXELS
    imgs.MAX_PIXELS = 100
    try:
        imgs.convert(png_bytes((40, 20)), "webp")
        ok(False, "an image over the pixel ceiling must be refused")
    except imgs.ImageError as exc:
        equal(exc.status, imgs.ST_TOO_MANY_PIXELS,
              "too many pixels has its own status, not a decode error")
    finally:
        imgs.MAX_PIXELS = saved


def test_gif_and_alpha_png_are_converted_from_the_first_frame():
    gif = io.BytesIO()
    frames = [Image.new("P", (8, 8), 1), Image.new("P", (8, 8), 2)]
    frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:])
    got = imgs.convert(gif.getvalue(), "png")
    equal((got["width"], got["height"]), (8, 8), "a gif is converted at its own size")
    equal(got["sourceFormat"], "gif", "and reported as a gif")
    png_alpha = imgs.convert(png_bytes((8, 8), "RGBA", (0, 0, 0, 128)), "webp")
    with Image.open(io.BytesIO(png_alpha["data"])) as out:
        ok(out.mode in ("RGBA", "RGB"), f"an alpha png to webp keeps a usable mode, got {out.mode}")


def main() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    print(f"{CHECKS[0]} checks, {len(FAILS)} failed")
    for line in FAILS:
        print("FAIL:", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
