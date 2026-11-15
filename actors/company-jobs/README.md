# Company Jobs API: every open role behind any domain

Paste company domains. Get every open job, in one schema, with salary when the company publishes it.

**No proxies. No cookies. No login.** The boards this actor reads are the companies' own applicant systems, not job aggregators - so a run answers in seconds and does not fall over when Indeed changes its markup for the third time this month.

## What most job scrapers do, and why this is different

| The usual offer | Here |
|---|---|
| Scrapes one site; LinkedIn or Indeed blocks it and the run returns nothing | Reads six ATS APIs directly - Greenhouse, Ashby, Lever, Workable, SmartRecruiters, Recruitee - and detects which one a company uses from its domain |
| Silent failure: no rows, no reason | Every company is reported: the ATS it uses, jobs on the board, jobs delivered, or the exact reason it could not be resolved (`COMPANY_REPORT` in run storage) |
| You pay per run, even when it returns an empty list | You pay per job delivered. A company with no open roles costs nothing |
| Descriptions, salary and remote flag arrive inconsistently | One flat schema for all six sources; salary parsed to min/max/currency/period |
| Needs residential proxies, billed to you | Zero proxy traffic - the sources are public JSON |
| Same list checked hourly = every board hit hourly, forever | Answers are cached in your own storage for 60 minutes (`cacheMinutes`): a repeat check inside that window is instant and sends no duplicate request |

## How it finds the board

A domain almost never matches the board slug, so detection tries, in order:

1. **The careers page.** `example.com/careers` and friends are read, and any ATS link on them is followed (`boards.greenhouse.io/acme`, `jobs.ashbyhq.com/acme`, and so on).
2. **The domain itself** as a slug against all six APIs.
3. If nothing answers, the company is marked `NO_ATS_FOUND` with a hint - pass the board slug (`leverdemo`) or the full careers URL instead of guessing.
   If the careers page names a provider this actor cannot read (personio today), the row says `UNSUPPORTED_ATS` and names it, so a found board is never reported as missing.

You can also pass a board slug or a careers URL directly; both are understood.

## Input

```json
{
  "companies": ["stripe.com", "figma.com", "notion.so", "canva.com"],
  "keywords": "engineer, data",
  "location": "remote",
  "onlyRemote": false,
  "includeDescription": false,
  "maxJobsPerCompany": 0,
  "maxCompanies": 50,
  "cacheMinutes": 60
}
```

- `keywords` filters on the job title, case-insensitive, comma separated. Filtered-out jobs are not charged.
- `location` is a substring match on the job location.
- `includeDescription` adds the full description as plain text; the price per job does not change.
- `cacheMinutes` reuses a result from your own key-value store within that window. `0` = always live. A company we could not resolve is cached for half the window, so a board that was briefly down recovers on the next run.

## Output

One row per job:

```json
{
  "company": "Stripe",
  "domain": "stripe.com",
  "ats": "greenhouse",
  "jobId": "4567890",
  "jobTitle": "Software Engineer, Payments",
  "department": "Engineering",
  "location": "London, UK",
  "remote": false,
  "employmentType": null,
  "salaryMin": null,
  "salaryMax": null,
  "salaryCurrency": null,
  "salaryPeriod": null,
  "postedAt": "2026-10-28T09:12:41-04:00",
  "url": "https://stripe.com/jobs/search?gh_jid=4567890",
  "status": "FOUND",
  "fromCache": false,
  "fetchedAt": "2026-11-04T15:30:00+00:00"
}
```

`fromCache: true` means the row was served from your cache - same data, no extra request to the board. The run summary also reports `cacheHits` and `cacheMisses`, so you can see exactly what was reused.

## Who uses this

- **Recruiters and sourcing teams** - a target-company list becomes a live vacancy list.
- **B2B sales** - a company that is hiring three sales reps has budget; that is the strongest intent signal there is.
- **Job boards and aggregators** - fill categories from company boards instead of scraping aggregators.
- **Market research** - salary ranges and remote share by company, straight from the source.

## Price

**$0.0015 per delivered job** ($1.50 per 1,000). A run that finds nothing costs nothing. Extraction of the description costs the same as the row itself.

The platform puts a default spending cap of about **$4.4** on a run when the buyer does not set one, which is roughly 2,900 delivered jobs: a list of 50 companies on large boards reaches it. That cap is the buyer's setting, not ours - raise `maxTotalChargeUsd` in the run options when the whole list must be covered. When it is hit, the run stops and every company it never reached comes back as `NOT_CHECKED_CAP` with the reason.

## Cost of a run

The actor only makes HTTP calls to public JSON endpoints: a 50-company run typically takes well under a minute on the smallest memory setting. With the cache on, a repeat check of an unchanged list makes almost no requests at all.

## Notes

- Only boards the company published publicly are read; nothing is logged in to.
- Salary is returned when the ATS exposes it (Ashby and Lever usually do; Greenhouse usually does not).
- Workday, SuccessFactors and Taleo boards are not covered yet - tell us if your list needs them.
- `maxJobsPerCompany` is a per-company safety stop. When it keeps jobs back, the summary reports
  `jobsOverMaxJobsPerCompany`, so a capped board is never presented as a board that small.
- Two input rows can resolve to the same board (`stripe.com` and `https://stripe.com/careers`): the second
  answer is not thrown away. It goes through the same job-id gate, so a job already delivered is skipped and a
  job only the second answer carried is delivered and charged once. When it brings nothing new it comes back as
  `DUPLICATE_BOARD` with the name of the row that carries the jobs, and nothing is charged twice. Those
  extra jobs are counted as `jobsFromDuplicateAnswers`, because `jobsMatched` counts a board once.
- A company whose jobs the spending cap interrupted mid-way is marked `partial: true` in `COMPANY_REPORT`
  with the counts it did deliver - half a board is never reported as a whole one.
- `maxCompanies` defaults to 50: a longer list is cut at the input, and the companies beyond the limit come
  back as `NOT_CHECKED_MAX_COMPANIES` records with the reason (rows in the dataset when `includeEmptyCompanies`
  is true). `companiesAsked` counts distinct companies and `companiesOverMaxCompanies` counts what the cut took,
  so a 100-company list never reads as a 50-company answer. Case-insensitive repeats of one input
  (`a.com`, `A.COM`) collapse into one company and `companiesDeduplicated` says how many lines that was.
- If the run stops at your spending limit, the companies it never reached are still in `COMPANY_REPORT`
  with `status: NOT_CHECKED_CAP` and a reason, and the summary counts them as `companiesNotChecked`.
  A company that was not checked is never presented as a company with no open jobs.
- If a charge call fails twice in a row, the run stops instead of handing out the rest of the list for free:
  the summary says `billingActive: false` with `stopReason: billing-error`, and the companies it never reached
  come back as `NOT_CHECKED_BILLING`. The row that was already delivered stays delivered and stays
  readable, but it is not paid for: rows handed out while billing was failing are free to the buyer.
- If the platform reports that this run is not pay-per-event (for example the price is not set up
  yet), nothing is charged at all: the rows are still delivered, and the summary says
  `billingActive: false` with `stopReason: billing-not-active`. A buyer is never told he paid
  for rows that came out free.

## Cache and billing

A row served from the cache is charged like any other row: you pay for delivered jobs, not for
network calls. What the cache buys you is time - a repeated company resolves in seconds instead of
probing six ATS APIs again.
