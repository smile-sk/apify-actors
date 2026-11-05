"""VAT number checks that do not need the network.

Two layers, and they are deliberately separate:

1. Shape and checksum - arithmetic on the digits themselves. A number that
   fails here is wrong even when VIES is up, and we can say exactly which
   digit is wrong instead of a flat "invalid".
2. VIES - the only authority on whether a number is *registered*. Everything
   here is a filter that saves VIES calls, never a replacement for them.

A checksum is implemented only for countries where it was verified against
numbers that VIES itself reports as valid. For the rest the field
`checksumImplemented` is False and the verdict comes from VIES alone - we do
not guess, because a false "invalid" on a real customer number is the one
error this actor must never make.

There is a third outcome, and it matters: when the national algorithm cannot
decide (the Dutch eleven-proof is not satisfied by every number the Dutch
registry issues), the check returns None. None means "not conclusive", the row
goes to VIES, and the checksum field says what was actually computed. A failed
check must never become a rejected customer.
"""
from __future__ import annotations

import re

# A checksum that was not applied: the number's shape is one this checker does
# not model (an alphanumeric French key, a ten-digit Czech number, a Spanish
# form we do not know). Distinct from None, which means the sum was computed
# and did not settle the question. Reporting either as True would be a claim we
# did not earn, and reporting them as False would reject a real number.
SKIPPED = "skipped"

# Country labels that travel with the number and are not part of it.
LABELS = {"NO": r"MVA$", "CH": r"(?:MWST|TVA|IVA)$"}

# A number with its prefix and spaces inside it, as people write them in
# documents: "DE 811 569 869", "CHE-432.825.998", "de 811 569 869". The
# single-token pass below cannot see these, and splitting them produced DE811,
# 569 and 869 - three rows, none of them the number that was sent. The prefix is
# restricted to codes this checker knows, so a word in front of a number ("in
# 2024") is not read as a country. Defined after COUNTRIES, which it uses.

# code: (min length, max length, allowed shape, checksum function name or "")
COUNTRIES: dict[str, tuple[int, int, str, str]] = {
    "AT": (9, 9, r"U\d{8}", "at"),
    "BE": (9, 10, r"\d{9,10}", "be"),
    "BG": (9, 10, r"\d{9,10}", ""),
    "CY": (9, 9, r"\d{8}[A-Z]", ""),
    "CZ": (8, 10, r"\d{8,10}", "cz"),
    "DE": (9, 9, r"\d{9}", "de"),
    "DK": (8, 8, r"\d{8}", "dk"),
    "EE": (9, 9, r"\d{9}", "ee"),
    "GR": (9, 9, r"\d{9}", ""),
    "ES": (9, 9, r"[A-Z0-9]\d{7}[A-Z0-9]", "es"),
    "FI": (8, 8, r"\d{8}", "fi"),
    "FR": (11, 11, r"[A-Z0-9]{2}\d{9}", "fr"),
    "HR": (11, 11, r"\d{11}", "hr"),
    "HU": (8, 8, r"\d{8}", "hu"),
    "IE": (8, 9, r"\d{7}[A-W][A-Z]?|\d[A-Z+*]\d{5}[A-Z]", "ie"),
    "IT": (11, 11, r"\d{11}", "it"),
    "LT": (9, 12, r"\d{9}|\d{12}", ""),
    "LU": (8, 8, r"\d{8}", "lu"),
    "LV": (11, 11, r"\d{11}", ""),
    "MT": (8, 8, r"\d{8}", ""),
    "NL": (12, 12, r"\d{9}B\d{2}", "nl"),
    "PL": (10, 10, r"\d{10}", "pl"),
    "PT": (9, 9, r"\d{9}", "pt"),
    "RO": (2, 10, r"\d{2,10}", ""),
    "SE": (12, 12, r"\d{12}", "se"),
    "SI": (8, 8, r"\d{8}", "si"),
    "SK": (10, 10, r"\d{10}", ""),
    "XI": (9, 12, r"\d{9}|\d{12}", "gb"),
    "GB": (5, 12, r"\d{9}|\d{12}|(?:GD|HA)\d{3}", "gb"),
    "NO": (9, 9, r"\d{9}", "no"),
    "CH": (9, 9, r"\d{9}", "ch"),
}

