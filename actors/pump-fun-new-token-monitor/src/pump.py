"""pump.fun on the chain: decode a creation, read the creator's buy.

Everything here was measured against Solana mainnet, not taken from documentation:

* a creation is found by the 8-byte event DISCRIMINATOR, never by the words
  "Instruction: Create" in the logs: that text also belongs to foreign programs
  (ATA, Jupiter) and produces false rows;
* the creation event itself carries name, symbol, uri, mint, the bonding curve,
  the creator, the timestamp and the reserves, so a base row costs no extra RPC call;
* the event body carries MORE bytes after the numbers than the classic layout needs
  (measured: 252 bytes left after the three strings, while four addresses plus five
  numbers take 168), so the numbers are located by an invariant that cannot drift:
  the timestamp of a creation sits in our era. The offset that satisfies it wins;
* measured on 8 live creations: 5 report exactly 30 virtual SOL, and 3 report a
  virtual-SOL figure that no fresh curve can hold (814 and 8901 SOL while the real
  token reserves are still untouched). Those three keep their mint, name and time,
  and report the reserves as null: a number we cannot stand behind is worse than none;
* token amounts carry 6 decimals and SOL carries 9: a measured launch came as
  1073000000000000 (1 073 000 000 tokens) and 30000000000 (30 SOL);
* the event has NO real SOL field: real SOL is virtual SOL minus the 30 SOL the curve
  starts with, and that is what curve progress counts.
"""
from __future__ import annotations

import base64
import struct
from datetime import datetime, timezone

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com"

# sha256("event:CreateEvent")[:8], the same discriminator our sniper used on mainnet
CREATE_EVENT_DISC = "1b72a94ddeeb6376"

TOKEN_DECIMALS = 6
SOL_DECIMALS = 9
# every curve starts at 30 virtual SOL and fills at 85 real SOL before migration
INITIAL_VIRTUAL_SOL = 30.0
CURVE_FILL_SOL = 85.0
INITIAL_SUPPLY_TOKENS = 1_000_000_000.0

# how many 32-byte addresses may sit between the strings and the five numbers
ADDRESS_COUNTS = ((4, "current"), (3, "older"), (5, "wide"))

MAX_STRING = 400
# a creation is never older than 2020 and never later than 2039
MIN_TIMESTAMP = 1_577_836_800
MAX_TIMESTAMP = 2_208_988_800
# a curve that has just been born holds 30 virtual SOL; allow a fee-change margin
MIN_BIRTH_SOL = 15.0
MAX_BIRTH_SOL = 45.0


def b58encode(raw: bytes) -> str:
    """base58 of a 32-byte address, leading zero bytes kept as leading '1'."""
    number = int.from_bytes(raw, "big")
    out = []
    while number:
        number, rest = divmod(number, 58)
        out.append(B58[rest])
    pad = 0
    for byte in raw:
        if byte == 0:
            pad += 1
        else:
            break
    return "1" * pad + "".join(reversed(out))


def _read_string(body: bytes, pos: int):
    if pos + 4 > len(body):
        return None, pos
    (length,) = struct.unpack_from("<I", body, pos)
    pos += 4
    if length > MAX_STRING or pos + length > len(body):
        return None, pos
    return body[pos:pos + length].decode("utf-8", "replace"), pos + length


def _iso(timestamp: int) -> str | None:
    if timestamp < MIN_TIMESTAMP or timestamp > MAX_TIMESTAMP:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _numbers_at(body: bytes, pos: int, count: int):
    base = pos + 32 * count
    if base + 40 > len(body):
        return None
    return struct.unpack_from("<qQQQQ", body, base)


def _aligned_numbers(body: bytes, pos: int):
    """Find the five numbers by the one field that cannot be faked: the timestamp.

    A wrong offset lands on bytes of an address, which never read as a date in
    2020..2039, so the first candidate whose timestamp is real is the alignment.
    """
    for count, label in ADDRESS_COUNTS:
        numbers = _numbers_at(body, pos, count)
        if numbers is None:
            continue
        if MIN_TIMESTAMP <= numbers[0] <= MAX_TIMESTAMP:
            return count, label, numbers
    return None


def _reserves_are_birth(values) -> bool:
    """Do these numbers describe a curve that has just been born?

    The supply is about a billion tokens, the virtual tokens about 1.073 billion and
    the virtual SOL about 30. Anything else is not a number we can publish.
    """
    try:
        _timestamp, v_tokens, v_sol, r_tokens, supply = values
    except (TypeError, ValueError):
        return False
    if not (MIN_BIRTH_SOL * 10 ** SOL_DECIMALS <= v_sol <= MAX_BIRTH_SOL * 10 ** SOL_DECIMALS):
        return False
    if not (1e14 <= supply <= 1e16):
        return False
    if not (1e14 <= v_tokens <= 1e16):
        return False
    return r_tokens <= v_tokens


