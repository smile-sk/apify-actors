"""Reference numbers for the offline layer, wider than one per country.

A polygon of one number per country passed every check while six algorithms were
wrong: HU, SI, IE, NL, CH and GB each rejected numbers that VIES itself reports
as valid, and every rejection was billed. The lists below come from live VIES
answers and from the two independent implementations the national rules were
cross-checked against - never from our own output.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import vat  # noqa: E402

# VIES answered "valid" for these on 2026-11-05.
VIES_VALID = [
    ("HU", "12892312"),
    ("SI", "23512580"),
    ("IE", "2984579BH"),
    ("DK", "13585628"),
    ("ES", "54362315K"),
    ("ES", "X5253868R"),
    ("ES", "M1234567L"),
]

# GB is outside VIES: the HMRC rule, cross-checked against two implementations.
GB_VALID = [
    "100195075",   # only the second modular method passes
    "100190874",
    "102675046",
    "211581400",   # remainder zero, written as 00
    "235410300",
    "536768700",
    "998898400",
    "100299897",   # remainder zero, written as 97
    "980780684",
]

CH_VALID = ["100155212", "432825998"]


def test_vies_valid_numbers_are_not_rejected():
    for country, number in VIES_VALID:
        result = vat.check_offline(country, number)
        assert result["status"] == "FORMAT_OK", (country, number, result["status"], result["reason"])


def test_dutch_eleven_proof_failure_is_not_a_rejection():
    # The Dutch registry issues numbers the eleven-proof does not accept; the
    # 97-mod variant covers some of them. A failure goes to VIES, never to invalid.
    result = vat.check_offline("NL", "002455799B11")
    assert result["status"] == "FORMAT_OK", result
    assert result["checksumValid"] is False, result


def test_gb_numbers_pass_both_writings_of_remainder_zero():
    for number in GB_VALID:
        result = vat.check_offline("GB", number)
        assert result["status"] == "FORMAT_OK", (number, result["status"], result["reason"])


def test_gb_government_and_health_ranges():
    assert vat.check_offline("GB", "GD103")["status"] == "FORMAT_OK"
    assert vat.check_offline("GB", "HA501")["status"] == "FORMAT_OK"
    assert vat.check_offline("GB", "GD500")["status"] == "INVALID_CHECKSUM"
    assert vat.check_offline("GB", "HA499")["status"] == "INVALID_CHECKSUM"


def test_gb_wrong_number_still_names_the_digits():
    result = vat.check_offline("GB", "980780685001")
    assert result["status"] == "INVALID_CHECKSUM", result
    assert "84" in result["expected"], result


def test_swiss_label_is_a_word_not_digits():
    country, number = vat.split_country("CHE-432.825.998 MWST")
    assert (country, number) == ("CH", "432825998"), (country, number)
    assert vat.check_offline("CH", "432825998MWST") == vat.check_offline("CH", "432825998")
    for label in ("MWST", "TVA", "IVA"):
        assert vat.check_offline("CH", "100155212" + label)["status"] == "FORMAT_OK", label


def test_swiss_reference_numbers_pass():
    for number in CH_VALID:
        result = vat.check_offline("CH", number)
        assert result["status"] == "FORMAT_OK", (number, result["status"], result["reason"])


def test_checksum_valid_is_null_when_no_sum_was_computed():
    # True here would be a claim we did not earn; False would reject the number.
    for country, number in (("FR", "AB123456789"), ("CZ", "1234567890"), ("IE", "0A12345A")):
        result = vat.check_offline(country, number)
        assert result["status"] == "FORMAT_OK", (country, number, result)
        assert result["checksumValid"] is None, (country, number, result["checksumValid"])


def test_number_with_spaces_inside_is_one_row_not_three():
    # The prefix sits next to the digits but is not glued to them: the row is
    # parsed as one number, and per README it is checked but never billed.
    rows = vat.rows_from_input("", "VAT No: DE 811 569 869")
    assert len(rows) == 1, rows
    assert rows == [("DE", "811569869", False)], rows


def test_input_rows_counts_spellings_and_unique_counts_numbers():
    stats: dict = {}
    rows = vat.rows_from_input("DE811569869\nDE 811 569 869\nde-811-569-869", "", "", stats)
    assert len(rows) == 1, rows
    assert stats["inputRows"] == 3, stats


def run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:                                  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
