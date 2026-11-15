"""Image compressor: one row per delivered image, free when the file did not shrink.

The money rule of this park is kept here literally: a row is written and charged by
one SDK call, and the buyer pays only for an image that really exists in the run
store and can be downloaded. A file that is not an image by its own signature, a file
that cannot be decoded, a file that is too large to hold and a line that never was an
image request all come back free with the reason on the row.

The second money rule of this actor: the buyer pays for a file that really got smaller.
When the re-encode is not smaller than the input, the original bytes are delivered
untouched and the row is free, because selling a file the buyer already had is not a
delivery. Format 'keep' means the bytes decide: no format is changed unless asked.

The free refusals are decided before any request where that is possible: a bad url,
a repeat, a line past maxImages and a private address cost nothing, not even a download.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from apify import Actor

from . import imgs
from .net import CODE_PRIVATE, host_of, host_verdict, normalize_url

EVENT_NAME = "image-compressed"
SUMMARY_STORE = "image-compressor-summary"
ZIP_KEY = "compressed-images.zip"
MAX_IMAGES_DEFAULT = 20
DOWNLOAD_TIMEOUT_S = 30.0
MIN_DOWNLOAD_BUDGET_S = 5.0
MAX_REFUSALS_IN_A_ROW = 3
MAX_REDIRECTS = 3
# The margin has to cover the whole file, not one read: DOWNLOAD_TIMEOUT_S is the
# timeout of a single read, so a server that sends a chunk a second keeps the
# connection open far longer. Measured with 13 s left: 20.3 s of work, and the old
# 12 s gate still delivered and charged. On the platform those seconds are a kill
# with no summary and no rows for the tail.
IMAGE_MARGIN_S = 25.0
# The archive is read into memory before it goes to the store, so its cap must fit
# next to the Pillow peak of one image: 40 MP takes about 600 MB of the 1024 MB.
MAX_ZIP_BYTES = 128 * 1024 * 1024

ST_OK = "OK"
ST_INVALID = "NOT_CHECKED_INVALID"
ST_DUPLICATE = "NOT_CHECKED_DUPLICATE"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TIME = "NOT_CHECKED_TIME"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_PRIVATE = "NOT_CHECKED_PRIVATE_ADDRESS"
ST_DNS = "NOT_CHECKED_DNS_ERROR"
ST_FETCH = "NOT_CHECKED_FETCH_ERROR"
ST_TOO_LARGE = "NOT_CHECKED_TOO_LARGE"
ST_NOT_IMAGE = "NOT_CHECKED_NOT_AN_IMAGE"
ST_STORE = "NOT_CHECKED_STORE_ERROR"
ST_TARGET = "NOT_CHECKED_UNSUPPORTED_TARGET"


def run_id() -> str:
    return (
        os.environ.get("ACTOR_RUN_ID")
        or (Actor.get_env() or {}).get("run_id")
        or "run"
    )


def deadline_seconds() -> float | None:
    """Seconds left before the platform kills the run, or None when unknown."""
    raw = os.environ.get("ACTOR_TIMEOUT_AT")
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - datetime.now(timezone.utc)).total_seconds()


def host_status(code: str) -> str:
    """The guard answers with a code, so a reworded reason cannot flip the diagnosis."""
    return ST_PRIVATE if code == CODE_PRIVATE else ST_DNS


def status_row(line: str, index: int, status: str, reason: str, **extra) -> dict:
    row = {
        "url": None,
        "input": line,
        "inputIndex": index,
        "status": status,
        "reason": reason,
        "billable": False,
    }
    row.update(extra)
    return row


def stopped_status(stop_reason: str | None) -> tuple[str, str]:
    if stop_reason == "spending-cap":
        return ST_CAP, "the spending cap was reached: not delivered and not charged"
    if stop_reason == "billing-price-unknown":
        return ST_BILLING, "the price could not be read: not delivered and not charged"
    if stop_reason == "deadline":
        return ST_TIME, "the platform deadline was close: not delivered and not charged"
    return ST_ERROR, "the run stopped before this image: nothing was charged"


def price_state(charging) -> tuple[bool | None, str]:
    """Can this run charge? True / False / None (unknown) and a plain reason."""
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001 - any failure means "price unknown"
        return None, f"pricing read failed: {type(exc).__name__}"
    if info is None:
        return None, "pricing info is not readable"
    is_ppe = getattr(info, "is_pay_per_event", None)
    if is_ppe is None and isinstance(info, dict):
        is_ppe = info.get("isPayPerEvent", info.get("is_pay_per_event"))
    if is_ppe is False:
        return False, "the actor is not sold per event"
    prices = getattr(info, "per_event_prices", None)
    if prices is None and isinstance(info, dict):
        prices = info.get("perEventPrices") or info.get("per_event_prices") or {}
    names: list[str] = []
    if isinstance(prices, dict):
        names = [str(key) for key in prices]
    elif isinstance(prices, list):
        for item in prices:
            name = getattr(item, "event_name", None)
            if name is None and isinstance(item, dict):
                name = item.get("eventName") or item.get("event_name")
            if name:
                names.append(str(name))
    if is_ppe is None:
        if names:
            return (EVENT_NAME in names), (
                f"{EVENT_NAME} is priced" if EVENT_NAME in names
                else f"{EVENT_NAME} is not in the price list")
        return None, "the pricing answer is not understood"
    if EVENT_NAME not in names:
        return False, f"{EVENT_NAME} is not priced, rows go out free"
    return True, f"{EVENT_NAME} is priced"


async def dataset_count(dataset) -> int | None:
    try:
        meta = await dataset.get_metadata()
    except Exception:  # noqa: BLE001 - a missing counter is not a lost row
        return None
    value = getattr(meta, "item_count", None)
    if value is None and isinstance(meta, dict):
        value = meta.get("itemCount", meta.get("item_count"))
    return value if isinstance(value, int) else None


def build_tasks(lines: list, max_images: int) -> tuple[list[dict], dict]:
    """Work in input order, with the free refusals decided before any request."""
    tasks: list[dict] = []
    seen: set[str] = set()
    counts = {"asked": len(lines), "unique": 0, "duplicates": 0, "invalid": 0,
              "blank": 0, "limit": 0}
    for index, raw in enumerate(lines):
        url, why = normalize_url(raw)
        if url is None:
            if not str(raw or "").strip():
                counts["blank"] += 1
            else:
                counts["invalid"] += 1
            tasks.append({"kind": "row", "row": status_row(
                "" if raw is None else str(raw), index, ST_INVALID,
                f"this line is not a url: {why}")})
            continue
        if url in seen:
            counts["duplicates"] += 1
            tasks.append({"kind": "row", "row": status_row(
                str(raw), index, ST_DUPLICATE,
                "the same url came earlier in the list, so it is read once and "
                "this line is free")})
            continue
        seen.add(url)
        counts["unique"] += 1
        if max_images and counts["unique"] > max_images:
            counts["limit"] += 1
            tasks.append({"kind": "row", "row": status_row(
                str(raw), index, ST_LIMIT,
                f"the list is cut at maxImages={max_images}: not read and "
                "not charged")})
            continue
        tasks.append({"kind": "work", "line": str(raw), "index": index, "url": url})
    return tasks, counts


_KEY_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!-_.'()")

IMAGE_KEY_PREFIX = "images_"

# The content type of a file that is delivered unchanged: with 'keep' the bytes decide
# the format, and a png served as image/webp is a lie in the file itself.
SOURCE_TYPES = {
    "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "avif": "image/avif", "gif": "image/gif", "tiff": "image/tiff",
    "bmp": "image/bmp", "heif": "image/heif",
}


def store_key(name: str) -> str:
    """A key-value record key the platform accepts.

    The store refused `images/<name>.<ext>` with 400 invalid-record-key: a record key
    allows only a-zA-Z0-9!-_.'() and at most 256 characters, so the slash prefix turned
    EVERY delivered image into NOT_CHECKED_STORE_ERROR on the platform, while the same
    code wrote files happily to the local disk. The prefix is flat and every key passes
    through this filter.
    """
    clean = "".join(ch if ch in _KEY_ALLOWED else "_" for ch in name)
    return clean[:256] or "image"


def short_error(exc: BaseException) -> str:
    """The message of an exception on one line, cut short: a reason must carry it."""
    text = " ".join(str(exc).split())
    return text[:140] or "no detail"


def safe_name(index: int, url: str, extension: str) -> str:
    """A stable, ascii only file name, so the zip and the store keys agree.

    The extension comes from the bytes that are really delivered: with 'keep' the
    format is only known after the image is read, and a png called .webp is a lie in
    the very file the buyer downloads.
    """
    path = unquote(urlsplit(url).path or "")
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0] if base else "image"
    clean = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in stem)
    clean = clean.strip("-.")[:40] or "image"
    ext = extension if extension.startswith(".") else ("." + extension)
    return f"{index:03d}-{clean}{ext}"


async def download(client: httpx.AsyncClient, url: str) -> dict:
    """The bytes of one file, with the redirects walked by hand.

    `follow_redirects=True` made the guard blind: the client followed a public url
    into 127.0.0.1 and the chain was read only after the bytes had arrived, so the
    request had already reached the private network. Every hop is now resolved and
    refused BEFORE its request goes out, and the chain is at most MAX_REDIRECTS long.
    """
    result: dict = {"data": None, "final_url": None, "hops": [], "status": None,
                    "reason": None, "content_type": None}
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        ok, code, why = host_verdict(host_of(current))
        if not ok:
            result["status"] = ST_PRIVATE if code == CODE_PRIVATE else ST_DNS
            result["final_url"] = current
            result["hops"] = result["hops"] + [current]
            if len(result["hops"]) > 1:
                result["reason"] = (
                    f"a redirect led to a non public address ({current}: {why}): "
                    "the file was not downloaded, nothing was delivered and nothing "
                    "was charged")
            else:
                result["reason"] = (
                    f"the address cannot be used, the file was not downloaded: {why}")
            return result
        result["hops"].append(current)
        try:
            async with client.stream("GET", current) as response:
                result["final_url"] = str(response.url)
                result["content_type"] = response.headers.get("content-type")
                location = response.headers.get("location")
                if response.is_redirect and location:
                    current = urljoin(str(response.url), location)
                    continue
                code_http = response.status_code
                if code_http >= 400:
                    result["status"] = ST_FETCH
                    result["reason"] = (f"the server answered HTTP {code_http}: not "
                                        "delivered and not charged")
                    return result
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > imgs.MAX_BYTES:
                        result["status"] = ST_TOO_LARGE
                        result["reason"] = (f"the file is larger than "
                                            f"{imgs.MAX_BYTES // (1024 * 1024)} MB: not "
                                            "delivered and not charged")
                        return result
                    chunks.append(chunk)
                result["data"] = b"".join(chunks)
                return result
        except httpx.TimeoutException:
            result["status"] = ST_FETCH
            result["reason"] = ("the download timed out before the file arrived: not "
                                "delivered and not charged")
            return result
        except Exception as exc:  # noqa: BLE001 - one file never kills the run
            result["status"] = ST_FETCH
            result["reason"] = (f"{type(exc).__name__}: the file could not be "
                                "downloaded, nothing was delivered and nothing was "
                                "charged")
            return result
    result["status"] = ST_FETCH
    result["reason"] = (f"the address redirected more than {MAX_REDIRECTS} times: not "
                        "delivered and not charged")
    return result


async def main() -> None:
    async with Actor:
        raw_input = await Actor.get_input() or {}
        dataset = await Actor.open_dataset()
        store = await Actor.open_key_value_store()
        charging = Actor.get_charging_manager()
        can_charge, charge_reason = price_state(charging)
        billable_claim = None if can_charge is None else bool(can_charge)

        lines = raw_input.get("imageUrls") or []
        if isinstance(lines, str):
            lines = [lines]
        lines = list(lines)

        # 'keep' is the default of a compressor: the format stays the one the bytes
        # already have, only the weight changes. Naming a format asks for a conversion.
        requested_format = str(raw_input.get("format") or "keep").strip().lower()
        if requested_format == "jpg":
            requested_format = "jpeg"
        try:
            quality = int(raw_input.get("quality")
                          if raw_input.get("quality") is not None else 80)
        except (TypeError, ValueError):
            quality = 80
        quality = max(1, min(quality, 100))
        try:
            target_kb = int(raw_input.get("targetKB")
                            if raw_input.get("targetKB") is not None else 0)
        except (TypeError, ValueError):
            target_kb = 0
        target_kb = max(0, min(target_kb, 25600))
        target_bytes = target_kb * 1024
        # No resize unless asked: this actor sells weight, not dimensions. A zero means
        # "keep every pixel", and the row reports the size that was kept.
        try:
            max_width = int(raw_input.get("maxWidth")
                            if raw_input.get("maxWidth") is not None else 0)
        except (TypeError, ValueError):
            max_width = 0
        max_width = max(0, min(max_width, 16000))
        try:
            max_height = int(raw_input.get("maxHeight")
                             if raw_input.get("maxHeight") is not None else 0)
        except (TypeError, ValueError):
            max_height = 0
        max_height = max(0, min(max_height, 16000))
        strip = bool(raw_input.get("stripMetadata", True))
        background = str(raw_input.get("background") or "#FFFFFF")
        try:
            max_images = int(raw_input.get("maxImages") or 0)
        except (TypeError, ValueError):
            max_images = 0
        if max_images <= 0:
            max_images = MAX_IMAGES_DEFAULT

        # A format this image cannot write stops the run before the first request:
        # converting everything and then failing to save it would burn the buyer's
        # money on files that never existed.
        if requested_format != "keep" and not imgs.target_supported(requested_format):
            await Actor.fail(status_message=(
                f"The format '{requested_format}' cannot be written by this image. Use "
                "keep, webp, jpeg, png or avif. Nothing was downloaded and nothing was "
                "charged."))
            return

        tasks, counts = build_tasks(lines, max_images)

        by_status: dict[str, int] = {}
        rows_written = 0
        delivered_images = 0
        charged_total = 0
        delivered_uncharged = 0
        delivered_unconfirmed = 0
        charge_errors = 0
        write_errors = 0
        images_processed = 0
        images_not_checked = 0
        compressed_images = 0
        already_small = 0
        target_missed = 0
        bytes_in_total = 0
        bytes_out_total = 0
        private_blocked = 0
        dns_errors = 0
        fetch_errors = 0
        too_large = 0
        not_an_image = 0
        decode_errors = 0
        too_many_pixels = 0
        store_errors = 0
        zip_names: list[str] = []
        zip_error: str | None = None
        zip_bytes = 0
        stop_reason: str | None = None
        deadline_capped = False
        unexpected_error: str | None = None
        dataset_api_count: int | None = None
        dataset_note: str | None = None
        billing_active = can_charge is True
        stopping = False
        refusals_in_a_row = 0
        failed_at = 0
        current_written = False

        def note(status: str) -> None:
            by_status[status] = by_status.get(status, 0) + 1

        async def write_free(row: dict) -> bool:
            nonlocal rows_written, write_errors, dataset_note
            nonlocal stopping, stop_reason, refusals_in_a_row, current_written
            row["billable"] = False
            try:
                await dataset.push_data(row)
            except Exception:  # noqa: BLE001
                write_errors += 1
                refusals_in_a_row += 1
                dataset_note = "a free status row was refused by the dataset"
                if refusals_in_a_row >= MAX_REFUSALS_IN_A_ROW:
                    stopping = True
                    stop_reason = stop_reason or "dataset-write-error"
                return False
            rows_written += 1
            refusals_in_a_row = 0
            current_written = True
            return True

        async def stop_row(line: str, index: int) -> None:
            status, reason = stopped_status(stop_reason)
            note(status)
            if stop_reason == "dataset-write-error":
                return
            await write_free(status_row(line, index, status, reason))

        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_S, connect=10.0)
        client = httpx.AsyncClient(
            # Redirects are walked by hand in download(): with the client following
            # them, a public url answering 302 to 127.0.0.1 was requested and only
            # then refused, which is a blind SSRF.
            follow_redirects=False, timeout=timeout, limits=limits,
            headers={"User-Agent": "apify-image-compressor/0.1 (+https://apify.com)"},
        )
        folder = tempfile.mkdtemp(prefix="image-compressor-")

        try:
            for position, task in enumerate(tasks):
                failed_at = position
                current_written = False
                if task["kind"] == "row":
                    row = task["row"]
                    written = await write_free(row)
                    note(row["status"] if written else ST_ERROR)
                    continue

                if stopping:
                    await stop_row(task["line"], task["index"])
                    images_not_checked += 1
                    continue

                left = deadline_seconds()
                if left is not None and left < IMAGE_MARGIN_S:
                    deadline_capped = True
                    stopping = True
                    stop_reason = stop_reason or "deadline"
                    note(ST_TIME)
                    images_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], ST_TIME,
                        "the platform deadline was close, nothing was delivered and "
                        "nothing was charged"))
                    continue

                if can_charge is None:
                    stopping = True
                    stop_reason = stop_reason or "billing-price-unknown"
                    note(ST_BILLING)
                    images_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], ST_BILLING,
                        f"the price could not be read ({charge_reason}): not delivered "
                        "and not charged"))
                    continue

                if can_charge and charging.is_event_charge_limit_reached(EVENT_NAME):
                    stopping = True
                    stop_reason = stop_reason or "spending-cap"
                    note(ST_CAP)
                    images_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], ST_CAP,
                        "the spending cap was reached: not delivered and not charged"))
                    continue

                url = task["url"]
                ok, host_code, why = host_verdict(host_of(url))
                if not ok:
                    status = host_status(host_code)
                    if status == ST_PRIVATE:
                        private_blocked += 1
                    else:
                        dns_errors += 1
                    note(status)
                    images_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], status,
                        f"the address cannot be used, the file was not downloaded: {why}"))
                    continue

                # The time left belongs to the whole file, not to one read: a server
                # that sends a chunk a second keeps the connection open past the
                # platform deadline, and a run killed by the clock leaves no summary.
                left = deadline_seconds()
                budget_s = (DOWNLOAD_TIMEOUT_S if left is None
                            else max(MIN_DOWNLOAD_BUDGET_S, left - IMAGE_MARGIN_S))
                try:
                    async with asyncio.timeout(budget_s):
                        got = await download(client, url)
                except TimeoutError:
                    deadline_capped = True
                    stopping = True
                    stop_reason = stop_reason or "deadline"
                    note(ST_TIME)
                    images_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], ST_TIME,
                        "the file did not arrive within the time left before the "
                        "platform deadline: nothing was delivered and nothing was "
                        "charged"))
                    continue
                data = got.get("data")
                final_url = got.get("final_url")
                if got["status"] is not None:
                    status, reason = got["status"], got["reason"]
                    if status == ST_PRIVATE:
                        private_blocked += 1
                    elif status == ST_DNS:
                        dns_errors += 1
                    elif status == ST_FETCH:
                        fetch_errors += 1
                    else:
                        too_large += 1
                    note(status)
                    images_not_checked += 1
                    await write_free(status_row(task["line"], task["index"], status,
                                                reason))
                    continue

                source_format = imgs.sniff(data or b"")
                if source_format is None:
                    not_an_image += 1
                    note(ST_NOT_IMAGE)
                    images_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], ST_NOT_IMAGE,
                        "the bytes are not an image, whatever the url or the content "
                        "type says: not delivered and not charged"))
                    continue

                try:
                    converted = imgs.compress(
                        data or b"", requested_format, quality=quality, max_w=max_width,
                        max_h=max_height, target_bytes=target_bytes, strip=strip,
                        background=background)
                except imgs.ImageError as exc:
                    note(exc.status)
                    if exc.status == imgs.ST_TOO_MANY_PIXELS:
                        too_many_pixels += 1
                        images_not_checked += 1
                    else:
                        decode_errors += 1
                        images_processed += 1
                    await write_free(status_row(
                        task["line"], task["index"], exc.status,
                        f"{exc.reason}: not delivered and not charged",
                        sourceFormat=source_format))
                    continue
                except Exception as exc:  # noqa: BLE001 - one file never kills the run
                    decode_errors += 1
                    images_processed += 1
                    note(ST_ERROR)
                    await write_free(status_row(
                        task["line"], task["index"], ST_ERROR,
                        f"{type(exc).__name__}: this image could not be compressed, "
                        "nothing was delivered and nothing was charged",
                        sourceFormat=source_format))
                    continue

                images_processed += 1
                # The second money rule: a file that did not get smaller is not a
                # delivery of anything new, so the original bytes go out untouched and
                # the row is free. Selling a file the buyer already had is not a sale.
                smaller = converted["bytesOut"] < converted["bytesIn"]
                if smaller:
                    payload = converted["data"]
                    content_type = converted["contentType"]
                    extension = converted["extension"]
                else:
                    payload = data or b""
                    content_type = SOURCE_TYPES.get(converted["sourceFormat"],
                                                    "application/octet-stream")
                    extension = "." + converted["sourceFormat"]
                name = safe_name(task["index"], final_url or url, extension)
                key = store_key(f"{IMAGE_KEY_PREFIX}{name}")
                try:
                    await store.set_value(key, payload, content_type=content_type)
                except Exception as exc:  # noqa: BLE001 - no file, no money
                    store_errors += 1
                    note(ST_STORE)
                    await write_free(status_row(
                        task["line"], task["index"], ST_STORE,
                        f"{type(exc).__name__}: {short_error(exc)}. The file could not be "
                        "written to the store, so there is nothing to download: not "
                        "delivered and not charged",
                        sourceFormat=source_format))
                    continue

                try:
                    with open(os.path.join(folder, name), "wb") as fh:
                        fh.write(payload)
                    zip_names.append(name)
                except Exception:  # noqa: BLE001 - the file itself is already in the store
                    zip_error = "one delivered file could not be added to the archive"

                bytes_out = len(payload)
                saved = None
                if converted["bytesIn"]:
                    saved = round((converted["bytesIn"] - bytes_out) * 100.0
                                  / converted["bytesIn"], 1)
                row = {
                    "url": final_url or url,
                    "input": task["line"],
                    "inputIndex": task["index"],
                    "status": ST_OK,
                    "reason": None,
                    "billable": False,
                    "compressed": smaller,
                    "format": converted["format"],
                    "sourceFormat": converted["sourceFormat"],
                    "formatNote": converted["formatNote"] or None,
                    "width": converted["width"],
                    "height": converted["height"],
                    "widthIn": converted["widthIn"],
                    "heightIn": converted["heightIn"],
                    "bytesIn": converted["bytesIn"],
                    "bytesOut": bytes_out,
                    "savedBytes": converted["bytesIn"] - bytes_out,
                    "savedPercent": saved,
                    "targetKB": target_kb or None,
                    "targetReached": converted["targetReached"],
                    "qualityUsed": converted["qualityUsed"],
                    "resized": converted["resized"],
                    "contentType": content_type,
                    "outputKey": key,
                    "maxWidth": max_width or None,
                    "maxHeight": max_height or None,
                    "metadataStripped": strip,
                }
                if target_bytes > 0 and not converted["targetReached"]:
                    target_missed += 1
                bytes_in_total += converted["bytesIn"]
                bytes_out_total += bytes_out

                if can_charge and smaller:
                    try:
                        result = await Actor.push_data(
                            {**row, "billable": billable_claim},
                            charged_event_name=EVENT_NAME,
                        )
                    except Exception as exc:  # noqa: BLE001
                        charge_errors += 1
                        refusals_in_a_row = 0
                        # One exception, two different outcomes: the row was written
                        # and the charge failed, or the dataset refused the row and
                        # nothing exists. The counter is the only proof; a missing
                        # growth is not proof of absence (metadata lags), so a row is
                        # never claimed as delivered on a zero.
                        after = await dataset_count(dataset)
                        if after is not None and after > rows_written:
                            delivered_uncharged += 1
                            rows_written += 1
                            delivered_images += 1
                            compressed_images += 1
                            note(ST_OK)
                            dataset_note = (
                                f"a paid row raised {type(exc).__name__}, the row is "
                                "in the dataset and it was not charged")
                        else:
                            delivered_unconfirmed += 1
                            note(ST_ERROR)
                            dataset_note = (
                                f"a paid row raised {type(exc).__name__} and its "
                                "delivery could not be confirmed: not counted as "
                                "delivered and nothing was charged")
                        stopping = True
                        stop_reason = "charge-error"
                        continue
                    charged = int(getattr(result, "charged_count", 0) or 0)
                    limit_reached = bool(getattr(result, "event_charge_limit_reached", False))
                    if charged <= 0:
                        status = ST_CAP if limit_reached else ST_ERROR
                        note(status)
                        await write_free(status_row(
                            task["line"], task["index"], status,
                            "the spending cap was reached: the file is in the store but "
                            "it was not charged" if limit_reached else
                            "nothing was written and nothing was charged",
                            outputKey=key, sourceFormat=source_format))
                        stopping = True
                        stop_reason = "spending-cap" if limit_reached else "charge-error"
                        continue
                    rows_written += 1
                    current_written = True
                    delivered_images += 1
                    compressed_images += 1
                    charged_total += charged
                    refusals_in_a_row = 0
                    note(ST_OK)
                    if limit_reached:
                        stopping = True
                        stop_reason = "spending-cap"
                    continue

                row["billable"] = False
                written = await write_free(row)
                if written:
                    delivered_images += 1
                    if smaller:
                        compressed_images += 1
                    else:
                        already_small += 1
                note(ST_OK if written else ST_ERROR)
        except Exception as exc:  # noqa: BLE001 - a crash must not lose the summary
            unexpected_error = f"{type(exc).__name__}: {exc}"[:300]
            stop_reason = stop_reason or "unexpected-error"
            for position, rest in enumerate(tasks[failed_at:], start=failed_at):
                if position == failed_at and current_written:
                    # the row of the task that died is already in the dataset: a second
                    # row for the same line is worse than a named gap
                    continue
                if rest.get("kind") == "row":
                    # A line that never was an image request keeps its own status:
                    # rewriting it as ERROR blamed the buyer for our crash.
                    row = rest.get("row") or {}
                    note(row.get("status") or ST_ERROR)
                    await write_free(row)
                    continue
                note(ST_ERROR)
                await write_free(status_row(
                    rest.get("line") or "", rest.get("index") or 0, ST_ERROR,
                    "the run stopped on an unexpected error before this image: "
                    "nothing was charged"))
            # The buckets close by definition: an image that was attempted is either
            # processed or not checked, whatever the crash interrupted.
            images_not_checked = max(0, (counts["unique"] - counts["limit"])
                                     - images_processed)
        finally:
            await client.aclose()

        images_in_zip = 0
        if zip_names:
            try:
                zip_path = os.path.join(folder, ZIP_KEY)
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
                    for name in zip_names:
                        archive.write(os.path.join(folder, name), arcname=name)
                size = os.path.getsize(zip_path)
                if size > MAX_ZIP_BYTES:
                    zip_error = (
                        f"the archive was skipped: {size} bytes is more than "
                        f"{MAX_ZIP_BYTES // (1024 * 1024)} MB, every file is still in the "
                        "store under its outputKey")
                else:
                    with open(zip_path, "rb") as fh:
                        await store.set_value(ZIP_KEY, fh.read(),
                                              content_type="application/zip")
                    zip_bytes = size
                    images_in_zip = len(zip_names)
            except Exception as exc:  # noqa: BLE001 - the delivered files stay delivered
                zip_error = (f"{type(exc).__name__}: the archive could not be built, "
                             "every file is still in the store under its outputKey")
        shutil.rmtree(folder, ignore_errors=True)
        dataset_api_count = await dataset_count(dataset)

        asked = counts["asked"]
        # A bad url, a repeat, a blank line and a line past maxImages were never image
        # requests. Without a bucket of their own the invariant looked broken on every
        # list that carried one of them.
        lines_not_images = (counts["invalid"] + counts["blank"]
                            + counts["duplicates"] + counts["limit"])
        images_attempted = counts["unique"] - counts["limit"]
        if images_processed + images_not_checked != images_attempted:
            by_status["imageMismatch"] = (
                images_attempted - (images_processed + images_not_checked))
        if lines_not_images + images_attempted != asked:
            by_status["lineCountMismatch"] = asked - lines_not_images - images_attempted

        summary = {
            "requested": asked,
            "uniqueImages": counts["unique"],
            "duplicates": counts["duplicates"],
            "invalidLines": counts["invalid"],
            "blankLines": counts["blank"],
            "cutByMaxImages": counts["limit"],
            "processedImages": images_processed,
            "imagesAttempted": images_attempted,
            "linesThatAreNotImages": lines_not_images,
            "notCheckedImages": images_not_checked,
            "deliveredImages": delivered_images,
            "rowsWritten": rows_written,
            "chargedEvents": charged_total if billing_active else 0,
            "billingActive": billing_active,
            "billingReason": charge_reason,
            "deliveredUncharged": delivered_uncharged,
            "deliveredUnconfirmed": delivered_unconfirmed,
            "chargeErrors": charge_errors,
            "writeErrors": write_errors,
            "stopReason": stop_reason,
            "unexpectedError": unexpected_error,
            "byStatus": by_status,
            "compressedImages": compressed_images,
            "alreadySmallImages": already_small,
            "targetMissed": target_missed,
            "targetKB": target_kb or None,
            "bytesInTotal": bytes_in_total,
            "bytesOutTotal": bytes_out_total,
            "savedBytesTotal": bytes_in_total - bytes_out_total,
            "savedPercentTotal": (round((bytes_in_total - bytes_out_total) * 100.0
                                        / bytes_in_total, 1) if bytes_in_total else None),
            "zipKey": ZIP_KEY if images_in_zip else None,
            "zipBytes": zip_bytes,
            "imagesInZip": images_in_zip,
            "zipError": zip_error,
            "privateBlocked": private_blocked,
            "dnsErrors": dns_errors,
            "fetchErrors": fetch_errors,
            "tooLarge": too_large,
            "notAnImage": not_an_image,
            "decodeErrors": decode_errors,
            "tooManyPixels": too_many_pixels,
            "storeErrors": store_errors,
            "format": requested_format,
            "quality": quality,
            "maxWidth": max_width or None,
            "maxHeight": max_height or None,
            "stripMetadata": strip,
            "maxImages": max_images,
            "deadlineCapped": deadline_capped,
            "datasetItemCount": rows_written,
            "datasetItemCountApi": dataset_api_count,
            "datasetItemCountNote": dataset_note,
            "priceEvent": EVENT_NAME,
            "runId": run_id(),
            "finishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if dataset_api_count is not None and dataset_api_count < rows_written:
            lag = f"metadata lag: expected {rows_written}, reports {dataset_api_count}"
            summary["datasetItemCountNote"] = (
                f"{dataset_note}; {lag}" if dataset_note else lag
            )

        store_summary = await Actor.open_key_value_store(name=SUMMARY_STORE)
        await store_summary.set_value(f"RUN_SUMMARY-{run_id()}", summary)
        default_store = await Actor.open_key_value_store()
        await default_store.set_value("RUN_SUMMARY", summary)

        if charged_total:
            billed = "compressed and charged"
        elif delivered_uncharged:
            billed = "delivered without a charge"
        elif compressed_images:
            billed = "compressed, nothing charged"
        else:
            billed = "delivered, nothing shrank"
        text = (
            f"{images_attempted} image(s) from {asked} line(s), "
            f"{delivered_images} {billed}, {already_small} already small and free, "
            f"{images_not_checked} not checked"
        )
        if delivered_unconfirmed:
            text += (f", {delivered_unconfirmed} row(s) whose delivery could not be "
                     "confirmed")
        if bytes_in_total:
            text += (f", {bytes_out_total} of {bytes_in_total} bytes kept")
        if not billing_active:
            text += f" (billing is off: {charge_reason})"
        if stop_reason:
            text += f", stopped: {stop_reason}"
        await Actor.set_status_message(text)

        if asked == 0:
            await Actor.fail(status_message=(
                "No image urls were sent: nothing to convert, nothing charged. Put one "
                "http url per line into the imageUrls field."))
        if unexpected_error:
            await Actor.fail(status_message=(
                f"The run stopped on an unexpected error ({unexpected_error}). "
                f"{delivered_images} image(s) delivered, {images_not_checked} not "
                f"checked, {charged_total if billing_active else 0} charged."))
        if asked > 0 and delivered_images == 0:
            breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
            await Actor.fail(status_message=(
                f"Nothing could be compressed from {asked} line(s): "
                f"{breakdown or 'no rows written'}. "
                f"{images_attempted} image(s) were attempted and {lines_not_images} "
                "line(s) were not image requests. Nothing was charged."))


if __name__ == "__main__":
    asyncio.run(main())
