# App Details API: iOS App Store and Google Play in one run

One list of apps in, one row per app out, with the numbers you would otherwise copy by
hand: rating, number of ratings, installs, price, genres, version, last update, content
rating, in app purchases, minimum OS, icon and store link.

Both stores are read by their public endpoints. No API key, no proxy, no browser, no
login, so nothing outside can be revoked to stop it working.

## Why this exists

Most actors in this niche cover one store. If your list has both iPhone and Android
apps, you either buy two actors, or you keep two spreadsheets and join them yourself. This actor takes a mixed list and decides per item which store to ask.

## Input

* `apps` (required): App Store numeric ids, `apps.apple.com` links, Google Play package
  names, `play.google.com` links, or plain dotted names such as `com.instagram.android`.
  A dotted name is looked up in Google Play first and in the App Store bundle id second,
  so a mixed list needs no extra field.
* `country` (default `us`): two letter store country. Ratings, prices and availability
  are reported for this country, and the App Store request really does ask that
  storefront, so an app that is not sold there comes back as not found.
* `lang` (default `en`): language of the Google Play page.
* `platform` (default `both`): which store to try for a dotted name. A numeric id or a
  store link already says its store, and this field does not override it.
* `maxApps` (default 25, maximum 100): how many apps one run reads.
* `includeDescription` (default false): add the long store description to each row.

## Output

One row per delivered app: `platform`, `appId`, `storeId`, `title`, `developer`,
`rating`, `ratingsCount`, `reviewsCount`, `installs`, `installsExact`, `price`,
`currency`, `free`, `genres`, `version`, `released`, `lastUpdated`, `contentRating`,
`adSupported`, `offersInAppPurchases`, `minimumOs`, `fileSizeBytes`, `languages`,
`country`, `iconUrl`, `storeUrl`, and `billable`.

Fields that a store does not publish are `null`, not zero. The App Store does not publish
installs or the number of written reviews, Google Play does not publish the app file
size or the language list. `minimumOs` is reported for iOS only: the Play client does
not expose the Android version requirement at all. `installs` is the store bracket as text (`5,000,000,000+`),
`installsExact` is the number Google Play reports behind it.

## Price

You pay for one delivered app detail (`app-detail`), and only when the row reached the
dataset. The lowest spending limit you can set per run is $0.05.

Free, with a status and a reason instead of data: an empty or unusable input, a repeated
app, an app the store does not have, an app past `maxApps`, an app the run had no time
for, a store that answered an error, and anything after the spending cap or after a
failed charge.

A row that could not be delivered carries no `appId`, `title`, `iconUrl` or `storeUrl`,
so a free row can never be mistaken for a delivered one.

`billable` on a row is the price claim taken before the write, not a receipt: the money
actually charged lives in `chargedEvents` and `deliveredUncharged` in the run summary.
An empty `apps` list is an input mistake and the run stops with a message naming the
field, rather than returning a green run with nothing in it.

## What this actor does not do

* It does not read reviews. That is `app-reviews-api`.
* It does not download screenshots or the app binary, it only returns their links.
* It does not track rank, keywords or search position over time.
* It does not read paid app price history.
* It does not look at your competitor's metrics beyond what the store page shows.

## Status codes

`OK`, `INVALID_INPUT`, `DUPLICATE`, `NOT_FOUND`, `NOT_CHECKED_LIMIT`,
`NOT_CHECKED_TIME`, `NOT_CHECKED_ERROR`, `NOT_CHECKED_CAP`, `NOT_CHECKED_BILLING`.

## Stop reasons

`charge-error` (the charge raised: when the dataset counter answers, the batch is
delivered and counted in `deliveredUncharged` and `chargeUncertain`; when the counter
cannot be read, delivery and the charge are **both unknown**, the batch is counted in
`deliveredUnverified` and nothing is claimed as sold, so check the dataset before
re-running), `dataset-write-error` (the dataset refused the rows: nothing was delivered
and nothing was charged; when the counter grew by only part of the batch, the note says
how many of the rows landed and the rest is counted in `deliveredUnverified`),
`billing-price-unknown` (the event price could not be read, so nothing is delivered and
nothing is charged), `spending-cap` (the buyer's spending limit was reached), `deadline`
(the run was close to being stopped by the platform).

On a failed charge the dataset counter is the only proof of what happened, so it is read
three times over about a second and the highest reading wins. A lag longer than that
window still reads as zero; in that narrow case the run cannot tell a refused write from
a slow counter, and the note says which case it assumed.

## Run summary

The summary is written to the run store under `RUN_SUMMARY`, and a copy per run goes to
the `app-details-summary` store under `RUN_SUMMARY-<runId>`. It carries the counters
listed in the output schema, including `counterMismatch`, which is `false` only when
every input line ended up in exactly one status. `appsCutByBudget` is the tail a
money stop cut (the spending limit, an unreadable price, a failed charge),
`appsCutByStop` is the tail any other stop cut (a dataset write error, for example) and
those rows carry the real reason instead of a money claim, `appsCutByDeadline` is the tail
the clock cut: they are counted apart because they mean different things to the buyer.
