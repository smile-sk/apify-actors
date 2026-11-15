# Image Converter & Compressor: WebP, AVIF, HEIC, JPG, PNG

Converts images from urls into WebP, JPEG, PNG or AVIF, resizes them without upscaling,
strips metadata on request and hands back every delivered file both on its own row and
in one zip. The buyer pays per converted image that really exists in the run store.

Most image converters in this store bill every file they touch, including files they
could not decode, and return nothing when a batch is cut. Here the signature decides
what an image is: an html page, a pdf, a video, a truncated file or a file over the
pixel ceiling comes back as its own row with `billable: false` and a plain reason.

## What comes out

- One dataset row per input line. A delivered image has `status: OK`, the delivered
  size, the original size, `bytesIn`, `bytesOut`, `savedPercent` and `outputKey`.
- The converted file itself in the run key-value store under `images_<index>-<name>.<ext>` (a record key allows only letters, digits and !-_.'() , so the prefix is flat, not a folder).
- One archive `converted-images.zip` with the same files, linked from the run's
  output card.

## Input

| field | default | meaning |
|---|---|---|
| `imageUrls` | - | http(s) urls, one per line. Read once each: a repeat, a bad url and a line past `maxImages` are free rows with a reason. |
| `format` | `webp` | `webp`, `jpeg`, `png` or `avif`. |
| `quality` | `82` | 1 to 100 for JPEG, WebP and AVIF. PNG ignores it. |
| `maxWidth`, `maxHeight` | `2000` | Scale down to fit, keep the aspect ratio, never upscale. `0` means no limit. |
| `stripMetadata` | `true` | Drops EXIF, GPS and camera tags. The orientation itself is applied to the pixels first, so a phone photo stays upright. |
| `background` | `#FFFFFF` | The colour transparency is flattened onto for JPEG. |
| `maxImages` | `20` | The list is cut here, the rest is free with `NOT_CHECKED_LIMIT`. |

## Price

$2.00 per 1,000 converted images (`image-converted`, $0.002 per image). The platform
adds its own start event. Nothing else is charged: a page of html, a broken file, a
private address, a timeout, a file over 25 MB, an image over 40 megapixels, a file that
could not be stored and every row of a stopped run are free.

`billable` on a row is the claim from the price list; the money actually taken is
`chargedEvents` in the run summary. When the two differ, the summary is right.

## Statuses

`OK` means the file is delivered and can be downloaded. Everything else is free:

- `NOT_CHECKED_INVALID` - the line is not an http(s) url.
- `NOT_CHECKED_DUPLICATE` - the same url came earlier.
- `NOT_CHECKED_LIMIT` - past `maxImages`.
- `NOT_CHECKED_PRIVATE_ADDRESS` - a host name answered with a non public address,
  whether the image host or any hop of the redirect chain.
- `NOT_CHECKED_DNS_ERROR` - the host name does not resolve or is not a valid name.
- `NOT_CHECKED_FETCH_ERROR` - HTTP error or timeout.
- `NOT_CHECKED_TOO_LARGE` - more than 25 MB.
- `NOT_CHECKED_NOT_AN_IMAGE` - the bytes are not an image, whatever the url says.
- `NOT_CHECKED_DECODE_ERROR` - an image signature, but the file cannot be read.
- `NOT_CHECKED_TOO_MANY_PIXELS` - more than 40 megapixels: the cap that fits next
  to the image peak inside 1 GB of memory, it is not the bytes on disk.
- `NOT_CHECKED_UNSUPPORTED_TARGET` - the image has no encoder for the asked format.
- `NOT_CHECKED_STORE_ERROR` - the converted file could not be written to the store, so
  there is nothing to download and nothing to charge.
- `NOT_CHECKED_CAP` - the spending cap was reached: this file and everything after it
  are free, the file itself is already in the store under its `outputKey`.
- `NOT_CHECKED_TIME`, `NOT_CHECKED_BILLING`, `NOT_CHECKED_ERROR` - the run stopped
  before this image; `stopReason` in the summary says why.

## Measured

One CPU, this project's machine, a 4000x3000 photo delivered as a 1 056 224 byte JPEG:

| target | time | size | note |
|---|---|---|---|
| webp, 2000 px | 0.69 s | 102 800 | 10x smaller |
| jpeg, 2000 px | 0.22 s | 187 071 | |
| png, 2000 px | 0.52 s | 2 903 277 | 2.7x bigger, `savedPercent` says so |
| avif, 2000 px | 0.25 s | 156 027 | |

The wall clock numbers are from a 4000x3000 source; smaller images are faster. PNG of a
photo usually grows, and the row reports a negative `savedPercent` instead of pretending
otherwise.

## What this actor does not do

- It does not upscale, crop, rotate by hand or change the aspect ratio.
- It does not convert to gif, bmp or tiff.
- It does not keep an ICC profile when `stripMetadata` is on, so an unusual colour
  profile can shift colours.
- It does not convert animated gif or animated webp frame by frame: the first frame is
  used.
- It does not fetch a page to find the image inside it: the url must point at the image.
- It does not write into the buyer's own store: files go into the store of this run,
  which the buyer owns.

## Local run

```bash
python -m src.main
```

with `APIFY_LOCAL_STORAGE_DIR=./storage` and an `INPUT.json` in
`storage/key_value_stores/default/`.
