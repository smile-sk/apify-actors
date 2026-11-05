"""IBAN validation that actually validates: country table, length and the mod-97 check.

Nothing here needs network or the Apify SDK, so every rule is testable on its own.

The complaint this module answers: most "IBAN validators" check the shape of the
string and then call it valid. A made-up number like DE89370400440532013001 passes
a shape check and fails arithmetic - here it fails, and we say what the right check
digits would have been.
"""
from __future__ import annotations

import re

# country code -> (IBAN length, country name, SEPA member)
REGISTRY: dict[str, tuple[int, str, bool]] = {
    "AD": (24, "Andorra", True),
    "AE": (23, "United Arab Emirates", False),
    "AL": (28, "Albania", False),
    "AT": (20, "Austria", True),
    "AZ": (28, "Azerbaijan", False),
    "BA": (20, "Bosnia and Herzegovina", False),
    "BE": (16, "Belgium", True),
    "BG": (22, "Bulgaria", True),
    "BH": (22, "Bahrain", False),
    "BR": (29, "Brazil", False),
    "BY": (28, "Belarus", False),
    "CH": (21, "Switzerland", True),
    "CR": (22, "Costa Rica", False),
    "CY": (28, "Cyprus", True),
    "CZ": (24, "Czechia", True),
    "DE": (22, "Germany", True),
    "DK": (18, "Denmark", True),
    "DO": (28, "Dominican Republic", False),
    "EE": (20, "Estonia", True),
    "EG": (29, "Egypt", False),
    "ES": (24, "Spain", True),
    "FI": (18, "Finland", True),
    "FO": (18, "Faroe Islands", False),
    "FR": (27, "France", True),
    "GB": (22, "United Kingdom", True),
    "GE": (22, "Georgia", False),
    "GI": (23, "Gibraltar", True),
    "GL": (18, "Greenland", False),
    "GR": (27, "Greece", True),
    "GT": (28, "Guatemala", False),
    "HR": (21, "Croatia", True),
    "HU": (28, "Hungary", True),
    "IE": (22, "Ireland", True),
    "IL": (23, "Israel", False),
    "IQ": (23, "Iraq", False),
    "IS": (26, "Iceland", True),
    "IT": (27, "Italy", True),
    "JO": (30, "Jordan", False),
    "KW": (30, "Kuwait", False),
    "KZ": (20, "Kazakhstan", False),
    "LB": (28, "Lebanon", False),
    "LC": (32, "Saint Lucia", False),
    "LI": (21, "Liechtenstein", True),
    "LT": (20, "Lithuania", True),
    "LU": (20, "Luxembourg", True),
    "LV": (21, "Latvia", True),
    "LY": (25, "Libya", False),
    "MC": (27, "Monaco", True),
    "MD": (24, "Moldova", False),
    "ME": (22, "Montenegro", False),
    "MK": (19, "North Macedonia", False),
    "MN": (20, "Mongolia", False),
    "MR": (27, "Mauritania", False),
    "MT": (31, "Malta", True),
    "MU": (30, "Mauritius", False),
    "NL": (18, "Netherlands", True),
    "NO": (15, "Norway", True),
    "PK": (24, "Pakistan", False),
    "PL": (28, "Poland", True),
    "PS": (29, "Palestine", False),
    "PT": (25, "Portugal", True),
    "QA": (29, "Qatar", False),
    "RO": (24, "Romania", True),
    "RS": (22, "Serbia", False),
    "SA": (24, "Saudi Arabia", False),
    "SC": (31, "Seychelles", False),
    "SD": (18, "Sudan", False),
    "SE": (24, "Sweden", True),
    "SI": (19, "Slovenia", True),
    "SK": (24, "Slovakia", True),
    "SM": (27, "San Marino", True),
    "ST": (25, "Sao Tome and Principe", False),
    "SV": (28, "El Salvador", False),
    "TL": (23, "Timor-Leste", False),
    "TN": (24, "Tunisia", False),
    "TR": (26, "Turkey", False),
    "UA": (29, "Ukraine", False),
    "VA": (22, "Holy See", True),
    "VG": (24, "Virgin Islands (British)", False),
    "XK": (20, "Kosovo", False),
}

# A candidate is matched with the length its own country prescribes, so the pattern
# cannot swallow the word (or the next IBAN) that follows it.
MIN_CANDIDATE = 15


def _patterns() -> list[tuple[str, re.Pattern]]:
    """One pattern per country: the registry gives the length, so nothing is guessed."""
    built = []
    for code, (length, _name, _sepa) in sorted(REGISTRY.items()):
        body = length - 4
        built.append((code, re.compile(
            rf"\b{code}\s?\d{{2}}(?:[\s-]?[A-Z0-9]){{{body}}}(?![A-Z0-9])", re.IGNORECASE)))
    return built


_PATTERNS: list[tuple[str, re.Pattern]] | None = None


def normalize(raw: str) -> str:
    """Drop spaces and dashes, upper-case. Nothing else: the characters stay as typed."""
    return re.sub(r"[\s\-]", "", (raw or "")).upper()


