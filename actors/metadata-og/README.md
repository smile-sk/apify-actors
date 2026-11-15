# Open Graph & Meta Tags API - Link Preview, OG Image

A link preview that shows nothing is almost always one of three things: the page has no
Open Graph block, its `og:image` is `/img/cover.png` instead of a full URL, or the page is
rendered in the browser and ships an empty HTML shell. This actor reads the page the way a
preview bot does and returns what a preview needs, with every URL already absolute, so the
row can be rendered as it is.

## What you get for each page

- `title` - `og:title` when declared, otherwise `<title>`, otherwise `twitter:title`;
- `description` - `og:description`, otherwise `<meta name="description">`, otherwise the Twitter one;
- `openGraph` - title, description, image, imageAlt, type, siteName, url, locale, video;
- `twitterCard` - card, title, description, image, site;
- `canonical`, `lang`, and `charset` (the encoding the page was really decoded with);
- `icons` - favicon, apple-touch-icon and manifest, absolute;
- `jsonLd` - how many `application/ld+json` blocks the page carries and their `@type` values,
  including `@graph` children;
- `hreflang` - language alternates, absolute;
- `preview` - `ready` plus the exact `issues`: `no_title`, `no_description`, `no_image`,
  `no_open_graph`, `image_was_relative`, `title_longer_than_60`, `description_longer_than_160`.

`image_was_relative` is the useful one: it means the value on the page was relative and this
actor resolved it, which is exactly the bug that makes a preview blank.

## Input

```json
{
  "urls": ["https://github.com/apify", "theverge.com", "https://www.wikipedia.org/"],
  "maxUrls": 100,
  "timeoutSecs": 20,
  "maxRedirects": 5,
  "includeJsonLd": true
}
```

`urls` takes a list or one string with newlines. `maxUrls: 0` means no limit.

## Output

One row per input line, in input order:

```json
{
  "index": 1,
  "source": "https://github.com/apify",
  "status": "EXTRACTED",
  "title": "Apify: Full-stack platform for web scraping, data extraction and AI agents",
  "description": "...",
  "canonical": "https://apify.com/",
  "lang": "en",
  "charset": "utf-8",
  "openGraph": {"image": "https://apify.com/img/og-image.png", "siteName": "Apify", "type": "website"},
  "twitterCard": {"card": "summary_large_image"},
  "icons": {"favicon": "https://apify.com/favicon.ico"},
  "jsonLd": {"count": 2, "types": ["Organization", "WebSite"]},
  "preview": {"ready": true, "issues": []},
  "billable": true
}
```

## Price

You pay **$0.002 per `metadata-extracted` event**, that is per page whose metadata was
delivered. Nothing else is charged by this actor:

- a dead link, a 404, a 403, a TLS failure, a timeout and a non-HTML body cost nothing;
- a redirect that was not followed (`REDIRECT`: `maxRedirects` is 0, or the answer names no
  `Location`) costs nothing - the redirect itself is the answer, not a page;
- a page that opened but has no metadata at all (`NO_METADATA`, a browser-rendered shell)
  costs nothing - there was no data to sell;
- the same page sent twice is delivered once and the repeat is free.

Apify also bills its own default dataset item event (about $0.00001 per row) and its actor
start event, as it does for every actor on the platform.

**Spending cap.** If the buyer does not set one, the platform applies its own default (about
$4.4 on our runs, `isMaxTotalChargeUsdSetByUser=false`). At $0.002 per page that is roughly
2300 pages. For a longer list, raise *Max total charge* before the run; pages that the cap cut
come back as `NOT_CHECKED_CAP` with the reason, never as silent gaps.

## Statuses

| status | meaning | charged |
|---|---|---|
| `EXTRACTED` | metadata delivered | yes |
| `NO_METADATA` | page opened, nothing to show (browser-rendered) | no |
| `HTTP_ERROR` | 4xx or 5xx answer | no |
| `NOT_HTML` | the link returned JSON, an image, a PDF, a file | no |
| `REDIRECT` | a 3xx that was not followed: `maxRedirects` is 0, or the answer names no target | no |
| `UNREACHABLE` | DNS, TLS, timeout, connection, redirect loop | no |
| `INVALID_URL` | not an http(s) address | no |
| `PRIVATE_TARGET` | the host resolves to a private or loopback address | no |
| `NOT_CHECKED_LIMIT` | beyond `maxUrls` | no |
| `NOT_CHECKED_CAP` | the spending cap stopped the run | no |
| `NOT_CHECKED_BILLING` | two billing failures stopped the run | no |
| `NOT_CHECKED_ERROR` | the dataset refused the row | no |

## Run summary

`requested`, `processed`, `delivered`, `rowsDelivered`, `notChecked`, `chargedEvents`,
`deliveredUncharged`, `billingActive`, `byStatus`, `duplicatesSkipped`, `cutByMaxUrls`,
`maxUrls`, `metadataMissing`, `stopReason`, `chargeErrors`, `datasetItemCount`,
`datasetItemCountApi`, `datasetItemCountLag`.

It is written as `RUN_SUMMARY` in the named store `open-graph-meta-tags-summary` and also as
`RUN_SUMMARY-<runId>`, because a named store is shared by the whole account: two runs at the
same time would otherwise overwrite each other's summary.

## What this actor does not do

- **It does not run JavaScript.** A page whose metadata is injected in the browser comes back
  `NO_METADATA` and costs nothing. Use a browser-rendering actor when you need those.
- **It does not check that the image really loads, or its size.** It returns the URL the page
  declares, made absolute.
- **It does not probe `/favicon.ico`.** If the page declares no icon, `icons.favicon` is null.
  Guessing would be an invention, not a fact.
- **No oEmbed, no Twitter/Telegram API calls.** The values are read from the page itself.
- **No retries.** One request per page. A 429 or a 503 is reported as `HTTP_ERROR` with its code
  and costs nothing. Retrying would slow every list down for a rare case.
- **No proxy rotation and no browser headers beyond a normal User-Agent.** Sites that block
  datacenter addresses answer 403, and that row is free.
  Wikipedia is one of them: from a datacenter address `en.wikipedia.org` answers 403, so that
  row comes back `HTTP_ERROR` and costs nothing.
- **No cookies, no logins, no authenticated pages.**
- **No crawling.** It reads exactly the addresses you send; a sitemap crawler is a different actor.
- **No cache between runs.** Every page is read live, so the answer is the page as it is now.
- **At most 4 MB of a page** is downloaded; the metadata lives in `<head>`, so a bigger body is
  cut, not failed.
- JSON-LD is capped at 50 blocks and 60 types, hreflang at 50 entries.

## Requests and safety

Redirects are followed in code, one hop at a time, and every hop is resolved and checked
against private, loopback and link-local ranges before the next request. A redirect into a
private address is blocked with `PRIVATE_TARGET`. Only `http` and `https` are fetched.
A 3xx that is not followed is reported as `REDIRECT` with its own status code: it is never
parsed as a page and never charged.
