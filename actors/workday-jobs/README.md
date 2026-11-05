# Workday Jobs API: every opening from any Workday career site

Paste a Workday career site URL and get every open job behind it: title, location,
country, remote flag, posting age, apply link, requisition id - and the full
description when you ask for it.

```json
{
  "companies": ["https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"],
  "keywords": "engineer",
  "countries": ["US"]
}
```

## The pain this Actor is built around

In this category the recurring complaint from buyers is not "give me more jobs" -
it is **"I cannot cut the result down to the country I actually work in"**. One
buyer put it plainly on a competing ATS jobs API: *"Need to be able to eliminate
or just segment the country you desire to receive leads."* The answer he got was
a question back, and the filter never appeared.

Here the country is a first-class field:

* `countries` is the answer to exactly that complaint: pass `US`, `Germany`, `Brazil`
  and the Actor reads the country from **every shape Workday writes it** -
  `US, CA, Santa Clara` (ISO code first), `Plano, TX` and `San Francisco, California`
  (US state), `Toronto, ON` (Canadian province), `Sao Paulo, Brazil` (country name),
  `United Kingdom - London` (country, dash, city);
* a two-letter tail that is **both** a state code and a country code is never
  guessed from a table: `CA` reads as California or Canada, `IN` as Indiana or
  India, `SK` as Saskatchewan or Slovakia. Those rows are looked up in their own
  detail record, which states the country, and are dropped - not charged - if it
  cannot be read. State codes that name one place only (`TX`, `ON`) decide on
  their own. This is not a detail: one buyer was charged for California jobs on a
  `Canada` filter and for Indiana jobs on a `US` filter, and the same bug
  returned nothing at all for `Plano, TX`;
* a row that names **no** country (`5 Locations`, a bare city) is looked up in its
  own detail record before the filter decides. `maxCountryProbes` (400 by default)
  is the **whole run's** budget across all boards, not a per-board allowance; when
  it is spent or the lookup fails, the remaining unproven rows are counted in
  `SUMMARY.rowsWithoutCountry`, named in the run log, and are never delivered or
  charged - an unproven row is not a guess, and a lost row is never a quiet zero.
  Only rows your filters could actually have kept are counted: a row that already
  names a different city is not "lost", it is filtered out by your own request.
  Such a row is not even probed, because a probe could not change its fate, and
  `SUMMARY.rowsNotProbedCityMismatch` tells you how many requests that saved. A
  row written "4 Locations" names no place at all, so it is still counted - the
  log says plainly that a bigger budget must also satisfy your `location` filter;
* `location` stays a free-text filter on the location **before** you are charged.
  A row whose location is only `5 Locations` or a bare city is resolved from its
  own detail record first, so the city filter sees the real place - before that
  fix, 97 of 200 rows on such a board could never match a city;
* `country` is a **code** (`US`, `CA`, `GB`, `DE`), not the board's wording: the
  same country used to arrive as `United States of America` on one row and
  `united states` on the next, which makes the column useless to join on. It is
  read from the job's own detail record when one was fetched, and from the
  location string when that string names the country. A country we do not
  recognise is passed through unchanged, never blanked.
* `onlyRemote` keeps the jobs the employer itself marks remote;
* and a job the filters drop costs you nothing - the charge is per delivered job.

## What Workday itself makes hard (measured, 04.11.2026)

| Fact | Number |
|---|---|
| Postings per request | **20** - `limit: 30` is answered with HTTP 400 |
| Capital One, 2 038 jobs | `Canada` filter: 4 Canadian rows, **0 California** (was 3 California, charged) |
| Capital One, 2 038 jobs | `US` filter: **189 rows**, `country` filled on every one (was 0 - a silent zero) |
| Salesforce, 1 556 jobs | `United Kingdom` filter: 2 rows from `United Kingdom - London` (was 0) |
| One job description | one extra request, **~0.7 s** |
| A 2 000-job board | 100 listing requests |
| Availability | 4 of 4 tenants answered 200 on the first try, no proxy, no cookies |

So the list is always paginated by 20, and descriptions are opt-in with a cap.
If a run hits your cap, the board's `BOARD_REPORT` entry carries `truncated: true`
and the board's real total - a cut run never looks like an empty board.

The same rule holds when **your spending limit** stops the run: the row that filled
the limit is charged **and delivered**, the next one is neither, and every board
this run never reached is written into `BOARD_REPORT` as `NOT_READ` with the
reason. `SUMMARY.stopReason` says `charge-limit`; a stop is never a silent tail of
the list. The two other reasons, `charge-uncertain` and `delivery-error`, are
explained under Pricing.

## Output

One row per open job:

```json
{
  "company": "nvidia",
  "tenant": "nvidia",
  "site": "NVIDIAExternalCareerSite",
  "jobId": "JR2022712",
  "jobTitle": "Senior Solutions Architect, Networking and GPU System",
  "location": "China, Remote",
  "country": "CN",
  "postedOn": "Posted Today",
  "postedDaysAgo": 0,
  "startDate": "2026-11-04",
  "timeType": "Full time",
  "remote": true,
  "url": "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/...",
  "status": "FOUND"
}
```

`status` is never empty: `FOUND` for a real job, `NO_JOBS` for a board that
answered with nothing matching, `NEEDS_CAREER_URL` / `NOT_A_WORKDAY_URL` /
`HTTP_403` for a board we could not read - with the reason attached. A board that
gave nothing is free.

## Pricing

**Pay per delivered job.** Empty boards, failed boards and rows your filters
dropped are not charged. A row your spending cap refuses is neither charged nor
delivered, and a charge that fails twice stops the run instead of handing rows
out for free. Rows served from the cache **are** charged the same as
fresh ones - you pay for the data, not for the request; the cache makes the run
faster and spares the employer's site, it is not a discount. The computation runs in your own account, so you always
see the real cost in the run log before you scale it up.

Two failure branches are named in `SUMMARY.stopReason`, never silent. If a charge
call raises - once, or on both attempts - we cannot tell whether the row was
billed, so the row is **delivered** anyway and the run stops with `charge-uncertain`:
we never keep money for a row you did not get, and at most one row per run can be
handed out in that doubt. If the dataset refuses a row we already paid for, we try
the write twice, then stop the run with `delivery-error` and write that paid row -
its job id, tenant, site, title, url and the error - into the `DELIVERY_FAILURES`
record of the run's key-value store, so there is a trail to claim a refund
against.

## Cache

A repeated run inside `cacheMinutes` (60 by default) reuses the answer instead of
hitting the same board again - the same jobs, faster, and without hammering the
employer's career site. The cache lives in **your** key-value store, under your
account; nothing is shared between customers.

## What this Actor does not do

* it does not guess a site name when the URL has none - it tells you what is
  missing instead of returning an empty table;
* it does not work around a company that removed its Workday board - that is an
  honest `HTTP_404`;
* it does not promise every employer on earth: Workday only, one URL per line.
  For Greenhouse, Lever, Ashby, Workable, SmartRecruiters and Recruitee use our
  Company Jobs API.
