"""Apify Actor: SmartRecruiters Jobs API, every open posting of a board, with filters.

Input  : `companies` (board identifiers or careers URLs), `query`, `country`,
         `releasedAfter`, `keywords`, `excludeKeywords`, `location`, `remoteOnly`,
         `employmentType`, `experienceLevel`, `department`, `maxJobsPerCompany`,
         `includeDetails`, `maxDetails`, `requestPaceSecs`.
Output : one row per posting: title, company, department, function, industry,
         experience level, employment type, location with coordinates, remote and
         hybrid flags, reference number, release date, URL and, when asked, the job
         description, qualifications and additional information.
Money  : PAY_PER_EVENT, one charge per delivered posting. A row that reports a board
         nobody owns, an empty board, a failed page or a filtered posting is in the
         dataset but costs nothing. A row is written and charged in one SDK call, so
         nothing is paid for and not delivered.

Honesty rules that live in code, not in promises:
* a board nobody owns and a board with no open roles both answer HTTP 200 with
  totalFound=0, so the page title decides which one it is and the row says it;
* function/typeOfEmployment/experienceLevel/department are filtered in code, because
  the API ignores those parameters silently (measured 07.11.2026), and paying for a
  row the buyer excluded is the same lie as charging for data nobody received;
* a page that fails after the first one succeeded is a truncated board with a reason,
  never a complete one;
* postings the run did not reach and postings the board has but the run did not read
  are counted, so the counter sums match the board total;
* a write that raised and a dataset counter that did not move is an unknown outcome,
  neither delivered nor lost.
"""
from __future__ import annotations

import asyncio
import os
import time
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from apify import Actor

from src import sc

SUMMARY_STORE = "smartrecruiters-jobs-api-summary"
CHARGE_EVENT = "job-delivered"
STATUS_MESSAGE_LIMIT = 400
DEFAULT_MAX_JOBS = 500
DEFAULT_MAX_DETAILS = 300


def _dep_versions() -> str:
    out = []
    for name in ("apify",):
        try:
            out.append("%s %s" % (name, _pkg_version(name)))
        except PackageNotFoundError:
            out.append("%s unknown" % name)
    return ", ".join(out)


