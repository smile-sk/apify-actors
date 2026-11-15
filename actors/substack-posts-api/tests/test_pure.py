"""Pure reader checks: what a buyer's string turns into, and what every failure is called."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import substack as s  # noqa: E402

PASSED = 0
FAILED: list = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


class Answer:
    def __init__(self, code: int, body=None, raises: Exception | None = None, headers=None):
        self.status_code = code
        self._body = body
        self._raises = raises
        self.headers = headers or {}

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._body


class Client:
    def __init__(self, answer):
        self.answer = answer
        self.urls = []

    def get(self, url, params=None, **kw):
        self.urls.append((url, params or {}))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Roads:
    """A client that answers per host, so the two roads can be played apart."""

    def __init__(self, answers):
        self.answers = answers
        self.hosts = []

    def get(self, url, params=None, **kw):
        host = url[len("https://"):].split("/")[0]
        self.hosts.append(host)
        answer = self.answers.get(host)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return Answer(404, {})
        return answer


def post(pid=1, **extra):
    base = {"id": pid, "title": "T", "subtitle": "S", "canonical_url": "https://x/p/1",
            "post_date": "2026-11-14T11:01:15.817Z", "publishedBylines": [{"name": "Ann"}],
            "section_name": "Weekend", "type": "newsletter", "audience": "everyone",
            "reaction_count": 3, "comment_count": 0, "wordcount": 1067, "language": "en",
            "cover_image": "https://img/1.jpg", "description": "D",
            "truncated_body_text": "Preview"}
    base.update(extra)
    return base


def run_all() -> int:
    # hosts
    check("host: bare slug becomes a substack host",
          s.as_host("platformer") == "platformer.substack.com")
    check("host: a url loses the scheme", s.as_host("https://www.thefp.com/") == "www.thefp.com")
    check("host: upper case is lowered", s.as_host("  Platformer.News ") == "platformer.news")
    check("host: junk is refused", s.as_host("not a host") is None)
    check("host: empty is refused", s.as_host("") is None)
    check("host: a label longer than 63 is refused", s.as_host("a" * 70 + ".com") is None)
    for literal in ("127.0.0.1", "169.254.169.254", "10.0.0.5", "http://127.0.0.1:8080/",
                    "[::1]", "0.0.0.0"):
        check(f"host: the address {literal} is not a publication", s.as_host(literal) is None)

    name, host = s.as_publication("platformer")
    check("publication: display name drops the subdomain suffix", name == "platformer")
    check("publication: host is the subdomain form", host == "platformer.substack.com")
    name2, host2 = s.as_publication("www.thefp.com")
    check("publication: a custom domain keeps its own name", name2 == "www.thefp.com")

    # rows
    row = s.post_row(post(), "thefp", False)
    check("row: id becomes a string postId", row["postId"] == "1")
    check("row: url comes from canonical_url", row["url"] == "https://x/p/1")
    check("row: authors are names, not byline objects", row["authors"] == ["Ann"])
    check("row: likes read reaction_count", row["likes"] == 3)
    check("row: wordCount read wordcount", row["wordCount"] == 1067)
    check("row: the paid row is billable", row["billable"] is True)
    check("row: the preview text is off by default", "previewText" not in row)
    check("row: the preview text appears when asked",
          s.post_row(post(), "thefp", True)["previewText"] == "Preview")
    bare = s.post_row(post(canonical_url=None, slug="my-post"), "thefp", False, "www.thefp.com")
    check("row: a post without a canonical url gets one built from the host",
          bare["url"] == "https://www.thefp.com/p/my-post")
    check("row: a bare slug is never sold as if it were a url",
          s.post_row(post(canonical_url=None, slug="my-post"), "thefp", False)["url"] is None)
    check("row: a post without an id is not sold", s.post_row({"title": "x"}, "thefp", False) is None)
    check("row: a non-dict is not sold", s.post_row("nonsense", "thefp", False) is None)
    check("row: bylines that are not dicts do not crash",
          s.post_row(post(publishedBylines=["Ann", {"name": "Bob"}]), "thefp", False)["authors"]
          == ["Bob"])

    # failures are named apart
    posts, nxt, kind, reason = s.fetch_page(Client(Answer(200, {"posts": [post()], "nextCursor": "c"})),
                                            "h.com", 50, None)
    check("ok: posts come back", len(posts) == 1 and kind == "")
    check("ok: the cursor is returned", nxt == "c")
    check("ok: the request asks for a limit", Client(Answer(200, {"posts": []})).urls == [])

    _, _, kind, reason = s.fetch_page(Client(Answer(404, {})), "h.com", 50, None)
    check("404 is a missing publication", kind == "not_found")
    check("404 reason names the host, not the network", "no publication" in reason)

    _, _, kind, reason = s.fetch_page(Client(Answer(302, {})), "h.com", 50, None)
    check("302 is a host that is not a publication", kind == "not_found")
    check("302 reason names the redirect", "redirect" in reason)

    for code in (401, 403, 429):
        _, _, kind, _ = s.fetch_page(Client(Answer(code, {})), "h.com", 50, None)
        check(f"{code} is a refusal, not a verdict", kind == "blocked")
    _, _, kind, _ = s.fetch_page(Client(Answer(500, {})), "h.com", 50, None)
    check("500 is an error", kind == "error")

    _, _, kind, reason = s.fetch_page(Client(Answer(200, None, raises=ValueError("bad json"))),
                                      "h.com", 50, None)
    check("bad json is an error", kind == "error" and "not JSON" in reason)

    _, _, kind, reason = s.fetch_page(Client(Answer(200, {"nope": 1})), "h.com", 50, None)
    check("a body without posts is an error", kind == "error" and "posts list" in reason)

    import httpx as _httpx
    _, _, kind, reason = s.fetch_page(Client(_httpx.ConnectTimeout("slow")), "h.com", 50, None)
    check("a timeout is an error with its own words", kind == "error" and "in time" in reason)
    _, _, kind, reason = s.fetch_page(Client(_httpx.ConnectError("dns")), "h.com", 50, None)
    check("an unreachable host is an error, not a missing publication",
          kind == "error" and "reached" in reason)

    # the request itself
    c = Client(Answer(200, {"posts": []}))
    s.fetch_page(c, "h.com", 500, "CUR")
    url, params = c.urls[0]
    check("request: goes to the archive endpoint", url == "https://h.com/api/v1/archive")
    check("request: the page never exceeds the 25 the archive accepts",
          params["limit"] == "25")

    # the two roads: what a copied address really is
    roads = Roads({"old.example": Answer(301, {}, headers={"location": "https://new.example"}),
                   "new.example": Answer(200, {"posts": [post()], "nextCursor": "c"})})
    used, posts, nxt, kind, reason = s.open_publication(roads, "old.example", 25)
    check("redirect: a public https redirect is followed", used == "new.example" and kind == "")
    check("redirect: the posts of the new address are sold", len(posts) == 1 and nxt == "c")

    roads = Roads({"old.example": Answer(302, {}, headers={"location": "https://127.0.0.1/x"})})
    used, posts, nxt, kind, reason = s.open_publication(roads, "old.example", 25)
    check("redirect: a redirect into an address is never followed",
          roads.hosts == ["old.example", "old.substack.com"])
    check("redirect: that road is closed and the verdict is not found", kind == "not_found")

    roads = Roads({"old.example": Answer(302, {}, headers={"location": "http://new.example/"})})
    _, _, _, kind, reason = s.open_publication(roads, "old.example", 25)
    check("redirect: a plain http redirect is not followed", kind == "not_found")
    check("redirect: new.example was never asked", "new.example" not in roads.hosts)

    roads = Roads({"www.platformer.news": Answer(301, {},
                                                headers={"location": "/api/v1/archive/?limit=25"}),
                   "platformer.substack.com": Answer(200, {"posts": [post(), post(2)]})})
    used, posts, nxt, kind, reason = s.open_publication(roads, "www.platformer.news", 25)
    check("redirect: a path-only redirect sends us to the label, not to www",
          used == "platformer.substack.com" and kind == "" and len(posts) == 2)

    roads = Roads({"noahpinion.blog": Answer(404, {}),
                   "noahpinion.substack.com": Answer(200, {"posts": [post()]})})
    used, posts, nxt, kind, reason = s.open_publication(roads, "noahpinion.blog", 25)
    check("second road: a 404 domain is tried as its substack subdomain",
          used == "noahpinion.substack.com" and kind == "" and len(posts) == 1)

    roads = Roads({"noahpinion.blog": Answer(404, {}),
                   "noahpinion.substack.com": Answer(404, {})})
    _, _, _, kind, reason = s.open_publication(roads, "noahpinion.blog", 25)
    check("second road: not found only when both roads answered 404", kind == "not_found")
    check("second road: the reason names both addresses",
          "noahpinion.blog" in reason and "noahpinion.substack.com" in reason)
    check("request: the cursor is passed through", params["cursor"] == "CUR")
    check("request: sorted newest first", params["sort"] == "new")

    print(f"{PASSED}/{PASSED + len(FAILED)} passed")
    if FAILED:
        print("FAILED:")
        for name in FAILED:
            print("  -", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
