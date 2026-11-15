"""The pure parts: what counts as a coordinate, how units convert, how a row is built."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import weather  # noqa: E402

PASSED = 0
FAILED: list = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


SAMPLE = {
    "properties": {
        "meta": {"updated_at": "2026-11-14T09:20:20Z"},
        "timeseries": [
            {"time": "2026-11-14T10:00:00Z",
             "data": {"instant": {"details": {"air_temperature": 6.3, "wind_speed": 6.0,
                                              "relative_humidity": 61.8,
                                              "air_pressure_at_sea_level": 1014.5,
                                              "cloud_area_fraction": 100.0,
                                              "wind_from_direction": 287.4}},
                      "next_1_hours": {"summary": {"symbol_code": "cloudy"},
                                       "details": {"precipitation_amount": 0.0}}}},
            {"time": "2026-11-14T11:00:00Z",
             "data": {"instant": {"details": {"air_temperature": 4.0, "wind_speed": 3.0}},
                      "next_1_hours": {"summary": {"symbol_code": "rain"},
                                       "details": {"precipitation_amount": 1.4}}}},
            {"time": "2026-11-15T00:00:00Z",
             "data": {"instant": {"details": {"air_temperature": 9.0, "wind_speed": 1.0}},
                      "next_1_hours": {"summary": {"symbol_code": "clear_sky"},
                                       "details": {"precipitation_amount": 0.2}}}},
        ],
    }
}


def run_all() -> int:
    # coordinates
    check("coords: a plain pair", weather.coordinates("50.45,30.52") == (50.45, 30.52))
    check("coords: spaces and a semicolon", weather.coordinates(" 50.45 ; 30.52 ")
          == (50.45, 30.52))
    check("coords: negative pair", weather.coordinates("-33.86,-70.65") == (-33.86, -70.65))
    check("coords: latitude above the pole is refused", weather.coordinates("95,30") is None)
    check("coords: longitude out of range is refused", weather.coordinates("50,200") is None)
    check("coords: a city name is not a pair", weather.coordinates("Kyiv") is None)
    check("coords: an empty string is not a pair", weather.coordinates("") is None)
    check("coords: one number is not a pair", weather.coordinates("50.45") is None)
    check("coords: an explicit plus is a pair, not rubbish",
          weather.coordinates("+50.45, 30.52") == (50.45, 30.52))

    # units
    check("units: celsius stays celsius", weather._temperature(6.3, "metric") == 6.3)
    check("units: zero celsius is 32 fahrenheit",
          weather._temperature(0.0, "imperial") == 32.0)
    check("units: wind in km/h", weather._wind(1.0, "metric") == 3.6)
    check("units: wind in mph", weather._wind(1.0, "imperial") == 2.24)
    check("units: a missing temperature stays empty",
          weather._temperature(None, "metric") is None)
    check("units: precipitation stays millimetres in metric",
          weather._precipitation(1.4, "metric") == 1.4)
    check("units: precipitation is converted to inches, not relabelled",
          weather._precipitation(25.4, "imperial") == 1.0)
    check("units: 1.4 mm is 0.06 inch", weather._precipitation(1.4, "imperial") == 0.06)
    check("units: a missing precipitation stays empty",
          weather._precipitation(None, "imperial") is None)

    # a row
    where = {"latitude": 50.45, "longitude": 30.52}
    row = weather.row(where, SAMPLE, raw="50.45,30.52", index=0, units="metric",
                      hours=3, with_daily=True)
    check("row: three hourly points", len(row["hourly"]) == 3)
    check("row: the status is OK and the reason empty",
          row["status"] == "OK" and row["reason"] == "")
    check("row: the source is named", row["source"].startswith("MET Norway"))
    check("row: updatedAt comes from the model", row["updatedAt"] == "2026-11-14T09:20:20Z")
    check("row: the first point carries a temperature", row["hourly"][0]["temperature"] == 6.3)
    check("row: the symbol is kept", row["hourly"][1]["symbol"] == "rain")
    check("row: two days in the daily summary", len(row["daily"]) == 2)
    day_one = [d for d in row["daily"] if d["date"] == "2026-11-14"][0]
    check("row: daily minimum is the coldest hour", day_one["temperatureMin"] == 4.0)
    check("row: daily maximum is the warmest hour", day_one["temperatureMax"] == 6.3)
    check("row: precipitation adds up", day_one["precipitationSum"] == 1.4)
    check("row: the input is echoed", row["actorInput"] == "50.45,30.52")

    short = weather.row(where, SAMPLE, raw="50.45,30.52", index=0, units="metric",
                        hours=1, with_daily=False)
    check("hours: one hour means one point", len(short["hourly"]) == 1)
    check("hours: daily is empty when it was not asked for", short["daily"] is None)
    imperial = weather.row(where, SAMPLE, raw="x", index=0, units="imperial", hours=3,
                           with_daily=True)
    check("row: imperial converts the series",
          imperial["hourly"][0]["temperature"] == 43.34)
    check("row: imperial converts the point precipitation too",
          imperial["hourly"][1]["precipitation"] == 0.06)
    check("row: imperial converts the daily sum too",
          [d for d in imperial["daily"] if d["date"] == "2026-11-14"][0]
          ["precipitationSum"] == 0.06)

    # The source is hourly near the point and every 6 hours further out: an entry without
    # next_1_hours must still carry a symbol and precipitation, and say which step it is.
    spread = {"properties": {"timeseries": [
        {"time": "2026-11-14T10:00:00Z",
         "data": {"instant": {"details": {"air_temperature": 3.0}},
                  "next_6_hours": {"summary": {"symbol_code": "snow"},
                                   "details": {"precipitation_amount": 2.5}}}},
    ]}}
    far = weather.row(where, spread, raw="x", index=0, units="metric", hours=1,
                      with_daily=True)
    check("step: a 6 hour entry keeps the symbol", far["hourly"][0]["symbol"] == "snow")
    check("step: a 6 hour entry keeps the precipitation",
          far["hourly"][0]["precipitation"] == 2.5)
    check("step: the entry names the step it came from",
          far["hourly"][0]["precipitationStepHours"] == 6)
    check("step: the hourly entry names a 1 hour step",
          row["hourly"][0]["precipitationStepHours"] == 1)

    empty = weather.row(where, {"properties": {"timeseries": []}}, raw="x", index=0,
                        units="metric", hours=5, with_daily=True)
    check("row: an empty series gives no points and no invented values",
          empty["hourly"] == [] and empty["daily"] is None)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    code = run_all()
    for name in FAILED:
        print("FAIL:", name)
    print(f"PASSED {PASSED} FAILED {len(FAILED)}")
    sys.exit(code)
