"""Pure-function tests for contact extraction. No network, no Apify SDK.

Every case below is a defect QA found in 0.1.6, reproduced as a test:
F1 a publication date billed as a phone, F2 the same site crawled three times,
F3 a company mailbox on its own subdomain filed as foreign.
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src import extract  # noqa: E402


class EmptyTree:
    """Stands in for the HTML tree: no tel: links, no mailto:, no code blocks."""

    def css(self, _selector: str):
        return []


class Node:
    def __init__(self, attributes: dict):
        self.attributes = attributes


class TelTree(EmptyTree):
    def __init__(self, href: str):
        self._href = href

    def css(self, selector: str):
        return [Node({"href": self._href})] if selector.startswith("a[href^=\"tel:") else []


def test_publication_date_is_not_a_phone():
    # The live page said 'Published 2026-11-04 19:48' and the row got phones=['+2026110419'].
    text = "Blog post. Published 2026-11-04 19:48 by the team."
    assert extract.phones_from(EmptyTree(), text, "https://blog.example.org") == []


def test_iso_date_without_time_is_not_a_phone():
    assert extract.phones_from(EmptyTree(), "Updated 2026-11-04.", "https://x.dev") == []


def test_real_international_phone_is_found():
    found = extract.phones_from(EmptyTree(), "Call us: +380 67 123 45 67 today", "https://x.dev")
    assert len(found) == 1
    assert found[0]["value"].replace(" ", "") == "+380671234567"


def test_tel_link_is_read():
    found = extract.phones_from(TelTree("tel:+420123456789"), "", "https://x.dev")
    assert [p["value"] for p in found] == ["+420123456789"]


def test_mailbox_on_own_subdomain_is_not_foreign():
    # QA case: info@mail.brand.com was filed as foreign and the site said NO_CONTACTS.
    assert extract.same_site("mail.brand.com", "brand.com") is True
    assert extract.same_site("brand.com", "brand.com") is True
    assert extract.same_site("brand-agency.com", "brand.com") is False


def test_emails_from_marks_own_subdomain_as_own():
    html = '<a href="mailto:info@mail.brand.com">write</a>'
    rows = extract.emails_from(EmptyTree(), html, "https://brand.com/contact", "brand.com")
    assert rows and all(row["foreign"] is False for row in rows)


def test_emails_from_marks_other_domain_as_foreign():
    html = '<a href="mailto:hi@agency.example">write</a>'
    rows = extract.emails_from(EmptyTree(), html, "https://brand.com", "brand.com")
    assert rows and rows[0]["foreign"] is True


def test_unique_sites_drops_repeats_three_to_one():
    # QA case: ["ok1.com","ok1.com","ok1.com"] produced three rows and three charges.
    sites, dropped = extract.unique_sites(["ok1.com", "ok1.com", "ok1.com"])
    assert sites == ["ok1.com"]
    assert dropped == 2


def test_unique_sites_sees_through_scheme_and_www():
    sites, dropped = extract.unique_sites(["apify.com", "https://apify.com", "https://www.apify.com/"])
    assert sites == ["apify.com"]
    assert dropped == 2


def test_unique_sites_keeps_different_sites():
    sites, dropped = extract.unique_sites(["a.com", "b.com"])
    assert sites == ["a.com", "b.com"]
    assert dropped == 0

def test_dmy_date_with_time_is_not_a_phone():
    # QA 0.1.8: 'Stand: 04.11.2026 19:48' gave phones=['+0411202619'].
    assert extract.phones_from(EmptyTree(), "Stand: 04.11.2026 19:48", "https://x.dev") == []


def test_short_dmy_date_is_not_a_phone():
    assert extract.phones_from(EmptyTree(), "Last update 04.11.2026.", "https://x.dev") == []


def test_coordinates_are_not_a_phone():
    # QA 0.1.8: 'Find us at 50.4501 30.5234' gave phones=['+504501305234'].
    assert extract.phones_from(EmptyTree(), "Find us at 50.4501 30.5234 here", "https://x.dev") == []


def test_site_key_is_the_host():
    assert extract.site_key("https://www.simplify.hr/contact?ref=x") == "simplify.hr"
    assert extract.site_key("simplify.hr:8080") == "simplify.hr"
    assert extract.site_key("  ") == ""


def test_unique_sites_merges_paths_of_one_host():
    # QA 0.1.8: simplify.hr and simplify.hr/contact were two crawls and two charges.
    sites, dropped = extract.unique_sites(["simplify.hr", "https://simplify.hr/contact"])
    assert sites == ["simplify.hr"]
    assert dropped == 1


def test_unique_sites_keeps_different_subdomains():
    sites, dropped = extract.unique_sites(["docs.google.com", "mail.google.com"])
    assert sites == ["docs.google.com", "mail.google.com"]
    assert dropped == 0


def test_string_input_is_split_not_iterated():
    # QA 0.1.8: a string 'apify.com' became 9 rows ['a','p','i','f','y','.','c','o','m'].
    assert extract.parse_sites("apify.com, simplify.hr\nstripe.com") == [
        "apify.com", "simplify.hr", "stripe.com"]
    assert extract.parse_sites(["a.com"]) == ["a.com"]
    assert extract.parse_sites("") == []
    assert extract.parse_sites(None) == []


def test_javascript_url_is_not_a_website():
    # QA 0.1.8: 'javascript:alert(1)' raised InvalidURL and emptied the whole run.
    assert extract.to_url("javascript:alert(1)") == ""
    assert extract.to_url("mailto:hi@x.dev") == ""
    assert extract.to_url("apify.com") == "https://apify.com"
    assert extract.to_url("http://apify.com") == "http://apify.com"
    assert extract.to_url("site.com:8080") == "https://site.com:8080"


def test_host_of_survives_junk():
    assert extract.host_of("https://apify.com/x") == "apify.com"
    assert extract.host_of("https://apify.com:8080/x") == "apify.com"
    assert extract.host_of("not a url") == ""
    assert extract.host_of("") == ""

def test_email_inside_script_payload_is_not_a_contact():
    # Live case: stripe.com ships jane.diaz@stripe.com in a translation JSON block.
    html = ('<script>{"checkout.email":"jane.diaz@stripe.com"}</script>'
            '<p>Write to sales@brand.com</p>')
    rows = extract.emails_from(EmptyTree(), html, "https://brand.com", "brand.com")
    assert [row["value"] for row in rows] == ["sales@brand.com"]


def test_jsonld_email_is_read_with_its_kind():
    row = extract.email_from_jsonld("mailto:info@brand.com", "https://brand.com", "brand.com")
    assert row and row["value"] == "info@brand.com" and row["kind"] == "jsonld"
    assert extract.email_from_jsonld("noreply@brand.com", "https://brand.com", "brand.com") is None


def test_phone_is_reported_as_written_not_invented():
    # QA 0.1.10: '8 (495) 123-45-67' became +84951234567 - Moscow turned into Vietnam.
    found = extract.phones_from(EmptyTree(), "Call 8 (495) 123-45-67 today", "https://x.dev")
    assert found and found[0]["value"] == "8 (495) 123-45-67"
    assert "e164" not in found[0]


def test_dot_separated_phone_is_found():
    # QA 0.1.10: the fraction guard ate US/UK dot phones ('Call 202.555.0123' -> []).
    found = extract.phones_from(EmptyTree(), "Call 202.555.0123 now", "https://x.dev")
    assert [p["value"] for p in found] == ["202.555.0123"]


def test_tracking_number_is_not_a_phone():
    # QA 0.1.10: 'Tracking number 1234 5678 9012' billed the site as a contact.
    text = "Tracking number 1234 5678 9012, shipped today."
    assert extract.phones_from(EmptyTree(), text, "https://x.dev") == []


def test_plus_is_kept_and_e164_only_with_plus():
    found = extract.phones_from(EmptyTree(), "Tel: +49 30 1234 5678", "https://x.dev")
    assert found and found[0]["value"] == "+49 30 1234 5678"
    assert found[0]["e164"] == "+493012345678"


def test_private_and_loopback_hosts_are_refused():
    assert extract.is_private_host("localhost") is True
    assert extract.is_private_host("127.0.0.1") is True
    assert extract.is_private_host("192.168.1.1") is True
    assert extract.is_private_host("10.0.0.5") is True
    # CGNAT 100.64.0.0/10 passes every flag test and is still not public.
    assert extract.is_private_host("100.64.0.1") is True
    assert extract.is_private_host("169.254.1.1") is True
    assert extract.is_private_host("apify.com") is False
    assert extract.is_private_host("8.8.8.8") is False


def test_broken_html_with_unclosed_scripts_is_fast():
    html = "<script>" * 2000 + "<p>write to sales@brand.com</p>"
    started = time.time()
    rows = extract.emails_from(EmptyTree(), html, "https://brand.com", "brand.com")
    assert time.time() - started < 1.0
    assert rows == []            # everything after an unclosed <script> is dropped, safely

def test_code_sample_is_not_a_phone():
    # QA 0.1.13, live run on python.org: the Fibonacci sample in <pre><code>
    # '8 13 21 34 55 89 144' became two phones and billed the site as a contact.
    text = "Fibonacci: 8 13 21 34 55 89 144 and 233 377 610 987"
    assert extract.phones_from(EmptyTree(), text, "https://python.org") == []


def test_price_and_vat_are_not_phones():
    assert extract.phones_from(EmptyTree(), "Price 1 350 000 000 EUR", "https://x.dev") == []
    assert extract.phones_from(EmptyTree(), "VAT 1234 5678 9012 registered", "https://x.dev") == []


def test_bare_number_without_a_phone_word_is_refused():
    assert extract.phones_from(EmptyTree(), "Ref 123 456 789 012 stored", "https://x.dev") == []
    assert extract.phones_from(EmptyTree(), "Phone 123 456 789 012", "https://x.dev") != []


def test_compact_international_phone_is_found():
    # QA 0.1.13: '+380956412207' with no separators was invisible to PHONE_RE.
    found = extract.phones_from(EmptyTree(), "Reach us: +380956412207", "https://x.dev")
    assert [p["value"] for p in found] == ["+380956412207"]
    assert found[0]["e164"] == "+380956412207"


def test_schema_org_telephone_is_read():
    # QA 0.1.13: a page that publishes its phone only in JSON-LD said NO_CONTACTS.
    row = extract.phone_from_jsonld("tel:+420 123 456 789", "https://brand.com")
    assert row and row["value"] == "+420 123 456 789" and row["kind"] == "jsonld"
    assert extract.phone_from_jsonld("n/a", "https://brand.com") is None


def test_words_are_not_a_website():
    # QA 0.1.13: 'hello world' went to the network and came back UNREACHABLE.
    assert extract.to_url("hello world") == ""
    assert extract.to_url("hello") == ""
    assert extract.to_url("brand.com") == "https://brand.com"


def test_two_token_international_phones_are_found():
    # QA 0.1.16, blocker B1: UK/DE/FR/NL mobiles written the normal way were invisible,
    # and a site whose only contact is a phone said NO_CONTACTS - the leader's complaint.
    cases = {
        "+44 7911 123456": "+44 7911 123456",
        "+49 171 1234567": "+49 171 1234567",
        "+33 6 12 34 56 78": "+33 6 12 34 56 78",
        "+31 6 12345678": "+31 6 12345678",
        "Telefon: 030 12345678": "030 12345678",
        "Phone: 07700 900123": "07700 900123",
    }
    for text, want in cases.items():
        found = extract.phones_from(EmptyTree(), text, "https://x.dev")
        assert [p["value"] for p in found] == [want], text


def test_slash_form_phone_is_found():
    found = extract.phones_from(EmptyTree(), "030/12345678", "https://x.dev")
    assert [p["value"] for p in found] == ["030/12345678"]


def test_contact_as_count_is_not_a_phone():
    # QA 0.1.16: 'Contact us - 1 350 000 000 customers' billed a site without contacts.
    text = "Contact us - 1 350 000 000 customers already trust us"
    assert extract.phones_from(EmptyTree(), text, "https://x.dev") == []


def test_russian_contact_label_is_a_phone_word():
    # QA 0.1.16 B5: the Russian phrase for "contact us" + a phone gave [].
    found = extract.phones_from(EmptyTree(), "\u0421\u0432\u044f\u0437\u0430\u0442\u044c\u0441\u044f \u0441 \u043d\u0430\u043c\u0438: 8 (495) 123-45-67", "https://x.dev")
    assert [p["value"] for p in found] == ["8 (495) 123-45-67"]


def test_tel_link_extension_is_not_glued_into_e164():
    # QA 0.1.16 B2: tel:+1-888-926-2289;ext=123 produced e164 '+18889262289123'.
    found = extract.phones_from(TelTree("tel:+1-888-926-2289;ext=123"), "", "https://x.dev")
    assert found and found[0]["value"] == "+1-888-926-2289"
    assert found[0]["e164"] == "+18889262289"


def test_code_samples_are_still_not_phones_after_the_loosening():
    text = "Example: +44 7911 123456 and 8 13 21 34 55 89 144"
    found = extract.phones_from(EmptyTree(), text, "https://x.dev")
    assert [p["value"] for p in found] == ["+44 7911 123456"]


def test_tax_registry_id_is_not_a_phone():
    # Live 0.1.17 run on stripe.com: the Brazilian CNPJ '22.121.209/0001-46'
    # from the footer was billed as a phone number.
    assert extract.phones_from(EmptyTree(), "CNPJ 22.121.209/0001-46", "https://stripe.com") == []
    assert extract.phones_from(EmptyTree(), "020/7946 0958", "https://x.dev") != []


def test_support_and_sales_labels_are_phone_words():
    # Live 0.1.18 check on a real page: 'Support: 07700 900123' was skipped because
    # only tel/phone/call/contact counted as a label.
    assert [p["value"] for p in extract.phones_from(EmptyTree(), "Support: 07700 900123", "https://x.dev")] == ["07700 900123"]
    assert extract.phones_from(EmptyTree(), "Support 123 456 789 012 tickets", "https://x.dev") == []


PHONES_FOUND = [
    "+44 7911 123456", "+49 171 1234567", "+33 6 12 34 56 78", "+31 6 12345678",
    "Telefon: 030 12345678", "Phone: 07700 900123", "030/12345678", "+380956412207",
    "+1 (888) 926-2289", "Tel: +7 916 123-45-67", "(044) 123-45-67", "+380 95 641 22 07",
    "Contact us: +1 415 555 0132", "Mob.: 067 123 45 67", "+49 (0) 171 1234567",
    "+44 (0)7911 123456", "Fon: 030 12345678", "Handy: 0171 1234567", "Mobil: 0171 1234567",
    "Telefonnummer: 030 12345678", "T\u00e9l : 01 23 45 67 89", "Tel\u00e9fono: 91 123 45 67",
    "Tlf: 030 12345678", "Ph: 07700 900123", "0 800 123 456", "030 12345678",
    "0049 171 1234567", "+33 (0)6 12 34 56 78", "WhatsApp: +44 7911 123456",
    "+61 2 1234 5678", "+81 3-1234-5678", "Call +1 888 926 2289 ext. 12", "Call 888-926-2289",
]

PHONES_NOISE = [
    "Published 2026-11-04 19:48", "Stand: 04.11.2026 19:48", "50.4501 30.5234",
    "8 13 21 34 55 89 144", "233 377 610 987", "1 350 000 000 customers",
    "Tracking number 1234 5678 9012", "VAT 1234 5678 9012", "Reg. no. 1234 5678 9012",
    "22.121.209/0001-46", "Invoice 2026 0001 2345", "ISBN 978 3 16 148410 0",
    "IBAN DE89 3704 0044 0532 0130 00", "Price 1 350 000 000", "Order #1234 5678 9012",
    "2026-11-04T19:48:00Z", "SKU 12 34 56 78 90", "Contact us - 1 350 000 000 customers",
    "Call us: we serve 1 350 000 000 customers", "Founded 2016 0001 2345 6789",
    "0 000 000 000", "00 000 000 000", "1234 5678 9012 3456", "2026 11 04 19 48",
    "Tel. 2026 2027 2028", "Support 1000 2000 3000", "Our office: 1999 2000 2001",
    "Phone 2026 2027 2028", "1000 2000 3000", "Support 3000 4000 5000",
]


def test_phone_polygon_finds_every_written_format():
    """QA 0.1.19: 'two-or-three token' numbers (UK/DE/FR/NL) were missed entirely.

    The list below is the polygon that caught it, kept as a permanent guard.
    """
    missed = [t for t in PHONES_FOUND
              if not extract.phones_from(EmptyTree(), t, "https://x.dev")]
    assert missed == [], missed


def test_phone_polygon_never_bills_numeric_noise():
    """The field the buyer pays for: a count, a date or an id is not a contact."""
    leaked = [(t, [p["value"] for p in extract.phones_from(EmptyTree(), t, "https://x.dev")])
              for t in PHONES_NOISE
              if extract.phones_from(EmptyTree(), t, "https://x.dev")]
    assert leaked == [], leaked


def test_row_of_years_is_not_a_phone():
    # QA 0.1.20, live check: with a phone word right before them,
    # "Tel. 2026 2027 2028" and "Support 1000 2000 3000" became phones and
    # billed a site that has no contacts at all.
    cases = ["Tel. 2026 2027 2028", "Support 1000 2000 3000",
             "Our office: 1999 2000 2001", "T\u00e9l. 2026 2027 2028"]
    leaked = {t: [p["value"] for p in extract.phones_from(EmptyTree(), t, "https://x.dev")]
              for t in cases}
    assert all(v == [] for v in leaked.values()), leaked


def test_bracketed_trunk_prefix_gives_correct_e164():
    found = extract.phones_from(EmptyTree(), "+44 (0)7911 123456", "https://x.dev")
    assert found[0]["value"] == "+44 (0)7911 123456"
    assert found[0]["e164"] == "+447911123456"

def run_all():
    import glob as _g  # noqa: F401
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print("PASS %s" % fn.__name__)
    print("%d/%d passed" % (passed, len(fns)))


if __name__ == "__main__":
    run_all()
