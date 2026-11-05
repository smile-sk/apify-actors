"""The VIES client: one question, and the silence made explicit.

Kept apart from the actor so it can be tested without Apify: the difference
between "the number is not registered" and "nobody answered" is the whole
product, and it must be provable in a test, not in a promise.

The only field the billing code may trust is `answered`, and it is True only
when VIES sent a real boolean verdict. Anything else - a null, an error code,
an HTTP failure, a timeout - is silence, and silence is retried and then
reported as UNVERIFIED.
"""
from __future__ import annotations

import asyncio

import httpx

VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
MAX_RETRY_AFTER = 30.0


def _clean(value: str | None) -> str:
    """VIES sends '---' when a country hides trader data, as Germany does."""
    text = (value or "").strip()
    return "" if text in ("---", "-", "") else text


def _retry_after(response: httpx.Response, default: float) -> float:
    """Honour Retry-After when VIES sends it, and never wait longer than half a minute."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return default
    try:
        return min(max(float(raw), 1.0), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return default


async def ask_vies(client: httpx.AsyncClient, country_code: str, number: str,
                   attempts: int = 3, pause: float = 1.2) -> dict:
    """Ask VIES once, retrying on silence.

    Returns {"answered": bool, "valid": bool | None, "name": str, "address": str,
             "error": str, "tries": int}

    `answered` is the only field the billing code may trust: when it is False the
    number is UNVERIFIED, and an UNVERIFIED row is never charged.
    """
    last_error = ""
    for attempt in range(1, attempts + 1):
        wait = pause * attempt
        try:
            response = await client.post(VIES_URL, json={"countryCode": country_code, "vatNumber": number})
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    last_error = "VIES returned a body that is not JSON"
                else:
                    verdict = data.get("valid")
                    if isinstance(verdict, bool) and data.get("actionSucceed") is not False:
                        return {"answered": True, "valid": verdict,
                                "name": _clean(data.get("name")), "address": _clean(data.get("address")),
                                "error": data.get("error") or "", "tries": attempt}
                    last_error = str(data.get("error") or "VIES returned no verdict (valid is null)")
            elif response.status_code in (429, 500, 502, 503, 504):
                wait = _retry_after(response, wait)
                last_error = f"HTTP {response.status_code}"
            else:
                last_error = f"HTTP {response.status_code}"
        except Exception as exc:                                  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        if attempt < attempts:
            await asyncio.sleep(wait)
    return {"answered": False, "valid": None, "name": "", "address": "",
            "error": last_error, "tries": attempts}
