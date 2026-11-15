"""Apify Actor: title, description, Open Graph, Twitter card, JSON-LD and icons from a list of pages.

Input  : `urls` (a list or one string with newlines), optional `timeoutSecs`,
         `maxRedirects`, `maxUrls`, `includeJsonLd`, `userAgent`.
Output : one row per page: status, title, description, canonical, language, charset,
         openGraph, twitterCard, icons, jsonLd types, hreflang, and a preview verdict
         with the issues that break a link preview.
Charge : `metadata-extracted`, only for a page whose metadata was delivered. Dead
         links, 404s, non-HTML bodies, an unfollowed redirect and pages with no metadata
         cost nothing, and a repeated page is delivered free. Money is reported in the
         run summary
         (chargedEvents / deliveredUncharged / billingActive); the row's `billable`
         only says the page was worth an event.
"""
from __future__ import annotations

import asyncio
import os
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from apify import Actor

from src import meta

CHARGE_EVENT = "metadata-extracted"
SUMMARY_STORE = "open-graph-meta-tags-summary"
STATUS_LIMIT = "NOT_CHECKED_LIMIT"
STATUS_CAP = "NOT_CHECKED_CAP"
STATUS_BILLING = "NOT_CHECKED_BILLING"
STATUS_WRITE = "NOT_CHECKED_ERROR"

CHARGE_ERROR_LIMIT = 200
LINK_ERROR_STATUSES = meta.LINK_ERROR_STATUSES
HONEST_STATUSES = meta.HONEST_STATUSES


def _dep_versions() -> str:
    out = []
    for name in ("apify", "httpx", "selectolax"):
        try:
            out.append("%s=%s" % (name, _pkg_version(name)))
        except PackageNotFoundError:
            out.append("%s=?" % name)
    return " ".join(out)


def _rows_from(value) -> list[str]:
    """Accepts a list of links, one string with newlines, or a messy mix."""
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        value = value.replace(",", "\n").splitlines()
    if isinstance(value, (list, tuple)):
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if not text:
                continue
            if "://" in text:
                out.append(text)
                continue
            for line in text.replace(",", "\n").splitlines():
                line = line.strip()
                if line:
                    out.append(line)
    return out


