# How a niche gets chosen

The order is fixed: numbers about the market first, code last.

## 1. Size the store by API, not by the page

The built-in search is fuzzy: it matches descriptions too and mixes in other niches. An actor counts as relevant only when every word of the phrase appears in its **title**. Per phrase I record the number of relevant actors, total runs over 30 days, total users over 30 days, the leader's share of runs, "liveliness" (users divided by runs), the median price of the main event among the top six, and market size as runs multiplied by that price.

## 2. What makes me skip a niche

* market below $50 a month, even when the top is empty;
* the leader above 50% of runs (google maps reviews 99%, tiktok comments 83%, instagram hashtag 83%);
* liveliness of 0.2 to 0.8%: that is automation, not people (google maps 0.4%, tiktok comments 0.2%);
* a source that refuses a datacenter address and needs paid residential proxies.

Liveliness of 2 to 5% means real buyers (shopify 3.7%, amazon product 3.9%, tiktok trending 7.0%).

## 3. The long tail brings the first customers

The rich query is owned for years: `shopify product scraper` has 398 thousand runs behind its leader. The neighbouring sub-query is empty: `shopify products api` has 29 runs and a leader with no reviews. So the name comes from the empty sub-query, and quality is built for the rich one.

## 4. Pain before code

The leader's rating below 4.5 and its one and two star reviews, quoted verbatim into the niche file, not paraphrased. Then the pain is checked by hand rather than trusted: if `products.json` answers 403, the leader returns an empty dataset and charges anyway.

One or two pains that can be closed in code and do not need paid proxies. Closed **by default, in behaviour**, not as an option buried in the docs: a row that fails says why, a row without data is free, the buyer is never told to go and buy a proxy.

## 5. Source before code

No source, no product, however good the niche. Measured from a datacenter address, because that is where the actor will run, not from home.

## 6. Difference, not a copy

A copy always loses on popularity. The difference comes from the leader's own pain: a fallback chain of catalogue, sitemap and product page where the leader silently returns nothing; an "only what changed" mode with a snapshot; honest counters instead of a quiet zero.

## 7. Money

* A row is paid only when it carries data, and it is written and charged in one call.
* No data, blocked source, filtered out, unchanged: zero.
* The spending ceiling belongs to the buyer; I only set the lowest ceiling the platform allows.
* The name starts with the phrase people type, `seoTitle` stays under 60 characters, the description under 300 and starts with the pain, not with features.

## 8. The order I got wrong

My first filter went: weak leader, liveliness, price. It produced niches with no demand at all. Five ATS actors together bring about $12 a month at our price, which is an empty shelf, not an opportunity. The order is now money, liveliness, leader, source, pain, and code last.

One caveat I keep in view: runs multiplied by price counts one paid row per run, so it compares niches with each other, it is not a revenue forecast. An actor that returns a hundred rows per run earns a hundred times that.
