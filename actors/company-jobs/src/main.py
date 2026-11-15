"""Apify Actor: every open job of any company, straight from its ATS.

Input  : company domains / careers URLs / board slugs.
Output : one row per open job, same fields for every ATS.
Charge : `job-found` - only for jobs actually delivered. A company with no
         open jobs costs the buyer nothing, and a company we could not resolve
         is reported, never silently dropped.

Cache  : resolving one company costs up to a dozen probes (slug guesses plus a
         read of its careers page), so an answer that worked is kept in a NAMED
         key-value store for `cacheMinutes`. Two things follow: a repeated run
         does not hammer the same board, and a buyer asking about the same
         company every hour does not pay us with his own waiting time. The store
         is named, therefore it lives in the account of whoever runs the Actor -
         their storage, their data, never mixed with another customer's. A
         failed resolution is cached for half the window only: a board that was
         down ten minutes ago must not stay dead for an hour.

The heavy lifting (ATS detection) lives in src/ats.py and is testable without
the Apify SDK.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from apify import Actor

from src import ats

CHARGE_EVENT = "job-found"
WORKERS = 10
CACHE_STORE = "company-jobs-cache"
CACHE_KEY_PREFIX = "jobs_"
DEFAULT_CACHE_MINUTES = 60
MIN_ERROR_CACHE_SECONDS = 300
NOT_CHECKED_STATUSES = ("NOT_CHECKED", "NOT_CHECKED_CAP", "NOT_CHECKED_MAX_COMPANIES",
                        "NOT_CHECKED_BILLING")


def _input_items(raw) -> list[str]:
    """Every non-empty input line, duplicates kept - the honest "asked" count."""
    if isinstance(raw, str):
        raw = [part for part in raw.replace("\n", ",").split(",")]
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            item = item.get("domain") or item.get("url") or item.get("company") or ""
        item = str(item).strip()
        if item:
            out.append(item)
    return out


def _targets(raw) -> list[str]:
    """Input lines with case-insensitive duplicates collapsed."""
    out, seen = [], set()
    for item in _input_items(raw):
        if item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def cache_key(company: str, detail: bool) -> str:
    """Same company + same detail level = same cache entry."""
    raw = f"{company.strip().lower()}|{'d' if detail else 'l'}".encode("utf-8")
    return CACHE_KEY_PREFIX + hashlib.sha1(raw).hexdigest()[:16]


def job_key(ats_name, board_slug, job, position=None):
    """Stable identity of one job inside one board.

    jobId is the honest key, the apply url the fallback. A board that carries
    neither still must not collapse into one row, so the job's own fields are
    used next, and only a job with nothing at all falls back to its position.
    """
    identity = job.get("jobId") or job.get("url")
    if not identity:
        stamp = "|".join(str(job.get(field) or "") for field in
                         ("jobTitle", "location", "department", "postedAt"))
        if stamp.strip("|"):
            identity = "fp_" + hashlib.sha1(stamp.encode()).hexdigest()[:16]
    if not identity:
        identity = f"pos{position if position is not None else 0}"
    return f"{ats_name}:{board_slug}:{identity}"


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        targets = _targets(inp.get("companies"))
        deduplicated = max(0, len(_input_items(inp.get("companies"))) - len(targets))
        if not targets:
            # A buyer who presses Run with an empty box must get a readable stop, not a
            # Python traceback: the run has to say what to fix.
            await Actor.fail(
                status_message="companies is empty - pass at least one domain, careers URL or board slug"
            )
            return

        max_companies = int(inp.get("maxCompanies") or 50)
        # The cut is remembered, not forgotten: companies beyond maxCompanies
        # were never looked at, and a summary that counts only the short list
        # reads as "asked 50" while the buyer sent 100.
        requested = list(targets)
        targets = requested[:max_companies]
        over_max = requested[max_companies:]
        keywords = str(inp.get("keywords") or "").strip()
        location = str(inp.get("location") or "").strip()
        only_remote = bool(inp.get("onlyRemote", False))
        include_desc = bool(inp.get("includeDescription", False))
        per_company = int(inp.get("maxJobsPerCompany") or 0)
        include_empty = bool(inp.get("includeEmptyCompanies", False))
        cache_minutes = int(inp.get("cacheMinutes", DEFAULT_CACHE_MINUTES) or 0)
        cache_seconds = max(0, cache_minutes) * 60

        cache = None
        if cache_seconds:
            try:
                cache = await Actor.open_key_value_store(name=CACHE_STORE)
            except Exception as exc:  # a local run without the platform must not crash
                Actor.log.warning(f"Cache store unavailable, running without cache: {exc}")

        started = time.time()
        now = started
        cached: dict[str, dict] = {}
        to_pull: list[str] = []

        for company in targets:
            entry = None
            if cache is not None:
                try:
                    entry = await cache.get_value(cache_key(company, include_desc))
                except Exception as exc:
                    Actor.log.warning(f"Cache read failed for {company}: {exc}")
            if isinstance(entry, dict) and isinstance(entry.get("result"), dict):
                result = entry["result"]
                ttl = max(MIN_ERROR_CACHE_SECONDS, cache_seconds // 2) if result.get("error") else cache_seconds
                if now - float(entry.get("fetchedAt") or 0) <= ttl:
                    cached[company] = result
                    continue
            to_pull.append(company)

        Actor.log.info(f"Resolving {len(targets)} of {len(requested)} companies "
                       f"(keywords={keywords or 'any'}, location={location or 'any'}, "
                       f"onlyRemote={only_remote}, descriptions={include_desc}, "
                       f"cache={cache_minutes}m: {len(cached)} fresh, {len(to_pull)} to fetch)")

        pulled: dict[str, dict] = {}
        if to_pull:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=min(WORKERS, len(to_pull))) as pool:
                fresh = await asyncio.gather(*[
                    loop.run_in_executor(pool, ats.pull, company, include_desc) for company in to_pull
                ], return_exceptions=True)
            # One unexpected exception in a single board must not take the whole run down:
            # the company becomes FAILED with a reason and the rest are still counted.
            pulled = {}
            for company, answer in zip(to_pull, fresh):
                if isinstance(answer, BaseException):
                    Actor.log.warning(f"{company}: pull raised {type(answer).__name__}: {answer}")
                    answer = {"input": company, "ats": None, "slug": None, "count": 0,
                              "error": f"PULL_FAILED: {type(answer).__name__}",
                              "hint": "the board could not be read: the fetcher raised an exception"}
                pulled[company] = answer
            if cache is not None:
                for company, result in pulled.items():
                    try:
                        await cache.set_value(cache_key(company, include_desc),
                                              {"fetchedAt": now, "result": result})
                    except Exception as exc:
                        Actor.log.warning(f"Cache write failed for {company}: {exc}")

        results = [cached.get(company) or pulled.get(company) for company in targets]
        results = [result for result in results if result]

        report, seen_ids = [], set()
        boards: dict[str, str] = {}
        delivered = failed_companies = 0
        stopped = False
        stop_reason = None
        billing_active = True
        charge_enabled = True
        charge_failures = 0

        for result in results:
            company = result.get("input")
            entry = {
                "input": company,
                "domain": result.get("domain"),
                "ats": result.get("ats"),
                "boardSlug": result.get("slug"),
                "jobsOnBoard": result.get("count", 0),
                "delivered": 0,
                "fromCache": company in cached,
                "error": result.get("error"),
            }

            if result.get("error"):
                failed_companies += 1
                entry["status"] = "FAILED"
                Actor.log.warning(f"{company}: {result['error']}")
                report.append(entry)
                if include_empty:
                    await Actor.push_data({
                        "company": result.get("display") or company,
                        "domain": result.get("domain"),
                        "ats": None, "jobId": None, "jobTitle": None,
                        "status": "FAILED", "error": result.get("error"),
                        "reason": result.get("hint"),
                        "fromCache": entry["fromCache"],
                    })
                continue

            # Two input rows can point at the same board (stripe.com and
            # https://stripe.com/careers). The second is not dropped: its answer may be
            # fresher than the first one, so it goes through the same seen_ids gate - a job
            # already delivered is skipped, a job only this answer carries is delivered
            # once and charged once.
            board_key = f"{result.get('ats')}:{result.get('slug')}"
            duplicate_of = boards.get(board_key)
            if duplicate_of is None:
                boards[board_key] = company
            else:
                entry["duplicateOf"] = duplicate_of

            # Filter once without the per-company cap, so the jobs the cap keeps back
            # are reported instead of vanishing from the summary.
            full = ats.filter_jobs(result.get("jobs") or [], keywords, location, only_remote, 0)
            ats_name, board_slug = result.get("ats"), result.get("slug")
            if duplicate_of is None:
                matched = full[:per_company] if per_company else full
                entry["jobsMatched"] = len(full)
                entry["jobsOverMaxJobsPerCompany"] = max(0, len(full) - len(matched))
            else:
                # The cap belongs to the jobs this answer would add, not to its raw list:
                # cutting the raw list first reported "nothing new" while three fresh jobs
                # sat behind the already delivered ones.
                fresh = [job for job in full if job_key(ats_name, board_slug, job) not in seen_ids]
                matched = fresh[:per_company] if per_company else fresh
                entry["jobsOverMaxJobsPerCompany"] = max(0, len(fresh) - len(matched))
            company_partial = False
            for job in matched:
                # A job without an id must not collapse the whole board into one row: the
                # key falls back to its url, then to a fingerprint of the job itself, and
                # only then to its position (a positional key charged twice when the same
                # board came back in another order).
                key = job_key(ats_name, board_slug, job, entry["delivered"])
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                row = {
                    "company": result.get("display") or company,
                    "domain": result.get("domain"),
                    "ats": result.get("ats"),
                    "jobId": job.get("jobId"),
                    "jobTitle": job.get("jobTitle"),
                    "department": job.get("department"),
                    "location": job.get("location"),
                    "remote": job.get("remote"),
                    "employmentType": job.get("employmentType"),
                    "salaryMin": job.get("salaryMin"),
                    "salaryMax": job.get("salaryMax"),
                    "salaryCurrency": job.get("salaryCurrency"),
                    "salaryPeriod": job.get("salaryPeriod"),
                    "postedAt": job.get("postedAt"),
                    "url": job.get("url"),
                    "status": "FOUND",
                    "fromCache": entry["fromCache"],
                    "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                if include_desc:
                    row["jobDescription"] = job.get("jobDescription")
                await Actor.push_data(row)
                delivered += 1
                entry["delivered"] += 1

                if charge_enabled:
                    try:
                        charge = await Actor.charge(event_name=CHARGE_EVENT)
                        charge_failures = 0
                        if getattr(charge, "event_charge_limit_reached", False):
                            billing_active = True
                            Actor.log.warning("User spending limit reached - stopping")
                            stopped = True
                            stop_reason = "spending-cap"
                            company_partial = True
                        elif not int(getattr(charge, "charged_count", 1) or 0):
                            # SDK 4.x answers (0, False) when the run is not pay-per-event
                            # or the event name is not in the price list: nothing was
                            # charged, and a summary claiming otherwise lies to the buyer.
                            billing_active = False
                            charge_enabled = False
                            stop_reason = stop_reason or "billing-not-active"
                            Actor.log.warning("Billing is not active for this run - the rest "
                                              "of the rows are delivered without charge")
                        else:
                            billing_active = True
                    except Exception as exc:  # local run without monetization must not crash
                        charge_failures += 1
                        Actor.log.warning(f"Charging failed ({charge_failures} in a row): {exc}")
                        # The row is already delivered, so it stays. But handing out the rest
                        # of the board for free is not a favour to the buyer, it is a silent
                        # loss - the run stops on the second failure in a row and says so.
                        if charge_failures >= 2:
                            billing_active = False
                            charge_enabled = False
                            stopped = True
                            stop_reason = "billing-error"
                            company_partial = True
                if stopped:
                    break

            # The cap stopped the run in the middle of this company's jobs: the board was
            # read, but only part of it was delivered. Saying nothing here made a half-read
            # board look fully processed.
            if company_partial and entry["delivered"] < len(matched):
                entry["partial"] = True
                over = entry.get("jobsOverMaxJobsPerCompany") or 0
                why = ("your spending limit (maxTotalChargeUsd)" if stop_reason == "spending-cap"
                       else "two billing failures in a row")
                capped = (f"; {over} more were held back by maxJobsPerCompany" if over else "")
                entry["reason"] = (f"the run stopped at {why} after {entry['delivered']} of "
                                   f"{len(matched)} jobs of this company that it was allowed to "
                                   f"deliver{capped} and the rest were neither delivered nor charged")

            if (include_empty and entry["delivered"] == 0 and not company_partial
                    and not entry.get("duplicateOf")):
                await Actor.push_data({
                    "company": result.get("display") or company,
                    "domain": result.get("domain"),
                    "ats": result.get("ats"),
                    "boardSlug": result.get("slug"),
                    "jobId": None, "jobTitle": None,
                    "status": "NO_JOBS",
                    "jobsOnBoard": result.get("count", 0),
                    "reason": ("the board answered but has no open jobs"
                               if not result.get("count")
                               else "the board has open jobs, none matched your filters"),
                    "fromCache": entry["fromCache"],
                })

            if entry.get("duplicateOf"):
                first = entry["duplicateOf"]
                if entry["delivered"]:
                    # This answer carried jobs the first one did not have. jobsMatched counts a
                    # board once, so those jobs are named separately instead of the summary
                    # contradicting jobsDelivered.
                    entry["jobsFromDuplicate"] = entry["delivered"]
                    entry["reason"] = (f"same board as {first}: this answer was fresher, so the "
                                       f"{entry['delivered']} jobs only it carried were delivered "
                                       f"once (jobs already reported were skipped)")
                    Actor.log.info(f"{company}: same board as {first} - "
                                   f"{entry['delivered']} newer jobs delivered once")
                else:
                    entry["status"] = "DUPLICATE_BOARD"
                    entry["reason"] = (f"this input resolves to the same board as {first}; "
                                       f"its jobs are reported once, under {first}, "
                                       f"and are not charged twice")
                    if include_empty:
                        await Actor.push_data({
                            "company": result.get("display") or company,
                            "domain": result.get("domain"),
                            "ats": result.get("ats"),
                            "boardSlug": result.get("slug"),
                            "jobId": None, "jobTitle": None,
                            "status": "DUPLICATE_BOARD", "reason": entry["reason"],
                            "fromCache": entry["fromCache"],
                        })
                    Actor.log.info(f"{company}: same board as {first} - jobs reported once")

            report.append(entry)
            Actor.log.info(f"{company}: {result.get('ats')} / {result.get('slug')} - "
                           f"{entry['delivered']} of {len(matched)} matched "
                           f"({result.get('count')} on the board) delivered"
                           f"{' (cached)' if entry['fromCache'] else ''}")
            if stopped:
                break

        # Companies the loop never reached (the spending cap stopped it midway) were
        # never checked at all. Saying nothing about them reads as "no open jobs",
        # so every one of them gets its own record with a reason.
        checked = {item.get("input") for item in report}
        skipped = [company for company in targets if company not in checked]
        for company in skipped:
            if stop_reason == "spending-cap":
                reason = ("the run stopped at your spending limit before this company was checked; "
                          "raise maxTotalChargeUsd to cover the whole list")
                status = "NOT_CHECKED_CAP"
            elif stop_reason == "billing-error":
                reason = ("the run stopped after two billing failures before this company was "
                          "checked, so no jobs were handed out for free")
                status = "NOT_CHECKED_BILLING"
            else:
                reason = "the run ended before this company was checked"
                status = "NOT_CHECKED"
            report.append({
                "input": company, "domain": None, "ats": None, "boardSlug": None,
                "jobsOnBoard": 0, "delivered": 0, "fromCache": False, "error": None,
                "status": status, "reason": reason,
            })
            if include_empty:
                await Actor.push_data({
                    "company": company, "domain": None, "ats": None,
                    "jobId": None, "jobTitle": None,
                    "status": status, "reason": reason, "fromCache": False,
                })

        # The input list itself was cut by maxCompanies: those companies were never
        # even looked at, so each gets the same kind of record as the cap tail.
        for company in over_max:
            reason = (f"maxCompanies is {max_companies}: this company was beyond the limit "
                      f"and was not checked; raise maxCompanies to cover the whole list")
            report.append({
                "input": company, "domain": None, "ats": None, "boardSlug": None,
                "jobsOnBoard": 0, "delivered": 0, "fromCache": False, "error": None,
                "status": "NOT_CHECKED_MAX_COMPANIES", "reason": reason,
            })
            if include_empty:
                await Actor.push_data({
                    "company": company, "domain": None, "ats": None,
                    "jobId": None, "jobTitle": None,
                    "status": "NOT_CHECKED_MAX_COMPANIES", "reason": reason, "fromCache": False,
                })

        not_checked = len(skipped) + len(over_max)
        processed = len(report) - not_checked
        partial_count = sum(1 for item in report if item.get("partial"))
        # A company the cap interrupted was read only in part: calling it resolved made
        # the summary say "all companies dealt with" while one of them was half-read.
        resolved = processed - failed_companies - partial_count
        await Actor.set_value("COMPANY_REPORT", report)
        await Actor.set_value("SUMMARY", {
            "companiesAsked": len(requested),
            "companiesProcessed": processed,
            "companiesResolved": resolved,
            "companiesFailed": failed_companies,
            "companiesWithNoJobs": sum(1 for item in report
                                       if item.get("status") not in NOT_CHECKED_STATUSES
                                       and item.get("status") != "DUPLICATE_BOARD"
                                       and not item.get("error") and not item.get("delivered")
                                       and not item.get("partial")),
            "companiesPartiallyProcessed": partial_count,
            "companiesDuplicateBoards": sum(1 for item in report if item.get("duplicateOf")),
            "jobsMatched": sum(int(item.get("jobsMatched") or 0) for item in report),
            "jobsOverMaxJobsPerCompany": sum(int(item.get("jobsOverMaxJobsPerCompany") or 0)
                                             for item in report),
            "jobsFromDuplicateAnswers": sum(int(item.get("jobsFromDuplicate") or 0)
                                            for item in report),
            "companiesNotChecked": not_checked,
            "companiesOverMaxCompanies": len(over_max),
            "companiesDeduplicated": deduplicated,
            "stopped": bool(stopped),
            "stopReason": stop_reason,
            "billingActive": billing_active,
            "jobsDelivered": delivered,
            "cacheMinutes": cache_minutes,
            "cacheHits": len(cached),
            "cacheMisses": len(to_pull),
            "seconds": round(time.time() - started, 1),
            "note": ("Full per-company detail is in the COMPANY_REPORT key of this run's storage. "
                     "Every distinct company appears there: processed, failed, not checked "
                     "(NOT_CHECKED_CAP, NOT_CHECKED_MAX_COMPANIES, NOT_CHECKED_BILLING), a duplicate "
                     "board (DUPLICATE_BOARD) or partially processed (partial: true, the run stopped "
                     "inside that company). companiesAsked counts distinct companies after collapsing "
                     "case-insensitive duplicates - companiesDeduplicated says how many input lines "
                     "that was - and it is never the number this run was allowed to touch. "
                     "jobsMatched counts each board once (the first answer for it), so jobsDelivered "
                     "can exceed it by jobsFromDuplicateAnswers - the jobs only a second input row "
                     "for the same board carried."),
        })
        Actor.log.info(f"Done: {delivered} jobs from {processed - failed_companies}/{len(requested)} companies, "
                       f"{not_checked} not checked, "
                       f"in {time.time() - started:.0f}s (cache hits: {len(cached)})")


if __name__ == "__main__":
    os.environ.setdefault("ACTOR_LOG_LEVEL", "INFO")
    asyncio.run(main())
