"""Phone number analysis: offline validation and formatting with Google's libphonenumber.

Nothing here talks to the network. The whole module is a pure function of
(input line, default country) -> (row, or an honest reason why not).
"""
from __future__ import annotations

import re
import unicodedata

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType, carrier, geocoder, timezone
from phonenumbers import NumberParseException

TYPE_NAMES = {
    PhoneNumberType.FIXED_LINE: "fixed_line",
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_line_or_mobile",
    PhoneNumberType.TOLL_FREE: "toll_free",
    PhoneNumberType.PREMIUM_RATE: "premium_rate",
    PhoneNumberType.SHARED_COST: "shared_cost",
    PhoneNumberType.VOIP: "voip",
    PhoneNumberType.PERSONAL_NUMBER: "personal_number",
    PhoneNumberType.PAGER: "pager",
    PhoneNumberType.UAN: "uan",
    PhoneNumberType.VOICEMAIL: "voicemail",
    PhoneNumberType.UNKNOWN: "unknown",
}

SMS_TYPES = ("mobile", "fixed_line_or_mobile")

# Labels people paste in front of a number: "Phone: +1...", "Tel. 044...", "Mob. 095...".
# Non-English labels are written as escapes on purpose: the Actor source must stay ASCII.
LABEL = re.compile(
    r"(?i)^\s*(?:phone(?:\s*number)?|tel(?:ephone)?|call|mobile|mob|cell|whats\s?app|wa|contact"
    r"|\u0442\u0435\u043b(?:\u0435\u0444\u043e\u043d)?"
    r"|\u043c\u043e\u0431(?:\u0438\u043b\u044c\u043d\u044b\u0439)?"
    r"|\u043a\u043e\u043d\u0442\u0430\u043a\u0442"
    r"|\u0437\u0432\u043e\u043d(?:\u0438\u0442\u0435)?)\s*[:.\-]?\s*"
)
_EXT_WORDS = r"(?:ext\.?|extension|dob\.?|x|#|\u0434\u043e\u0431\.?|\u0434\u043e\u0431\u0430\u0432\u043e\u0447\u043d\u044b\u0439)"
EXT = re.compile(r"(?i)\s*" + _EXT_WORDS + r"\s*(\d{1,6})\s*$")
EXT_AHEAD = re.compile(r"(?i)^\s*" + _EXT_WORDS + r"[\s.:\-]*(\d{1,6})")
RUN = re.compile(r"[+]?\d[\d ()\-./\s]{4,}\d")

# International form with a national trunk prefix in brackets: "+34 (0) 91 123 45 67".
# libphonenumber strips "(0)" only in countries whose trunk prefix really is 0, so the
# bracket has to go before parsing, and only right after the country code.
TRUNK_ZERO = re.compile(r"^(\+\d{1,3})[\s.\-]*\(\s*0\s*\)\s*")

_DIGIT_MAP = {}
for _i, _ch in enumerate("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"):
    _DIGIT_MAP[ord(_ch)] = str(_i)
for _i, _ch in enumerate("\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"):
    _DIGIT_MAP[ord(_ch)] = str(_i)

_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)

