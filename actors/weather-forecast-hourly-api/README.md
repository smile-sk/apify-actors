# Weather Forecast API - Hourly Series and Daily High/Low by Coordinates

You should not pay for a place that was never read. Send coordinates, get the hourly
series back, and every pair that is not a real point comes back free with the reason
instead of an empty row and a charge.

## What it returns

One row per coordinate pair:

* `latitude`, `longitude`, `units`, `hours` (how many points came back), `updatedAt`
  (when the model ran), `source` (the attribution string);
* `hourly`: the first N points of the series, each entry with `time`, `temperature`,
  `windSpeed`, `windDirection`, `humidity`, `pressure`, `cloudCover`, `precipitation`,
  `precipitationStepHours` and the weather `symbol`. Every entry carries all of them: near
  the point the source is hourly, further out it gives a 6 or 12 hour step, and the step
  used is named instead of leaving the field empty;
* `daily` (optional): one entry per day with `temperatureMin`, `temperatureMax` and
  `precipitationSum`, computed from the same series, so nothing is invented to fill it.

Units are `metric` (celsius, km/h, mm) or `imperial` (fahrenheit, mph, inch). The source
gives celsius, metres per second and millimetres; the conversion is ours and covers all
three, including the precipitation of each point and the daily sum. Millimetres are divided
by 25.4, so an imperial row never shows a metric amount under an imperial label.

## Input

`locations` is a list of `latitude,longitude` pairs, for example `50.45,30.52`. Names are
not accepted: geocoding is a different service with its own limits, and pulling it in
would add a wall to a source that has none. `hours` is 1 to 72, `maxLocations` is 1 to 500,
`includeDaily` adds the daily summary.

## What is free and what is charged

Charged only for a place whose series was really read and written to the dataset, at
$0.0005 per place. Free, with a status and a plain reason:

* `INVALID_INPUT`: not a coordinate pair, or outside the real ranges;
* `DUPLICATE`: the same pair was already sent or already delivered in this run;
* `NOT_CHECKED_LIMIT`: past `maxLocations`;
* `NOT_CHECKED_CAP`: the spending limit or the remaining budget ended before this place;
* `NOT_CHECKED_TIME`: the run was running out of its five minutes;
* `NOT_CHECKED_BILLING`: the price of this event could not be read, so nothing is claimed
  as sold;
* `NOT_CHECKED_ERROR`: the source did not answer for this place, with the reason named.

The lowest spending limit you can set per run is $0.05. That number is a floor for
your own limit, not a fee: one pair costs a fraction of a cent, and a run of a
hundred pairs costs five cents.

If every coordinate pair was asked for and none was read, the run fails and says how many
attempts were made and why the first one failed. A different failure is an input with no
coordinate pair at all: the message then names the number of places that were not pairs and
gives the expected format, because the source was never asked and nothing is charged. A run
that read nothing is never reported as a success, because a green run with an empty dataset
is the most expensive kind of quiet.

## Source and attribution

Forecast data is from the Norwegian Meteorological Institute (MET Norway), Locationforecast
2.0, published under CC-BY 4.0. Attribution is required and is given here and in every run
summary (`source`). Requests carry a User-Agent with a contact, as their terms ask. The
service is free of charge, including commercial use, and needs no API key.

We do not use Open-Meteo for this actor: their free endpoint is for non-commercial use
only, and a paid actor on the Store is commercial use.

## Limits

* `hours` up to 72; the source covers about nine days and the first two are hourly.
* `maxLocations` up to 500 per run.
* The run is designed to finish inside the platform's five minute window; places it could
  not reach are listed as free rows with a status, never silently dropped.
* Memory is 256 MB, which is enough because a run holds one timeseries at a time.

## What this actor does not do

* It does not accept city names, and it does not geocode.
* It does not give historical weather, only the forecast the source publishes now.
* It does not smooth or interpolate the series: what the source says is what a row says,
  and a missing value stays empty rather than becoming a zero.
* It does not promise that a place is covered by a local model; the source decides, and a
  place it cannot answer for comes back free.
