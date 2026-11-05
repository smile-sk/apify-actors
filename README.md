# Apify Actor Factory

A factory of paid automation actors for the Apify Store. Every actor is built pain-first:
we read the 1-2 star reviews of the niche leader, close one or two of those complaints in
code by default, and say so on the store page. No silent failures, pay only for delivered
data, no proxy required where our own egress gateway does the job.

## Published actors

| Actor | What it does |
|---|---|
| Website Screenshot API | Full-page screenshots plus visual diff against the previous run |
| YouTube Transcript API | Captions for any video, 20/20 through our own egress gateway |
| Company Jobs API | Openings from Greenhouse, Lever, Ashby, Workable and more |
| Website Email Finder | Emails and phone numbers from any site, with noise filtering |
| Workday Jobs API | Workday job boards by country, any site |
| IBAN Validator | IBAN structure and checksum validation, 70+ countries |
| VAT Validator | EU VAT validation against VIES |
| SSL Certificate Checker | Chain, expiry and trust checks |

## Quality gates

- Every actor goes through a review pass before publishing: source files, schemas, billing
  semantics, store promises vs code.
- A preflight script checks the whole publishing checklist per actor (run it before any push).
- Each money field gets a noise polygon: dates, coordinates, VAT/IBAN lookalikes, Fibonacci
  sequences and demo data are rejected before they can be charged for.

## Layout

- `actors/<slug>/` - actor code (this is what goes to the platform)
- `docs/` - pain-first rule, publish checklist, market research, search targets
- `scripts/` - preflight, niche scan, manifest
- `assets/icons/` - actor icons
