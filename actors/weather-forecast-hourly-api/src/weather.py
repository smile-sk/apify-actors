"""MET Norway: a forecast with no key, no account and no proxy.

Why this source and not the free Open-Meteo endpoint: Open-Meteo is free for
non-commercial use only, and a paid actor on the Apify Store is commercial, so using it
would break their terms. The Norwegian Meteorological Institute publishes the same kind
of data under CC-BY 4.0, allows commercial use, asks only for a User-Agent with a contact
and for attribution, which this actor gives in the README and in every run summary.

The product takes coordinates, not city names: geocoding is a different service with its
own limits, and pretending otherwise would add a wall to a source that has none.
"""

from __future__ import annotations

import re

ENDPOINT = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
USER_AGENT = "apify-weather-forecast-api/0.1 (contact: krisanov.stas@gmail.com)"

COORDINATES = re.compile(r"^\s*([+-]?\d{1,2}(?:\.\d+)?)\s*[,;]\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$")

C_TO_F = 9.0 / 5.0
MS_TO_MPH = 2.2369362920544
MM_PER_INCH = 25.4


def coordinates(text: str) -> tuple[float, float] | None:
    """A literal pair inside the real ranges, or None when the input is not one."""
    found = COORDINATES.match(text or "")
    if not found:
        return None
    lat, lon = float(found.group(1)), float(found.group(2))
    if not -90.0 <= lat <= 90.0:
        return None
    if not -180.0 <= lon <= 180.0:
        return None
    return lat, lon


def forecast(lat: float, lon: float, client,
             timeout: float | None = None) -> tuple[dict | None, str]:
    """The whole timeseries for one point, or the reason the source did not answer."""
    extra = {} if timeout is None else {"timeout": max(2.0, float(timeout))}
    try:
        response = client.get(ENDPOINT, params={"lat": lat, "lon": lon},
                              headers={"User-Agent": USER_AGENT}, **extra)
    except Exception as exc:  # noqa: BLE001
        return None, f"the forecast service did not answer: {type(exc).__name__}"
    if response.status_code == 429:
        return None, ("the forecast service is busy (HTTP 429): it asks clients to send "
                      "fewer requests per second")
    if response.status_code != 200:
        return None, f"the forecast service answered HTTP {response.status_code}"
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return None, "the forecast service did not answer JSON"
    properties = (payload or {}).get("properties") or {}
    series = properties.get("timeseries") or []
    if not series:
        return None, "the forecast service answered no timeseries for this point"
    return payload, ""


def _number(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _temperature(celsius, units: str):
    if celsius is None:
        return None
    if units == "imperial":
        return round(celsius * C_TO_F + 32.0, 2)
    return round(celsius, 2)


def _wind(metres, units: str):
    if metres is None:
        return None
    if units == "imperial":
        return round(metres * MS_TO_MPH, 2)
    return round(metres * 3.6, 2)


def _precipitation(millimetres, units: str):
    """The source gives millimetres; an imperial buyer reads inches, not millimetres.

    A single `units` field per row meant 1.4 mm was sold as 1.4 inch: the same number,
    25.4 times the real amount, silently and for money.
    """
    if millimetres is None:
        return None
    if units == "imperial":
        return round(millimetres / MM_PER_INCH, 2)
    return round(millimetres, 2)


def row(where: dict, payload: dict, *, raw: str, index: int, units: str,
        hours: int, with_daily: bool) -> dict:
    """One delivered point: the first N hours of the series, and daily high and low."""
    properties = (payload or {}).get("properties") or {}
    series = properties.get("timeseries") or []
    points: list = []
    daily: dict = {}
    for entry in series[:max(1, hours)]:
        moment = entry.get("time")
        data = (entry.get("data") or {}).get("instant", {}).get("details") or {}
        # The source is hourly near the point and every 6 or 12 hours further out: reading
        # only next_1_hours left 10 of 72 points without a symbol or precipitation, so the
        # daily sum undercounted and the promise "each entry carries these fields" lied.
        step_hours = 1
        following = (entry.get("data") or {}).get("next_1_hours") or {}
        if not following:
            following = (entry.get("data") or {}).get("next_6_hours") or {}
            step_hours = 6
        if not following:
            following = (entry.get("data") or {}).get("next_12_hours") or {}
            step_hours = 12
        symbol = ((following.get("summary") or {}).get("symbol_code"))
        rain_mm = _number((following.get("details") or {}).get("precipitation_amount"))
        celsius = _number(data.get("air_temperature"))
        points.append({
            "time": moment,
            "temperature": _temperature(celsius, units),
            "windSpeed": _wind(_number(data.get("wind_speed")), units),
            "windDirection": _number(data.get("wind_from_direction")),
            "humidity": _number(data.get("relative_humidity")),
            "pressure": _number(data.get("air_pressure_at_sea_level")),
            "cloudCover": _number(data.get("cloud_area_fraction")),
            "precipitation": _precipitation(rain_mm, units),
            "precipitationStepHours": step_hours,
            "symbol": symbol,
        })
        if with_daily and moment:
            day = str(moment)[:10]
            bucket = daily.get(day)
            if bucket is None:
                bucket = {"date": day, "temperatureMin": None, "temperatureMax": None,
                          "precipitationSum": 0.0}
                daily[day] = bucket
            if celsius is not None:
                if bucket["temperatureMin"] is None or celsius < bucket["temperatureMin"]:
                    bucket["temperatureMin"] = celsius
                if bucket["temperatureMax"] is None or celsius > bucket["temperatureMax"]:
                    bucket["temperatureMax"] = celsius
            if rain_mm is not None:
                bucket["precipitationSum"] = round(bucket["precipitationSum"] + rain_mm, 2)
    days_out = []
    if with_daily:
        for bucket in daily.values():
            days_out.append({
                "date": bucket["date"],
                "temperatureMin": _temperature(bucket["temperatureMin"], units),
                "temperatureMax": _temperature(bucket["temperatureMax"], units),
                "precipitationSum": _precipitation(bucket["precipitationSum"], units),
            })
    return {
        "latitude": where.get("latitude"),
        "longitude": where.get("longitude"),
        "units": units,
        "source": "MET Norway Locationforecast",
        "updatedAt": ((properties.get("meta") or {}).get("updated_at")),
        "hours": len(points),
        "hourly": points,
        "daily": days_out or None,
        "actorInput": raw,
        "actorIndex": index,
        "status": "OK",
        "reason": "",
    }
