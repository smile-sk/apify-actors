# arXiv Paper API - metadata by id, no key, free for missing

Send arXiv ids or links on arxiv.org, get one row per paper: title, authors, abstract,
categories, dates, DOI, journal reference and PDF link. No API key, no browser, no proxy.

An id that arXiv never saw is a **verdict**: the row comes back free with the reason, so a
literature check does not pay for papers that do not exist.

## Input

| field | what it is |
|---|---|
| `papers` | arXiv ids (`1706.03762`, `hep-th/9901001`, `cond-mat.mes-hall/0211022`) or links (`https://arxiv.org/abs/1706.03762`, `https://arxiv.org/pdf/1706.03762.pdf`). A version such as `1706.03762v5` reads that version, not the newest one. The API answers old ids only in canonical form, so a sub-archive is dropped for the request: `cond-mat.mes-hall/0211022` and `cond-mat/0211022` are the same paper and are charged once. |
| `maxPapers` | How many papers to read and sell. Default 100, maximum 500. |
| `includeAbstract` | Add the full abstract to every row. On by default. |

## Output, one row per paper

`arxivId`, `version`, `title`, `authors`, `publishedAt`, `updatedAt`, `primaryCategory`,
`categories`, `comment`, `journalRef`, `doi`, `abstract`, `absUrl`, `pdfUrl`, `billable`,
plus `actorIndex` and `actorInput` so the input order can be rebuilt.

Free status rows carry `status` and `reason` instead of paper fields:

- `INVALID_INPUT` - the line is not an arXiv id or a link on arxiv.org.
- `DUPLICATE` - the same id was already sent in this run.
- `NOT_FOUND` - a valid id, but arXiv has no paper behind it.
- `NOT_CHECKED_TIME`, `NOT_CHECKED_CAP`, `NOT_CHECKED_BILLING`, `NOT_CHECKED_ERROR` - the
  line was not read, and the reason says why (deadline, spending limit, unreadable price,
  an unreachable arXiv, an id arXiv refused).

One refused id does not cost the rest. arXiv answers HTTP 500 for a whole `id_list` when a
single id in it is bad, so a version the API will not serve used to take down every paper in
the chunk. The run now splits the chunk until the refused id stands alone: the papers beside
it are sold, and that one line comes back free as `NOT_CHECKED_ERROR` with the reason, to be
sent again later.

## Price

$0.80 per 1,000 papers: `paper-delivered` is charged once per paper that came back with
metadata. A missing id, a malformed line and a duplicate cost nothing. The lowest spending
limit you can set per run is $0.05.

## Politeness and limits

The arXiv API is keyless, and its terms ask for one request at a time with a pause of a few
seconds and a User-Agent that names the caller. Ids therefore go out in chunks of 50 with a
3 second pause between chunks, so 500 papers take 10 requests and about 30 seconds of
waiting. The run reads at most 500 papers.

## What this actor does not do

- It does not search by keyword, author or category: it reads the ids you give it.
- It does not download or parse the PDF: `pdfUrl` points at the file, the text is yours to
  fetch.
- It does not give the full version history: a row carries the newest version and its
  number. Older versions are not read.
- It does not build a citation graph.

## Source

Data comes from the official arXiv Atom API (`export.arxiv.org/api/query`), keyless.
Metadata is reported as arXiv returns it; the abstract is returned with its line breaks
collapsed into single spaces. arXiv is operated by Cornell University and subject to its own
terms of use.
