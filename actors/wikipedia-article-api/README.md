# Wikipedia API - Article Text, Summary, Categories & Pageviews

Read Wikipedia articles through the official Wikimedia APIs, not through the page HTML.
Send titles, article URLs or `lang:title` lines and get one row per article with its text,
categories, pageviews and the size of its Wikidata item.

Open source, no key, no browser, no proxy: the MediaWiki Action API, the Wikimedia
pageviews metric API and the Wikidata EntityData endpoint all answer a plain HTTP client.

## What you get per article

| field | meaning |
|---|---|
| `title`, `lang`, `url`, `pageId` | the article as the wiki sees it |
| `description` | the short description stored in the page properties |
| `wikidataId` | the Q-id of the item this article is attached to |
| `text` | the lead section, or the whole article text with `includeFullText` |
| `wordCount` | words in the text that was returned; every CJK character counts as one word |
| `categories`, `categoryCount` | every visible category, hidden maintenance ones skipped; `categoriesTruncated` says if the wiki cut the list |
| `pageviews`, `pageviewsPerDay`, `pageviewsDaily` | views from the official metric API over the window you ask for |
| `sitelinkCount`, `claimCount` | how many language editions and how many Wikidata statements the item has |
| `pageLengthBytes`, `lastEdited`, `lastEditor` | the size and the last revision |
| `redirectedFrom` | the title you sent, when the wiki redirected it |
| `actorInput`, `actorIndex`, `status`, `billable`, `source` | which line it came from, and whether it was sold |

## What it does not do

- It does not scrape the rendered page, so it never breaks on a layout change, and it does
  not read the "References", "See also" sections or the infobox as HTML.
- It does not translate, summarise or rewrite the text: it returns what Wikipedia stores.
- It does not read other wikis that are not Wikipedia projects, and it does not read a
  MediaWiki installed on some other domain.
- It does not resolve a topic. If you send an ambiguous word you get the page the wiki
  serves for that title, which may be a disambiguation page. The row says so in `status`
  of the wiki page itself, not in this actor: such a page exists, so it is billable.
- It does not give exact live view counts: pageviews are the official daily series over the
  window you ask for, not a real-time counter.

## Input

```json
{
  "articles": [
    "Python (programming language)",
    "https://de.wikipedia.org/wiki/Berlin",
    "fr:Paris"
  ],
  "lang": "en",
  "includePageviews": true,
  "includeCategories": true,
  "includeFullText": false,
  "pageviewsDays": 30,
  "maxArticles": 200
}
```

Every line may be a bare title, a full article URL (the language comes from the host) or a
`lang:title` line. `lang` is only the default used when the line carries none. A title in a
non-Latin script is fine: send it as a plain title, or as a percent-encoded URL.

`maxArticles` caps how many lines a run reads (default and maximum 200). A line past the cap
comes back as a free `NOT_CHECKED_CAP` row that names the cap, so a huge list never turns
into a silent cut.

## Money

You pay for one thing: an article that was read and delivered. The event is
`article-delivered`, and the price is on the card.

Free, with a status and a plain reason, and with no field you could mistake for data:

| status | when |
|---|---|
| `INVALID_INPUT` | the line is not a title, URL or `lang:title` line |
| `DUPLICATE` | the same article was already sent in this run (case and underscores do not matter) |
| `NOT_FOUND` | no article at this title: the page does not exist, or the title lives in another namespace (`Category:`, `File:`, `Special:`), or it is a special page |
| `NOT_CHECKED_ERROR` | the source could not be read: a timeout, a 5xx, a body that is not JSON |
| `NOT_CHECKED_CAP` | the spending limit on the run ended before this article |
| `NOT_CHECKED_BILLING` | the price list does not carry `article-delivered`, so nothing can be sold |
| `NOT_CHECKED_TIME` | the run had less than one article's worth of seconds left |

A missing article and an unreadable source are **different** statuses. A page that simply
does not exist is a verdict about your input and costs nothing. A 503 from the wiki is our
problem or theirs, and it also costs nothing, but it never pretends the article is absent.

The first row of a run with no articles at all is a failed run with an example in the
message, not a green run with zero rows.

A run is also refused when nothing could be read at all: every article that reached
Wikipedia failed and not one row was delivered. That message is about the source, never
about your titles, and it names the stop reason and the pricing state. A title that simply
does not exist is a free `NOT_FOUND` row and never fails a run.

If the platform charges for fewer rows than were written, the rest is delivered free, the
counters say so in `deliveredUncharged`, and the run stops instead of continuing to give
away the list.

## Run summary

`RUN_SUMMARY` in the run's key-value store, plus a copy under
`RUN_SUMMARY-<runId>` in the named store `wikipedia-article-summary`, so two runs at the
same time do not overwrite each other's counters. The output card links the bare
`RUN_SUMMARY` in the default store, because a run-scoped key only exists in the named one.

Status rows are written in batches of up to 100, so they may come after the paid rows:
sort the dataset by `actorIndex` to get your input order back.

Counters: `requested`, `attempted`, `delivered`, `rowsWritten`, `chargedEvents`,
`deliveredUncharged`, `deliveredUnverified`, `chargeUncertain`, `writeErrors`,
`cappedNotCharged`, `articlesNotFound`, `articlesInvalid`, `articlesDuplicate`,
`cutByDeadline`, `cutByStop`, `cutByLimit`, `byStatus`, `counterMismatch`, `stopReason`,
`datasetNote`, `firstFailure`, `billingActive`, `priceNote`, `source`.

`counterMismatch` is true when the input lines do not add up to the statuses counted. It is
there so that a lost line shows up by itself instead of looking like a short input.

If a paid write fails, the answer is never "nothing was delivered". The dataset counter is
read before and after, and the three outcomes are named separately: the rows landed and the
charge failed (`chargeUncertain`), the write was refused, or the counter lags and both
delivery and the charge are unknown. In every case `datasetNote` says what is known and
what is not, and no second row is written.

## Source and licence

Wikipedia text is CC BY-SA 4.0 and Wikidata is CC0. This actor reads them and hands you the
fields; if you republish the text, keep the attribution that the licence requires. Every row
carries the article `url`, which is the attribution the licence asks for.

## Notes

- A page that exists but is a redirect comes back resolved, with `redirectedFrom` naming
  what you sent.
- One article costs three requests (the page, the pageviews series and the Wikidata item),
  and a measured run takes about 1 s per article with pageviews on, about 0.4 s with them
  off. The remaining run time is divided between those three requests, so a slow source
  ends in a free `NOT_CHECKED_TIME` row instead of a killed run with no summary.
- `includeFullText` really returns the whole article: the request simply omits the intro
  flag that MediaWiki reads as "lead section only".
