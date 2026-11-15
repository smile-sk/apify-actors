# Teamtailor Jobs API - All Offices, Remote & Expiry

Read any public Teamtailor career board and get one row per open role: every office of a
multi-location role (not only the first), the remote flag the employer actually set,
employment type, publication date, expiry date and the full description.

## Why this exists

Most Teamtailor scrapers read the rendered career page and keep the first location they
see. The board's own feed knows better: 15 of 109 live roles carried more than one office,
and the feed sends them as one list. It also marks remote roles explicitly, says when a
posting is valid until, and carries the whole description inside the same answer, so no
second request per role is needed.

## Source

The employer's own public JSON Feed: `https://<board>/jobs.json`, JSON Feed 1.1 with a
schema.org `JobPosting` record per role. No API key, no login, no proxy, no browser.

A board is either `<name>.teamtailor.com` or the employer's own host: `jobs.tibber.com`,
`career.kry.se` and `jobs.lunar.app` were all read live. Both forms are accepted.

The feed is a static export: it arrives in one answer and `next_url` is null on every live
board, so there is no page 2 that could be silently skipped.

## What a row carries

`board, jobId, title, company, employmentType, directApply, remote, city, state, country,
countryCode, postalCode, locations[], publishedAt, validThrough, url, description`
and, for rows that are not roles, `status` and `reason`.

`locations` is a list of every office with city, region, country, country code, postal code
and street. `city`, `state`, `country`, `countryCode` and `postalCode` on the row belong to
the first office.

`remote` is true only when the posting says so (`jobLocationType` is `TELECOMMUTE`, or the
posting carries applicant location requirements). It is never guessed from the word
"remote" in a title.

## Filters

`keywords` (comma separated, matched against title and company), `employmentType` (contains),
`city` (any office of the role), `country`, `remoteOnly`, `publishedAfter`, `maxJobsPerBoard`.

`country` deserves a word: a two letter code is compared for equality, a longer word is
matched against the country name, and both are checked across every office of the role.
A substring match would have delivered Singapore and the United Kingdom for the code `in`,
which is a paid row for a country the buyer never asked for.

## What this actor does not do

- It does not invent fields the feed does not carry. There is no salary in the Teamtailor
  feed and no department, so there are no `salary` or `department` columns at all. An
  always empty column would look like a promise.
- It does not read private addresses. A buyer types their own host, so every address we
  open is resolved first and refused if any address is private, loopback, link local or
  reserved, including IPv4-mapped IPv6 such as `::ffff:169.254.169.254`. Every redirect hop
  is checked again, and only `https` is followed.
- It does not log in, does not apply, and does not read anything behind a login.
- It does not follow a board's `next_url` into other hosts. If Teamtailor ever paginates a
  board, the summary says how many roles were read, so a short answer is visible.

## Price

Pay per event: `job-delivered`, 0.0015 USD per delivered role, so 1.50 per 1,000 roles.
A row that is not a role is free: `NOT_CHECKED_BOARD_NOT_FOUND`, `NOT_CHECKED_BOARD_EMPTY`,
`NOT_CHECKED_NO_MATCH`, `NOT_CHECKED_CAP`, `NOT_CHECKED_ERROR`, `INVALID_INPUT`.
The row and the charge are written in one call, so "paid" and "delivered" are one event.

The platform applies its own spending cap when the buyer does not set one (about 4.4 USD on
our runs, `options.maxTotalChargeUsd` in the run). Roles past the cap are counted in
`positionsNotProcessed` and written as `NOT_CHECKED_CAP`, never charged and never silently
dropped. For a board with thousands of roles, set Max total charge accordingly.

## Summary

The run summary is written to the named key value store `teamtailor-jobs-api-summary`
under the key `RUN_SUMMARY-<runId>`, and that key is also reachable from the run output
schema. The bare `RUN_SUMMARY` key inside the run's own store belongs to that run only.

Counters are built so that they close:

- `boardsAsked = boardsOk + boardsEmpty + boardsNotFound + boardsInvalid + boardsError + boardsSkipped`
- `positionsOnBoard = positionsUnparsable + positionsMerged + filteredOut + positionsSeen`
- `positionsSeen = positionsNotRead + positionsProcessed`
- `positionsProcessed = delivered + jobsUnverified + positionsNotProcessed`

When a sum does not close, the summary carries `countersMismatch` naming it, and
`truncatedMismatch` when the per-board truncated flags disagree with a recount. A run that
stops on `spending-cap`, `charge-error` or `dataset-write-error` stops everywhere: the
remaining boards are written as `NOT_CHECKED_ERROR` instead of being handed out free.

## Two states of a board

A company with no board answers 404 on the feed itself, so it becomes a free
`NOT_CHECKED_BOARD_NOT_FOUND` row. A board that exists and has no open role answers 200
with an empty list, so it becomes a free `NOT_CHECKED_BOARD_EMPTY` row. Neither is reported
as a green run with no data.
