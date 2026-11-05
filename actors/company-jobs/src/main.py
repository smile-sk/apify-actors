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


def _targets(raw) -> list[str]:
    if isinstance(raw, str):
        raw = [part for part in raw.replace("\n", ",").split(",")]
    out, seen = [], set()
    for item in raw or []:
        if isinstance(item, dict):
            item = item.get("domain") or item.get("url") or item.get("company") or ""
        item = str(item).strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def cache_key(company: str, detail: bool) -> str:
    """Same company + same detail level = same cache entry."""
    raw = f"{company.strip().lower()}|{'d' if detail else 'l'}".encode("utf-8")
    return CACHE_KEY_PREFIX + hashlib.sha1(raw).hexdigest()[:16]


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        targets = _targets(inp.get("companies"))
        if not targets:
            raise ValueError("companies is empty - pass at least one domain, careers URL or board slug")

        max_companies = int(inp.get("maxCompanies") or 50)
        targets = targets[:max_companies]
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

        Actor.log.info(f"Resolving {len(targets)} companies "
                       f"(keywords={keywords or 'any'}, location={location or 'any'}, "
                       f"onlyRemote={only_remote}, descriptions={include_desc}, "
                       f"cache={cache_minutes}m: {len(cached)} fresh, {len(to_pull)} to fetch)")

        pulled: dict[str, dict] = {}
        if to_pull:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=min(WORKERS, len(to_pull))) as pool:
                fresh = await asyncio.gather(*[
                    loop.run_in_executor(pool, ats.pull, company, include_desc) for company in to_pull
                ])
            pulled = dict(zip(to_pull, fresh))
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
        delivered = failed_companies = 0
        stopped = False

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

            matched = ats.filter_jobs(result.get("jobs") or [], keywords, location, only_remote, per_company)
            for job in matched:
                key = f"{result.get('ats')}:{result.get('slug')}:{job.get('jobId')}"
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

                try:
                    charge = await Actor.charge(event_name=CHARGE_EVENT)
                    if getattr(charge, "event_charge_limit_reached", False):
                        Actor.log.warning("User spending limit reached - stopping")
                        stopped = True
                except Exception as exc:  # local run without monetization must not crash
                    Actor.log.warning(f"Charging skipped: {exc}")
                if stopped:
                    break

            if include_empty and entry["delivered"] == 0:
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
            report.append(entry)
            Actor.log.info(f"{company}: {result.get('ats')} / {result.get('slug')} - "
                           f"{len(matched)} of {result.get('count')} jobs delivered"
                           f"{' (cached)' if entry['fromCache'] else ''}")
            if stopped:
                break

        await Actor.set_value("COMPANY_REPORT", report)
        await Actor.set_value("SUMMARY", {
            "companiesAsked": len(targets),
            "companiesResolved": len(targets) - failed_companies,
            "companiesFailed": failed_companies,
            "companiesWithNoJobs": sum(1 for item in report
                                       if not item.get("error") and not item.get("delivered")),
            "jobsDelivered": delivered,
            "cacheMinutes": cache_minutes,
            "cacheHits": len(cached),
            "cacheMisses": len(to_pull),
            "seconds": round(time.time() - started, 1),
            "note": "Full per-company detail is in the COMPANY_REPORT key of this run's storage.",
        })
        Actor.log.info(f"Done: {delivered} jobs from {len(targets) - failed_companies}/{len(targets)} companies "
                       f"in {time.time() - started:.0f}s (cache hits: {len(cached)})")


if __name__ == "__main__":
    os.environ.setdefault("ACTOR_LOG_LEVEL", "INFO")
    asyncio.run(main())
