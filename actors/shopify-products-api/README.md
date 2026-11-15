# Shopify Products API - Catalog, Prices & Stock

Read a Shopify store's catalog the way the store itself publishes it: product title, price,
compare-at price, currency, stock, variants, tags and images. Point it at a store and get one
row per product, or turn on `onlyChanges` and get only what moved since the last run.

## Why this Actor exists

Three things go wrong with store catalogs, and each one has a cost:

1. **The catalog endpoint is closed.** A store can answer `403` on `/products.json` and still
   be a normal, healthy shop. A scraper that stops there returns an empty dataset, and an empty
   dataset reads as "this shop has no products".
2. **A page with no price still gets billed** by scrapers that charge per product attempt.
3. **A one-shot export says nothing about movement.** For pricing and stock you need to know
   what became cheaper, what sold out and what came back.

This Actor walks a fallback chain instead of one endpoint, never charges for a product it could
not price, and keeps a snapshot per store so `onlyChanges` can answer the third question.

## Input

| field | meaning |
|---|---|
| `stores` | store domains (`allbirds.com`, `gymshark.com`) or product URLs (one product each) |
| `maxProductsPerStore` | hard stop per store, `250` is one catalog page, `0` means no limit (stops at 40 pages) |
| `onlyChanges` | deliver only new products, price moves and stock moves against the stored snapshot |
| `inStockOnly`, `minPrice`, `maxPrice`, `nameContains` | filters, applied before delivery |
| `includeVariants` | write the variant list into each row, or keep rows light |
| `requestPaceSecs` | polite pause between stores |

The same store typed twice, or typed once with and once without `www`, is read once and charged
once: `storesDeduplicated` says how many input lines were folded away. Two product URLs of one
store are two different lines and both stay, unless the whole catalog of that store is in the
input too: then the catalog already carries those products, so the product lines are folded as
well instead of delivering and charging the same product twice.

## How a store is read

The chain, cheapest first. The first path that answers wins, and the row says which one it was
in `source`:

1. `/meta.json` for the shop name and the currency (may be missing, then `currency` is `null`
   unless a product page supplies it);
2. `/products.json` page by page, 250 products at a time, full variants and stock;
3. `/products/<handle>.json` when the input is a product URL;
4. `/sitemap.xml` then `sitemap_products_N.xml` to discover product addresses when the JSON
   catalog is closed, then each product page, four at a time;
5. the product page JSON-LD, both `Product` with `offers` and `ProductGroup` with
   `hasVariant[].offers` (the shape gymshark.com serves).

A `3xx` answer is never parsed as a page: it is a status with the `Location` it wanted. One
redirect is followed, and only inside the same registrable domain, so a store's `www` redirect
works while a redirect to somebody else's host does not.

A store that refuses every path becomes a row with `status: NOT_CHECKED_BLOCKED`,
`reason` and `httpStatus`, and it is not charged. A host that resolves into a private or
loopback network is refused the same way as `NOT_CHECKED_PRIVATE_TARGET`, which also covers
IPv4-mapped IPv6 literals such as `::ffff:169.254.169.254`.

A sitemap is content the store writes, not an address you typed, so every address it names is
checked before it is fetched: it has to stay on the same registrable domain and it has to
resolve to a public address. Refused entries are never requested and are counted in
`skippedOffsite`. Without that check, a sitemap naming
`http://169.254.169.254/latest/meta-data/...` or `http://127.0.0.1:8080/...` would turn a run
into a request to the container's own metadata service.

## Output

One row per product: `store`, `handle`, `title`, `price` (lowest variant price), `priceMax`,
`compareAtPrice`, `currency`, `availability`, `variantCount`, `variants`, `productType`,
`vendor`, `tags`, `productUrl`, `imageUrl`, `updatedAt`, `source`, and with `onlyChanges` on,
`change` and `previousPrice`.

Rows that are not a delivered product carry a `status`: `NO_PRICE` (the store publishes no
price), `NOT_CHECKED_BLOCKED`, `NOT_CHECKED_NOT_SHOPIFY`, `NOT_CHECKED_PRIVATE_TARGET`,
`NOT_CHECKED_ERROR` and `INVALID_INPUT`.

`change` is one of `new`, `price-down`, `price-up`, `out-of-stock`, `back-in-stock`. Stock
outranks price in that field: a product that got cheaper and sold out in the same run is
reported as a stock move, because one string per row cannot carry both honestly.

