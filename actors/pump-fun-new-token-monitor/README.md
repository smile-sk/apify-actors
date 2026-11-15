# pump.fun New Token Monitor - Live Launches, Read From the Chain

A new token lives or dies in the first minute, and by the time a page refreshes it is already
old. Every site feed is somebody else's copy of the same chain, and most of them are geo-blocked
or rate-limited exactly when the market is hot.

This actor watches the pump.fun program on Solana mainnet itself. One row per token creation,
with the facts only the chain can give you.

## What you get, per token

- `mint`, `name`, `symbol`, `uri` - exactly as the creator wrote them
- `creator` - the address that launched it, from the creation event
- `createdAt` - the on-chain timestamp of the creation, ISO 8601 UTC, plus `ageSeconds`
- `devBuyTokens`, `devBuyPercent` - what the creator bought in the creation transaction itself
- `virtualSolReserves`, `realSolReserves`, `curveProgressPercent` - where the bonding curve stood
  at that second (the curve fills at 85 SOL before migration)
- `creatorTokensThisRun` - how many launches by the same creator this run had already seen
- `signature` - the creation transaction, so you can check every row yourself
- `source` - `stream` for the live logs subscription, `poll` for the HTTP fallback

## Why from the chain

- No page to refresh and no frontend to geo-block: the actor reads program logs.
- No per-token request for the base row: the creation event already carries the mint, the
  creator and the curve.
- Measured before building: `logsSubscribe` on the pump.fun program gave 5654 notifications in
  40 seconds and 16 creations, about 24 new tokens a minute (roughly 34 000 a day).

## Input

| field | default | meaning |
|---|---|---|
| `durationSecs` | 90 | how long to watch, 10 to 3600 |
| `maxTokens` | 200 | stop after this many rows, 0 = no limit |
| `minDevBuyPercent` | 0 | keep tokens the creator bought at least this share of |
| `maxDevBuyPercent` | 100 | drop tokens the creator grabbed more than this at birth |
| `onlyNewCreators` | false | keep only creators this run has not seen before |
| `nameContains` | "" | case-insensitive filter on the name |
| `symbolContains` | "" | case-insensitive filter on the symbol |
| `includeDevBuy` | true | read the creator's buy from the creation transaction |
| `rpcUrl` | public | your own Solana HTTP RPC |
| `rpcWsUrl` | public | your own Solana websocket RPC |
| `rpcPaceSecs` | 2.5 | minimum pause between two RPC calls; lower it on your own endpoint |

The watch window never overruns the platform: a run reads `ACTOR_TIMEOUT_AT`, and if the time left
is shorter than `durationSecs` it shortens the window to fit, leaving room for the summary. The
summary then carries `durationSecsRequested` and `durationCappedByPlatform: true`. This matters in
practice: an automatic check that allows five minutes killed a 300 s run as TIMED-OUT before it
could say anything, and a container restart in the middle can double the window, which is why the
default is 90 and not 300.

## Pricing

One charge per delivered token row, event `token-delivered`, whatever the row holds. A ten-minute
run that catches 240 launches is 240 charges; a quiet minute, a token dropped by your filters, a
duplicate mint or a row that failed to write costs nothing. The row is written and charged in one
call, so you never pay for a row you did not receive.

Apify puts a spending cap on the run when you do not set one (about $4.4), and the run stops at
the cap with `stopReason: "spending-cap"` instead of going green with a half-empty dataset. Raise
`maxTotalChargeUsd` in the run options for a longer stream. Compute of your own run is billed by
Apify separately from the rows.

## Run summary

In the named key-value store `pump-fun-new-token-monitor-summary` as `RUN_SUMMARY` and as
`RUN_SUMMARY-<runId>` (one named store is shared by the whole account, so two runs at once would
otherwise overwrite each other). The same summary also lands in the key-value store of the run
itself under `RUN_SUMMARY`.

