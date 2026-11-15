# IBAN Validator: Checksum, Country & SEPA

Most "IBAN validators" check the **shape** of the string and answer *valid*. A made-up
number passes them: right country code, right length, right letters - and the money is
gone when the payout batch runs. This Actor does the arithmetic a bank does.

Give it one IBAN per line, or paste a whole invoice and it finds them itself.

## What the answer contains

| field | meaning |
|---|---|
| `valid` | `true` only when the **mod-97 checksum** holds (and the BIC, if given, matches) |
| `status` | `VALID`, `BAD_CHECKSUM`, `BAD_LENGTH`, `UNKNOWN_COUNTRY`, `BAD_CHARACTERS`, `BAD_FORMAT`, `BAD_BIC`, `EMPTY` |
| `expectedCheckDigits` | when the checksum fails - **the pair the number should have had** |
| `country`, `countryCode` | from the official IBAN registry (80+ countries), not from the string alone |
| `sepa` | whether that country is in the SEPA area |
| `printed` | the number the way banks print it, groups of four |
| `bban` | the national part, for your own downstream checks |

## What it refuses to overclaim

- A **BIC** is checked for shape and for its country: `DEUTGB2L` with a German IBAN is a
  contradiction and is reported as one. We do **not** claim to know which bank owns the
  account - the IBAN alone cannot prove that, so we say nothing.
- A country we do not know is `UNKNOWN_COUNTRY`, never a guess.
- Nothing is dropped silently: an empty line, a truncated number, a URL typed by mistake -
  each becomes its own row with a reason.

## Pricing

**$1.00 per 1,000 IBANs whose checksum was actually computed** ($0.001 each), and platform
usage is included in that price.

- A malformed line, an unknown country, an empty line - **free**, and still reported.
- The input is pure arithmetic: no proxy, no network calls, so a run of 10,000 numbers is
  a second of work, not a scraping bill.

## Typical use

- validating a payout batch before it leaves your ERP;
- onboarding check for a B2B customer's bank details;
- cleaning a supplier file where a typo sends money to nowhere;
- catching `O`/`0` and `l`/`1` typos that pass every regex.

## Example input

```json
{
  "ibans": "DE89 3704 0044 0532 0130 00\nGB82 WEST 1234 5698 7654 32\nDE89 3704 0044 0532 0130 01",
  "bics": "DEUTDEFF\n\n"
}
```

The third number has the right shape and the wrong arithmetic - the Actor answers
`BAD_CHECKSUM` and names the check digits it should have had.

## API

```bash
curl "https://api.apify.com/v2/acts/stas_k~iban-validator/run-sync-get-dataset-items?token=YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ibans": "NL91ABNA0417164300"}'
```

## Notes

- Spaces and dashes are ignored, upper/lower case is normalised; characters themselves are
  never "corrected" - a letter `O` where a zero was meant is caught by arithmetic, not by
  silently rewriting the input.
- Up to 10,000 IBANs per run.

### Spending cap

If you do not set a spending cap, Apify applies its own default (about $4.4 per run). When that cap is reached the run stops early: the remaining items are skipped and you are charged only for what was actually delivered. For a full pass, set `maxTotalChargeUsd` in the run options.

## Run summary and where to find it

The run writes one summary record with the counters of the whole run: how many IBANs
came in, how many were valid, how many were billed, how many were stopped by the
spending limit, whether the run was cancelled, and why it stopped.

Money truth lives in separate counters, and they are not the same thing: `billableIbans` counts
rows written with `billable: true` (the price claim), `chargedEvents` counts the events really
charged, and `deliveredUncharged` counts rows that reached the dataset without money being taken
(the SDK wrote the row and the charge failed afterwards).

`pricingEventPriced` says whether the price list was readable (true / false / null), and
`billingActive` says whether billing was really on: `null` when the price could not be read,
`false` when the run is not pay-per-event or the SDK took nothing, `true` only when a charge
really happened. `billingActive: null` beside zero charges is not a healthy run.

When the price list cannot be read, the Actor does **not** hand the rows out for free: it stops
at the first billable row, that row and every row after it are written as free
`NOT_CHECKED_BILLING` status rows, `stopReason` reads `billing-price-unknown`, and nothing is
charged.

When the price list is readable but our event is not in it, billing is off for the run: the rows
are delivered free with `billable: false` and `stopReason` reads `billing-not-active`, the same
words phone-number-validator uses so the two summaries can be compared.

It is written to the named key-value store `iban-validator-summary` under **one key**,
`RUN_SUMMARY-<runId>`, with `runId` inside. The plain `RUN_SUMMARY` is left in the run's
own store only, where nothing else can overwrite it.

The named store is shared by the whole account, so the run-scoped key is the only one that
is safe to read: a plain key there would be overwritten by a parallel run. The cancellation case is covered too: if the run is
aborted, the summary is still written and says `cancelled: true` with
`stopReason: "cancelled"`, and the rows already delivered stay in the dataset.
