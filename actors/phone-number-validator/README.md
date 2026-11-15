# Phone Number Validator - Bulk Validate, Format & Clean

Validate, format and clean phone numbers from any country with Google's libphonenumber, the library behind Android
and billions of contact records. Up to 10,000 lines per run. Every line comes back exactly as you sent it, with a
plain reason when it fails.

## What this Actor does not do

Stated up front, so nobody pays a run to find out:

- **No live carrier lookup.** The `carrier` field is allocation-time metadata from the bundled libphonenumber
  dataset. It can be empty for many countries. Empty means unknown, not "no carrier".
- **No reverse lookup.** This is not an owner, name, email or address finder.
- **No WhatsApp or Telegram registration check.** The actor never contacts WhatsApp, Telegram or any phone.
- **No guessing.** A local number written without a country code is a reason in the row, not a silent guess.
  Set `Default country` if your list is written that way.

## Three problems this Actor was built to fix

Taken from public reviews of other phone actors in this Store:

1. **Sample data instead of your data.** One actor returned "some another phone numbers" and the answer was
   "that is the sample data". Here an empty input fails the run with a message. There is no demo row anywhere.
2. **No bulk.** One actor accepted a single number per call. Here a list or a pasted column is the normal input,
   order preserved, up to 10,000 lines.
3. **Paying for junk.** Here you are charged only when a delivered line carries a new valid number. Invalid lines,
   unparsed lines and duplicate lines cost nothing, and each of them still appears in the output with its reason.

## Input

```json
{
  "numbers": [
    "+1 (202) 555-0173 ext. 42",
    "tel:+44 20 7946 0018",
    "+34 (0) 91 123 45 67",
    "044 123 45 67",
    "not a phone"
  ],
  "defaultCountry": "UA"
}
```

`numbers` is a list, `text` is a pasted block used only when the list is empty. Messy lines are accepted:
`tel:` links, labels such as Phone, Tel., Call, Mobile, Mob, `(0)` trunk prefixes after the country code in any
country (for example `+34 (0) 91 ...`, `+7 (0) 495 ...`, `+1 (0) 202 ...`), `00` international prefixes,
spaces, dashes, dots, slashes, parentheses, non-breaking spaces, full-width digits, Arabic-Indic and Persian
digits, and extensions written as `ext`, `extension`, `x`, `#`, `dob`.

**One warning about `Default country`.** It is meant for lists written in the local format, such as `044 123 45 67`
for Ukraine. If you set it on a list of dates, order numbers or other digit strings, some of them can match a real
number format of that country and come back `VALID`. With no default country, a line without a country code is a
reason, not a guess, and none of the 30 noise samples we test (dates, timestamps, IBAN, VAT, coordinates, prices)
becomes valid.

## Output

One row per input line:

| Field | Meaning |
|---|---|
| `index` | Line number, starting at 1, in the order you sent it. A blank line inside the list keeps its row, so indexes still match your source list. |
| `inputAsGiven` | The line exactly as received, so rows join back to your list |
| `status` | `VALID`, `POSSIBLE_ONLY`, `INVALID`, `NO_NUMBER`, `ERROR`, `NOT_CHECKED_CAP`, `NOT_CHECKED_BILLING`, `NOT_CHECKED_LIMIT` |
| `reason` | Plain-language explanation for every non-valid line |
| `valid`, `possible` | libphonenumber verdicts |
| `type` | `mobile`, `fixed_line`, `fixed_line_or_mobile`, `voip`, `toll_free`, `premium_rate`, `shared_cost`, `uan`, `pager`, `voicemail`, `personal_number`, `unknown` |
| `countryCode`, `countryName`, `callingCode` | ISO-3166 alpha-2, country name, `+380` |
| `e164`, `international`, `national`, `rfc3966` | All four formats |
| `extension` | Parsed extension, or null |
| `smsCapable` | true only for valid mobile and fixed-line-or-mobile numbers |
| `timeZones` | Time zones of the number's region, useful for calling hours |
| `regionDescription` | City or area, when the metadata knows it |
| `carrier` | Allocation-time carrier from the offline dataset, or empty |
| `otherNumbers` | Additional valid numbers found in the same line |
| `duplicateOfIndex` | Index of the first occurrence, when this number already appeared |
| `billable` | The price claim for this row: the event is in the price list and the row carries a new valid number. What was actually charged is counted in the run summary, not in the row |
| `billingInactive` | true when billing was already off for this row (not pay-per-event); the row was delivered free |

## Billing

`number-validated` costs **$0.80 per 1,000 delivered lines** (`$0.0008` each). A line is one event, however many numbers it carries - so the price follows delivered lines, not numbers.

- Charged once per delivered line that carries at least one new valid number: money follows delivery, and a
  line is written and billed in the same call.
- **One line is one event, however many numbers it holds.** A line saying `Call +1 202 555 0173 or
  +44 20 7946 0018` costs one event; the second number is free, listed in `otherNumbers` and counted in
  `extraNumbersFree` in the summary.
- Invalid, unparsed, blank and duplicate lines: free.
- Example: 1,000 lines, 700 valid, 60 duplicates of earlier lines, 240 invalid.
  700 - 60 = 640 charged lines = **$0.51**. The same list at a per-line price of $0.60 per 1,000 would cost
  $0.60 and return nothing for the 240 rejects.

If a run hits the spending cap set on it (`maxTotalChargeUsd`), the actor stops charging and every unchecked line
is delivered as a `NOT_CHECKED_CAP` row with a reason, including the line the cap landed on. If billing itself
fails after a row was written, the run stops the same way but marks the rest `NOT_CHECKED_BILLING`, so a billing
problem is never reported as your cap. No line is ever dropped silently.

If the run is not pay-per-event at all (for example the first run right after a new version, before a price is
enabled for it), no event can be charged: every row is still delivered, `billingInactive` is true on them,
`billingActive` is false in the summary and `stopReason` is `billing-not-active`. You never get a false
"spending cap" for that.

## Limits

- 10,000 lines per run.
- Lines beyond the limit are reported as `NOT_CHECKED_LIMIT` rows (up to 5,000 of them) and never charged.
  The summary shows `truncatedLines` when the list was longer.

## Run summary

`RUN_SUMMARY-<runId>` is written to the named key-value store `phone-validator-summary` (one key per run,
`runId` inside). The plain `RUN_SUMMARY` is left in the run's own store only: the named store is shared by the whole
Apify account, so a plain key there would be overwritten by a parallel run.

The summary holds counts by status, unique valid numbers, duplicates and extra numbers found inside one line (both
free), `chargedEvents` (what was actually charged) and `deliveredUncharged` (rows delivered with no money taken).
The dataset row carries `billable`, which is the price claim decided before the write; the summary carries the fact.

`pricingEventPriced` says whether the price list was readable: `true`, `false`, or `null` when it could not be read.
`billingActive` is `null` in exactly that case, never `true`, so a monitor cannot read "billing works" from a run
that charged nothing. `stopReason` names the outcome: `completed`, `spending-cap`, `charge-error`,
`billing-not-active`, `billing-price-unknown` or `cancelled`, beside `cappedNotCharged`, `notCheckedError`,
`chargeErrors` and `truncatedLines`.

## Accuracy

Validation is offline and follows the bundled libphonenumber metadata. `VALID` means the number matches an assigned
range for its country in this build. Ranges assigned after the dataset was built can come back as `INVALID`; the
reason says the prefix is not in range. Nothing here proves that a number currently answers, and no number is ever
dialled.
