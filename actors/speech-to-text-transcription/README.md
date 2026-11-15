# Speech to Text Transcription - Whisper, Audio to Text

Turns audio files into text with Whisper, on the actor's own CPU, with no API key and no
per minute bill from a third party. Give it a list of http(s) addresses of audio files and
get one row per file: the transcript, the detected language, the duration, segments with
timestamps and optionally ready SRT and VTT.

## Why this actor exists

The complaint in this niche is always the same: a run comes back green, the dataset is
empty or full of nonsense, and the buyer paid for it. It happens for boring reasons that a
plain Whisper wrapper never checks:

* the URL answers 404 or a login page, and the HTML is fed to the model as if it were audio;
* the file is two hours of silence and the model invents sentences to fill it;
* the file is a two hour podcast and the run dies at the platform timeout with no summary;
* the file lives on `127.0.0.1` or in the container metadata and should never be read at all.

Here every input line gets a row. Either it is a transcript, or it is a status with a plain
reason, and only a delivered transcript is charged.

## What a run does

1. Checks the URL: only http(s), only a public address, and every redirect hop is checked
   again. A private or local address is refused before a socket is opened.
2. Downloads with a byte limit (`maxFileMb`, 100 MB by default).
3. Reads the duration from the container header before decoding anything, and refuses a
   file longer than `maxMinutesPerFile` (10 by default) with its real length in the row.
4. Decodes with PyAV to 16 kHz mono. Anything that is not audio becomes
   `NOT_CHECKED_UNSUPPORTED` and costs nothing.
5. Transcribes with the chosen Whisper model (all three are already inside the image, so a
   run never downloads weights), optionally skipping silence with voice activity detection.
6. Before a file starts, plans it with numbers measured on one thread plus a margin and
   the model load, and refuses the file when the platform clock cannot fit it: a free
   `NOT_CHECKED_TIME` row instead of a run killed in the middle of a file.
7. Writes the row and charges for it in one call, then repeats until the list, the file
   limit, the spending cap or the platform clock says stop.

## Pricing

Pay per event, `audio-transcribed`: **$0.01 for one delivered transcript**. A file that
comes back with any `NOT_CHECKED_*` status is free, including a file longer than the limit,
an oversized one, a dead link and an address that was refused. The platform charges compute
separately, and compute is the real cost of running Whisper yourself: memory in GB times
hours, so ten minutes of audio on `small` at 4096 MB is about 1.9 compute units and on
`base` about 0.8. Nothing is charged for a file the clock could not fit.

## Input

| Field | Meaning |
| --- | --- |
| `audioUrls` | http(s) addresses, one per line. mp3, wav, m4a, ogg, opus, flac, webm, mp4: anything PyAV opens. |
| `model` | `tiny`, `base` or `small` (default). `small` reads names and accented speech noticeably better. |
| `language` | Two or three letter code, or empty to detect it from the audio. |
| `maxMinutesPerFile` | Default 10, maximum 120. Longer files are refused, named with their real length, and not charged. |
| `maxFileMb` | Default 100. The download stops at the limit, so a huge file cannot eat the run. |
| `maxFiles` | Default 10, `0` means no limit. Files past the limit get a free `NOT_CHECKED_LIMIT` row. |
| `includeSegments` | Timestamped phrases in the row, default on. |
| `includeSubtitles` | Builds `srt` and `vtt` strings from the same segments, default off. |
| `vadFilter` | Drops silence and music before the model sees them, default on. |

## Output

A transcript row carries `status: TRANSCRIBED`, `text`, `language`, `languageProbability`,
`durationSecs`, `audioMinutes`, `model`, `charCount`, `wordCount`, `segments`, and `billable`.
Every other row keeps `index`, `url` and `fileName` and says what happened:

`NOT_CHECKED_INVALID_URL`, `NOT_CHECKED_PRIVATE_TARGET`, `NOT_CHECKED_FETCH_ERROR`,
`NOT_CHECKED_TOO_LARGE`, `NOT_CHECKED_UNSUPPORTED`, `NOT_CHECKED_TOO_LONG`,
`NOT_CHECKED_TIME`, `NOT_CHECKED_LIMIT`, `NOT_CHECKED_DUPLICATE`, `NOT_CHECKED_BILLING`,
`NOT_CHECKED_CAP`, `NOT_CHECKED_ERROR`.

The run summary lives in the key-value store under `RUN_SUMMARY-<runId>` with a plain
`RUN_SUMMARY` copy in the same run store, and it separates what happened to files from what
happened to money: `filesTranscribed`, `audioMinutes`, `chargedEvents`, `deliveredUncharged`,
`billingActive`, `stopReason`, `byStatus`. If the numbers ever fail to add up, the summary
says so in `counterMismatch` or `moneyMismatch` instead of rounding quietly.

## What this actor does not do

* No speaker diarization. One voice or five, the transcript does not say who spoke.
* No translation. It writes what was said, in the language it was said.
* No word level timestamps, only phrase level.
* No scraping of YouTube, Instagram or TikTok pages: the input is a direct media address.
  A page that is not audio comes back as `NOT_CHECKED_UNSUPPORTED`.
* No private or local addresses, by design, and no way to switch that off.

## Honest numbers

The same 11 second 16 kHz file, faster-whisper 1.2.1 with int8, on a laptop class CPU (so
not a promise about the platform's hardware):

| model | one thread | two threads |
| --- | --- | --- |
| `base` | 0.94 s of work per second of audio | 0.54 |
| `small` (default) | **2.31** | 1.37 |

Loading the model costs about 7 seconds, once per run. Worker threads follow the container
budget, one core per 4096 MB, so the default 4096 MB run gets **one** thread and the `small`
numbers in that column are the ones that apply.

That is the number that matters: `small` on one thread is 2.3 seconds of work per second of
audio, so a 10 minute file is about 23 minutes of compute, and `base` is about 9. Before each
file the actor plans with exactly these numbers, a 20 percent margin and the model load, and
refuses the file when the platform clock cannot fit it: a free `NOT_CHECKED_TIME` row and the
run stops by its own clock with `stopReason: deadline`, instead of being killed mid file.
For long recordings give the run 8192 MB (two threads, about 1.4 s per second on `small`, and
the wall time halves) or choose `base`.

## Development

```
python tests/test_audio.py   # 68 checks, real decoding, the address guard and the plan
python tests/test_main.py    # 132 checks, the money path on a fake Actor that follows apify 4.2
python -m src.main           # local run; without a price it refuses to give transcripts away
```
