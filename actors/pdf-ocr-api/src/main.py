"""Scanned and digital pdf to text, one row per page, charged only for text.

The money rule of the park is kept here literally: a row is written and charged by one
SDK call, and a page that produced no text is never charged. A page whose text layer is
empty and whose scan could not be read is written free with the reason, because selling
an empty string is exactly what the leader of this niche does and what buyers complain
about elsewhere.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import httpx
import pymupdf  # noqa: F401 - imported so a broken image fails at once, not mid run
from apify import Actor

from . import ocr, pdfx
from .net import host_is_public, host_of, normalize_url

EVENT_NAME = "page-ocr"
SUMMARY_STORE = "pdf-ocr-api-summary"
MAX_DOCUMENTS_DEFAULT = 10
MAX_PAGES_DEFAULT = 10
DOWNLOAD_TIMEOUT_S = 30.0
MAX_REFUSALS_IN_A_ROW = 3
DOC_MARGIN_S = 35.0
PAGE_EXTRA_S = 6.0

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
ST_NOT_PDF = "NOT_CHECKED_NOT_PDF"
ST_ENCRYPTED = "NOT_CHECKED_ENCRYPTED"
ST_BROKEN = "NOT_CHECKED_BROKEN_PDF"
ST_NO_TEXT = "NOT_CHECKED_NO_TEXT"
ST_NO_SUCH_PAGE = "NOT_CHECKED_NO_SUCH_PAGE"


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


def page_margin_seconds(dpi: int) -> float:
    """Worst case for one page: raster, recognise, write, plus a breath.

    Recognition is the only slow part and it grows with the rendered size, so the
    margin is derived from dpi instead of being one number for every setting.
    """
    return PAGE_EXTRA_S + (dpi / 100.0) * 4.0


def host_status(reason: str | None) -> str:
    """A host that does not resolve is not a private address."""
    return ST_PRIVATE if "not a public address" in (reason or "") else ST_DNS


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
    return ST_ERROR, "the run stopped before this document: nothing was charged"


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


def build_tasks(lines: list, max_documents: int) -> tuple[list[dict], dict]:
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
        if max_documents and counts["unique"] > max_documents:
            counts["limit"] += 1
            tasks.append({"kind": "row", "row": status_row(
                str(raw), index, ST_LIMIT,
                f"the list is cut at maxDocuments={max_documents}: not read and "
                "not charged")})
            continue
        tasks.append({"kind": "work", "line": str(raw), "index": index, "url": url})
    return tasks, counts


async def download(client: httpx.AsyncClient, url: str) -> dict:
    """The bytes of one document, and an honest refusal when there are none.

    The signature decides, not the content type and not the file name: a link that
    answers with an html login page is not a pdf, and that exact page was billed as
    a document once in this park before the check existed.
    """
    result: dict = {"data": None, "final_url": None, "hops": [], "status": None,
                    "reason": None, "content_type": None}
    try:
        async with client.stream("GET", url) as response:
            result["hops"] = [str(h.url) for h in response.history] + [str(response.url)]
            result["final_url"] = result["hops"][-1]
            result["content_type"] = response.headers.get("content-type")
            code = response.status_code
            if code >= 400:
                result["status"] = ST_FETCH
                result["reason"] = (f"the server answered HTTP {code}: not delivered and "
                                    "not charged")
                return result
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > pdfx.MAX_BYTES:
                    result["status"] = ST_TOO_LARGE
                    result["reason"] = (f"the document is larger than "
                                        f"{pdfx.MAX_BYTES // (1024 * 1024)} MB: not "
                                        "delivered and not charged")
                    return result
                chunks.append(chunk)
            data = b"".join(chunks)
            result["data"] = data
            content_type = (result["content_type"] or "").lower()
            if "html" in content_type and not pdfx.looks_like_pdf(data):
                result["status"] = ST_NOT_PDF
                result["reason"] = ("the link answered with an html page, not a pdf: not "
                                    "delivered and not charged")
                return result
            return result
    except httpx.TimeoutException:
        result["status"] = ST_FETCH
        result["reason"] = (f"the download timed out before the file arrived: not "
                            "delivered and not charged")
        return result
    except Exception as exc:  # noqa: BLE001 - one document never kills the run
        result["status"] = ST_FETCH
        result["reason"] = (f"{type(exc).__name__}: the document could not be downloaded, "
                            "nothing was delivered and nothing was charged")
        return result


async def main() -> None:
    async with Actor:
        raw_input = await Actor.get_input() or {}
        dataset = await Actor.open_dataset()
        charging = Actor.get_charging_manager()
        can_charge, charge_reason = price_state(charging)
        billable_claim = None if can_charge is None else bool(can_charge)

        lines = raw_input.get("pdfUrls") or []
        if isinstance(lines, str):
            lines = [lines]
        lines = list(lines)

        try:
            max_documents = int(raw_input.get("maxDocuments") or 0)
        except (TypeError, ValueError):
            max_documents = 0
        if max_documents <= 0:
            max_documents = MAX_DOCUMENTS_DEFAULT
        try:
            max_pages = int(raw_input.get("maxPagesPerDocument") or 0)
        except (TypeError, ValueError):
            max_pages = 0
        if max_pages < 0:
            max_pages = 0
        if raw_input.get("maxPagesPerDocument") is None:
            max_pages = MAX_PAGES_DEFAULT
        try:
            dpi = int(raw_input.get("dpi") or 200)
        except (TypeError, ValueError):
            dpi = 200
        dpi = max(120, min(dpi, 400))
        ocr_mode = str(raw_input.get("ocrMode") or "auto").lower()
        if ocr_mode not in ("auto", "always", "never"):
            ocr_mode = "auto"
        lang = str(raw_input.get("languages") or "eng").strip().lower()
        include_words = bool(raw_input.get("includeWords"))
        pages_spec = str(raw_input.get("pages") or "")
        margin_s = page_margin_seconds(dpi)

        # A page range that cannot be understood stops the run before the first
        # request. Silently ignoring it would sell pages outside the asked window.
        try:
            pdfx.parse_pages(pages_spec, 10)
        except ValueError as exc:
            await Actor.fail(status_message=(
                f"The pages field cannot be understood ({exc}). Nothing was "
                "downloaded and nothing was charged. Use a range like 1-5,9."))
            return

        needs_ocr = ocr_mode != "never"
        if needs_ocr:
            ok, engine_reason = ocr.available()
            if not ok:
                await Actor.fail(status_message=(
                    f"This image has no OCR engine ({engine_reason}), so a scanned "
                    "page could not be read. Nothing was downloaded and nothing was "
                    "charged."))
                return
            have = ocr.languages()
            wanted = [part for part in lang.split("+") if part]
            missing = [part for part in wanted if part not in have]
            if missing:
                await Actor.fail(status_message=(
                    f"The language pack {', '.join(missing)} is not in this image "
                    f"(it has: {', '.join(sorted(have)) or 'none'}). Nothing was "
                    "downloaded and nothing was charged."))
                return

        tasks, counts = build_tasks(lines, max_documents)

        by_status: dict[str, int] = {}
        rows_written = 0
        delivered_pages = 0
        charged_total = 0
        delivered_uncharged = 0
        charge_errors = 0
        write_errors = 0
        documents_processed = 0
        documents_not_checked = 0
        pages_from_layer = 0
        pages_from_ocr = 0
        pages_without_text = 0
        pages_cut_by_limit = 0
        pages_not_in_document = 0
        private_blocked = 0
        dns_errors = 0
        fetch_errors = 0
        not_pdf = 0
        too_large = 0
        encrypted = 0
        broken = 0
        ocr_failures = 0
        ocr_seconds = 0.0
        stop_reason: str | None = None
        deadline_capped = False
        unexpected_error: str | None = None
        dataset_api_count: int | None = None
        dataset_note: str | None = None
        billing_active = can_charge is True
        stopping = False
        refusals_in_a_row = 0
        failed_at = 0

        def note(status: str) -> None:
            by_status[status] = by_status.get(status, 0) + 1

        async def write_free(row: dict) -> bool:
            nonlocal rows_written, write_errors, dataset_note
            nonlocal stopping, stop_reason, refusals_in_a_row
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
            follow_redirects=True, timeout=timeout, limits=limits,
            headers={"User-Agent": "apify-pdf-ocr/0.1 (+https://apify.com)"},
        )

        try:
            for position, task in enumerate(tasks):
                failed_at = position
                if task["kind"] == "row":
                    row = task["row"]
                    written = await write_free(row)
                    note(row["status"] if written else ST_ERROR)
                    continue

                if stopping:
                    await stop_row(task["line"], task["index"])
                    documents_not_checked += 1
                    continue

                left = deadline_seconds()
                if left is not None and left < DOC_MARGIN_S:
                    deadline_capped = True
                    stopping = True
                    stop_reason = stop_reason or "deadline"
                    note(ST_TIME)
                    documents_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], ST_TIME,
                        "the platform deadline was close, nothing was delivered and "
                        "nothing was charged"))
                    continue

                if can_charge is None:
                    stopping = True
                    stop_reason = stop_reason or "billing-price-unknown"
                    note(ST_BILLING)
                    documents_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], ST_BILLING,
                        f"the price could not be read ({charge_reason}): not delivered "
                        "and not charged"))
                    continue

                if can_charge and charging.is_event_charge_limit_reached(EVENT_NAME):
                    stopping = True
                    stop_reason = stop_reason or "spending-cap"
                    note(ST_CAP)
                    documents_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], ST_CAP,
                        "the spending cap was reached: not delivered and not charged"))
                    continue

                url = task["url"]
                ok, why = host_is_public(host_of(url))
                if not ok:
                    status = host_status(why)
                    if status == ST_PRIVATE:
                        private_blocked += 1
                    else:
                        dns_errors += 1
                    note(status)
                    documents_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], status,
                        f"the address cannot be used, the file was not downloaded: {why}"))
                    continue

                got = await download(client, url)
                data = got.get("data")
                final_url = got.get("final_url")
                hops = got.get("hops") or []
                if got["status"] is not None:
                    status, reason = got["status"], got["reason"]
                    if status == ST_FETCH:
                        fetch_errors += 1
                    elif status == ST_TOO_LARGE:
                        too_large += 1
                    else:
                        not_pdf += 1
                    note(status)
                    documents_not_checked += 1
                    await write_free(status_row(task["line"], task["index"], status, reason))
                    continue

                # A public url may answer with a redirect into the private network.
                redirected_private = None
                for hop in hops:
                    if not hop:
                        continue
                    hop_ok, hop_why = host_is_public(host_of(hop))
                    if not hop_ok:
                        redirected_private = f"{hop}: {hop_why}"
                        break
                if redirected_private:
                    private_blocked += 1
                    note(ST_PRIVATE)
                    documents_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], ST_PRIVATE,
                        f"a redirect led to a non public address ({redirected_private}): "
                        "nothing was delivered and nothing was charged"))
                    continue

                if not pdfx.looks_like_pdf(data or b""):
                    not_pdf += 1
                    note(ST_NOT_PDF)
                    documents_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], ST_NOT_PDF,
                        "the bytes are not a pdf, whatever the url or the content type "
                        "says: not delivered and not charged"))
                    continue

                doc, status, reason = pdfx.open_document(data)
                if doc is None:
                    if status == ST_ENCRYPTED:
                        encrypted += 1
                    else:
                        broken += 1
                    note(status)
                    documents_not_checked += 1
                    await write_free(status_row(
                        task["line"], task["index"], status,
                        f"{reason}: not delivered and not charged"))
                    continue

                try:
                    total_pages = doc.page_count
                    try:
                        indexes, missing = pdfx.parse_pages(pages_spec, total_pages)
                    except ValueError as exc:
                        note(ST_INVALID)
                        documents_not_checked += 1
                        await write_free(status_row(
                            task["line"], task["index"], ST_INVALID,
                            f"the pages field cannot be understood ({exc}): nothing was "
                            "read and nothing was charged"))
                        continue
                    pages_not_in_document += missing
                    if not indexes:
                        # The url is fine and the range is fine: this document simply
                        # has no such page. Calling that INVALID blames the buyer for
                        # a document that is short.
                        status = ST_NO_SUCH_PAGE if missing else ST_INVALID
                        reason = (
                            f"none of the asked pages exist in this document of "
                            f"{total_pages} page(s): nothing was read and nothing was "
                            "charged" if missing else
                            "the pages field selected no page of this document of "
                            f"{total_pages} page(s)")
                        note(status)
                        documents_not_checked += 1
                        await write_free(status_row(
                            task["line"], task["index"], status, reason))
                        continue
                    planned = indexes if max_pages <= 0 else indexes[:max_pages]
                    pages_cut_by_limit += max(0, len(indexes) - len(planned))
                    title = pdfx.document_title(doc)
                    delivered_here = 0
                    pages_read_here = 0
                    for page_no in planned:
                        if stopping:
                            status, reason = stopped_status(stop_reason)
                            note(status)
                            if stop_reason != "dataset-write-error":
                                await write_free(status_row(
                                    task["line"], task["index"], status, reason,
                                    page=page_no + 1))
                            continue
                        left = deadline_seconds()
                        if left is not None and left < margin_s:
                            deadline_capped = True
                            stopping = True
                            stop_reason = stop_reason or "deadline"
                            note(ST_TIME)
                            await write_free(status_row(
                                task["line"], task["index"], ST_TIME,
                                f"the platform deadline was close before page "
                                f"{page_no + 1}: not delivered and not charged",
                                page=page_no + 1))
                            continue
                        if can_charge and charging.is_event_charge_limit_reached(EVENT_NAME):
                            stopping = True
                            stop_reason = stop_reason or "spending-cap"
                            note(ST_CAP)
                            await write_free(status_row(
                                task["line"], task["index"], ST_CAP,
                                f"the spending cap was reached before page "
                                f"{page_no + 1}: not delivered and not charged",
                                page=page_no + 1))
                            continue
                        try:
                            page = doc.load_page(page_no)
                            pages_read_here += 1
                            width, height = pdfx.page_size(page)
                            layer = "" if ocr_mode == "always" else pdfx.text_layer(page)
                            text = ""
                            source = None
                            confidence = None
                            words = None
                            if layer.strip():
                                text, source = layer, "text"
                            elif ocr_mode == "never":
                                pages_without_text += 1
                                note(ST_NO_TEXT)
                                await write_free(status_row(
                                    task["line"], task["index"], ST_NO_TEXT,
                                    "this page has no text layer and ocrMode is never: "
                                    "nothing was delivered and nothing was charged",
                                    page=page_no + 1, pagesInDocument=total_pages,
                                    documentTitle=title))
                                continue
                            else:
                                started = asyncio.get_event_loop().time()
                                # The page budget is three margins, but the time left
                                # before the platform kills the run caps it: a page
                                # that cannot finish in time fails as a free row
                                # instead of the whole run dying without a summary.
                                ocr_budget = max(5.0, min(
                                    margin_s * 3, (1e9 if left is None else left) - 1.0))
                                try:
                                    got = ocr.recognize(
                                        pdfx.rasterize(page, dpi), lang, include_words,
                                        timeout=ocr_budget,
                                    )
                                except ocr.OcrError as exc:
                                    ocr_failures += 1
                                    note(ST_ERROR)
                                    await write_free(status_row(
                                        task["line"], task["index"], ST_ERROR,
                                        f"{exc}: nothing was delivered and nothing was charged",
                                        page=page_no + 1, pagesInDocument=total_pages,
                                        documentTitle=title))
                                    continue
                                ocr_seconds += max(0.0, asyncio.get_event_loop().time() - started)
                                text = got["text"]
                                source = "ocr"
                                confidence = got["confidence"]
                                words = got["words"] if include_words else None
                                if not text.strip():
                                    pages_without_text += 1
                                    note(ST_NO_TEXT)
                                    await write_free(status_row(
                                        task["line"], task["index"], ST_NO_TEXT,
                                        "the page has no text layer and the scan gave no "
                                        "readable text: not delivered and not charged",
                                        page=page_no + 1, pagesInDocument=total_pages,
                                        documentTitle=title))
                                    continue
                        except Exception as exc:  # noqa: BLE001 - one page never kills the run
                            note(ST_ERROR)
                            await write_free(status_row(
                                task["line"], task["index"], ST_ERROR,
                                f"{type(exc).__name__}: this page could not be read, nothing "
                                "was delivered and nothing was charged",
                                page=page_no + 1, pagesInDocument=total_pages))
                            continue

                        row = {
                            "url": final_url or url,
                            "input": task["line"],
                            "inputIndex": task["index"],
                            "status": ST_OK,
                            "reason": None,
                            "billable": False,
                            "page": page_no + 1,
                            "pagesInDocument": total_pages,
                            "pagesLeft": max(0, len(indexes) - len(planned)),
                            "source": source,
                            "chars": len(text),
                            "confidence": confidence,
                            "text": text,
                            "words": words,
                            "pageWidth": width,
                            "pageHeight": height,
                            "documentTitle": title,
                        }
                        if source == "text":
                            pages_from_layer += 1
                        else:
                            pages_from_ocr += 1

                        if can_charge:
                            try:
                                result = await Actor.push_data(
                                    {**row, "billable": billable_claim},
                                    charged_event_name=EVENT_NAME,
                                )
                            except Exception as exc:  # noqa: BLE001
                                charge_errors += 1
                                refusals_in_a_row = 0
                                delivered_uncharged += 1
                                rows_written += 1
                                delivered_pages += 1
                                delivered_here += 1
                                note(ST_OK)
                                stopping = True
                                stop_reason = "charge-error"
                                dataset_note = (
                                    f"a paid row could not be confirmed after "
                                    f"{type(exc).__name__}: it may be in the dataset and it "
                                    "was not charged")
                                continue
                            charged = int(getattr(result, "charged_count", 0) or 0)
                            limit_reached = bool(getattr(result, "event_charge_limit_reached", False))
                            if charged <= 0:
                                status = ST_CAP if limit_reached else ST_ERROR
                                note(status)
                                await write_free(status_row(
                                    task["line"], task["index"], status,
                                    "the spending cap was reached: not delivered and not charged"
                                    if limit_reached else
                                    "nothing was written and nothing was charged",
                                    page=page_no + 1))
                                stopping = True
                                stop_reason = "spending-cap" if limit_reached else "charge-error"
                                continue
                            rows_written += 1
                            delivered_pages += 1
                            delivered_here += 1
                            charged_total += charged
                            refusals_in_a_row = 0
                            note(ST_OK)
                            if limit_reached:
                                stopping = True
                                stop_reason = "spending-cap"
                            continue

                        row["billable"] = False
                        written = await write_free(row)
                        delivered_here += 1
                        delivered_pages += 1
                        note(ST_OK if written else ST_ERROR)
                    # A document whose pages were opened and read was checked, even
                    # when every page of it turned out to carry no text. Only a
                    # document we never reached is not checked.
                    if pages_read_here == 0:
                        documents_not_checked += 1
                    else:
                        documents_processed += 1
                finally:
                    try:
                        doc.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001 - a crash must not lose the summary
            unexpected_error = f"{type(exc).__name__}: {exc}"[:300]
            stop_reason = stop_reason or "unexpected-error"
            for rest in tasks[failed_at:]:
                if rest.get("kind") == "row":
                    row = rest.get("row") or {}
                    line = row.get("input") or ""
                    index = row.get("inputIndex") or 0
                else:
                    line = rest.get("line") or ""
                    index = rest.get("index") or 0
                note(ST_ERROR)
                await write_free(status_row(
                    line, index, ST_ERROR,
                    "the run stopped on an unexpected error before this document: "
                    "nothing was charged"))
        finally:
            await client.aclose()
            dataset_api_count = await dataset_count(dataset)

        asked = counts["asked"]
        # A bad url, a repeat, a blank line and a line past maxDocuments were never
        # documents to read. Without a bucket of their own the invariant looked
        # broken on every list that carried one of them.
        lines_not_documents = (counts["invalid"] + counts["blank"]
                               + counts["duplicates"] + counts["limit"])
        documents_attempted = counts["unique"] - counts["limit"]
        if documents_processed + documents_not_checked != documents_attempted:
            by_status["documentMismatch"] = (
                documents_attempted - (documents_processed + documents_not_checked))
        if lines_not_documents + documents_attempted != asked:
            by_status["lineCountMismatch"] = asked - lines_not_documents - documents_attempted

        summary = {
            "requested": asked,
            "uniqueDocuments": counts["unique"],
            "duplicates": counts["duplicates"],
            "invalidLines": counts["invalid"],
            "blankLines": counts["blank"],
            "cutByMaxDocuments": counts["limit"],
            "processedDocuments": documents_processed,
            "documentsAttempted": documents_attempted,
            "linesThatAreNotDocuments": lines_not_documents,
            "notCheckedDocuments": documents_not_checked,
            "deliveredPages": delivered_pages,
            "rowsWritten": rows_written,
            "chargedEvents": charged_total if billing_active else 0,
            "billingActive": billing_active,
            "billingReason": charge_reason,
            "deliveredUncharged": delivered_uncharged,
            "chargeErrors": charge_errors,
            "writeErrors": write_errors,
            "stopReason": stop_reason,
            "unexpectedError": unexpected_error,
            "byStatus": by_status,
            "pagesFromTextLayer": pages_from_layer,
            "pagesFromOcr": pages_from_ocr,
            "pagesWithoutText": pages_without_text,
            "pagesCutByLimit": pages_cut_by_limit,
            "pagesNotInDocument": pages_not_in_document,
            "ocrFailures": ocr_failures,
            "ocrSeconds": round(ocr_seconds, 1),
            "dpi": dpi,
            "languages": lang,
            "ocrMode": ocr_mode,
            "maxDocuments": max_documents,
            "maxPagesPerDocument": max_pages,
            "pagesField": pages_spec or None,
            "deadlineCapped": deadline_capped,
            "pageMarginSecs": round(margin_s, 1),
            "privateBlocked": private_blocked,
            "dnsErrors": dns_errors,
            "fetchErrors": fetch_errors,
            "notPdf": not_pdf,
            "tooLarge": too_large,
            "encrypted": encrypted,
            "brokenPdf": broken,
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

        store = await Actor.open_key_value_store(name=SUMMARY_STORE)
        await store.set_value(f"RUN_SUMMARY-{run_id()}", summary)
        default_store = await Actor.open_key_value_store()
        await default_store.set_value("RUN_SUMMARY", summary)

        billed = "read and charged" if billing_active else "read, nothing charged"
        text = (
            f"{documents_attempted} document(s) from {asked} line(s), "
            f"{delivered_pages} page(s) {billed}, {documents_not_checked} not checked"
        )
        if pages_without_text:
            text += f", {pages_without_text} page(s) with no text"
        if not billing_active:
            text += f" (billing is off: {charge_reason})"
        if stop_reason:
            text += f", stopped: {stop_reason}"
        await Actor.set_status_message(text)

        if asked == 0:
            await Actor.fail(status_message=(
                "No pdf urls were sent: nothing to read, nothing charged. Put one pdf "
                "url per line into the pdfUrls field."))
        if unexpected_error:
            await Actor.fail(status_message=(
                f"The run stopped on an unexpected error ({unexpected_error}). "
                f"{delivered_pages} page(s) delivered, {documents_not_checked} "
                f"document(s) not checked, {charged_total if billing_active else 0} charged."))
        if asked > 0 and delivered_pages == 0:
            breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
            await Actor.fail(status_message=(
                f"Nothing could be read: {breakdown or 'no rows written'}. "
                "Nothing was charged."))


if __name__ == "__main__":
    asyncio.run(main())
