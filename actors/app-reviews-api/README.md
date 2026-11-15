# App Reviews API - iOS App Store and Google Play in one run

The shelf sells you two actors: one for the App Store, one for Google Play, and you glue
them yourself. Half of them stop at a hidden page cap, and the weaker ones still bill a run
that answers with nothing. This actor reads both stores from their own open feeds, keeps the
developer reply, tells you which storefront every review came from, and charges only for a
review that reached your dataset.

No API key, no proxy, no browser, no login. Both feeds are public, which is why the price
can be lower than the shelf and why a wall or a 404 costs you nothing.

## What you get

One row per review, both platforms in one list:

| field | meaning |
| --- | --- |
| `platform` | `ios` or `android` |
| `appId` | the App Store id or the Play package |
| `appTitle`, `appRating`, `appRatingsTotal` | the app itself, from the store |
| `country` | the storefront the review was read from |
| `reviewId`, `rating`, `title`, `text`, `author`, `version`, `publishedDate`, `thumbsUp` | the review |
| `developerReply`, `developerReplyDate` | the public answer, Google Play only |
| `url` | the review link on Play, the app review page on iOS (Apple has no per review permalink) |
| `source` | `apple-rss` or `google-play` |
| `status`, `reason`, `billable` | the outcome and why |

## Input

- `apps`: Play packages (`com.instagram.android`), App Store ids (`389801252`) or store links.
- `countries`: storefront codes (`us`, `de`, `cn`). Apple gives 50 reviews a page for 10 pages
  per storefront, so depth comes from storefronts. Google Play uses the first code.
- `maxReviewsPerApp` (default 200), `maxApps` (default 5), `maxPagesPerCountry` (default 10).
- `minRating`, `maxRating`, `publishedAfter` for incremental or complaint-only runs.
- `includeReplies` (default on).

## Price

$0.09 per 1,000 reviews, event `review-delivered`: $0.00009 per review, one charge per row
that reached the dataset. The lowest spending limit you can set per run is $0.05. The top of this shelf sells at
$0.10 per 1,000 with one store per actor.

A review that is read but not delivered is never dropped in silence: when the run stops for
time, for the spending cap or after a charge error, each read review comes back as its own
free row that says `read but not delivered, not charged`.

`maxReviewsPerApp` is part of the price, not a suggestion: a review past your limit comes
back as a free `NOT_CHECKED_LIMIT` row with its input and the reason, without the review
text, so the limit cannot be used to take the whole feed for one event. An empty `apps`
list fails the run with a message instead of returning a green run with an empty dataset,
because that would read as "this app has no reviews".

## What is free, with the reason in the row

| status | when | money |
| --- | --- | --- |
| `NOT_CHECKED_INVALID` | the line is not an app id, a package or a store link, or a storefront code is wrong | 0 |
| `NOT_CHECKED_DUPLICATE` | the same app came earlier in the run | 0 |
| `NOT_CHECKED_LIMIT` | the app is beyond `maxApps`, or a review is beyond `maxReviewsPerApp` (no review text in that row) | 0 |
| `NOT_CHECKED_NOT_FOUND` | the store does not know the app | 0 |
| `NOT_CHECKED_BLOCKED_PAGE` | a wall answered instead of the feed | 0 |
| `NOT_CHECKED_HTTP_ERROR` | 4xx or 5xx other than not found | 0 |
| `NO_REVIEWS` | the app is open and the store carried nothing | 0 |
| `FILTERED_OUT` | reviews were read and your own filters removed all of them | 0 |
| `NOT_CHECKED_TIME` | the run ran out of its budget before this app | 0 |
| `NOT_CHECKED_CAP` | the spending cap was reached | 0 |
| `NOT_CHECKED_BILLING` | the price could not be read | 0 |
| `NOT_CHECKED_ERROR` | the feed could not be read at all | 0 |

## Boundaries, named instead of hidden

1. Apple serves 10 pages of 50 reviews per storefront per run: page 11 answers HTTP 400
   (measured 2026-11-11). That is 500 reviews per storefront; depth comes from storefronts,
   not from paging. This actor does not sell "unlimited".
2. Google Play is deep and continues by continuation token, 200 reviews a page.
3. A developer reply exists only on Google Play. Apple's feed carries no reply field, so
   `developerReply` is `null` for iOS rather than "nobody answered".
4. The same review seen in two storefronts is charged once: rows are deduplicated by
   (`platform`, `reviewId`) across the whole run.
5. App charts, keyword ranks and store search are not sold by this actor.
6. A review with no date at all is dropped without a charge when `publishedAfter` is set:
   the window cannot be checked, so it is not sold.
7. When the dataset itself refuses rows, the run stops and fails after three refusals in a
   row: counting rows the buyer never receives would be a false success.

## What this actor does not do

- It does not sign in, it does not post reviews and it does not answer reviews.
- It does not translate reviews: the text is exactly what the store served.
- It does not sell store search, category charts or keyword ranking.
- It does not promise a review count per app: the count is whatever the feeds carry.
- It does not use a proxy or a browser, so a storefront that starts to wall its feed comes
  back as a free `NOT_CHECKED_BLOCKED_PAGE` row instead of a charged empty answer.

## The shelf, measured 2026-11-11

- "app store reviews": 79 actors by relevant title, 64 165 runs in 30 days, 1 440 live users.
- "google play reviews": 85 actors, 70 223 runs, 1 378 live users.
- Top: `thewolves/appstore-reviews-scraper` 38 456 runs, 677 live, 4.98 stars on 10 reviews;
  `thewolves/google-play-reviews-scraper` 29 857 runs, 519 live, 4.0 stars on 8;
  `neatrat/google-play-store-reviews-scraper` 20 316 runs, 438 live.
- Pain, verbatim from 1-2 star reviews: "invalid reviews. Scam API", "kept spewing out the
  same incorrect reviews", "Failed to run", and "Why is there no China mainland as an option
  for country? There is only the option of Hong Kong here."

## Default input

```json
{
  "apps": ["com.instagram.android", "389801252"],
  "countries": ["us"],
  "maxReviewsPerApp": 200
}
```
