# Broken Link Checker API - 404s, Redirects & Retries

Check any list of URLs - or a whole sitemap - for broken links in one run.
Every URL is tested with a real HTTP request and reported with its final
status code, redirect chain, response time, error type and how many retries
it took.

The most-installed broken link checker in the store carries a 2-star review
that says it plainly: links that are still working are scanned as broken,
and its own FAQ admits it does not support custom headers. This Actor is
built so that does not happen here:

| What usually happens | What happens here |
|---|---|
| One failed request marks a working link broken | Every check retries on network errors, timeouts, DNS failures, 429 and 5xx before the broken verdict - the row reports how many retries were used, and a Retry-After header sent by the site is respected |
| 403 from a bot-hating site is sold to you as a broken link | Custom headers and User-Agent are first-class input: pass your Authorization header and a real browser UA |
| Sitemap support means buying a second Actor | Pass sitemapUrl and all links inside it are extracted and checked together with your list |
| You are told "not blocked by robots.txt or a firewall" in troubleshooting | Blocked, private and invalid targets are reported honestly and cost nothing |

## Input

- **urls** - list of public HTTP(S) URLs or bare domains. Duplicates are
  checked once. Query strings are kept: two URLs that differ only by query
  are two checks.
- **sitemapUrl** - optional sitemap URL; all its links are checked together
  with the list. Sitemap index files are followed.
- **expectedStatusCodes** - a response is healthy only when its final status
  code is in this list. Empty means the default HTTP 200-399 rule.
- **headers** - extra request headers for every check (for example an
  Authorization bearer token). This is how you check endpoints that block
  the default bot User-Agent.
- **userAgent** - User-Agent header for the checks.
- **retries** - extra attempts on network errors, timeouts, DNS failures,
  429 and 5xx before a link is marked broken. Default 2. This is the core
  fix for false broken links: a link that answers 200 on a retry is reported
  healthy.
- **timeoutSec**, **maxRedirects**, **slowThresholdMs**, **maxItems**,
  **concurrency**, **cacheMinutes**, **timeBudgetSeconds** - self-explanatory
  caps, all visible in the Input tab.

## Output

One row per URL:

| field | meaning |
|---|---|
| url | the checked URL |
| statusCode | final HTTP status, null when the request never got an answer |
| broken | true when the final status is outside the expected rule, or the request failed after all retries |
| errorType | null, http_error, network_error, timeout, dns_error, tls_error, redirect_loop, invalid_input, private_target |
| error | human-readable reason |
| finalUrl | URL after following redirects |
| redirectCount | redirect hops followed |
| responseTimeMs | time of the accepted final response |
| slow | true when responseTimeMs exceeds slowThresholdMs |
| retriesUsed | how many retries this URL needed |
| expectedStatusCodes | the health rule applied to this row |
| source | list or sitemap |
| fromCache | true when a fresh verdict from another run was reused |
| checkedAt | UTC timestamp of the actual probe (kept on cache reuse) |
| cachedAt | UTC timestamp of the cache reuse, only on rows with fromCache true |

The RUN_SUMMARY key of the run's storage carries the totals: urlsChecked,
brokenCount, healthyCount, allHealthy, coverageComplete, cache hits,
invalid and private counts, rows refused by the spending limit, and rows
omitted by the time budget. allHealthy is true only when every listed URL
was checked and healthy - omitted, refused, dropped or failed rows make it
false, so a release gate can never pass on a half-checked list.

## Pricing

Pay per result: **$0.001 per checked URL** (charged as $1.00 / 1,000
url-checked events). Only URLs that were actually checked are charged.

- invalid and private targets are free, whether the private address is the
  link itself or the destination of its redirect chain;
- rows reused from the cache are free (a fresh verdict from another run
  inside the cache window costs nothing);
- a row refused by your spending limit is neither billed nor delivered.

You control the spending limit in the run options, so a huge sitemap can
never bill past what you allow.

### Spending cap

If you do not set a spending cap, Apify applies its own default (about $4.4 per run). When that cap is reached the run stops early: the remaining items are skipped and you are charged only for what was actually delivered. For a full pass, set `maxTotalChargeUsd` in the run options.
