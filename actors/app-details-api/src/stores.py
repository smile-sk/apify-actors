"""One row per app, read from the two open stores. No key, no browser, no login.

Both stores answer a plain HTTP request:
  * the App Store lookup endpoint returns JSON with 40+ fields per app,
  * Google Play is read by the public package page.
Nothing here needs an API key, a proxy or a browser, so the actor has no
dependency that can be revoked from outside except the stores themselves.
"""

from __future__ import annotations

import re

APPLE_LOOKUP = "https://itunes.apple.com/lookup"
ANDROID_PACKAGE_URL = "https://play.google.com/store/apps/details?id={package}"

_NUMERIC = re.compile(r"^\d{6,12}$")
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_APPLE_URL = re.compile(r"apps\.apple\.com/(?:[a-z]{2}/)?app/(?:[^/]+/)?id(\d+)", re.I)
_ANDROID_URL = re.compile(r"play\.google\.com/store/apps/details\?(?:[^#]*&)?id=([A-Za-z0-9_.]+)", re.I)


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _whole(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify(raw: str) -> tuple[str | None, str, str]:
    """Work out what the input is.

    Returns (kind, value, reason) where kind is one of:
      "ios"      an App Store numeric id (from a bare number or an apps.apple.com link)
      "android"  a Google Play package name (from a bare store link)
      "package"  a dotted name that may live in either store
      None       nothing usable, and reason says why
    """
    text = (raw or "").strip()
    if not text:
        return None, "", "the input is empty"
    found = _APPLE_URL.search(text)
    if found:
        return "ios", found.group(1), ""
    found = _ANDROID_URL.search(text)
    if found:
        return "android", found.group(1), ""
    if _NUMERIC.match(text):
        return "ios", text, ""
    if _PACKAGE.match(text) and len(text) <= 200:
        return "package", text, ""
    return None, text, "not an App Store id, a Google Play package or a store link"


def apple_lookup(client, *, app_id: str | None = None, bundle_id: str | None = None,
                 country: str = "us") -> tuple[dict | None, str]:
    """Ask the App Store for one app. Returns (record, reason-when-missing)."""
    params = {"country": country}
    if app_id:
        params["id"] = app_id
    elif bundle_id:
        params["bundleId"] = bundle_id
    else:
        return None, "no App Store id was given"
    try:
        response = client.get(APPLE_LOOKUP, params=params)
    except Exception as exc:  # noqa: BLE001
        return None, f"the App Store lookup failed: {type(exc).__name__}"
    if response.status_code != 200:
        return None, f"the App Store lookup answered HTTP {response.status_code}"
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return None, "the App Store lookup did not answer JSON"
    results = payload.get("results") or []
    if not results:
        return None, "the App Store has no app with this id"
    record = results[0]
    if not isinstance(record, dict) or not record.get("trackId"):
        return None, "the App Store answered without app details"
    return record, ""


def _restore_tls_after_play_import() -> None:
    """The Play client turns TLS verification off for the whole process on import.

    Importing it is the price of reading Play, but leaving the global context patched
    would quietly disable certificate checks for every later request in this run.
    """
    import ssl
    if getattr(ssl, "_create_default_https_context", None) is not ssl.create_default_context:
        ssl._create_default_https_context = ssl.create_default_context


def android_lookup(package: str, *, country: str = "us",
                   lang: str = "en") -> tuple[dict | None, str]:
    """Ask Google Play for one app. The public page is the source, no key involved."""
    try:
        from google_play_scraper import app as play_app
    except Exception as exc:  # noqa: BLE001
        return None, f"the Google Play client is unavailable: {type(exc).__name__}"
    _restore_tls_after_play_import()
    try:
        record = play_app(package, lang=lang, country=country)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        lowered = message.lower()
        if "not found" in lowered or "404" in lowered:
            return None, "Google Play has no app with this package"
        return None, f"Google Play answered an error: {message[:160]}"
    if not isinstance(record, dict) or not record.get("appId"):
        return None, "Google Play returned no app details"
    return record, ""


def _genre_list(primary, many) -> list[str]:
    out: list[str] = []
    for name in [primary] + list(many or []):
        if name and name not in out:
            out.append(str(name))
    return out


def row_apple(record: dict, *, raw: str, index: int, country: str,
              with_description: bool = False) -> dict:
    """One delivered App Store app."""
    price = _number(record.get("price"))
    row = {
        "platform": "ios",
        "appId": str(record.get("trackId")),
        "storeId": record.get("bundleId"),
        "title": record.get("trackName"),
        "developer": record.get("sellerName") or record.get("artistName"),
        "rating": _number(record.get("averageUserRating")),
        "ratingsCount": _whole(record.get("userRatingCount")),
        "reviewsCount": None,
        "installs": None,
        "installsExact": None,
        "price": price,
        "currency": record.get("currency"),
        "free": (price == 0) if price is not None else None,
        "genres": _genre_list(record.get("primaryGenreName"), record.get("genres")),
        "version": record.get("version"),
        "released": record.get("releaseDate"),
        "lastUpdated": record.get("currentVersionReleaseDate"),
        "contentRating": record.get("contentAdvisoryRating"),
        "adSupported": None,
        "offersInAppPurchases": None,
        "minimumOs": record.get("minimumOsVersion"),
        "fileSizeBytes": _whole(record.get("fileSizeBytes")),
        "languages": list(record.get("languageCodesISO2A") or []),
        "country": country,
        "iconUrl": record.get("artworkUrl512") or record.get("artworkUrl100"),
        "storeUrl": record.get("trackViewUrl"),
    }
    if with_description:
        row["description"] = record.get("description")
    row.update({"actorInput": raw, "actorIndex": index, "status": "OK", "reason": ""})
    return row


def row_android(record: dict, *, raw: str, index: int, country: str,
                with_description: bool = False) -> dict:
    """One delivered Google Play app."""
    price = _number(record.get("price"))
    free = record.get("free")
    row = {
        "platform": "android",
        "appId": record.get("appId"),
        "storeId": record.get("appId"),
        "title": record.get("title"),
        "developer": record.get("developer"),
        "rating": _number(record.get("score")),
        "ratingsCount": _whole(record.get("ratings")),
        "reviewsCount": _whole(record.get("reviews")),
        "installs": record.get("installs"),
        "installsExact": _whole(record.get("realInstalls")),
        "price": price,
        "currency": record.get("currency"),
        "free": bool(free) if free is not None else None,
        "genres": _genre_list(record.get("genre"), record.get("genres")),
        "version": record.get("version"),
        "released": record.get("released"),
        "lastUpdated": record.get("lastUpdatedOn"),
        "contentRating": record.get("contentRating"),
        "adSupported": record.get("adSupported"),
        "offersInAppPurchases": record.get("offersIAP"),
        # google-play-scraper 1.2.7 has no androidVersion key at all: the Play page
        # does not expose the Android version requirement, so this stays null.
        "minimumOs": record.get("androidVersion"),
        "fileSizeBytes": None,
        "languages": None,
        "country": country,
        "iconUrl": record.get("icon"),
        "storeUrl": record.get("url"),
    }
    if with_description:
        row["description"] = record.get("description")
    row.update({"actorInput": raw, "actorIndex": index, "status": "OK", "reason": ""})
    return row
