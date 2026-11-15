"""Text to speech: one row per delivered clip, and every refusal is free.

The money rule of this park is kept literally: a row is written and charged by one
SDK call, and the buyer pays only for audio that really exists in the run store and
can be downloaded. A blank line, a repeat, a text longer than the limit, a line past
maxClips and every line the run never reached come back free with the reason on the
row.

This actor has no network at all. Text comes from the buyer, audio is made by the
engine baked into the image, so there is no source to block, no key to leak and no
third party to rate-limit a batch.
"""
from __future__ import annotations

import asyncio
import os
import re
import zipfile
from datetime import datetime, timezone

from apify import Actor

from . import speech

EVENT_NAME = "clip-synthesized"
SUMMARY_STORE = "text-to-speech-api-summary"
MAX_CLIPS_DEFAULT = 10
MAX_CHARS_DEFAULT = 5000
MAX_REFUSALS_IN_A_ROW = 3
ZIP_KEY = "clips.zip"

ST_OK = "OK"
ST_BLANK = "NOT_CHECKED_BLANK"
ST_DUPLICATE = "NOT_CHECKED_DUPLICATE"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_TOO_LONG = "NOT_CHECKED_TOO_LONG"
ST_TIME = "NOT_CHECKED_TIME"
ST_CAP = "NOT_CHECKED_CAP"
ST_BILLING = "NOT_CHECKED_BILLING"
ST_ERROR = "NOT_CHECKED_ERROR"
ST_STORE = "NOT_CHECKED_STORE_ERROR"
ST_SYNTH = "NOT_CHECKED_SYNTH_ERROR"

# A store key may hold only these characters; a slash is refused by the platform
# with 400 invalid-record-key and the buyer silently loses the file.
_KEY_OK = re.compile(r"[^A-Za-z0-9!\-_.'()]+")


def run_id() -> str:
    return (
        os.environ.get("ACTOR_RUN_ID")
        or (Actor.get_env() or {}).get("run_id")
        or "run"
    )


def deadline_seconds() -> float | None:
    """Seconds left before the platform kills the run, or None when unknown."""
    raw = os.environ.get("ACTOR_TIMEOUT_AT")
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - datetime.now(timezone.utc)).total_seconds()


def store_key(name: str) -> str:
    """A key the platform will accept: no slashes, at most 256 characters."""
    cleaned = _KEY_OK.sub("_", str(name)).strip("_") or "audio"
    return cleaned[:256]


def price_state(charging) -> tuple[bool | None, str]:
    """Can this run charge? True / False / None (unknown) and a plain reason."""
    try:
        info = charging.get_pricing_info()
    except Exception as exc:  # noqa: BLE001 - any failure means "price unknown"
        return None, f"pricing read failed: {type(exc).__name__}"
    if info is None:
        return None, "pricing info is not readable"
    is_ppe = getattr(info, "is_pay_per_event", None)
    if is_ppe is None and isinstance(info, dict):
        is_ppe = info.get("isPayPerEvent", info.get("is_pay_per_event"))
    if is_ppe is False:
        return False, "the actor is not sold per event"
    prices = getattr(info, "per_event_prices", None)
    if prices is None and isinstance(info, dict):
        prices = info.get("perEventPrices") or info.get("per_event_prices") or {}
    names: list[str] = []
    if isinstance(prices, dict):
        names = [str(key) for key in prices]
    elif isinstance(prices, list):
        for item in prices:
            name = getattr(item, "event_name", None)
            if name is None and isinstance(item, dict):
                name = item.get("eventName") or item.get("event_name")
            if name:
                names.append(str(name))
    if is_ppe is None:
        if names:
            return (EVENT_NAME in names), (
                f"{EVENT_NAME} is priced" if EVENT_NAME in names
                else f"{EVENT_NAME} is not in the price list")
        return None, "the pricing answer is not understood"
    if EVENT_NAME not in names:
        return False, f"{EVENT_NAME} is not priced, rows go out free"
    return True, f"{EVENT_NAME} is priced"


async def dataset_count(dataset) -> int | None:
    try:
        meta = await dataset.get_metadata()
    except Exception:  # noqa: BLE001 - a missing counter is not a lost row
        return None
    value = getattr(meta, "item_count", None)
    if value is None and isinstance(meta, dict):
        value = meta.get("itemCount", meta.get("item_count"))
    return value if isinstance(value, int) else None