# A run of digits with spaces, dots or dashes inside it. Used only when a country
# hint is given: a column pasted without prefixes then stays one number per row
# instead of splitting into 811, 569 and 869.
SPACED_NUMBER = re.compile(r"\d(?:[\d .\-]{5,20}\d)")

_COUNTRY_PREFIXES = "|".join(sorted(COUNTRIES, key=len, reverse=True))
PREFIXED_TOKEN = re.compile(
    rf"\b(?:CHE|EL|{_COUNTRY_PREFIXES})[ .\-]?[0-9](?:[0-9]|[ .\-][0-9]){{6,17}}\b",
    re.IGNORECASE,
)


def _shape_ok(country: str, number: str) -> bool:
    """Whether a number fits a country's published shape. No network, no guess."""
    spec = COUNTRIES.get(country)
    if not spec:
        return False
    low, high, pattern, _check = spec
    return low <= len(number) <= high and re.fullmatch(pattern, number) is not None

# VIES covers EU members plus Northern Ireland (XI). GB, NO and CH are not in it:
# for them the verdict is the national checksum alone, and every row says so.
VIES_COUNTRIES = frozenset({
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR",
    "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI",
    "SK", "XI",
})

# VIES answers in ISO codes, and Greece is the one that disagrees with itself:
# the country code is GR, the VAT prefix is EL. Accept both, always ask as EL.
VIES_PREFIX = {"GR": "EL"}


def _digits(value: str) -> list[int]:
    return [int(ch) for ch in value if ch.isdigit()]


def _luhn(value: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(value)):
        digit = int(ch)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _de(number: str) -> tuple[bool, str]:
    """ISO 7064 MOD 11,10 over the first eight digits."""
    product = 10
    for digit in _digits(number[:8]):
        total = (digit + product) % 10
        if total == 0:
            total = 10
        product = (2 * total) % 11
    expected = 11 - product
    if expected == 10:
        expected = 0
    ok = expected == int(number[8])
    return ok, f"last digit should be {expected}"


def _dk(number: str) -> tuple[bool, str]:
    """Danish CVR: weights 2..1 over eight digits, the sum must be a multiple of 11."""
    weights = (2, 7, 6, 5, 4, 3, 2, 1)
    total = sum(int(d) * w for d, w in zip(number[:8], weights))
    ok = total % 11 == 0
    return ok, f"weighted sum is {total}, must be a multiple of 11"


def _nl(number: str) -> tuple[bool | None, str]:
    """Eleven-proof over the nine digits before the B.

    The Dutch registry also issues numbers that do not satisfy the eleven-proof,
    so a failure here is not proof of a wrong number: it is handed to VIES, and
    the row says the proof did not hold. Only a pass is treated as a verdict.
    """
    digits = _digits(number[:9])
    total = sum(d * weight for d, weight in zip(digits[:8], range(9, 1, -1))) - digits[8]
    if total % 11 == 0:
        return True, ""
    return None, (f"the eleven-proof does not hold (weighted sum {total}), and not every Dutch "
                  f"number satisfies it, so VIES decides")


def _be(number: str) -> tuple[bool, str]:
    number = number.zfill(10) if len(number) == 9 else number
    head, tail = int(number[:8]), int(number[8:])
    expected = 97 - head % 97
    ok = expected == tail
    return ok, f"check digits should be {expected:02d}"


def _fr(number: str) -> tuple[bool | None | str, str]:
    key, siren = number[:2], number[2:]
    if not key.isdigit():
        return SKIPPED, "alphanumeric key, not checked here"
    expected = (12 + 3 * (int(siren) % 97)) % 97
    ok = int(key) == expected
    return ok, f"key should be {expected:02d}"


def _it(number: str) -> tuple[bool, str]:
    ok = _luhn(number)
    return ok, "Luhn check failed" if not ok else ""


def _pl(number: str) -> tuple[bool, str]:
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    total = sum(int(d) * w for d, w in zip(number[:9], weights)) % 11
    if total == 10:
        return False, "checksum digit cannot be produced"
    ok = total == int(number[9])
    return ok, f"last digit should be {total}"


