# Recruitee Jobs API - All Offers, Every Office, Descriptions

Read any public Recruitee board: every open offer, every office of that offer, the full
description and requirements, salary as numbers. No key, no login, no proxy.

You pay **1.50 per 1,000 delivered offers** (event `job-delivered`, $0.0015 per row).
A row that is not an offer - a missing board, an empty board, a filtered run, an offer the
spending cap stopped - sits in the dataset, explains itself and costs nothing.

## Why this Actor exists

Measured on live boards while building it:

* **The whole board arrives in one answer.** `?page=` and `?limit=` are ignored by the
  offers endpoint: same bytes, same first id, every time. A 124-offer board (ggzbreburg)
  is 1.9 MB, of which 715 KB is employer text. Nothing is "on page two".
* **27% of live offers list more than one office** (34 of 124 on ggzbreburg, 8 of 23 on
  bunq). They stay **one row** with every office in `locations`, so one vacancy can never
  be charged twice.
* **A company that does not exist answers 404 on the endpoint itself**, so "there is no
  such board" and "the board has no openings today" are different answers, not one silent
  empty run. The first is `NOT_CHECKED_BOARD_NOT_FOUND`, the second is
  `NOT_CHECKED_BOARD_EMPTY`, both free.
* **Salary is read as a number.** `1,299`, `1,299.00` and `1.299,00` all arrive as
  `1299.0`; a field that quietly changes by a thousand times is worse than an empty one.

## Input

| field | what it does |
|---|---|
| `boards` | Recruitee boards: `bunq`, `bunq.recruitee.com` or `https://bunq.recruitee.com/`. Several per run. |
| `includeDetails` | Full `description` and `requirements` (default on). |
| `descriptionFormat` | `html` (default) keeps the employer markup, `text` strips it. |
| `maxJobsPerBoard` | How many matching offers per board; `0` means no limit. The rest are counted as `positionsNotRead` and never charged. |
| `keywords` | Comma separated; matches title, department, category and tags. |
| `department`, `employmentType`, `experience`, `category` | Substring filters on those fields. |
| `city` | Matches **any** office of an offer, not only the first one. |
| `remoteOnly` | Only offers the employer marked remote. A hybrid offer is not remote. |
| `publishedAfter` | `YYYY-MM-DD`; older offers are skipped. |
| `requestTimeoutSecs` | Wait per board answer (default 60). A big board is one big answer. |

## Output

One dataset row per open offer: `board`, `jobId`, `title`, `department`, `location`,
`city`, `state`, `country`, `countryCode`, `locations` (every office), `remote`, `hybrid`,
`onSite`, `employmentType`, `experience`, `education`, `category`, `salaryMin`,
`salaryMax`, `salaryCurrency`, `salaryPeriod`, `tags`, `publishedAt`, `url`, `applyUrl`,
`description`, `requirements`, `delivered`, `billable`.

Rows that are not offers carry a `status` and cost nothing: `NOT_CHECKED_BOARD_NOT_FOUND`,
`NOT_CHECKED_BOARD_EMPTY`, `NOT_CHECKED_NO_MATCH`, `NOT_CHECKED_CAP`, `NOT_CHECKED_ERROR`,
`INVALID_INPUT`.

The run summary is written to `RUN_SUMMARY` in the run's key-value store and, under
**`RUN_SUMMARY-<runId>`**, into the named store `recruitee-jobs-api-summary`. The named key
is the one to read when several runs share the account: a bare `RUN_SUMMARY` there would be
overwritten by the next run.

Every counter closes inside the summary: `boardsAsked = boardsOk + boardsEmpty +
boardsNotFound + boardsInvalid + boardsError + boardsSkipped`; `positionsOnBoard =
positionsUnparsable + positionsMerged + filteredOut + positionsSeen`; `positionsSeen =
positionsNotRead + positionsProcessed`; `positionsProcessed = delivered + jobsUnverified +
positionsNotProcessed`. When a sum does not close the summary says so in
`countersMismatch`, and `truncatedMismatch` when the truncation flags disagree with a
recount.

## What this Actor does not do

* It does not scrape career-site HTML. It reads the public offers endpoint, which is the
  employer's own export.
* It does not bypass anything: no proxy, no login, no captcha.
* It cannot read a board that the owner made private: you get `NOT_CHECKED_BOARD_NOT_FOUND`
  or `NOT_CHECKED_ERROR` with the HTTP code, not an empty run.
* It does not translate offers. Descriptions arrive in the language the employer wrote.
* It does not guess a board from a company name. `bunq` works, `bunq.com` is rejected as a
  bad identifier, because a guess would hand you another company's rows.

## Price and platform limits

The price is pay-per-event: `job-delivered`, **$0.0015** per delivered offer. The lowest spending limit you can set per run is
$0.05. Platform charges its own fee on top.

Apify puts a default spending cap on a run when the buyer does not set one - about **$4.4**
for this Actor, the exact number is `options.maxTotalChargeUsd` in the run - which is
roughly 2,900 delivered offers. Past that the run stops with `stopReason=spending-cap` and
the rest is written as `NOT_CHECKED_CAP`. Raise the cap in run options for bigger boards.

Memory: 512 MB, measured peak on a 124-offer board is far below that (a 1.9 MB answer and
its parsed rows).

## Run it locally

```bash
PYTHONPATH=. apify run --input '{"boards": ["bunq"], "maxJobsPerBoard": 5}'
```
