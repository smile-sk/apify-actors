# YouTube Channel API - Videos, Views and Subscribers

You should not have to open a channel, scroll its Videos tab by hand and copy ids one at
a time. Give this actor a channel handle, a channel url or a channel id, and it returns one
row per video: the id, a direct `youtube.com/watch?v=<id>` link, the title, the view count
as a number and as shown, the age, the duration in seconds, the position on the tab and the
thumbnail. The channel's own subscriber line is repeated on every row, so a sheet of videos
still says whose they are. Paging follows the continuation token the page itself hands back,
so a channel with thousands of uploads is covered, not just the first screen.

## What you get

One row per video:

| field | meaning |
|---|---|
| `channelInput` | the channel exactly as you sent it |
| `channelId`, `channelTitle` | the resolved channel id (`UC...`) and its title |
| `channelSubscribersText` | the subscriber line the page shows (`30.7M subscribers`) |
| `videoId`, `url` | the id and `https://www.youtube.com/watch?v=<id>` |
| `title` | as the tab shows it |
| `views`, `viewsText` | view count as a number and as shown (`92M views`) |
| `ageText`, `ageDays` | age as the page states it, and that age as a lower bound in days |
| `durationText`, `durationSeconds` | length, both as shown and in seconds |
| `position` | its place on the Videos tab, starting at 0 |
| `thumbnailUrl` | the cover image YouTube shows |
| `status`, `billable` | `OK` for a delivered video; a status row carries a reason instead of data |

## Depth, measured

On 2026-11-12 one Videos page returned **30 videos and a continuation token**, and three
pages gave **90 unique** videos. `maxVideosPerChannel` defaults to 50 and is capped at 1000.
The tab is found on the channel page itself: the actor reads the tab list and takes the
Videos params from it, so a change to YouTube's own tabs does not silently turn into an
empty run.

The current YouTube layout renders the Videos tab as `lockupViewModel`, not `videoRenderer`;
the id sits in `contentId` and views and age in `metadataRows`. That is why the rows carry a
number and a text for views: "92M views" is what the page says, and 92000000 is what it
means.

## What this actor does not do

* It does not read Shorts. They live on their own tab and in a different actor.
* It does not read comments, captions or transcripts.
* It does not return exact upload timestamps: YouTube shows an age ("11 days ago"), so
  `ageDays` is a lower bound, not a date. Do not treat it as one.
* It does not search by keyword. It reads channels you name; a channel with no Videos tab
  comes back as a free row saying so.
* It does not bypass a block. If YouTube answers something other than a channel or a video
  list, the row says so and costs nothing.

## Money

Price: **$0.0002 per delivered video** (`video-delivered`), the lowest spending limit you can set per run is $0.05.

A row is charged only when it reached the dataset. A status row never carries a video id, a
title, a thumbnail or a view count, so it can never be mistaken for the product.

Free, with the reason in the row:

| status | when |
|---|---|
| `INVALID_INPUT` | the channel could not be resolved from what you sent |
| `DUPLICATE` | the same channel appears twice in one run |
| `NO_VIDEOS_FOUND` | the channel has no Videos tab, or the tab is open and empty |
| `NOT_CHECKED_LIMIT` | beyond `maxVideosPerChannel` |
| `NOT_CHECKED_TIME` | not enough time before the platform deadline |
| `NOT_CHECKED_ERROR` | the channel or video page could not be read; the reason names the error |
| `NOT_CHECKED_CAP` | the spending cap leaves no budget |
| `NOT_CHECKED_BILLING` | the price could not be read, so nothing is sold |
| `NOT_CHECKED_CHARGE_UNVERIFIED` | the charge failed and the row was not confirmed |

`stopReason` names the money fact first: `spending-cap`, `billing-price-unknown`,
`charge-error`, `dataset-write-error` or `deadline`. `counterMismatch` in the summary is
`true` when delivered rows do not add up to charged plus delivered-unpaid, and `datasetNote`
explains a doubt the counters cannot express.

## Input

| field | default | meaning |
|---|---|---|
| `channels` | required | handles, urls or channel ids, one or more |
| `maxVideosPerChannel` | 50 | stop after this many videos per channel |
| `region` | US | two letter country code that shapes the mix |
| `language` | en | two letter language code for titles and labels |

## Resource use

512 MB of memory and no browser: the actor talks to the public innertube endpoint with
plain HTTP, so a run of 100 videos finishes in seconds.

## Status of this build

`0.1`. The parsing is checked against a saved real channel page and a saved real Videos page,
and the money path is checked by running the real `main()` against a stand that repeats the
SDK, including its silent cut of a batch that does not fit the budget.