def printed(iban: str) -> str:
    """The way banks print it: groups of four."""
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def extract(text: str, limit: int = 500) -> list[str]:
    """IBANs out of free text: invoices, emails, pasted tables. Order kept, duplicates dropped."""
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = _patterns()
    hits: list[tuple[int, str]] = []
    for _code, pattern in _PATTERNS:
        for match in pattern.finditer(text or ""):
            hits.append((match.start(), normalize(match.group(0))))
    hits.sort()

    found: list[str] = []
    seen: set[str] = set()
    for _pos, value in hits:
        if value in seen:
            continue
        seen.add(value)
        found.append(value)
        if len(found) >= limit:
            break
    return found


def mod97(iban: str) -> int:
    """ISO 13616: move the first four characters to the end, letters become numbers."""
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    remainder = 0
    for start in range(0, len(digits), 8):        # chunked: no big integers, same answer
        remainder = int(str(remainder) + digits[start:start + 8]) % 97
    return remainder


def expected_check_digits(iban: str) -> str | None:
    """What the two check digits should be. Used to explain a failure, never to hide it."""
    if len(iban) < 5:
        return None
    body = iban[4:]
    probe = iban[:2] + "00" + body
    try:
        value = 98 - mod97(probe)
    except ValueError:
        return None
    return f"{value:02d}"


def validate_bic(bic: str, country: str) -> tuple[bool | None, str]:
    """BIC is 8 or 11 characters and carries the country in positions 5-6.

    We can prove the pair is inconsistent - that is a real check. We cannot prove
    the BIC belongs to the account from the IBAN alone, and we do not claim it.
    """
    value = (bic or "").strip().upper()
    if not value:
        return None, ""
    if not re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?", value):
        return False, "BIC is not 8 or 11 characters of the right shape"
    if value[4:6] != (country or ""):
        return False, f"BIC is registered in {value[4:6]}, the IBAN in {country}"
    return True, ""


def check(raw: str, bic: str = "") -> dict:
    """One row per input line. `valid` is True only when the arithmetic holds."""
    iban = normalize(raw)
    row = {
        "input": raw,
        "iban": iban,
        "printed": printed(iban) if iban else "",
        "valid": False,
        "status": "",
        "reason": "",
        "countryCode": iban[:2] if len(iban) >= 2 else "",
        "country": "",
        "sepa": False,
        "expectedLength": None,
        "actualLength": len(iban),
        "checkDigits": iban[2:4] if len(iban) >= 4 else "",
        "expectedCheckDigits": None,
        "bban": iban[4:] if len(iban) > 4 else "",
        "checksumRemainder": None,
        "bic": (bic or "").strip().upper(),
        "bicValid": None,
        "bicReason": "",
        "checkable": False,          # did we reach real arithmetic, or only the shape?
    }
    if not iban:
        row["status"] = "EMPTY"
        row["reason"] = "no IBAN in this line"
        return row
    if not re.fullmatch(r"[A-Z0-9]+", iban):
        row["status"] = "BAD_CHARACTERS"
        row["reason"] = "only letters and digits are allowed inside an IBAN"
        return row
    if len(iban) < 4 or not iban[:2].isalpha() or not iban[2:4].isdigit():
        row["status"] = "BAD_FORMAT"
        row["reason"] = "an IBAN starts with a two-letter country code and two check digits"
        return row

    country = REGISTRY.get(row["countryCode"])
    if country is None:
        row["status"] = "UNKNOWN_COUNTRY"
        row["reason"] = f"{row['countryCode']} is not in the IBAN country registry"
        return row

    length, name, sepa = country
    row["country"] = name
    row["sepa"] = sepa
    row["expectedLength"] = length
    if len(iban) != length:
        row["status"] = "BAD_LENGTH"
        row["reason"] = f"{name} IBANs are {length} characters, this one is {len(iban)}"
        return row

    row["checkable"] = True
    remainder = mod97(iban)
    row["checksumRemainder"] = remainder
    if remainder != 1:
        row["expectedCheckDigits"] = expected_check_digits(iban)
        row["status"] = "BAD_CHECKSUM"
        row["reason"] = ("check digits do not match: mod-97 is "
                         f"{remainder}, it must be 1"
                         + (f"; the right pair would be {row['expectedCheckDigits']}"
                            if row["expectedCheckDigits"] else ""))
    else:
        row["valid"] = True
        row["status"] = "VALID"
        row["reason"] = "mod-97 holds" + (", SEPA member" if sepa else ", not a SEPA member")

    bic_ok, bic_reason = validate_bic(bic, row["countryCode"])
    row["bicValid"] = bic_ok
    row["bicReason"] = bic_reason
    if bic_ok is False:
        row["valid"] = False
        row["status"] = "BAD_BIC"
        row["reason"] = f"{row['reason']}; {bic_reason}"
    return row


def rows_from_input(value, bics) -> tuple[list[tuple[str, str]], int]:
    """Accept a list, one-per-line text, or a single string. Same for BICs (parallel list)."""
    if isinstance(value, str):
        items = [line for line in re.split(r"[\r\n;,]+", value) if line.strip()]
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value if str(item).strip()]
    elif value is None:
        items = []
    else:
        items = [str(value)]

    bic_list: list[str] = []
    if isinstance(bics, str):
        bic_list = [line.strip() for line in re.split(r"[\r\n;,]+", bics) if line.strip()]
    elif isinstance(bics, (list, tuple)):
        bic_list = [str(item).strip() for item in bics]

    pairs = [(item, bic_list[i] if i < len(bic_list) else "") for i, item in enumerate(items)]
    return pairs, len(bic_list)
