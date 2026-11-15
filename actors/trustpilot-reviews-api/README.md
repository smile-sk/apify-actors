# Trustpilot Reviews API

Company domains in, a row per Trustpilot review out. Rating, title, text, language, author and
country, publication and experience dates, the company's public reply with its date, the company's
TrustScore and its total review count, all from the public listing, without a Trustpilot account
and without a third-party key.

## What it really does about the 200 review wall

Every Trustpilot scraper hits the same thing: an anonymous visitor gets **10 pages of 20 reviews per
listing**, and page 11 is not an empty list - it is Trustpilot's login page (`/users/connect`), the
same wall wearing a different hat. Measured here on 2026-11-11, page by page, on a company with
50 290 reviews, both on a plain listing and on a star listing. The actor therefore never asks for page
11: past page 10 the reviews are not reachable without an account, and a login page is never sold as
an empty company.

One detail decides how deep a run can go, and it is subtle enough that two measurements of the same
site disagreed: **the first page of a star listing behaves differently depending on how the address is
written**. Measured here on 2026-11-11, same company, same minute:

| Address asked for | What came back |
| --- | --- |
| `?sort=recency` | 20 reviews, mixed ratings, `hasActiveFilters: false` |
| `?sort=recency&stars=1` | 20 reviews, every one rating 1, `hasActiveFilters: true` |
| `?sort=recency&stars=1&page=1` | 20 reviews, mixed ratings, `hasActiveFilters: false` |
| `?sort=recency&stars=1&page=2` | 20 reviews, every one rating 1 |
| `?sort=recency&stars=1&page=10` | 20 reviews, every one rating 1 |

So with an explicit `page=1` the filter is ignored, and without it the page is filtered. That is a
server quirk, not a rule to build a product on, so this actor never relies on it: it reads the newest
page once without any filter, and reads every star bucket from page 2, where the filter is confirmed
by two independent measurements. Every row from a bucket also passes one check before it is written:
if a star listing ever answers a rating it does not carry, the row is dropped and the run says so
instead of selling it.

That gives **20 + 5 x 9 x 20 = 920 reviews per company** when all five buckets are asked for, and 180
reviews when one bucket is asked for. 920 is what this actor promises and what `maxReviewsPerCompany`
allows. Skipping page 1 of every bucket costs you a hundred reviews, and it buys a promise that
cannot quietly turn into an unfiltered list.

Two consequences stated plainly, because a buyer deserves them in words:

- the twenty reviews from the newest page carry `starBucket: null` and `starSource: "newest"`, since
  no star filter was active for them;
- every other row carries its `starBucket` from 1 to 5 and `starSource: "star-filter"`, and never a
  rating other than its bucket.

## How it passes the wall

Not with a proxy and not with your key. Measured from our Frankfurt VPS on the same address:

| How the request was made | What Trustpilot answered |
| --- | --- |
| plain HTTP client with full Chrome headers | 403, a 991 byte "Verifying Connection" page |
| Chromium `--headless` | the wall passes, then a 430 byte "identified as coming from a bot" |
| **Chromium headful under Xvfb** | the real listing, 878 981 bytes, 50 289 reviews |

So the actor runs a real browser window on a virtual display inside its own container. Every run
reports `browserMode` in the summary: `headful` is the mode that works, and if a platform change ever
forces `headless`, the summary says so instead of pretending.

## Price and what is free

**$0.0004 per review delivered**, one charge per row that reached the dataset, and the lowest
spending limit you can set per run is $0.05. For comparison, the two leaders of this niche charge
$0.000575 and from $0.00065 per review plus a start fee, and neither of them is cheaper than this
actor for the same page.

Free, with the reason written into the row:

