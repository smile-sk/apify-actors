# WHOIS Domain Lookup API - RDAP, DNS, MX, TXT, Expiry

A WHOIS actor is usually judged on the day a registry rate-limits it. The run comes back green, the
dataset is full of rows, and half of them say nothing: a name, a null registrar, a null creation
date. The buyer pays for that. This actor is built the other way round: a row either carries the
registry answer, or it says in plain words why there is no answer, and that row costs nothing.

One run takes a list of domains and returns, per domain:

* registration, expiry and last-change dates, and the domain age in whole days;
* registrar name when the registry publishes it;
* EPP statuses (client transfer prohibited and the like) and nameservers;
* whether DNSSEC is signed;
* DNS records: nameservers, MX with priorities, TXT, the SPF record, the DMARC record and its `p=`
  policy, plus the resolver that answered.

Data comes from RDAP, the open replacement for port 43 WHOIS: the IANA bootstrap file maps each TLD
to its own registry. No key, no proxy, no paid middleman.

## What a run looks like

Input:

```json
{
  "domains": ["example.com", "google.io", "google.de", "this-name-is-free-xz9q.com", "https://www.apify.com/pricing"],
  "includeDns": true,
  "maxDomains": 50
}
```

A link or an email is accepted: only the host is looked up, so `https://www.apify.com/pricing`
becomes `apify.com`. `www.` is dropped, because it is the same registration.

Output, one row per domain (real values from a run on 2026-11-09):

```json
{
  "domain": "openai.com",
  "status": "REGISTERED",
  "registered": true,
  "registrar": "MarkMonitor Inc.",
  "createdAt": "2007-01-19T19:28:24Z",
  "expiresAt": "2029-01-19T19:28:24Z",
  "ageDays": 7233,
  "daysUntilExpiry": 802,
  "statuses": ["client delete prohibited", "client transfer prohibited"],
  "nameservers": ["ns1-02.azure-dns.com", "ns2-02.azure-dns.net"],
  "dnssecSigned": false,
  "dns": {
    "mx": [{"preference": 1, "exchange": "aspmx.l.google.com"}],
    "spf": "v=spf1 include:_spf.google.com ~all",
    "dmarc": "v=DMARC1; p=reject",
    "dmarcPolicy": "reject",
    "resolver": "system"
  },
  "billable": true
}
```

## Statuses

| status | meaning | charged |
|---|---|---|
| `REGISTERED` | the registry returned a record | yes |
| `AVAILABLE` | the registry answered 404: the name is not registered | yes |
| `NOT_CHECKED_NO_RDAP` | this TLD has no public RDAP service (for example .co, .eu, .ru); DNS records are still returned | no |
| `NOT_CHECKED_SOURCE` | the registry did not answer or answered with an error | no |
| `NOT_CHECKED_INVALID` | the line is not a domain, with the reason | no |
| `NOT_CHECKED_DUPLICATE` | the same domain came earlier in the list | no |
| `NOT_CHECKED_LIMIT` | beyond `maxDomains` | no |
| `NOT_CHECKED_TIME` | the run was close to the platform deadline; send it in a second run | no |
| `NOT_CHECKED_CAP` | the spending cap of this run was reached | no |
| `NOT_CHECKED_BILLING` | the run already knew it could not bill | no |
| `NOT_CHECKED_ERROR` | the dataset refused the row after a retry | no |

`registered` is `true`, `false` or `null`, and `null` never means `false`: it means the registry was
not read. `AVAILABLE` is the only `false`, and it comes from a registry 404, never from a timeout.

## Price

`domain-checked` is charged per row that carries a registry answer: a registered domain, or a
registry saying the name is free. A TLD without RDAP, an unread registry, a bad line, a repeated
line and the tail cut by `maxDomains` are all free and say so in `status` and `reason`.

The run summary (in the key-value store as `RUN_SUMMARY`, and in the named store
`whois-domain-lookup-summary` under `RUN_SUMMARY-<runId>`) reports `chargedEvents`, `byStatus`,
`deliveredUncharged` and `stopReason`, so the bill can be checked against the dataset row by row.

## Limits, honestly

* **RDAP coverage.** The IANA bootstrap file listed 1203 TLDs when this was written. Four more have
  RDAP outside it and are mapped by hand, each measured on a registered name: `.io`, `.me` (Identity
  Digital), `.us` and `.de` (thin: no registrar, no creation date). A TLD outside both lists has no
  public RDAP service, and this actor does not fall back to guessing.
* **Rate limits.** One registry is one host, and requests to it are spaced. A 429 is retried once
  with a short pause; a second failure becomes `NOT_CHECKED_SOURCE` for that domain and the run reads
  on.
* **`maxDomains`** defaults to 50. The list beyond it is not dropped: it comes back as
  `NOT_CHECKED_LIMIT` rows with their own line in `input`, so the buyer sees the whole list.
* **Platform deadline.** The actor reads `ACTOR_TIMEOUT_AT` and stops starting new lookups 10 seconds
  before the platform kills the run, marking the rest `NOT_CHECKED_TIME` instead of dying silently.
* **Personal data.** Registrant names, addresses and emails are not collected, and none of it is
  asked for. Registries redact most of it anyway; a country code is returned only when the registry
  publishes it.

## What this actor does not do

* It does not say whether a premium or reserved name is for sale: `AVAILABLE` means "not registered",
  not "you can buy it at the standard price".
* It does not value a domain, score its history, or read the Wayback Machine.
* It does not use port 43 WHOIS: registries are moving to RDAP, and a port 43 path would be a second
  source of half-answers.
* It does not hide a failure inside an empty row. If nothing could be read at all, the run ends with
  a failed status and a sentence naming every status that came back.

## Run it locally

```bash
pip install -r requirements.txt
APIFY_LOCAL_STORAGE_DIR=./storage python -m src.main
```

Tests: `python tests/test_domain.py`, `python tests/test_rdap.py`, `python tests/test_dnsrec.py`,
`python tests/test_main.py`. They run without network: the RDAP fixtures are real registry answers
captured on 2026-11-09.
