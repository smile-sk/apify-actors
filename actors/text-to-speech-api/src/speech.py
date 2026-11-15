"""Neural text to speech that lives inside the image: no keys, no network, no quota.

The engine is Piper, a small ONNX voice set baked into the Dockerfile. A buyer sends
text and gets audio back, and nothing leaves the container: there is no third party
service to rate-limit the run, no API key to leak and no per-character bill on our
side. The voices are the same files the build verifies, so a voice named in the
schema is a voice that exists in the image.

Everything that is awkward to test is kept out of this module's decisions: it either
returns audio bytes or raises SpeechError with a plain reason.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

VOICES_DIR = Path(os.environ.get("TTS_VOICES_DIR", "/opt/voices"))

# What a buyer types in `voice` -> the file stem baked into the image.
VOICES: dict[str, str] = {
    "en-us-lessac": "en_US-lessac-medium",
    "en-us-amy": "en_US-amy-medium",
    "ru-ru-irina": "ru_RU-irina-medium",
    "uk-ua-ukrainian": "uk_UA-ukrainian_tts-medium",
}
DEFAULT_VOICE = "en-us-lessac"
FORMATS = ("wav", "mp3")

# Piper medium voices run several times faster than real time on one core; the
# estimate is deliberately pessimistic because a wrong plan ends as a killed run
# without a summary, and a killed run costs a buyer the whole batch.
SECONDS_PER_CHAR = 1.0 / 60.0
FIRST_CLIP_EXTRA_S = 20.0


class SpeechError(Exception):
    """The engine could not turn this text into audio."""


def available_voices() -> list[str]:
    """The voices that really exist on disk, in the order the schema lists them."""
    found = []
    for name, stem in VOICES.items():
        if (VOICES_DIR / f"{stem}.onnx").is_file():
            found.append(name)
    return found


def mp3_available() -> bool:
    return shutil.which("lame") is not None


def prepare_text(raw: object) -> str:
    """Whitespace is collapsed so two copies of the same sentence are one clip."""
    return " ".join(str("" if raw is None else raw).split())


def estimate_seconds(chars: int, speed: float) -> float:
    """A pessimistic upper bound for synthesising this many characters."""
    rate = speed if speed and speed > 0 else 1.0
    return 2.0 + (max(0, chars) * SECONDS_PER_CHAR) / rate


def _voice_files(voice: str) -> tuple[Path, Path]:
    stem = VOICES.get(voice)
    if stem is None:
        raise SpeechError(f"the voice {voice!r} is not one of {', '.join(sorted(VOICES))}")
    model = VOICES_DIR / f"{stem}.onnx"
    config = VOICES_DIR / f"{stem}.onnx.json"
    if not model.is_file():
        raise SpeechError(f"the voice file {model.name} is not in this image")
    return model, config


_LOADED: dict[str, object] = {}


def load_voice(voice: str):
    """Load once per run: model load is the slowest step of a short batch."""
    if voice in _LOADED:
        return _LOADED[voice]
    model, config = _voice_files(voice)
    try:
        from piper import PiperVoice  # imported here so a missing engine is a reason, not a crash
    except Exception as exc:  # noqa: BLE001
        raise SpeechError(f"the speech engine is not installed ({type(exc).__name__})") from exc
    try:
        loaded = PiperVoice.load(str(model), str(config))
    except Exception as exc:  # noqa: BLE001
        raise SpeechError(f"the voice could not be loaded: {type(exc).__name__}: {exc}") from exc
    _LOADED[voice] = loaded
    return loaded


def wav_bytes(raw: bytes, sample_rate: int, sample_width: int, channels: int) -> bytes:
    payload = bytearray()
    with tempfile.SpooledTemporaryFile() as handle:
        with wave.open(handle, "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(sample_width)
            out.setframerate(sample_rate)
            out.writeframes(raw)
        handle.seek(0)
        payload += handle.read()
    return bytes(payload)


def to_mp3(wav: bytes) -> bytes:
    """WAV to MP3 with the lame binary that the Dockerfile installs and checks."""
    exe = shutil.which("lame")
    if exe is None:
        raise SpeechError("no mp3 encoder is installed in this image")
    with tempfile.TemporaryDirectory() as folder:
        source = os.path.join(folder, "in.wav")
        target = os.path.join(folder, "out.mp3")
        with open(source, "wb") as handle:
            handle.write(wav)
        done = subprocess.run(
            [exe, "--silent", "-h", source, target],
            capture_output=True, timeout=180, check=False,
        )
        if done.returncode != 0 or not os.path.isfile(target):
            detail = (done.stderr or b"").decode("utf-8", "replace").strip()[:120]
            raise SpeechError(f"the mp3 encoder failed: {detail or done.returncode}")
        with open(target, "rb") as handle:
            return handle.read()


def synthesize(text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0, fmt: str = "wav") -> dict:
    """Audio for one piece of text, with everything a row needs to describe it."""
    fmt = (fmt or "wav").lower()
    if fmt not in FORMATS:
        raise SpeechError(f"the format {fmt!r} is not one of {', '.join(FORMATS)}")
    if not text.strip():
        raise SpeechError("the text is empty")
    loaded = load_voice(voice)
    try:
        from piper import SynthesisConfig
    except Exception as exc:  # noqa: BLE001
        raise SpeechError(f"the speech engine is not installed ({type(exc).__name__})") from exc
    rate = speed if speed and speed > 0 else 1.0
    config = SynthesisConfig(length_scale=1.0 / rate)
    try:
        chunks = list(loaded.synthesize(text, syn_config=config))
    except Exception as exc:  # noqa: BLE001
        raise SpeechError(f"the engine failed: {type(exc).__name__}: {exc}") from exc
    if not chunks:
        raise SpeechError("the engine returned no audio for this text")
    sample_rate = int(getattr(chunks[0], "sample_rate", 22050) or 22050)
    sample_width = int(getattr(chunks[0], "sample_width", 2) or 2)
    channels = int(getattr(chunks[0], "sample_channels", 1) or 1)
    raw = b"".join(chunk.audio_int16_bytes for chunk in chunks)
    if not raw:
        raise SpeechError("the engine returned empty audio")
    seconds = len(raw) / float(sample_rate * sample_width * channels)
    wav = wav_bytes(raw, sample_rate, sample_width, channels)
    if fmt == "mp3":
        data, content_type = to_mp3(wav), "audio/mpeg"
    else:
        data, content_type = wav, "audio/wav"
    return {
        "data": data,
        "format": fmt,
        "contentType": content_type,
        "sampleRate": sample_rate,
        "channels": channels,
        "seconds": round(seconds, 2),
    }