- a line that is not a domain, a repeated line, a company beyond `maxCompanies`;
- a `stars` value that is not a rating from 1 to 5, refused before one request is made;
- a domain Trustpilot has no profile for (HTTP 404);
- a bot wall or an HTTP error instead of the listing;
- a profile that is open but has no reviews in the pages asked;
- reviews that your own `publishedAfter` or `language` filter excluded;
- **reviews that were read and not delivered** because the run ran out of time, hit the spending cap
  or stopped after a charge error: each one comes back as its own row with the reason, never lost;
- a company the run never reached.

Nothing is charged for a review you cannot see in the dataset.

## Where the money goes when something fails

One SDK call writes the row and charges for it, so the two cannot drift apart. If the spending cap
arrives mid-batch, the SDK writes exactly the reviews it charged for and the rest come back as free
`NOT_CHECKED_CAP` rows: the buyer sees what they paid for and what they did not. If a charge cannot
be confirmed at all, the run stops.

That last case is split in two, because the two halves are different money:

- `deliveredUncharged`: the dataset counter shows this batch landed, so those rows are in the dataset
  and were not paid for. The buyer has them for free and the summary says so;
- `chargeUncertain`: the counter does not show them and they are not in the dataset either. The summary
  names the size of that batch, because repeating it could hand you two rows for one review.

## Input

- `companies`: domains, urls or profile addresses, one per line.
- `maxReviewsPerCompany`: 200 by default, 920 maximum (the measured ceiling of an anonymous run).
- `maxPagesPerCompany`: 10 by default, 10 maximum (that is the platform's own wall).
- `stars`: which star listings to read; empty means all five, `[1, 2]` reads the worst reviews only.
  The star filter takes effect from page 2 of the listing, so a filtered run starts there.
- `publishedAfter`: an ISO date; older reviews are read and dropped without a charge.
- `language`: a two letter code; other languages are read and dropped without a charge.
- `maxCompanies`: 5 by default, a fresh navigation per company.
- `includeReplies`: keep the company's public answer and its date.

## Summary

`companiesRequested` always equals `companiesAttempted` plus `companiesNotAttempted`, and that second
number counts the invalid lines, the duplicates, the lines beyond `maxCompanies` and the companies the
run never reached. Every attempted company ends in exactly one of `companiesRead`,
`companiesNotFound`, `companiesBlocked`, `companiesNoReviews`, `companiesFilteredOut`,
`companiesFailed`. `companiesPartial` marks a company whose reviews were read and then the listing
stopped answering. `reviewsDelivered` counts the rows written, `chargedEvents` is the money really
taken, `reviewsFiltered` and `reviewsLimited` were read and not charged, and `reviewsLimited` splits
into `reviewsCapped`, `reviewsStopped` and `reviewsUncertain`. `pagePlan` shows the pages the run
intended to read, so a shallow run is visible instead of silent. `reviewsBucketMismatch` counts reviews
that a star listing answered with a rating it does not carry: they are dropped, never charged, and the
company gets its own row naming the reason. `counterMismatch` is true when `reviewsUnique` does not
equal delivered plus filtered plus limited plus uncertain, or when the companies do not add up, so a
wrong count is never silent.

`stopReason` is empty on a complete run. Otherwise it names the real reason: `deadline` (the platform's
time budget ran out), `spending-cap` (the buyer's own ceiling), `charge-limit` (the hourly charge limit)
or `billing-price-unknown` (the event price could not be read: nothing is delivered and nothing is
charged).

## What this actor does not do

- It does not create, edit or delete anything on Trustpilot. It reads public pages.
- It does not log in, does not use your account, and does not touch private data.
- It does not promise "unlimited": the measured ceiling is 920 reviews per company per run, and page
  11 of any listing sends the reader to Trustpilot's login page instead of a list.
- It does not read the first page of a star listing, because that page ignores the star filter when
  the address carries `page=1`: the filter is confirmed from page 2 on.
- It does not read reviews older than what those pages hold in one run; for history, run it on a
  schedule with `publishedAfter`.
- It does not translate, summarise or score the reviews. The text is delivered as written.
- It does not bypass a captcha by solving it: it starts a real browser window, which is what a
  normal visitor has.