def _num(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _row(index: int, source: str, status: str, reason: str = "") -> dict:
    return {
        "index": index,
        "source": source,
        "url": source,
        "status": status,
        "reason": reason,
        "statusCode": None,
        "finalUrl": None,
        "redirectCount": 0,
        "responseTimeMs": None,
        "title": None,
        "description": None,
        "canonical": None,
        "lang": None,
        "charset": None,
        "openGraph": {},
        "twitterCard": {},
        "icons": {},
        "jsonLd": {"count": 0, "types": []},
        "hreflang": [],
        "preview": {"ready": False, "issues": []},
        "metaCount": 0,
        "billable": False,
    }


def _no_page_message(by_status: dict, requested: int) -> str:
    parts = ", ".join("%s x%d" % (status, count) for status, count in sorted(by_status.items()) if count)
    return ("Nothing was read: %d page(s) were sent and every one of them came back as %s. "
            "Check the addresses; nothing was charged." % (requested, parts))


def _has_preview_data(row: dict) -> bool:
    """True when the row still holds something a link preview can show.

    jsonLd is not counted: this runs after the buyer turned that block off, and a page
    whose only metadata was JSON-LD then has nothing left to deliver.
    """
    if row.get("title") or row.get("description"):
        return True
    return any(bool(value) for value in (row.get("openGraph") or {}).values())


async def main() -> None:
    async with Actor:
        Actor.log.info("deps: %s", _dep_versions())
        actor_input = await Actor.get_input() or {}
        all_urls = _rows_from(actor_input.get("urls"))
        if not all_urls:
            await Actor.fail(status_message="No pages: put at least one URL into 'urls'. Nothing was charged.")
            return

        timeout = _num(actor_input.get("timeoutSecs"), 20.0, 1.0, 120.0)
        max_redirects = int(_num(actor_input.get("maxRedirects"), 5, 0, 20))
        include_jsonld = bool(actor_input.get("includeJsonLd", True))
        user_agent = str(actor_input.get("userAgent") or "")

        requested = len(all_urls)
        max_urls_raw = actor_input.get("maxUrls")
        if max_urls_raw is None:
            max_urls = 100
        else:
            max_urls = int(_num(max_urls_raw, 100, 0, 1_000_000))
            if max_urls <= 0:
                # 0 means "no limit", and the summary reports the number actually applied.
                max_urls = requested
        urls = all_urls[:max_urls]
        beyond = all_urls[max_urls:]

        summary = {
            "requested": requested,
            "processed": 0,
            "delivered": 0,
            "rowsDelivered": 0,
            "notChecked": 0,
            "chargedEvents": 0,
            "billingActive": True,
            "stopReason": None,
            "byStatus": {},
            "duplicatesSkipped": 0,
            "cutByMaxUrls": len(beyond),
            "maxUrls": max_urls,
            "metadataMissing": 0,
            "deliveredUncharged": 0,
            "datasetWriteErrors": 0,
            "datasetWriteUnverified": 0,
            "datasetItemCount": None,
            "datasetItemCountApi": None,
            "datasetItemCountLag": False,
            "chargeErrors": [],
        }

        charge_errors: list[str] = []
        consecutive_failures = 0
        write_failures = 0
        billing_active = True
        run_stopped = None      # a reason that stops reading the rest of the list
        stop_reason = None      # the reason reported in the summary, may allow reading on

        # Ask the platform up front whether this run can be billed at all: a run that is not
        # pay-per-event, or whose pricing has no CHARGE_EVENT, delivers rows for free and says
        # so, instead of mistaking "no charge happened" for "the spending cap was reached".
        try:
            pricing = Actor.get_charging_manager().get_pricing_info()
        except Exception:  # noqa: BLE001 - local runs and test doubles carry no pricing
            pricing = None
        if pricing is not None:
            prices = getattr(pricing, "per_event_prices", None) or {}
            if not getattr(pricing, "is_pay_per_event", False):
                billing_active = False
            elif CHARGE_EVENT not in prices:
                billing_active = False
                Actor.log.error("pricing has no %r event: this run delivers metadata without charging",
                                CHARGE_EVENT)

        def count_status(row: dict) -> None:
            summary["byStatus"][row["status"]] = summary["byStatus"].get(row["status"], 0) + 1

        def count_row(row: dict) -> None:
            count_status(row)
            summary["rowsDelivered"] += 1

        async def item_count() -> int | None:
            """Rows the run dataset holds now, or None when it cannot be asked.

            apify 4.x has no get_info hook on a dataset: the metadata comes from
            get_metadata(). Both names are tried, so a missing method never reads as
            "no rows".
            """
            try:
                dataset = await Actor.open_dataset()
                meta_info = None
                for getter in ("get_metadata", "get_info"):
                    method = getattr(dataset, getter, None)
                    if method is not None:
                        meta_info = await method()
                        break
                if meta_info is None:
                    return None
                if isinstance(meta_info, dict):
                    for key in ("item_count", "itemCount"):
                        if meta_info.get(key) is not None:
                            return int(meta_info[key])
                    return None
                for attr in ("item_count", "itemCount"):
                    value = getattr(meta_info, attr, None)
                    if value is not None:
                        return int(value)
                return None
            except Exception:  # noqa: BLE001 - a local run may have no dataset handle
                return None

        async def push(row: dict) -> bool:
            """Write a row and count it only when the dataset really grew.

            The write is retried once: a single transient refusal must not cost the buyer
            the rest of the list. The run stops only after three refusals in a row, and
            delivery is judged by the dataset row count, never by the absence of an
            exception.
            """
            nonlocal run_stopped, stop_reason, write_failures
            before = await item_count()
            error = None
            for attempt in (1, 2):
                try:
                    await Actor.push_data(row)
                    error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    error = exc
                    Actor.log.error("dataset write failed for %s (attempt %d): %s",
                                    row.get("source"), attempt, exc)
            if error is None:
                count_row(row)
                write_failures = 0
                return True
            after = await item_count()
            if before is not None and after is not None:
                landed = after > before
            else:
                Actor.log.warning("could not verify the dataset row count for %s", row.get("source"))
                summary["datasetWriteUnverified"] += 1
                landed = True
            if landed:
                count_row(row)
                return True
            row["status"] = STATUS_WRITE
            row["reason"] = ("The dataset refused to store this page's row (%s). Nothing was charged for it."
                             % type(error).__name__)
            row["title"] = None
            row["description"] = None
            row["openGraph"] = {}
            row["twitterCard"] = {}
            row["preview"] = {"ready": False, "issues": []}
            row["billable"] = False
            summary["datasetWriteErrors"] += 1
            count_status(row)
            write_failures += 1
            if stop_reason is None:
                stop_reason = "dataset-write-error"
            if write_failures >= 3:
                run_stopped = stop_reason = "dataset-write-error"
            return False

        seen: dict[str, dict] = {}

        async with meta.make_client(user_agent=user_agent, timeout=timeout) as client:
            for index, url in enumerate(urls, 1):
                row = _row(index, url, meta.STATUS_ERROR)
                if run_stopped:
                    if run_stopped == "spending-cap":
                        row["status"] = STATUS_CAP
                        row["reason"] = "Not read: the run reached the spending cap earlier."
                    elif run_stopped == "dataset-write-error":
                        row["status"] = STATUS_WRITE
                        row["reason"] = "Not read: the dataset refused earlier rows, so the run stopped."
                    else:
                        row["status"] = STATUS_BILLING
                        row["reason"] = ("Not read: billing stopped the run earlier (%s). "
                                         "Nothing was charged for this row." % run_stopped)
                    await push(row)
                    continue

                key = meta.normalize_url(url) or url.strip().lower()
                if key in seen:
                    row = dict(seen[key])
                    row["index"] = index
                    row["source"] = url
                    row["url"] = url
                    row["duplicateOf"] = seen[key]["index"]
                    row["billable"] = False
                    if seen[key]["status"] == meta.STATUS_EXTRACTED:
                        row["reason"] = ("The same page as row %d: delivered free, no second charge."
                                         % seen[key]["index"])
                    else:
                        # A repeated dead link is still the same dead link: calling it
                        # "delivered free" would hide the real diagnosis from the buyer.
                        row["reason"] = ("The same page as row %d with the same answer (%s). "
                                         "No second charge."
                                         % (seen[key]["index"], seen[key]["status"]))
                    summary["duplicatesSkipped"] += 1
                    if await push(row) and row["status"] == meta.STATUS_EXTRACTED:
                        summary["delivered"] += 1
                    if row["status"] == meta.STATUS_NO_METADATA:
                        summary["metadataMissing"] += 1
                    continue

                try:
                    body = await meta.read_page(client, url, timeout=timeout, max_redirects=max_redirects)
                except Exception as exc:  # noqa: BLE001 - a broken page is a row, not a dead run
                    body = meta._fail(meta.STATUS_ERROR,
                                      "This page could not be read: %s: %s" % (type(exc).__name__, str(exc)[:140]))
                    Actor.log.error("page %s failed: %s", index, exc)

                row.update({k: v for k, v in body.items() if k not in ("status", "reason")})
                row["status"] = body.get("status") or meta.STATUS_ERROR
                row["reason"] = body.get("reason") or ""
                if not include_jsonld:
                    row["jsonLd"] = {"count": 0, "types": []}
                    if row["status"] == meta.STATUS_EXTRACTED and not _has_preview_data(row):
                        # The only metadata on the page was JSON-LD: with that block dropped
                        # the row is empty, so it is an answer with no data and costs nothing.
                        row["status"] = meta.STATUS_NO_METADATA
                        row["reason"] = ("The page carried only JSON-LD and includeJsonLd is false: "
                                         "with that block dropped there is nothing to show in a "
                                         "preview. Nothing was charged for it.")
                if row["status"] == meta.STATUS_NO_METADATA:
                    summary["metadataMissing"] += 1
                seen[key] = dict(row)

                pushed = False
                free_delivery = False
                if row["status"] == meta.STATUS_EXTRACTED:
                    if not billing_active:
                        # The run already knows it cannot be billed: the metadata goes out free
                        # and the row says so at the moment it is written.
                        row["billable"] = True
                        row["billingInactive"] = True
                        free_delivery = True
                    else:
                        row["billable"] = True
                        before = await item_count()
                        try:
                            # Charge together with the write: the SDK stores the row first and
                            # charges it under the same lock, so a failed charge can no longer
                            # pay for a row the buyer never receives.
                            result = await Actor.push_data(row, charged_event_name=CHARGE_EVENT)
                            pushed = True
                            billed = int(getattr(result, "charged_count", 0) or 0)
                            if billed:
                                consecutive_failures = 0
                                summary["chargedEvents"] += billed
                                if getattr(result, "event_charge_limit_reached", False):
                                    run_stopped = stop_reason = "spending-cap"
                            elif getattr(result, "event_charge_limit_reached", False):
                                # The cap let nothing through and the SDK wrote no row, so the
                                # page goes out as a plain row saying it was not checked.
                                pushed = False
                                row["status"] = STATUS_CAP
                                row["reason"] = ("The spending cap was reached, so this page was not charged "
                                                 "and its metadata is not delivered.")
                                row["title"] = None
                                row["description"] = None
                                row["openGraph"] = {}
                                row["twitterCard"] = {}
                                row["preview"] = {"ready": False, "issues": []}
                                row["billable"] = False
                                run_stopped = stop_reason = "spending-cap"
                            else:
                                # The platform charged nothing, so this row is a free delivery and
                                # the counter has to say it.
                                billing_active = False
                                free_delivery = True
                        except Exception as exc:  # noqa: BLE001
                            after = await item_count()
                            Actor.log.error("billing failed for %s: %s", url, exc)
                            if len(charge_errors) < CHARGE_ERROR_LIMIT:
                                charge_errors.append("%s: %s: %s" % (url, type(exc).__name__, str(exc)[:120]))
                            if before is not None and after is not None:
                                wrote = after > before
                            else:
                                Actor.log.warning("could not verify the dataset row count for %s", url)
                                summary["datasetWriteUnverified"] += 1
                                wrote = True
                            pushed = wrote
                            free_delivery = True
                            if wrote:
                                consecutive_failures += 1
                                if consecutive_failures >= 2:
                                    billing_active = False
                                    if stop_reason is None:
                                        run_stopped = stop_reason = "charge-error"

                if pushed:
                    count_row(row)
                else:
                    pushed = await push(row)
                if pushed:
                    if free_delivery:
                        summary["deliveredUncharged"] += 1
                    if row["status"] == meta.STATUS_EXTRACTED:
                        summary["delivered"] += 1

        for offset, url in enumerate(beyond, len(urls) + 1):
            extra = _row(offset, url, STATUS_LIMIT,
                         "Not read: maxUrls is %d and this page is beyond the limit." % max_urls)
            await push(extra)

        # One place counts both buckets, so requested == processed + notChecked holds even
        # when the dataset refused rows or the cap cut the tail.
        summary["processed"] = sum(count for status, count in summary["byStatus"].items()
                                   if not status.startswith("NOT_CHECKED"))
        summary["notChecked"] = sum(count for status, count in summary["byStatus"].items()
                                    if status.startswith("NOT_CHECKED"))
        summary["billingActive"] = billing_active
        summary["stopReason"] = stop_reason
        api_rows = await item_count()
        summary["datasetItemCount"] = summary["rowsDelivered"]
        summary["datasetItemCountApi"] = api_rows
        summary["datasetItemCountLag"] = api_rows is not None and api_rows < summary["rowsDelivered"]
        summary["chargeErrors"] = charge_errors

        try:
            store = await Actor.open_key_value_store(name=SUMMARY_STORE)
            await store.set_value("RUN_SUMMARY", summary)
            run_id = (
                os.environ.get("ACTOR_RUN_ID")
                or os.environ.get("APIFY_ACTOR_RUN_ID")
                or Actor.get_env().get("run_id")
                or "run"
            )
            await store.set_value("RUN_SUMMARY-%s" % run_id, summary)
        except Exception as exc:  # noqa: BLE001
            Actor.log.warning("the named summary store is unavailable: %s", exc)
        try:
            run_store = await Actor.open_key_value_store()
            await run_store.set_value("RUN_SUMMARY", summary)
        except Exception as exc:  # noqa: BLE001
            Actor.log.warning("the run summary could not be stored: %s", exc)

        Actor.log.info("done: requested %s, rows %s, pages with metadata %s, charged %s, duplicates %s, stop %s",
                       summary["requested"], summary["rowsDelivered"], summary["delivered"],
                       summary["chargedEvents"], summary["duplicatesSkipped"], stop_reason)

        if summary["delivered"] == 0 and urls:
            if stop_reason == "spending-cap":
                await Actor.fail(status_message=(
                    "The spending cap was reached before any page was delivered: %d page(s) were sent. "
                    "Raise the run's max total charge and run again." % len(urls)))
            elif stop_reason == "dataset-write-error":
                await Actor.fail(status_message=(
                    "The run dataset refused to store rows: %d of %d page(s) came back as rows and the "
                    "rest are reported as not checked. Nothing was charged for the lost rows."
                    % (summary["rowsDelivered"], requested)))
            elif set(summary["byStatus"]) <= LINK_ERROR_STATUSES:
                # A list of typos or blocked pages is not a job done: a green run with zero rows
                # of data reads as success, so it stops loudly with the reason per page.
                await Actor.fail(status_message=_no_page_message(summary["byStatus"], requested))
            elif any(status in HONEST_STATUSES for status in summary["byStatus"]):
                Actor.log.info("no metadata delivered, but every row is an answer: not a failed run")
            else:
                await Actor.fail(status_message=_no_page_message(summary["byStatus"], requested))


if __name__ == "__main__":
    asyncio.run(main())
