"""Input parsing and row building, without a network and without money."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import stores  # noqa: E402

PASSED = 0
FAILED: list = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


APPLE = {
    "trackId": 389801252, "bundleId": "com.burbn.instagram", "trackName": "Instagram",
    "sellerName": "Instagram, Inc.", "averageUserRating": 4.69055,
    "userRatingCount": 29706439, "price": 0.0, "currency": "USD",
    "primaryGenreName": "Photo & Video", "genres": ["Photo & Video", "Social Networking"],
    "version": "455.0.0", "releaseDate": "2010-10-06T00:00:00Z",
    "currentVersionReleaseDate": "2026-11-09T15:57:59Z", "contentAdvisoryRating": "12+",
    "minimumOsVersion": "16.3", "fileSizeBytes": "699975680",
    "languageCodesISO2A": ["EN", "DE"], "artworkUrl512": "https://is1.example/512.png",
    "trackViewUrl": "https://apps.apple.com/us/app/instagram/id389801252",
    "description": "long text",
}

ANDROID = {
    "appId": "com.instagram.android", "title": "Instagram", "developer": "Instagram",
    "score": 3.9847674, "ratings": 170022733, "reviews": 4538640,
    "installs": "5,000,000,000+", "realInstalls": 7872113666, "price": 0,
    "currency": "USD", "free": True, "genre": "Social", "version": "Varies with device",
    "released": "Apr 3, 2012", "lastUpdatedOn": "Nov 9, 2026",
    "contentRating": "Teen", "adSupported": True, "offersIAP": True,
    "androidVersion": None, "icon": "https://play.example/icon.png",
    "url": "https://play.google.com/store/apps/details?id=com.instagram.android",
    "description": "long text",
}


def run_all() -> int:
    # classify
    check("numeric id is an App Store id", stores.classify("389801252")[0] == "ios")
    check("an apple link gives the numeric id",
          stores.classify("https://apps.apple.com/us/app/instagram/id389801252")[1] == "389801252")
    check("a play link gives the package",
          stores.classify("https://play.google.com/store/apps/details?id=com.whatsapp")[1]
          == "com.whatsapp")
    check("a dotted name is ambiguous, not a guess",
          stores.classify("com.instagram.android")[0] == "package")
    check("empty input is refused with a reason",
          stores.classify("   ")[0] is None and "empty" in stores.classify("   ")[2])
    check("junk is refused with a reason",
          stores.classify("hello world")[0] is None)
    check("a short number is not treated as an app id", stores.classify("123")[0] is None)

    # apple row
    row = stores.row_apple(APPLE, raw="389801252", index=0, country="us")
    check("apple row: platform and id", row["platform"] == "ios" and row["appId"] == "389801252")
    check("apple row: bundle id kept", row["storeId"] == "com.burbn.instagram")
    check("apple row: free computed from the price", row["free"] is True)
    check("apple row: installs stay null, the store does not publish them",
          row["installs"] is None and row["reviewsCount"] is None)
    check("apple row: genres deduplicated",
          row["genres"] == ["Photo & Video", "Social Networking"])
    check("apple row: no description by default", "description" not in row)
    check("apple row: status is OK", row["status"] == "OK")

    # android row
    row = stores.row_android(ANDROID, raw="com.instagram.android", index=1, country="us")
    check("android row: installs kept as text", row["installs"] == "5,000,000,000+")
    check("android row: exact installs kept", row["installsExact"] == 7872113666)
    check("android row: file size stays null", row["fileSizeBytes"] is None)
    check("android row: languages stay null", row["languages"] is None)
    check("android row: in app purchases reported",
          row["offersInAppPurchases"] is True and row["adSupported"] is True)

    # description on demand
    row = stores.row_apple(APPLE, raw="389801252", index=0, country="us", with_description=True)
    check("description is included only when asked", row.get("description") == "long text")

    # numbers that arrive as text
    check("a text price becomes a number",
          stores.row_apple({**APPLE, "price": "4.99"}, raw="1", index=0,
                           country="us")["price"] == 4.99)
    check("a missing number stays null",
          stores.row_apple({**APPLE, "userRatingCount": None}, raw="1", index=0,
                           country="us")["ratingsCount"] is None)

    print("PASSED", PASSED, "FAILED", len(FAILED))
    for name in FAILED:
        print("  FAIL:", name)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
