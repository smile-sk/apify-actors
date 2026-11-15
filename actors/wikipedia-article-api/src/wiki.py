"""Reading Wikipedia and Wikidata through their own public APIs.

No key, no browser, no proxy: the Wikimedia REST API, the MediaWiki Action API,
the pageviews metric API and the Wikidata EntityData endpoint all answer a plain
HTTP client. That is the whole source of this actor, so a failure here is a real
failure of the source, never a wall.

The reader returns either a finished row or a (kind, reason) pair, so the money
layer never has to guess what happened: it knows whether the article was read.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

USER_AGENT = "stas-k-apify-actor/1.0 (https://apify.com/stas_k; contact: krisanov.stas@gmail.com)"
TIMEOUT = 25.0


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
            or 0xAC00 <= code <= 0xD7AF)


def count_words(text: str) -> int:
    """Words in a text, counting one word per CJK character.

    A plain space-separated count calls a 1000-character Chinese paragraph one word and
    understates the product by two orders of magnitude, so every character of a CJK
    script counts as a word of its own and only the rest is counted by spaces.
    """
    if not text:
        return 0
    total = 0
    for token in text.replace("\u3000", " ").split():
        cjk = sum(1 for char in token if _is_cjk(char))
        total += cjk + (1 if cjk < len(token) else 0)
    return total

# Input forms we accept: a bare title, a full article URL, or "lang:Title".
_LANG_PREFIX = re.compile(r"^([a-z]{2,3}(?:-[a-z0-9]{2,8})?):(.+)$")
_BAD_TITLE = re.compile(r"[\x00-\x1f]")
MAX_TITLE = 255


class Article:
    """What one article request resolved to before any network call."""

    __slots__ = ("lang", "title", "url")

    def __init__(self, lang: str, title: str, url: str) -> None:
        self.lang = lang
        self.title = title
        self.url = url


def parse_request(entry: str, default_lang: str = "en") -> tuple[Article | None, str]:
    """Turn one input line into (lang, title) or a plain reason it is not usable."""
    text = (entry or "").strip()
    if not text:
        return None, ("an empty line is not an article: send a title such as "
                      "\"Python (programming language)\" or a full article URL")
    if len(text) > MAX_TITLE * 3:
        return None, "the line is longer than any Wikipedia title or URL can be"
    if _BAD_TITLE.search(text):
        return None, "the line contains control characters"

    lang = default_lang
    body = text

    if "://" in text or text.lower().startswith("www."):
        parsed = urlparse(text if "://" in text else "https://" + text)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host.startswith("m."):
            host = host[2:]
        if not host.endswith(".wikipedia.org") and host != "wikipedia.org":
            return None, ("the host is not a Wikipedia project: send "
                          "https://<lang>.wikipedia.org/wiki/<title>")
        maybe_lang = host[: -len(".wikipedia.org")] if host.endswith(".wikipedia.org") else ""
        if maybe_lang:
            lang = maybe_lang
        path = parsed.path or ""
        if "/wiki/" not in path:
            return None, ("the URL is not an article: the path must contain /wiki/<title>")
        body = unquote(path.split("/wiki/", 1)[1].split("#", 1)[0].split("?", 1)[0])
        if not body.strip():
            return None, "the URL points at the wiki root, not at an article"
    else:
        prefix = _LANG_PREFIX.match(text)
        if prefix and len(prefix.group(2)) > 2:
            lang = prefix.group(1)
            body = prefix.group(2)

    title = body.replace("_", " ").strip()
    if not title:
        return None, "the line names no article"
    if len(title) > MAX_TITLE:
        return None, f"the title is longer than {MAX_TITLE} characters"
    lang = (lang or "en").strip().lower()
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", lang):
        return None, f"\"{lang}\" is not a Wikipedia language code such as en or de"
    url = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='()_,-./!~:*')}"
    return Article(lang, title, url), ""


def _api_base(lang: str) -> str:
    return f"https://{lang}.wikipedia.org/w/api.php"


def _json(resp):
    """Decode a response, or raise ValueError with a short reason."""
    text = resp.text
    if not text.strip():
        raise ValueError("the source answered an empty body")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the source answered something that is not JSON ({exc.msg})") from exc


def _page_of(payload: dict) -> dict | None:
    pages = ((payload.get("query") or {}).get("pages")) or {}
    if isinstance(pages, dict):
        for _pid, page in pages.items():
            return page if isinstance(page, dict) else None
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, dict):
                return page
    return None


def _month_window(days: int) -> tuple[str, str]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(1, days))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def read_article(
    client,
    article: Article,
    *,
    include_text: bool = False,
    include_pageviews: bool = True,
    include_categories: bool = True,
    pageviews_days: int = 30,
    timeout: float | None = None,
) -> tuple[dict | None, str, str]:
    """Read one article. Returns (row, kind, reason); row is None when kind is set.

    kind is "not_found" when Wikipedia says the page does not exist, and "error"
    when the source could not be read at all. The two are never merged: a missing
    article is a verdict, an unreadable source is not.
    """
    wanted = ["info", "pageprops", "revisions", "extracts"]
    if include_categories:
        wanted.append("categories")
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
        "prop": "|".join(wanted),
        "titles": article.title,
        "rvprop": "timestamp|user",
        "rvlimit": "1",
        "explaintext": "1",
        "cllimit": "max",
        "clshow": "!hidden",
    }
    # MediaWiki reads the PRESENCE of exintro as "the lead section only": exintro=0 does not
    # switch it off, only an absent parameter does. Sending it always meant that
    # includeFullText returned the same lead section as with the option off.
    if not include_text:
        params["exintro"] = "1"
    request: dict = {"headers": {"User-Agent": USER_AGENT}}
    if timeout is not None:
        request["timeout"] = timeout
    try:
        resp = client.get(_api_base(article.lang), params=params, **request)
    except Exception as exc:  # noqa: BLE001
        return None, "error", f"the request to Wikipedia failed ({type(exc).__name__})"
    if resp.status_code == 404:
        return None, "not_found", "Wikipedia has no article at this title"
    if resp.status_code != 200:
        return None, "error", f"Wikipedia answered HTTP {resp.status_code}"
    try:
        payload = _json(resp)
    except ValueError as exc:
        return None, "error", str(exc)

    api_error = payload.get("error")
    if isinstance(api_error, dict) and api_error.get("info"):
        return None, "error", f"Wikipedia refused the query: {api_error['info']}"
    page = _page_of(payload)
    if page is None:
        return None, "error", "Wikipedia answered without a page object"
    if page.get("missing"):
        return None, "not_found", "Wikipedia has no article at this title"
    if page.get("invalidreason"):
        return None, "not_found", f"Wikipedia rejected the title: {page['invalidreason']}"
    # Special:Random and Special:Search answer ns=-1 with no page id: Wikipedia serves a
    # page, but there is no article behind it, so such a line must never become a paid row.
    if page.get("special") or page.get("pageid") is None:
        return None, "not_found", ("the title resolves to a special page or to a page "
                                   "without an id, not to an article")
    try:
        namespace = int(page.get("ns"))
    except (TypeError, ValueError):
        namespace = 0
    if namespace != 0:
        return None, "not_found", (f"the title lives in namespace {namespace}, not in the "
                                   "article namespace")

    title = page.get("title") or article.title
    lang = page.get("pagelanguage") or article.lang
    props = page.get("pageprops") or {}
    revisions = page.get("revisions") or []
    last = revisions[0] if revisions and isinstance(revisions[0], dict) else {}

    extract = (page.get("extract") or "").strip()
    row = {
        "actorInput": article.title,
        "actorIndex": 0,
        "status": "OK",
        "billable": True,
        "lang": lang,
        "title": title,
        "pageId": page.get("pageid"),
        "url": f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='()_,-./!~:*')}",
        "wikidataId": props.get("wikibase_item"),
        "description": props.get("wikibase-shortdesc"),
        "redirectedFrom": article.title if title.strip().lower() != article.title.strip().lower() else None,
        "pageLengthBytes": page.get("length"),
        "lastEdited": last.get("timestamp"),
        "lastEditor": last.get("user"),
        "wordCount": count_words(extract),
        "text": extract or None,
    }

    categories = [c.get("title") for c in (page.get("categories") or []) if c.get("title")]
    row["categories"] = categories if include_categories else None
    row["categoryCount"] = len(categories) if include_categories else None
    # cllimit=max returns every category in one answer, but any property can hand back a
    # continue block (revisions always do), so only a category block means the list was cut.
    cont = payload.get("continue")
    cut = isinstance(cont, dict) and any(str(key).startswith("cl") for key in cont)
    row["categoriesTruncated"] = cut if include_categories else None

    if include_pageviews:
        total, daily, note = read_pageviews(client, lang, title, pageviews_days,
                                            timeout=timeout)
        row["pageviews"] = total
        row["pageviewsPerDay"] = (round(total / max(1, pageviews_days), 2) if total is not None else None)
        row["pageviewsDaily"] = daily
        row["pageviewsNote"] = note
    else:
        row["pageviews"] = None
        row["pageviewsPerDay"] = None
        row["pageviewsDaily"] = None
        row["pageviewsNote"] = "pageviews were not requested for this run"

    if article.lang and row.get("wikidataId"):
        links, claims, note = read_wikidata(client, row["wikidataId"], timeout=timeout)
        row["sitelinkCount"] = links
        row["claimCount"] = claims
        row["wikidataNote"] = note
    else:
        row["sitelinkCount"] = None
        row["claimCount"] = None
        row["wikidataNote"] = "the article names no Wikidata item"

    return row, "", ""


def read_pageviews(client, lang: str, title: str, days: int, *,
                   timeout: float | None = None) -> tuple[int | None, list | None, str]:
    """Total views over the window, the daily series, and a note when unread."""
    start, end = _month_window(days)
    article = quote(title.replace(" ", "_"), safe="")
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
           f"{lang}.wikipedia/all-access/user/{article}/daily/{start}/{end}")
    request: dict = {"headers": {"User-Agent": USER_AGENT}}
    if timeout is not None:
        request["timeout"] = timeout
    try:
        resp = client.get(url, **request)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"pageviews could not be read ({type(exc).__name__})"
    if resp.status_code != 200:
        return None, None, f"pageviews answered HTTP {resp.status_code}"
    try:
        payload = _json(resp)
    except ValueError as exc:
        return None, None, f"pageviews: {exc}"
    items = payload.get("items")
    if not isinstance(items, list):
        return None, None, "pageviews answered without a series"
    daily = [{"date": str(item.get("timestamp") or "")[:8], "views": item.get("views")}
             for item in items if isinstance(item, dict)]
    total = sum(item["views"] for item in daily if isinstance(item.get("views"), int))
    return total, daily, ""


def read_wikidata(client, qid: str, *,
                  timeout: float | None = None) -> tuple[int | None, int | None, str]:
    """How many sitelinks and claims the Wikidata item has: an outside view of importance."""
    if not re.fullmatch(r"Q[1-9][0-9]{0,11}", qid or ""):
        return None, None, "the Wikidata id is not a Q-number"
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    request: dict = {"headers": {"User-Agent": USER_AGENT}}
    if timeout is not None:
        request["timeout"] = timeout
    try:
        resp = client.get(url, **request)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"Wikidata could not be read ({type(exc).__name__})"
    if resp.status_code != 200:
        return None, None, f"Wikidata answered HTTP {resp.status_code}"
    try:
        payload = _json(resp)
    except ValueError as exc:
        return None, None, f"Wikidata: {exc}"
    entity = (payload.get("entities") or {}).get(qid)
    if not isinstance(entity, dict):
        return None, None, "Wikidata has no entity for this id"
    links = entity.get("sitelinks") or {}
    claims = entity.get("claims") or {}
    return (len(links) if isinstance(links, dict) else None,
            len(claims) if isinstance(claims, dict) else None, "")
