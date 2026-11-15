# SEO Audit API - on-page score, issues and fixes

Opens a page in a real Chromium and turns what it finds into a number you can act on:
a score from 0 to 100, and a list of issues where each one carries a severity, the
points it took away, and the sentence that says what to change.

One row per page. Payment is per audited page, and a page that could not be read is
free with a reason a human can act on.

## Why this exists

Two things are wrong with the SEO checkers that are already on the store, and both of
them are visible from the outside:

1. Most of them fetch raw html with an http client. On a site that renders with
   javascript, that returns an empty shell, and the shell scores as a page with no
   title and no text. The report looks precise and says nothing true.
2. Many charge per page whether or not the page was read. A 404, a pdf, a timeout and
   a real audit can land on the same invoice.

This actor opens every url in its own browser, reads the page as a visitor sees it,
and never charges for a row it could not fill.

## What the score means

The score starts at 100 and every triggered check takes its own penalty away. Nothing
is hidden: add the `points` of the issues in the row, subtract the sum from 100, and
you get the score back. Grades: `good` at 80 and above, `needs-improvement` from 50 to
79, `poor` below 50.

| check | severity | points | what it means | what to do |
|---|---|---|---|---|
| `title-missing` | critical | 20 | The page has no title tag. | Add a unique title of 30 to 65 characters; it is the line a search result shows. |
| `title-length` | warning | 6 | The title is outside the 30 to 65 character window. | Rewrite the title so the whole line is shown instead of being cut with an ellipsis. |
| `description-missing` | critical | 12 | The page has no meta description. | Add a meta description of 70 to 165 characters; the search engine may use it as the snippet. |
| `description-length` | warning | 5 | The meta description is outside the 70 to 165 character window. | Shorten or expand it so it is not cut in the snippet. |
| `h1-missing` | critical | 12 | The page has no h1. | Add one h1 that names what the page is about. |
| `h1-multiple` | warning | 5 | The page has more than one h1. | Keep one h1 and move the rest to h2. |
| `canonical-missing` | warning | 5 | The page does not declare a canonical url. | Add rel=canonical with the preferred url so duplicates do not split the signals. |
| `noindex` | critical | 25 | The page tells search engines not to index it. | Remove noindex from the robots meta tag if this page should be found. |
| `lang-missing` | notice | 4 | The html tag does not declare a language. | Add lang to the html tag, for example lang=en. |
| `viewport-missing` | warning | 8 | The page has no viewport meta tag. | Add the viewport meta tag so the page scales on a phone. |
| `images-alt` | warning | 8 | More than a fifth of the images have no alt text. | Describe each meaningful image in alt; leave alt empty only for decoration. |
| `images-alt-few` | notice | 4 | Some images have no alt text. | Describe each meaningful image in alt; leave alt empty only for decoration. |
| `structured-data-missing` | notice | 3 | The page carries no JSON-LD structured data. | Add JSON-LD for the type that fits the page, for example Article or Product. |
| `structured-data-invalid` | warning | 6 | A JSON-LD block on the page is not valid JSON. | Fix the JSON inside the script tag; a broken block is ignored by search engines. |
| `social-missing` | warning | 5 | The page has no Open Graph tags. | Add og:title, og:description and og:image so shared links show a card. |
| `twitter-missing` | notice | 2 | The page has no Twitter card tags. | Add twitter:card and twitter:title if links to this page are shared on social media. |
| `thin-content` | warning | 8 | The page has very little readable text. | Add the text a visitor needs; a page with almost no words has little to rank on. |
| `no-internal-links` | notice | 4 | The page links to no other page of the same site. | Link to related pages so a crawler can reach them from here. |
| `empty-anchors` | notice | 2 | Some links have no text and no image. | Give each link text or an image, otherwise its destination is not described. |
| `meta-robots-nofollow` | notice | 3 | The page tells search engines not to follow its links. | Remove nofollow from the robots meta tag unless you really want the links ignored. |

Total penalty available: 147 points, so a page with everything wrong scores 0.

Title window 30 to 65 characters, meta description window
70 to 165, thin content below 150 words.
These are published ranges where a search result shows the whole line instead of
cutting it; being outside them costs points but is never called critical.

## What is free, and why

Every one of these comes back as its own row with `billable: false` and a reason:

- `NOT_CHECKED_INVALID` - the line is not an http url.
- `NOT_CHECKED_DUPLICATE` - the same url came earlier in the list.
- `NOT_CHECKED_LIMIT` - the url is beyond `maxPages`.
- `NOT_CHECKED_PRIVATE_ADDRESS` - a host name answered with a non public address.
- `NOT_CHECKED_DNS_ERROR` - the host name does not resolve, or is not a valid name.
- `NOT_CHECKED_TIMEOUT` - the page did not finish loading in `pageTimeoutSecs`.
- `NOT_CHECKED_HTTP_ERROR` - the page answered 4xx or 5xx.
- `NOT_CHECKED_NOT_HTML` - the response is a pdf, an image or something that is not a page.
- `NOT_CHECKED_PROBE_ERROR` - Chromium could not read the page.
- `NOT_CHECKED_TIME`, `NOT_CHECKED_CAP`, `NOT_CHECKED_BILLING`, `NOT_CHECKED_ERROR` -
  the run stopped before this page; the summary says why.

