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

1. **The careers page.** `example.com/careers` and friends are read, and any ATS link on them is followed (`boards.greenhouse.io/acme`, `jobs.ashbyhq.com/acme`, …).
2. **The domain itself** as a slug against all six APIs.
3. If nothing answers, the company is marked `NO_ATS_FOUND` with a hint - pass the board slug (`leverdemo`) or the full careers URL instead of guessing.

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

Pay per job delivered. A run that finds nothing costs nothing. Extraction of the description costs the same as the row itself.

## Cost of a run

The actor only makes HTTP calls to public JSON endpoints: a 50-company run typically takes well under a minute on the smallest memory setting. With the cache on, a repeat check of an unchanged list makes almost no requests at all.

## Notes

- Only boards the company published publicly are read; nothing is logged in to.
- Salary is returned when the ATS exposes it (Ashby and Lever usually do; Greenhouse usually does not).
- Workday, SuccessFactors and Taleo boards are not covered yet - tell us if your list needs them.

## Cache and billing

A row served from the cache is charged like any other row: you pay for delivered jobs, not for
network calls. What the cache buys you is time - a repeated company resolves in seconds instead of
probing six ATS APIs again.