The snapshot follows what was **read**, not what was sold: a product your filters dropped still
remembers its price and stock, so `inStockOnly` plus `onlyChanges` reports a product coming back
in stock instead of calling it unchanged forever. Two kinds of product stay out of the snapshot
on purpose: the ones the spending cap stopped on, and the ones whose write the dataset counter
could not confirm. Those stay `new`, so the next run offers them again.

## Money

This Actor is pay per event: one charge for `product-delivered`, a product row with a price.
Not charged: a product without a price, a blocked or non-Shopify store, a product page that
failed, a product dropped by your filters, and a product that did not change under
`onlyChanges`. The summary separates charged rows from delivered-but-uncharged ones.

A row is written and charged in one SDK call, so a row cannot be paid for and then lost. If the
charge fails after the row was already stored, the row stays delivered, the summary counts it as
`deliveredUncharged` and `chargeUncertain`, and the Actor does not push it a second time.

When a write raises and the dataset counter does not confirm the row, the outcome is called
unknown instead of lost: `productsUnverified` counts it, `writeFailures` counts the failed
attempts, and the row is not called missing while it may be sitting in the dataset. Dataset
metadata lags a write, so a counter that has not moved yet proves nothing either way.

## Spending cap

The platform gives every run a default cap of about $4.4 when the buyer does not set one. Set
`maxTotalChargeUsd` yourself if you need a hard cost ceiling: 1000 delivered products at
$0.002 is $2.00. When the cap lets nothing through, the run stops with `stopReason:
spending-cap` and delivers the rows it already had.

## The summary

The run summary is stored in the named key-value store `shopify-products-api-summary` under
`RUN_SUMMARY-<runId>`, and also under plain `RUN_SUMMARY` and in the run's own store. One named
store is shared by the whole account, so two runs at once would overwrite the plain key; the
`<runId>` key is the one to read with a script.

Three sets of counters, and each set adds up:

- products: `productsSeen == productsDelivered + noPrice + filteredOut + productsUndelivered +
  productsUnverified`. Every product the run reached lands in exactly one of those five, and
  `productsNotChecked` is the number it never reached because `maxProductsPerStore` or the
  spending cap stopped it.
- stores: `storesAsked == storesOk + storesBlocked + storesEmpty + invalidInputs +
  storesNotChecked`, where `storesAsked` counts the input after duplicate lines are folded
  (`storesDeduplicated`), so a buyer who pasted one store twice is not charged twice.
- per store: `productsByStore` gives `read`, `delivered`, `undelivered`, `unverified`,
  `notChecked`, `noPrice`, `filteredOut`, `pages`, `pagesFailed`, `skippedOffsite` and
  `truncated` with `truncatedReason`. `truncated` is true when `maxProductsPerStore` stopped the catalog, when a
  later catalog page failed, or when a product page could not be read, so an incomplete catalog
  is never reported as a complete one. `truncatedStores` counts how many stores were cut.

If a set does not add up, the Actor writes `counterMismatch` or `storeCounterMismatch` into the
summary and logs it, rather than rounding the difference away.

`pagesFailed` counts product pages that could not be read, and they are listed in the dataset as
a single `NOT_CHECKED_ERROR` row with a count. A run that delivers nothing always says why: no
store in the input, every store refused with its HTTP codes, a store that answered with an empty
catalog, a product catalog that was not parseable, or your own filters.

## What this Actor does not do

- It does not buy, add to cart, log in or use an account: only public storefront endpoints.
- It does not read a store's admin API, orders, customers or private data.
- It does not bypass a store that blocks it. A full block is reported, not worked around.
- It does not promise the whole catalog when `maxProductsPerStore` stops it: `productsByStore`
  shows how many products were read per store and `truncated` says the catalog was cut.
- It does not follow a redirect to another domain, it does not read private addresses, and it
  does not fetch an address that came out of a sitemap unless it is on the store's own domain.
- It does not give prices in a currency the store did not state: `currency` is `null` rather
  than assumed.
- Sitemap mode is slower than the JSON catalog: one page per product is fetched, so 250
  products means 250 page reads.

## Notes on rate limits

Pages are read four at a time and stores are read one after another with `requestPaceSecs`
between them. A `429` or a `5xx` is retried once after a second; if it still refuses, the store
is reported with the status instead of being read halfway. If you see `NOT_CHECKED_BLOCKED`
with a `429`, raise `requestPaceSecs` and run again.
