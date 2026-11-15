# Wayback Machine API - First Seen, Last Seen & Versions

Answers the question SEO audits, link-rot checks and research work actually ask: was this
page ever archived, when did it first appear, when did it change last, and which distinct
versions exist.

It reads the Internet Archive CDX API. No API key, no proxy, no browser, no HTML parsing.

## What you get per URL

| field | meaning |
|---|---|
| `url` | the URL as the archive sees it, normalized |
| `host` | the host name |
| `firstSnapshotAt` | when the archive first captured this page, inside your window if you set one |
| `firstSnapshotStatus` | the HTTP status of that earliest capture |
| `lastSnapshotAt` | the newest capture inside your window |
| `versionsReturned` | how many distinct versions are in `latestVersions` |
| `capturesRead` | how many raw captures were read to get them |
| `latestVersions` | the newest versions, each with `at`, `timestamp`, `url`, `statusCode`, `mimeType`, `digest`, `lengthBytes` |
| `windowFrom`, `windowTo` | the date filter you sent, echoed back |
| `partial` | true when the newest versions could not be read and only `firstSnapshotAt` is filled |
| `source` | where the row came from |

Versions are collapsed by content digest, so ten identical copies of a page count as one
version and the list stays short. The archive collapses only neighbouring captures, and two
variants of one page alternate, so the reader pulls up to three times the requested number of
captures and keeps the newest capture of each digest. `capturesRead` says how many raw
captures that took.

## What you pay for, and what you do not

One `url-checked` event per URL that the archive really holds history for. The row is
written and charged in one call, so a row you paid for is always a row you can download.

These come back **free**, with a status and a plain reason, and carry no history fields:

| status | when |
|---|---|
| `NOT_FOUND` | the archive never captured that URL, or nothing inside your date window |
| `INVALID_INPUT` | not a public http/https page: no dot in the host, a private or loopback address, another scheme, an empty line |
| `DUPLICATE` | the same URL was already sent in this run |
| `NOT_CHECKED_CAP` | the run hit `maxUrls` or the spending limit |
| `NOT_CHECKED_TIME` | the run ran out of time before this line |
| `NOT_CHECKED_ERROR` | the archive could not be read |
| `NOT_CHECKED_BILLING` | the price list could not be read, so nothing can be sold |

The lowest spending limit you can set per run is $0.05. That is a floor on the cap the
buyer sets, not a price you pay to start: ten URLs at the default price cost well under it.

## Speed and limits

Two calls per URL, about 0.3 to 3 seconds depending on how deep the archive has to search.
`maxUrls` defaults to 200. `maxSnapshotsPerUrl` defaults to 20 and caps at 100. A date
filter that is not `YYYY`, `YYYYMM`, `YYYYMMDD` or `YYYYMMDDhhmmss` stops the run before the
first request instead of being silently ignored.

## What this actor does not do

* It does not download the archived page bodies. It reports that a capture exists and what
  its status, MIME type, digest and size are.
* `lengthBytes` is the length of the archive's WARC record, not the size of the page as the
  browser would see it.
* It does not search by domain or prefix. One URL per line, exactly as the archive indexes
  them; a domain-wide sweep is a different tool.
* It does not bypass anything and it does not need to: the CDX API is public.

## Run summary

`RUN_SUMMARY` in the run's key-value store, plus a copy under `RUN_SUMMARY-<runId>` in the
named store `wayback-machine-summary`, so two simultaneous runs do not overwrite each other.
It names `delivered`, `chargedEvents`, `deliveredUncharged`, `deliveredUnverified`,
`chargeUncertain`, `urlsNotFound`, `stopReason`, `datasetNote` and `firstFailure`: when the
platform cannot confirm a charge, the summary says the outcome is unknown instead of
claiming either delivery or a refusal.

## Example input

```json
{
  "urls": ["example.com", "https://apify.com/pricing", "this-domain-never-existed-xyz-2026.com"],
  "maxSnapshotsPerUrl": 20
}
```

The third line has no snapshots, so it comes back as a free `NOT_FOUND` row with the reason.

Source: Internet Archive CDX API. This actor is not affiliated with or endorsed by the
Internet Archive.
