# SSL Certificate Checker API - Expiry, Chain & Distrust

The most installed SSL checkers in the Store charge a run-start fee even
for an empty run, fold "expired", "wrong hostname" and "the checker's own
IP was blocked" into one F letter, and know nothing about the biggest
certificate event of 2027.

This actor fixes that.

## The pain we remove

- **No run-start fee.** You pay $0.001 per delivered host verdict. An
  empty input, an invalid host or an unreachable one costs you nothing.
- **Blocked is not F.** If the server closes the connection before a
  handshake (rate limits, cloud-IP blocks), you get `verdict: unreachable`
  with the reason, and it is not charged. The leader of this niche admits
  in its own FAQ that valid domains get an F because of cloud IP blocks.
- **SHA-1 intermediates are not "invalid".** Browsers still accept chains
  whose intermediate is signed with SHA-1; checkers that blindly apply a
  stricter policy call such sites invalid. We validate the signature path
  step by step and mark the row `weakSignature: ["SHA1"]` instead.
- **One verdict per problem.** `ok`, `breaks-2027-04-15`,
  `invalid-today`, `unreachable` are separate, each with a reason field.
- **The 15 Apr 2027 Mozilla root distrust, in the same run.** On that day
  Mozilla removes the websites trust bit of 19 old roots (Go Daddy G2,
  Starfield G2, USERTrust, COMODO, GlobalSign R3 and others) under its
  15-year key-lifecycle rule. Firefox, curl, Python, Node.js and most
  Linux clients will reject chains that only reach those roots. This
  actor validates every host twice: against the current Mozilla store and
  against the same store without the removed roots (18 of the 19 are in
  the current certifi bundle; the actor strips them by their published
  SHA-256 hashes, and the missing one is absent from certifi anyway).

## How it works

It opens a real TLS handshake, collects the certificate chain the server
sends, and runs full WebPKI path validation (cryptography library,
hostname included) twice. The root list is Mozilla's, as shipped in
certifi. Source of the removal table:
https://wiki.mozilla.org/CA/Root_CA_Lifecycles

Verdicts:

- `ok` - valid today and still valid after the 2027 removals.
- `breaks-2027-04-15` - valid today, but the chain only reaches a root
  that Mozilla distrusts for websites on 15 Apr 2027.
- `invalid-today` - the chain fails today (expired, wrong hostname,
  missing intermediate, untrusted root), with the validation error.
- `unreachable` - no TLS handshake happened (timeout, connection refused,
  blocked IP, private address, invalid input). Free.

Every row carries the anchor root name and SHA-256, whether it is removed
and when, the leaf issuer, validity window, days to expiry, DNS and IP
SAN lists, weak-signature marks, TLS version and cipher.

IP addresses work as hosts: `1.1.1.1` is matched against the certificate's
IP SAN, not the DNS SAN.

## What we do NOT claim

- Only the chain your server sends is judged. Firefox can sometimes build
  an alternative path through preloaded intermediates; scripts, servers
  and containers cannot, and they are what this check protects.
- This is the Mozilla schedule. Chrome (Chrome Root Store), Apple and
  Microsoft keep their own lists and timelines.
- TLS only, port 443 by default, or any port you append
  (`api.example.com:8443`). STARTTLS on port 25/587 is not supported yet.

## Pricing

Pay-per-event: **$1.00 / 1,000 host checked** = $0.001 per delivered
verdict row. `unreachable` rows, invalid input and cache hits are
delivered free. There is no run-start fee.

Cache: a charged host verdict stays cached in your store for
`cacheMinutes` (default 1440) and then expires. A repeat check within
that window is delivered from cache and is not charged. A verdict is
cached only after it was charged, never before.

Set a run spending limit when checking long lists: the run stops when it
is reached, and hosts that were not read are listed in the SUMMARY row.

## Input

```json
{
  "hosts": ["www.godaddy.com", "sectigo.com", "api.example.com:8443"],
  "cacheMinutes": 1440,
  "maxConcurrency": 8,
  "timeoutSeconds": 10
}
```

Full URLs are cleaned automatically (scheme and path removed, `www`
preserved).

## Example

`www.godaddy.com` (checked 27 Oct 2026):

```json
{
  "host": "www.godaddy.com",
  "verdict": "breaks-2027-04-15",
  "validToday": true,
  "validAfter2027RootRemovals": false,
  "anchorRootName": "CN=Go Daddy Root Certificate Authority - G2",
  "anchorRootRemovedOn": "2027-04-15",
  "daysToExpiry": 195,
  "tlsVersion": "TLSv1.3"
}
```

`sectigo.com` in the same run is `ok` through Sectigo Public Server
Authentication Root R46, which stays trusted.
