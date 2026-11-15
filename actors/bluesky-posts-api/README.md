# Bluesky Posts API

Read the public posts of any Bluesky profile: text, publish time, likes, reposts,
replies, quotes, languages, the author and a link to the post. No API key, no login,
no browser and no proxy.

## What this actor does

1. Takes a list of profiles: handles (`bsky.app`), dids or full profile urls
   (`https://bsky.app/profile/bsky.app`).
2. Reads each profile's public author feed, newest first, 50 posts per request.
3. Writes one row per post into the dataset, with the profile's follower and post counts
   attached, so a buyer can filter by reach without a second call.
4. Everything that was not delivered comes back as a row with a status and a reason,
   and costs nothing.

## What this actor does not do

- It does not search. The public search endpoint needs a session and answers 403 without
  one, so search is not offered at all rather than promised and broken.
- It does not read a whole network graph: profiles come from the input, not from follows.
- Reposts and replies are off by default. A repost is somebody else's post shown on this
  profile and a reply is a post written to somebody else. `includeReposts` filters them
  from the rows; `includeReplies` also changes the **request**, because the feed endpoint
  drops replies at the source unless it is asked otherwise (`posts_no_replies` versus
  `posts_with_replies`). Measured 2026-11-12: 0 replies in 50 items without the flag, 22
  on `jay.bsky.team` and 18 on `bsky.app` with it. `feedKind` and `isReply` report the
  kind either way.
- It does not resolve deleted posts: if the source does not return a post, it is not
  invented.
- A profile whose feed is closed or empty gets a free status row, not a paid empty row.

## Money

- Price: **$0.0003 per delivered post** (`post-delivered`), the lowest spending limit you can set per run is $0.05.
- You pay only for a row that lands in the dataset with text or media. A row with a
  status other than `OK` is free, and it carries no post id, no text, no author and no url.
- A post without text and without media (a like-only feed item) is not delivered and not
  charged.
- If the price cannot be read, the run stops before the first payable post: nothing is
  delivered and nothing is charged, and each remaining line says why.
- If the spending cap or the remaining budget ends, the rows beyond it are returned free
  with `NOT_CHECKED_CAP`, and `stopReason` says `spending-cap`.
- `maxPostsPerActor` is a paid boundary, not a free sample: when this profile's limit is
  reached, one free `NOT_CHECKED_LIMIT` row says so and carries no post data. Nothing
  beyond the limit is read.
- The same profile sent twice (as a handle, a did or a url) is read once, and the second
  line gets a free `DUPLICATE` row.

## What was measured against the live source (2026-11-12)

- `public.api.bsky.app/xrpc/app.bsky.actor.getProfile` answers 200 without auth and
  returns handle, did, displayName, description, followersCount, followsCount, postsCount.
- `app.bsky.feed.getAuthorFeed` answers 200 with **50 items and a cursor**; the cursor is a
  timestamp, and paging continues until the feed ends.
- A feed item is `{"post": {...}}`; the post carries `uri`, `cid`, `indexedAt`,
  `likeCount`, `repostCount`, `replyCount`, `quoteCount`, `bookmarkCount`, `embed`,
  `labels` and `author`.
- Text lives in `post.record.text`, the publish moment in `post.record.createdAt`, the
  indexed moment in `post.indexedAt`, the languages in `post.record.langs`.
- Reposts and pins arrive as `item.reason` (`#reasonRepost`, `#reasonPin`) with the
  original post inside.
- It answers from a datacenter, so no proxy is needed.

## Status codes

| Status | Meaning | Charged |
| --- | --- | --- |
| `OK` | the post was delivered | yes |
| `INVALID_INPUT` | not a handle, did or profile url | no |
| `DUPLICATE` | the same profile was already sent in this run, in any form | no |
| `NO_POSTS_FOUND` | the profile is open and its feed returned no posts | no |
| `NOT_CHECKED_LIMIT` | this profile reached `maxPostsPerActor`; more posts may exist | no |
| `NOT_CHECKED_TIME` | the platform deadline came first | no |
| `NOT_CHECKED_ERROR` | the profile or feed could not be read | no |
| `NOT_CHECKED_CAP` | the spending cap or budget ended | no |
| `NOT_CHECKED_BILLING` | the price could not be read | no |
| `NOT_CHECKED_CHARGE_UNVERIFIED` | delivered, but the charge was not confirmed | not claimed |

## Stop reasons

`spending-cap`, `billing-price-unknown`, `charge-error`, `dataset-write-error`, `deadline`.

## Output

- Dataset: one row per post, see `.actor/output_schema.json` for the field list.
- `postsLimited` counts profiles that hit `maxPostsPerActor`, not posts that were skipped:
  how many posts lie beyond the limit is not measurable without reading them.
- Run summary: `RUN_SUMMARY` in the run's default key-value store, and the same document
  under `RUN_SUMMARY-<runId>` in the named store `bluesky-posts-summary`.

## Memory

512 MB. There is no browser in the image: the source is plain JSON over HTTPS, and 512 MB
has been enough for every measured run.

## Local run

```bash
python -m src.main
```
