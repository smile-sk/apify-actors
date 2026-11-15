# Paid actors for the Apify Store

Fifty small, single-purpose API actors. Each one does a narrow job well enough that somebody pays for the row: read a job board, validate an IBAN, transcribe a video, take a screenshot, check a certificate. Python, one Dockerfile per actor, no key of your own needed on the buyer's side.

![The icon set, drawn to one template: dark navy tile, white silhouette, a single cyan accent](assets/icons.jpg)

## The rule that shapes every actor

**Charged equals delivered.** That sounds obvious until you see how a paid actor usually breaks: the platform writes the row first and charges after, so a failed charge leaves a free row, and a failed write leaves a charge for nothing. Every actor here treats that as the main risk, not an edge case.

* A paid row is written and charged in a single call, so the dataset cannot grow without the money moving.
* A row that carries no data is free and says why: `NOT_FOUND`, `EMPTY`, `INVALID`, `NOT_CHECKED_*`. There is no silent "zero results" that still bills.
* The spending budget is reserved before the write, never discovered after it.
* When the charge call fails, the dataset counter is read three times and the summary tells three cases apart: written and not charged, not written at all, unknown. It never claims "nothing happened" on a guess.
* Counters have to reconcile: `asked = processed + notChecked`, and `counterMismatch` turns true on a real gap instead of being rounded away.

## Before an actor is published

* `scripts/preflight.py` runs 53 machine checks over the tree and the store card: schema shape the platform accepts, declared memory, price event titles that read as nouns, README promises that match the code, no secrets, no Cyrillic, no long dashes, test blocks that actually execute, SDK calls that match the installed 4.2 signatures.
* 1,477 test functions in 114 files. The money tests drive a real `main()` against a fake platform SDK that fails before or after a write, lags its metadata and cuts a batch at the spend limit.
* Live probes against the real source, including empty and garbage input, because a green unit test proves the code, not the world.
* A real platform build, because a schema can pass every local check and still be refused by the platform.

## The actors

### Job boards (8)

Read a company's open roles straight from its ATS, with descriptions, offices and remote flags.

| Actor | What it returns |
|---|---|
| [`company-jobs`](actors/company-jobs) | Company Jobs API: Every Open Role From Any Domain |
| [`linkedin-jobs-api`](actors/linkedin-jobs-api) | LinkedIn Jobs API - Job Listings with Full Descriptions |
| [`personio-jobs-api`](actors/personio-jobs-api) | Personio Jobs API - All Openings, Descriptions, Any Board |
| [`recruitee-jobs-api`](actors/recruitee-jobs-api) | Recruitee Jobs API - All Offers, Every Office, Descriptions |
| [`smartrecruiters-jobs-api`](actors/smartrecruiters-jobs-api) | SmartRecruiters Jobs API - Open Postings & Descriptions |
| [`teamtailor-jobs-api`](actors/teamtailor-jobs-api) | Teamtailor Jobs API - All Offices, Remote & Expiry |
| [`workable-jobs-api`](actors/workable-jobs-api) | Workable Jobs API - Every Opening, All Locations |
| [`workday-jobs`](actors/workday-jobs) | Workday Jobs API: Every Opening From Any Career Site |

### YouTube (6)

Views, lengths, comments, captions and search results without an API key of your own.

| Actor | What it returns |
|---|---|
| [`youtube-channel-api`](actors/youtube-channel-api) | YouTube Channel API - Videos, Views & Subs |
| [`youtube-comments-api`](actors/youtube-comments-api) | YouTube Comments API - Replies, Likes & Authors |
| [`youtube-search-results-api`](actors/youtube-search-results-api) | YouTube Search API - Video Results by Keyword |
| [`youtube-shorts-api`](actors/youtube-shorts-api) | YouTube Shorts API - Channel Shorts with Views |
| [`youtube-video-api`](actors/youtube-video-api) | YouTube Video API - Full Metadata Without a Key |
| [`yt-transcript`](actors/yt-transcript) | YouTube Transcript API: Bulk Captions, Fast |

### Social and newsletters (3)

Posts from Bluesky and Telegram, full archive of a Substack publication.

| Actor | What it returns |
|---|---|
| [`bluesky-posts-api`](actors/bluesky-posts-api) | Bluesky Posts API: profile posts with likes, reposts, replies |
| [`substack-posts-api`](actors/substack-posts-api) | Substack Posts API - Newsletter Archive, Likes & Authors |
| [`telegram-channel-scraper`](actors/telegram-channel-scraper) | Telegram Channel Scraper - Posts, Views, Reactions |

### App stores and reviews (4)

App details and reviews from the iOS App Store and Google Play, Trustpilot reviews, Shopify catalogues.

| Actor | What it returns |
|---|---|
| [`app-details-api`](actors/app-details-api) | App Details API - iOS App Store and Google Play |
| [`app-reviews-api`](actors/app-reviews-api) | App Reviews API - iOS App Store & Google Play Reviews |
| [`trustpilot-reviews-api`](actors/trustpilot-reviews-api) | Trustpilot Reviews API - Reviews, Ratings & Company Replies |
| [`shopify-products-api`](actors/shopify-products-api) | Shopify Products API - Catalog, Prices & Stock |

### Web and SEO (10)

Crawl checks, page scores, meta tags, tech stack, screenshots, WHOIS, RDAP and the Wayback Machine.

