"""Polygon for the VAT checks. No network: every case here is arithmetic or parsing.

The anchor cases are numbers that VIES itself reports as valid, so a checksum
that disagrees with them is our bug, not the customer's typo.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import vat  # noqa: E402

# Numbers confirmed valid by a live VIES call on 2026-11-05.
VIES_VALID = [
    "DE811907980", "DE811569869", "DE129273398", "FR40303265045", "IT00743110157",
    "IT00159560366", "IT00488410010", "PL7740001454", "PL5261040828", "ESB58378431",
    "ESA28015865", "SE556036079301", "SE556703748501", "FI10410900", "IE6388047V",
    "PT500100144", "CZ00006947", "CZ45274649", "HR33392005961", "NL004495445B01",
    "BE0417497106", "ATU13576101", "GB980780684",
]


def test_all_vies_valid_numbers_pass_our_checks():
    for raw in VIES_VALID:
        country, number = vat.split_country(raw)
        result = vat.check_offline(country, number)
        assert result["status"] == "FORMAT_OK", f"{raw}: {result['status']} {result['reason']}"


def test_single_digit_change_is_caught():
    for raw in VIES_VALID:
        country, number = vat.split_country(raw)
        if country == "NL":
            # The Dutch eleven-proof is not satisfied by every number the registry
            # issues, so a failed proof is handed to VIES, not called invalid here.
            continue
        if number[-1].isdigit():
            mutated = number[:-1] + str((int(number[-1]) + 1) % 10)
        else:
            continue
        result = vat.check_offline(country, mutated)
        assert result["status"] == "INVALID_CHECKSUM", f"{country}{mutated}: {result['status']}"


def test_irish_check_letter_is_caught():
    country, number = vat.split_country("IE6388047V")
    assert vat.check_offline(country, number)["status"] == "FORMAT_OK"
    wrong = number[:7] + "W" + number[8:]
    result = vat.check_offline("IE", wrong)
    assert result["status"] == "INVALID_CHECKSUM", result


def test_swedish_numbers_must_end_with_01():
    result = vat.check_offline("SE", "556036079302")
    assert result["status"] == "INVALID_CHECKSUM", result
    assert "01" in result["reason"]


def test_checksum_reason_names_the_expected_digit():
    result = vat.check_offline("DE", "811569860")
    assert result["status"] == "INVALID_CHECKSUM"
    assert result["expected"] == "last digit should be 9", result


def test_alpha_key_is_not_guessed():
    # French keys can be alphanumeric; there we must not invent a verdict.
    result = vat.check_offline("FR", "ZZ303265045")
    assert result["status"] == "FORMAT_OK", result


def test_short_and_wrong_chars_are_format_errors():
    assert vat.check_offline("DE", "81156986")["status"] == "INVALID_FORMAT"
    assert vat.check_offline("DE", "81156986A")["status"] == "INVALID_FORMAT"
    assert vat.check_offline("NL", "004495445C01")["status"] == "INVALID_FORMAT"
    assert vat.check_offline("IT", "0074311015")["status"] == "INVALID_FORMAT"


def test_unknown_country_is_not_judged():
    result = vat.check_offline("XX", "123")
    assert result["status"] == "UNSUPPORTED_COUNTRY", result
    assert result["checksumImplemented"] is False


def test_countries_without_checksum_say_so():
    for code, number in [("BG", "123456789"), ("RO", "12345678"), ("SK", "1234567890"),
                         ("LT", "123456789"), ("LV", "12345678901"), ("CY", "12345678A")]:
        result = vat.check_offline(code, number)
        assert result["status"] == "FORMAT_OK", (code, result)
        assert result["checksumImplemented"] is False, (code, result)


def test_split_country_forms():
    assert vat.split_country("DE 259 137 088") == ("DE", "259137088")
    assert vat.split_country("de-259137088") == ("DE", "259137088")
    assert vat.split_country("DE.259.137.088") == ("DE", "259137088")
    assert vat.split_country("259137088", "DE") == ("DE", "259137088")
    assert vat.split_country("EL094014170") == ("GR", "094014170")
    assert vat.split_country("GR094014170") == ("GR", "094014170")
    assert vat.split_country("CHE116281277") == ("CH", "116281277")


def test_greece_asks_vies_as_el():
    assert vat.vies_code("GR") == "EL"
    assert vat.in_vies("GR") is True


def test_vies_coverage_flags():
    assert vat.in_vies("DE") is True
    assert vat.in_vies("XI") is True       # Northern Ireland is covered
    assert vat.in_vies("GB") is False      # Great Britain is not
    assert vat.in_vies("NO") is False
    assert vat.in_vies("CH") is False


def test_rows_from_list_drop_repeats_and_keep_order():
    rows = vat.rows_from_input(["DE811569869", "DE 811 569 869", "FR40303265045"], "", "")
    assert [(c, n) for c, n, _ in rows] == [("DE", "811569869"), ("FR", "40303265045")], rows


def test_rows_from_textarea_split_on_lines_and_commas():
    rows = vat.rows_from_input("DE811569869\nFR40303265045, IT00743110157", "", "")
    assert [c for c, _, _ in rows] == ["DE", "FR", "IT"], rows


def test_rows_from_document_use_the_hint():
    text = "Invoice from Acme GmbH, VAT 811569869, and from Sodimas FR40303265045."
    rows = vat.rows_from_input("", text, "DE")
    values = {"DE811569869", "FR40303265045"}
    found = {c + n for c, n, _ in rows}
    assert values <= found, rows
    # the prefix was written next to the first number and guessed from the hint for the rest
    flags = {c + n: explicit for c, n, explicit in rows}
    assert flags.get("DE811569869") is False   # 811569869 was bare, DE came from the hint
    assert flags.get("FR40303265045") is True


def test_empty_input_gives_nothing():
    assert vat.rows_from_input("", "", "") == []


def test_belgian_nine_digit_form_is_padded():
    assert vat.check_offline("BE", "417497106")["status"] == "FORMAT_OK"


def test_dutch_eleven_proof_passes_only_when_it_really_holds():
    # 004495445B01: sum of the first eight with weights 9..2 minus the ninth is
    # 132, a multiple of 11.
    good = vat.check_offline("NL", "004495445B01")
    assert good["status"] == "FORMAT_OK" and good["checksumValid"] is True, good
    # 004495455B01 fails it. NL002455799B11 is valid in VIES and fails it too, so a
    # failed proof is reported and handed over: rejecting here rejects real firms.
    weak = vat.check_offline("NL", "004495455B01")
    assert weak["status"] == "FORMAT_OK", weak
    assert weak["checksumValid"] is False, weak
    assert "VIES decides" in weak["reason"], weak


def test_hungarian_check_digit_is_ten_minus_the_sum():
    # HU12892312 is valid in VIES; the weighted sum is 98, so the digit is 2.
    good = vat.check_offline("HU", "12892312")
    assert good["status"] == "FORMAT_OK", good
    assert vat.check_offline("HU", "12892313")["status"] == "INVALID_CHECKSUM"


def test_slovenian_ten_becomes_zero():
    # SI23512580 is valid in VIES: 111 mod 11 is 1, so 11 - 1 gives ten, which is 0.
    good = vat.check_offline("SI", "23512580")
    assert good["status"] == "FORMAT_OK", good
    assert vat.check_offline("SI", "23512581")["status"] == "INVALID_CHECKSUM"


def test_irish_second_letter_carries_weight_nine():
    # IE2984579BH is valid in VIES: the H adds 9 * 8 to the sum before the mod 23.
    good = vat.check_offline("IE", "2984579BH")
    assert good["status"] == "FORMAT_OK", good
    wrong = vat.check_offline("IE", "2984579BX")
    assert wrong["status"] == "INVALID_CHECKSUM", wrong


def test_dutch_number_that_vies_calls_valid_is_not_rejected():
    # NL002455799B11 is a live VIES answer (GOEDKOPELATEXSPUITER.NL) and fails the
    # eleven-proof, which is exactly why the proof may not decide alone.
    result = vat.check_offline("NL", "002455799B11")
    assert result["status"] == "FORMAT_OK", result


def test_spanish_personal_numbers_use_the_mod_23_letter():
    # DNI, NIE and the old K/L/M forms all pass VIES and none of them is a CIF.
    for number in ("54362315K", "X5253868R", "M1234567L"):
        result = vat.check_offline("ES", number)
        assert result["status"] == "FORMAT_OK", (number, result)
    bad = vat.check_offline("ES", "54362315J")
    assert bad["status"] == "INVALID_CHECKSUM", bad


def test_gb_numbers_that_only_pass_the_second_hmrc_method():
    # These three pass HMRC's second method (check digits plus 42) and fail the first.
    for number in ("100195075", "100190874", "102675046"):
        result = vat.check_offline("GB", number)
        assert result["status"] == "FORMAT_OK", (number, result)


def test_swiss_number_arrives_without_its_che_prefix():
    country, number = vat.split_country("CHE100155212")
    assert (country, number) == ("CH", "100155212"), (country, number)
    assert vat.check_offline(country, number)["status"] == "FORMAT_OK"
    assert vat.check_offline(country, "100155213")["status"] == "INVALID_CHECKSUM"


def test_norwegian_mva_suffix_is_a_label_not_digits():
    assert vat.split_country("NO123456785MVA") == ("NO", "123456785")
    assert vat.check_offline("NO", "123456785MVA") == vat.check_offline("NO", "123456785")
    assert vat.check_offline("NO", "123456785")["status"] == "FORMAT_OK"


def test_cif_letter_control_is_checked():
    assert vat.check_offline("ES", "B58378431")["status"] == "FORMAT_OK"
    assert vat.check_offline("ES", "B58378432")["status"] == "INVALID_CHECKSUM"


def test_gb_remainder_zero_is_written_as_00_and_as_97():
    # Both writings are valid: the reference set ends in 00, older records in 97.
    # Accepting only 97 rejected roughly one real number in 97, and billed it.
    assert vat.check_offline("GB", "211581400")["status"] == "FORMAT_OK"
    assert vat.check_offline("GB", "100299897")["status"] == "FORMAT_OK"
    assert vat.check_offline("GB", "980780684")["status"] == "FORMAT_OK"


def test_gb_twelve_digit_branch_number_checks_the_first_nine():
    good = vat.check_offline("GB", "980780684001")
    assert good["status"] == "FORMAT_OK", good
    bad = vat.check_offline("GB", "980780685001")
    assert bad["status"] == "INVALID_CHECKSUM", bad


def test_danish_checksum_is_implemented():
    good = vat.check_offline("DK", "13585628")
    assert good["status"] == "FORMAT_OK", good
    assert good["checksumImplemented"] is True
    assert vat.check_offline("DK", "13585629")["status"] == "INVALID_CHECKSUM"


def test_every_declared_checksum_has_a_function():
    declared = {name for (_lo, _hi, _pat, name) in vat.COUNTRIES.values() if name}
    missing = declared - set(vat.CHECKS)
    assert not missing, f"declared but not implemented: {sorted(missing)}"
    assert not set(vat.CHECKS) - declared, "a checksum exists that no country declares"


def test_text_rows_without_a_prefix_are_marked_as_guesses():
    rows = vat.rows_from_input("", "Invoice 2024, order 259137088, VAT DE811569869", "DE")
    flags = {c + n: explicit for c, n, explicit in rows}
    assert flags.get("DE811569869") is True
    assert flags.get("DE259137088") is False


def test_a_repeat_with_a_prefix_upgrades_the_guess():
    rows = vat.rows_from_input("", "811569869 and later DE811569869", "DE")
    assert rows == [("DE", "811569869", True)], rows


def test_the_word_no_in_a_document_is_not_norway():
    """A document number is not a VAT number, and a guess is never charged."""
    for text in ("Invoice No 259137088", "VAT No 259137088", "Order No 123456785", "NO 259137088"):
        rows = vat.rows_from_input("", text)
        billed = [c + n for c, n, explicit in rows if explicit]
        assert not billed, f"{text!r} produced a billable row: {billed}"


def test_lowercase_prefix_with_spaces_is_one_free_row():
    rows = vat.rows_from_input("", "de 811 569 869")
    assert rows == [("DE", "811569869", False)], rows


def test_hint_keeps_a_spaced_number_together():
    rows = vat.rows_from_input("", "Steuer: 811 569 869", "DE")
    assert rows == [("DE", "811569869", False)], rows


def test_norwegian_remainder_one_has_no_check_digit():
    bad = vat.check_offline("NO", "100000130")
    assert bad["status"] == "INVALID_CHECKSUM", bad
    assert "10" not in bad["reason"], bad["reason"]


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
