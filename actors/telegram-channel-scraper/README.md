# Telegram Channel Scraper - Posts, Views, Reactions

Read public Telegram channels through their web preview: no account, no phone number, no bot
token. One row per post with the exact publication time, the text, view count, reactions,
reply target, media and links, plus one free row per channel that says how that channel reads.

Pay only for delivered posts: one charge event `message-delivered` per post that reaches the
dataset. A channel that does not exist, turns out to be a group, hides its public preview or
answers with an error costs nothing, and its row names the reason in plain words.

## Why this actor exists

The complaint in this niche is not "the data is thin", it is "I got nothing and nobody told me
why". Telegram answers HTTP 200 for a username that does not exist, for a group chat, and for a
channel whose owner switched the public preview off. Scrapers that return an empty dataset for
all three leave the buyer guessing whether the channel is dead, private, or a group.

Here every input line produces a row. If posts arrive, the row says `READ` and the posts follow.
If they do not, the row says which of the impossible cases actually happened.

## What you get

Per post (`rowType: "message"`):

| field | meaning |
|---|---|
| `channel`, `messageId`, `url` | the post identity, `https://t.me/<channel>/<id>` |
| `date` | exact UTC timestamp from the page, not a rounded "2 hours ago" |
| `text`, `textLength` | the post body as plain text, line breaks kept |
| `views`, `viewsRaw`, `viewsApproximate` | `1.67M` becomes `1670000` and is marked approximate |
| `reactions` | list of `{emoji, emojiId, count, approx, paid, kind}` where `kind` is `emoji` or `stars` |
| `reactionsTotal` | the sum of reaction counts |
| `replyToId` | the post this one answers, when it answers one |
| `forwardedFrom`, `authorSignature` | the source of a forward, the signature of a signed channel |
| `edited` | Telegram marks the post as edited |
| `mediaType`, `mediaUrl`, `mediaTitle`, `mediaCount` | `photo`, `video`, `album`, `document`, `sticker`, `poll` |
| `linkPreviewUrl`, `linkPreviewSite` | the link card Telegram shows under the post |
| `links`, `hashtags`, `mentions` | what the body itself points at |

Per channel (`rowType: "channel"`, never charged): `channel`, `channelTitle`, `channelDescription`,
`channelPhotoUrl`, `subscribers` and `subscribersApprox`, `channelPhotos`, `channelVideos`,
`channelLinks`, `messagesSeen` (posts the page carried inside the date range, before the limit),
`messagesDelivered` (posts of this channel that reached the dataset), `pagesFetched`, `truncated`
(true when `maxPostsPerChannel` cut this channel), `status`, `reason`.

## What this actor does not do

Say this before the first run, not after:

* **No forward counts and no comment counts.** Telegram's public web preview publishes neither.
  Other actors list those fields and fill them with a guess; a wrong number is worse than a
  missing one.
* **No media download.** Media is reported as the CDN link Telegram hands out. Those links carry
  a token and expire, so fetch them within the same day if you need the file itself.
* **No groups.** The public preview covers channels only. A group comes back as
  `NOT_CHECKED_NOT_A_CHANNEL`, with its member count if Telegram shows one.
* **No private channels.** An invite link comes back as `NOT_CHECKED_PRIVATE_INVITE`.
* **No search across Telegram.** This reads channels you name. It does not find channels by
  keyword.
* **Views are exact only as far as Telegram publishes them.** Below one thousand the number is
  exact; above it Telegram rounds to three significant digits, and `viewsApproximate` says so.

## Input

```json
{
  "channels": ["durov", "@telegram", "https://t.me/s/ukrpravda_news"],
  "maxPostsPerChannel": 100,
  "sinceDate": "2026-11-01",
  "maxChannels": 20
}
```

`channels` accepts a bare username, `@username`, `https://t.me/<name>`, `t.me/s/<name>` or a
link to a single post (only the channel is taken). `maxPostsPerChannel` walks from the newest
post backwards, so the newest posts are the ones a limit keeps; `0` means walk until `sinceDate`
or the start of the channel. `sinceDate` is a UTC date floor and must be written `YYYY-MM-DD`: a
date the actor cannot read is refused before the first request, because ignoring it silently
would charge you for posts outside the window you asked for. `maxChannels` cuts the list, and the
cut lines still get their own row.

## Charging

One `message-delivered` event per post written to the dataset. The charge rides on the same call
that writes the row, so a post is never billed without being delivered. A post that hits the
spending cap keeps its identity (`channel`, `messageId`, `url`, `date`) with `text: null` and
status `NOT_CHECKED_CAP`: you see what the cap cut, and it costs nothing.

The platform sets its own default cap per run when you do not set one, which is roughly 3 to 4
dollars. For a long history export, set "Max cost per run" yourself.

## Statuses

`READ` posts delivered. `NOT_CHECKED_CHANNEL_NOT_FOUND` Telegram does not know the username.
`NOT_CHECKED_NOT_A_CHANNEL` it is a group or chat. `NOT_CHECKED_NO_PUBLIC_PREVIEW` the channel
exists but shows no public posts. `NOT_CHECKED_PRIVATE_INVITE` an invite link. `NOT_CHECKED_INVALID`
the line is not a Telegram username. `NOT_CHECKED_DUPLICATE` the same channel came earlier.
`NOT_CHECKED_LIMIT` beyond `maxChannels`. `NOT_CHECKED_EMPTY` the channel has posts, none inside
`sinceDate`. `NOT_CHECKED_SOURCE` Telegram refused or timed out. `NOT_CHECKED_TIME` the platform
deadline stopped the walk. `NOT_CHECKED_CAP` the spending cap was reached. `NOT_CHECKED_ERROR` the
dataset refused the row.

A message row can also carry `NOT_CHECKED_CHARGE_UNVERIFIED`: the write raised, and the dataset
count could neither confirm nor deny that the row landed. In that case the post is not repeated
as a second row, because a duplicate in your dataset is worse than a named uncertainty.

## Run summary

The key-value store keeps `RUN_SUMMARY-<runId>`, plus a plain `RUN_SUMMARY` copy in the same
store: the default store of a run belongs to that run alone, so the copy cannot be overwritten by
another run. Counters:

* channels: requested, unique, read, empty, unreadable, invalid, cut by `maxChannels`,
  duplicates, and `channelsMismatch` if they fail to add up;
* posts: seen, delivered, cut by the limit, older than `sinceDate`, cut by the cap, refused by
  the dataset, and `messagesChargeUnverified` for the writes that could not be settled;
* money: `chargedEvents` from the platform, `deliveredUncharged` for rows that reached you free,
  `rowsDelivered` for every row written, and `billingActive` with `billingReason`.

`byStatusChannels` and `byStatusMessages` keep the two kinds of status apart. If the numbers ever
disagree, the summary says so in `counterMismatch` instead of rounding it away.

## Politeness and limits

The actor reads one page (20 posts) per request with at least 0.7 seconds between requests, and
walks back through `?before=<id>`. On HTTP 429 it waits and retries, and after that it stops the
run with `rate-limited` rather than hammering Telegram. The default window is small enough for
the platform's own five minute check, so the actor does not fall under maintenance.

## Local development

```bash
python -m src.main            # reads APIFY_LOCAL_STORAGE_DIR/key_value_stores/default/INPUT.json
python tests/test_parse.py    # parser checks on real saved pages
python tests/test_main.py     # flow checks with a fake Actor that follows the SDK
```