# Dash variants (written as escapes on purpose: the live characters are banned in our text).
_DASHES = {ord(ch): "-" for ch in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"}


def normalize(text: str) -> str:
    """Fold look-alikes: full-width forms, Arabic-Indic digits, odd spaces and dashes."""
    out = unicodedata.normalize("NFKC", str(text))
    out = out.translate(_DIGIT_MAP)
    out = out.translate(_INVISIBLE)
    out = out.translate(_DASHES)
    out = out.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ").replace("\t", " ")
    return out


def resolve_default_region(raw) -> str | None:
    """ISO-2 country code, or None for auto. A wrong code is an error, never a silent guess."""
    value = (raw or "").strip()
    if not value or value.lower() in ("auto", "none", "-", "any"):
        return None
    upper = value.upper()
    if upper not in phonenumbers.SUPPORTED_REGIONS:
        raise ValueError(
            "Default country '%s' is not an ISO-2 code. Use two letters such as UA, US, GB, DE, "
            "or leave the field empty to read only numbers that carry a country code." % value[:40]
        )
    return upper


def lines_from_input(numbers, text) -> list[str]:
    """Numbers array first, pasted text only when the array is empty (no double processing)."""
    def split(value):
        parts = str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return [part.lstrip("\ufeff") for part in parts]

    raw: list[str] = []
    if isinstance(numbers, str):
        raw.extend(split(numbers))
    elif isinstance(numbers, (list, tuple)):
        for item in numbers:
            if item is None:
                continue
            raw.extend(split(item))
    if not raw and text:
        raw.extend(split(text))
    # A blank line at the very start or end is not a row (a trailing newline is not a cell),
    # but a blank line in the middle stays and becomes a NO_NUMBER row: that keeps `index`
    # equal to the line number of the source list.
    while raw and not raw[0].strip():
        raw.pop(0)
    while raw and not raw[-1].strip():
        raw.pop()
    return raw


def candidates(text: str) -> list[tuple[str, int]]:
    """Digit runs that could be a phone number, with their position in the line."""
    found: list[tuple[str, int]] = []
    for match in RUN.finditer(text):
        raw = match.group(0).strip(" ()-./")
        digits = sum(ch.isdigit() for ch in raw)
        if digits < 6 or digits > 17:
            continue
        ahead = EXT_AHEAD.match(text[match.end(): match.end() + 24])
        if ahead:
            raw = raw + " " + ahead.group(0).strip()
        found.append((raw, match.start()))
    return found


def _parse(raw: str, default_region: str | None):
    """One candidate -> (phone number, extension, error type, error text)."""
    body = raw.strip(" ()-./")
    extension = None
    ext_match = EXT.search(body)
    if ext_match:
        extension = ext_match.group(1)
        body = body[: ext_match.start()].strip(" ()-./")
    if body.lower().startswith("tel:"):
        body = body[4:].strip()
    if body.startswith("00"):
        body = "+" + body[2:]

    # Two readings of the same line: with the national trunk prefix removed and as typed.
    # The first valid parse wins, so "+34 (0) 91 ..." and "+34 91 ..." give the same number.
    attempts = [body]
    without_trunk = TRUNK_ZERO.sub(r"\1 ", body)
    if without_trunk != body:
        attempts.insert(0, without_trunk)

    best = None
    best_score = 0
    first_error = None
    for attempt in attempts:
        try:
            candidate = phonenumbers.parse(attempt, default_region)
        except NumberParseException as exc:
            first_error = first_error or exc
            continue
        if phonenumbers.is_valid_number(candidate):
            score = 3
        elif phonenumbers.is_possible_number(candidate):
            score = 2
        else:
            score = 1
        if score > best_score:
            best, best_score = candidate, score
        if score == 3:
            break
    if best is None:
        exc = first_error
        if exc is None:                                          # pragma: no cover - defensive
            return None, extension, NumberParseException.NOT_A_NUMBER, "This does not look like a phone number."
        return None, extension, exc.error_type, str(exc)

    number = best
    if extension:
        number.extension = extension
    return number, extension, None, ""


def type_name(number) -> str:
    try:
        return TYPE_NAMES.get(phonenumbers.number_type(number), "unknown")
    except Exception:                                            # noqa: BLE001
        return "unknown"


def number_fields(number) -> dict:
    """Everything we can honestly say about a parsed number, offline."""
    valid = phonenumbers.is_valid_number(number)
    possible = phonenumbers.is_possible_number(number)
    region = phonenumbers.region_code_for_number(number) if (valid or possible) else None
    kind = type_name(number)
    try:
        zones = list(timezone.time_zones_for_number(number))
    except Exception:                                            # noqa: BLE001
        zones = []
    try:
        area = geocoder.description_for_number(number, "en") or ""
    except Exception:                                            # noqa: BLE001
        area = ""
    try:
        operator = carrier.name_for_number(number, "en") or ""
    except Exception:                                            # noqa: BLE001
        operator = ""
    try:
        country = geocoder.country_name_for_number(number, "en") or ""
    except Exception:                                            # noqa: BLE001
        country = ""
    return {
        "valid": valid,
        "possible": possible,
        "type": kind,
        "countryCode": region,
        "countryName": country,
        "callingCode": "+%d" % number.country_code if number.country_code else "",
        "e164": phonenumbers.format_number(number, PhoneNumberFormat.E164) if (valid or possible) else "",
        "international": phonenumbers.format_number(number, PhoneNumberFormat.INTERNATIONAL) if (valid or possible) else "",
        "national": phonenumbers.format_number(number, PhoneNumberFormat.NATIONAL) if (valid or possible) else "",
        "rfc3966": phonenumbers.format_number(number, PhoneNumberFormat.RFC3966) if (valid or possible) else "",
        "extension": str(number.extension) if number.extension else None,
        "smsCapable": bool(valid and kind in SMS_TYPES),
        "timeZones": zones,
        "regionDescription": area,
        "carrier": operator,
    }


def classify(number, default_region: str | None) -> tuple[str, str]:
    """Status plus a reason a human can act on."""
    valid = phonenumbers.is_valid_number(number)
    possible = phonenumbers.is_possible_number(number)
    region = phonenumbers.region_code_for_number(number)
    try:
        country = geocoder.country_name_for_number(number, "en") or region or ""
    except Exception:                                            # noqa: BLE001
        country = region or ""
    # Leading zeros live outside national_number (they are a separate field), and a
    # caller reading "1 digits" about a 10-digit number would rightly not trust us.
    digits = len(str(number.national_number)) + int(getattr(number, "number_of_leading_zeros", 0) or 0)

    if valid:
        kind = type_name(number).replace("_", " ")
        return "VALID", "Valid %s number%s." % (kind, (" for " + country) if country else "")
    if possible:
        return "POSSIBLE_ONLY", (
            "Right length for %s (%d digits) but libphonenumber does not treat it as an assigned "
            "number: the prefix is not in range." % (country or ("+" + str(number.country_code)), digits)
        )
    if region:
        return "INVALID", (
            "Not valid for %s: %d digits do not match any phone format of that country." % (country, digits)
        )
    if number.country_code:
        # The calling code itself is fine (it parsed), the digits after it are not.
        return "INVALID", (
            "Country code +%d is a real calling code, but the %d digits after it do not form a "
            "number of that country." % (number.country_code, digits)
        )
    if default_region is None:
        return "INVALID", (
            "No country code in this line. Local numbers need 'Default country' (ISO-2), for example UA."
        )
    return "INVALID", "Not a phone number: %d digits given." % digits


def _parse_reason(error_type, error_text: str, default_region: str | None,
                  had_country_prefix: bool) -> str:
    if error_type == NumberParseException.INVALID_COUNTRY_CODE:
        if had_country_prefix:
            return "Country code is not recognised: no country uses these leading digits."
        if default_region is None:
            return ("No country code in this line. Local numbers need 'Default country' (ISO-2), "
                    "for example UA, US, GB.")
        return "Country code is not recognised; with the default country set, the line still has no valid prefix."
    if error_type == NumberParseException.TOO_SHORT_NSN:
        return "Too few digits for a phone number."
    if error_type == NumberParseException.TOO_LONG:
        return "Too many digits for a phone number."
    if error_type == NumberParseException.NOT_A_NUMBER:
        return "This does not look like a phone number."
    return ("Could not parse this line as a phone number: %s" % (error_text or "")).strip()


def analyze_cell(cell, default_region: str | None = None) -> dict:
    """One input line -> one row. Never raises on bad input, never invents a number."""
    given = "" if cell is None else str(cell)
    text = normalize(given)
    probe = LABEL.sub("", text).strip()
    found = candidates(probe)

    parsed = []
    errors = []
    for raw, position in found:
        number, extension, error_type, error_text = _parse(raw, default_region)
        if number is None:
            had_prefix = raw.strip().startswith("+") or raw.strip().startswith("00")
            errors.append((raw, error_type, error_text, had_prefix))
            continue
        if phonenumbers.is_valid_number(number):
            score = 3
        elif phonenumbers.is_possible_number(number):
            score = 2
        else:
            score = 1
        parsed.append((score, position, raw, number))

    if not parsed:
        row = {
            "inputAsGiven": given,
            "status": "NO_NUMBER" if not errors else "INVALID",
            "reason": ("No phone number found in this line." if not errors
                       else _parse_reason(errors[0][1], errors[0][2], default_region,
                                          errors[0][3])),
            "valid": False,
            "possible": False,
            "type": "unknown",
            "countryCode": None,
            "countryName": "",
            "callingCode": "",
            "e164": "",
            "international": "",
            "national": "",
            "rfc3966": "",
            "extension": None,
            "smsCapable": False,
            "timeZones": [],
            "regionDescription": "",
            "carrier": "",
            "candidatesFound": len(found),
            "otherNumbers": [],
            "duplicateOfIndex": None,
            "billable": False,
        }
        if errors and not found:
            row["status"] = "INVALID"
        return row

    parsed.sort(key=lambda item: (-item[0], item[1]))
    score, _position, raw, number = parsed[0]
    fields = number_fields(number)
    status, reason = classify(number, default_region)

    other_numbers = []
    seen = {fields["e164"]} if fields["e164"] else set()
    for _score, _pos, raw2, number2 in parsed[1:]:
        if not phonenumbers.is_valid_number(number2):
            continue
        fields2 = number_fields(number2)
        if fields2["e164"] and fields2["e164"] not in seen:
            seen.add(fields2["e164"])
            other_numbers.append({
                "e164": fields2["e164"],
                "type": fields2["type"],
                "inputAsGiven": raw2,
            })

    row = {"inputAsGiven": given, "status": status, "reason": reason}
    row.update(fields)
    row["candidatesFound"] = len(found)
    row["otherNumbers"] = other_numbers
    row["duplicateOfIndex"] = None
    row["billable"] = False
    row["parsedAsGiven"] = raw
    return row
