# Core Web Vitals Audit - LCP, CLS, TBT, No API Key

PageSpeed actors on this store come in two kinds, and both hurt the buyer. One asks for a
Google API key before it will do anything. The other wraps the free PageSpeed Insights API
and starts answering `429 Quota exceeded` the moment the shared anonymous quota is gone,
which is what happens today: measured on 2026-11-10 from two different networks, the
keyless API refused every request with `429` before it ever ran an audit.

This actor measures the page itself, in its own Chromium, on purpose built emulated
hardware. There is no key to own, no quota to exhaust and no third party in the middle.
A staging URL that Google cannot reach is fine too, because the browser runs here.

## What one row is

One page, measured once:

| field | meaning |
| --- | --- |
| `lcpMs` | Largest Contentful Paint: when the biggest visible element was painted |
| `cls` | Cumulative Layout Shift: how far things moved while you were reading |
| `tbtMs` | Total Blocking Time: how long the main thread was blocked after first paint |
| `ttfbMs` | Time To First Byte: the server's answer time |
| `fcpMs` | First Contentful Paint: when the first text or image appeared |
| `ratings` | `good`, `needs-improvement` or `poor` for each of the five, by Google's published thresholds |
| `verdict` | the worst of those ratings, so one bad number is not hidden by four good ones |
| `requests`, `transferBytes`, `thirdPartyRequests`, `thirdPartyBytes` | how much the page pulled, and how much of it came from other people's servers |
| `renderBlockingResources` | scripts and stylesheets that hold the first paint |
| `domNodes`, `longTasks`, `layoutShifts` | page size and the raw events behind CLS and TBT |
| `httpStatus`, `finalUrl`, `redirects` | what the server actually answered, and every hop it took to get there |
| `strategy`, `cpuThrottling`, `networkProfile` | the conditions this row was measured under |

Every row also carries `status` and `reason`, so a page that was not measured says why in
plain words instead of quietly missing.

## How the measurement is made

The `mobile` profile does what Lighthouse does for its lab run: a 412x823 viewport, a 4x
CPU slowdown, and a slow 4G link (1.6 Mbps down, 150 ms latency). The `desktop` profile
uses a 1440x900 viewport, 10 Mbps and no CPU slowdown. Two runs are only comparable on the
same profile, so the profile is written into every row.

The browser is opened once per run and warmed before the first page. That is not
housekeeping: a cold browser was measured at 2.9 s time to first byte against 0.1 s warm,
and a number like that would have turned every verdict into `poor`.

## What this actor does not do

- **No INP.** Interaction to Next Paint needs a real person clicking, and a lab page load
  has nobody clicking. Total blocking time is reported as a lab proxy under its own name,
  and calling it INP would be a lie.
- **No Lighthouse score.** Google's 0-100 score is a weighted curve of five lab metrics.
  This actor gives you the five metrics and Google's published good / needs-improvement /
  poor thresholds, and does not invent a sixth number to summarise them.
- **No field data.** Nothing here comes from the Chrome User Experience Report. These are
  lab numbers for the page as it loads right now, from one location.
- **`transferBytes` is a floor, not the page weight.** A cross origin resource without a
  `Timing-Allow-Origin` header reports zero bytes to the browser, so the total can only be
  too low. `bytesPartial` says when that happened on this page.
- **No look inside iframes.** A cross origin frame is part of the page and is charged as
  one audited page, but the requests that frame makes on its own do not appear in the
  page's own timeline, so the address guard never sees them. What an embedded frame loads
  from inside a private network is outside this guard; the frame itself is not.
- **A dead host in the markup is not a private address.** When a subresource host name does
  not resolve, the page is still measured and still charged: one file could not be loaded,
  which is a fact about the page and not about the buyer's network. Those names are counted
  in `subresourceDnsSkipped`, and no row accuses the buyer of pointing at a private
  address. `NOT_CHECKED_PRIVATE_ADDRESS` means a name that really answered with a non
  public address, nothing else. The subresource host list is capped at 25 names, so
  `resourceHostsTruncated` on a row and `pagesWithTruncatedHostList` in the summary say
  when the list was cut. Every name costs one resolution, so the guard's worst case (25
  names) is part of the deadline margin, and `subresourceHostLookups` says how many
  resolutions this run really made.
- **No private addresses, and no pretending about when they are caught.** A host that
  resolves inside the private network is refused before the browser opens it. A redirect
  into that network, and a subresource loaded from it, are recognised *after* the page has
  loaded: Chromium starts the navigation itself and a route handler does not see every hop,
  so promising "refused before it opens" there would be a lie. Such a page never costs
  anything, it comes back as `NOT_CHECKED_PRIVATE_ADDRESS` with the address in the reason.
  Blocking the request itself needs a filtering proxy in front of the browser, which this
  actor does not have.

