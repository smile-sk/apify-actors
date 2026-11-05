"""Apify Actor: every open job of any Workday-powered career site.

Input  : Workday career site URLs (or tenant:site pairs).
Output : one row per open job - title, location, remote flag, posted date,
         apply URL, tenant and site - and the full description on request.
Charge : `job-found`, only for rows actually delivered. A board with no
         matching job costs the buyer nothing, and a board we could not read is
         reported with its HTTP status, never dropped silently.

Why the caps exist: Workday rejects `limit` above 20, and a description costs
one extra request per job (~0.7 s measured). So the list is always paginated by
20 and descriptions are opt-in with their own cap - a 2000-job board is
100 listing requests, and asking for 2000 descriptions would be half an hour.
The Actor says both numbers in the log and in SUMMARY, so a truncated run is
never mistaken for an empty one.

Three anti-overcharge rules, each learned from a live billing complaint:

  * the same board pasted twice (with and without `/en-US/`, with a trailing
    slash) is read once and billed once - boards are deduped by what they point
    at, and jobs by (tenant, site, jobId) before any charge;
  * a country code inside a location is never read as a country: `CA` in
    "US, CA, Santa Clara" is California, not Canada;
  * a board that fails after some pages still delivers what it read, and says
    so in BOARD_REPORT, instead of throwing away collected rows;
  * the charge is issued BEFORE the row is pushed, and the row is pushed only
    when that charge actually counted. The SDK's ChargeResult says it plainly:
    `event_charge_limit_reached` means "this event was charged and no more will
    fit", so breaking before push_data sold a row the buyer never received
    (live run HTwGDQFtghVZKdABV: 16 charged, 15 delivered).

Cache: a named key-value store in the caller's own account. Repeating a run
inside `cacheMinutes` does not hammer the same board, and cached rows are
still delivered (and still charged) - they are real data, just faster.
"""
from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from apify import Actor

from src import workday

CHARGE_EVENT = "job-found"
CACHE_STORE = "workday-jobs-cache"
DEFAULT_CACHE_MINUTES = 60
MIN_ERROR_CACHE_SECONDS = 300
LIST_WORKERS = 6
DETAIL_WORKERS = 6