def _es(number: str) -> tuple[bool | None | str, str]:
    """Spanish numbers: a mod-23 letter for people, a digit sum for companies.

    The nine character shape is shared by four kinds of number, and they do not
    share a checksum: DNI (eight digits), NIE (X, Y or Z), the old K/L/M forms,
    and CIF for companies. Checking them all as CIF rejects real taxpayers.
    """
    body, control = number[:8], number[8]
    letters = "TRWAGMYFPDXBNJZSQVHLCKE"

    if body.isdigit():
        expected = letters[int(body) % 23]
        return control == expected, f"control letter should be {expected}"
    first, rest = body[0], body[1:]
    if first in "XYZ" and rest.isdigit():
        expected = letters[int({"X": "0", "Y": "1", "Z": "2"}[first] + rest) % 23]
        return control == expected, f"control letter should be {expected}"
    if first in "KLM" and rest.isdigit():
        expected = letters[int("0" + rest) % 23]
        return control == expected, f"control letter should be {expected}"
    if not (first.isalpha() and rest.isdigit()):
        return SKIPPED, "not a Spanish shape this checker knows"

    total = 0
    for index, ch in enumerate(rest):
        digit = int(ch)
        if index % 2 == 1:          # positions 2, 4, 6 of the CIF: added as is
            total += digit
        else:                        # positions 1, 3, 5, 7: doubled, digits summed
            doubled = digit * 2
            total += doubled // 10 + doubled % 10
    digit_control = (10 - total % 10) % 10
    letter_control = "JABCDEFGHI"[digit_control]
    if control.isdigit():
        ok = int(control) == digit_control
    else:
        ok = control == letter_control
    return ok, f"control character should be {digit_control} or {letter_control}"


def _se(number: str) -> tuple[bool, str]:
    if number[10:] != "01":
        return False, "Swedish VAT numbers end with 01 after the organisation number"
    ok = _luhn(number[:10])
    return ok, "Luhn check failed on the organisation number" if not ok else ""


def _fi(number: str) -> tuple[bool, str]:
    weights = (7, 9, 10, 5, 8, 4, 2)
    total = sum(int(d) * w for d, w in zip(number[:7], weights))
    remainder = total % 11
    if remainder == 0:
        expected = 0
    elif remainder == 1:
        return False, "no valid check digit exists for this number"
    else:
        expected = 11 - remainder
    ok = expected == int(number[7])
    return ok, f"last digit should be {expected}"


def _pt(number: str) -> tuple[bool, str]:
    weights = (9, 8, 7, 6, 5, 4, 3, 2)
    remainder = sum(int(d) * w for d, w in zip(number[:8], weights)) % 11
    expected = 0 if remainder < 2 else 11 - remainder
    ok = expected == int(number[8])
    return ok, f"last digit should be {expected}"


def _cz(number: str) -> tuple[bool | None | str, str]:
    if len(number) != 8:
        return SKIPPED, "only the eight-digit form is checked here"
    weights = (8, 7, 6, 5, 4, 3, 2)
    remainder = sum(int(d) * w for d, w in zip(number[:7], weights)) % 11
    expected = 0 if remainder == 1 else (1 if remainder == 0 else 11 - remainder)
    ok = expected == int(number[7])
    return ok, f"last digit should be {expected}"


def _hr(number: str) -> tuple[bool, str]:
    product = 10
    for digit in _digits(number[:10]):
        total = (digit + product) % 10
        if total == 0:
            total = 10
        product = (2 * total) % 11
    expected = 11 - product
    if expected == 10:
        expected = 0
    ok = expected == int(number[10])
    return ok, f"last digit should be {expected}"


def _ie(number: str) -> tuple[bool | None | str, str]:
    """Irish numbers: seven digits, a check letter, and a second letter.

    The nine character form (seven digits and two letters) is not checked by the
    first letter alone: the second letter carries weight 9 and moves the whole
    sum, so ignoring it rejects real numbers.
    """
    if len(number) < 8:
        return False, "too short for the Irish form"
    digits, check = number[:7], number[7]
    if not digits.isdigit():
        return SKIPPED, "not the seven-digit form, not checked here"
    weights = (8, 7, 6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(digits, weights))
    if len(number) == 9 and number[8].isalpha():
        total += 9 * (ord(number[8]) - ord("A") + 1)
    letters = "WABCDEFGHIJKLMNOPQRSTUV"
    expected = letters[total % 23]
    ok = check == expected
    return ok, f"check letter should be {expected}"


