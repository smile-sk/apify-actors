# Substack Posts API - Newsletter Archive, Likes and Authors

A publication that does not exist should never look like a paid row. Send a Substack host
or subdomain, get its posts back, and a host that never answered comes back free with the
reason instead of an empty row and a charge.

## What it returns

One row per post:

* `publication`, `postId`, `title`, `subtitle`, `url`, `publishedAt`, `authors` (a list of
  byline names), `section`, `type`, `audience`, `likes` (`reaction_count` as the archive
  reports it), `comments`, `wordCount`, `language`, `coverImage`, `description`;
* `previewText` when `includeBodyText` is on: the public preview the archive returns for
  every post. The full body is not in the archive answer and is not sold.

Every other row is a status row: `actorInput`, `actorIndex`, `status`, `reason`,
`billable: false`, and no post fields at all. A free row cannot be mistaken for data.

## Input

`publications` is a list: a custom domain such as `www.thefp.com`, a full url, or a bare
subdomain such as `platformer`, which becomes `platformer.substack.com`. `maxPostsPerPublication`
is 1 to 500 and defaults to 50. `includeBodyText` adds the preview text.

## What is free and what is charged

Charged once per post that was actually delivered:

* a publication the source does not know (HTTP 404) comes back `NOT_FOUND` and free. A
  custom domain that answers 404 is tried once as `<label>.substack.com` before that
  verdict, and a redirect is followed, because a copied address is often not the address
  the archive lives on. A redirect that leaves public https, or points to a path only,
  closes that road and the substack road is tried instead, so it is never called a
  network error;
* a host that does not resolve, a timeout, or a refusal (401, 403, 429, 5xx) comes back
  `NOT_CHECKED_ERROR` with the reason in words, and free;
* a host that is not a host at all (`not a host`) comes back `INVALID_INPUT` and free;
* the same publication sent twice, or the same post seen twice, comes back `DUPLICATE`
  and free. A Substack post id is unique across the platform, so a post that appears in
  two of the publications of one run is delivered once;
* a publication that published nothing is a successful run with zero rows sold, not a
  failure: it gets one free `EMPTY` row saying that the archive answered normally and holds
  no post, and the run summary counts it in `publicationsEmpty`.

When the run is stopped by the clock, by the spending limit or by an unreadable price,
the remaining publications get a status row naming which of those stopped the run, and they
are free. Posts past `maxPostsPerPublication` are simply not read: nothing is claimed about
them.

## Money, honestly

The price is per delivered post; see the Price section of the card. The lowest spending
limit you can set per run is $0.05, and a run of 20 posts stays well under it.

The archive answer is what it is: the archive gives a preview of the body, not the whole
post. If a post arrives without an id it is not sold; the row says so and is free.

## What this actor does not do

* It does not read the full text of an article: the archive endpoint does not carry it.
* It does not read paid-only posts behind a subscription: it sees what a visitor sees.
* It does not search Substack for a topic. It reads the archive of the publications you
  name, and nothing else.

## Source

Substack's own public archive endpoint, `https://<publication>/api/v1/archive`. No key, no
account, no proxy, and no browser: one GET per page, 25 posts per page, newest first. The
endpoint answers HTTP 400 to a bigger `limit`, so 25 is the page and a default of 50 posts
is read as two pages.

## Notes on counters

The run summary reports `requested`, `publicationsRead`, `publicationsFailed`,
`publicationsNotFound`, `publicationsEmpty`, `postsSeen`, `postsDelivered`, `rowsWritten`,
`chargedEvents`, `deliveredUncharged`, `deliveredUnverified`, `chargeUncertain`,
`writeErrors`, `byStatus`, `counterMismatch`, `billingActive`, `stopReason`, `datasetNote`
and `firstFailure`.
The summary lands in the default key-value store twice: as `RUN_SUMMARY-<runId>` and as
`RUN_SUMMARY`.

A paid write and its charge happen in one call. If that call raises, the dataset counter is
read again (three times, taking the maximum) to tell a refused write from a failed charge.
When the counter cannot be read, the summary says that delivery and the charge are both
unknown instead of claiming that nothing happened.
