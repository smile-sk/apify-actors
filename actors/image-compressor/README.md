# Image Compressor API - Batch JPG, PNG, WebP to Target Size

Most compressors bill every image they touch, including the ones they could not make
smaller. This actor has one rule on top of the park rule: **you pay for an image that
really got smaller**. When the re-encode is not smaller than the input, the original
bytes are delivered untouched, the row says `compressed: false`, and it costs nothing.

Everything happens inside the container with Pillow: no key, no image service, no
per image cost besides the platform itself.

## What you send

`imageUrls` is a list of http(s) links, one per line. Two optional numbers decide the
work:

- `targetKB` asks for a weight: the encoder lowers quality until the file fits, and
  only then scales the picture down, because a sharp photo beats a blurry one.
- `quality` is the ceiling of that search, and the quality used when no target is set.
- `format` is `keep` by default: the bytes decide the format, so a png is not quietly
  turned into a jpeg. Naming webp, jpeg, png or avif asks for a conversion.
- `maxWidth` / `maxHeight` are 0 by default. This actor sells weight, not dimensions:
  it does not resize unless you ask.

## What you get

One dataset row per image with the delivered size, the size it came from, bytes saved,
percent saved, the quality the search landed on, whether the target was reached, and
`outputKey` pointing at the file in the run store. Every delivered image is also packed
into one zip, `compressed-images.zip`.

## What is free

A row that is not a delivered file costs nothing and names its reason: a line that is
not an http url, a repeat, a line past `maxImages`, a private or unresolvable host, a
file that cannot be downloaded, a file over 25 MB, bytes that are not an image by their
own signature, a file whose signature is right but which cannot be decoded, an image
over 40 megapixels, and every row for work the run never reached because it stopped early
(`TIME`, `CAP`, `BILLING`, `ERROR`).

And the rule this actor exists for: **an image that did not get smaller is delivered
unchanged and is free**.

## Money

The event is `image-compressed`, charged per delivered image that really shrank. One SDK
call writes the row and takes the money together, so a row you can read is a row that
was written. The spending cap is checked before each image; on the cap the run stops,
the rest of the images come back free with `NOT_CHECKED_CAP`, and the summary says
`stopReason: spending-cap`. If the price cannot be read at all, the run stops before the
first paid image and nothing is delivered - a price this actor cannot see is a price it
does not guess.

`chargedEvents` in the summary is the number to trust. The platform reports
`chargedEventCounts` with a lag, so a zero in the first minute is not proof that nothing
was charged.

## Honest limits

- 25 MB per input file and 40 megapixels per image. Both are measured limits of the
  container memory, not taste: a 40 MP RGBA image already takes about 600 MB of the
  1024 MB.
- No resize unless `maxWidth` or `maxHeight` is set.
- PNG is lossless, so `quality` and `targetKB` only get what deflate can give; a png
  that cannot be made smaller comes back unchanged and free.
- A source format this actor cannot write back (gif, heic, tiff, bmp) is delivered as
  jpeg, and the row names the source format it came from.
- Redirect hops are resolved and refused before their request goes out, so a public link
  that answers 302 into a private network never reaches it.
- The archive is built after the last image; if it fails, every file is still in the
  store under its `outputKey` and the summary says why.
- Nothing here reads a key or calls a third party, so there is no quota to hit and no
  external outage to wait out.
