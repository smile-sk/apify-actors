# YouTube Search API - Video Results by Keyword

You should not have to copy YouTube results by hand page after page. Give this actor one
or more keyword phrases and it returns one row per video: title, channel, views, age,
duration, thumbnail and a direct url. It pages through the results with the continuation
token the page itself hands back, so the long tail of a niche is reachable, not just the
first screen.

## What you get

One row per video:

| field | meaning |
|---|---|
| `query` | the phrase this video was found for |
| `videoId`, `url` | the id and `https://www.youtube.com/watch?v=<id>` |
| `title` | as shown in the results |
| `channel`, `channelId`, `channelUrl` | who published it |
| `views`, `viewsText` | view count as a number and as shown (`13,456,364` / `13,456,364 views`) |
| `publishedText`, `publishedDaysAgo` | age as the page states it, and that age converted to a lower bound in days |
| `durationText`, `durationSeconds` | length, both as shown and in seconds |
| `isLive` | `true` for a live broadcast, otherwise `null` |
| `badges` | labels the page carries (for example `Fundraiser`) |
| `description` | the snippet YouTube shows under the result, when it shows one |
| `thumbnailUrl` | cover image |
| `status`, `billable` | `OK` for a delivered video; a status row carries a reason instead of data |

## Depth, measured

On 2026-11-12 a search page returned 9-20 videos and a continuation token, and five pages
of one phrase gave **79 unique videos**. `maxResultsPerQuery` defaults to 50 and is capped
at 500. Depth is limited by how many pages YouTube is willing to serve for a phrase, not by
this actor: when the token runs out, the phrase is finished.

`sortBy=views` really reorders the page (the first result is the most watched).
`sortBy=date` and `sortBy=rating` are **ignored by the endpoint** - the first page comes
back identical - so they are not offered here. For fresh videos use `uploadDate`:
`hour`, `today`, `week`, `month` or `year` really do restrict the page (measured: a week
filter returned videos published 23 hours to 6 days ago).

## What this actor does not do

* It does not read transcripts or captions - that is a different actor.
* It does not read comments.
* It does not return the exact upload timestamp: YouTube shows an age ("2 years ago"), so
  `publishedDaysAgo` is a lower bound, not a date. Do not treat it as one.
* It does not click through to the video page. Everything above comes from the results
  page itself.
* It does not bypass a block. If YouTube answers something other than results, the row says
  so and costs nothing.
* It does not return **Shorts**. YouTube puts a Shorts shelf beside the results and renders
  those entries as `shortsLockupViewModel`, not as videos. They are counted in
  `shortsSkipped` in the summary and cost nothing; they never become rows. Measured on
  2026-11-12: a search for "cats" carried 40 Shorts next to 9 videos, so this is the
  difference between "nothing was found" and "a shelf was skipped".

## Money

Price: **$0.0004 per delivered video** (`video-delivered`), The lowest spending limit you can set per run is $0.05.

A row is charged only when it reached the dataset. A status row never carries a video id,
a title or a description, so it can never be mistaken for the product.

Free, with the reason in the row:

| status | when |
|---|---|
| `INVALID_INPUT` | the phrase is empty or not a phrase |
| `DUPLICATE` | the same phrase appears twice in one run |
| `NO_RESULTS_FOUND` | the search returned no videos |
| `NOT_CHECKED_LIMIT` | beyond `maxResultsPerQuery` |
| `NOT_CHECKED_TIME` | not enough time before the platform deadline |
| `NOT_CHECKED_ERROR` | the search page could not be read; the reason names the error |
| `NOT_CHECKED_CAP` | the spending cap leaves no budget |
| `NOT_CHECKED_BILLING` | the price could not be read, so nothing is sold |
| `NOT_CHECKED_CHARGE_UNVERIFIED` | the charge failed and the row was not confirmed |

`shortsSkipped` is a counter, not a row: Shorts are never sold here and never delivered,
so nothing about them is charged.

`stopReason` names the money fact first: `spending-cap`, `billing-price-unknown`,
`charge-error`, `dataset-write-error` or `deadline`. `counterMismatch` in the summary is
`true` when delivered rows do not add up to charged plus delivered-unpaid, and
`datasetNote` explains a doubt the counters cannot express.

## Input

| field | default | meaning |
|---|---|---|
| `queries` | required | one or more keyword phrases |
| `maxResultsPerQuery` | 50 | stop after this many videos per phrase |
| `sortBy` | relevance | `relevance` or `views` |
| `uploadDate` | any | `hour`, `today`, `week`, `month`, `year` |
| `videoDuration` | any | `short` (under 4 min), `medium`, `long` (over 20 min) |
| `region` | US | two letter country code that shapes the mix |
| `language` | en | two letter language code for titles and labels |

## Resource use

512 MB of memory and no browser: the actor talks to the public search endpoint with plain
HTTP, so a run of 50 videos finishes in seconds and a large run in a couple of minutes.

## Status of this build

`0.1`. The parsing is checked against a saved real page, and the money path is checked by
running the real `main()` against a stand that repeats the SDK, including its silent cut of
a batch that does not fit the budget.
