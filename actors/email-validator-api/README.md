# Email Validator API: format, domain and MX, with no guess about the mailbox

Send a list of addresses, get one row per address: is the format valid, does the domain
exist, does it publish a mail server, is it a disposable or a role address, is it on a
free provider, and is there a popular domain it looks like a typo of.

DNS is asked through a public DNS-over-HTTPS resolver, so the actor needs no API key, no
system resolver and no open UDP port, and it works inside a container.

## What this actor does not do, and why

It does **not** connect to the mail server and does **not** tell you whether a particular
mailbox exists. Port 25 is closed both on our side and inside the Apify container, so any
actor that claims a mailbox verdict is either paying for a third party service or
guessing. We chose to say what we can prove:

* `acceptsMail: true` means the domain publishes an MX record (or, with no MX, an A
  record), so mail to that domain has somewhere to go.
* `acceptsMail` never means "this mailbox is alive". That claim is not made anywhere in
  this actor, and a check that cannot be made comes back as a reason, not as a verdict.

## Input

* `emails` (required): a list of addresses. Surrounding spaces, angle brackets and a
  trailing dot or comma are trimmed, which is what a spreadsheet export adds.
* `checkDns` (default `true`): ask the resolver for MX. Turn it off to get the format
  verdict only: those rows come back as `SYNTAX_ONLY` and nothing is charged, because
  nothing was asked. Such a row keeps what was really computed about the address itself
  (`normalised`, `localPart`, `domain`, `domainAscii`, `syntaxValid`, `isDisposable`,
  `isRole`, `isFreeProvider`, `suggestion`), and it carries **no DNS verdict fields at
  all**: `domainExists`, `hasMx`, `mxHosts` and `acceptsMail` are null.
* `maxEmails` (default 50, maximum 200): how many addresses one run checks.

## Output

One row per address: `email`, `normalised`, `localPart`, `domain`, `domainAscii`,
`syntaxValid`, `domainExists`, `hasMx`, `mxHosts`, `acceptsMail`, `isDisposable`,
`isRole`, `isFreeProvider`, `suggestion`.

There is deliberately no `valid` field. It could only repeat `acceptsMail`, and buyers
read `valid: true` as "this mailbox exists", which nothing here can prove. `acceptsMail`
is the whole verdict and it is named for exactly what it means.

`domainAscii` is the punycode form that DNS actually uses, so a Cyrillic or accented
domain is reported both as you sent it and as the resolver sees it. `suggestion` is a
popular domain one or two edits away, which catches the `gmial.com` kind of typo.

## Price

You pay for one address that got a verdict from DNS (`email-checked`). The lowest
spending limit you can set per run is $0.05.

Verdicts that are paid, because they answer the question: `OK` (the domain accepts
mail), `NO_SUCH_DOMAIN` (the domain is not in DNS), `NO_MAIL_SERVER` (the domain exists
but publishes no MX and no A or AAAA record).

Free, with a status and a reason instead of a verdict: an address with a broken format
(no lookup was made, so there is nothing to pay for), a repeated address, an address
past `maxEmails`, an address the run had no time for, a resolver we could not reach,
and anything after the spending cap or after a failed charge.

A row that is not a verdict carries no `mxHosts` and no `acceptsMail`, so a free row can
never be read as a verdict.

## What the DNS answer actually said

* A domain whose MX record is `0 .` publishes a **null MX** (RFC 7505): it refuses all
  mail on purpose. That is a paid verdict, `NO_MAIL_SERVER`, and we do not fall back to
  the A record to pretend otherwise.
* A domain with no MX but with an address record still accepts mail through the implicit
  MX (RFC 5321 5.1), and the address record may be an **AAAA** as well as an A. A domain
  that only publishes IPv6 is not reported as having no mail server.
* Addresses are deduplicated by their **parsed identity**, not by the line they arrived
  on, so `a@münchen.de` and `A@xn--mnchen-3ya.de` are one address and are charged once.
* The local part is checked against ASCII rules only. An internationalised local part
  (EAI, a name written in Cyrillic or Chinese) comes back as `INVALID_SYNTAX`: SMTPUTF8
  is not supported here, and the row says so instead of pretending the address is bad.

## Status codes

`OK`, `INVALID_SYNTAX`, `SYNTAX_ONLY`, `NO_SUCH_DOMAIN`, `NO_MAIL_SERVER`,
`NOT_CHECKED_DNS_ERROR`, `DUPLICATE`, `NOT_CHECKED_LIMIT`, `NOT_CHECKED_TIME`,
`NOT_CHECKED_CAP`, `NOT_CHECKED_BILLING`, `NOT_CHECKED_ERROR`.

## Stop reasons

`dataset-write-error`, `billing-price-unknown`, `spending-cap`, `charge-error`,
`deadline`, `unexpected-error`.

A line the run never reached is named for the real cause: a spending cap gives
`NOT_CHECKED_CAP`, any other stop gives `NOT_CHECKED_ERROR` with the reason spelled out.

## Run summary

Written to the run store under `RUN_SUMMARY`, and a copy per run to the
`email-validator-summary` store under `RUN_SUMMARY-<runId>`. `counterMismatch` is `false`
only when every input line ended up in exactly one status.

The dataset counter is the only instrument that tells "the rows landed but the charge
failed" from "the write was refused", so it is read three times before and after a paid
write. If it still cannot be read, the summary says that **delivery and the charge are
both unknown**, counts those lines in `emailsUnverified` and `chargeUncertain`, and asks
you to check the dataset before re-running. It never claims that nothing was delivered or
that nothing was charged, because that claim is what makes a buyer pay twice.

The counter of addresses whose domain accepts mail is `emailsAcceptMail`, named for the
verdict rather than for the mailbox. An unexpected error still ends with a written
summary: the rows already written stay, the rest is named, and the run reports
`unexpected-error` instead of dying without a word.

## Notes on the lists inside

The disposable and free provider lists are short and hand checked, and they are in
`src/checks.py` where you can read them. A domain that is missing from them is a gap in
a list, not a wrong answer about the address, and the row still reports the DNS verdict.
