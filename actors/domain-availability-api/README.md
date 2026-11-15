# Domain Availability Checker - Bulk RDAP Lookup

You should not pay for a failed lookup. Most availability checkers charge every line
they touch, including the lines where the registry never answered, and you find out
only after the run that half of the list was never really checked. This actor asks the
registry through RDAP, the modern WHOIS, and charges only for a domain that got a real
verdict: **available** or **registered**.

## What you get

One row per line of input:

| field | meaning |
|---|---|
| `verdict` | `available` (the registry answered 404 for the name) or `registered` (it answered 200) |
| `available` | `true` only for `available`, `false` for `registered`, `null` on a free status row |
| `domain`, `tld` | the name normalised to punycode and its top level domain |
| `registrar`, `registeredAt`, `expiresAt`, `updatedAt` | what the registry published for a taken name |
| `statuses` | the registry status flags, for example `client transfer prohibited` |
| `nameservers` | the delegated name servers |
| `httpStatus` | the raw RDAP answer, so a buyer can audit the verdict |
| `status`, `reason` | the row status and, for a free row, why it is free |

Input takes plain domains, full URLs or email addresses: `https://apify.com/pricing`
becomes `apify.com`, and international names are converted to punycode before the
lookup, so `münchen.de` and `xn--mnchen-3ya.de` are one domain and are charged once.

## Price

**$0.001 per domain that got a verdict from the registry.** The lowest spending limit
you can set per run is $0.05, and you pay only for the rows that reached the dataset
with a verdict. For comparison, the busiest actor in this shelf charges $0.00049 per
result and bills the lines the registry never answered; a thousand domains that all get
an answer cost $1.00 here.

Everything else costs nothing and says why:

| status | when |
|---|---|
| `INVALID` | the line is not a domain name |
| `DUPLICATE` | the same domain was already in this run |
| `NOT_CHECKED_UNKNOWN` | the registry did not answer, or answered 429, 5xx or with something that is not RDAP |
| `NOT_CHECKED_LIMIT` | the run reached `maxDomains` |
| `NOT_CHECKED_CAP` | the run reached its spending limit |
| `NOT_CHECKED_TIME` | the run ran out of time |
| `NOT_CHECKED_BILLING` | the price list could not be read, so nothing was sold |
| `NOT_CHECKED_ERROR` | an unexpected error |

A free row never carries `verdict` or `available`: a row that was not checked cannot be
misread as a verdict.

## What this actor does not do

* It does not guess. If the registry is silent, the row says `NOT_CHECKED_UNKNOWN`
  instead of `available`, because "we could not tell" and "nobody owns it" are
  different answers.
* It does not cover every TLD ever delegated. RDAP is published by most registries but
  not by all: a TLD whose registry publishes no RDAP comes back `NOT_CHECKED_UNKNOWN`
  with that reason, is not charged, and no request is made for it. `.de`, `.io`, `.me`
  and `.us` are covered through their own registry endpoints, measured 2026-11-13.
* It does not read a 404 from a bootstrap service as "free". A 404 is a verdict only
  when it comes from the registry of that TLD; a 404 that carries a notice about no
  RDAP service stays unknown and free, because "nobody owns it" and "we could not ask"
  are different answers.
* It does not check whether a name is a trademark problem, and it does not register
  anything. It reports what the registry says.
* It does not read WHOIS port 43 fallbacks. RDAP only, so the answer is structured and
  the same shape for every registry.

## Run summary

The run writes a JSON summary into the run's key-value store under `RUN_SUMMARY` and
`RUN_SUMMARY-<runId>`. It carries `requested`, `domainsAttempted`, `domainsChecked`,
`chargedEvents`, `rowsWritten`, `byStatus`, `chargeErrors`, `writeErrors`,
`deliveredUnverified`, `chargeUncertain`, `stopReason`, `counterMismatch`,
`billingActive` and `datasetNote`. If a charge failed after the rows were written, the
note says that delivery and the charge are both unknown and that the dataset must be
read before re-running, so a buyer is not charged twice.