def _at(number: str) -> tuple[bool, str]:
    body = number[1:]
    if not body.isdigit() or len(body) != 8:
        return False, "Austrian form is U plus eight digits"
    total = 0
    for index, ch in enumerate(body[:7]):
        digit = int(ch)
        if index % 2 == 1:
            doubled = digit * 2
            total += doubled // 10 + doubled % 10
        else:
            total += digit
    expected = (96 - total) % 10
    ok = expected == int(body[7])
    return ok, f"last digit should be {expected}"


def _lu(number: str) -> tuple[bool, str]:
    head, tail = int(number[:6]), int(number[6:])
    ok = head % 89 == tail
    return ok, f"check digits should be {head % 89:02d}"


def _hu(number: str) -> tuple[bool, str]:
    """Hungarian number: the check digit is ten minus the last digit of the sum."""
    weights = (9, 7, 3, 1, 9, 7, 3)
    total = sum(int(d) * w for d, w in zip(number[:7], weights))
    expected = (10 - total % 10) % 10
    ok = expected == int(number[7])
    return ok, f"last digit should be {expected}"


def _si(number: str) -> tuple[bool, str]:
    """Slovenian number: 11 minus the remainder, where ten becomes zero and eleven is no digit."""
    weights = (8, 7, 6, 5, 4, 3, 2)
    remainder = sum(int(d) * w for d, w in zip(number[:7], weights)) % 11
    expected = 11 - remainder
    if expected == 11:
        return False, "no valid check digit exists for this number"
    if expected == 10:
        expected = 0
    ok = expected == int(number[7])
    return ok, f"last digit should be {expected}"


def _ee(number: str) -> tuple[bool, str]:
    weights = (3, 7, 1, 3, 7, 1, 3, 7)
    total = sum(int(d) * w for d, w in zip(number[:8], weights))
    expected = (10 - total % 10) % 10
    ok = expected == int(number[8])
    return ok, f"last digit should be {expected}"


def _gb(number: str) -> tuple[bool | None | str, str]:
    """HMRC: two modular methods, the branch form, and the GD and HA ranges.

    The published rule is modular. Add the weighted sum of the first seven digits
    to the two check digits: the number is valid when the total is a multiple of
    97, and HMRC also accepts two older variants where the same total leaves 42
    or 55. A remainder of zero is written as check digits 00 (97 also appears in
    older records), so accepting only 97 rejects roughly one number in 97 - all
    of them real. With the twelve-digit branch form only the first nine digits
    take part. Government departments and health authorities carry three digits
    and are valid inside their published ranges: GD000 to GD499, HA500 to HA999.
    """
    if len(number) == 5 and number[:2] in ("GD", "HA") and number[2:].isdigit():
        value = int(number[2:])
        if number[:2] == "GD":
            return value <= 499, "government department numbers are GD000 to GD499"
        return value >= 500, "health authority numbers are HA500 to HA999"
    core = number[:9] if len(number) == 12 else number
    if len(core) != 9 or not core.isdigit():
        return SKIPPED, "only the nine- and twelve-digit forms are checked here"
    weights = (8, 7, 6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(core[:7], weights)) % 97
    check = int(core[7:9])
    if (total + check) % 97 in (0, 42, 55):
        return True, ""
    expected = (97 - total) % 97
    return False, f"check digits should be {expected:02d}"


def _no(number: str) -> tuple[bool, str]:
    weights = (3, 2, 7, 6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(number[:8], weights))
    remainder = total % 11
    if remainder == 1:
        # Ten is not a digit: no number with this weighted sum can be valid.
        return False, "no valid check digit exists for this number"
    expected = 0 if remainder == 0 else 11 - remainder
    ok = expected == int(number[8])
    return ok, f"last digit should be {expected}"


def _ch(number: str) -> tuple[bool, str]:
    """Swiss form is CHE plus nine digits; the prefix is already stripped upstream."""
    body = number[3:] if number.startswith("CHE") else number
    if not body.isdigit() or len(body) != 9:
        return False, "Swiss form is CHE plus nine digits"
    weights = (5, 4, 3, 2, 7, 6, 5, 4)
    total = sum(int(d) * w for d, w in zip(body[:8], weights))
    expected = 11 - total % 11
    if expected == 10:
        return False, "no valid check digit exists for this number"
    if expected == 11:
        expected = 0
    ok = expected == int(body[8])
    return ok, f"last digit should be {expected}"


