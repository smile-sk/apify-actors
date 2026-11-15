# Image OCR API: text and word boxes from a picture

Point it at a list of image urls and get one row per picture: the text, the mean word
confidence, the lines, and the box of every word with its own confidence. The
recogniser (Tesseract, with eng, rus and ukr packs) ships inside the actor, so there is
no key to bring, no OCR service to pay, and no queue that belongs to somebody else.

Formats: jpeg, png, webp, gif, bmp, tiff and heic from a phone. A photo taken sideways
is turned the right way up before recognition, and the row says `rotated: true`.

## Why this exists

The biggest actors in this niche wrap Google Lens: they open a Google page, scrape the
answer and stop working the day Google answers with a captcha. That is a free service
of somebody else resold as a product. Here the engine lives in the image, and a picture
that has no text comes back **free** with the reason instead of an empty string.

## Price

A row with text on it costs $0.006. Every other row is free: a picture without text, an
html page, a broken file, a repeat, a line that is not a url, a line beyond `maxImages`,
and every line after a stop. The summary says `chargedEvents` and that is the number
the platform really charged.

## Statuses

`OK` means the picture was read and the row is billable. Everything else is free and
carries a plain reason:

- `NOT_CHECKED_NOT_AN_IMAGE`: the bytes are not a picture (an html login page, a pdf)
- `NOT_CHECKED_BROKEN_IMAGE`: the signature says jpeg, the decoder disagrees
- `NOT_CHECKED_NO_TEXT`: a real picture with no text on it
- `NOT_CHECKED_FETCH_ERROR`: the server refused or timed out
- `NOT_CHECKED_DNS_ERROR`: the host name does not resolve, or is not a name at all
- `NOT_CHECKED_PRIVATE_ADDRESS`: the url, or a redirect it leads to, points inside a
  private network
- `NOT_CHECKED_TOO_LARGE`: the file is above 25 MB
- `NOT_CHECKED_LIMIT`, `NOT_CHECKED_DUPLICATE`, `NOT_CHECKED_INVALID`: the list, not the site
- `NOT_CHECKED_TIME`, `NOT_CHECKED_CAP`, `NOT_CHECKED_BILLING`: the run stopped before this row
- `NOT_CHECKED_ERROR`: the run stopped on an unexpected error before this picture

## What this actor does not do

- It does not read pdf files. That is a separate actor in this park, pdf-ocr-api.
- It does not recognise handwriting reliably. Tesseract reads print; a handwritten note
  will come back with little text, and the row is still charged if any text came out.
- It does not read a language whose pack is not installed. Only `eng`, `rus` and `ukr`
  ship inside the image: ask for another one in `languages` and the run refuses before
  it downloads a single picture, with nothing charged. Leave the default `eng` on, say,
  a German scan and the text comes back partial and at low confidence, and the row is
  still charged if any text came out, the same as with handwriting.
- An animated gif is read from its first frame. The row says `imageFormat: gif` and the
  text is of that frame.
- It does not go into private networks: a url that resolves inside `10.0.0.0/8`,
  `127.0.0.0/8`, `100.64.0.0/10` or behind a public name that answers from a private
  address is refused before the request is sent. Redirects are followed by hand, at
  most three, and every hop passes the same guard before its own request goes out, so a
  public url that answers 302 into a private address is refused before that hop is
  requested.

## Limits

`maxImages` defaults to 10, and the run says how many it applied. A picture above
`maxPixels` (40 million by default) is scaled down before recognition, never silently.
The picture is turned to png inside the run, so a 25 MB jpeg is not held twice in
memory.

## Summary

The run writes `RUN_SUMMARY-<runId>` into the `image-ocr-api-summary` store. The bare
`RUN_SUMMARY` key lives only in the store of that run, which belongs to nobody else.
Counters close on themselves: `imagesProcessed + imagesNotChecked` equals
`imagesAttempted`, and `linesThatAreNotImages + imagesAttempted` equals `requested`.
`deliveredImages` counts every picture row that reached the buyer, paid or free, while
`chargedImages` counts only the rows that took money and `chargedEvents` is what the
platform confirms. When a paid row raises and the dataset counter shows the row did
land, it is named in `deliveredUncharged`; when the counter cannot confirm it, it is
named in `deliveredUnconfirmed` instead of being counted as delivered.

## No key, no externals

The actor only fetches the urls the buyer sent. There is no Google, no API key and no
account anywhere in the path.