## Price

`page-audited` costs **$0.01 per audited page**. One delivered row with a real measurement
is one event. Nothing else is charged: a page that timed out, answered 404, turned out to
be a PDF, was refused as a private address, came twice, or was cut by `maxPages` comes back
as its own row with a `NOT_CHECKED_*` status and costs nothing.

The spending cap is the buyer's to set, and it is respected before every paid write, not
after: a cap reached mid run stops the run and the rest of the list comes back free with
`NOT_CHECKED_CAP`.

`billable` on a row is a claim about the price, not a receipt. One paid push writes the row
first and charges second, so a row whose charge failed is already in the dataset and stays
`billable: true`; it is counted in `deliveredUncharged` of the summary. The money really
taken is `chargedEvents`, and nowhere else.

## Statuses

| status | what happened | charged |
| --- | --- | --- |
| `OK` | the page was measured | yes |
| `NOT_CHECKED_INVALID` | the line is not an http or https url, an empty line included | no |
| `NOT_CHECKED_DUPLICATE` | the same url came earlier | no |
| `NOT_CHECKED_LIMIT` | beyond `maxPages` | no |
| `NOT_CHECKED_PRIVATE_ADDRESS` | the host, a redirect, or a subresource points inside a private network | no |
| `NOT_CHECKED_DNS_ERROR` | the host name does not resolve, or is not a valid host name | no |
| `NOT_CHECKED_TIMEOUT` | the page did not finish loading in `pageTimeoutSecs` | no |
| `NOT_CHECKED_HTTP_ERROR` | the page answered 4xx or 5xx | no |
| `NOT_CHECKED_NOT_HTML` | the response is a pdf, an image or anything that is not a page | no |
| `NOT_CHECKED_PROBE_ERROR` | Chromium could not measure the page, the reason is in the row | no |
| `NOT_CHECKED_CAP` | the spending cap was reached before this page | no |
| `NOT_CHECKED_BILLING` | the run could not read its own price, so it delivered nothing and charged nothing | no |
| `NOT_CHECKED_TIME` | the run was close to the platform deadline | no |
| `NOT_CHECKED_ERROR` | the dataset refused the row, or the run stopped earlier | no |

## Input

```json
{
  "urls": ["https://example.com", "https://apify.com"],
  "strategy": "mobile",
  "maxPages": 10,
  "settleSecs": 2,
  "pageTimeoutSecs": 45,
  "ignoreHttpsErrors": false
}
```

`maxPages` defaults to 10 pages with a 45 second page timeout. Ten slow pages can outlast the
platform's five minute check, so the run watches the platform deadline itself: when time runs
short it stops, delivers what it has and marks the tail `NOT_CHECKED_TIME` free of charge,
instead of being killed mid page. The margin it keeps is computed from `pageTimeoutSecs`,
the network idle wait and `settleSecs`, not from a constant, so one slow page can never be
cut in half by the clock. Raise `maxPages` together with the run timeout.

## Run summary

The run writes `RUN_SUMMARY-<runId>` into the named store `core-web-vitals-audit-summary`,
and a bare `RUN_SUMMARY` into the run's own store, whose template the output schema links to.

The numbers that matter: `requested` equals `processed` plus `notChecked` always.
`delivered` counts audited pages, `rowsWritten` counts every row written, `chargedEvents` is
what the platform charged. `billingActive` says whether this run could bill at all and
`billingReason` says why not. `stopReason` is null on a clean run, or `spending-cap`,
`charge-error`, `dataset-write-error`, `deadline`, `billing-price-unknown`,
`unexpected-error`. `byStatus` and `byVerdict` break the run down by cause and by rating.
`privateBlocked` counts every page the address guard stopped, `privateSubresourceBlocked`
the part of it that came from a subresource, `dnsErrors` the host names that did not
resolve, and `subresourceDnsSkipped` the subresource names that did not resolve at all and
therefore blocked nothing. `pagesWithTruncatedHostList` counts the pages whose subresource
host list was cut at 25 names; on those rows `resourceHostsTotal` is bigger than
`resourceHosts`, and `subresourceHostLookups` is how many names the guard resolved. `pageMarginSecs` is the margin the deadline check actually used.

A run that delivers nothing is not reported as successful: it ends as `FAILED` with the
breakdown in the status message, because a green run with an empty dataset tells the buyer
nothing.

## Run it locally

```bash
export PYTHONPATH=$PWD
python -m src.main
```

With no platform around it, `billingActive` comes back false and every audited page is
delivered free with `billable: false`, which is the honest answer for a run nobody is
selling.
