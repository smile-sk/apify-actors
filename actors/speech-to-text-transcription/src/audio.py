"""Fetch, check and decode audio for the speech-to-text actor.

Two jobs, both about money.

1. Refuse anything that is not a public http(s) address BEFORE a socket is opened,
   and check every redirect hop instead of only the first URL. A private address
   would let a stranger read the container's own metadata, and a run that reads it
   still costs the buyer a file.
2. Turn bytes into 16 kHz mono float32 without faster-whisper's own decoder: its
   decode_audio() calls av.open(..., metadata_errors="ignore"), and av 19 dropped
   that keyword, so the stock decoder raises TypeError on every file. Measured on
   faster-whisper 1.2.1 with av 19.0.1.
"""
from __future__ import annotations

import io
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import av
import httpx
import numpy as np

from src import statuses as st

SAMPLE_RATE = 16000
MAX_REDIRECTS = 5
READ_CHUNK = 262144


class AudioError(Exception):
    """A file that cannot be delivered, with the status the buyer will see."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class Downloaded:
    url: str
    final_url: str
    file_name: str
    data: bytes
    content_type: str | None
    bytes_read: int


def normalize_url(raw) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if any(ch.isspace() for ch in text):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        # a broken bracket ("http://[abc/x.mp3") comes straight from a buyer's line:
        # one such line must become a status row, not the end of the run
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if not parts.hostname:
        return None
    return text


def file_name_of(url: str) -> str:
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return name[:120] if name else "audio"


def _is_public(addr) -> bool:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return bool(addr.is_global)


def _resolve(host: str):
    """One place that talks to DNS, so a test can hand in its own answers."""
    return socket.getaddrinfo(host, None)


NAME_PROBLEMS = ("the host name",)


def _host_status(reason: str) -> str:
    """A name that is not a name is a failed fetch; an address that is not public was
    refused on purpose. Neither one costs the buyer anything."""
    return st.FETCH_ERROR if reason.startswith(NAME_PROBLEMS) else st.PRIVATE_TARGET


def public_host(host: str) -> tuple[bool, str]:
    """Resolve the host and accept it only when every address is public."""
    try:
        infos = _resolve(host)
    except socket.gaierror as exc:
        return False, f"the host name does not resolve: {exc.strerror or exc}"
    except (UnicodeError, OSError) as exc:
        # getaddrinfo raises UnicodeError ("label empty or too long") on a label of 64
        # characters: that is not a host name at all, and it used to end the whole run
        # with zero rows and a valid second file left undelivered
        return False, f"the host name is not a valid name: {exc}"
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        return False, "the host name does not resolve to an address"
    for addr in addresses:
        if not _is_public(addr):
            return False, ("the address " + str(addr) + " is not public, and this actor "
                           "does not read inside private networks")
    return True, ""


def peer_address(response) -> str | None:
    """The address the socket really connected to, after DNS and any redirect."""
    stream = (getattr(response, "extensions", None) or {}).get("network_stream")
    if stream is None:
        return None
    try:
        info = stream.get_extra_info("server_addr")
    except Exception:  # noqa: BLE001 - a backend without the info must not fail the run
        return None
    if isinstance(info, tuple):
        return str(info[0])
    return str(info) if info else None


async def download(client: httpx.AsyncClient, url: str, max_bytes: int) -> Downloaded:
    """Follow redirects by hand so every hop is checked, and stop at max_bytes."""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        host = urlsplit(current).hostname or ""
        ok, why = public_host(host)
        if not ok:
            raise AudioError(_host_status(why), why)
        try:
            async with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise AudioError(st.FETCH_ERROR, (
                            f"the server answered HTTP {response.status_code} without a location"))
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise AudioError(st.FETCH_ERROR,
                                     f"the server answered HTTP {response.status_code}")
                peer = peer_address(response)
                if peer:
                    try:
                        addr = ipaddress.ip_address(peer)
                    except ValueError:
                        addr = None
                    if addr is not None and not _is_public(addr):
                        raise AudioError(st.PRIVATE_TARGET, (
                            "the connection landed on " + peer + ", which is not a public address"))
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise AudioError(st.TOO_LARGE, (
                        f"the file is {int(declared)} bytes and the limit is {max_bytes} bytes"))
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(READ_CHUNK):
                    total += len(chunk)
                    if total > max_bytes:
                        raise AudioError(st.TOO_LARGE, (
                            f"the file is larger than the limit of {max_bytes} bytes"))
                    chunks.append(chunk)
                return Downloaded(
                    url=url,
                    final_url=current,
                    file_name=file_name_of(current),
                    data=b"".join(chunks),
                    content_type=response.headers.get("content-type"),
                    bytes_read=total,
                )
        except AudioError:
            raise
        except httpx.HTTPError as exc:
            raise AudioError(st.FETCH_ERROR, f"the request failed: {type(exc).__name__}") from exc
    raise AudioError(st.FETCH_ERROR, f"the file took more than {MAX_REDIRECTS} redirects")


def probe_duration(data: bytes) -> float | None:
    """Duration from the container header, before any decoding."""
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            if not container.streams.audio:
                return None
            stream = container.streams.audio[0]
            if stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
            if container.duration:
                return float(container.duration / av.time_base)
            if stream.frames and stream.average_rate:
                return float(stream.frames / stream.average_rate)
    except Exception:  # noqa: BLE001 - a missing duration is not a broken file
        return None
    return None


def decode(data: bytes, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Bytes of any container PyAV reads into mono float32 samples."""
    chunks: list[np.ndarray] = []
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            if not container.streams.audio:
                raise AudioError(st.UNSUPPORTED, "the file carries no audio stream")
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1))
    except AudioError:
        raise
    except Exception as exc:  # noqa: BLE001 - a foreign file is a status, not a crash
        raise AudioError(st.UNSUPPORTED, (
            f"the file could not be decoded as audio: {type(exc).__name__}")) from exc
    if not chunks:
        raise AudioError(st.UNSUPPORTED, "the file decoded to zero samples")
    samples = np.concatenate(chunks).astype(np.float32)
    if samples.size == 0:
        raise AudioError(st.UNSUPPORTED, "the file decoded to zero samples")
    return samples
