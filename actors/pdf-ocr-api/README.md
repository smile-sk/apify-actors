# PDF OCR API: scanned PDF to text, charged per page

Give it a list of pdf urls. It reads the pages you ask for (10 per document by
default, `maxPagesPerDocument` changes that, 0 means all of them) and writes one row per
page: the text, where that text came from, and how confident the recogniser was. Pages
past the limit are not read and not charged, and `pagesCutByLimit` says how many were
left, so a cut document is visible instead of looking complete.

No API key is needed on either side. The recognition runs inside the image with
Tesseract, so a scanned contract is read on our machine and your document never goes
to a third party service.

## What you get

One dataset row per page:

* `text`: the page as plain text, lines rebuilt in reading order rather than one long
  run of words.
* `source`: `text` when the page carried its own text layer (exact, instant, free of
  recognition error) or `ocr` when Tesseract read a scan.
* `confidence`: mean word confidence for an OCR page, `null` for a text layer because
  there is nothing estimated there.
* `words`: every word with its box and confidence, when `includeWords` is on. This is
  what a form parser or a redaction tool needs.
* `page`, `pagesInDocument`, `pagesLeft`: where you are in the document, and how much of
  it this run did not cover.

## What it costs

`$6.00 per 1,000 pages`, charged per page that carried text.

A page with nothing readable on it is free and comes back as `NOT_CHECKED_NO_TEXT`: a
blank scan, a separator page, or a picture without print. That is the difference from
most actors in this niche, which bill the whole document whether the text came out or
not.

## What it does not do

* It does not guess. A page whose scan was unreadable is marked free with a reason, not
  delivered as an empty string.
* It does not handle a password protected pdf. It says `NOT_CHECKED_ENCRYPTED` and
  charges nothing.
* It does not read handwriting reliably. Tesseract reads print; a handwritten note will
  come back with wrong words and a low confidence, and the confidence is there so you
  can filter those rows yourself.
* It does not keep your file. The pdf is downloaded, read and dropped inside the run.
* It does not translate, summarise or classify. This actor turns pages into text, and
  nothing else.

## Settings that matter

* `ocrMode`: `auto` reads a digital page from its text layer and only sends a scan to
  OCR. `always` forces recognition, for a pdf whose text layer is garbled. `never` never
  pays for recognition, and a scan then comes back free with `NOT_CHECKED_NO_TEXT`.
* `pages`: a range like `1-5,9`. Pages outside the document are counted in the summary
  as `pagesNotInDocument`, never silently dropped. A range that names only pages this
  document does not have comes back as `NOT_CHECKED_NO_SUCH_PAGE`, not as a bad url.
* `maxPagesPerDocument`: how far into one document this run goes. What is left is
  counted in `pagesCutByLimit`.
* `dpi`: 200 reads ordinary 10 to 12 point print. 300 helps with small print and costs
  roughly twice the time.
* `languages`: English, Russian, Ukrainian, and their combinations. A page in a language
  that is not selected comes back with wrong words rather than with an error.

## Honest numbers

Recognition time depends on the page, not on a promise: a plain text page at 200 dpi
takes about one to two seconds on one core, a dense scan with small print can take four
or five. The actor watches the platform deadline and stops before it, and the time left
caps the recognition itself, so a page that cannot finish in time comes back as a free
`NOT_CHECKED_TIME` or `NOT_CHECKED_ERROR` row instead of the run being killed mid file.

Accuracy is a property of the scan, not of this actor: a clean 300 dpi scan of printed
text usually comes back word perfect, a photo of a page at an angle does not. The
`confidence` field is there because a single number is more useful than a claim.

## Refusals

Every refusal is a row of its own, free, with the reason in plain words:

| status | meaning |
| --- | --- |
| `NOT_CHECKED_INVALID` | the line is not an http url |
| `NOT_CHECKED_DUPLICATE` | the same url came earlier, the document is read once |
| `NOT_CHECKED_LIMIT` | beyond `maxDocuments` |
| `NOT_CHECKED_PRIVATE_ADDRESS` | a host name really answered with a non public address |
| `NOT_CHECKED_DNS_ERROR` | the host name does not resolve or is not a valid name |
| `NOT_CHECKED_FETCH_ERROR` | the download failed or timed out |
| `NOT_CHECKED_TOO_LARGE` | bigger than 80 MB |
| `NOT_CHECKED_NOT_PDF` | the bytes are not a pdf, whatever the url or content type says |
| `NOT_CHECKED_ENCRYPTED` | the document needs a password |
| `NOT_CHECKED_BROKEN_PDF` | the file could not be parsed |
| `NOT_CHECKED_NO_TEXT` | the page had neither a text layer nor readable print |
| `NOT_CHECKED_NO_SUCH_PAGE` | the `pages` range named only pages this document does not have |
| `NOT_CHECKED_TIME` | the platform deadline was close |
| `NOT_CHECKED_CAP` | the spending cap of the run was reached |
| `NOT_CHECKED_BILLING` | the price could not be read, so nothing was sold |
| `NOT_CHECKED_ERROR` | this page could not be read, or the run stopped before it |

Note what `NOT_CHECKED_NOT_PDF` means: a link that answers with an html login page is
refused before any parser touches it, because a pdf signature is looked for in the
first kilobyte. An html page billed as a document is the classic way a pdf actor takes
money for nothing.

## The run summary

In the key value store, under `RUN_SUMMARY` and under `RUN_SUMMARY-<runId>` in the
`pdf-ocr-api-summary` store:

* `requested`, `uniqueDocuments`, `duplicates`, `invalidLines`, `blankLines`
* `linesThatAreNotDocuments` (a bad url, a repeat, a blank line, a line past
  `maxDocuments`) and `documentsAttempted`: the lines that really were documents
* `processedDocuments`, `documentsAttempted` and `notCheckedDocuments`: processed counts
  a document that was opened and read, even when every page of it had no text, and a
  document we never reached is not checked, so the two add up to `documentsAttempted`
  and `linesThatAreNotDocuments` closes the count back to `requested`
* `deliveredPages` and `chargedEvents`: the second one is the money actually taken, so
  when the two differ, trust it and read `deliveredUncharged`
* `pagesFromTextLayer` and `pagesFromOcr`: how much of the work was real recognition
* `pagesWithoutText`, `pagesCutByLimit`, `pagesNotInDocument`, `ocrSeconds`
`stopReason` names why the run stopped early, if it did.