def status_row(line: str, index: int, status: str, reason: str, **extra) -> dict:
    row = {
        "text": None,
        "input": line,
        "inputIndex": index,
        "status": status,
        "reason": reason,
        "billable": False,
    }
    row.update(extra)
    return row


def stopped_status(stop_reason: str | None) -> tuple[str, str]:
    if stop_reason == "spending-cap":
        return ST_CAP, "the spending cap was reached: not delivered and not charged"
    if stop_reason == "billing-price-unknown":
        return ST_BILLING, "the price could not be read: not delivered and not charged"
    if stop_reason == "deadline":
        return ST_TIME, "the platform deadline was close: not delivered and not charged"
    return ST_ERROR, "the run stopped before this text: nothing was charged"


def build_tasks(texts: list, max_clips: int, max_chars: int, fingerprint: str) -> tuple[list[dict], dict]:
    """Work in input order, with every free refusal decided before synthesis starts."""
    tasks: list[dict] = []
    seen: set[str] = set()
    counts = {"asked": len(texts), "unique": 0, "accepted": 0, "duplicates": 0,
              "blank": 0, "limit": 0, "tooLong": 0}
    for index, raw in enumerate(texts):
        shown = "" if raw is None else str(raw)
        text = speech.prepare_text(raw)
        if not text:
            counts["blank"] += 1
            tasks.append({"kind": "row", "row": status_row(
                shown, index, ST_BLANK,
                "this line carries no text: nothing was made and nothing was charged")})
            continue
        key = f"{fingerprint}|{text}"
        if key in seen:
            counts["duplicates"] += 1
            tasks.append({"kind": "row", "row": status_row(
                shown, index, ST_DUPLICATE,
                "the same text with the same voice came earlier, so it is made once "
                "and this line is free")})
            continue
        seen.add(key)
        counts["unique"] += 1
        if max_chars and len(text) > max_chars:
            counts["tooLong"] += 1
            tasks.append({"kind": "row", "row": status_row(
                shown, index, ST_TOO_LONG,
                f"the text is {len(text)} characters and maxCharactersPerLine is "
                f"{max_chars}: nothing was made and nothing was charged "
                "(split it into shorter lines)")})
            continue
        counts["accepted"] += 1
        if max_clips and counts["accepted"] > max_clips:
            counts["limit"] += 1
            tasks.append({"kind": "row", "row": status_row(
                shown, index, ST_LIMIT,
                f"the list is cut at maxClips={max_clips}: not made and not charged")})
            continue
        tasks.append({"kind": "work", "line": shown, "text": text, "index": index,
                      "chars": len(text)})
    return tasks, counts