It reports `tokensSeen`, `rowsDelivered`, `rowsUndelivered`, `filteredOut` with `filteredBy`,
`silentForSecs` (how long the feed has been quiet at the end of the run),
`duplicatesSkipped`, `reservesUnavailable`, `devBuyUnknown`, `notifications` (what the feed heard,
not what became rows), `signaturesSeen`, `signaturesUnread`, `chainGapSecs`,
`unparsableCreations`, `queuedNotDelivered`, `mode`, `streamFailures`, `stopReason` and
`datasetItemCount` (the dataset's own count, which the platform updates a few seconds late;
when it still trails `rowsDelivered` after a re-read, the summary says `datasetItemCountLag: true`
and reports `null` rather than a smaller number that would read as lost rows). The counters add up: `tokensSeen = rowsDelivered + filteredOut +
rowsUndelivered`.

Money is reported the same way: `chargedEvents` is what the platform actually charged,
`deliveredUncharged` counts rows that went out without a charge (a run whose pricing has no
`token-delivered` event), and `billingActive` says which mode the run was in. On a billed run
`chargedEvents` equals `rowsDelivered`.

## Nulls mean unknown, never zero

- `devBuyTokens` and `devBuyPercent` are `0` when the creation transaction was read and no
  tokens of this mint moved between the launch wallets: the creator really bought nothing, and
  that is the strongest signal a launch has. They are `null` only when the transaction could not
  be read at all (`includeDevBuy` off, or the endpoint refused), because not knowing is not the
  same as knowing zero.
- `virtualSolReserves`, `realSolReserves` and `curveProgressPercent` are `null` when the reserve
  fields of the event do not describe a curve that has just been born. Measured on 8 live
  creations: 5 reported exactly 30 virtual SOL, 3 reported a figure no newborn curve can hold
  (814 and 8901 SOL while the real token reserves were still untouched). The row keeps the mint
  and the time; the numbers we cannot stand behind are dropped.

## What this actor does not do

- It does not trade, sign or hold keys.
- It does not scrape the pump.fun website (that frontend answers 403 from some regions; the chain
  does not).
- It does not track migration to an AMM, holder concentration or a creator's whole on-chain past:
  `creatorTokensThisRun` counts only what this run has seen.
- It does not promise every launch under a heavy websocket: the public endpoint drops connections,
  and the actor switches to HTTP polling, marking those rows `source: poll`. That fallback is a
  sample of the flow, sized by `rpcPaceSecs`, and the summary reports what it missed.

## Rate limits and the fallback, honestly

The public endpoint rate-limits hard. Measured: a burst of one request every 0.3 s earned 429
within ten calls, while one call every 2.5 s (0.4 a second, the natural rate of creations) went
through 6 of 6. That is why `rpcPaceSecs` defaults to 2.5 and is yours to lower on a paid
endpoint. With `includeDevBuy` on, the actor makes one extra call per token in stream mode and
none at all in poll mode, where the transaction is already open.

The HTTP fallback samples the stream, it does not copy it. Measured: a page of 100 signatures
covers about 0.71 s of chain at 141 notifications a second, and opening those transactions at the
safe pace takes minutes. The summary says how much was missed instead of pretending otherwise:

- `signaturesSeen` - signatures the fallback listed;
- `signaturesUnread` - signatures it could not open (rate limit, network). They stay pending for
  the next cycle: a signature is remembered only after its transaction came back, so a 429 no
  longer hides a page forever;
- `chainGapSecs` - how many seconds of chain the last page covered;
- `unparsableCreations` - creations that carried our event discriminator but whose body this
  version could not decode;
- `queuedNotDelivered` - tokens seen but still waiting when the run hit `durationSecs`.

## Failure is loud

A run that receives nothing at all, or receives notifications but not a single creation, fails
with a message naming the endpoint instead of ending green with an empty dataset. Notifications
are counted as the feed hears them, so a live socket on a quiet minute is not reported as a dead
RPC. A run whose rows were all dropped by your own filters stays green and says in the run header
that the filter did it. Creations this version cannot decode are counted in
`unparsableCreations`, never silently dropped.