def _parse_body(body: bytes):
    pos = 0
    out: dict = {}
    for key in ("name", "symbol", "uri"):
        text, pos = _read_string(body, pos)
        if text is None:
            return None
        out[key] = text

    left = len(body) - pos
    aligned = _aligned_numbers(body, pos)
    if aligned is None:
        # Only the tail is unreadable: keep the addresses so the mint is still delivered.
        count = 4 if left >= 168 else 3
        if left < 32 * count:
            return None
        aligned = (count, "unknown", None)

    count, label, numbers = aligned
    keys = ["mint", "bondingCurve", "user"]
    if count >= 4:
        keys.append("creator")
    for key in keys:
        if pos + 32 > len(body):
            return None
        out[key] = b58encode(body[pos:pos + 32])
        pos += 32
    if "creator" not in out:
        # no separate creator in this layout: the user who signed is the creator
        out["creator"] = out["user"]

    out["layout"] = label
    if numbers is None or not _reserves_are_birth(numbers):
        timestamp = numbers[0] if numbers else 0
        out.update({
            "timestamp": int(timestamp) if numbers else 0,
            "createdAt": _iso(int(timestamp)) if numbers else None,
            "totalSupply": None,
            "virtualTokenReserves": None,
            "virtualSolReserves": None,
            "realTokenReserves": None,
            "realSolReserves": None,
            "reservesKnown": False,
        })
        return out

    timestamp, v_tokens, v_sol, r_tokens, supply = numbers
    out["timestamp"] = int(timestamp)
    out["createdAt"] = _iso(int(timestamp))
    out["totalSupply"] = supply / 10 ** TOKEN_DECIMALS
    out["virtualTokenReserves"] = v_tokens / 10 ** TOKEN_DECIMALS
    out["virtualSolReserves"] = v_sol / 10 ** SOL_DECIMALS
    out["realTokenReserves"] = r_tokens / 10 ** TOKEN_DECIMALS
    out["realSolReserves"] = round(max(0.0, out["virtualSolReserves"] - INITIAL_VIRTUAL_SOL), 9)
    out["reservesKnown"] = True
    return out


def has_create_event(logs) -> bool:
    """Do these logs carry our creation discriminator, even if the body is unreadable?

    parse_create_event answers None to two very different things: logs that hold no
    creation at all, and a creation whose body this version cannot decode. Losing the
    second silently means the summary reports "no tokens" while tokens were born, so an
    unreadable creation gets its own counter instead of vanishing.
    """
    for line in logs or []:
        if not isinstance(line, str) or not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line[len("Program data: "):])
        except Exception:  # noqa: BLE001 - a foreign encoding is not our event
            continue
        if len(raw) >= 8 and raw[:8].hex() == CREATE_EVENT_DISC:
            return True
    return False


def parse_create_event(logs):
    """The creation carried by these logs, or None when they carry none.

    Only a line that decodes to our discriminator counts: a program that merely
    mentions pump.fun in its own log text is not a creation.
    """
    for line in logs or []:
        if not isinstance(line, str) or not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line[len("Program data: "):])
        except Exception:  # noqa: BLE001 - a foreign encoding is not our event
            continue
        if len(raw) < 8 or raw[:8].hex() != CREATE_EVENT_DISC:
            continue
        parsed = _parse_body(raw[8:])
        if parsed:
            return parsed
    return None


def curve_progress_percent(real_sol_reserves):
    """Share of the curve filled, in percent, clamped to 0..100, or None if unknown."""
    if real_sol_reserves is None:
        return None
    try:
        value = float(real_sol_reserves)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return 0.0
    return round(min(100.0, value / CURVE_FILL_SOL * 100), 4)


def _ui_amount(entry: dict) -> float:
    amount = entry.get("uiTokenAmount") or {}
    value = amount.get("uiAmount")
    if value is None:
        try:
            return float(amount.get("uiAmountString") or 0)
        except (TypeError, ValueError):
            return 0.0
    return float(value)


def dev_buy_tokens(tx, creator: str, mint: str, alt_owner=None):
    """Tokens the launch's insider held after the creation transaction, minus before.

    Three answers, and they are not the same thing:

    * None - the transaction was never read (no meta): we do not know.
    * 0.0 - the transaction was read and no tokens of this mint moved between those
      wallets: the creator really bought nothing, and for a pump.fun launch that is
      the strongest signal there is, so it must not be hidden behind null.
    * a number - the balance delta.

    `alt_owner` covers the launches where the buy is signed by the paying wallet that
    is not the `creator` field; both wallets are counted once each.
    """
    meta = (tx or {}).get("meta") or {}
    if not meta:
        return None
    owners = {creator}
    if alt_owner:
        owners.add(alt_owner)

    def total(entries) -> float:
        summary = 0.0
        for entry in entries or []:
            if entry.get("mint") != mint:
                continue
            if (entry.get("owner") or "") not in owners:
                continue
            summary += _ui_amount(entry)
        return summary

    return round(total(meta.get("postTokenBalances")) - total(meta.get("preTokenBalances")), 6)


def dev_buy_percent(dev_tokens, total_supply):
    """The creator's buy as a share of the total supply, or None when unknown."""
    if dev_tokens is None or total_supply is None:
        return None
    try:
        supply = float(total_supply)
    except (TypeError, ValueError):
        return None
    if supply <= 0:
        return None
    return round(float(dev_tokens) / supply * 100, 4)