def _num(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


async def _item_count():
    """Rows the run dataset holds now, or None when it cannot be asked."""
    try:
        dataset = await Actor.open_dataset()
    except Exception:  # noqa: BLE001 - a local run may carry no dataset handle
        return None
    info = None
    for getter in ("get_metadata", "get_info"):
        method = getattr(dataset, getter, None)
        if method is None:
            continue
        try:
            info = await method()
            break
        except Exception:  # noqa: BLE001
            continue
    if info is None:
        return None
    if isinstance(info, dict):
        for key in ("item_count", "itemCount"):
            if info.get(key) is not None:
                return int(info[key])
        return None
    for attr in ("item_count", "itemCount"):
        value = getattr(info, attr, None)
        if value is not None:
            return int(value)
    return None


async def _detect_billing(log) -> bool:
    """Whether this run can charge at all.

    Measured on apify 4.2.0: prices live on the charging manager, not on the Actor. A
    run with no pricing (local runs, test doubles) keeps billing off and simply cannot
    charge; a run whose pricing has no CHARGE_EVENT delivers rows free and logs why,
    instead of reading "nothing was charged" as a spending cap.
    """
    try:
        pricing = Actor.get_charging_manager().get_pricing_info()
    except Exception:  # noqa: BLE001 - local run or a double without a charging manager
        return False
    if pricing is None:
        log.info("no pricing is configured for this run: rows are delivered without a charge")
        return False
    prices = getattr(pricing, "per_event_prices", None) or {}
    if not getattr(pricing, "is_pay_per_event", False):
        log.error("this Actor is not pay-per-event: rows are delivered without a charge")
        return False
    if CHARGE_EVENT not in prices:
        log.error("pricing has no %r event: rows are delivered without a charge", CHARGE_EVENT)
        return False
    return True


async def _push(row: dict, summary: dict, log, billing: bool, state: dict):
    """Write a row and charge for it in the same call. Returns (delivered, cap, outcome).

    `outcome` is "charged" (in the dataset and paid for), "free" (in the dataset and not
    charged: no billing in this run or the budget let the row through for nothing) or
    "unverified" (the write raised and the dataset counter could not confirm the row).

    Measured in apify 4.2.0: push_data stores the row and then charges it under one
    lock, so an exception can arrive AFTER the row is already in the dataset. Repeating
    such a write duplicates the row for the buyer, so a paid write is never repeated:
    one attempt, and the dataset counter decides what landed.
    """
    attempts = 1 if billing else 2
    for attempt in range(1, attempts + 1):
        try:
            if billing:
                result = await Actor.push_data(row, charged_event_name=CHARGE_EVENT)
                charged = int(getattr(result, "charged_count", 0) or 0)
                cap = bool(getattr(result, "event_charge_limit_reached", False))
                if charged:
                    summary["chargedEvents"] += charged
                    state["written"] += 1
                    return True, cap, "charged"
                if cap:
                    # measured: when nothing may be written, the SDK writes nothing
                    return False, True, "free"
                summary["deliveredUncharged"] += 1
                state["written"] += 1
                return True, False, "free"
            await Actor.push_data(row)
            state["written"] += 1
            return True, False, "free"
        except Exception as exc:  # noqa: BLE001
            summary["writeFailures"] += 1
            log.error("dataset write failed for %s (attempt %d of %d): %s",
                      row.get("jobId") or row.get("company"), attempt, attempts, exc)
            if billing:
                return await _after_write_failure(summary, log, state, billing=True)
            after = await _item_count()
            if after is not None and after > state["written"]:
                state["written"] = after
                return True, False, "free"
            if attempt == attempts:
                return await _after_write_failure(summary, log, state, billing=False)
    return False, False, "unverified"


async def _after_write_failure(summary: dict, log, state: dict, billing: bool = True):
    """What a failed write left behind, decided by the dataset counter and nothing else.

    Measured 07.11.2026: dataset metadata lags the write, so a counter that has not
    moved is not a lost row. One short re-read is taken; if the row still does not
    show, the outcome is "unverified" and the caller counts it as such.
    """
    after = await _item_count()
    if after is not None and after <= state["written"]:
        await asyncio.sleep(2)
        after = await _item_count()
    if after is not None and after > state["written"]:
        state["written"] = after
        if billing:
            # only a paid row that landed without its charge belongs here: a status row
            # is free by design, so counting it would inflate the money counters
            summary["deliveredUncharged"] += 1
            summary["chargeUncertain"] += 1
        return True, False, "free"
    if billing:
        summary["chargeUncertain"] += 1
    return False, False, "unverified"


async def _set_status_message(text: str, log) -> None:
    """Tell the buyer in the run header why a green run delivered nothing.

    Measured in apify 4.2.0: set_status_message is a coroutine. A call without await
    returns it, sends nothing and leaves the header empty.
    """
    if not hasattr(Actor, "set_status_message"):
        log.info("status: %s", text)
        return
    try:
        await Actor.set_status_message(text[:STATUS_MESSAGE_LIMIT])
    except Exception as exc:  # noqa: BLE001
        log.warning("could not set the status message: %s", exc)


def _status_row(status: str, company, reason: str | None) -> dict:
    """A row that says a board could not be read, and why."""
    return {"status": status, "company": company, "jobId": None, "jobTitle": None,
            "reason": reason, "detailFetched": False}


def _money_text(summary: dict) -> str:
    if summary["billingActive"]:
        return "charged %d, unpaid %d, uncertain %d" % (
            summary["chargedEvents"], summary["deliveredUncharged"], summary["chargeUncertain"])
    return "billing is off in this run, so nothing was charged"


async def main() -> None:
    async with Actor:
        log = Actor.log
        log.info("deps: %s", _dep_versions())

        actor_input = await Actor.get_input() or {}
        raw_companies = actor_input.get("companies") or []
        if isinstance(raw_companies, str):
            raw_companies = [line for line in raw_companies.splitlines() if line.strip()]

        planned = []
        seen = set()
        deduplicated = 0
        for raw in raw_companies:
            parsed = sc.parse_company(raw)
            key = (parsed.get("identifier") or "").lower()
            if parsed["error"] is None and key in seen:
                # the same board typed twice is one board; reading it twice would
                # deliver and charge the same postings twice
                deduplicated += 1
                continue
            if parsed["error"] is None:
                seen.add(key)
            planned.append(parsed)

        query = str(actor_input.get("query") or "").strip()
        country = str(actor_input.get("country") or "").strip().lower()
        released_after = str(actor_input.get("releasedAfter") or "").strip()
        max_jobs = int(_num(actor_input.get("maxJobsPerCompany"), DEFAULT_MAX_JOBS, 0, 100000))
        include_details = bool(actor_input.get("includeDetails", False))
        max_details = int(_num(actor_input.get("maxDetails"), DEFAULT_MAX_DETAILS, 0, 100000))
        pace = _num(actor_input.get("requestPaceSecs"), 0.3, 0.0, 30.0)

        settings = {
            "keywords": sc.split_words(str(actor_input.get("keywords") or "")),
            "exclude_keywords": sc.split_words(str(actor_input.get("excludeKeywords") or "")),
            "location": str(actor_input.get("location") or "").strip().lower(),
            "only_remote": bool(actor_input.get("remoteOnly", False)),
            "employment_type": str(actor_input.get("employmentType") or ""),
            "experience_level": str(actor_input.get("experienceLevel") or ""),
            "department": str(actor_input.get("department") or ""),
        }

        country_note = None
        if country and not (len(country) == 2 and country.isalpha()):
            # the API wants a lowercase ISO-2 code; anything else is dropped rather
            # than sent to be silently ignored
            country_note = ("country %r is not an ISO-2 code such as us or de, so it was "
                            "not applied" % country)
            log.warning(country_note)
            country = ""
        released_note = None
        if released_after and not (len(released_after) >= 10 and released_after[4] == "-"):
            released_note = ("releasedAfter %r is not an ISO date such as 2026-11-01, so it "
                             "was not applied" % released_after)
            log.warning(released_note)
            released_after = ""

        client = sc.Client(pace=pace)
        detail_client = sc.Client(pace=min(pace, 0.1))
        billing = await _detect_billing(log)
        started = time.monotonic()
        write_state = {"written": 0}
        by_status: dict[str, int] = {}
        summary = {
            "companiesAsked": len(planned),
            "companiesDeduplicated": deduplicated,
            "companiesOk": 0,
            "companiesNotFound": 0,
            "companiesEmpty": 0,
            "companiesError": 0,
            "companiesInvalid": 0,
            "companiesNotChecked": 0,
            "jobsAsked": 0,
            "jobsSeen": 0,
            "jobsDelivered": 0,
            "jobsUndelivered": 0,
            "jobsUnverified": 0,
            "jobsNotProcessed": 0,
            "jobsNotRead": 0,
            "filteredOut": 0,
            "filteredBy": {},
            "duplicatesSkipped": 0,
            "pagesRead": 0,
            "pagesFailed": 0,
            "truncatedCompanies": 0,
            "detailsFetched": 0,
            "detailFailures": 0,
            "detailsSkipped": 0,
            "requests": 0,
            "byCompany": {},
            "chargedEvents": 0,
            "deliveredUncharged": 0,
            "chargeUncertain": 0,
            "billingActive": billing,
            "writeFailures": 0,
            "datasetItemCount": None,
            "datasetItemCountLag": False,
            "stopReason": "completed",
            "elapsedSecs": 0,
            "maxJobsPerCompany": max_jobs,
            "includeDetails": include_details,
            "maxDetails": max_details,
            "requestPaceSecs": pace,
            "query": query or None,
            "country": country or None,
            "releasedAfter": released_after or None,
            "countryIgnored": country_note,
            "releasedAfterIgnored": released_note,
            "lastBoardError": None,
        }

        stop_reason = "completed"

        for index, item in enumerate(planned):
            identifier = item.get("identifier")
            if identifier is None:
                summary["companiesInvalid"] += 1
                row = _status_row("INVALID_INPUT", item.get("raw"), item["error"])
                by_status["INVALID_INPUT"] = by_status.get("INVALID_INPUT", 0) + 1
                await _push(row, summary, log, False, write_state)
                continue

            stats = {"read": 0, "delivered": 0, "undelivered": 0, "unverified": 0,
                     "filteredOut": 0, "notProcessed": 0, "notRead": 0,
                     "pages": 0, "pagesFailed": 0, "details": 0, "detailFailed": 0,
                     "truncated": False, "truncatedReason": None, "boardTotal": 0,
                     "status": None}
            try:
                listed = sc.list_postings(client, identifier, query=query, country=country,
                                          released_after=released_after, max_jobs=max_jobs)
            except sc.ScError as exc:
                summary["lastBoardError"] = "%s: %s" % (identifier, exc)
                if sc.board_name(client, identifier) is None:
                    summary["companiesNotFound"] += 1
                    stats["status"] = "BOARD_NOT_FOUND"
                    row = _status_row(
                        "NOT_CHECKED_BOARD_NOT_FOUND", identifier,
                        "SmartRecruiters has no board %r: %s. Check the identifier, it is the "
                        "one in jobs.smartrecruiters.com/<identifier>." % (identifier, exc))
                    by_status["NOT_CHECKED_BOARD_NOT_FOUND"] = \
                        by_status.get("NOT_CHECKED_BOARD_NOT_FOUND", 0) + 1
                else:
                    summary["companiesError"] += 1
                    stats["status"] = "ERROR"
                    # Measured 07.11.2026: a board that was not read at all is not a
                    # truncated board, there is no partial list to warn about. It belongs
                    # to companiesError, and truncatedCompanies stays reserved for boards
                    # whose list really was cut short (page failure, maxJobsPerCompany,
                    # spending cap), so the flag and the counter can never disagree.
                    row = _status_row("NOT_CHECKED_ERROR", identifier, str(exc))
                    by_status["NOT_CHECKED_ERROR"] = by_status.get("NOT_CHECKED_ERROR", 0) + 1
                summary["byCompany"][identifier] = stats
                await _push(row, summary, log, False, write_state)
                continue

            stats["pages"] = listed["pages"]
            stats["pagesFailed"] = listed["pagesFailed"]
            stats["boardTotal"] = listed["total"]
            summary["pagesRead"] += listed["pages"]
            summary["duplicatesSkipped"] += int(listed.get("duplicates") or 0)
            summary["pagesFailed"] += listed["pagesFailed"]
            summary["jobsAsked"] += listed["total"]
            if listed["truncated"]:
                summary["truncatedCompanies"] += 1
                stats["truncated"] = True
                stats["truncatedReason"] = listed["truncatedReason"]

            if not listed["postings"]:
                name = sc.board_name(client, identifier)
                if name is None:
                    summary["companiesNotFound"] += 1
                    stats["status"] = "BOARD_NOT_FOUND"
                    row = _status_row(
                        "NOT_CHECKED_BOARD_NOT_FOUND", identifier,
                        "SmartRecruiters answered with no postings and the public page shows no "
                        "company, so board %r does not exist. Nothing was charged." % identifier)
                    by_status["NOT_CHECKED_BOARD_NOT_FOUND"] = \
                        by_status.get("NOT_CHECKED_BOARD_NOT_FOUND", 0) + 1
                else:
                    summary["companiesEmpty"] += 1
                    stats["status"] = "BOARD_EMPTY"
                    row = _status_row(
                        "NOT_CHECKED_BOARD_EMPTY", identifier,
                        "The board of %s exists and has no open postings%s. Nothing was charged."
                        % (name, " matching your filters" if (query or country or released_after)
                           else ""))
                    by_status["NOT_CHECKED_BOARD_EMPTY"] = \
                        by_status.get("NOT_CHECKED_BOARD_EMPTY", 0) + 1
                summary["byCompany"][identifier] = stats
                await _push(row, summary, log, False, write_state)
                continue

            not_read = max(0, listed["total"] - len(listed["postings"]))
            if not_read:
                summary["jobsNotRead"] += not_read
                stats["notRead"] = not_read
            stats["status"] = "OK"
            summary["companiesOk"] += 1

            candidates = []
            for post in listed["postings"]:
                summary["jobsSeen"] += 1
                stats["read"] += 1
                row = sc.normalize(post, identifier)
                reason = sc.filter_reason(row, settings)
                if reason:
                    summary["filteredOut"] += 1
                    summary["filteredBy"][reason] = summary["filteredBy"].get(reason, 0) + 1
                    stats["filteredOut"] += 1
                    continue
                candidates.append(row)

            processed = 0
            for position, row in enumerate(candidates):
                processed = position + 1
                if include_details:
                    if max_details and summary["detailsFetched"] >= max_details:
                        summary["detailsSkipped"] += 1
                    else:
                        try:
                            row.update(sc.fetch_detail(detail_client, identifier, row["jobId"]))
                            row["detailFetched"] = True
                            summary["detailsFetched"] += 1
                            stats["details"] += 1
                        except sc.ScError as exc:
                            row["detailError"] = str(exc)
                            summary["detailFailures"] += 1
                            stats["detailFailed"] += 1
                delivered, cap, outcome = await _push(row, summary, log, billing, write_state)
                if delivered:
                    summary["jobsDelivered"] += 1
                    stats["delivered"] += 1
                    by_status["DELIVERED"] = by_status.get("DELIVERED", 0) + 1
                elif outcome == "unverified":
                    summary["jobsUnverified"] += 1
                    stats["unverified"] += 1
                elif cap:
                    # measured: at the spending cap the SDK writes nothing, so this row was
                    # neither delivered nor lost. It belongs to the not-processed bucket;
                    # counting it as undelivered would name a row that was never attempted.
                    processed = position
                else:
                    summary["jobsUndelivered"] += 1
                    stats["undelivered"] += 1
                if cap:
                    stop_reason = "spending-cap"
                    break

            leftover = len(candidates) - processed
            if leftover:
                summary["jobsNotProcessed"] += leftover
                stats["notProcessed"] = leftover
                if not stats["truncated"]:
                    stats["truncated"] = True
                    summary["truncatedCompanies"] += 1
                stats["truncatedReason"] = stats["truncatedReason"] or "spending-cap"
            summary["byCompany"][identifier] = stats
            if stop_reason == "spending-cap":
                remaining = len(planned) - index - 1
                if remaining:
                    summary["companiesNotChecked"] += remaining
                break

        # the counters the README promises to add up; a mismatch is shown, not rounded away
        buckets = (summary["jobsDelivered"] + summary["filteredOut"] + summary["jobsUndelivered"]
                   + summary["jobsUnverified"] + summary["jobsNotProcessed"])
        if buckets != summary["jobsSeen"]:
            summary["counterMismatch"] = {"jobsSeen": summary["jobsSeen"], "buckets": buckets}
            log.error("counter mismatch: jobsSeen %s, buckets %s", summary["jobsSeen"], buckets)
        asked = summary["jobsSeen"] + summary["jobsNotRead"]
        if asked != summary["jobsAsked"]:
            summary["askedMismatch"] = {"jobsAsked": summary["jobsAsked"], "seenPlusNotRead": asked}
            log.error("asked mismatch: jobsAsked %s, seen+notRead %s", summary["jobsAsked"], asked)
        company_buckets = (summary["companiesOk"] + summary["companiesNotFound"]
                           + summary["companiesEmpty"] + summary["companiesError"]
                           + summary["companiesInvalid"] + summary["companiesNotChecked"])
        if company_buckets != summary["companiesAsked"]:
            summary["companyCounterMismatch"] = {"companiesAsked": summary["companiesAsked"],
                                                 "buckets": company_buckets}
            log.error("company counter mismatch: asked %s, buckets %s",
                      summary["companiesAsked"], company_buckets)
        truncated_boards = sum(1 for board in summary["byCompany"].values()
                               if board.get("truncated"))
        if truncated_boards != summary["truncatedCompanies"]:
            # Measured 07.11.2026: the flag was set on boards nothing had truncated and
            # the counter stayed behind at the spending cap, so the summary disagreed
            # with itself. A mismatch is printed, never rounded away.
            summary["truncatedMismatch"] = {"truncatedCompanies": summary["truncatedCompanies"],
                                            "byCompany": truncated_boards}
            log.error("truncated mismatch: counter %s, boards %s",
                      summary["truncatedCompanies"], truncated_boards)
        summary["byStatus"] = by_status

        summary["stopReason"] = stop_reason
        summary["elapsedSecs"] = round(time.monotonic() - started, 2)
        summary["requests"] = client.requests + detail_client.requests

        api_count = await _item_count()
        if api_count is not None:
            rows_expected = (summary["jobsDelivered"] + summary["companiesNotFound"]
                             + summary["companiesEmpty"] + summary["companiesError"]
                             + summary["companiesInvalid"])
            if api_count < rows_expected:
                await asyncio.sleep(2)
                again = await _item_count()
                if again is not None:
                    api_count = again
                if api_count < rows_expected:
                    summary["datasetItemCountLag"] = True
                    api_count = None
        summary["datasetItemCount"] = api_count

        try:
            store = await Actor.open_key_value_store(name=SUMMARY_STORE)
            run_id = (os.environ.get("ACTOR_RUN_ID")
                      or Actor.get_env().get("run_id")
                      or "run")
            summary["runId"] = run_id
            await store.set_value("RUN_SUMMARY", summary)
            await store.set_value("RUN_SUMMARY-%s" % run_id, summary)
        except Exception as exc:  # noqa: BLE001
            log.warning("the named summary store is unavailable: %s", exc)
        try:
            run_store = await Actor.open_key_value_store()
            await run_store.set_value("RUN_SUMMARY", summary)
        except Exception as exc:  # noqa: BLE001
            log.warning("the run summary could not be stored: %s", exc)

        log.info("done: boards ok %s, not found %s, empty %s, seen %s, delivered %s, "
                 "filtered %s, details %s, stop %s, %s",
                 summary["companiesOk"], summary["companiesNotFound"], summary["companiesEmpty"],
                 summary["jobsSeen"], summary["jobsDelivered"], summary["filteredOut"],
                 summary["detailsFetched"], stop_reason, _money_text(summary))

        if not planned:
            await Actor.fail(status_message=(
                "No companies in the input: `companies` was empty, so nothing could be read. "
                "Put a SmartRecruiters board identifier such as SGS, or the careers URL "
                "https://jobs.smartrecruiters.com/SGS."))
            return

        if summary["jobsDelivered"] == 0:
            if summary["companiesOk"] == 0 and summary["companiesEmpty"] == 0:
                # Measured 07.11.2026 on the published build: the text was built as
                # companiesAsked - companiesInvalid, so a run whose only input was invalid
                # said "None of the 0 board(s) could be read: no such board" while the
                # dataset held INVALID_INPUT. The header now names each bucket that is not
                # zero, and a board count is never reduced by the invalid ones.
                reasons = []
                if summary["companiesInvalid"]:
                    examples = [str(item.get("raw")) for item in planned
                                if item.get("identifier") is None][:3]
                    reasons.append(
                        "%d of %d input(s) are not board identifiers (%s)"
                        % (summary["companiesInvalid"], summary["companiesAsked"],
                           ", ".join(examples)))
                if summary["companiesNotFound"]:
                    reasons.append("%d board(s) do not exist" % summary["companiesNotFound"])
                if summary["companiesError"]:
                    reasons.append("%d board(s) could not be read (%s)"
                                   % (summary["companiesError"],
                                      summary["lastBoardError"] or "see the log"))
                await Actor.fail(status_message=(
                    "No posting was read: %s. Every input is reported as a row with its own "
                    "reason, so the dataset shows which one was wrong. Nothing was delivered "
                    "and nothing was charged." % ("; ".join(reasons) or "no board was tried")
                )[:STATUS_MESSAGE_LIMIT])
            elif summary["jobsSeen"] == 0:
                await _set_status_message(
                    "The board(s) answered with no open postings%s. Nothing was delivered and "
                    "nothing was charged."
                    % (" matching your filters" if (query or country or released_after) else ""),
                    log)
            else:
                await _set_status_message(
                    "%d posting(s) read in %.1f s and none was delivered: filters dropped %d %s, "
                    "write failures %d. The summary says which filter did it."
                    % (summary["jobsSeen"], summary["elapsedSecs"], summary["filteredOut"],
                       summary["filteredBy"], summary["writeFailures"]), log)


if __name__ == "__main__":
    asyncio.run(main())
