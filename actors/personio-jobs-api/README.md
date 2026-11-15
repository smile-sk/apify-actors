# Personio Jobs API

A wrong company name gives you a clean run with zero jobs. Personio answers an empty
feed for a company that does not exist, and the empty feed looks exactly like a board
with no open positions, so a scraper that trusts it reports success and you cannot tell
a typo from a hiring freeze. This Actor checks the career page before it calls a board
quiet: a missing board becomes a row that says so, and a quiet board becomes a different
row. Both cost nothing.

It reads the employer's own public XML feed: no key, no login, no proxy, and one request
returns the whole board together with every description section.

## What you get

One dataset row per open position:

| field | meaning |
| --- | --- |
| `jobId`, `title`, `url`, `applyUrl` | the position and its public page |
| `subcompany` | the legal unit the employer filed it under |
| `department`, `recruitingCategory` | where the employer put it |
| `employmentType`, `seniority`, `schedule` | permanent, internship, full-time, experienced and so on |
| `occupation`, `occupationCategory` | Personio's own codes |
| `keywords` | the employer's keyword list, split on commas |
| `city`, `locations` | every office, the main one first, not only the primary one |
| `remote` | true when the office name or the description says remote or homeoffice, even with `includeDetails` off |
| `createdAt` | creation date as published |
| `description`, `descriptionSections` | the full text, and the same text split into the sections the employer wrote (team, tasks, benefits) |
| `status`, `reason`, `delivered`, `billable` | what happened to this row |

`includeDetails` off gives you the metadata without the text, which keeps rows small.
`language` asks the board for a translation (`de`, `en`), and a board that has none falls
back to its own language.

## Rows that are not positions

| status | when |
| --- | --- |
| `NOT_CHECKED_BOARD_NOT_FOUND` | the company has no Personio board: the feed is empty and the career page answers 404 |
| `NOT_CHECKED_BOARD_EMPTY` | the board exists and has no open position |
| `INVALID_INPUT` | the string is not a board identifier |
| `NOT_CHECKED_ERROR` | the board could not be read, or the run had to stop |
| `NOT_CHECKED_NO_MATCH` | the board was read and no position matched your filters |
| `NOT_CHECKED_CAP` | the spending cap stopped the run before this position |

Only `DELIVERED` rows are charged. `billable` is what the price list said before the row was
written (true when it carries this event, false when billing is off, null when the price could
not be read). The money actually taken is `chargedEvents` in the run summary: a row can land
uncharged, and it is then counted in `deliveredUncharged`.

## Price

You pay for a delivered position, about 1.50 USD per 1,000 positions, and nothing for a
position that was not delivered. A board that does not exist, a board with no openings, a
position filtered out, a position past `maxJobsPerBoard` and a row the spending cap
stopped all cost zero. The platform also puts a default ceiling on one run (about 4.4 USD
unless you set your own, see `maxTotalChargeUsd` of your run), and when that ceiling is
reached the run stops, names the reason `spending-cap`, and writes the rest as
`NOT_CHECKED_CAP` rows instead of handing them out.

## Limits worth knowing before you buy

- The feed carries no salary and no country field. City names come as the employer typed
  them, so a country filter is not possible from this source.
- `remote` is read from the employer's text, not from a flag, because the feed has no such
  flag. Treat it as a hint. Turning `includeDetails` off does not change it: the text is read
  for the hint even when it is not shipped in the row.
- One board is one request. The largest board I measured returned 196 positions and 2.5 MB
  in a single answer, which is the whole board: there is no page cap here and no silent
  truncation. A feed over 40 MB is refused with a reason rather than cut.
- Description text is large. A thousand positions with `includeDetails` on is hundreds of
  megabytes of dataset, so set `maxJobsPerBoard` when you only need a sample.
- `datasetItemCount` in the summary is null when the platform counter cannot be read, and then
  `datasetItemCountNote` says why. The counters next to it are counted inside this run.

## What this Actor does not do

- It does not guess a company name. `ottonova` works, `ottonova inc` does not.
- It does not read the XML of a company that has no Personio board, because there is none.
- It does not promise that a position is still open after the employer closed it: the feed
  is a snapshot of the moment you run.
- It does not translate descriptions itself. It asks the board, and shows what the board
  answers.

## Summary for machines

The run writes `RUN_SUMMARY` into the run's own key value store and the same object into
the named store `personio-jobs-api-summary` under `RUN_SUMMARY-<runId>`, so two runs at the
same time cannot overwrite each other's numbers. The counters close inside the summary:
`boardsAsked` splits into ok, empty, not found, invalid, error and skipped; `positionsOnBoard`
splits into unparsable, merged, filtered and seen; `positionsSeen` splits into read and
processed; `positionsProcessed` splits into delivered, unverified and not processed. When a
sum does not close, the summary says so in a `*Mismatch` field instead of rounding the
difference away.
