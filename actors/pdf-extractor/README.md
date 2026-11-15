# PDF Text Extractor API: text, tables and page ranges

Give it a list of PDF links and get back the text of every document, one row per PDF,
with the page numbers you asked for and the tables found on each page.

Built for the two things that break most PDF jobs: a scan that has no text layer, and a
document that quietly costs you a run while returning nothing. Here a scan comes back as
`NO_TEXT_LAYER` with a plain reason and costs nothing, and so does a broken link, a locked
file or a document cut by the spending cap.
If every link in the list is a typo or a page that is not a PDF, the run ends as `FAILED`
with the status of each link instead of a green run with zero documents, so a successful
run always means at least one document came back. A dataset that refuses a row is retried
once with the text already extracted: one transient refusal does not cost you the whole
document.

## What you get for each document

- `status`: `EXTRACTED`, `NO_TEXT_LAYER`, `ENCRYPTED`, `INVALID_PDF`, `UNREACHABLE`,
  `TOO_LARGE`, `PRIVATE_TARGET`, `ERROR`, `NOT_CHECKED_LIMIT`, `NOT_CHECKED_CAP`,
  `NOT_CHECKED_BILLING` or `NOT_CHECKED_ERROR`.
- `pages`: one entry per page with its number, the text and the tables found on it.
- `pageCount`, `pagesRequested`, `pagesRead`, `chars`, `truncated`.
- `pagesDropped` (first 20 page numbers you asked for that are outside the document),
  `pagesDroppedCount` (how many such pages there were) and `pagesOverLimit` (how many
  pages sit above the 500-page read limit, so nothing is cut off silently).
- `reason`: a plain sentence for every status that is not `EXTRACTED`.
- The run summary keeps the first 200 charge errors in `chargeErrors`, so a broken billing
  setup is visible without flooding the store.
- `billable`: `true` when this document qualifies for the `document-extracted` event,
  i.e. its text was delivered. What the run actually charged is in the summary:
  `chargedEvents`, `deliveredUncharged` and `billingActive`.
- `billingInactive`: `true` on a row that was delivered while the run already knew it
  could not be billed (no pay-per-event pricing, or no `document-extracted` event in the
  price list), so that row cost nothing. A row whose charge call itself failed is already
  stored by then and cannot carry the flag retroactively: those rows are counted by
  `deliveredUncharged` in the summary.

## What this actor does not do

- **No OCR.** A scan without a text layer is reported, not guessed. Saying so is cheaper
  than pretending the text was read.
- **No Office files.** PDF only. DOCX, XLSX and images are out of scope here.
- **No layout reconstruction into Markdown or HTML.** You get text per page plus tables
  as rows of cells, which is what most pipelines actually feed into a model or a database.
- **Tables are detected on lined or well spaced tables.** A page with no table comes back
  with an empty list; nothing is invented.

## A bad batch is an answer, not a crash

If every link in the run is a scan or a locked file, the run still succeeds: each link has
a row saying what happened and nothing is charged. The run is marked failed only when no
document could be opened at all (for example every link returned HTTP 404), and then the
message names every status the links ended with.

## Price

You are charged once per document whose text was delivered (`document-extracted`, $0.01).
Scans, locked PDFs, broken links, files over the size limit, documents cut by
`maxDocuments`, documents stopped by the spending cap and rows whose charge failed are free.

If the platform's charge call fails twice in a row, the run stops charging: the documents
already read stay delivered and unpaid, the rest come back as `NOT_CHECKED_BILLING`. Every
row is counted once, so `requested` always equals `processed` plus `notChecked`:

- `NOT_CHECKED_CAP`: the spending cap was reached, the document was not read;
- `NOT_CHECKED_BILLING`: billing itself stopped the run before this document was read;
- `NOT_CHECKED_ERROR`: the dataset refused the row even after one retry, so that document is
  answered instead of disappearing. A single refusal does not stop the list: the run keeps
  reading the remaining links and stops only after three refusals in a row;
- `NOT_CHECKED_LIMIT`: the link sits beyond `maxDocuments`.

All of them are free. Delivery is judged by the dataset row count, never by the absence of
an exception: `deliveredUncharged` counts every row delivered without a charge,
`datasetWriteErrors` counts refused writes, `datasetWriteUnverified` counts rows whose write
could not be checked (the dataset would not report its size), and `datasetItemCount` is how
many rows this run wrote. The dataset metadata is read too and reported as
`datasetItemCountApi`; when that number was still behind at the end of the run,
`datasetItemCountLag` is true. The count of rows we wrote is never taken from a reader that
can lag behind, so a finished run never claims zero rows while the dataset holds them. A run that stored nothing fails with
`stopReason: dataset-write-error`.

A run that cannot be billed is not a stopped run: it delivers every document for free,
reports `billingActive: false` and leaves `stopReason` empty.

### Spending cap

Apify puts a default cap of about $4.4 on a run unless you set `maxTotalChargeUsd` in the
run options. That is about 460 documents at $0.01. For bigger lists set a higher cap,
otherwise the tail comes back as `NOT_CHECKED_CAP` instead of being read.

## Limits

- 100 documents per run by default (`maxDocuments`); 0 means no limit, and the summary
  reports the number actually applied. The rest come back as `NOT_CHECKED_LIMIT` with their
  own link in `source`.
- 500 pages per document are read at most; `pages` narrows it, for example `1-3,7`.
- 50 MB per file, 45 seconds per download.
- `maxCharsPerDocument` caps the text per document and sets `truncated: true`.

## Input example

```json
{
  "urls": ["https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"],
  "pages": "1-5,8",
  "includeTables": true,
  "maxCharsPerDocument": 200000
}
```

A link that is not a URL is treated as the name of a file you uploaded to this run's
key-value store.

## Private addresses

Links that resolve to loopback, private, link-local or reserved addresses are refused
with `PRIVATE_TARGET` before any request is made, and every redirect hop is checked the
same way.
