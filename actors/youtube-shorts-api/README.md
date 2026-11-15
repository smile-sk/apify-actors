# YouTube Shorts API - Channel Shorts with Views

You should not have to open a channel, scroll its Shorts shelf by hand and copy ids
one at a time. Give this actor a channel handle, a channel url or a channel id, and it
returns one row per Short: the video id, a direct `youtube.com/shorts/<id>` link, the
title, the view count as a number and as shown, its position on the shelf and the
thumbnail. Paging follows the continuation token the page itself hands back, so a
channel with hundreds of Shorts is covered, not just the first screen.

## What you get

One row per Short:

| field | meaning |
|---|---|
| `channelInput` | the channel exactly as you sent it |
| `channelId`, `channelTitle` | the resolved channel id (`UC...`) and its title |
| `videoId`, `url` | the id and `https://www.youtube.com/shorts/<id>` |
| `title` | the Short's title as the shelf shows it |
| `views`, `viewsText` | view count as a number and as shown (`79M views`, `17K views`) |
| `position` | its place on the shelf, starting at 0 |
| `thumbnailUrl` | the frame YouTube shows on the shelf |
| `status`, `billable` | `OK` for a delivered Short; a status row carries a reason instead of data |

## Depth, measured

On 2026-11-12 one Shorts page returned **48 entries and a continuation token**, and
three pages gave **144 unique** Shorts. `maxShortsPerChannel` defaults to 100 and is
capped at 1000. The Shorts tab is found on the channel page itself: the actor reads the
tab list and takes the Shorts params from it, so a change to YouTube's own tabs does not
silently turn into an empty run.

## What this actor does not do

* It does not read long-form videos. Shorts only - the search and video actors cover the rest.
* It does not read comments, captions or transcripts.
* It does not return upload dates: the shelf shows a view count, not a date, and inventing
  one would be worse than leaving it out.
* It does not search by keyword. It reads channels you name; a channel with no Shorts tab
  comes back as a free row saying so.
* It does not bypass a block. If YouTube answers something other than a channel or a
  Shorts shelf, the row says so and costs nothing.

## Money

Price: **$0.0002 per delivered Short** (`short-delivered`), The lowest spending limit you can set per run is $0.05.

A row is charged only when it reached the dataset. A status row never carries a video id,
a title or a thumbnail, so it can never be mistaken for the product.

Free, with the reason in the row:

| status | when |
|---|---|
| `INVALID_INPUT` | the channel could not be resolved from what you sent |
| `DUPLICATE` | the same channel appears twice in one run |
| `NO_SHORTS_FOUND` | the channel has no Shorts tab, or the tab is open and empty |
| `NOT_CHECKED_LIMIT` | beyond `maxShortsPerChannel` |
| `NOT_CHECKED_TIME` | not enough time before the platform deadline |
| `NOT_CHECKED_ERROR` | the channel or Shorts page could not be read; the reason names the error |
| `NOT_CHECKED_CAP` | the spending cap leaves no budget |
| `NOT_CHECKED_BILLING` | the price could not be read, so nothing is sold |
| `NOT_CHECKED_CHARGE_UNVERIFIED` | the charge failed and the row was not confirmed |

`stopReason` names the money fact first: `spending-cap`, `billing-price-unknown`,
`charge-error`, `dataset-write-error` or `deadline`. `counterMismatch` in the summary is
`true` when delivered rows do not add up to charged plus delivered-unpaid, and
`datasetNote` explains a doubt the counters cannot express.

## Input

| field | default | meaning |
|---|---|---|
| `channels` | required | handles, urls or channel ids, one or more |
| `maxShortsPerChannel` | 100 | stop after this many Shorts per channel |
| `region` | US | two letter country code that shapes the mix |
| `language` | en | two letter language code for titles and labels |

## Resource use

512 MB of memory and no browser: the actor talks to the public innertube endpoint with
plain HTTP, so a run of 100 Shorts finishes in seconds.

## Status of this build

`0.1`. The parsing is checked against a saved real Shorts page and a real channel page,
and the money path is checked by running the real `main()` against a stand that repeats
the SDK, including its silent cut of a batch that does not fit the budget.
