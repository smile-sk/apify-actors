"""Pure checks: input parsing, the page picker and the pageviews window.

No network here: everything is decided by the arguments given to the functions.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import wiki  # noqa: E402

PASSED = 0
FAILED: list = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


def run_all() -> int:
    # 1. a bare title
    a, why = wiki.parse_request("Python (programming language)")
    check("title: parsed", a is not None and why == "")
    check("title: kept as is", a is not None and a.title == "Python (programming language)")
    check("title: default language", a is not None and a.lang == "en")
    check("title: url built", a is not None
          and a.url == "https://en.wikipedia.org/wiki/Python_(programming_language)")

    # 2. underscores become spaces
    a, _ = wiki.parse_request("Python_(programming_language)")
    check("underscores: turned into spaces", a is not None and a.title == "Python (programming language)")

    # 3. a full URL carries its own language
    a, _ = wiki.parse_request("https://de.wikipedia.org/wiki/Berlin")
    check("url: language from the host", a is not None and a.lang == "de")
    check("url: title from the path", a is not None and a.title == "Berlin")

    # 4. a prefixed line
    a, _ = wiki.parse_request("fr:Paris")
    check("prefix: language taken", a is not None and a.lang == "fr")
    check("prefix: title taken", a is not None and a.title == "Paris")

    # 5. fragments and query strings are dropped
    a, _ = wiki.parse_request("https://en.wikipedia.org/wiki/Berlin#History?x=1")
    check("url: fragment dropped", a is not None and a.title == "Berlin")

    # 6. a percent encoded URL is decoded
    a, _ = wiki.parse_request("https://ru.wikipedia.org/wiki/%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0")
    check("url: percent decoding", a is not None and a.title == "\u041c\u043e\u0441\u043a\u0432\u0430")

    # 7. junk is refused with a reason, never silently
    for junk, needle in [("", "empty"), ("   ", "empty"),
                         ("https://example.com/wiki/Berlin", "not a Wikipedia project"),
                         ("https://en.wikipedia.org/w/index.php?title=Berlin", "not an article"),
                         ("https://en.wikipedia.org/wiki/", "wiki root")]:
        a, why = wiki.parse_request(junk)
        check(f"junk {junk!r}: refused", a is None and needle in why)

    # 8. an impossible default language is refused, and a real title with a colon is not
    a, why = wiki.parse_request("Berlin", default_lang="zzz9")
    check("language: nonsense refused", a is None and "language code" in why)
    a, _ = wiki.parse_request("Star Trek: The Next Generation")
    check("colon in a real title is not a language", a is not None and a.lang == "en")

    # 9. control characters are refused
    a, why = wiki.parse_request("Berlin\u0007")
    check("control characters: refused", a is None and "control" in why)

    # 10. the page picker reads formatversion 2 and formatversion 1
    page = wiki._page_of({"query": {"pages": [{"pageid": 1, "title": "A"}]}})
    check("page picker: a list", page is not None and page["pageid"] == 1)
    page = wiki._page_of({"query": {"pages": {"1": {"pageid": 1, "title": "A"}}}})
    check("page picker: a dict", page is not None and page["pageid"] == 1)
    check("page picker: an empty answer", wiki._page_of({}) is None)

    # 11. the pageviews window is bounded and ordered
    start, end = wiki._month_window(30)
    check("window: eight digits", len(start) == 8 and len(end) == 8)
    check("window: start before end", start < end)
    check("window: one day still works", wiki._month_window(1)[0] <= wiki._month_window(1)[1])

    # 12. a real summary payload becomes a row, and a billable one
    class FakeResp:
        status_code = 200
        text = "{}"

    class FakeClient:
        def __init__(self, payload):
            self.payload = payload

        def get(self, url, params=None, headers=None):
            import json as _json
            resp = FakeResp()
            resp.status_code = 200
            resp.text = _json.dumps(self.payload)
            return resp

    payload = {"query": {"pages": [{
        "pageid": 23862, "title": "Python (programming language)", "pagelanguage": "en",
        "length": 145760, "pageprops": {"wikibase_item": "Q28865",
                                        "wikibase-shortdesc": "General-purpose programming language"},
        "revisions": [{"timestamp": "2026-11-10T13:32:08Z", "user": "Someone"}],
        "extract": "Python is a high-level programming language.",
        "categories": [{"title": "Category:Programming languages"}]}]}}
    article, _ = wiki.parse_request("Python (programming language)")
    row, kind, reason = wiki.read_article(FakeClient(payload), article,
                                          include_pageviews=False, include_categories=True)
    check("row: built", row is not None and kind == "" and reason == "")
    check("row: billable", row is not None and row.get("billable") is True)
    check("row: wikidata id", row is not None and row.get("wikidataId") == "Q28865")
    check("row: description from the page props", row is not None
          and row.get("description") == "General-purpose programming language")
    check("row: word count", row is not None and row.get("wordCount") == 6)
    check("row: categories", row is not None and row.get("categoryCount") == 1)
    check("row: pageviews not requested is said out loud", row is not None
          and "not requested" in str(row.get("pageviewsNote")))

    # 13. a missing page is a verdict, not an error
    missing = {"query": {"pages": [{"title": "Nope", "missing": True}]}}
    row, kind, reason = wiki.read_article(FakeClient(missing), article,
                                          include_pageviews=False)
    check("missing: no row", row is None)
    check("missing: named as not found", kind == "not_found" and "no article" in reason)

    # 14. a source that answers an empty body is an error, not a verdict
    class Empty(FakeClient):
        def get(self, url, params=None, headers=None):
            resp = FakeResp()
            resp.status_code = 200
            resp.text = ""
            return resp

    row, kind, reason = wiki.read_article(Empty({}), article, include_pageviews=False)
    check("empty body: an error", row is None and kind == "error" and "empty body" in reason)

    # 15. a 500 from the source is an error too
    class Failing(FakeClient):
        def get(self, url, params=None, headers=None):
            resp = FakeResp()
            resp.status_code = 503
            resp.text = "down"
            return resp

    row, kind, reason = wiki.read_article(Failing({}), article, include_pageviews=False)
    check("http 503: an error", row is None and kind == "error" and "503" in reason)

    # 16. pageviews are summed and the note is empty on success
    class PvClient(FakeClient):
        def get(self, url, params=None, headers=None):
            import json as _json
            resp = FakeResp()
            resp.status_code = 200
            resp.text = _json.dumps({"items": [
                {"timestamp": "2026110100", "views": 10},
                {"timestamp": "2026110200", "views": 32}]})
            return resp

    total, daily, note = wiki.read_pageviews(PvClient({}), "en", "Berlin", 2)
    check("pageviews: summed", total == 42)
    check("pageviews: daily series", isinstance(daily, list) and len(daily) == 2)
    check("pageviews: no note", note == "")

    # 17. an unreadable pageviews series is a note, not a crash
    total, daily, note = wiki.read_pageviews(Failing({}), "en", "Berlin", 2)
    check("pageviews: failure is a note", total is None and "503" in note)

    # 18. a Wikidata id that is not a Q number is refused without a request
    links, claims, note = wiki.read_wikidata(Failing({}), "not-a-qid")
    check("wikidata: nonsense refused", links is None and "Q-number" in note)

    # 19. includeFullText really asks for the whole article
    class LeadClient:
        """Repeats MediaWiki: exintro present means the lead section, absent means all."""

        def __init__(self):
            self.seen = []

        def get(self, url, params=None, headers=None, **_kw):
            self.seen.append(dict(params or {}))
            body = ("lead text" if "exintro" in (params or {})
                    else "lead text " + "body " * 200)
            payload = {"query": {"pages": [{
                "pageid": 7, "ns": 0, "title": "T", "pagelanguage": "en",
                "extract": body}]}}
            resp = FakeResp()
            resp.status_code = 200
            import json as _json
            resp.text = _json.dumps(payload)
            return resp

    lead_client, full_client = LeadClient(), LeadClient()
    lead_row, _, _ = wiki.read_article(lead_client, article, include_pageviews=False)
    full_row, _, _ = wiki.read_article(full_client, article, include_text=True,
                                       include_pageviews=False)
    check("full text: the lead request carries exintro", "exintro" in lead_client.seen[0])
    check("full text: the whole-article request does not", "exintro" not in full_client.seen[0])
    check("full text: the whole article is longer than the lead",
          lead_row is not None and full_row is not None
          and len(full_row["text"]) > len(lead_row["text"]))
    check("full text: the word count follows the text",
          full_row is not None and full_row["wordCount"] > lead_row["wordCount"])

    # 20. categories are asked for in full, and a cut is said out loud
    params_seen = lead_client.seen[0]
    check("categories: asked with no cap", params_seen.get("cllimit") == "max")
    cut = {"query": {"pages": [{"pageid": 8, "ns": 0, "title": "T",
                                "categories": [{"title": "Category:A"}]}]},
           "continue": {"clcontinue": "page|9", "continue": "||"}}
    cut_row, _, _ = wiki.read_article(FakeClient(cut), article, include_pageviews=False)
    check("categories: a cut list is flagged", cut_row is not None
          and cut_row.get("categoriesTruncated") is True)

    # 21. a special page and a foreign namespace never become a paid row
    special = {"query": {"pages": [{"ns": -1, "special": True, "title": "Special:Random"}]}}
    row, kind, reason = wiki.read_article(FakeClient(special), article, include_pageviews=False)
    check("special page: no row", row is None and kind == "not_found")
    check("special page: the reason is plain", "special page" in reason)
    other_ns = {"query": {"pages": [{"pageid": 9, "ns": 14, "title": "Category:Physics"}]}}
    row, kind, reason = wiki.read_article(FakeClient(other_ns), article, include_pageviews=False)
    check("other namespace: no row", row is None and kind == "not_found")
    check("other namespace: the number is named", "namespace 14" in reason)

    # 22. an API error block is a source error, not a missing page
    api_error = {"error": {"code": "badvalue", "info": "Unrecognized value for parameter"}}
    row, kind, reason = wiki.read_article(FakeClient(api_error), article, include_pageviews=False)
    check("api error: an error", row is None and kind == "error")
    check("api error: the source wording is kept", "Unrecognized value" in reason)

    # 23. the word count counts CJK characters, not spaces
    check("words: a latin sentence", wiki.count_words("Python is a language") == 4)
    check("words: a chinese paragraph", wiki.count_words("\u4e2d\u6587\u5b57\u7b26\u6d4b\u8bd5") == 6)
    check("words: a mixed token", wiki.count_words("abc\u4e2d\u6587") == 3)
    check("words: nothing", wiki.count_words("") == 0)

    print(f"pure: {PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print("  FAIL:", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
