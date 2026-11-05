# VAT Validator API - VIES Check, Company Name and Address

**The problem this solves.** Every VAT checker has the same hidden failure: when
VIES (the EU registry that actually knows whether a number is registered) goes
down, or answers only part of the time, the checker reports `invalid`. The buyer
then flags a real customer as a tax fraud, blocks a legitimate invoice, or
rejects a supplier who is perfectly fine.

VIES is not a stable API. It goes down, it throttles, it answers
`actionSucceed: false`, and sometimes it answers with `valid: null` plus an
error code. That silence is not an answer, and this actor refuses to pretend
otherwise: a null is retried, and if VIES still has not spoken, the row says so.

## Three answers, never two

| Status | What it means | Charged |
|---|---|---|
| `VALID` | VIES confirmed the number is registered, with the company name and address | yes |
| `INVALID_VIES` | VIES answered and said the number is not registered | yes |
| `INVALID_CHECKSUM` | Arithmetic proves the number wrong, with the digits it should have | yes |
| `NOT_IN_VIES` | Country outside VIES (GB, NO, CH): the national checksum passes, registration unknown | yes |
| `INVALID_FORMAT` | Wrong length or characters for that country | no |
| `UNVERIFIED` | **VIES did not answer.** Not proof of anything, retry later | no |
| `NOT_CHECKED` | VIES switched off in the input: `valid` is `null`, only `checksumValid` is filled | no |
| `UNSUPPORTED_COUNTRY` | Prefix is not a VAT country we know | no |
| `NOT_ATTEMPTED` | The run stopped or ran out of time before this number | no |
| `ERROR` | Our own failure on this row, with the reason in `reason` | no |

A run where VIES was down returns rows, not a false verdict. You never pay for a
question nobody answered.

## Why the offline layer matters

Every number first goes through the national shape and checksum. A German number
with a bad check digit is rejected in microseconds, with the digit it should have
had, without spending a VIES call. That means bigger batches fit inside the VIES
rate limits, and a mistyped number is caught before it becomes a "not registered"
false alarm.

Checksums are implemented for AT, BE, CH, CZ, DE, DK, EE, ES, FI, FR, GB, HR, HU,
IE, IT, LU, NL, NO, PL, PT, SE, SI and XI. Where VIES covers the country, the
reference numbers are ones VIES reports as valid. GB, NO and CH are not in VIES,
so there the reference is the published national rule cross-checked against two
independent implementations: for GB both modular methods (a remainder of zero is
written as check digits 00, and 97 also appears in older records), the
twelve-digit branch form, and the GD and HA ranges for government departments and
health authorities; for Switzerland the MWST, TVA and IVA labels are dropped
before the sum, and for Norway the MVA label likewise.

Two rules keep this layer honest. Where the algorithm covers every number a
country issues, a failed check is a verdict, and the row names the digit it
expected. Where it does not, the row goes to VIES: the Dutch eleven-proof, for
example, is not satisfied by every number the Dutch registry issues, so a failure
there is reported in `checksumValid` and VIES decides, rather than a real firm
being rejected. For countries without an implemented checksum the field
`checksumImplemented` is `false` and VIES alone decides. A false `invalid` on a
real customer number is the one mistake this actor must never make.

## What you get per row

`rowIndex`, `input`, `country`, `vatNumber`, `status`, `valid`, `reason`,
`companyName`, `companyAddress` (what VIES returns), `checksumImplemented`,
`expected` (which digits were expected), `checksumValid`, `fromCache`,
`viesAnswered`, `charged`, `checkedAt`.

Rows arrive as they finish, so with `maxConcurrency` above 1 they are not in
input order. Sort by `rowIndex` when order matters.

## Price

Pay per event `vat-checked`: once per number whose verdict was actually produced.

Free: malformed numbers, unknown countries, numbers VIES could not answer,
answers taken from cache, and numbers pulled out of a pasted document without a
country prefix written as one. A prefix counts only when it is uppercase and
glued to the digits (`DE811569869`, `CHE432825998`). Everything else from free
text is checked and not charged: a document number looks exactly like a VAT
number, and "Invoice No 259137088" reads "No" as Norway if you let it. Numbers
written with spaces and a lowercase prefix ("de 811 569 869") are read as one
number and are still free.

## Capacity

One VIES question takes roughly 2 to 6 seconds, and the input cap is 4,000
numbers per run, which is also the realistic ceiling for one hour: 4,000
questions at `maxConcurrency: 8` take most of the hour, and at `3` they do not
fit at all. If the time budget runs out, the remaining rows come back as
`NOT_ATTEMPTED` with the reason, and nothing is charged for them. Split longer
lists into runs of about 2,000 at concurrency 3.

## Duplicates

The same number written twice, with or without spaces and prefix, is checked once
and billed once. `inputRows` in `SUMMARY` counts every row you sent, all spellings
included; `uniqueNumbers` counts the distinct numbers, and `vatNumbers` counts the
rows actually returned.

## If the dataset refuses a row

We retry the write twice. If it still fails, the row is written to the key-value
record `DELIVERY_FAILURES` with its number and whether it was charged, and the run
stops with `stopReason: delivery-error` - the record is what you need to claim a
refund, and stopping keeps us from piling charges into a broken dataset.

## Example

Input:

```
DE811569869
FR40303265045
DE811569860
XX123
```

Output (trimmed):

```
{"input": "DE811569869", "status": "VALID", "valid": true, "reason": "VIES confirmed the number"}
{"input": "FR40303265045", "status": "VALID", "valid": true, "companyName": "SA SODIMAS",
 "companyAddress": "11 RUE AMPERE 26600 PONT DE L ISERE"}
{"input": "DE811569860", "status": "INVALID_CHECKSUM", "valid": false, "expected": "last digit should be 9"}
{"input": "XX123", "status": "UNSUPPORTED_COUNTRY", "valid": null, "reason": "XX is not a known VAT country"}
```

## What this actor does not claim

It does not tell you whether a company is trustworthy, solvent, or has paid its
taxes. It tells you what VIES says today, and whether VIES actually said
anything. For GB, NO and CH, VIES has no record: those rows carry the national
checksum verdict and say so.

Answers are cached for `cacheMinutes` in the `vat-check-cache` key-value store of
the account that runs the actor, so a cached answer may come from an earlier run
of yours: it is the same public record of the same number. A cached answer is not
charged.

Ideas, bugs or a country you need checksum coverage for: open an issue on the
actor page.