def _targets(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").split(",")
    out, seen = [], set()
    for item in raw or []:
        if isinstance(item, dict):
            item = item.get("url") or item.get("company") or item.get("tenant") or ""
        item = str(item).strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def _monetization_off(charge) -> bool:
    """True when this run is not pay-per-event at all (local dev, free Actor).

    The SDK answers such a charge with `charged_count=0` and an empty chargeable
    map; a pay-per-event run always knows at least the event it was asked about.
    Without this test a local run would deliver nothing at all.
    """
    return (not getattr(charge, "chargeable_within_limit", None)
            and not getattr(charge, "event_charge_limit_reached", False))


def _city_hopeless(job: dict, location: str) -> bool:
    """True when the row's own listing already names a place the buyer filtered out.

    Such a row can never be delivered, so a country probe spent on it is waste -
    the earlier order probed 97 rows of an "N Locations" board before the city
    filter ever saw them. A row that names no real place ("5 Locations", a bare
    "Remote") is NOT hopeless: the probe is the only way to learn where it is.
    """
    if not location:
        return False
    if not workday.location_is_informative(job):
        return False
    return not workday.matches(job, "", location)


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        targets = _targets(inp.get("companies"))
        if not targets:
            raise ValueError("companies is empty - pass at least one Workday career site URL")

        max_companies = int(inp.get("maxCompanies") or 200)
        max_jobs = int(inp.get("maxJobsPerCompany") or 200)
        keywords = str(inp.get("keywords") or "").strip()
        location = str(inp.get("location") or "").strip()
        countries = [str(c).strip() for c in (inp.get("countries") or []) if str(c).strip()]
        only_remote = bool(inp.get("onlyRemote", False))
        include_desc = bool(inp.get("includeDescription", False))
        max_desc = int(inp.get("maxDescriptionsPerCompany") or 40)
        max_probes = int(inp.get("maxCountryProbes", 400))
        include_empty = bool(inp.get("includeEmptyCompanies", False))
        cache_minutes = int(inp.get("cacheMinutes", DEFAULT_CACHE_MINUTES) or 0)
        cache_seconds = max(0, cache_minutes) * 60

        # the same board pasted twice must cost once: dedupe by tenant/wd/site
        prepared = workday.prepare_targets(targets)
        pairs = prepared["pairs"]
        dropped_duplicates = len(prepared["dropped"])
        if dropped_duplicates:
            Actor.log.info(f"{dropped_duplicates} duplicate board line(s) collapsed before reading")

        dropped_by_cap = max(0, len(pairs) - max_companies)
        if dropped_by_cap:
            Actor.log.warning(f"maxCompanies={max_companies} - {dropped_by_cap} board(s) NOT read "
                              f"this run; raise maxCompanies to read them")
        pairs = pairs[:max_companies]
        if not pairs:
            raise ValueError("no boards left after deduplication")

        cache = None
        if cache_seconds:
            try:
                cache = await Actor.open_key_value_store(name=CACHE_STORE)
            except Exception as exc:  # a local run without the platform must not crash
                Actor.log.warning(f"Cache store unavailable, running without cache: {exc}")

        started = time.time()
        now = started
        cached: dict[str, dict] = {}
        to_pull: list[tuple[str, dict]] = []

        for raw, parsed in pairs:
            if parsed.get("error"):
                to_pull.append((raw, parsed))
                continue
            entry = None
            if cache is not None:
                try:
                    entry = await cache.get_value(workday.cache_key(parsed, include_desc, max_jobs, keywords,
                                                            location, countries))
                except Exception as exc:
                    Actor.log.warning(f"Cache read failed for {raw}: {exc}")
            if isinstance(entry, dict) and isinstance(entry.get("result"), dict):
                result = entry["result"]
                ttl = max(MIN_ERROR_CACHE_SECONDS, cache_seconds // 2) if result.get("error") else cache_seconds
                if now - float(entry.get("fetchedAt") or 0) <= ttl:
                    cached[raw] = result
                    continue
            to_pull.append((raw, parsed))

        Actor.log.info(f"Reading {len(pairs)} Workday boards "
                       f"(keywords={keywords or 'any'}, location={location or 'any'}, "
                       f"countries={','.join(countries) or 'any'}, "
                       f"onlyRemote={only_remote}, descriptions={include_desc}, "
                       f"cache={cache_minutes}m: {len(cached)} fresh, {len(to_pull)} to fetch)")

        pulled: dict[str, dict] = {}
        if to_pull:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=min(LIST_WORKERS, len(to_pull))) as pool:
                fresh = await asyncio.gather(*[
                    loop.run_in_executor(pool, _pull_board, parsed, raw, max_jobs, keywords)
                    for raw, parsed in to_pull
                ])
            pulled = dict(zip([raw for raw, _ in to_pull], fresh))
            for raw, result in pulled.items():
                if result.get("searchFallback"):
                    Actor.log.warning(f"{raw}: Workday's own keyword search returned nothing - "
                                      f"read the full board instead")
            if cache is not None:
                for raw, result in pulled.items():
                    if result.get("error") in ("NOT_A_WORKDAY_URL", "NEEDS_CAREER_URL", "EMPTY_INPUT"):
                        continue  # a bad paste is not worth caching
                    parsed = workday.parse_target(raw)
                    try:
                        await cache.set_value(workday.cache_key(parsed, include_desc, max_jobs, keywords,
                                                        location, countries),
                                              {"fetchedAt": now, "result": result})
                    except Exception as exc:
                        Actor.log.warning(f"Cache write failed for {raw}: {exc}")

        report = []
        delivered = failed = no_jobs = partial_boards = charge_failures = 0
        boards_processed = probes_used = probes_resolved = 0
        rows_unknown_country = 0
        duplicate_jobs = 0
        descriptions_sent = 0
        rows_refused = 0
        rows_skipped_city = 0
        delivery_failures = 0
        delivery_failure_rows = []
        seen_jobs: set[tuple] = set()
        stopped = False
        stop_reason = ""

        for board_index, (raw, _parsed) in enumerate(pairs):
            result = cached.get(raw) or pulled.get(raw)
            if not result:
                continue
            entry = {
                "input": raw,
                "tenant": result.get("tenant"),
                "site": result.get("site"),
                "jobsOnBoard": result.get("total"),
                "delivered": 0,
                "descriptions": 0,
                "truncated": result.get("truncated", False),
                "fromCache": raw in cached,
                "error": result.get("error"),
                "partial": False,
                "countryProbes": 0,
            }

            jobs_raw = result.get("jobs") or []
            if result.get("error") and not jobs_raw:
                failed += 1
                Actor.log.warning(f"{raw}: {result['error']} {result.get('detail') or result.get('hint') or ''}")
                report.append(entry)  # counted in boardsFailed, not in boardsRead
                if include_empty:
                    await Actor.push_data({
                        "company": result.get("tenant") or raw,
                        "tenant": result.get("tenant"), "site": result.get("site"),
                        "jobId": None, "jobTitle": None,
                        "status": result["error"],
                        "reason": result.get("hint") or result.get("detail"),
                        "fromCache": entry["fromCache"],
                    })
                continue
            if result.get("error"):
                # some pages were read before the failure - deliver them, do not bin them
                partial_boards += 1
                entry["partial"] = True
                Actor.log.warning(f"{raw}: {result['error']} after {len(jobs_raw)} jobs - "
                                  f"delivering what was read")

            jobs = [workday.normalize(p, result["tenant"], result["wd"], result["site"], None)
                    for p in jobs_raw]
            # keywords first: local and free, so no request is spent on a row the
            # buyer filtered out by name anyway
            matched = [j for j in jobs if workday.matches(j, keywords, None)]
            if only_remote:
                # remote is stated by the listing itself - drop before spending a probe
                matched = [j for j in matched if j.get("remote")]

            details_done: dict[str, dict] = {}

            def detail_for(job: dict) -> dict:
                """One detail request per job, reused for the probe and the description."""
                path = str(job.get("externalPath") or "")
                if path not in details_done:
                    details_done[path] = workday.job_detail(
                        result["tenant"], result["wd"], result["site"], path)
                return details_done[path]

            unproven_here = 0
            if countries or location:
                # A row that names no country ("5 Locations", "Berlin") is resolved
                # from its own detail record BEFORE any filter decides - the city
                # filter needs the resolved location too, otherwise 97 of 200 rows
                # on a "5 Locations" board can never match a city.
                candidates = [j for j in matched if workday.needs_country_probe(j)]
                unknown = [j for j in candidates if not _city_hopeless(j, location)]
                # a row whose own text already names another city is not probed:
                # the probe could not change its fate, it would only cost money
                rows_skipped_city += len(candidates) - len(unknown)
                budget_left = max(0, max_probes - probes_used)
                probed = unknown[:budget_left] if budget_left > 0 else []
                if probed:
                    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
                        found = list(pool.map(detail_for, probed))
                    for job, detail in zip(probed, found):
                        probes_used += 1
                        entry["countryProbes"] += 1
                        if not detail.get("error"):
                            workday.apply_detail(job, detail, with_description=False)
                            if workday.implied_countries(job):
                                probes_resolved += 1
                # Counted BEFORE the filter and from the rows themselves: a row
                # dropped for a country we could not prove is a LOST row, and the
                # summary must say so. Counting after the filter always gave 0,
                # because the filter had just thrown exactly those rows away.
                unproven = [j for j in unknown if workday.needs_country_probe(j)]
                unproven_here = len(unproven)
                if unproven_here:
                    rows_unknown_country += unproven_here
                    why = (f"maxCountryProbes={max_probes} is spent ({probes_used} of "
                           f"{max_probes} used this run) - raise it to resolve them"
                           if len(probed) < len(unknown)
                           else "their own detail record names no country either")
                    if location:
                        # do not promise that a bigger budget buys these rows: they
                        # must also match the city filter once their place is known
                        why += (f"; they name no specific place, so they also have to "
                                f"match location='{location}'")
                    Actor.log.warning(f"{raw}: {unproven_here} row(s) left unproven ({why}) - "
                                      f"NOT delivered, not charged")
                if countries:
                    matched = [j for j in matched if workday.country_matches(j, countries)]
            if location:
                matched = [j for j in matched if workday.matches(j, "", location)]

            # descriptions belong to the jobs the buyer will actually see
            if include_desc and matched and max_desc > 0:
                wanted = matched[:max_desc]
                with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
                    details = list(pool.map(detail_for, wanted))
                for job, detail in zip(wanted, details):
                    if not detail.get("error"):
                        workday.apply_detail(job, detail)
                        entry["descriptions"] += 1

            for job in matched:
                key = workday.job_key(job)
                if key in seen_jobs:
                    duplicate_jobs += 1
                    continue
                seen_jobs.add(key)
                row = dict(job)
                row.update({
                    "company": result.get("display") or result.get("tenant"),
                    "status": "FOUND",
                    "fromCache": entry["fromCache"],
                    "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
                if row.get("jobDescription"):
                    descriptions_sent += 1
                # Charge first, deliver on a charge that actually counted. Twice,
                # under the same idempotency key - the SDK drops a repeat inside
                # three minutes, so a retry can never bill the same row twice.
                charge = None
                charged = 0
                charge_raised = False
                idem = f"{result.get('tenant')}:{job.get('jobId') or job.get('externalPath')}"
                for attempt in (1, 2):
                    try:
                        charge = await Actor.charge(event_name=CHARGE_EVENT,
                                                    idempotency_key=idem)
                        charged = int(getattr(charge, "charged_count", 0) or 0)
                        break
                    except Exception as exc:
                        charge_raised = True
                        if attempt == 2:
                            charge_failures += 1
                            Actor.log.error(f"Charge failed twice for job {job.get('jobId')}: "
                                            f"{exc} - we cannot tell whether the platform billed "
                                            f"it, so the row IS delivered and the run stops")
                            charge = None
                uncertain = False
                if charge is None:
                    # Both attempts raised: the row may or may not have been billed and
                    # no answer can settle it. Handing out this one row costs at most one
                    # job per run and never leaves the buyer paying for nothing.
                    uncertain = True
                elif charged <= 0 and not _monetization_off(charge):
                    if charge_raised:
                        # The first attempt raised AFTER the platform may already have
                        # billed it. The retry carries the same idempotency key, so the
                        # SDK drops it and answers 0 - which is indistinguishable from a
                        # spent budget. Handing out one row in doubt is cheaper than
                        # billing a row the buyer never receives, and the run gets its
                        # own reason instead of a false "charge-limit".
                        uncertain = True
                        Actor.log.warning("Charge raised on the first attempt and the idempotent "
                                          "retry answered 0 - the row may already be billed, so it "
                                          "IS delivered and the run stops here")
                    else:
                        # nothing was charged: the buyer's budget is spent, so this row
                        # would be free data - it is neither billed nor delivered
                        rows_refused += 1
                        stop_reason = "charge-limit"
                        stopped = True
                        Actor.log.warning("User spending limit reached - no event was charged, "
                                          "so the row is NOT delivered and NOT billed")
                        break
                pushed = False
                for push_attempt in (1, 2):
                    try:
                        await Actor.push_data(row)
                        pushed = True
                        break
                    except Exception as exc:  # charged, but the dataset refused the row
                        if push_attempt == 2:
                            delivery_failures += 1
                            delivery_failure_rows.append({
                                "jobId": job.get("jobId") or job.get("externalPath"),
                                "tenant": result.get("tenant"),
                                "site": result.get("site"),
                                "jobTitle": job.get("jobTitle"),
                                "url": row.get("url"),
                                "error": str(exc)[:300],
                            })
                            Actor.log.error(f"Row {job.get('jobId')} was charged but the dataset "
                                            f"refused it twice: {exc}")
                if not pushed:
                    # the row is billed: stop before more charges land in a dataset
                    # that is refusing writes
                    stop_reason = "delivery-error"
                    stopped = True
                    break
                delivered += 1
                entry["delivered"] += 1
                if uncertain:
                    stop_reason = "charge-uncertain"
                    stopped = True
                    break
                if getattr(charge, "event_charge_limit_reached", False):
                    # THIS row was charged and THIS row was delivered - the next one
                    # would not fit, so stop here rather than sell air
                    stop_reason = "charge-limit"
                    stopped = True
                    Actor.log.warning("User spending limit reached - the last row was charged "
                                      "and delivered, stopping here")
                    break

            if include_empty and not entry["delivered"] and not stopped:
                no_jobs += 1
                reason = ("the board answered but has no open jobs" if not result.get("total")
                          else "the board has open jobs, none matched your filters")
                if unproven_here:
                    reason += (f". {unproven_here} row(s) named no country and were not "
                               f"resolved (see SUMMARY.rowsWithoutCountry) - raise "
                               f"maxCountryProbes to deliver them")
                elif result.get("total") and (countries or keywords or location or only_remote):
                    reason += (". Locations that named no country were resolved from their own "
                               "detail record; those that still name none are not delivered")
                await Actor.push_data({
                    "company": result.get("display") or result.get("tenant"),
                    "tenant": result.get("tenant"), "site": result.get("site"),
                    "jobId": None, "jobTitle": None,
                    "status": "NO_JOBS", "jobsOnBoard": result.get("total"),
                    "reason": reason, "fromCache": entry["fromCache"],
                })
            boards_processed += 1
            report.append(entry)
            Actor.log.info(f"{raw}: {entry['delivered']} of {result.get('total')} jobs delivered"
                           f"{' (truncated)' if entry['truncated'] else ''}"
                           f"{' (partial)' if entry['partial'] else ''}"
                           f"{' (cached)' if entry['fromCache'] else ''}")
            if stopped:
                break

        # A board we never reached because the run stopped is reported, not hidden:
        # the loop used to `break` and the tail of the list simply disappeared, so
        # a run cut at board 2 of 10 looked like a run over 2 boards.
        boards_not_read = 0
        if stopped and board_index + 1 < len(pairs):
            for raw, _p in pairs[board_index + 1:]:
                boards_not_read += 1
                report.append({
                    "input": raw,
                    "tenant": _p.get("tenant"),
                    "site": _p.get("site"),
                    "jobsOnBoard": None,
                    "delivered": 0,
                    "descriptions": 0,
                    "truncated": False,
                    "fromCache": False,
                    "partial": False,
                    "countryProbes": 0,
                    "error": "NOT_READ",
                    "reason": (f"the run stopped before this board was read "
                               f"(stopReason={stop_reason or 'charge-limit'})"),
                })

        if delivery_failures:
            await Actor.set_value("DELIVERY_FAILURES", {
                "count": delivery_failures,
                "rows": delivery_failure_rows,
                "note": ("The row below was charged or possibly charged, then refused by the dataset "
                         "twice. The run stops on the first such row, so no further charges are made after it; "
                         "keep this record as the trail for a refund claim."),
            })
        await Actor.set_value("BOARD_REPORT", report)
        await Actor.set_value("SUMMARY", {
            "boardsAsked": len(targets),
            "boardsDeduplicated": dropped_duplicates,
            "boardsSkippedByMaxCompanies": dropped_by_cap,
            "boardsRead": boards_processed,
            "boardsFailed": failed,
            "boardsPartial": partial_boards,
            "boardsWithoutMatches": no_jobs,
            "jobsDelivered": delivered,
            "jobsDuplicateSkipped": duplicate_jobs,
            "descriptionsFetched": descriptions_sent,
            "chargeFailures": charge_failures,
            "deliveryFailures": delivery_failures,
            "stoppedByChargeLimit": stopped and (stop_reason or "charge-limit") == "charge-limit",
            "rowsRefusedByChargeLimit": rows_refused,
            "boardsNotReadAfterStop": boards_not_read,
            "stopReason": stop_reason or None,
            "countryProbes": probes_used,
            "countryProbesResolved": probes_resolved,
            "rowsWithoutCountry": rows_unknown_country,
            "rowsNotProbedCityMismatch": rows_skipped_city,
            "cacheMinutes": cache_minutes,
            "cacheHits": len(cached),
            "cacheMisses": len(to_pull),
            "seconds": round(time.time() - started, 1),
            "note": ("Per-board totals are in the BOARD_REPORT key of this run's storage. "
                     "Only rows marked FOUND were charged."),
        })
        Actor.log.info(f"Done: {delivered} jobs from {boards_processed}/{len(pairs)} boards "
                       f"in {time.time() - started:.0f}s (cache hits: {len(cached)}, "
                       f"duplicates skipped: {duplicate_jobs})")


def _pull_board(parsed: dict, raw: str, max_jobs: int, keywords: str = "") -> dict:
    """One board: list the postings. Details are fetched later, per delivered job."""
    if parsed.get("error"):
        return {"error": parsed["error"], "hint": parsed.get("hint"),
                "tenant": parsed.get("tenant"), "site": parsed.get("site")}
    listed = workday.list_jobs(parsed["tenant"], parsed["wd"], parsed["site"],
                               max_jobs=max_jobs, search_text=keywords)
    fallback = False
    if not listed.get("error") and not listed.get("jobs") and keywords:
        # Workday's server-side search is not portable across tenants: an empty
        # answer for a board we know exists is retried as a plain listing.
        listed = workday.list_jobs(parsed["tenant"], parsed["wd"], parsed["site"],
                                   max_jobs=max_jobs)
        fallback = True
    return {
        "tenant": parsed["tenant"], "wd": parsed["wd"], "site": parsed["site"],
        "display": parsed["tenant"], "total": listed.get("total"),
        "jobs": listed.get("jobs") or [], "truncated": listed.get("truncated", False),
        "error": listed.get("error"), "detail": listed.get("detail"),
        "partial": listed.get("partial", False),
        "searchFallback": fallback,
    }


if __name__ == "__main__":
    os.environ.setdefault("ACTOR_LOG_LEVEL", "INFO")
    asyncio.run(main())
