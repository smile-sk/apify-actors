# Website Contact Finder: emails, phones and socials from any site

Contact scrapers in this store tend to fail in the same three ways, and the reviews say it
better than any marketing page could:

1. **The email is right there on the site and the scraper does not retrieve it.** The most
   repeated complaint in this category, word for word.
2. **You pay per lead, so you cannot predict the bill.** The advertised price is per page,
   the real one is per contact - and a list of 500 sites can cost ten times what you
   planned, or nothing at all, depending on luck.
3. **You get a value with no source, and sometimes a guess dressed up as verified.** If an
   email was reconstructed from a pattern, calling it "verified" is not a detail - it is
   the difference between a working campaign and a bounced one.

## What this Actor does instead

- **Crawls the pages where contacts actually live.** The address you give, plus every
  contact / about / imprint / support / privacy page linked from it (up to
  `maxPagesPerSite`, default 5). More pages, more real emails - and no browser, no proxies.
- **Every value carries its source.** Each email and phone has `foundOn` - the exact page
  it was read from, and `kind` - whether it came from a `mailto:`/`tel:` link or from the
  page text. Nothing is generated, nothing is guessed, no pattern is filled in.
- **Pay only for sites that delivered.** A site that returns at least one email or phone
  costs one event. **A site with no contacts is free** - you still get its row, with the
  reason. **A row served from the cache is free too.** That is the whole pricing model,
  no tiers to decode.
- **The same site sent twice is crawled and billed once.** `apify.com`, `https://apify.com`,
  `https://www.apify.com/` and `simplify.hr/contact` are one site, not four - duplicates are
  dropped by host before any request, and `SUMMARY.duplicatesRemoved` tells you how many.
  Different subdomains stay different sites.
- **Nothing disappears silently.** Every unique input gets a row and one of six statuses:
  `OK` (contacts found), `NO_CONTACTS` (with how many pages were checked), `BLOCKED` (the
  site answered with an anti-bot challenge page - free, HTTP status in `reason`),
  `UNREACHABLE` (the HTTP code or the exact network error in `reason`), `INVALID_URL` (the
  line is not a http(s) address - free) or `ERROR` (that one line failed unexpectedly and
  the rest of the run continued). Compare that with a run that returns fewer rows than you
  sent.

## What you get

One row per website:

```json
{
  "website": "apify.com",
  "url": "https://apify.com",
  "domain": "apify.com",
  "company": "Apify",
  "status": "OK",
  "emails": [
    {"value": "sales@apify.com", "foundOn": "https://apify.com/contact", "kind": "mailto", "role": true}
  ],
  "phones": [
    {"value": "+420 123 456 789", "e164": "+420123456789", "foundOn": "https://apify.com/contact", "kind": "tel-link"}
  ],
  "socials": {"linkedin": "https://linkedin.com/company/apify", "github": "https://github.com/apify"},
  "address": {"street": "Vodickova 704/36", "city": "Praha", "postalCode": "11000", "country": "CZ"},
  "pagesScanned": ["https://apify.com", "https://apify.com/contact"],
  "pagesFailed": [],
  "reason": "",
  "fromCache": false,
  "scannedAt": "2026-11-05T09:12:44+00:00"
}
```

Social profiles collected when linked on the site: LinkedIn, X, Facebook, Instagram,
YouTube, GitHub, TikTok, Telegram, Threads. Address comes from the site's own schema.org
(JSON-LD) markup and is `null` when the site does not publish one.

## Price

Per site **that returned contacts** - not per page, not per address, not per email inside
the row. A site with nothing to give costs **$0.00**, and the row still tells you what was
checked. Long lists behave predictably: 500 sites that all have contacts cost 500 events,
not 1,400. Duplicates are dropped before crawling, and a cached row is not charged at all.

**You set the cap and the platform enforces it.** Every run has a hard spending limit
(`maxTotalChargeUsd` - in the Console or in the API call). We lowered the smallest limit the
Actor accepts to **$0.02**, the price of one result, so a run can never cost more than the
number you chose. No tiers to decode, no invoice surprises.

**For a long list, set the cap deliberately.** 500 sites that all have contacts cost $10, while
the platform default is about $4.8 - if your list is that long, raise the cap before the run.
**If the cap is reached, the SDK documentation says the platform stops that run.** In our own two
tests, with a $0.02 and a $0.04 cap, the run finished with every row intact and nothing was
charged beyond the cap - we have not reproduced an abort ourselves, so treat it as the
documented behaviour and not as our promise. Either way `SUMMARY` is saved as the run goes
(every 25 sites, `partial: true`), so rows already found are not lost, and
`SUMMARY.cappedNotCharged` counts the sites that were reported but not billed.

## Honest limits

- **HTTP only, no JavaScript.** Sites that draw their contact block with client-side JS will
  come back as `NO_CONTACTS`. We say so instead of pretending. A browser-based mode is not
  included - it would multiply the price of every run.
- **No deliverability claims.** We do not say an email is "verified", "valid" or
  "deliverable", because we did not run an SMTP handshake. We say where we found it.
- **A phone is returned exactly as it is written on the page.** `8 (495) 123-45-67` stays
  exactly that. We never glue a `+` in front of a number that did not have one - that turned
  a Moscow number into a Vietnamese one in an earlier build. `e164` appears only when the
  page itself wrote the international form.

- **A bare number is not a phone.** A number written without `+` is taken only when a
  phone word (`phone`, `tel`, `call`, `contact`…) stands next to it, or when it is a `tel:`
  link; a `+` number is taken as written. That is how a code sample on python.org - the
  Fibonacci sequence `8 13 21 34 55 89 144` - stopped being read as two phone numbers.
- **Code samples are not contacts.** Text inside `<pre>`, `<code>` and `<samp>` is left out
  of phone and email extraction, and a phone or an address published in the site's
  schema.org (JSON-LD) markup is read too.
- **A date is not a phone, and neither is a parcel id.** Publication dates (`04.11.2026 19:48`),
  map coordinates (`50.4501 30.5234`) and tracking numbers (`1234 5678 9012` after the words
  "tracking number") are rejected before they can become a fake contact - every one of them
  was a live bug here, and every one now has a test.
- **Dot-separated phones are understood.** `202.555.0123` is a US number, not a decimal
  fraction; the guard that removed `3.14159` no longer eats it.
- **A third-party mailbox still counts.** If the only address on a site is a gmail.com
  inbox, it is returned in `foreignEmails` and the site counts as having contacts - the
  complaint we built this Actor against was "the email is on the site and the scraper
  misses it".
- **Cloudflare and friends.** A site that blocks automated access is reported as
  `BLOCKED` with the HTTP status in `reason` - and it is free.
- **Same answer twice is cached.** For `cacheMinutes` (default 24h) a repeated run is served
  from the cache in the account of whoever runs the Actor; those rows are marked
  `fromCache: true` and are **not charged** - you paid for that crawl the first time.

## Run it your way

From the Apify UI, from the API, or from an MCP client. One input is enough:

```json
{ "websites": ["apify.com", "stripe.com", "figma.com"] }
```

Words that are not guessed here: every email in the row exists on the site you sent us.