CHECKS = {
    "at": _at, "be": _be, "ch": _ch, "cz": _cz, "de": _de, "dk": _dk, "ee": _ee, "es": _es,
    "fi": _fi, "fr": _fr, "gb": _gb, "hr": _hr, "hu": _hu, "ie": _ie, "it": _it,
    "lu": _lu, "nl": _nl, "no": _no, "pl": _pl, "pt": _pt, "se": _se, "si": _si,
}


def _strip_label(country: str, number: str) -> str:
    """Drop the country's own label: the Norwegian MVA, the Swiss MWST/TVA/IVA.

    'CHE-432.825.998 MWST' is how a Swiss number is normally written, and reading
    the label as part of the number rejected every Swiss number in the reference
    set with "must match nine digits". A label is a word, not a digit.
    """
    pattern = LABELS.get(country)
    return re.sub(pattern, "", number) if pattern else number


def split_country(raw: str, hint: str = "") -> tuple[str, str]:
    """Separate the country prefix from the number.

    Accepts 'DE259137088', 'DE 259 137 088', 'de-259137088', and a bare number
    when a country hint is given. GR is normalised to EL only when asking VIES.
    Country labels are dropped: they are words, not digits.
    """
    cleaned = re.sub(r"[\s.\-_/]", "", (raw or "").upper())
    cleaned = cleaned.rstrip(",;")
    hint = (hint or "").strip().upper()
    if cleaned.startswith("EL"):
        cleaned = "GR" + cleaned[2:]
    if cleaned.startswith("CHE") and len(cleaned) > 3:
        return "CH", _strip_label("CH", cleaned[3:])
    if len(cleaned) > 2 and cleaned[:2].isalpha() and cleaned[:2] in COUNTRIES:
        country, rest = cleaned[:2], cleaned[2:]
        return country, _strip_label(country, rest)
    if hint in COUNTRIES and cleaned:
        return hint, _strip_label(hint, cleaned)
    return "", cleaned


def check_offline(country: str, number: str) -> dict:
    """Shape and checksum.

    Returns a dict with `status` INVALID_FORMAT / INVALID_CHECKSUM / FORMAT_OK /
    UNSUPPORTED_COUNTRY, a human `reason`, `checksumImplemented`, `expected` and
    `checksumValid`. `checksumValid` is None only when no sum was computed, and
    False when the sum was computed and did not settle the question.
    """
    number = _strip_label(country, (number or "").upper())
    spec = COUNTRIES.get(country)
    if not spec:
        return {"status": "UNSUPPORTED_COUNTRY", "reason": f"{country or 'no country'} is not a known VAT country",
                "checksumImplemented": False, "expected": "", "checksumValid": None}
    low, high, pattern, check_name = spec
    if not (low <= len(number) <= high) or not re.fullmatch(pattern, number):
        return {"status": "INVALID_FORMAT",
                "reason": (f"{country} numbers are {low} characters long" if low == high
                           else f"{country} numbers are {low} to {high} characters long")
                          + f" and must match {pattern}",
                "checksumImplemented": bool(check_name), "expected": "", "checksumValid": None}
    if not check_name:
        return {"status": "FORMAT_OK", "reason": "checksum not implemented for this country, VIES decides",
                "checksumImplemented": False, "expected": "", "checksumValid": None}
    ok, detail = CHECKS[check_name](number)
    if ok == SKIPPED:
        return {"status": "FORMAT_OK", "reason": detail, "checksumImplemented": True,
                "expected": "", "checksumValid": None}
    if ok is None:
        return {"status": "FORMAT_OK", "reason": detail, "checksumImplemented": True,
                "expected": "", "checksumValid": False}
    if not ok:
        return {"status": "INVALID_CHECKSUM", "reason": f"{detail}",
                "checksumImplemented": True, "expected": detail, "checksumValid": False}
    return {"status": "FORMAT_OK", "reason": "shape and checksum pass", "checksumImplemented": True,
            "expected": "", "checksumValid": True}


def vies_code(country: str) -> str:
    return VIES_PREFIX.get(country, country)


def in_vies(country: str) -> bool:
    return vies_code(country) in VIES_COUNTRIES


