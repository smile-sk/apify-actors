# YouTube Comments API

Give it video urls and get one row per comment: the text, the author, likes, reply count,
whether the channel is verified or the video owner, whether the uploader hearted the
comment, whether it is pinned, the age YouTube prints, and a direct link to the comment.
Replies come as their own rows with `parentCommentId`, and you can ask for the newest
comments first instead of the default "top".

Built for the two ways a comment scraper usually cheats you. It does not come back green
with an empty dataset when a video has its comments turned off: that video gets a free row
that says so. And it does not charge for a comment you cannot use: an emoji-only comment
has no text to sell and is counted, not billed.

## What you pay for

One event, `comment-delivered`, is charged per comment row that lands in the dataset.
Everything else is free and says why in the row's `reason`:

* the line is not a video (`INVALID_VIDEO`), or the same video appeared earlier
  (`DUPLICATE`);
* the watch page exposes no comment section, which is what a closed comment section looks
  like (`NOT_CHECKED_NO_COMMENTS`);
* YouTube answered 404 (`NOT_CHECKED_UNAVAILABLE`) or something else went wrong while
  reading (`NOT_CHECKED_READ_ERROR`);
* the run hit `maxVideos` or `maxCommentsPerVideo` (`NOT_CHECKED_LIMIT`), the spending cap,
  or the platform's clock (`NOT_CHECKED_TIME`);
* the price could not be read (`NOT_CHECKED_BILLING`): the run then reads nothing and
  charges nothing rather than guessing.

A comment that was read but not delivered is never dropped in silence: when the run stops
for time, for the cap or after a charge error, every read comment comes back as its own
free row naming the reason, so `commentsSeen` always adds up.

## What it does not do

* It does not invent an absolute date. YouTube's payload carries a relative age
  ("6 years ago"), so `publishedTimeText` is what you get, and `publishedAt` does not
  exist in the output.
* It does not read replies of replies (YouTube shows one level below a comment).
* It does not log in, so it only reads what a guest sees.
* It does not download video, audio or thumbnails, and it does not translate comments.
* It does not promise every comment of a huge video: `maxCommentsPerVideo` is your budget
  and the run says when it stopped at it.

## Input

`videos` (required): a watch url, a `youtu.be` link, a shorts link or the bare 11
character id, one per line. The schema prefill holds two real videos so the Run button
works without typing anything.

`maxCommentsPerVideo` defaults to 100 (hard ceiling 2000). `sort` is `top` (what the watch
page shows) or `newest`. `includeReplies` defaults to true and `maxCommentsPerThread` to
20. `maxVideos` defaults to 20 (hard ceiling 200).

A quarter of `maxCommentsPerVideo` is held back for replies, and the leftover space goes
back to the listing. Measured on a live video with 40 rows: 32 top level comments and 8
replies, every reply naming its parent. Without that split a busy video fills the whole
budget with top level comments and never opens a thread, so a title that promises replies
would sell none.

## How it reads, measured on 2026-11-12

The actor talks to YouTube's public `youtubei/v1/next` endpoint over plain HTTP. The same
request answered from a home connection and from our Frankfurt VPS, a datacenter address:

* the watch payload came back at 420 086 bytes and carried the comment section as an
  `itemSectionRenderer` with `sectionIdentifier: "comment-item-section"`;
* that section's continuation token returned 21 comment threads and 20 comment entities,
  284 311 bytes, from both addresses;
* the comment text lives in `frameworkUpdates.entityBatchUpdate`, not inside the thread
  renderer, and like and reply counts are strings ("4.8M") that are parsed into numbers;
* one listing page carries 20 comments, and 10 pages gave 200 unique comments in 4.7
  seconds on our machine;
* the comment toolbar carries a static heart label ("by @channel") on every record, 180
  of 180 on three videos, so it is a state template and not a fact. `heartedByCreator`
  is computed from the creator's thumbnail, which appeared on 4 of 60 comments on one
  video and 0 of 60 on two others, and a test pins the difference;
* `activeFilters` style tricks are not needed: the sort menu tokens for "Top" and "Newest"
  are in the watch payload.

`Comments are turned off.` appears in the page header even on a video with ten million
comments, because the reply box is disabled for a guest. The actor never uses that phrase
as a signal, and a test pins it.

## Notes for integrators

The summary is written under `RUN_SUMMARY` in the run's default key value store, which is
where the schema link points. Fields: `videosRequested`, `videosAttempted`, `videosRead`,
`videosNoComments`, `videosStopped`, `videosFailed`, `commentsSeen`, `commentsDelivered`,
`commentsSkippedNoText`, `commentsDuplicate`, `commentsUndelivered`, `repliesDelivered`,
`pagesRead`, `chargedEvents`, `deliveredUncharged`, `chargeUncertain`, `writeErrors`,
`rowsWritten`, `videosInvalid`, `videosDuplicate`, `videosLimited`, `videosCutByBudget`,
`priceEvent`, `datasetNote`, `byStatus`, `billingActive`, `priceNote`, `stopReason`,
`counterMismatch`, `finishedAt`. `videosRequested` always equals `videosAttempted` plus
`videosStopped` plus the invalid, duplicate and limited lines. `videosCutByBudget` counts
read videos that ran out of their own `maxCommentsPerVideo` budget: it describes an
attempted video and is never added to that sum.

Apache 2.0. Issues and feature requests are welcome on the actor's page.
