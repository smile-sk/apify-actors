# Uptime Monitor API - Website Status, SSL and Response Time

Bulk website uptime checks with automatic retries and an optional second
EU vantage point. HTTP status, response time, SSL validity and certificate
expiry for scheduled monitoring, agency client portfolios and deployment
gates. Pay only for delivered observations.

## The pain this Actor removes

The leading uptime actors in this niche have three known gaps:

1. **No automatic retries.** A single transient timeout or a hiccup on the
   way to the site is reported as downtime, and you get woken up for nothing.
   This Actor probes every URL at least twice before it calls the site
   unreachable, and keeps every probe attempt visible in the output.

2. **One observer only.** "Does it measure global uptime? No. One Apify
   runtime location." A false alarm or a false all-clear both start there.
   This Actor can add a second vantage point: an EU gate reports whether the
   target answers at all. A URL is reported up when EITHER observer saw it
   answer, so a healthy site is not marked down just because one observer
   had a bad path to it.

3. **SSL stops at "valid or not".** No certificate expiry date is extracted
   by the leading actor. This Actor adds sslDaysToExpiry for HTTPS targets,
   read even while the target is failing, so a certificate that dies in 7
   days shows up before it breaks your site.

Also: no charge for starting the run. A run of 100 URLs here costs the same
per observation as the niche leader ($0.50 / 1,000) but with retries, a
second observer and certificate expiry included.

## What you get per URL

| Field | Meaning |
|---|---|
| up | A probe received a final HTTP status in 200-399, or the second observer reported the target answering. Custom health rules do not change up. |
| healthy | Transport succeeded and all configured health rules passed. |
| healthIssues | Reasons such as unexpected_status, slow_response, timeout, dns_error, tls_error, gate_unverified (second observer answered without an HTTP code). |
| statusCode | Final HTTP status after redirects; null without a response. When the second observer is the only one that saw the target answer, its reported code is used; when the runtime itself answered with a final 4xx/5xx, that code is reported even if the second observer saw something else. |
| responseTimeMs | Latency of the accepted final apify response, through final headers. A 500 in 100 ms followed by a 200 in 900 ms reads 900, not the minimum over attempts. The second observer's latency is visible in probes and is not mixed in. A final 5xx from the runtime carries no latency number: responseTimeMs is null there, because only accepted probe responses count toward it. |
| httpStatusExpected | Whether the final status matches expectedStatusCodes; null when only the second observer saw the target. |
| responseTimeOk | Whether latency is within maxResponseTimeMs; null when only the second observer saw the target. |
| sslValid, sslDaysToExpiry | TLS verification result and days until expiry for HTTPS. sslValid is true when the HTTPS handshake completed and the chain verified (any final status, including 4xx and 5xx), false on tls_error, null when no answer arrived. Expiry is read even for targets that are currently down. |
| probes | Every probe attempt: location (apify or eu-gate), attempt, status, latency, error. |
| fromCache | True when the observation was reused from another run inside the cache window. Cached rows are free. |
| errorType | One of: timeout, dns_error, tls_error, connection_error, redirect_blocked, too_many_redirects, private_target, invalid_url. |

## Billing, honestly

- $0.0005 per delivered URL observation. An unreachable public website,
  including one whose DNS does not resolve (errorType dns_error), is a
  useful, billed observation, exactly like the niche leader.
- Starting a run costs nothing.
- Invalid URLs and non-public targets (private, loopback, link-local) are
  reported for free and are never billed. URLs with embedded credentials
  (https://user:pass@host/) are rejected as invalid and never billed.
- A fresh observation from another run within cacheMinutes is reused for
  free and marked fromCache. The cache key includes the health rules, so
  changing expectedStatusCodes or maxResponseTimeMs does not reuse a verdict
  made under different rules.
- Set a maximum cost per run (maxTotalChargeUsd). When the budget is
  exhausted, rows stop being delivered and the coverage summary reports the
  gap so an incomplete run cannot look like an all-clear. The platform refuses charges only when the next event would not fit, so a run can overshoot the cap by up to one event (41 checks at $0.0005 costs $0.0205 against a $0.02 cap).

## Coverage summary: no false all-clear

RUN_SUMMARY in the run's key-value store provides:
requested_unique_urls, selected_urls, checked_urls, omitted_by_max_items,
omitted_by_time_budget, invalid_count, private_count, cached_count,
refused_by_charge_limit, delivery_failures, healthy_count,
unhealthy_count, unhealthy_urls, finished, run_error, complete, all_healthy.

For an all-clear, require run status SUCCEEDED plus finished=true,
complete=true and all_healthy=true. all_healthy is false while any row was
invalid, private or undeliverable, so a run cannot read as an all-clear when
part of the input was never measured. A run may succeed while a website is
down: that is a valid measurement. complete=false means not every supplied
URL produced an observation.

## Example

Input:

```json
{
  "urls": ["https://example.com", "https://apify.com"],
  "expectedStatusCodes": [200],
  "maxResponseTimeMs": 5000
}
```

Output row:

```json
{
  "url": "https://example.com/",
  "up": true,
  "healthy": true,
  "healthIssues": [],
  "statusCode": 200,
  "responseTimeMs": 412,
  "httpStatusExpected": true,
  "responseTimeOk": true,
  "sslValid": true,
  "sslDaysToExpiry": 83,
  "probes": [
    {"location": "apify", "attempt": 1, "status_code": 200, "latency_ms": 412, "error": null},
    {"location": "eu-gate", "alive": true, "http_code": 200, "latency_ms": 51, "error": null}
  ]
}
```

## Limits and honest notes

- "Unhealthy" is an observation from up to two locations, not global uptime
  and not a contractual SLA percentage. It does not measure availability
  between checks.
- The Actor sends a HEAD/GET request and stops after final headers. It does
  not render pages, run JavaScript or detect content errors behind a
  healthy HTTP response.
- Redirects are followed up to ten hops, and every hop is checked against
  private ranges. Private, loopback and link-local targets are refused.
  Public IPv6 literals are accepted.
- The query string is part of the checked URL: https://e.com/a?token=1 and
  https://e.com/a?token=2 are checked as two different targets.
- The eu-gate observer is optional: it is used when the run's environment
  provides UPTIME_GATE_URL and UPTIME_GATE_AUTH. Without them, checks run
  from the Apify runtime only and the retry logic still applies.
- timeBudgetSeconds caps the checking phase; when it is exhausted the
  coverage summary reports omitted_by_time_budget instead of silently
  finishing.
- The Actor sends no emails and creates no schedules automatically. For
  alerts, save your input as a task, add an Apify Schedule and route the
  ACTOR.RUN.SUCCEEDED webhook to your alerting system.
