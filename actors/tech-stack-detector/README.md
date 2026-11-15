# Tech Stack Detector API - CMS, Ecommerce and Frameworks

Give it a list of domains. It reads each page and tells you what that site is built with:
the CMS, the ecommerce platform, the framework, the analytics, the ad pixels, the payment
provider, the CDN and the hosting. Every technology comes with the mark that proves it,
so a row is evidence, not a guess.

## What you get

One row per site:

- `technologies` - every match as `name`, `categories`, `confidence`, `evidence` and
  `version`;
- `evidence` names the exact mark that matched: a response header, a cookie, a script
  url, a meta tag or a snippet of the markup;
- `technologyNames` and `categories` - the same answer flattened, ready for a filter or
  a spreadsheet;
- `server` and `poweredBy` - the raw headers, which often say a lot on their own;
- `finalUrl` and `redirects` - where the request ended up.

A name is reported only when its mark proves it. A product word buried inside a hash,
inside somebody else's file name or inside a longer token is not that product: a Cloudinary
hash that spells `modx`, or the BBC's own `dotcom-bootstrap.js`, is not MODX and not
Bootstrap, and such a page is free. A header, a cookie or a generator meta tag is
`confidence: high`; a mark found only in the markup is `medium`, and `medium` on a name
like Contentful means an asset came from that platform, not that the whole site is built
on it. Filter on `high` when you need certainty.

About 160 signatures ship in the image: WordPress, Shopify, WooCommerce, Magento, Wix,
Webflow, Squarespace, HubSpot, Next.js, Nuxt, React, Vue, Angular, Svelte, Gatsby, Astro,
Laravel, Django, Rails, Google Analytics 4, Google Tag Manager, Meta Pixel, TikTok Pixel,
Klaviyo, Mailchimp, Intercom, Stripe, PayPal, Cloudflare, CloudFront, Fastly, Vercel,
Netlify, nginx, Apache, and more across 25 categories.

## How the money works

Pay per event: `site-scanned`, charged once for a site that was read and whose answer you
are given. Everything else is free, and the row still tells you why:

| status | when | charged |
|---|---|---|
| `OK` | the page was read and at least one technology was recognised | yes |
| `NO_TECHNOLOGY_FOUND` | the page was read and no signature matched | no |
| `FILTERED_OUT` | the site was scanned but your own `onlyIf` or `excludeIf` removed it | no |
| `NOT_CHECKED_BLOCKED_PAGE` | the site answered a challenge page instead of itself, whether it names itself (Cloudflare, Fastly, an anti-bot wall) or is a wall of markup with nothing to read | no |
| `NOT_CHECKED_NO_CONTENT` | the edge answered 202, 203 or 204: accepted, but there is no page inside | no |
| `NOT_CHECKED_EMPTY_PAGE` | the answer had no readable text at all, or an empty body | no |
| `NOT_CHECKED_*` | the site was not scanned: a typo, a timeout, a 404, a private address, a duplicate, a page that is a file, or the run stopped early | no |

A filter is a promise about the money: with `onlyIf: ["shopify"]` you pay only for the
sites that really run Shopify, and the rest come back as `FILTERED_OUT` with the answer
attached and no charge. That is the difference between buying a scan and buying a lead
list.

## Input

- `urls` - one site per line, a full url or a bare domain (`example.com` works);
- `maxSites` - the list is cut here (default 100), the rest come back free;
- `onlyIf` - deliver and charge only sites that use one of these technologies;
- `excludeIf` - skip sites that use any of these technologies;
- `timeoutSecs` - request timeout, default 20;
- `includeEvidence` - turn the evidence off for a leaner dataset;
- `userAgent` - empty means desktop Chrome. Some sites serve a different stack to a bot,
  and a custom value is how you look at that other answer.

A name in a filter matches a part of the technology name: `shopify` finds Shopify,
`next` finds Next.js, `google` finds Google Analytics 4 and Google Tag Manager.

## What this actor does not do

- it does not run javascript. A technology that only exists after a script executes is
  not seen, and a page with nothing recognised comes back free rather than wrong;
- it does not guess versions. A version is reported only where the vendor prints it,
  such as a generator meta tag or a versioned asset path;
- it does not use a commercial database such as BuiltWith or Wappalyzer: the answer is
  read from the page itself, which is why the price is a fraction of theirs;
- it does not keep history, take screenshots or crawl the whole site: one url, one page;
- it does not read a site through a proxy. A site that answers a challenge page
  instead of itself (Cloudflare, a captcha wall, "enable javascript") is recognised and
  comes back free as `NOT_CHECKED_BLOCKED_PAGE`, because the stack of a challenge page is
  not the stack of the site. The same goes for a wall that does not name itself: a page
  that ships kilobytes of markup and almost nothing to read is a challenge too, and an
  interim answer such as `202 Accepted` is not a page at all. Try a custom `userAgent`
  for those sites.

## Limits

- a page larger than 3 MB is refused with `NOT_CHECKED_TOO_LARGE`, because the marks live
  in the head and the first screens of markup;
- only `200` and `201` count as a page: `202`, `203` and `204` come back as
  `NOT_CHECKED_NO_CONTENT`, and an empty body as `NOT_CHECKED_EMPTY_PAGE`;
- at most 3 redirect hops, each one resolved and refused before its request goes out if it
  leads into a private network;
- a private or loopback address is refused before any request: this actor never reads the
  inner network, and it never charges for trying;
- one site gets the time left before the platform deadline, minus a margin. A server that
  drips a hundred bytes every few seconds comes back as `NOT_CHECKED_TIME` instead of
  taking the whole run down with it, which would lose every row of the run.

## Output

Every row also carries `status`, `reason`, `input`, `inputIndex`, `httpStatus`,
`contentType`, `detected` and `billable`. The run summary says how many lines were asked
for, how many were scanned, how many were filtered, how many had no known technology,
what the whole list is built with, and what was really charged. The summary lands under
`RUN_SUMMARY` in the run's own default key-value store, and under `RUN_SUMMARY-<runId>` in
the named store `tech-stack-detector-summary`, so two runs in a row do not overwrite each
other. `uniqueSites` counts the distinct urls in the list with the cut lines included, and
`sitesAttempted` says how many sites this run really worked on: a list cut by `maxSites`
is not a scan.

## Categories

The `categories` field groups a match as one of: `cms`, `ecommerce`, `framework`,
`js-library`, `ui`, `programming-language`, `server`, `hosting`, `cdn`, `analytics`,
`tag-manager`, `advertising`, `marketing`, `email`, `crm`, `support`, `chat`, `payments`,
`security`, `ab-testing`, `seo`, `video`, `fonts`, `page-builder`, `blogs`.
