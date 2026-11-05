# Search targets - naming our actors by what people type

Measured 2026-11-05 against the live Apify Store API (`/v2/store?search=`, limit 100).
Our owner account: `stas_k`. Position - where our actor appears for that query, `-` = not in the first 100.

| query | actors matching | our position |
|---|---|---|
| full page screenshot api | 449 | 2 |
| screenshot diff | 284 | 6 |
| screenshot api | 898 | - |
| website screenshot | 737 | - |
| youtube transcript api | 779 | 57 |
| youtube transcript no proxy | 182 | 6 |
| job openings api | 1616 | 15 |
| greenhouse jobs api | 1531 | - |
| workday jobs api | 651 | 64 |
| workday jobs by country | 455 | 32 |
| find emails on website | 1271 | 16 |
| email finder | 2895 | - |
| phone number finder | 1072 | - |

## Rule (operator's order, 2026-11-05)

1. Before publishing a new actor: collect the phrases people actually type in the store (titles of the leaders, long-tail variants).
2. The actor title starts with that phrase - not with our own wording of the idea.
3. After publishing: measure our position for 10-15 queries of the niche and repeat until we are visible.

## Current titles

- `KJlZ1w1PjP59l4usw` - Full Page Screenshot API - Screenshot & Visual Diff
- `GuanyXsDjWKCwRK45` - YouTube Transcript API - Captions, No Proxy
- `JfScsAVmXb5VnofgJ` - Company Jobs API - Greenhouse, Lever, Ashby Openings
- `uhUG2WtA670A2gqNX` - Website Email Finder - Emails & Phone Numbers
- `NdJWWljtR7mfDrPEj` - Workday Jobs API - Openings by Country, Any Site