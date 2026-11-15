"""Apify Actor: PDF text and tables from a list of links.

Input  : `urls` (direct links or uploaded file names), optional `pages`,
         `includeTables`, `maxCharsPerDocument`, `password`, `maxDocuments`.
Output : one row per document: status, page count, per-page text, tables,
         truncation marker, the plain reason for a reject, `billable` flag.
Charge : `document-extracted` - only when text was actually delivered. Money is reported
         in the run summary (chargedEvents / deliveredUncharged / billingActive); the row's
         `billable` only says the document was worth an event.
         Scans, locked PDFs, broken links and cut-off documents cost nothing.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from apify import Actor

from src import pdf

CHARGE_EVENT = "document-extracted"
SUMMARY_STORE = "pdf-extractor-summary"
STATUS_LIMIT = "NOT_CHECKED_LIMIT"
STATUS_CAP = "NOT_CHECKED_CAP"
STATUS_BILLING = "NOT_CHECKED_BILLING"
STATUS_WRITE = "NOT_CHECKED_ERROR"

# Statuses that are a finished answer about a document the actor did open.
# A batch of scans or locked files is an answer, not a failed run.
CHARGE_ERROR_LIMIT = 200
LINK_ERROR_STATUSES = frozenset((pdf.STATUS_INVALID, pdf.STATUS_UNREACHABLE))
HONEST_STATUSES = (pdf.STATUS_EXTRACTED, pdf.STATUS_NO_TEXT, pdf.STATUS_ENCRYPTED,
                    pdf.STATUS_INVALID, pdf.STATUS_UNREACHABLE)


def _dep_versions() -> str:
    out = []
    for name in ("apify", "pymupdf"):
        try:
            out.append("%s=%s" % (name, _pkg_version(name)))
        except PackageNotFoundError:
            out.append("%s=?" % name)
    return " ".join(out)


def _rows_from(value) -> list[str]:
    """Accepts a list of links, a single string with newlines, or a messy mix."""
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        value = value.replace(",", "\n").splitlines()
    if isinstance(value, (list, tuple)):
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                # a comma separates documents only when the text is not a link itself
                for line in (item if "://" in item else item.replace(",", "\n")).splitlines():
                    line = line.strip()
                    if line:
                        out.append(line)
            else:
                out.append(str(item).strip())
    return out


def _row(index: int, source: str, status: str, reason: str = "") -> dict:
    return {
        "index": index,
        "source": source,
        "status": status,
        "reason": reason,
        "billable": False,
        "pageCount": 0,
        "pagesRequested": 0,
        "pagesRead": 0,
        "chars": 0,
        "truncated": False,
        "pages": [],
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _no_document_message(by_status: dict, requested: int) -> str:
    """A plain sentence naming every status that came back, not just the loudest one."""
    counts = {status: count for status, count in by_status.items() if not status.startswith("NOT_CHECKED")}
    if not counts:
        return "No document could be read: %d link(s) were sent and none was processed. Nothing was charged." % requested
    parts = ", ".join("%s x%d" % (status, count)
                      for status, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))
    return "No document could be read: %d link(s) -> %s. Nothing was charged." % (requested, parts)


async def _read_local(name: str) -> bytes:
    store = await Actor.open_key_value_store()
    value = await store.get_value(name)
    if value is None:
        raise pdf.DocError(pdf.STATUS_UNREACHABLE, "No file named %r in this run's key-value store." % name)
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    return bytes(value)


async def main() -> None:
    async with Actor:
        Actor.log.info("deps: %s", _dep_versions())
        actor_input = await Actor.get_input() or {}
        all_links = _rows_from(actor_input.get("urls"))
        if not all_links:
            await Actor.fail(status_message="No PDF links: put at least one direct link or uploaded file name into 'urls'. Nothing was charged.")
            return

        pages_spec = str(actor_input.get("pages") or "").strip()
        include_tables = bool(actor_input.get("includeTables", True))
        max_chars = int(actor_input.get("maxCharsPerDocument") or 200_000)
        password = str(actor_input.get("password") or "")
        requested = len(all_links)
        max_docs_raw = actor_input.get("maxDocuments")
        if max_docs_raw is None:
            max_docs = 100
        else:
            max_docs = int(max_docs_raw)
            if max_docs <= 0:
                # 0 used to turn into 100 silently. It now means "no limit", and the
                # summary reports the number actually applied.
                max_docs = requested
        links = all_links[:max_docs]
        beyond = all_links[max_docs:]

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
            "cutByMaxDocuments": len(beyond),
            "maxDocuments": max_docs,
            "deliveredUncharged": 0,
            "datasetWriteErrors": 0,
            "datasetWriteUnverified": 0,
            "datasetItemCount": None,
            "datasetItemCountApi": None,
            "datasetItemCountLag": False,
        }
        charge_errors: list[str] = []
        consecutive_failures = 0
        write_failures = 0
        billing_active = True
        run_stopped = None      # a reason that stops reading the rest of the list
        stop_reason = None      # the reason reported in the summary, may allow reading on

        # Ask the platform up front whether this run can be billed at all: a run that is not
        # pay-per-event, or whose pricing has no CHARGE_EVENT, delivers text for free and says
        # so, instead of mistaking "no charge happened" for "the spending cap was reached".
        try:
            # apify 4.x keeps no Actor-level pricing getter: the prices live on the charging manager.
            pricing = Actor.get_charging_manager().get_pricing_info()
        except Exception:  # noqa: BLE001 - local runs and test doubles carry no pricing
            pricing = None
        if pricing is not None:
            prices = getattr(pricing, "per_event_prices", None) or {}
            if not getattr(pricing, "is_pay_per_event", False):
                billing_active = False
            elif CHARGE_EVENT not in prices:
                billing_active = False
                Actor.log.error("pricing has no %r event: this run delivers text without charging", CHARGE_EVENT)
        # A run that cannot be billed is not a stopped run: every document is delivered free,
        # billingActive=false says so, and the old stopReason value read like a failure.

        def count_status(row: dict) -> None:
            summary["byStatus"][row["status"]] = summary["byStatus"].get(row["status"], 0) + 1

        def count_row(row: dict) -> None:
            count_status(row)
            summary["rowsDelivered"] += 1

        async def item_count() -> int | None:
            """How many rows the run dataset holds right now, or None if it cannot be asked.

            apify 4.x has no get_info hook on a dataset: metadata comes from get_metadata()
            and the count lives on it as item_count. Both names are tried, so the guard works
            on the SDK we ship and on a test double, and a missing method is never read as
            "no rows".
            """
            try:
                dataset = await Actor.open_dataset()
                meta = None
                for getter in ("get_metadata", "get_info"):
                    method = getattr(dataset, getter, None)
                    if method is not None:
                        meta = await method()
                        break
                if meta is None:
                    return None
                if isinstance(meta, dict):
                    for key in ("item_count", "itemCount"):
                        if meta.get(key) is not None:
                            return int(meta[key])
                    return None
                for attr in ("item_count", "itemCount"):
                    value = getattr(meta, attr, None)
                    if value is not None:
                        return int(value)
                return None
            except Exception:  # noqa: BLE001 - a local run may have no dataset handle
                return None

        async def push(row: dict) -> bool:
            """Write a row and count it only when the dataset really grew.

            The write is retried once: a single transient refusal must not cost the buyer the
            rest of the list. The run stops only after three refusals in a row, and delivery is
            judged by the dataset row count, never by the absence of an exception.
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
                # The dataset cannot be asked how many rows it holds, so calling the row lost
                # would be a guess: it stays a delivery and the summary says the write could
                # not be verified.
                Actor.log.warning("could not verify the dataset row count for %s", row.get("source"))
                summary["datasetWriteUnverified"] += 1
                landed = True
            if landed:
                count_row(row)
                return True
            # The single place that decides a row is lost: it is marked here, with its text
            # dropped, so a row that never landed is never counted as delivered content.
            row["status"] = STATUS_WRITE
            row["reason"] = ("The dataset refused to store this document: %s. Nothing was charged "
                             "for it." % (type(error).__name__,))
            row["pages"] = []
            row["chars"] = 0
            row["billable"] = False
            # Counted here and nowhere else: one refused row is one refused row.
            summary["datasetWriteErrors"] += 1
            count_status(row)
            write_failures += 1
            if stop_reason is None:
                stop_reason = "dataset-write-error"
            if write_failures >= 3:
                run_stopped = stop_reason = "dataset-write-error"
            return False

        for index, link in enumerate(links, 1):
            row = _row(index, link, pdf.STATUS_ERROR)
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
            try:
                if "://" in link:
                    data = await asyncio.to_thread(pdf.fetch, link)
                else:
                    data = await _read_local(link)
                body = await asyncio.to_thread(pdf.extract, data, pages_spec, include_tables, max_chars, password)
                row.update({k: v for k, v in body.items() if k != "status"})
                row["status"] = body["status"]
                if body["status"] == pdf.STATUS_NO_TEXT:
                    row["reason"] = body["reason"]
            except pdf.DocError as exc:
                row["status"] = exc.status
                row["reason"] = exc.reason
            except ValueError as exc:
                row["status"] = pdf.STATUS_ERROR
                row["reason"] = "Bad page selection: %s" % exc
            except Exception as exc:  # noqa: BLE001
                row["status"] = pdf.STATUS_ERROR
                row["reason"] = "This document could not be read: %s: %s" % (type(exc).__name__, str(exc)[:140])
                Actor.log.error("document %s failed: %s", index, exc)

            pushed = False
            free_delivery = False
            if row["status"] == pdf.STATUS_EXTRACTED:
                if not billing_active:
                    # The run already knows it cannot be billed: the text is delivered free and
                    # the row says so at the moment it is written.
                    row["billable"] = True
                    row["billingInactive"] = True
                    free_delivery = True
                else:
                    row["billable"] = True
                    before = await item_count()
                    try:
                        # Charge together with the write. The SDK stores the row first and charges
                        # it under the same lock, so a failed charge can no longer pay for a row the
                        # buyer never receives (the old order charged first and pushed second).
                        result = await Actor.push_data(row, charged_event_name=CHARGE_EVENT)
                        pushed = True
                        billed = int(getattr(result, "charged_count", 0) or 0)
                        if billed:
                            consecutive_failures = 0
                            summary["chargedEvents"] += billed
                            if getattr(result, "event_charge_limit_reached", False):
                                run_stopped = stop_reason = "spending-cap"
                        elif getattr(result, "event_charge_limit_reached", False):
                            # The cap let nothing through and the SDK wrote no row, so the document
                            # goes out as a plain row saying it was not checked.
                            pushed = False
                            row["status"] = STATUS_CAP
                            row["reason"] = ("The spending cap was reached, so this document was not charged "
                                             "and its text is not delivered.")
                            row["pages"] = []
                            row["chars"] = 0
                            row["pagesDropped"] = []
                            row["billable"] = False
                            run_stopped = stop_reason = "spending-cap"
                        else:
                            # The platform charged nothing, so this row is a free delivery and the
                            # counter has to say it. The row is already stored, hence no
                            # billingInactive flag on it: the summary carries the truth.
                            billing_active = False
                            free_delivery = True
                    except Exception as exc:  # noqa: BLE001
                        # The SDK stores a row before it bills, so a billing failure that stored
                        # nothing took no money. The extracted text is kept either way: the row
                        # goes out uncharged through push(), and push() retries a refused write.
                        after = await item_count()
                        Actor.log.error("billing failed for %s: %s", link, exc)
                        if len(charge_errors) < CHARGE_ERROR_LIMIT:
                            charge_errors.append("%s: %s: %s" % (link, type(exc).__name__, str(exc)[:120]))
                        if before is not None and after is not None:
                            wrote = after > before
                        else:
                            # The row count cannot be read, so calling the row lost would be a
                            # guess: it stays a delivery and the summary says it is unverified.
                            Actor.log.warning("could not verify the dataset row count for %s", link)
                            summary["datasetWriteUnverified"] += 1
                            wrote = True
                        # Either the data already reached the buyer or it is about to: both are
                        # deliveries the platform did not charge for, and the summary says so.
                        pushed = wrote
                        free_delivery = True
                        if wrote:
                            # The row was stored, so the platform did bill and the call still
                            # failed: that is a billing failure, counted by strike.
                            consecutive_failures += 1
                            if consecutive_failures >= 2:
                                billing_active = False
                                if stop_reason is None:
                                    run_stopped = stop_reason = "charge-error"
                        # Nothing was stored, so the call never reached the charge: push() decides
                        # whether the row is lost and names it "dataset-write-error", not billing.

            if pushed:
                count_row(row)
            else:
                pushed = await push(row)
            if pushed:
                if free_delivery:
                    # Counted only after the row really landed: a free delivery that never
                    # reached the buyer is a lost row, not a free one.
                    summary["deliveredUncharged"] += 1
                if row["status"] == pdf.STATUS_EXTRACTED:
                    summary["delivered"] += 1

        for offset, link in enumerate(beyond, len(links) + 1):
            extra = _row(offset, link, STATUS_LIMIT,
                         "Not read: maxDocuments is %d and this link is beyond the limit." % max_docs)
            await push(extra)

        # One place counts both buckets, so requested == processed + notChecked holds even when
        # the dataset refused rows or the cap cut the tail.
        summary["processed"] = sum(count for status, count in summary["byStatus"].items()
                                  if not status.startswith("NOT_CHECKED"))
        summary["notChecked"] = sum(count for status, count in summary["byStatus"].items()
                                    if status.startswith("NOT_CHECKED"))

        summary["billingActive"] = billing_active
        summary["stopReason"] = stop_reason
        # How many rows this run wrote is what we know for sure: the dataset metadata
        # read right after the writes can still be behind, and a zero there reads as
        # "nothing was delivered" while the rows do sit in the dataset.
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

        Actor.log.info("done: requested %s, rows %s, documents with text %s, charged %s, stop %s",
                       summary["requested"], summary["rowsDelivered"], summary["delivered"],
                       summary["chargedEvents"], stop_reason)

        if summary["delivered"] == 0 and links:
            if stop_reason == "spending-cap":
                await Actor.fail(status_message=(
                    "The spending cap was reached before any document was delivered: %d link(s) were sent. "
                    "Raise the run's max total charge and run again." % len(links)))
            elif stop_reason == "dataset-write-error":
                await Actor.fail(status_message=(
                    "The run dataset refused to store rows: %d of %d link(s) came back as rows and the "
                    "rest are reported as not checked. Nothing was charged for the lost rows."
                    % (summary["rowsDelivered"], requested)))
            elif set(summary["byStatus"]) <= LINK_ERROR_STATUSES:
                # A list of typos or non-PDF pages is not a job done: a green run with zero
                # documents reads as success, so it stops loudly with the reason per link.
                await Actor.fail(status_message=_no_document_message(summary["byStatus"], requested))
            elif any(status in HONEST_STATUSES for status in summary["byStatus"]):
                # Scans, locked files or an unpaid run: the buyer got an answer per row.
                Actor.log.info("no text delivered, but every row is an answer: not a failed run")
            else:
                await Actor.fail(status_message=_no_document_message(summary["byStatus"], requested))


if __name__ == "__main__":
    asyncio.run(main())