A redirect into the private network, and a page that loads something from a private
address, are recognised after the browser has already asked for it, and such a page is
never charged. This is written plainly instead of promising that the request was
stopped: a route handler in this browser does not see every hop. A host name that does
not resolve at all is not a private address, and such a page is audited as usual.

## Input

- `urls` - one page url per line.
- `maxPages` - default 10; urls beyond it are free.
- `strategy` - `mobile` (412 px, phone user agent) or `desktop` (1440 px).
- `settleSecs` - how long the page keeps running after load before the signals are read.
- `pageTimeoutSecs` - default 45.
- `ignoreHttpsErrors` - for a staging site with a self signed certificate.

## Output

One row per page, in the default dataset. The fields that matter most:

Real row from a local run against example.com, not an illustration:

```json
{
  "url": "https://example.com/",
  "status": "OK",
  "billable": true,
  "title": "Example Domain",
  "titleLength": 14,
  "metaDescription": null,
  "h1Count": 0,
  "canonical": null,
  "noindex": false,
  "images": 0,
  "internalLinks": 0,
  "wordCount": 125,
  "seoScore": 43,
  "seoGrade": "poor",
  "issues": [
    {
      "code": "description-missing",
      "severity": "critical",
      "points": 12,
      "message": "The page has no meta description.",
      "fix": "Add a meta description of 70 to 165 characters; the search engine may use it as the snippet."
    },
    {
      "code": "h1-missing",
      "severity": "critical",
      "points": 12,
      "message": "The page has no h1.",
      "fix": "Add one h1 that names what the page is about."
    },
    {
      "code": "thin-content",
      "severity": "warning",
      "points": 8,
      "message": "The page has very little readable text.",
      "fix": "Add the text a visitor needs; a page with almost no words has little to rank on."
    },
    {
      "code": "title-length",
      "severity": "warning",
      "points": 6,
      "message": "The title is outside the 30 to 65 character window.",
      "fix": "Rewrite the title so the whole line is shown instead of being cut with an ellipsis."
    },
    {
      "code": "canonical-missing",
      "severity": "warning",
      "points": 5,
      "message": "The page does not declare a canonical url.",
      "fix": "Add rel=canonical with the preferred url so duplicates do not split the signals."
    },
    {
      "code": "social-missing",
      "severity": "warning",
      "points": 5,
      "message": "The page has no Open Graph tags.",
      "fix": "Add og:title, og:description and og:image so shared links show a card."
    },
    {
      "code": "no-internal-links",
      "severity": "notice",
      "points": 4,
      "message": "The page links to no other page of the same site.",
      "fix": "Link to related pages so a crawler can reach them from here."
    },
    {
      "code": "structured-data-missing",
      "severity": "notice",
      "points": 3,
      "message": "The page carries no JSON-LD structured data.",
      "fix": "Add JSON-LD for the type that fits the page, for example Article or Product."
    },
    {
      "code": "twitter-missing",
      "severity": "notice",
      "points": 2,
      "message": "The page has no Twitter card tags.",
      "fix": "Add twitter:card and twitter:title if links to this page are shared on social media."
    }
  ]
}
```

The run summary is written to the named store `seo-audit-api-summary` under
`RUN_SUMMARY-<runId>`, and to the run's own store under `RUN_SUMMARY`. It carries the
counters, `byGrade`, `duplicateTitles` (the same title on two pages of one run is a
problem a single page cannot see), the address guard counters and the stop reason.

## Price

Charged per audited page, event `page-audited`, only when the row carries an audit.
The price itself is set on the actor page and is not repeated here, so this file cannot
drift away from the card.

## What this actor does not do

Said plainly, because an audit tool that implies everything is worse than one that
names its edges:

- It does not crawl a site. It audits the pages you give it.
- It does not read backlinks, domain authority or traffic.
- It does not call Google, does not use a Google key and does not guess search positions.
- It does not measure Core Web Vitals; the measurements are of the page as a document,
  not of its speed.
- It does not see inside an iframe.

## Limits and run time

- One browser for the whole run, warmed once, so the cold start is not charged to the
  first page.
- The deadline is watched before each page: if less time is left than one page needs in
  the worst case, the rest comes back free with `NOT_CHECKED_TIME` instead of the
  platform killing the run mid page.
- `maxPages` defaults to 10 and the summary reports the limit actually applied.
- The subresource host list is capped at 25 names for the address guard, and the row
  says when it was cut (`resourceHostsTruncated`).
