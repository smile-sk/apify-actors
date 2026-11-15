"""Whisper itself: load a model once, transcribe samples, format subtitles.

faster-whisper runs CTranslate2 on the CPU, so the work is blocking and main() sends
it to a thread. The model is baked into the image (see Dockerfile), which is why the
run does not download 250 MB of weights while the buyer is waiting.
"""
from __future__ import annotations

import os

MODELS = ("tiny", "base", "small")

# Planning numbers: seconds of work per SECOND OF AUDIO ON ONE THREAD, measured on an
# 11 second 16 kHz file with faster-whisper 1.2.1, int8, ctranslate2, on a laptop class
# CPU. A 4096 MB container gets exactly one thread, so a two-thread number understates
# the plan by 2.3x, the gate lets a long file start, and the platform kills the run in
# the middle of it instead of the actor stopping by its own clock.
PRIOR_RTF_SINGLE = {"tiny": 0.5, "base": 1.0, "small": 2.4}
MODEL_LOAD_SEC = 8.0
PLAN_MARGIN = 1.2


def prior_rtf(name: str, threads: int) -> float:
    """Planning seconds per second of audio for THIS container.

    More threads help, but not linearly: measured 2.31 on one thread against 1.37 on two,
    so the scaling is threads ** 0.75. The guess is replaced by what the run itself
    measures after the first file.
    """
    base = PRIOR_RTF_SINGLE.get(name, PRIOR_RTF_SINGLE["small"])
    return base / max(1, int(threads)) ** 0.75


def plan_seconds(duration: float, rtf: float, model_loaded: bool) -> float:
    """How long this file will probably take, rounded UP: work, margin, model load.

    The load is charged once per run, before the first file. A wrong plan means either a
    paid file that never arrives or a run killed by the platform clock mid file.
    """
    work = max(0.0, float(duration)) * float(rtf) * PLAN_MARGIN
    return work + (0.0 if model_loaded else MODEL_LOAD_SEC)


def worker_threads() -> int:
    """CTranslate2 threads from the container's own budget: 1 core per 4096 MB."""
    try:
        memory = int(os.environ.get("ACTOR_MEMORY_MBYTES") or 4096)
    except (TypeError, ValueError):
        memory = 4096
    cores = max(1, memory // 4096)
    return max(1, min(4, cores))


def load_model(name: str, threads: int):
    from faster_whisper import WhisperModel
    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)


def transcribe(model, samples, language: str | None, vad_filter: bool) -> dict:
    segments, info = model.transcribe(
        samples,
        language=language or None,
        beam_size=1,
        vad_filter=vad_filter,
        word_timestamps=False,
        condition_on_previous_text=True,
    )
    cues: list[dict] = []
    parts: list[str] = []
    for segment in segments:
        text = (segment.text or "").strip()
        parts.append(text)
        cues.append({"start": round(float(segment.start), 2),
                     "end": round(float(segment.end), 2),
                     "text": text})
    merged = " ".join(p for p in parts if p).strip()
    return {
        "text": merged,
        "segments": cues,
        "language": getattr(info, "language", None),
        "languageProbability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
        "durationAfterVad": float(getattr(info, "duration_after_vad", 0.0) or 0.0),
    }


def _clock(seconds: float, sep: str) -> str:
    total_ms = int(round(max(0.0, seconds) * 1000))
    hours, rest = divmod(total_ms, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def to_srt(cues: list[dict]) -> str:
    blocks = []
    for number, cue in enumerate(cues, start=1):
        blocks.append(f"{number}\n{_clock(cue['start'], ',')} --> {_clock(cue['end'], ',')}\n"
                      f"{cue['text']}\n")
    return "\n".join(blocks)


def to_vtt(cues: list[dict]) -> str:
    blocks = ["WEBVTT\n"]
    for cue in cues:
        blocks.append(f"{_clock(cue['start'], '.')} --> {_clock(cue['end'], '.')}\n{cue['text']}\n")
    return "\n".join(blocks)
