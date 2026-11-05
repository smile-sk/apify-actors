"""Local smoke test for the website-contacts Actor: real sites, real output.

Run with the project venv:
    .venv\\Scripts\\python.exe scripts\\test_contacts_local.py
No Apify SDK, no platform - it exercises src/extract.py + the crawl loop.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "actors" / "website-contacts"))

import httpx  # noqa: E402

from src import extract  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SITES = ["apify.com", "stripe.com", "figma.com", "https://www.simplify.hr/contact",
         "https://www.pexels.com", "https://github.com"]


async def crawl(client, site, max_pages=5):
    url = site if site.startswith(("http://", "https://")) else "https://" + site
    try:
        r = await client.get(url, follow_redirects=True, headers={"User-Agent": UA})
        html = r.text[:3_000_000]
    except Exception as exc:                                     # noqa: BLE001
        return {"website": site, "status": "UNREACHABLE", "reason": f"{type(exc).__name__}"}

    tree = extract.parse(html)
    if extract.is_blocked(html, tree):
        return {"website": site, "status": "BLOCKED", "company": "anti-bot page"}
    domain = extract.registrable(httpx.URL(url).host)
    org = extract.jsonld_org(tree)
    text = extract.page_text(tree)
    emails = {e["value"]: e for e in extract.emails_from(tree, html, url, domain)}
    phones = {p["value"]: p for p in extract.phones_from(tree, text, url)}
    socials = extract.socials_from(html)
    pages = [url]
    for link in extract.contact_links(tree, url, domain, max_pages - 1)[: max_pages - 1]:
        try:
            sub = await client.get(link, follow_redirects=True, headers={"User-Agent": UA})
        except Exception:                                        # noqa: BLE001
            continue
        pages.append(link)
        st = extract.parse(sub.text)
        for e in extract.emails_from(st, sub.text, link, domain):
            emails.setdefault(e["value"], e)
        for p in extract.phones_from(st, extract.page_text(st), link):
            phones.setdefault(p["value"], p)
    allm = list(emails.values())
    return {
        "website": site,
        "foreign": [e for e in allm if e.get("foreign")],
        "emails": [e for e in allm if not e.get("foreign")],
        "company": extract.company_name(tree, org, domain),
        "status": "OK" if (emails or phones) else "NO_CONTACTS",
        "phones": list(phones.values()),
        "socials": socials,
        "address": org.get("address"),
        "pages": pages,
    }


async def main() -> None:
    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        results = await asyncio.gather(*(crawl(client, s) for s in SITES))
    for row in results:
        print("=" * 90)
        print(f"{row['website']:38} {row['status']:12} company={row.get('company','')!r}")
        if row["status"] in ("UNREACHABLE", "BLOCKED"):
            print("   reason:", row.get("reason", "anti-bot challenge"))
            continue
        for e in row["emails"][:5]:
            print(f"   EMAIL {e['value']:38} role={e['role']} kind={e['kind']:6} <- {e['foundOn'][:60]}")
        for p in row["phones"][:3]:
            print(f"   PHONE {p['value']:24} {p['kind']:9} <- {p['foundOn'][:60]}")
        for e in row.get("foreign", [])[:4]:
            print(f"   FOREIGN {e['value']:36} <- {e['foundOn'][:55]}")
        print("   socials:", ", ".join(sorted(row["socials"])) or "-")
        print("   address:", row["address"])
        print(f"   pages: {len(row['pages'])}")


if __name__ == "__main__":
    asyncio.run(main())
