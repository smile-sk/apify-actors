"""Page capture and image comparison.

Nothing Apify-specific here on purpose: this part can be measured locally,
on real websites, without touching the platform.
"""
import io

from PIL import Image, ImageChops
from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


async def capture(
    url: str,
    full: bool = True,
    width: int = 1440,
    height: int = 900,
    fmt: str = "png",
    wait_ms: int = 1000,
    timeout_ms: int = 60_000,
) -> bytes:
    """Open the url in Chromium and return the screenshot bytes."""
    if fmt not in ("png", "jpeg"):
        raise ValueError(f"unsupported format: {fmt}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = await browser.new_context(
                viewport={"width": int(width), "height": int(height)},
                user_agent=UA,
                locale="en-US",
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="load", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass  # a page with an endless request stream - capture it as it is
            if wait_ms:
                await page.wait_for_timeout(int(wait_ms))
            return await page.screenshot(full_page=bool(full), type=fmt)
        finally:
            await browser.close()


def diff_ratio(old: bytes, new: bytes, sens: int = 12):
    """Share of pixels differing by more than sens (0..255).

    Returns (ratio 0..1 or None, png mask of changes or None, reason or None).
    If the image sizes differ the pair cannot be compared at all, so the ratio
    is None - not 1.0, because "every pixel changed" would be a number the
    pixels do not support - and the reason says why.
    """
    a = Image.open(io.BytesIO(old)).convert("RGB")
    b = Image.open(io.BytesIO(new)).convert("RGB")
    if a.size != b.size:
        return None, None, f"size-mismatch {a.size} vs {b.size}"
    mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > sens else 0)
    hist = mask.histogram()
    changed = sum(hist[1:])
    total = a.size[0] * a.size[1]
    preview = None
    if changed:
        # The mask is painted red on black, as the listing promises.
        colored = Image.new("RGB", a.size, (0, 0, 0))
        colored.paste((255, 0, 0), mask=mask)
        buf = io.BytesIO()
        colored.save(buf, format="PNG")
        preview = buf.getvalue()
    return changed / total, preview, None
