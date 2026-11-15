"""Apify Actor: speech to text transcription with Whisper, priced per delivered file.

Input  : `audioUrls` - http(s) addresses of audio files; optional `model`, `language`,
         `maxMinutesPerFile`, `maxFileMb`, `maxFiles`, `includeSegments`,
         `includeSubtitles`, `vadFilter`.
Output : one row per input line: the transcript (text, language, duration, segments,
         SRT and VTT when asked) or a status row that names why this file is not
         there and confirms it was not charged.
Charge : `audio-transcribed`, only for a transcript that reached the dataset. A file
         that is too long, too large, not audio, refused by the server, private, over
         the spending cap or cut by the clock costs nothing and says so.

Why the file is downloaded by this actor instead of being handed to Whisper: Whisper
alone cannot tell a refused page from a silent file, and a buyer who pays for an HTML
error page once never comes back. Here the bytes are checked, the container header is
read for the duration, and only real audio reaches the model.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version

import httpx
from apify import Actor

from src import audio as audio_mod
from src import speech
from src import statuses as st

CHARGE_EVENT = "audio-transcribed"
MAX_CONSECUTIVE_CHARGE_ERRORS = 3
MAX_CONSECUTIVE_WRITE_ERRORS = 3
MAX_CONSECUTIVE_MODEL_ERRORS = 3
DEADLINE_RESERVE_SEC = 25.0
DEFAULT_MAX_MINUTES = 10
DEFAULT_MAX_FILES = 10
DEFAULT_MAX_FILE_MB = 100
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$")
FETCH_TIMEOUT_SEC = 60.0


def _dep_versions() -> dict:
    out = {}
    for name in ("apify", "httpx", "faster-whisper", "ctranslate2", "av"):
        try:
            out[name] = _pkg_version(name)
        except PackageNotFoundError:
            out[name] = None
    return out


def _platform_budget_secs() -> float | None:
    """Seconds left before the platform kills this run, or None when it does not say."""
    raw = os.environ.get("ACTOR_TIMEOUT_AT")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    left = moment.timestamp() - time.time()
    return left if left > 0 else 0.0


def learn_rtf(previous: float | None, spent: float, duration: float) -> float | None:
    """What this run has learned about its own speed, in seconds per second of audio.

    A measurement from a five second file is noise, and one lucky fast file must not lower
    the plan for the long ones that follow: only files of twenty seconds or more teach the
    run, and the slowest of them wins.
    """
    if duration < 20.0:
        return previous
    measured = spent / duration
    return measured if previous is None else max(previous, measured)


def _as_int(value, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _row(index: int, url: str, file_name: str | None = None) -> dict:
    return {
        "rowType": "audio",
        "index": index,
        "url": url,
        "fileName": file_name,
        "status": None,
        "reason": None,
        "billable": False,
    }


def _status_row(index: int, url: str, status: str, reason: str,
                file_name: str | None = None) -> dict:
    row = _row(index, url, file_name)
    row["status"] = status
    row["reason"] = reason
    return row


def _transcript_row(index: int, downloaded, result: dict, model_name: str,
                    include_segments: bool, include_subtitles: bool) -> dict:
    row = _row(index, downloaded.url, downloaded.file_name)
    row.update({
        "status": st.TRANSCRIBED,
        "reason": None,
        "bytes": downloaded.bytes_read,
        "contentType": downloaded.content_type,
        "durationSecs": round(result["duration"] or 0.0, 2),
        "audioMinutes": round((result["duration"] or 0.0) / 60.0, 3),
        "language": result["language"],
        "languageProbability": result["languageProbability"],
        "model": model_name,
        "text": result["text"],
        "charCount": len(result["text"]),
        "wordCount": len(result["text"].split()),
        "billable": True,
    })
    if include_segments:
        row["segments"] = result["segments"]
    if include_subtitles:
        row["srt"] = speech.to_srt(result["segments"])
        row["vtt"] = speech.to_vtt(result["segments"])
    return row


async def main() -> None:
    async with Actor:
        raw_input = await Actor.get_input() or {}
        raw_urls = raw_input.get("audioUrls") or []
        if isinstance(raw_urls, str):
            raw_urls = raw_urls.splitlines()
        lines = [str(x).strip() for x in (raw_urls or []) if str(x).strip()]

        model_name = str(raw_input.get("model") or "small").strip().lower()
        if model_name not in speech.MODELS:
            await Actor.fail(status_message=(
                f"The model {model_name!r} is not offered. Choose one of "
                f"{', '.join(speech.MODELS)}. Nothing was requested and nothing was charged."))
            return
        language = str(raw_input.get("language") or "").strip().lower()
        if language and not LANGUAGE_RE.match(language):
            await Actor.fail(status_message=(
                f"The language {language!r} is not a two or three letter code such as en, de "
                "or uk. Leave the field empty to detect the language from the audio. "
                "Nothing was requested and nothing was charged."))
            return

        max_minutes = max(1, _as_int(raw_input.get("maxMinutesPerFile"), DEFAULT_MAX_MINUTES))
        max_files = max(0, _as_int(raw_input.get("maxFiles"), DEFAULT_MAX_FILES))
        max_file_mb = max(1, _as_int(raw_input.get("maxFileMb"), DEFAULT_MAX_FILE_MB))
        include_segments = _as_bool(raw_input.get("includeSegments"), True)
        include_subtitles = _as_bool(raw_input.get("includeSubtitles"), False)
        vad_filter = _as_bool(raw_input.get("vadFilter"), True)
        max_bytes = max_file_mb * 1024 * 1024
        threads = speech.worker_threads()

        summary = {
            "filesAsked": len(lines),
            "filesUnique": 0,
            "filesInvalid": 0,
            "duplicateFiles": 0,
            "filesCutByLimit": 0,
            "filesTranscribed": 0,
            "filesCap": 0,
            "filesWriteError": 0,
            "filesChargeUnverified": 0,
            "filesModelError": 0,
            "audioMinutes": 0.0,
            "rowsDelivered": 0,
            "chargedEvents": 0,
            "deliveredUncharged": 0,
            "billingActive": True,
            "billingReason": None,
            "stopReason": None,
            "deadlineCapped": False,
            "model": model_name,
            "threads": threads,
            "languageRequested": language or None,
            "maxMinutesPerFile": max_minutes,
            "maxFileMb": max_file_mb,
            "maxFiles": max_files,
            "vadFilter": vad_filter,
            "includeSegments": include_segments,
            "includeSubtitles": include_subtitles,
            "byStatus": {},
            "chargeErrors": [],
            "datasetWriteErrors": 0,
            "datasetItemCount": None,
            "datasetItemCountApi": None,
            "datasetItemCountNote": None,
            "versions": _dep_versions(),
        }
        charge_errors: list = summary["chargeErrors"]
        consecutive_charge_errors = 0
        consecutive_write_errors = 0
        consecutive_model_errors = 0
        billing_active = True
        billing_known = True
        stop_reason: str | None = None
        run_stopped = False
        started = time.monotonic()
        budget = _platform_budget_secs()
        measured_rtf: float | None = None

        def deadline_left() -> float | None:
            if budget is None:
                return None
            return budget - (time.monotonic() - started) - DEADLINE_RESERVE_SEC

        try:
            pricing = Actor.get_charging_manager().get_pricing_info()
        except Exception:  # noqa: BLE001 - a local run carries no pricing
            pricing = None
        if pricing is None:
            billing_known = False
            billing_active = False
            summary["billingActive"] = False
            summary["billingReason"] = "the price of this run could not be read"
        else:
            prices = getattr(pricing, "per_event_prices", None) or {}
            if not getattr(pricing, "is_pay_per_event", False):
                billing_active = False
                summary["billingActive"] = False
                summary["billingReason"] = "this run is not priced per event"
            elif CHARGE_EVENT not in prices:
                billing_active = False
                summary["billingActive"] = False
                summary["billingReason"] = f"the price list has no {CHARGE_EVENT!r} event"
                Actor.log.error("pricing has no %r event: this run delivers rows without charging",
                                CHARGE_EVENT)

        def count(status: str) -> None:
            bucket = summary["byStatus"]
            bucket[status] = bucket.get(status, 0) + 1

        async def item_count():
            try:
                dataset = await Actor.open_dataset()
                meta = await dataset.get_metadata()
            except Exception:  # noqa: BLE001 - metadata is a check, not the result
                return None
            return getattr(meta, "item_count", None)

        async def push(row: dict, count_failure: bool = True) -> bool:
            """count_failure=False for the status row that explains a failed write:
            one refused file is one failed write, not two."""
            nonlocal consecutive_write_errors, run_stopped, stop_reason
            try:
                await Actor.push_data(row)
            except Exception as exc:  # noqa: BLE001 - the dataset refused the row
                Actor.log.error("dataset refused a row: %s", exc)
                if not count_failure:
                    return False
                consecutive_write_errors += 1
                summary["datasetWriteErrors"] += 1
                if consecutive_write_errors >= MAX_CONSECUTIVE_WRITE_ERRORS:
                    run_stopped, stop_reason = True, "dataset-write-error"
                return False
            consecutive_write_errors = 0
            summary["rowsDelivered"] += 1
            return True

        async def deliver(row: dict) -> None:
            """One transcript, one paid row. The charge rides on the write, never apart."""
            nonlocal billing_active, consecutive_charge_errors, consecutive_write_errors
            nonlocal run_stopped, stop_reason
            if not billing_active:
                # the row is written before the charge, so the claim has to be made here:
                # a row that was never charged must not say billable true
                row["billable"] = False
                row["billingInactive"] = True
                if await push(row):
                    summary["filesTranscribed"] += 1
                    summary["deliveredUncharged"] += 1
                    summary["audioMinutes"] = round(
                        summary["audioMinutes"] + float(row.get("audioMinutes") or 0.0), 3)
                    count(st.TRANSCRIBED)
                return
            try:
                result = await Actor.push_data(row, charged_event_name=CHARGE_EVENT)
            except Exception as exc:  # noqa: BLE001 - money may or may not have been taken
                consecutive_charge_errors += 1
                charge_errors.append({"index": row.get("index"), "url": row.get("url"),
                                      "error": f"{type(exc).__name__}: {exc}"[:300]})
                Actor.log.error("the write or the charge failed for %s: %s", row.get("url"), exc)
                after = await item_count()
                if after is not None and after > summary["rowsDelivered"]:
                    summary["rowsDelivered"] += 1
                    summary["filesTranscribed"] += 1
                    summary["deliveredUncharged"] += 1
                    summary["audioMinutes"] = round(
                        summary["audioMinutes"] + float(row.get("audioMinutes") or 0.0), 3)
                    count(st.TRANSCRIBED)
                elif after is not None and after == summary["rowsDelivered"]:
                    consecutive_charge_errors = 0
                    consecutive_write_errors += 1
                    summary["datasetWriteErrors"] += 1
                    summary["filesWriteError"] += 1
                    count(st.ERROR)
                    await push(_status_row(int(row.get("index") or 0), str(row.get("url") or ""),
                                           st.ERROR,
                                           "the dataset refused this row: it is not stored and "
                                           "not charged", row.get("fileName")),
                               count_failure=False)
                    if consecutive_write_errors >= MAX_CONSECUTIVE_WRITE_ERRORS:
                        run_stopped, stop_reason = True, "dataset-write-error"
                    return
                else:
                    summary["filesChargeUnverified"] += 1
                    count(st.CHARGE_UNVERIFIED)
                if consecutive_charge_errors >= MAX_CONSECUTIVE_CHARGE_ERRORS:
                    run_stopped, stop_reason = True, "charge-error"
                return
            consecutive_charge_errors = 0
            billed = int(getattr(result, "charged_count", 0) or 0)
            limit_reached = bool(getattr(result, "event_charge_limit_reached", False))
            if billed:
                summary["rowsDelivered"] += 1
                summary["chargedEvents"] += billed
                summary["filesTranscribed"] += 1
                summary["audioMinutes"] = round(
                    summary["audioMinutes"] + float(row.get("audioMinutes") or 0.0), 3)
                count(st.TRANSCRIBED)
                if limit_reached:
                    run_stopped, stop_reason = True, "spending-cap"
            elif limit_reached:
                summary["filesCap"] += 1
                count(st.CAP)
                await push(_status_row(int(row.get("index") or 0), str(row.get("url") or ""),
                                       st.CAP,
                                       "the spending cap was reached, so this file is not "
                                       "delivered and not charged", row.get("fileName")))
                run_stopped, stop_reason = True, "spending-cap"
            else:
                # the platform took no money for a delivered row: stop claiming to bill
                billing_active = False
                summary["billingActive"] = False
                summary["billingReason"] = "the platform reported no charge for a delivered row"
                summary["rowsDelivered"] += 1
                summary["filesTranscribed"] += 1
                summary["deliveredUncharged"] += 1
                summary["audioMinutes"] = round(
                    summary["audioMinutes"] + float(row.get("audioMinutes") or 0.0), 3)
                count(st.TRANSCRIBED)

        client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(FETCH_TIMEOUT_SEC),
            headers={"User-Agent": "ApifySpeechToText/0.1 (+https://apify.com/stas_k)"},
        )
        seen_urls: set[str] = set()
        model = None
        accepted = 0

        try:
            for index, line in enumerate(lines, start=1):
                if run_stopped:
                    # the status must name the real stop: "out of time" on a charge or a
                    # dataset failure sends the buyer looking in the wrong place
                    if stop_reason == "spending-cap":
                        status = st.CAP
                        summary["filesCap"] += 1
                    elif stop_reason == "deadline":
                        status = st.TIME
                    else:
                        status = st.ERROR
                    reason = ("the run stopped earlier: " + str(stop_reason) +
                              "; this file is not delivered and not charged")
                    count(status)
                    await push(_status_row(index, line, status, reason))
                    continue
                url = audio_mod.normalize_url(line)
                if url is None:
                    summary["filesInvalid"] += 1
                    count(st.INVALID_URL)
                    await push(_status_row(index, line, st.INVALID_URL,
                                           "the line is not an http(s) address of an audio file"))
                    continue
                if url in seen_urls:
                    summary["duplicateFiles"] += 1
                    count(st.DUPLICATE)
                    await push(_status_row(index, url, st.DUPLICATE,
                                           "the same file came earlier in this run, it is "
                                           "transcribed once"))
                    continue
                seen_urls.add(url)
                summary["filesUnique"] += 1
                if max_files and accepted >= max_files:
                    summary["filesCutByLimit"] += 1
                    count(st.LIMIT)
                    await push(_status_row(index, url, st.LIMIT,
                                           f"the list was cut at maxFiles={max_files}"))
                    continue
                accepted += 1

                if not billing_known:
                    count(st.BILLING)
                    await push(_status_row(index, url, st.BILLING,
                                           "the price of this run could not be read, so this "
                                           "file is not delivered and not charged"))
                    continue

                prior = (measured_rtf if measured_rtf is not None
                         else speech.prior_rtf(model_name, threads))
                # a first gate that does not guess the file length: with less than a minute
                # left there is no honest way to start, and the platform QA runs with five
                left = deadline_left()
                if left is not None and left < 60.0:
                    summary["deadlineCapped"] = True
                    run_stopped, stop_reason = True, "deadline"
                    count(st.TIME)
                    await push(_status_row(index, url, st.TIME,
                                           "the run is out of time: this file is not delivered "
                                           "and not charged"))
                    continue

                try:
                    downloaded = await audio_mod.download(client, url, max_bytes)
                except audio_mod.AudioError as exc:
                    count(exc.status)
                    await push(_status_row(index, url, exc.status, exc.reason))
                    continue
                except Exception as exc:  # noqa: BLE001 - one junk line must never end the run
                    # a foreign address fails in ways nobody listed (a label of 64
                    # characters, a broken bracket, a surprise from the ssl layer): the
                    # line gets its status row and the rest of the list still runs
                    Actor.log.error("the file could not be fetched: %s", exc)
                    count(st.FETCH_ERROR)
                    await push(_status_row(index, url, st.FETCH_ERROR,
                                           "the file could not be fetched: "
                                           f"{type(exc).__name__}: {str(exc)[:200]}"))
                    continue

                header_duration = await asyncio.to_thread(audio_mod.probe_duration,
                                                          downloaded.data)
                if header_duration is not None and header_duration > max_minutes * 60.0:
                    count(st.TOO_LONG)
                    await push(_status_row(
                        index, url, st.TOO_LONG,
                        f"the file runs {header_duration / 60.0:.1f} minutes and the limit is "
                        f"maxMinutesPerFile={max_minutes}: raise the limit to transcribe it",
                        downloaded.file_name))
                    continue

                try:
                    samples = await asyncio.to_thread(audio_mod.decode, downloaded.data)
                except audio_mod.AudioError as exc:
                    count(exc.status)
                    await push(_status_row(index, url, exc.status, exc.reason,
                                           downloaded.file_name))
                    continue

                duration = header_duration or (samples.size / float(audio_mod.SAMPLE_RATE))
                if duration > max_minutes * 60.0:
                    count(st.TOO_LONG)
                    await push(_status_row(
                        index, url, st.TOO_LONG,
                        f"the file runs {duration / 60.0:.1f} minutes and the limit is "
                        f"maxMinutesPerFile={max_minutes}: raise the limit to transcribe it",
                        downloaded.file_name))
                    continue

                # the plan carries the model load on the first file and a margin, so the
                # gate refuses what cannot finish instead of letting the platform kill it
                estimate = speech.plan_seconds(duration, prior, model is not None)
                left = deadline_left()
                if left is not None and left < estimate + 10.0:
                    summary["deadlineCapped"] = True
                    run_stopped, stop_reason = True, "deadline"
                    count(st.TIME)
                    await push(_status_row(index, url, st.TIME,
                                           "the run is out of time for this file: it is not "
                                           "delivered and not charged", downloaded.file_name))
                    continue

                try:
                    if model is None:
                        model = await asyncio.to_thread(speech.load_model, model_name, threads)
                    began = time.monotonic()
                    result = await asyncio.to_thread(speech.transcribe, model, samples,
                                                     language or None, vad_filter)
                    spent = time.monotonic() - began
                except Exception as exc:  # noqa: BLE001 - a broken model must not kill the run
                    consecutive_model_errors += 1
                    summary["filesModelError"] += 1
                    count(st.ERROR)
                    Actor.log.error("transcription failed for %s: %s", url, exc)
                    await push(_status_row(index, url, st.ERROR,
                                           "the transcription failed: "
                                           f"{type(exc).__name__}: {str(exc)[:200]}",
                                           downloaded.file_name))
                    if consecutive_model_errors >= MAX_CONSECUTIVE_MODEL_ERRORS:
                        run_stopped, stop_reason = True, "transcription-error"
                    continue
                consecutive_model_errors = 0
                measured_rtf = learn_rtf(measured_rtf, spent, duration)
                row = _transcript_row(index, downloaded, result, model_name,
                                      include_segments, include_subtitles)
                await deliver(row)

            if not lines:
                stop_reason = stop_reason or "empty-input"
        finally:
            await client.aclose()
            summary["stopReason"] = stop_reason
            summary["deadlineCapped"] = summary["deadlineCapped"] or stop_reason == "deadline"
            written = sum(summary["byStatus"].values())
            if written != summary["filesAsked"]:
                summary["counterMismatch"] = (f"asked {summary['filesAsked']} != rows "
                                              f"{written} by status")
            if summary["filesTranscribed"] != (summary["chargedEvents"]
                                               + summary["deliveredUncharged"]):
                summary["moneyMismatch"] = (f"transcribed {summary['filesTranscribed']} != "
                                            f"charged {summary['chargedEvents']} + uncharged "
                                            f"{summary['deliveredUncharged']}")
            summary["durationSecs"] = round(time.monotonic() - started, 1)
            run_env = Actor.get_env() or {}
            run_id = run_env.get("run_id") or os.environ.get("ACTOR_RUN_ID") or "run"
            summary["runId"] = run_id
            summary["summaryKey"] = f"RUN_SUMMARY-{run_id}"

            api_count = await item_count()
            summary["datasetItemCount"] = summary["rowsDelivered"]
            summary["datasetItemCountApi"] = api_count
            if api_count is None:
                summary["datasetItemCountNote"] = "the platform did not report a dataset count"
            elif api_count != summary["rowsDelivered"]:
                summary["datasetItemCountNote"] = (f"metadata lag: expected "
                                                   f"{summary['rowsDelivered']}, reports {api_count}")
            try:
                store = await Actor.open_key_value_store()
                await store.set_value(summary["summaryKey"], summary)
                # the default store of a run belongs to this run alone, so a plain key
                # cannot be overwritten by another run, and the output schema points here
                await store.set_value("RUN_SUMMARY", summary)
            except Exception as exc:  # noqa: BLE001 - the summary must not fail the run
                Actor.log.error("could not write the summary: %s", exc)

            headline = (f"{summary['filesTranscribed']} file(s) transcribed, "
                        f"{summary['audioMinutes']:.1f} min of audio, "
                        f"{summary['chargedEvents']} charged")
            if summary["stopReason"]:
                headline += f", stopped: {summary['stopReason']}"
            if not summary["billingActive"]:
                headline += ", billing inactive"
            try:
                await Actor.set_status_message(headline)
            except Exception:  # noqa: BLE001 - a local run has no status message
                pass
            Actor.log.info("%s | %s", headline, summary["byStatus"])

        if not lines:
            await Actor.fail(status_message=(
                "No audio files in the input. Put at least one http(s) address of an audio "
                "file into 'audioUrls'; nothing was charged."))
        elif not billing_known:
            await Actor.fail(status_message=(
                "The price of this run could not be read, so no file was transcribed and "
                "nothing was charged. Each input row says NOT_CHECKED_BILLING."))
        elif summary["filesTranscribed"] == 0 and summary["filesChargeUnverified"] == 0:
            reasons = ", ".join(sorted(summary["byStatus"])) or "no status"
            await Actor.fail(status_message=(
                f"No file could be transcribed: every line came back with its own status "
                f"({reasons}). Nothing was charged, and each line has a row that says why."))


if __name__ == "__main__":
    asyncio.run(main())
