"""Tests for the image diff, which is what the buyer actually pays for.

The browser path needs Chromium, so it is not exercised here; instead the test
checks that a bad format is refused BEFORE any browser is launched (that is the
point of the guard, and it is testable without a browser).

Run directly:  python tests/test_shot.py
"""
import asyncio
import io
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

# playwright is installed in the Actor image, not on a dev machine. diff_ratio
# does not touch it, so a stub keeps this file runnable anywhere.
try:  # pragma: no cover - depends on the machine
    import playwright.async_api  # noqa: F401
except Exception:  # pragma: no cover
    fake = types.ModuleType("playwright")
    fake_api = types.ModuleType("playwright.async_api")

    class _NeverCalled:
        def __init__(self, *args, **kwargs):
            raise AssertionError("browser must not be launched on a bad format")

    fake_api.async_playwright = _NeverCalled
    fake.async_api = fake_api
    sys.modules["playwright"] = fake
    sys.modules["playwright.async_api"] = fake_api

from PIL import Image  # noqa: E402

from shot import capture, diff_ratio  # noqa: E402


def _png(size, boxes=(), color=(255, 255, 255)):
    image = Image.new("RGB", size, color)
    for box in boxes:
        for x in range(box[0], box[2]):
            for y in range(box[1], box[3]):
                image.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_identical_images_give_zero_and_no_mask():
    image = _png((100, 100), boxes=[(0, 0, 10, 10)])
    ratio, preview, reason = diff_ratio(image, image)
    assert ratio == 0.0, ratio
    assert preview is None
    assert reason is None


def test_changed_area_is_measured_not_guessed():
    old = _png((100, 100))
    new = _png((100, 100), boxes=[(0, 0, 10, 10)])  # 100 of 10 000 pixels
    ratio, preview, reason = diff_ratio(old, new)
    assert abs(ratio - 0.01) < 1e-9, ratio
    assert reason is None
    assert preview is not None


def test_mask_is_painted_red_on_black():
    old = _png((20, 20))
    new = _png((20, 20), boxes=[(0, 0, 5, 5)])
    _ratio, preview, _reason = diff_ratio(old, new)
    mask = Image.open(io.BytesIO(preview)).convert("RGB")
    assert mask.getpixel((0, 0)) == (255, 0, 0)
    assert mask.getpixel((10, 10)) == (0, 0, 0)


def test_small_difference_below_sensitivity_is_ignored():
    old = _png((10, 10))
    pixels = list(Image.open(io.BytesIO(old)).getdata())
    pixels[0] = (250, 250, 250)  # 5 levels of noise, sens is 12
    noisy = Image.new("RGB", (10, 10))
    noisy.putdata(pixels)
    buf = io.BytesIO()
    noisy.save(buf, format="PNG")
    ratio, preview, _reason = diff_ratio(old, buf.getvalue())
    assert ratio == 0.0
    assert preview is None


def test_size_mismatch_returns_none_not_one():
    # "Every pixel changed" would be a number the pixels do not support.
    old = _png((100, 100))
    new = _png((100, 101))
    ratio, preview, reason = diff_ratio(old, new)
    assert ratio is None
    assert preview is None
    assert reason == "size-mismatch (100, 100) vs (100, 101)", reason


def test_bad_format_is_refused_before_launching_browser():
    try:
        asyncio.run(capture("https://example.com", fmt="gif"))
    except ValueError as exc:
        assert "unsupported format" in str(exc)
    else:
        raise AssertionError("capture accepted fmt='gif'")


def run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:
            failed.append((name, exc))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
