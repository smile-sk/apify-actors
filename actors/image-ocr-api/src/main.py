"""Text from a picture, one row per image, charged only when there is text.

The money rule of the park is kept literally: a row is written and charged by one SDK
call, and a picture that produced no text is never charged. The top of this niche is
built on Google Lens, which is somebody else's free service behind a rate limit; the
recogniser here lives inside the image, so the answer does not depend on a widget that
can start asking for a captcha tomorrow.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from apify import Actor

from . import imgs, ocr
from .net import CODE_PRIVATE, host_of, host_verdict, normalize_url

EVENT_NAME = "image-ocr"
SUMMARY_STORE = "image-ocr-api-summary"
MAX_IMAGES_DEFAULT = 10
DOWNLOAD_TIMEOUT_S = 30.0
MAX_REFUSALS_IN_A_ROW = 3
MAX_REDIRECTS = 3
MIN_DOWNLOAD_BUDGET_S = 5.0
IMAGE_MARGIN_S = 20.0
MAX_OCR_S = 240.0
MAX_PIXELS_DEFAULT = imgs.MAX_PIXELS

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
ST_BROKEN = "NOT_CHECKED_BROKEN_IMAGE"
ST_NO_TEXT = "NOT_CHECKED_NO_TEXT"


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
                f"the list is cut at maxImages={max_images}: not read and not charged")})
            continue
        tasks.append({"kind": "work", "line": str(raw), "index": index, "url": url})
    return tasks, counts


async def download(client: httpx.AsyncClient, url: str) -> dict:
    """The bytes of one picture, with the redirects walked by hand.

    The signature decides, not the content type and not the file name: a link that
    answers with an html login page is not a picture, and that exact page was billed
    as a document once in this park before the check existed.

    `follow_redirects=True` made the guard blind: the client followed a public url into
    127.0.0.1, the chain was read only after the bytes had arrived, and the picture was
    read and CHARGED, which is money for the inner network and a blind SSRF. Every hop
    is now resolved and refused BEFORE its request goes out, and the chain is at most
    MAX_REDIRECTS long.
    """
    result: dict = {"data": None, "final_url": None, "hops": [], "status": None,
                    "reason": None, "content_type": None}
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        ok, code, why = host_verdict(host_of(current) or "")
        if not ok:
            result["status"] = ST_PRIVATE if code == CODE_PRIVATE else ST_DNS
            result["final_url"] = current
            result["hops"] = result["hops"] + [current]
            if len(result["hops"]) > 1:
                result["reason"] = (
                    f"a redirect led to an address that cannot be used ({current}: "
                    f"{why}): that hop was never requested, nothing was delivered and "
                    "nothing was charged")
            else:
                result["reason"] = (
                    f"the address cannot be used, the picture was not downloaded: "
                    f"{why}. Nothing was delivered and nothing was charged")
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
                data = b"".join(chunks)
                result["data"] = data
                content_type = (result["content_type"] or "").lower()
                if imgs.sniff(data) is None:
                    if "html" in content_type:
                        result["status"] = ST_NOT_IMAGE
                        result["reason"] = ("the link answered with an html page, not an "
                                            "image: not delivered and not charged")
                    else:
                        result["status"] = ST_NOT_IMAGE
                        result["reason"] = ("the bytes are not a jpeg, png, webp, gif, "
                                            "bmp, tiff or heic image: not delivered and "
                                            "not charged")
                    return result
                return result
        except httpx.TimeoutException:
            result["status"] = ST_FETCH
            result["reason"] = ("the download timed out before the picture arrived: not "
                                "delivered and not charged")
            return result
        except Exception as exc:  # noqa: BLE001 - one image never kills the run
            result["status"] = ST_FETCH
            result["reason"] = (f"{type(exc).__name__}: the picture could not be "
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
        store = await Actor.open_key_value_store(name=SUMMARY_STORE)
        default_store = await Actor.open_key_value_store()

        started = datetime.now(timezone.utc)
        lines = raw_input.get("imageUrls") or []
        if isinstance(lines, str):
            lines = [lines]
        lines = [str(x) for x in lines]
        try:
            max_images = int(raw_input.get("maxImages") or 0)
        except (TypeError, ValueError):
            max_images = 0
        max_images = max_images if max_images > 0 else MAX_IMAGES_DEFAULT
        lang = str(raw_input.get("languages") or "eng").strip() or "eng"
        want_words = bool(raw_input.get("includeWordBoxes", True))
        try:
            max_pixels = int(raw_input.get("maxPixels") or 0)
        except (TypeError, ValueError):
            max_pixels = 0
        max_pixels = max_pixels if max_pixels > 0 else MAX_PIXELS_DEFAULT

        engine_ok, engine_note = ocr.available()
        if not engine_ok:
            await Actor.fail(status_message=(
                "the tesseract recognition engine is not installed in this image, "
                "so no picture can be read: nothing was charged"))
            return
        have = ocr.languages()
        wanted = [part for part in lang.split("+") if part]
        missing = [part for part in wanted if have and part not in have]
        if missing:
            await Actor.fail(status_message=(
                f"the language pack {', '.join(missing)} is not installed in this image: "
                "nothing was read and nothing was charged"))
            return

        charging = Actor.get_charging_manager()
        can_charge, price_reason = price_state(charging)

        tasks, counts = build_tasks(lines, max_images)

        by_status: dict[str, int] = {}
        note_cache: dict[str, int] = {}

        def note(status: str) -> None:
            by_status[status] = by_status.get(status, 0) + 1

        rows_written = 0
        delivered_images = 0
        charged_images = 0
        delivered_uncharged = 0
        delivered_unconfirmed = 0
        charged_total = 0
        charge_errors = 0
        write_errors = 0
        refusals_in_a_row = 0
        images_processed = 0
        images_not_checked = 0
        images_attempted = 0
        texts_read = 0
        chars_total = 0
        stop_reason: str | None = None
        stopping = False
        unexpected_error: str | None = None
        dataset_note: str | None = None
        failed_at = len(tasks)
        current_written = False

        async def write_free(row: dict) -> bool:
            """A row with no money: the counter of refusals decides when to stop."""
            nonlocal rows_written, write_errors, refusals_in_a_row, stopping, stop_reason
            nonlocal current_written
            try:
                await dataset.push_data(row)
                rows_written += 1
                refusals_in_a_row = 0
                current_written = True
                return True
            except Exception as exc:  # noqa: BLE001
                write_errors += 1
                refusals_in_a_row += 1
                if refusals_in_a_row >= MAX_REFUSALS_IN_A_ROW:
                    stopping = True
                    stop_reason = stop_reason or "dataset-write-error"
                    dataset_note = (f"{MAX_REFUSALS_IN_A_ROW} dataset writes failed in a "
                                    f"row, the last one {type(exc).__name__}: the run "
                                    "stopped so the rest are not lost silently")
                return False

        headers = {"user-agent": "apify-image-ocr/1.0 (+https://apify.com)"}
        client = httpx.AsyncClient(
            headers=headers,
            # Redirects are walked by hand in download(): with the client following
            # them, a public url answering 302 into 127.0.0.1 was requested, read and
            # charged, and the guard saw the chain only afterwards.
            follow_redirects=False,
            timeout=DOWNLOAD_TIMEOUT_S,
        )
        try:
            for position, task in enumerate(tasks):
                failed_at = position
                current_written = False
                if stopping:
                    break
                if task["kind"] == "row":
                    note(task["row"]["status"])
                    if task["row"]["status"] in (ST_INVALID, ST_DUPLICATE, ST_LIMIT):
                        await write_free(task["row"])
                    continue

                line = task["line"]
                index = task["index"]
                url = task["url"]
                images_attempted += 1

                left = deadline_seconds()
                if left is not None and left < IMAGE_MARGIN_S:
                    note(ST_TIME)
                    await write_free(status_row(
                        line, index, ST_TIME,
                        "the platform deadline is close: not read and not charged"))
                    stopping = True
                    stop_reason = stop_reason or "deadline"
                    images_not_checked += 1
                    failed_at = position + 1
                    break

                host_ok, host_code, host_why = host_verdict(host_of(url) or "")
                if not host_ok:
                    status = host_status(host_code)
                    note(status)
                    await write_free(status_row(
                        line, index, status,
                        f"{host_why}: not fetched and not charged"))
                    images_not_checked += 1
                    continue

                if can_charge is None:
                    status, reason = stopped_status("billing-price-unknown")
                    note(status)
                    await write_free(status_row(
                        line, index, status,
                        f"the price could not be read ({price_reason}): not delivered "
                        "and not charged"))
                    images_not_checked += 1
                    stopping = True
                    stop_reason = stop_reason or "billing-price-unknown"
                    failed_at = position + 1
                    break

                budget = (DOWNLOAD_TIMEOUT_S if left is None
                          else max(MIN_DOWNLOAD_BUDGET_S, left - IMAGE_MARGIN_S))
                try:
                    async with asyncio.timeout(budget):
                        got = await download(client, url)
                except (TimeoutError, asyncio.TimeoutError):
                    note(ST_TIME)
                    await write_free(status_row(
                        line, index, ST_TIME,
                        "the download did not fit the time left before the deadline: "
                        "not delivered and not charged"))
                    images_not_checked += 1
                    stopping = True
                    stop_reason = stop_reason or "deadline"
                    failed_at = position + 1
                    break

                if got["status"]:
                    note(got["status"])
                    await write_free(status_row(line, index, got["status"],
                                                got["reason"] or "not delivered"))
                    images_not_checked += 1
                    continue

                try:
                    picture = imgs.to_png(got["data"], max_pixels)
                except Exception as exc:  # noqa: BLE001 - a broken file is a status
                    note(ST_BROKEN)
                    await write_free(status_row(
                        line, index, ST_BROKEN,
                        f"{exc}: not delivered and not charged"))
                    images_not_checked += 1
                    continue

                left = deadline_seconds()
                # The recogniser gets what is left of the run, not a fixed 240 s: a
                # picture started a few seconds before the kill used to be read by
                # nobody, and the platform takes the run down without a summary.
                ocr_budget = MAX_OCR_S if left is None else max(3.0, left - 8.0)
                ocr_budget = min(MAX_OCR_S, ocr_budget)
                try:
                    found = ocr.recognize(picture["png"], lang, want_words, ocr_budget)
                except ocr.OcrError as exc:
                    note(ST_ERROR)
                    await write_free(status_row(
                        line, index, ST_ERROR,
                        f"{exc}: not delivered and not charged",
                        imageFormat=picture["format"],
                        imageWidth=picture["width"],
                        imageHeight=picture["height"]))
                    images_not_checked += 1
                    continue

                text = found.get("text") or ""
                row = {
                    "url": got["final_url"] or url,
                    "input": line,
                    "inputIndex": index,
                    "status": ST_OK,
                    "reason": None,
                    "billable": False,
                    "imageFormat": picture["format"],
                    "imageWidth": picture["width"],
                    "imageHeight": picture["height"],
                    "inputWidth": picture["inputWidth"],
                    "inputHeight": picture["inputHeight"],
                    "rotated": picture["rotated"],
                    "scaled": picture["scaled"],
                    "language": lang,
                    "chars": len(text),
                    "lines": len([ln for ln in text.splitlines() if ln.strip()]),
                    "confidence": found.get("confidence"),
                    "text": text,
                    "words": found.get("words") or [],
                }

                if not text.strip():
                    note(ST_NO_TEXT)
                    # The row was built as OK a moment ago; a picture nobody read must
                    # not leave the door claiming it was delivered.
                    row["status"] = ST_NO_TEXT
                    row["reason"] = ("no text was found on this picture: the row is "
                                     "delivered free")
                    written = await write_free(row)
                    images_processed += 1
                    if written:
                        delivered_images += 1
                    continue

                texts_read += 1
                chars_total += len(text)

                if not can_charge:
                    note(ST_OK)
                    row["reason"] = f"billing is off ({price_reason}): delivered free"
                    written = await write_free(row)
                    images_processed += 1
                    if written:
                        delivered_images += 1
                    continue

                try:
                    if charging.is_event_charge_limit_reached(EVENT_NAME):
                        note(ST_CAP)
                        await write_free(status_row(
                            line, index, ST_CAP,
                            "the spending cap was reached: not delivered and not "
                            "charged",
                            chars=len(text)))
                        images_processed += 1
                        stopping = True
                        stop_reason = stop_reason or "spending-cap"
                        failed_at = position + 1
                        break
                except Exception:  # noqa: BLE001 - the SDK check is a guard, not the truth
                    pass

                try:
                    result = await Actor.push_data(
                        {**row, "billable": True},
                        charged_event_name=EVENT_NAME,
                    )
                except Exception as exc:  # noqa: BLE001
                    charge_errors += 1
                    refusals_in_a_row = 0
                    # One exception, two different outcomes: the row was written and
                    # the charge failed, or the dataset refused the row and nothing
                    # exists. The counter is the only proof, and a zero right after a
                    # write is a metadata lag rather than an absence, so a missing
                    # growth is never read as delivered.
                    after = await dataset_count(dataset)
                    if after is not None and after > rows_written:
                        delivered_uncharged += 1
                        rows_written += 1
                        delivered_images += 1
                        current_written = True
                        note(ST_OK)
                        dataset_note = (
                            f"a paid row raised {type(exc).__name__}, the row is in "
                            "the dataset and it was not charged")
                    else:
                        delivered_unconfirmed += 1
                        note(ST_ERROR)
                        dataset_note = (
                            f"a paid row raised {type(exc).__name__} and its delivery "
                            "could not be confirmed: not counted as delivered, nothing "
                            "was charged")
                    images_processed += 1
                    stopping = True
                    stop_reason = "charge-error"
                    failed_at = position + 1
                    break

                charged = int(getattr(result, "charged_count", 0) or 0)
                limit_reached = bool(getattr(result, "event_charge_limit_reached", False))
                if charged <= 0:
                    status = ST_CAP if limit_reached else ST_ERROR
                    note(status)
                    await write_free(status_row(
                        line, index, status,
                        "the spending cap was reached: not delivered and not charged"
                        if limit_reached else
                        "nothing was written and nothing was charged",
                        chars=len(text)))
                    images_processed += 1
                    stopping = True
                    stop_reason = "spending-cap" if limit_reached else "charge-error"
                    failed_at = position + 1
                    break

                rows_written += 1
                delivered_images += 1
                charged_images += 1
                charged_total += charged
                images_processed += 1
                refusals_in_a_row = 0
                note(ST_OK)
                if limit_reached:
                    stopping = True
                    stop_reason = "spending-cap"
                    failed_at = position + 1
        except Exception as exc:  # noqa: BLE001 - a crash must not lose the summary
            unexpected_error = f"{type(exc).__name__}: {exc}"[:300]
            stop_reason = stop_reason or "unexpected-error"
            # A crash on one picture used to leave the rest of the list without a row:
            # failed_at stayed at len(tasks) and the slice was empty, so three pictures
            # with a failure on the second produced one row and an imageMismatch of 2.
            # Every remaining line gets its row: its own status for a line that never
            # was a picture request, NOT_CHECKED_ERROR for one that was not reached.
            for offset, rest in enumerate(tasks[failed_at:], start=failed_at):
                if offset == failed_at and current_written:
                    # this line's row is already in the dataset: a second row for the
                    # same input is worse than a named gap
                    continue
                if rest.get("kind") == "row":
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
            dataset_api_count = await dataset_count(dataset)
        # A stopped run still closes every line: the rows after the stop are free
        # and carry the reason, because a missing row reads as a lost document.
        if stopping and not unexpected_error:
            status, reason = stopped_status(stop_reason)
            for rest_task in tasks[failed_at:]:
                if rest_task.get("kind") == "row":
                    row = rest_task.get("row") or {}
                    note(row.get("status") or ST_ERROR)
                    await write_free(row)
                else:
                    note(status)
                    await write_free(status_row(
                        rest_task.get("line") or "", rest_task.get("index") or 0,
                        status, reason))
                    images_not_checked += 1

        asked = counts["asked"]
        lines_not_images = (counts["invalid"] + counts["blank"]
                            + counts["duplicates"] + counts["limit"])
        attempted_expected = counts["unique"] - counts["limit"]
        if images_processed + images_not_checked != attempted_expected:
            by_status["imageMismatch"] = (
                attempted_expected - (images_processed + images_not_checked))
        if lines_not_images + attempted_expected != asked:
            by_status["lineCountMismatch"] = asked - lines_not_images - attempted_expected

        summary = {
            "requested": asked,
            "maxImages": max_images,
            "languages": lang,
            "includeWordBoxes": want_words,
            "maxPixels": max_pixels,
            "imagesAttempted": attempted_expected,
            "imagesProcessed": images_processed,
            "imagesNotChecked": images_not_checked,
            "linesThatAreNotImages": lines_not_images,
            "deliveredImages": delivered_images,
            "chargedImages": charged_images,
            "deliveredUncharged": delivered_uncharged,
            "deliveredUnconfirmed": delivered_unconfirmed,
            "chargedEvents": charged_total,
            "chargeErrors": charge_errors,
            "writeErrors": write_errors,
            "rowsWritten": rows_written,
            "textsRead": texts_read,
            "charactersRead": chars_total,
            "billingActive": can_charge is not False,
            "pricingEventPriced": can_charge,
            "pricingReason": price_reason,
            "engine": engine_note,
            "engineLanguages": sorted(have)[:12],
            "stopReason": stop_reason,
            "unexpectedError": unexpected_error,
            "byStatus": by_status,
            "runId": run_id(),
            "startedAt": started.isoformat(),
            "finishedAt": datetime.now(timezone.utc).isoformat(),
        }
        if dataset_api_count is None:
            summary["datasetItemCountApi"] = None
            summary["datasetItemCountNote"] = dataset_note or (
                "the dataset counter could not be read: it does not mean a row was lost")
        else:
            summary["datasetItemCountApi"] = dataset_api_count
            summary["datasetItemCount"] = rows_written
            if dataset_api_count != rows_written:
                summary["datasetItemCountNote"] = dataset_note or (
                    f"metadata lag: this run wrote {rows_written} rows, the counter "
                    f"reports {dataset_api_count}")

        await store.set_value(f"RUN_SUMMARY-{run_id()}", summary)
        await default_store.set_value("RUN_SUMMARY", summary)

        if delivered_uncharged:
            text_line = (f"{delivered_images} image(s) delivered, "
                         f"{delivered_uncharged} of them without a charge")
        elif charged_total:
            text_line = f"{delivered_images} image(s) read and charged"
        else:
            text_line = f"{delivered_images} image(s) delivered, nothing charged"
        if delivered_unconfirmed:
            text_line += (f", {delivered_unconfirmed} row(s) whose delivery could not "
                          "be confirmed")
        if images_not_checked or lines_not_images:
            text_line += f", {images_not_checked + lines_not_images} not checked"
        if can_charge is False:
            text_line += f" (billing is off: {price_reason})"
        elif can_charge is None:
            text_line += f" (no charge was possible: {price_reason})"
        if stop_reason:
            text_line += f", stopped: {stop_reason}"
        await Actor.set_status_message(text_line)

        if unexpected_error:
            await Actor.fail(status_message=(
                f"{text_line}. Unexpected error: {unexpected_error}"))
        elif not engine_ok:
            await Actor.fail(status_message=text_line)
        elif not asked:
            await Actor.fail(status_message=(
                "the input list is empty, so there was nothing to read: "
                "nothing was charged"))


if __name__ == "__main__":
    asyncio.run(main())
