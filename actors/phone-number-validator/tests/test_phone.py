"""Polygon for the phone analysis: formats, messy input, noise, extensions, multi-number lines.

Run: python tests/test_phone.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import phone  # noqa: E402

# One real number per country, in the international form. Nine of these countries used to
# fail on "+CC (0) <number>" (review: B1), so every one of them is pinned here.
TRUNK_ZERO_COUNTRIES = {
    "US": ("+1 202 555 0173", "+12025550173"),
    "CA": ("+1 613 555 0100", "+16135550100"),
    "RU": ("+7 495 123 45 67", "+74951234567"),
    "ES": ("+34 91 123 45 67", "+34911234567"),
    "MX": ("+52 55 1234 5678", "+525512345678"),
    "PT": ("+351 21 123 4567", "+351211234567"),
    "IT": ("+39 06 1234 5678", "+390612345678"),
    "PL": ("+48 22 123 45 67", "+48221234567"),
    "GR": ("+30 21 0123 4567", "+302101234567"),
    "GB": ("+44 20 7946 0018", "+442079460018"),
    "UA": ("+380 44 123 45 67", "+380441234567"),
    "DE": ("+49 30 901820", "+4930901820"),
    "FR": ("+33 1 42 68 53 00", "+33142685300"),
    "NL": ("+31 20 123 4567", "+31201234567"),
    "TR": ("+90 212 123 45 67", "+902121234567"),
    "IN": ("+91 11 2345 6789", "+911123456789"),
    "JP": ("+81 3 1234 5678", "+81312345678"),
    "BR": ("+55 11 91234 5678", "+5511912345678"),
    "CN": ("+86 10 1234 5678", "+861012345678"),
    "AU": ("+61 2 1234 5678", "+61212345678"),
    "ZA": ("+27 11 123 4567", "+27111234567"),
    "KR": ("+82 2 1234 5678", "+82212345678"),
    "SE": ("+46 8 123 456 78", "+46812345678"),
    "CH": ("+41 44 123 45 67", "+41441234567"),
}


def test_international_formats_are_valid():
    cases = {
        "+1 202 555 0173": ("US", "+12025550173"),
        "+44 20 7946 0018": ("GB", "+442079460018"),
        "+380 44 123 45 67": ("UA", "+380441234567"),
        "+49 30 901820": ("DE", "+4930901820"),
        "+33 1 42 68 53 00": ("FR", "+33142685300"),
        "+91 98765 43210": ("IN", "+919876543210"),
        "+81 3 1234 5678": ("JP", "+81312345678"),
        "+55 11 98765 4321": ("BR", "+5511987654321"),
        "+48 22 123 45 67": ("PL", "+48221234567"),
        "+90 212 123 45 67": ("TR", "+902121234567"),
    }
    for raw, (region, e164) in cases.items():
        row = phone.analyze_cell(raw, None)
        assert row["status"] == "VALID", (raw, row["status"], row["reason"])
        assert row["countryCode"] == region, (raw, row["countryCode"])
        assert row["e164"] == e164, (raw, row["e164"])


def test_trunk_prefix_in_brackets_for_every_country():
    """+CC (0) <number> must give exactly the same answer as +CC <number>, in all 24 countries."""
    for country, (plain, expected) in TRUNK_ZERO_COUNTRIES.items():
        reference = phone.analyze_cell(plain, None)
        assert reference["status"] == "VALID", (country, "reference is not valid", reference["reason"])

        bracketed = plain.replace(" ", " (0) ", 1)
        row = phone.analyze_cell(bracketed, None)
        assert row["status"] == "VALID", (country, bracketed, row["status"], row["reason"])
        assert row["e164"] == expected, (country, bracketed, row["e164"], expected)

        # No space before the bracket, and the "00" international prefix: same number.
        tight = plain.replace(" ", "(0)", 1)
        tight_row = phone.analyze_cell(tight, None)
        assert tight_row["status"] == "VALID", (country, tight, tight_row["reason"])
        assert tight_row["e164"] == expected, (country, tight, tight_row["e164"])


def test_trunk_prefix_does_not_eat_a_real_area_code():
    """A bracket that is not "(0)" stays part of the number."""
    row = phone.analyze_cell("+1 (202) 555-0173", None)
    assert row["status"] == "VALID"
    assert row["e164"] == "+12025550173"
    assert row["national"] == "(202) 555-0173"


def test_all_four_formats_present():
    row = phone.analyze_cell("+1 202 555 0173", None)
    assert row["e164"] == "+12025550173"
    assert row["international"] == "+1 202-555-0173"
    assert row["national"] == "(202) 555-0173"
    assert row["rfc3966"] == "tel:+1-202-555-0173"


def test_local_number_needs_default_country():
    row = phone.analyze_cell("044 123 45 67", None)
    assert row["status"] == "INVALID"
    assert "Default country" in row["reason"]
    with_country = phone.analyze_cell("044 123 45 67", "UA")
    assert with_country["status"] == "VALID"
    assert with_country["e164"] == "+380441234567"


def test_local_number_with_label_and_trunk_prefix():
    row = phone.analyze_cell("Tel.: (044) 123-45-67", "UA")
    assert row["status"] == "VALID", row["reason"]


def test_zero_zero_international_prefix():
    row = phone.analyze_cell("0044 20 7946 0018", None)
    assert row["status"] == "VALID"
    assert row["e164"] == "+442079460018"


def test_zero_zero_with_trunk_bracket():
    row = phone.analyze_cell("0034 (0) 91 123 45 67", None)
    assert row["status"] == "VALID", row["reason"]
    assert row["e164"] == "+34911234567"


def test_tel_uri():
    row = phone.analyze_cell("tel:+380441234567", None)
    assert row["status"] == "VALID"
    assert row["e164"] == "+380441234567"


def test_full_width_digits():
    row = phone.analyze_cell("\uff0b\uff13\uff18\uff10 \uff14\uff14 \uff11\uff12\uff13 \uff14\uff15 \uff16\uff17", None)
    assert row["status"] == "VALID", row["reason"]
    assert row["e164"] == "+380441234567"


def test_arabic_indic_digits():
    row = phone.analyze_cell("\u0660\u0664\u0664 \u0661\u0662\u0663 \u0664\u0665 \u0666\u0667", "UA")
    assert row["status"] == "VALID", row["reason"]
    assert row["e164"] == "+380441234567"


def test_extensions_all_forms():
    for raw, expected in [
        ("+1 202 555 0173 ext. 42", "42"),
        ("+1 202 555 0173 extension 42", "42"),
        ("+1 202 555 0173 x42", "42"),
        ("+1 202 555 0173 #42", "42"),
        ("+380 44 123 45 67 dob. 5", "5"),
    ]:
        row = phone.analyze_cell(raw, None)
        assert row["status"] == "VALID", (raw, row["reason"])
        assert row["extension"] == expected, (raw, row["extension"])
        assert "ext=" + expected in row["rfc3966"], row["rfc3966"]


def test_trunk_bracket_with_extension():
    row = phone.analyze_cell("+34 (0) 91 123 45 67 ext. 12", None)
    assert row["status"] == "VALID", row["reason"]
    assert row["e164"] == "+34911234567"
    assert row["extension"] == "12"


def test_multiple_numbers_in_one_line():
    row = phone.analyze_cell("Call +1 202 555 0173 or +44 20 7946 0018 for the office", None)
    assert row["status"] == "VALID"
    assert row["e164"] == "+12025550173"
    assert [other["e164"] for other in row["otherNumbers"]] == ["+442079460018"]


def test_sms_capable_only_for_mobile():
    mobile = phone.analyze_cell("+380 95 641 22 07", None)
    landline = phone.analyze_cell("+380 44 123 45 67", None)
    toll_free = phone.analyze_cell("+1 800 555 0199", None)
    assert mobile["type"] == "mobile" and mobile["smsCapable"] is True
    assert landline["type"] == "fixed_line" and landline["smsCapable"] is False
    assert toll_free["type"] == "toll_free" and toll_free["smsCapable"] is False


def test_possible_but_not_assigned():
    row = phone.analyze_cell("+81 3 0000 0000", None)
    assert row["status"] == "POSSIBLE_ONLY", row["status"]
    assert "Right length" in row["reason"]
    assert row["valid"] is False


def test_unknown_country_code_has_its_own_reason():
    row = phone.analyze_cell("+999 123456789", None)
    assert row["status"] == "INVALID"
    assert "Country code is not recognised" in row["reason"], row["reason"]


def test_recognised_country_code_never_claims_it_is_unknown():
    """B1: a real calling code with digits that do not fit must not be called unrecognised."""
    cases = ["+7 000 000 00 00", "+1 000 000 0000", "+34 00 000 00 00", "+1 202 555 017"]
    for raw in cases:
        row = phone.analyze_cell(raw, None)
        assert row["status"] in ("INVALID", "POSSIBLE_ONLY"), (raw, row["status"])
        assert "is not recognised" not in row["reason"], (raw, row["reason"])
        if row["status"] == "INVALID":
            assert "real calling code" in row["reason"], (raw, row["reason"])
    # Leading zeros are digits too: the reason must not call a 10-digit number "1 digits".
    zero_row = phone.analyze_cell("+1 000 000 0000", None)
    assert "10 digits" in zero_row["reason"], zero_row["reason"]
    short_row = phone.analyze_cell("+1 202 555 017", None)
    assert short_row["status"] == "INVALID", short_row["status"]
    assert "9 digits" in short_row["reason"], short_row["reason"]


def test_noise_never_becomes_valid():
    noise = [
        "2026-11-06", "10:30", "Total: $1,299.00", "IBAN DE89370400440532013000",
        "192.168.1.1", "VAT DE811569869", "Order 987654321012345", "99.95%",
        "1 2 3 5 8 13 21 34", "version 4.2.0", "2026-11-06T12:45:00Z",
        "\u043a\u043e\u0434 1234", "-", "", "n/a",
    ]
    for cell in noise:
        row = phone.analyze_cell(cell, None)
        assert row["status"] != "VALID", (cell, row["status"], row["e164"])
        assert row["e164"] == "", (cell, row["e164"])


def test_date_with_default_country_is_invalid_not_valid():
    row = phone.analyze_cell("2026-11-06", "UA")
    assert row["status"] == "INVALID", row["status"]
    assert row["valid"] is False


def test_words_give_no_number():
    for cell in ["call me maybe", "hello world", "+"]:
        row = phone.analyze_cell(cell, None)
        assert row["status"] == "NO_NUMBER", (cell, row["status"])


def test_row_echoes_input_exactly():
    given = "  Phone: +1 (202) 555-0173 ext. 42  "
    row = phone.analyze_cell(given, None)
    assert row["inputAsGiven"] == given
    assert row["status"] == "VALID"


def test_invalid_row_is_free_by_default():
    row = phone.analyze_cell("junk line", None)
    assert row["billable"] is False
    # money detail is not a row field: what was actually charged lives in the run summary
    assert "chargedEvents" not in row and "chargedNumbers" not in row


def test_default_country_validation():
    assert phone.resolve_default_region("") is None
    assert phone.resolve_default_region("auto") is None
    assert phone.resolve_default_region("ua") == "UA"
    try:
        phone.resolve_default_region("Ukraine")
    except ValueError as exc:
        assert "not an ISO-2" in str(exc)
    else:
        raise AssertionError("a word instead of ISO-2 must be an error")


def test_lines_from_input_order_and_fallback():
    assert phone.lines_from_input(["+1 202 555 0173", "+44 20 7946 0018"], "ignored") == [
        "+1 202 555 0173", "+44 20 7946 0018"]
    # A blank cell inside the list is kept (it becomes a NO_NUMBER row); the edges are trimmed.
    assert phone.lines_from_input([], "a\n\nb\r\nc") == ["a", "", "b", "c"]
    assert phone.lines_from_input([], "\nout\ntrailing\n\n") == ["out", "trailing"]
    assert phone.lines_from_input(["+1", "", "+44"], "") == ["+1", "", "+44"]
    assert phone.lines_from_input("+1\n+2", "") == ["+1", "+2"]
    assert phone.lines_from_input(["+1\n+44"], "") == ["+1", "+44"]
    assert phone.lines_from_input(None, None) == []
    assert phone.lines_from_input([], "   \n  ") == []


def test_blank_cell_keeps_line_numbers():
    cells = ["+1 202 555 0173", "", "+44 20 7946 0018"]
    first = phone.analyze_cell(cells[0], None)
    blank = phone.analyze_cell(cells[1], None)
    last = phone.analyze_cell(cells[2], None)
    assert first["status"] == "VALID"
    assert blank["status"] == "NO_NUMBER"
    assert last["status"] == "VALID"


def test_carrier_is_labelled_metadata_not_live():
    row = phone.analyze_cell("+380 95 641 22 07", None)
    assert row["timeZones"] == ["Europe/Kyiv"]
    assert isinstance(row["carrier"], str)


def run_all():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %s" % (test.__name__, exc))
        except Exception as exc:                                 # noqa: BLE001
            failed += 1
            print("ERROR %s: %s: %s" % (test.__name__, type(exc).__name__, exc))
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
