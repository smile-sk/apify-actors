"""The compression engine: the signature decides, the weight falls, the format is kept.

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


def _detail(size=(120, 90), mode="RGB") -> Image.Image:
    """A picture with real detail: a flat square would not shrink at all."""
    image = Image.new(mode, size)
    px = image.load()
    for x in range(size[0]):
        for y in range(size[1]):
            px[x, y] = ((x * 7 + y * 13) % 256, (x * 3 + y * 29) % 256, (x * 11 + y * 5) % 256)
    return image


def png_bytes(size=(120, 90), mode="RGB", colour=(10, 120, 200), level=0) -> bytes:
    """Stored loose on purpose: png at level 0 is what a phone gallery or a scan gives."""
    image = Image.new(mode, size, colour) if level != 0 else _detail(size, mode)
    out = io.BytesIO()
    image.save(out, "PNG", compress_level=level)
    return out.getvalue()


def jpeg_bytes(size=(120, 90), colour=(200, 30, 30), exif=None, quality=100) -> bytes:
    image = _detail(size) if colour is None else Image.new("RGB", size, colour)
    out = io.BytesIO()
    kwargs = {"quality": quality}
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
    equal(imgs.sniff(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"),
          "heif", "a heic brand is recognised even without a decoder")
    equal(imgs.sniff(b"<!doctype html><html><body>login</body></html>"), None,
          "an html page is not an image, whatever the url says")
    equal(imgs.sniff(b"%PDF-1.7\n1 0 obj"), None, "a pdf is not an image")
    equal(imgs.sniff(b""), None, "empty bytes are not an image")


def test_targets():
    for name in ("jpeg", "png", "webp"):
        ok(imgs.target_supported(name), f"{name} must be writable by this build")
    ok(not imgs.target_supported("bmp"), "a format with no encoder is refused, not guessed")
    ok(not imgs.target_supported("keep"), "keep is a request, not a writable format")


def test_keep_means_the_source_format():
    equal(imgs.resolve_target("keep", "png"), ("png", ""), "a png stays a png")
    equal(imgs.resolve_target("keep", "jpeg"), ("jpeg", ""), "a jpeg stays a jpeg")
    equal(imgs.resolve_target("keep", "webp"), ("webp", ""), "a webp stays a webp")
    equal(imgs.resolve_target("keep", "gif"), ("jpeg", ""),
          "a gif cannot be written back by this build, so it lands as jpeg")
    equal(imgs.resolve_target("keep", "heif"), ("jpeg", ""), "a heic photo lands as jpeg")
    equal(imgs.resolve_target("jpg", "png"), ("jpeg", ""), "jpg is read as jpeg")
    equal(imgs.resolve_target("bmp", "png"), ("png", "unknown-format"),
          "an unknown name falls back to the source format and the row says so")


def test_a_loose_png_really_gets_smaller():
    raw = png_bytes()
    got = imgs.compress(raw, "keep")
    ok(got["bytesOut"] < got["bytesIn"],
       f'a png stored loose must shrink: {got["bytesIn"]} -> {got["bytesOut"]}')
    equal(got["format"], "png", "and it stays a png")
    equal(got["extension"], ".png", "the delivered name follows the bytes")
    equal(got["contentType"], "image/png", "the content type is the real one")
    equal((got["width"], got["height"]), (120, 90), "every pixel is kept by default")
    equal(got["resized"], False, "nothing was resized")


def test_the_target_weight_is_reached():
    raw = jpeg_bytes(size=(900, 700), colour=None)
    got = imgs.compress(raw, "keep", quality=95, target_bytes=40 * 1024)
    ok(got["bytesOut"] <= 40 * 1024,
       f'the target is met: {got["bytesOut"]} bytes against {40 * 1024}')
    equal(got["targetReached"], True, "and the row says so")
    equal(got["targetBytes"], 40 * 1024, "the row repeats the target asked")
    ok(got["bytesOut"] < got["bytesIn"], "the file is smaller than the input")
    ok(30 <= got["qualityUsed"] <= 95, f'quality stays in range: {got["qualityUsed"]}')


def test_a_target_smaller_than_quality_can_give_scales_down():
    raw = jpeg_bytes(size=(1200, 900), colour=None)
    got = imgs.compress(raw, "keep", quality=95, target_bytes=6 * 1024)
    ok(got["bytesOut"] <= 6 * 1024, f'the hard target is met: {got["bytesOut"]}')
    ok(got["width"] < 1200, f'the picture was scaled down to reach it: {got["width"]}')
    equal(got["resized"], True, "and the row admits the resize")


def test_resize_only_when_asked():
    raw = jpeg_bytes(size=(300, 200), colour=None)
    kept = imgs.compress(raw, "keep")
    equal((kept["width"], kept["height"]), (300, 200), "no limit means no resize")
    shrunk = imgs.compress(raw, "keep", max_w=100)
    equal(shrunk["width"], 100, "the width limit is applied")
    equal(shrunk["height"], 67, "and the height follows the aspect ratio")
    equal(shrunk["resized"], True, "the row says the pixels moved")


def test_never_upscales():
    raw = jpeg_bytes(size=(60, 40), colour=None)
    got = imgs.compress(raw, "keep", max_w=4000, max_h=4000)
    equal((got["width"], got["height"]), (60, 40), "a small image is never blown up")


def test_transparency_flattens_on_the_background_for_jpeg():
    raw = png_bytes((20, 20), "RGBA", (255, 0, 0, 128), level=9)
    got = imgs.compress(raw, "jpeg", background="#000000")
    with Image.open(io.BytesIO(got["data"])) as out:
        equal(out.mode, "RGB", "a jpeg has no alpha channel")
        pixel = out.getpixel((10, 10))
        # red at half alpha over black is (128, 0, 0): the composite, not the red
        ok(120 <= pixel[0] <= 136 and pixel[1] < 12 and pixel[2] < 12,
           f"the transparent half is flattened on black, got {pixel}")


def test_metadata_is_stripped_and_orientation_applied():
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90
    raw = jpeg_bytes(size=(80, 40), colour=None, exif=exif)
    got = imgs.compress(raw, "keep")
    equal((got["width"], got["height"]), (40, 80),
          "a sideways phone photo is delivered upright, so its sides swap")
    with Image.open(io.BytesIO(got["data"])) as out:
        equal(len(out.getexif()), 0, "the camera tags are gone by default")


def test_png_is_lossless_and_says_so():
    raw = png_bytes()
    got = imgs.compress(raw, "keep", quality=5)
    ok(got["bytesOut"] > 0, "a png is still written")
    equal(got["format"], "png", "and it is still a png")
    ok(got["bytesOut"] < got["bytesIn"], "deflate still does its job on a loose file")


def test_a_file_that_cannot_shrink_says_so_honestly():
    raw = png_bytes((4, 4), "RGB", (12, 34, 56), level=9)
    got = imgs.compress(raw, "keep")
    ok(got["bytesOut"] >= got["bytesIn"],
       "the engine reports the truth even when it lost: the caller must not charge")


def test_refusals_are_free_and_named():
    for bad, status in ((b"", imgs.ST_DECODE),
                        (b"<html>nope</html>", imgs.ST_DECODE),
                        (b"%PDF-1.7", imgs.ST_DECODE)):
        try:
            imgs.compress(bad)
            ok(False, f"a non image must be refused: {bad[:12]!r}")
        except imgs.ImageError as exc:
            equal(exc.status, status, f"the refusal carries its own status for {bad[:12]!r}")
            ok("not delivered" in exc.reason or "empty" in exc.reason,
               f"the reason says nothing was delivered: {exc.reason}")


def test_too_many_pixels_has_its_own_status():
    saved = imgs.MAX_PIXELS
    imgs.MAX_PIXELS = 100
    try:
        try:
            imgs.compress(png_bytes((40, 40)))
            ok(False, "an image over the pixel cap must be refused")
        except imgs.ImageError as exc:
            equal(exc.status, imgs.ST_TOO_MANY_PIXELS,
                  "too many pixels has its own status, not a decode error")
    finally:
        imgs.MAX_PIXELS = saved


def test_a_gif_is_delivered_from_its_first_frame():
    gif = io.BytesIO()
    frames = [Image.new("P", (12, 12), 1), Image.new("P", (12, 12), 2)]
    frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:])
    got = imgs.compress(gif.getvalue(), "keep")
    equal(got["sourceFormat"], "gif", "the row names what it really was")
    equal(got["format"], "jpeg", "a gif lands as jpeg, because this build cannot write gif")
    equal(got["extension"], ".jpg", "and the delivered name follows the bytes")
    with Image.open(io.BytesIO(got["data"])) as out:
        equal(out.format, "JPEG", "the delivered bytes really are a jpeg")


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