def _from_text(raw: str, country_hint: str) -> tuple[str, str, bool] | None:
    """Turn one token found in free text into (country, number, explicit) or None.

    Two traps live here. A word glued to digits ("our number is 980780684")
    must not become the country "IS", and a country code written with a space
    ("DE 811 569 869") is not proof that the number is a VAT number at all, so
    it is checked but never charged.
    """
    token = (raw or "").strip()
    if not any(ch.isdigit() for ch in token):
        return None
    cleaned = re.sub(r"[\s.\-_/]", "", token.upper())
    head = cleaned[:2]
    known = (cleaned.startswith("CHE") or cleaned.startswith("EL")
             or (head.isalpha() and head in COUNTRIES))
    if not known:
        cleaned = re.sub(r"^[A-Z]+", "", cleaned)
        if len(cleaned) < 2:
            return None
        token = cleaned
    # A prefix counts as deliberately given only when it is written as one:
    # uppercase and glued to the digits. "Invoice No 259137088" and "VAT No
    # 259137088" read "No" as Norway and charged for a document number; a word
    # followed by a number is not a VAT number, and a guess is not billable.
    word = re.match(r"^([A-Za-z]{2,3})", token)
    prefix_word = word.group(1) if word else ""
    glued = (known and prefix_word.isupper()
             and bool(re.match(r"^(?:CHE|EL|[A-Z]{2})[0-9]", token)))
    country, number = split_country(cleaned, country_hint)
    if not number:
        return None
    # A lowercase prefix is used only when the number fits that country's shape:
    # "de 811 569 869" is a German number, "no 2024" is a word.
    if prefix_word and not prefix_word.isupper() and not _shape_ok(country, number):
        return None
    return country, number, glued


def rows_from_input(numbers, text: str | None, country_hint: str = "",
                    stats: dict | None = None) -> list[tuple[str, str, bool]]:
    """Collect the rows to check, keeping the order the buyer gave and dropping repeats.

    The third field says whether the number counts as deliberately given: a
    prefix glued to the digits, or a row typed into `vatNumbers`. It is False
    for a bare number pulled out of free text - an invoice number, an order id
    or a price looks exactly like one - and such a row is checked but never
    charged.
    """
    candidates: list[tuple[str, str, bool]] = []

    if isinstance(numbers, list):
        for item in numbers:
            country, number = split_country(str(item), country_hint)
            if number:
                candidates.append((country, number, True))
    elif isinstance(numbers, str):
        for line in numbers.splitlines():
            for token in re.split(r"[,;\t]", line):
                country, number = split_country(token, country_hint)
                if number:
                    candidates.append((country, number, True))

    # Read numbers that carry a prefix and spaces before the single-token pass,
    # then blank them out, so "DE 811 569 869" is one row and not three.
    left = text or ""
    for match in PREFIXED_TOKEN.finditer(left):
        parsed = _from_text(match.group(0), country_hint)
        if parsed:
            candidates.append(parsed)
        left = left.replace(match.group(0), " ", 1)
    # With a country hint, a number written with spaces ("811 569 869") is one
    # number. Without one there is nothing to tell it from three separate rows.
    if country_hint:
        spaced = list(SPACED_NUMBER.finditer(left))
        for match in reversed(spaced):
            parsed = _from_text(match.group(0), country_hint)
            if parsed and _shape_ok(parsed[0], parsed[1]):
                candidates.append((parsed[0], parsed[1], False))
                left = left[:match.start()] + " " + left[match.end():]
    for raw in re.findall(r"\b(?:[A-Za-z]{2}[\s.\-]?)?[A-Z0-9]{2,14}\b", left):
        parsed = _from_text(raw, country_hint)
        if parsed:
            candidates.append(parsed)

    # `inputRows` counts what the buyer sent, every spelling included: counting
    # unique numbers here made four spellings of one number look like one row.
    if stats is not None:
        stats["inputRows"] = len(candidates)

    rows: list[tuple[str, str, bool]] = []
    index: dict[tuple[str, str], int] = {}
    for country, number, explicit in candidates:
        key = (country, number)
        if key in index:
            if explicit:
                rows[index[key]] = (country, number, True)
            continue
        index[key] = len(rows)
        rows.append((country, number, explicit))
    return rows
