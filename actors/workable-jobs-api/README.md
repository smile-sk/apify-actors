# Workable Jobs API

Most Workable scrapers stop at a few hundred postings and quietly drop multi-location roles. This Actor
reads the board's own public JSON in one request, keeps every location and every filter field the board
publishes, and returns the posting description when you want it.

## What you get

- Every open posting of a board, from one request: 744 postings on `fuku` arrive in a single 2.4 MB answer.
- One row per posting, even when the board splits a multi-city role into one entry per city. The entries are
  merged by job id before any filter or limit, all cities land in `locations`, and the posting is charged once.
- `title`, `department`, `function`, `industry`, `education`, `experience`, `employmentType`.
- `remote` as a real boolean, taken from the board, not guessed from the text.
- `locations`: all of them, each with `country`, `countryCode`, `city`, `region`. The flat `country`,
  `city` and `state` fields are the board's own primary location.
- `publishedOn` and `createdAt` dates, `url` and `applyUrl`.
- `description` when `includeDetails` is on, as `html` or as plain `text`.
- Optional filters: keywords, department, function, industry, employment type, country, remote only,
  published after.

## Input

| field | meaning |
|---|---|
| `boards` | board identifiers or URLs: `celsius`, `fuku`, `https://apply.workable.com/prx/`. Duplicates are read once. |
| `includeDetails` | put the description into each row (default `true`) |
| `descriptionFormat` | `html` keeps the employer markup, `text` strips tags and decodes entities |
| `maxJobsPerBoard` | how many matching postings to process per board, `0` means all |
| `sortBy` | `published` or `created`, newest first, applied before `maxJobsPerBoard` |
| `keywords` | comma separated phrases, a posting matches when any phrase occurs in title, department or function |
| `department`, `function`, `industry`, `employmentType` | case insensitive substring match |
| `country` | country name or ISO code: the code is compared by equality (`in` means India, not Singapore), a name on a word boundary, and a posting matches when any of its locations is in that country |
| `remoteOnly` | keep postings the board flags as remote |
| `publishedAfter` | `YYYY-MM-DD`, keeps postings published on or after that day |

## Money

One charge per delivered posting: event `job-delivered`, $0.0015 per row.

Never charged: board identifiers that are not identifiers, boards that do not exist, boards with no open
postings, boards that could not be read, postings filtered out, postings past `maxJobsPerBoard`, and
postings the spending cap stopped. Those rows either do not exist or carry a `NOT_CHECKED_*` or
`INVALID_INPUT` status and cost nothing.

The row is written and charged in one call, so a run cannot charge for a row the dataset never received.
If that one call raises, the row is never re-sent (the SDK writes before it charges, so a retry could
duplicate it): we ask the dataset how many rows it holds and report the row as delivered and uncharged
when it landed, or as `jobsUnverified` when it did not. An unconfirmed write is never counted as delivered.

`billable` is decided before the write and follows the platform price list: `true` when the list carries
`job-delivered`, `false` when it does not, `null` when the price list cannot be read.

## Counters

Every run writes `RUN_SUMMARY` and closes these sums inside it:

- `boardsAsked = boardsOk + boardsEmpty + boardsNotFound + boardsInvalid + boardsError + boardsSkipped`
- `jobsOnBoard = jobsUnparsable + postingsMerged + filteredOut + jobsSeen`
- `jobsSeen = jobsNotRead + jobsProcessed`
- `jobsProcessed = delivered + jobsUnverified + jobsNotProcessed`

`postingsRead` is the number of distinct postings after the multi-city merge, `postingsMerged` is how many
duplicate entries that merge removed, and `jobsUnverified` counts postings whose write the dataset did not
confirm. `byBoard` carries `read`, `unparsable`, `merged`, `filtered`, `seen`, `notRead`, `delivered`,
`unverified`, `notProcessed`, `truncated`, `skipped` and a human `reason` for every board.

`truncated` means one thing only: this board's list is short. It is true when `maxJobsPerBoard` cut the
list, when the spending cap or a failing write stopped the run inside the board, and when the board was
not read at all because the run had already stopped. A board that does not exist is never called
truncated. `truncatedBoards` is checked against the counters of every board and prints `truncatedMismatch`
when they disagree. Any sum that does not close prints a `*Mismatch` field instead of being rounded away.

`chargedEvents` is what the platform charged, `deliveredUncharged` counts rows written while billing was
inactive, and `chargeUncertain` counts rows written when the charge call failed after the write. We never
retry such a row, because the SDK writes before it charges and a retry would duplicate it.

## The platform cap

Apify puts a default spending cap on a run when the buyer sets none: about $4.4, and the exact number is
in the run's `options.maxTotalChargeUsd`. At $0.0015 per posting that is roughly 2 900 postings, so a
board like SGS with several thousand postings stops at the cap with `stopReason: spending-cap`. The rest
is counted in `jobsNotProcessed`, each of those postings gets a free `NOT_CHECKED_CAP` row, and the boards
after it in `boardsSkipped`; raise the cap on the run if you want the whole board.

The same stop happens on money and on storage: two charge failures in a row (`stopReason: charge-error`)
and two writes the dataset did not confirm in a row (`stopReason: dataset-write-error`) stop the whole run,
so the boards after them are never delivered free.

## What this Actor does not do

- It does not scrape `workable.com` HTML pages. It reads the board's public JSON endpoint, which is what
  the board itself serves to embed widgets.
- It returns only what the board publishes. There is no salary field in that answer, so salary stays null
  rather than guessed.
- It does not filter on the server. Filters run in code after the whole board is read, which is why a
  filtered posting still costs you nothing but still takes time to read.
- It does not use proxies. If Workable ever blocks a datacenter address, the run says so with the HTTP
  code and status `NOT_CHECKED_ERROR` instead of pretending the board is empty.
- Private or unlisted boards are not readable, and neither is a board that has been closed.

## Summary location

The run summary is in the run's key value store as `RUN_SUMMARY`, and also in the named store
`workable-jobs-api-summary` as `RUN_SUMMARY-<runId>`, which is the key to read when several runs overlap.