async def main() -> None:
    await Actor.init()
    dataset = await Actor.open_dataset()
    store = await Actor.open_key_value_store()

    actor_input = await Actor.get_input() or {}
    raw_texts = actor_input.get("texts")
    if raw_texts is None:
        single = actor_input.get("text")
        raw_texts = [single] if single else []
    if isinstance(raw_texts, str):
        raw_texts = [raw_texts]
    texts = list(raw_texts) if isinstance(raw_texts, (list, tuple)) else []

    voice = str(actor_input.get("voice") or speech.DEFAULT_VOICE).strip().lower()
    fmt = str(actor_input.get("format") or "wav").strip().lower()
    try:
        speed = float(actor_input.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = min(2.0, max(0.5, speed))
    try:
        max_clips = int(actor_input.get("maxClips") or MAX_CLIPS_DEFAULT)
    except (TypeError, ValueError):
        max_clips = MAX_CLIPS_DEFAULT
    try:
        max_chars = int(actor_input.get("maxCharactersPerLine") or MAX_CHARS_DEFAULT)
    except (TypeError, ValueError):
        max_chars = MAX_CHARS_DEFAULT

    have = speech.available_voices()
    if have and voice not in have:
        await Actor.fail(status_message=(
            f"The voice {voice!r} is not in this image (it has: {', '.join(have)}). "
            "Nothing was made and nothing was charged."))
        return
    if not have:
        await Actor.fail(status_message=(
            "This image carries no speech voices, so no clip could be made. "
            "Nothing was charged."))
        return
    if fmt == "mp3" and not speech.mp3_available():
        await Actor.fail(status_message=(
            "The mp3 encoder is not in this image, and format='mp3' was asked. "
            "Use format='wav' or a build with an encoder. Nothing was charged."))
        return

    charging = Actor.get_charging_manager()
    can_charge, charge_reason = price_state(charging)
    billable_claim = None if can_charge is None else bool(can_charge)

    fingerprint = f"{voice}|{fmt}|{speed:.2f}"
    tasks, counts = build_tasks(texts, max_clips, max_chars, fingerprint)

    by_status: dict[str, int] = {}
    rows_written = 0
    clips_delivered = 0
    charged_total = 0
    delivered_uncharged = 0
    delivered_unconfirmed = 0
    charge_errors = 0
    write_errors = 0
    store_errors = 0
    synth_errors = 0
    texts_processed = 0
    texts_not_checked = 0
    chars_made = 0
    audio_seconds = 0.0
    sample_rate: int | None = None
    keys: list[str] = []
    bundle: list[tuple[str, bytes]] = []
    zip_key: str | None = None
    zip_bytes = 0
    stop_reason: str | None = None
    deadline_capped = False
    unexpected_error: str | None = None
    dataset_api_count: int | None = None
    dataset_note: str | None = None
    billing_active = can_charge is True
    stopping = False
    refusals_in_a_row = 0
    failed_at = 0
    margin_s = speech.FIRST_CLIP_EXTRA_S

    def note(status: str) -> None:
        by_status[status] = by_status.get(status, 0) + 1

    async def write_free(row: dict) -> bool:
        nonlocal rows_written, write_errors, dataset_note
        nonlocal stopping, stop_reason, refusals_in_a_row
        row["billable"] = False
        try:
            await dataset.push_data(row)
        except Exception:  # noqa: BLE001
            write_errors += 1
            refusals_in_a_row += 1
            dataset_note = "a free status row was refused by the dataset"
            if refusals_in_a_row >= MAX_REFUSALS_IN_A_ROW:
                stopping = True
                stop_reason = stop_reason or "dataset-write-error"
            return False
        rows_written += 1
        refusals_in_a_row = 0
        return True

    async def stop_row(line: str, index: int) -> None:
        status, reason = stopped_status(stop_reason)
        note(status)
        if stop_reason == "dataset-write-error":
            return
        await write_free(status_row(line, index, status, reason))

    try:
        for position, task in enumerate(tasks):
            failed_at = position
            if task["kind"] == "row":
                row = task["row"]
                written = await write_free(row)
                note(row["status"] if written else ST_ERROR)
                continue

            if stopping:
                await stop_row(task["line"], task["index"])
                texts_not_checked += 1
                continue

            left = deadline_seconds()
            need = margin_s + speech.estimate_seconds(task["chars"], speed)
            if left is not None and left < need:
                deadline_capped = True
                stopping = True
                stop_reason = stop_reason or "deadline"
                note(ST_TIME)
                texts_not_checked += 1
                await write_free(status_row(
                    task["line"], task["index"], ST_TIME,
                    "the platform deadline was close before this text: nothing was "
                    "delivered and nothing was charged"))
                continue

            if can_charge is None:
                stopping = True
                stop_reason = stop_reason or "billing-price-unknown"
                note(ST_BILLING)
                texts_not_checked += 1
                await write_free(status_row(
                    task["line"], task["index"], ST_BILLING,
                    f"the price could not be read ({charge_reason}): not delivered "
                    "and not charged"))
                continue

            if can_charge and charging.is_event_charge_limit_reached(EVENT_NAME):
                stopping = True
                stop_reason = stop_reason or "spending-cap"
                note(ST_CAP)
                texts_not_checked += 1
                await write_free(status_row(
                    task["line"], task["index"], ST_CAP,
                    "the spending cap was reached before this text: not delivered "
                    "and not charged"))
                continue

            budget = max(5.0, min(margin_s * 3, (1e9 if left is None else left) - 1.0))
            try:
                made = await asyncio.wait_for(
                    asyncio.to_thread(
                        speech.synthesize, task["text"], voice, speed, fmt),
                    timeout=budget,
                )
            except asyncio.TimeoutError:
                deadline_capped = True
                stopping = True
                stop_reason = stop_reason or "deadline"
                note(ST_TIME)
                texts_not_checked += 1
                await write_free(status_row(
                    task["line"], task["index"], ST_TIME,
                    "this text could not be finished before the deadline: nothing "
                    "was delivered and nothing was charged"))
                continue
            except speech.SpeechError as exc:
                synth_errors += 1
                note(ST_SYNTH)
                texts_not_checked += 1
                await write_free(status_row(
                    task["line"], task["index"], ST_SYNTH,
                    f"{exc}: nothing was delivered and nothing was charged"))
                continue
            except Exception as exc:  # noqa: BLE001 - one text never kills the run
                synth_errors += 1
                note(ST_ERROR)
                texts_not_checked += 1
                await write_free(status_row(
                    task["line"], task["index"], ST_ERROR,
                    f"{type(exc).__name__}: this text could not be made, nothing was "
                    "delivered and nothing was charged"))
                continue

            slug = re.sub(r"[^a-z0-9]+", "", task["text"][:24].lower()) or "clip"
            key = store_key(f"audio_{len(keys):03d}-{slug}.{made['format']}")
            try:
                await store.set_value(key, made["data"], content_type=made["contentType"])
            except Exception as exc:  # noqa: BLE001
                store_errors += 1
                note(ST_STORE)
                texts_not_checked += 1
                await write_free(status_row(
                    task["line"], task["index"], ST_STORE,
                    f"{type(exc).__name__}: the audio could not be saved, so this clip "
                    "is not delivered and not charged"))
                continue

            row = {
                "text": task["text"],
                "input": task["line"],
                "inputIndex": task["index"],
                "status": ST_OK,
                "reason": None,
                "billable": False,
                "voice": voice,
                "format": made["format"],
                "speed": round(speed, 2),
                "characters": task["chars"],
                "seconds": made["seconds"],
                "sampleRate": made["sampleRate"],
                "channels": made["channels"],
                "audioKey": key,
            }
            sample_rate = made["sampleRate"]

            if can_charge:
                try:
                    result = await Actor.push_data(
                        {**row, "billable": billable_claim},
                        charged_event_name=EVENT_NAME,
                    )
                except Exception as exc:  # noqa: BLE001
                    # The SDK writes the row and then charges it. A counter that did
                    # not grow means the row never landed and the buyer lost nothing;
                    # a counter that grew means the clip is there and unpaid. Calling
                    # both "delivered" is the lie this park already paid for.
                    charge_errors += 1
                    refusals_in_a_row = 0
                    api_now = await dataset_count(dataset)
                    stopping = True
                    stop_reason = "charge-error"
                    note(ST_OK)
                    if api_now is not None and api_now > rows_written:
                        delivered_uncharged += 1
                        rows_written += 1
                        clips_delivered += 1
                        texts_processed += 1
                        chars_made += task["chars"]
                        audio_seconds += made["seconds"]
                        keys.append(key)
                        bundle.append((key, made["data"]))
                        dataset_note = (
                            f"a paid row could not be confirmed after "
                            f"{type(exc).__name__}: the clip is in the dataset and it "
                            "was not charged")
                    else:
                        delivered_unconfirmed += 1
                        texts_not_checked += 1
                        dataset_note = (
                            f"a paid row could not be confirmed after "
                            f"{type(exc).__name__} and the dataset counter did not "
                            "grow: the clip may be missing")
                    continue
                charged = int(getattr(result, "charged_count", 0) or 0)
                limit_reached = bool(getattr(result, "event_charge_limit_reached", False))
                if charged <= 0:
                    status = ST_CAP if limit_reached else ST_ERROR
                    note(status)
                    rows_written += 1
                    clips_delivered += 1
                    texts_processed += 1
                    keys.append(key)
                    bundle.append((key, made["data"]))
                    stopping = True
                    stop_reason = "spending-cap" if limit_reached else "charge-error"
                    continue
                rows_written += 1
                clips_delivered += 1
                texts_processed += 1
                charged_total += charged
                chars_made += task["chars"]
                audio_seconds += made["seconds"]
                keys.append(key)
                bundle.append((key, made["data"]))
                refusals_in_a_row = 0
                note(ST_OK)
                if limit_reached:
                    stopping = True
                    stop_reason = "spending-cap"
                continue

            row["billable"] = False
            written = await write_free(row)
            clips_delivered += 1
            texts_processed += 1
            chars_made += task["chars"]
            audio_seconds += made["seconds"]
            keys.append(key)
            bundle.append((key, made["data"]))
            note(ST_OK if written else ST_ERROR)
    except Exception as exc:  # noqa: BLE001 - a crash must not lose the summary
        unexpected_error = f"{type(exc).__name__}: {exc}"[:300]
        stop_reason = stop_reason or "unexpected-error"
        for rest in tasks[failed_at:]:
            if rest.get("kind") == "row":
                row = rest.get("row") or {}
                line, index = row.get("input") or "", row.get("inputIndex") or 0
            else:
                line, index = rest.get("line") or "", rest.get("index") or 0
            note(ST_ERROR)
            await write_free(status_row(
                line, index, ST_ERROR,
                "the run stopped on an unexpected error before this text: nothing "
                "was charged"))
    finally:
        if bundle:
            try:
                buffer = __import__("io").BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, data in bundle:
                        archive.writestr(name, data)
                zip_key = store_key(ZIP_KEY)
                await store.set_value(zip_key, buffer.getvalue(),
                                      content_type="application/zip")
                zip_bytes = len(buffer.getvalue())
            except Exception:  # noqa: BLE001 - a missing zip is not a lost clip
                zip_key = None
                zip_bytes = 0
        dataset_api_count = await dataset_count(dataset)

    asked = counts["asked"]
    lines_not_texts = (counts["blank"] + counts["duplicates"]
                       + counts["limit"] + counts["tooLong"])
    texts_attempted = counts["accepted"] - counts["limit"]
    if texts_processed + texts_not_checked != texts_attempted:
        by_status["textMismatch"] = (
            texts_attempted - (texts_processed + texts_not_checked))
    if lines_not_texts + texts_attempted != asked:
        by_status["lineCountMismatch"] = asked - lines_not_texts - texts_attempted

    summary = {
        "requested": asked,
        "uniqueTexts": counts["unique"],
        "duplicates": counts["duplicates"],
        "blankLines": counts["blank"],
        "longerThanLimit": counts["tooLong"],
        "cutByMaxClips": counts["limit"],
        "textsAttempted": texts_attempted,
        "textsProcessed": texts_processed,
        "notCheckedTexts": texts_not_checked,
        "linesThatAreNotTexts": lines_not_texts,
        "clipsDelivered": clips_delivered,
        "charactersSynthesized": chars_made,
        "audioSeconds": round(audio_seconds, 2),
        "rowsWritten": rows_written,
        "chargedEvents": charged_total if billing_active else 0,
        "billingActive": billing_active,
        "billingReason": charge_reason,
        "deliveredUncharged": delivered_uncharged,
        "deliveredUnconfirmed": delivered_unconfirmed,
        "chargeErrors": charge_errors,
        "writeErrors": write_errors,
        "storeErrors": store_errors,
        "synthesisErrors": synth_errors,
        "stopReason": stop_reason,
        "unexpectedError": unexpected_error,
        "byStatus": by_status,
        "voice": voice,
        "voicesInImage": have,
        "format": fmt,
        "speed": round(speed, 2),
        "sampleRate": sample_rate,
        "maxClips": max_clips,
        "maxCharactersPerLine": max_chars,
        "deadlineCapped": deadline_capped,
        "clipMarginSecs": round(margin_s, 1),
        "audioKeys": keys,
        "zipKey": zip_key,
        "zipBytes": zip_bytes,
        "datasetItemCount": rows_written,
        "datasetItemCountApi": dataset_api_count,
        "datasetItemCountNote": dataset_note,
        "priceEvent": EVENT_NAME,
        "runId": run_id(),
        "finishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if dataset_api_count is not None and dataset_api_count < rows_written:
        lag = f"metadata lag: expected {rows_written}, reports {dataset_api_count}"
        summary["datasetItemCountNote"] = f"{dataset_note}; {lag}" if dataset_note else lag

    summary_store = await Actor.open_key_value_store(name=SUMMARY_STORE)
    await summary_store.set_value(f"RUN_SUMMARY-{run_id()}", summary)
    await store.set_value("RUN_SUMMARY", summary)

    billed = "made and charged" if billing_active else "made, nothing charged"
    text = (
        f"{texts_attempted} text(s) from {asked} line(s), "
        f"{clips_delivered} clip(s) {billed}, {texts_not_checked} not checked"
    )
    if not billing_active:
        text += f" (billing is off: {charge_reason})"
    if stop_reason:
        text += f", stopped: {stop_reason}"
    await Actor.set_status_message(text)

    if asked == 0:
        await Actor.fail(status_message=(
            "No text was sent: nothing to say, nothing charged. Put one piece of "
            "text per line into the texts field."))
    if unexpected_error:
        await Actor.fail(status_message=(
            f"The run stopped on an unexpected error ({unexpected_error}). "
            f"{clips_delivered} clip(s) delivered, {texts_not_checked} text(s) not "
            f"checked, {charged_total if billing_active else 0} charged."))
    if asked > 0 and clips_delivered == 0:
        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
        await Actor.fail(status_message=(
            f"Nothing could be made: {breakdown or 'no rows written'}. "
            "Nothing was charged."))


if __name__ == "__main__":
    asyncio.run(main())
