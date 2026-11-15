# SmartRecruiters Jobs API

Every open posting of a SmartRecruiters board, as JSON: title, company, department, function, industry, experience level, employment type, location with coordinates, remote and hybrid flags, reference number, release date and URL. Ask for descriptions and you also get the job description, qualifications, additional information and the company blurb.

Boards are the part of the job market that still answers a plain HTTP request. Indeed, LinkedIn and Glassdoor block data-centre IPs; `api.smartrecruiters.com` does not, carries no key and no login, and one large board holds thousands of open roles (SGS answered with 4 632 on 7 November 2026).

## Why this Actor and not another job scraper

Three things were measured against the live API on 2026-11-07, and each one is a place where a cheaper job scraper quietly loses your data or your money:

1. **The whole board, not the first 500.** The list is paged 100 rows at a time and `offset` keeps working far past 500 (offset 4 500 answered 100 rows). Most Actors stop at 500 and call the board finished. This one walks until the board ends, up to a guard of 8 000 postings, and the summary says how many postings the board had and how many were read.
2. **The filters the API applies are applied, the ones it ignores are done in code.** `q`, `country` and `releasedAfter` really narrow the board on the server, so a daily run downloads a fraction of it. `function`, `typeOfEmployment`, `experienceLevel` and `department` are silently ignored by the API (department even answers HTTP 400): sending them would deliver exactly what you excluded and charge you for it, so they are matched in code instead and every dropped posting is counted by reason.
3. **A board nobody owns is not an empty board.** Both answer HTTP 200 with `totalFound=0`. The public page separates them: a real board renders `Careers at <name>`, an unknown identifier renders the generic `SmartRecruiters Job Search`. You get a different status row for each, so a typo never reads as "this company hires nobody".

## Input

| Field | What it does |
|---|---|
| `companies` | Board identifiers (`SGS`, `Ubisoft2`) or careers URLs (`https://jobs.smartrecruiters.com/SGS`). The identifier is the segment right after the domain. It is **not** always the company name: `ubisoft` has no board, `Ubisoft2` has 298 postings. |
| `query` | Sent to the server as `q=`. On SGS, `designer` narrowed 4 632 postings to 128 before anything was downloaded (214 a few hours later: the board itself moves, so this is a measurement, not a promise). |
| `country` | Lowercase ISO-2 (`us`, `de`, `pe`), applied by the server. Anything else is dropped and the summary says `countryIgnored`. |
| `releasedAfter` | ISO date (`2026-11-01`), applied by the server. The cheapest way to follow a board daily. |
| `keywords`, `excludeKeywords` | Keywords for the title, matched in code. Comma separates alternatives, a space inside keeps one phrase: `game designer` matches that role and not `game artist`. |
| `location` | City, region or country word matched as a whole word, so `us` does not match Australia. |
| `remoteOnly`, `employmentType`, `experienceLevel`, `department` | Filtered in code, because the API ignores its own parameters for them. |
| `maxJobsPerCompany` | Hard stop per board, default 500, `0` means the whole board. The list is cut to the limit even though the API always answers a page of 100. |
| `includeDetails` | One extra request per delivered posting, about 0.2 s, for the description, qualifications and benefits. |
| `maxDetails` | Bound on description requests in one run, default 300, so a 4 000 posting board cannot run for an hour. |
| `requestPaceSecs` | Pause between list requests, default 0.3. Descriptions use at most 0.1 s. |

## Money

Pay per event, one charge per delivered posting (`job-delivered`). Everything else is free:

* a board nobody owns (`NOT_CHECKED_BOARD_NOT_FOUND`),
* a board with no open postings (`NOT_CHECKED_BOARD_EMPTY`),
* a board that could not be read (`NOT_CHECKED_ERROR`),
* a string that is not an identifier (`INVALID_INPUT`),
* every posting your filters dropped,
* every posting past your spending cap, which is not written at all.

A row is written and charged in one SDK call, so a run cannot charge for a row the dataset never received.

**The platform cap.** Apify puts a default spending cap on a run when the buyer sets none, whatever we price: about $4.4 on our runs (the exact number varies a little per run and is shown in the run's `options.maxTotalChargeUsd`; on this actor we measured 4.41). At $0.0015 per posting that is roughly 2 900 postings, so a 4 632 posting board like SGS stops at the cap with `stopReason: spending-cap`; the rest is counted in `jobsNotProcessed` and `companiesNotChecked`, never delivered free and never charged. Raise the cap to read the whole board.

## Output

One row per posting. `status` is `DELIVERED` for a posting; every other status is a row that explains a board instead of pretending it was empty. The run summary is stored in the named key-value store `smartrecruiters-jobs-api-summary` as `RUN_SUMMARY` and as `RUN_SUMMARY-<runId>`, so read the second one when you need the summary of *this* run: the named store is shared by the whole account.

The counters add up and a mismatch is printed, not rounded away:

```
jobsSeen  = jobsDelivered + filteredOut + jobsUndelivered + jobsUnverified + jobsNotProcessed
jobsAsked = jobsSeen + jobsNotRead
companiesAsked = companiesOk + companiesNotFound + companiesEmpty
               + companiesError + companiesInvalid + companiesNotChecked
truncatedCompanies = number of boards flagged truncated in byCompany
```

## What this Actor does not do

* It does not touch LinkedIn, Indeed or any site that blocks data centres. It reads SmartRecruiters boards only.
* It does not invent a salary. `compensation` is `null` on most boards, and then `salaryMin`, `salaryMax`, `salaryCurrency` and `salaryPeriod` are all `null` rather than guessed.
* It does not read the board behind an employers' private API or an unlisted posting.
* It does not promise a description on every row: when the description request fails, the posting is still delivered with its list data and `detailError` names the reason.
* It does not follow a board that moved to another ATS. If a company left SmartRecruiters, you get `NOT_CHECKED_BOARD_NOT_FOUND` and nothing is charged.

## Measured numbers

| Fact | Value |
|---|---|
| Page size the API really gives | 100 rows, whatever `limit` you ask for |
| List request | about 0.3 s |
| Description request | about 0.2 s, about 5 KB |
| SGS board | 4 632 open postings |
| Ubisoft board (`Ubisoft2`) | 298 open postings |
| Server-side search | `q=designer` on SGS: 128 at 09:00, 214 at 15:00 the same day |
| Country filter | `country=pe` on SGS: 611 instead of 4 632 |

`byStatus` counts the rows of each status, and `duplicatesSkipped` counts a posting the board answered twice, which is delivered and charged once. Local checks: `python tests/test_sc.py` and `python tests/test_main.py` (no network).
