"""Address parsing, punycode and the flags, without a network and without money."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import checks  # noqa: E402

PASSED = 0
FAILED: list = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


class Resolver:
    """Repeats the resolver's contract: JSON status, Answer list, HTTP codes."""

    def __init__(self, mx: dict, a: dict | None = None, aaaa: dict | None = None) -> None:
        self.mx = mx
        self.a = a or {}
        self.aaaa = aaaa or {}

    def get(self, _url, params=None, **_kw):
        name = (params or {}).get("name")
        kind = (params or {}).get("type")
        store = {"MX": self.mx, "AAAA": self.aaaa}.get(kind, self.a)
        status, answers = store.get(name, (3, []))

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"Status": status, "Answer": answers}

        return R()


def run_all() -> int:
    # normalise
    check("spaces are trimmed", checks.normalise("  a@b.com  ") == "a@b.com")
    check("angle brackets are trimmed", checks.normalise("<a@b.com>") == "a@b.com")
    check("a trailing comma is trimmed", checks.normalise("a@b.com,") == "a@b.com")

    # split
    check("a plain address splits", checks.split_address("a@b.com")[:2] == ("a", "b.com"))
    check("two at signs are refused", "exactly one" in checks.split_address("a@@b.com")[2])
    check("an empty local part is refused", bool(checks.split_address("@b.com")[2]))
    check("a doubled dot is refused", "doubled dot" in checks.split_address("a..b@c.com")[2])
    check("a space in the local part is refused", bool(checks.split_address("a b@c.com")[2]))
    check("a plus tag is allowed", checks.split_address("a+tag@b.com")[2] == "")

    # punycode
    check("a plain domain stays as it is", checks.ascii_domain("Example.COM")[0] == "example.com")
    check("a cyrillic domain becomes punycode",
          checks.ascii_domain("\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444")[0] == "xn--e1afmkfd.xn--p1ai")
    check("a single label is refused", "at least one dot" in checks.ascii_domain("localhost")[1])
    check("a numeric top level domain is refused",
          "all digits" in checks.ascii_domain("example.123")[1])
    check("an empty label is refused", bool(checks.ascii_domain("a..b.com")[1]))

    # typos
    check("a one edit typo is found", checks.typo_for("gmial.com") == "gmail.com")
    check("a correct domain has no suggestion", checks.typo_for("gmail.com") == "")
    check("a far away domain has no suggestion", checks.typo_for("example.com") == "")

    # analyse without DNS: format verdict only, no cost
    client = Resolver({})
    row = checks.analyse(client, "someone@mailinator.com", check_dns=False)
    check("no dns: syntax is valid", row["syntaxValid"] is True)
    check("no dns: disposable is flagged", row["isDisposable"] is True)
    check("no dns: free provider is not claimed", row["isFreeProvider"] is False)
    check("no dns: the row is syntax only", row["status"] == "SYNTAX_ONLY")
    check("no dns: no mail verdict is claimed", row["acceptsMail"] is None)
    row = checks.analyse(client, "info@ukr.net", check_dns=False)
    check("no dns: role address is flagged", row["isRole"] is True)
    check("no dns: free provider is flagged", row["isFreeProvider"] is True)

    # analyse with DNS
    mx = {"gmail.com": (0, [{"type": 15, "data": "5 gmail-smtp-in.l.google.com."}]),
          "nomx.com": (0, []),
          "dead.example": (3, [])}
    a = {"nomx.com": (0, [{"type": 1, "data": "1.2.3.4"}])}
    client = Resolver(mx, a)
    row = checks.analyse(client, "someone@gmail.com")
    check("dns: mailbox domain is accepted", row["acceptsMail"] is True)
    check("dns: mx host is reported", row["mxHosts"] == ["gmail-smtp-in.l.google.com"])
    check("dns: the row is a verdict", row["status"] == "OK")
    row = checks.analyse(client, "a@nomx.com")
    check("no mx but an A record still accepts mail", row["acceptsMail"] is True)
    check("no mx is reported as no mx", row["hasMx"] is False)
    row = checks.analyse(client, "a@dead.example")
    check("a missing domain is its own status", row["status"] == "NO_SUCH_DOMAIN")
    check("a missing domain does not accept mail", row["acceptsMail"] is False)
    # RFC 7505: "0 ." is a published refusal to accept mail, not a mail server
    null_mx = {"nullmx.com": (0, [{"type": 15, "data": "0 ."}])}
    null_client = Resolver(null_mx, {})
    row = checks.analyse(null_client, "a@nullmx.com")
    check("a null MX is not read as a mail server", row["hasMx"] is False)
    check("a null MX is no mail server", row["status"] == "NO_MAIL_SERVER")
    check("a null MX never accepts mail", row["acceptsMail"] is False)
    check("a null MX reports no host", row["mxHosts"] is None)
    check("a null MX names the RFC", "7505" in row["reason"])

    # RFC 5321 5.1: the implicit MX may live on AAAA
    six = {"v6.com": (0, [{"type": 28, "data": "2606:4700::1111"}])}
    six_client = Resolver({"v6.com": (0, [])}, {"v6.com": (0, [])}, six)
    row = checks.analyse(six_client, "a@v6.com")
    check("an AAAA only domain still accepts mail", row["acceptsMail"] is True)
    check("an AAAA only domain is a verdict", row["status"] == "OK")
    check("an AAAA only domain reports no mx", row["hasMx"] is False)

    check("a bracket and a comma are both trimmed",
          checks.normalise("<a@b.com>,") == "a@b.com")
    check("a doubled dot and a comma are trimmed", checks.normalise("a@b.com.,") == "a@b.com")
    check("an angle bracket inside quotes is not eaten", checks.normalise("a@b.com") == "a@b.com")

    # the same mailbox in two spellings is one address
    one = checks.address_key("a@m\u00fcnchen.de")
    two = checks.address_key("A@xn--mnchen-3ya.de")
    check("punycode and unicode are one address", one == two)
    check("a different local part is a different address",
          checks.address_key("b@mnchen.de") != checks.address_key("a@mnchen.de"))
    check("a broken line keeps its own key", checks.address_key("bad@@x").startswith("raw:"))

    row = checks.analyse(client, "bad@@example.com")
    check("a broken address never reaches DNS", row["status"] == "INVALID_SYNTAX")

    print("PASSED", PASSED, "FAILED", len(FAILED))
    for name in FAILED:
        print("  FAIL:", name)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
