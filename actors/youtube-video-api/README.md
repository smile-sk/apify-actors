# YouTube Video API: full metadata for a list of videos

One list of video ids or links in, one row per video out: title, channel, views, length,
publish date, category, keywords, live flag, thumbnail and a playable link.

The data comes from the public innertube endpoint. No API key, no browser, no login, so
there is no key of yours that can expire and nothing of yours to configure.

## How the request travels

YouTube refuses this endpoint for the addresses Apify containers run on: the same request
from an ordinary datacenter address in Europe answers with a bot check while the identical
call from a home address is answered. So the actor asks a small relay we run on our own
VPS, and the relay asks YouTube. The relay does exactly one thing: it takes an 11 character
video id and returns the player answer. It is not a general proxy, it holds no data after
the answer is sent, and it needs a token that lives in this actor's environment, not in
its source.

If the relay itself fails (dead, busy, wrong token), the actor falls back to calling
YouTube directly for that video and says so in the summary. `YT_RELAY_REQUIRED=1` switches
that fallback off; it is our own switch, declared in this actor's settings and set by us,
so a buyer never has to change anything. The summary names the road every delivered row
took: `gateway`, `relayCalls`, `relayFallbacks`, `relayErrors` and `transports`.

Three rules keep that fallback honest:

* **The two roads do not share one time budget.** The direct call gets what is left of the
  run, not the number the relay already spent, so one video cannot run past the deadline
  and take the summary down with it. When less than two video-times are left, the direct
  road is not called at all and the row says so.
* **A busy relay is asked once more.** On HTTP 429 the actor waits for the relay's
  `Retry-After` and repeats the same video once, instead of pushing it onto the road
  YouTube closes.
* **A road in trouble that answered nothing is a failure, not a green run.** If every
  attempted video was refused by YouTube or by the relay, the run fails with the number of
  attempts, the number of refusals and the first real reason. A video that does not exist is
  a different thing: it comes back as a free `NOT_FOUND` row with the reason YouTube gave,
  so a list of deleted videos is a normal green run with free status rows.

Honest about the dependency: this actor is not fully independent of our infrastructure.
The relay is one small service. If the relay is down and the direct road is refused, the
row comes back free with `NOT_CHECKED_ERROR` and the reason the relay gave, never as "the
video does not exist".

## Why this exists

A video that cannot be played still has metadata. Most scrapers answer an error when the
player refuses playback, and the buyer loses a row they already know exists. Here the
playability status is reported as a field, and the row is delivered with the numbers.

## Input

* `videos` (required): bare 11 character ids, `youtube.com/watch?v=` links, `youtu.be`
  links, `/shorts/` links and `/embed/` links.
* `maxVideos` (default 50, maximum 200): how many videos one run reads.
* `includeDescription` (default false): add the video description to each row.

## Output

One row per delivered video: `videoId`, `url`, `title`, `channelTitle`, `channelId`,
`views`, `lengthSeconds`, `publishedAt`, `category`, `keywords`, `isLive`,
`thumbnailUrl`, `playability`, `playabilityReason`, `description` (with
`includeDescription`), `status` and `billable`.

A field the endpoint does not publish is `null`, not zero. `playability` is what YouTube
answered about playback: `OK`, `UNPLAYABLE`, `LOGIN_REQUIRED` and so on. A video with a
playability other than `OK` is still delivered, because its metadata is real.

`playability` and `playabilityReason` are the answer this client got, not a property of
the video: a datacenter address often receives `UNPLAYABLE` with the reason "The page
needs to be reloaded" for videos that play normally in a home browser. The metadata next
to it is unaffected.

## Price

You pay for one delivered video (`video-delivered`), and only when the row reached the
dataset. The lowest spending limit you can set per run is $0.05.

Free, with a status and a reason instead of data: an empty or unusable input, a repeated
video, a video YouTube does not have, a video past `maxVideos`, a video the run had no
time for, an endpoint that answered an error, and anything after the spending cap or
after a failed charge.

A row that could not be delivered carries no `videoId`, `title`, `thumbnailUrl` or `url`,
so a free row can never be mistaken for a delivered one.

`billable` on a row is the price claim taken before the write, not a receipt: the money
actually charged lives in `chargedEvents` and `deliveredUncharged` in the run summary.
An empty `videos` list is an input mistake and the run stops with a message naming the
field, rather than returning a green run with nothing in it.

## What this actor does not do

* It does not download the video or the audio.
* It does not read comments. That is `youtube-comments-api`.
* It does not read transcripts. That is `youtube-transcript-api-fast`.
* It does not list a channel or a search. That is `youtube-channel-api` and
  `youtube-search-results-api`.
* It does not go past what the endpoint shows, and it does not guess a hidden view count.

## Status codes

`OK`, `INVALID_INPUT`, `DUPLICATE`, `NOT_FOUND`, `NOT_CHECKED_LIMIT`, `NOT_CHECKED_TIME`,
`NOT_CHECKED_ERROR`, `NOT_CHECKED_CAP`, `NOT_CHECKED_BILLING`.

## Stop reasons

`dataset-write-error` (the paid write raised and the dataset count did not grow, so the
batch is counted in `deliveredUnverified` and nothing is claimed as sold),
`billing-price-unknown` (the event price could not be read, so nothing is delivered and
nothing is charged), `spending-cap` (the buyer's spending limit was reached),
`charge-error` (the rows landed but the charge failed or did not report a count),
`deadline` (the run was close to being stopped by the platform).

## Run summary

The summary is written to the run store under `RUN_SUMMARY`, and a copy per run goes to
the `youtube-video-summary` store under `RUN_SUMMARY-<runId>`. It carries the counters
listed in the output schema, including `gateway` (which road was configured),
`transports` (how many delivered rows went through the relay and how many were fetched
directly), `relayCalls`, `relayFallbacks` and `relayErrors`, plus `counterMismatch`, which is `false` only when every
input line ended up in exactly one status. `videosCutByBudget` is the tail the spending
limit cut, `videosCutByDeadline` is the tail the clock cut and `videosCutByStop` is the
tail any other stop cut: they are counted apart because they mean different things to the
buyer, and only the money tail is marked `NOT_CHECKED_CAP`.

When the run stops on a paid write it names what it knows and nothing more. If the dataset
counter could not be read at all, the note says delivery is unknown; if it grew by fewer
rows than were sent, the note names both numbers. In neither case does the run claim that
nothing was delivered.
