"""Every rule that decides money or a 'valid' answer is a test here."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import iban  # noqa: E402

VALID = [
    "DE89370400440532013000",
    "GB82WEST12345698765432",
    "FR1420041010050500013M02606",
    "NL91ABNA0417164300",
    "ES9121000418450200051332",
    "IT60X0542811101000000123456",
    "PL61109010140000071219812874",
    "BE68539007547034",
    "AT611904300234573201",
    "UA213223130000026007233566001",
]


def test_valid_ibans_are_valid():
    for value in VALID:
        row = iban.check(value)
        assert row["valid"] is True, (value, row["status"], row["reason"])
        assert row["status"] == "VALID"
        assert row["checkable"] is True


def test_country_and_sepa_are_named():
    assert iban.check("DE89370400440532013000")["country"] == "Germany"
    assert iban.check("DE89370400440532013000")["sepa"] is True
    assert iban.check("UA213223130000026007233566001")["country"] == "Ukraine"
    assert iban.check("UA213223130000026007233566001")["sepa"] is False


def test_spaces_and_dashes_are_ignored():
    row = iban.check("DE89 3704 0044 0532 0130 00")
    assert row["valid"] is True
    assert row["iban"] == "DE89370400440532013000"
    assert row["printed"] == "DE89 3704 0044 0532 0130 00"
    assert iban.check("de89-3704-0044-0532-0130-00")["valid"] is True


def test_wrong_check_digits_are_caught_and_explained():
    row = iban.check("DE89370400440532013001")
    assert row["valid"] is False
    assert row["status"] == "BAD_CHECKSUM"
    # the digits are computed for THIS body, so they are not the "89" of the valid twin
    assert len(row["expectedCheckDigits"]) == 2 and row["expectedCheckDigits"].isdigit()
    assert row["expectedCheckDigits"] != "89"
    assert row["expectedCheckDigits"] in row["reason"]
    assert "mod-97" in row["reason"]
    assert iban.check("DE89370400440532013000")["expectedCheckDigits"] is None


def test_made_up_number_with_right_shape_is_rejected():
    # Right length, right characters, wrong arithmetic: the whole point of the Actor.
    row = iban.check("DE00370400440532013000")
    assert row["valid"] is False
    assert row["status"] == "BAD_CHECKSUM"
    assert row["checkable"] is True


def test_wrong_length_is_named_with_the_expected_one():
    row = iban.check("DE8937040044053201300")
    assert row["status"] == "BAD_LENGTH"
    assert row["expectedLength"] == 22
    assert row["actualLength"] == 21
    assert row["checkable"] is False


def test_unknown_country_is_not_guessed():
    row = iban.check("ZZ89370400440532013000")
    assert row["status"] == "UNKNOWN_COUNTRY"
    assert row["checkable"] is False
    assert row["country"] == ""


def test_symbols_are_bad_characters():
    row = iban.check("DE89#3704#0044#0532#0130#00")
    assert row["status"] == "BAD_CHARACTERS"
    assert row["valid"] is False


def test_letter_o_instead_of_zero_fails_the_checksum():
    # A letter inside a BBAN is legal shape, so only arithmetic can catch the typo.
    row = iban.check("DE89 3704 0044 0532 0130 0O")
    assert row["status"] == "BAD_CHECKSUM"
    assert row["valid"] is False


def test_empty_line_is_reported_not_dropped():
    row = iban.check("   ")
    assert row["status"] == "EMPTY"
    assert row["checkable"] is False


def test_non_iban_text_is_rejected_not_crashed():
    for value in ["hello world", "1234567890", "!!!" .strip(), "https://example.com"]:
        row = iban.check(value)
        assert row["valid"] is False


def test_extract_from_document():
    text = ("Please settle the invoice to DE89 3704 0044 0532 0130 00 or, "
            "for UK accounts, GB82WEST12345698765432. Thanks!")
    found = iban.extract(text)
    assert found == ["DE89370400440532013000", "GB82WEST12345698765432"]


def test_extract_drops_duplicates_and_keeps_order():
    text = "GB82WEST12345698765432 then DE89370400440532013000 then GB82 WEST 1234 5698 7654 32"
    assert iban.extract(text) == ["GB82WEST12345698765432", "DE89370400440532013000"]


def test_extract_ignores_short_tokens():
    assert iban.extract("DE89 3704") == []


def test_bic_country_mismatch_is_caught():
    row = iban.check("DE89370400440532013000", "DEUTGB2L")
    assert row["valid"] is False
    assert row["status"] == "BAD_BIC"
    assert row["bicValid"] is False
    assert "GB" in row["reason"]


def test_bic_matching_country_passes_and_is_not_overclaimed():
    row = iban.check("DE89370400440532013000", "DEUTDEFF")
    assert row["valid"] is True
    assert row["bicValid"] is True


def test_bic_of_wrong_shape_is_rejected():
    row = iban.check("DE89370400440532013000", "DEUTDE")
    assert row["bicValid"] is False
    assert row["valid"] is False


def test_no_bic_means_no_claim():
    row = iban.check("DE89370400440532013000")
    assert row["bicValid"] is None
    assert row["valid"] is True


def test_input_forms():
    pairs, bic_count = iban.rows_from_input("DE89370400440532013000\nGB82WEST12345698765432", "")
    assert len(pairs) == 2 and bic_count == 0
    pairs, bic_count = iban.rows_from_input(["DE89370400440532013000"], "DEUTDEFF")
    assert pairs == [("DE89370400440532013000", "DEUTDEFF")] and bic_count == 1
    pairs, bic_count = iban.rows_from_input("DE89370400440532013000;GB82WEST12345698765432", "")
    assert len(pairs) == 2
    pairs, _ = iban.rows_from_input(None, None)
    assert pairs == []


def test_mod97_matches_the_reference_example():
    # ISO 13616 worked example: DE89 3704 0044 0532 0130 00 -> remainder 1
    assert iban.mod97("DE89370400440532013000") == 1
    assert iban.mod97("DE89370400440532013001") != 1


def test_large_input_is_linear_enough():
    import time
    start = time.time()
    for _ in range(2000):
        iban.check("DE89370400440532013000")
    assert time.time() - start < 5.0


def test_extract_does_not_swallow_the_next_word():
    found = iban.extract("pay to DE89 3704 0044 0532 0130 00 or use another account")
    assert found == ["DE89370400440532013000"]