| Actor | What it returns |
|---|---|
| [`broken-link-checker`](actors/broken-link-checker) | Broken Link Checker API - 404s, Redirects & Retries |
| [`core-web-vitals-audit`](actors/core-web-vitals-audit) | Core Web Vitals Audit - LCP, CLS, TBT, No API Key |
| [`seo-audit-api`](actors/seo-audit-api) | SEO Audit API - On-Page Score, Issues and Fixes |
| [`metadata-og`](actors/metadata-og) | Open Graph & Meta Tags API - Link Preview, OG Image |
| [`tech-stack-detector`](actors/tech-stack-detector) | Tech Stack Detector API - CMS, Ecommerce & Frameworks |
| [`website-screenshot`](actors/website-screenshot) | Website Screenshot API: Full-Page PNG & Visual Diff |
| [`website-contacts`](actors/website-contacts) | Website Contact Finder: Emails, Phones & Socials From Any Site |
| [`domain-availability-api`](actors/domain-availability-api) | Domain Availability Checker - Bulk RDAP Lookup |
| [`whois-domain-lookup`](actors/whois-domain-lookup) | WHOIS Domain Lookup API - RDAP, DNS, MX, TXT, Expiry |
| [`wayback-machine-api`](actors/wayback-machine-api) | Wayback Machine API - First Seen, Last Seen & Versions |

### Uptime and security (3)

Is the site up, is the certificate valid, is the CVE known and exploited.

| Actor | What it returns |
|---|---|
| [`uptime-monitor`](actors/uptime-monitor) | Uptime Monitor API - Website Status, SSL & Response Time |
| [`ssl-certificate-checker`](actors/ssl-certificate-checker) | SSL Certificate Checker API - Expiry, Chain & Distrust |
| [`cve-lookup-api`](actors/cve-lookup-api) | CVE Lookup API - NVD, CVSS, EPSS, CISA KEV |

### Validators (4)

Checksum and registry checks for the fields that cost money when wrong.

| Actor | What it returns |
|---|---|
| [`email-validator-api`](actors/email-validator-api) | Email Validator API - Syntax, MX and Domain Checks |
| [`iban-validator`](actors/iban-validator) | IBAN Validator: Checksum, Country & SEPA |
| [`phone-number-validator`](actors/phone-number-validator) | Phone Number Validator - Bulk Validate, Format & Clean |
| [`vat-validator`](actors/vat-validator) | VAT Validator API - VIES Check, Company Name & Address |

### Media and documents (7)

Images, scanned PDFs, audio and speech, in bulk.

| Actor | What it returns |
|---|---|
| [`image-compressor`](actors/image-compressor) | Image Compressor API - Batch JPG, PNG, WebP to Target Size |
| [`image-converter`](actors/image-converter) | Image Converter & Compressor: WebP, AVIF, HEIC, JPG, PNG |
| [`image-ocr-api`](actors/image-ocr-api) | Image OCR API - Text and Word Boxes from JPG, PNG, WebP |
| [`pdf-extractor`](actors/pdf-extractor) | PDF Text Extractor API - Text, Tables & Page Ranges |
| [`pdf-ocr-api`](actors/pdf-ocr-api) | PDF OCR API - Scanned PDF to Text and Word Boxes |
| [`speech-to-text-transcription`](actors/speech-to-text-transcription) | Speech to Text Transcription - Whisper, Audio to Text |
| [`text-to-speech-api`](actors/text-to-speech-api) | Text to Speech API - Neural Voices, No API Key |

### Data and research (5)

Papers, packages, articles, weather and on-chain launches.

| Actor | What it returns |
|---|---|
| [`arxiv-paper-api`](actors/arxiv-paper-api) | arXiv Paper API - Metadata by ID, No Key, Free for Missing |
| [`pypi-package-api`](actors/pypi-package-api) | PyPI Package API - Versions, Dependencies & License |
| [`wikipedia-article-api`](actors/wikipedia-article-api) | Wikipedia API - Article Text, Summary, Categories & Pageviews |
| [`weather-forecast-hourly-api`](actors/weather-forecast-hourly-api) | Weather Forecast API - Hourly Series by Coordinates |
| [`pump-fun-new-token-monitor`](actors/pump-fun-new-token-monitor) | Pump.fun New Token Monitor - On-Chain Dev Check |

## Layout

```
actors/<slug>/
  .actor/actor.json        the store card: title, description, categories, memory
  .actor/input_schema.json
  .actor/output_schema.json
  Dockerfile
  src/                     one reader per source, plus main.py with the money engine
  tests/                   unit tests on real response bodies, and the money harness
docs/METHOD.md             how a niche is sized and a pain is picked, in one page
scripts/                   preflight, manifest, niche scan, live probe, card scan
assets/icons.jpg           the icon set
```

Every actor carries an input schema, an output schema (all but one), its own Dockerfile and its own README written for the buyer, not for me.

## Running the tests

```
cd actors/iban-validator
python tests/test_iban.py
```

Each test file prints one `PASS` line per check and a final `N/N passed`, and exits non-zero on the first failure. Everything an actor needs is inside its folder: `requirements.txt`, the Dockerfile and the card under `.actor/`.

## Notes

* No credentials live in this repository. A source that needs one reads it from an environment variable, bound to an account secret on the platform.
* Public sources rate-limit by address. When that happens the row comes back free with the reason, instead of a charge and a lie.
* Two actors depend on infrastructure I run: the YouTube transcript and video actors call a small relay when the platform's own address is refused. That is declared in their README and in the summary of every run, not hidden.
* The reasoning is in `docs/METHOD.md`: size the niche in money first, start from the leader's bad reviews, and never trade quality for a faster push.

MIT.
