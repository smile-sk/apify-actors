# Website Screenshot API: Full-Page PNG & Visual Diff

**Most screenshot APIs hand you a picture and forget it.** Then you build the rest yourself: a storage
bucket for old images, a diff tool, a scheduler, and a rule for what counts as "changed".
This Actor does that part in the same run: a full-page screenshot, the **share of changed pixels**
against the previous screenshot of the same URL, and a **diff image with the changes painted red**.
The new screenshot becomes the baseline for the next run - automatically.

One call = one page. Nothing to install, nothing to store on your side.

## The pain this removes

| The usual story | What happens here |
|---|---|
| Screenshot taken, change detection left to you | `changed`, `changedRatio` and `diff.png` come back in the same run |
| Baselines on your disk, or silently mixed with other users' | A named store `screenshot-baselines` inside **your own** Apify account, keyed by URL + viewport + format |
| Blank or half-rendered images on JavaScript-heavy sites | Real Chromium, waits for the load event plus `waitFor` ms for lazy images |
| A monthly subscription for a handful of checks | **$0.05 per page, no subscription, no minimum** |
| "It worked" with nothing to prove it | Every run is a dataset row: size, dimensions, timestamp, keys, change ratio |
| Mobile and desktop shots overwriting each other | Viewport and format are part of the baseline key, so each keeps its own history |

## What you get

| Output | Where |
|---|---|
| Change ratio, baseline key, capture metadata | Dataset (`Run result`) |
| Screenshot (PNG or JPEG) | Key-value store, `screenshot.png` |
| Diff image, red = changed pixels | Key-value store, `diff.png` - only when something changed |

Example dataset item:

```json
{
  "url": "https://example.com",
  "changed": false,
  "changedRatio": 0.0,
  "firstRun": false,
  "sizeBytes": 71152,
  "width": 1440,
  "height": 900,
  "fullPage": true,
  "screenshotKey": "shot_2f1a6c1e8b3f0d92",
  "checkedAt": "2026-11-04T12:15:00+00:00"
}
```

When the page differs from the baseline, `changed` is `true`, `changedRatio` holds the share of changed
pixels (for example `0.0853` = 8.53 %) and an extra record `diffKey` points to the diff image.

## Who uses it

- **Price and content monitoring** - watch a product page, a tariff table or a hotel page for edits.
- **Competitor landing pages** - a silent redesign shows up as a change ratio, not as a surprise next month.
- **Terms of service and legal pages** - a timestamped image of what the text said on a given day.
- **Status pages and dashboards** - a picture is the proof when the status API is down.
- **Deploy checks** - your own site after a release, with an alert when the layout breaks.
- **QA of marketing pages** across viewports, without a browser in CI.

## Input

| Field | Default | Meaning |
|---|---|---|
| `url` | - | Page address. `example.com` is accepted and upgraded to `https://`. |
| `fullPage` | `true` | Whole scrollable page, or only the visible screen. |
| `width` / `height` | `1440` / `900` | Viewport in CSS pixels. |
| `format` | `png` | `png` for pixel-perfect detail, `jpeg` for smaller files. |
| `diffPixels` | `12` | How much a colour channel must change (0-255) for a pixel to count. Higher ignores soft noise, lower catches subtle shifts. |
| `waitFor` | `1000` | Extra milliseconds after load, for lazy images and animations. |
| `resetBaseline` | `false` | Forget the stored screenshot for this URL and treat this run as a new baseline. |

## Use it from code

```bash
curl -X POST "https://api.apify.com/v2/acts/YOUR_USER~website-screenshot-visual-diff/run-sync-get-dataset-items?token=YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","fullPage":true}'
```

The response is the dataset item above; the images are in the run's key-value store under the returned keys.
Works with Python, Node, Zapier, Make and n8n through the same endpoint.

## Use it from an AI assistant (MCP)

The Actor is callable as an MCP tool, so Claude, Cursor or any MCP client can check a page directly:
"compare this page with the version from last week" - the assistant runs the Actor and reads the
change ratio instead of guessing.

## Where the baselines live

In a **named key-value store** inside your own Apify account (`screenshot-baselines`). They survive
between runs, they are billed to you at Apify storage prices, and nobody else's baselines are mixed
with yours.

## Pricing

**$0.05 per screenshot page** - one event covers the screenshot and its comparison with the previous
run, no matter how large the page is. No subscription, no minimum. On Apify's free plan the run is covered by the included monthly usage credit,
so no card is needed until that credit runs out.

## Tips for stable results

- Pages with carousels, live counters or ads change every minute - raise `diffPixels` to 20-30 to ignore them.
- Cookie banners appear on the first visit only; run twice with `resetBaseline: false` to get a clean baseline.
- For a long page with lazily loaded images, raise `waitFor` to 3000 ms.
- To watch many pages, run the Actor once per URL via API, a schedule, or an Apify task.

## Limitations

- Pages behind a login or a paywall cannot be captured (no authentication is sent).
- Very long or heavy pages may hit the run timeout; increase the timeout and memory in the run settings.
- The diff is pixel-based, so a 1-pixel layout shift counts as a change across the whole page.

## Feedback

A page that renders badly, or a site that refuses to load? Open an issue on the Actor page with the URL -
that is the fastest way to get it fixed.
