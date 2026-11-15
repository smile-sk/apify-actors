"""Checks for src/tg.py on real Telegram pages saved under tests/fixtures.

Run: python tests/test_parse.py   (exit code 0 = every check passed)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import tg  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
CHECKS = 0
FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILS.append(f"{name}{(' -> ' + detail) if detail else ''}")


def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"got {got!r}, expected {expected!r}")


def run_all() -> int:
    checks_before = CHECKS

    # counts
    eq("count 1.67M", tg.parse_count("1.67M"), (1670000, True))
    eq("count 445", tg.parse_count("445"), (445, False))
    eq("count 17 031", tg.parse_count("17 031"), (17031, False))
    eq("count 1.2B", tg.parse_count("1.2B"), (1200000000, True))
    eq("count junk", tg.parse_count("views"), (None, True))
    eq("count empty", tg.parse_count(""), (None, True))
    eq("count 0", tg.parse_count("0"), (0, False))

    # usernames
    eq("normalize @durov", tg.normalize_channel("@durov"), ("durov", None))
    eq("normalize link s", tg.normalize_channel("https://t.me/s/durov"), ("durov", None))
    eq("normalize link post", tg.normalize_channel("t.me/durov/553?single"), ("durov", None))
    eq("normalize dollars", tg.normalize_channel("$durov"), ("durov", None))
    eq("normalize spaces", tg.normalize_channel("  durov  "), ("durov", None))
    check("normalize invite", tg.normalize_channel("https://t.me/+AbCdEf")[0] is None)
    check("normalize joinchat", tg.normalize_channel("https://t.me/joinchat/AAAA")[0] is None)
    check("normalize bad", tg.normalize_channel("py thon")[0] is None)
    check("normalize short", tg.normalize_channel("ab")[0] is None)
    check("normalize empty", tg.normalize_channel("")[0] is None)
    check("normalize digits only", tg.normalize_channel("12345")[0] is None)

    # text
    eq("text br", tg.clean_text("a<br/>b"), "a\nb")
    eq("text entity", tg.clean_text("a &amp; b"), "a & b")
    eq("text tags", tg.clean_text('<b>bold</b> text'), "bold text")

    # a real post: album, stars, views
    block = (FIX / "durov_last_block.html").read_text(encoding="utf-8")
    post = tg.parse_post(block)
    check("durov block parsed", post is not None)
    if post is not None:
        eq("durov id", post.id, 553)
        eq("durov channel", post.channel, "durov")
        eq("durov url", post.url, "https://t.me/durov/553")
        eq("durov date", post.date, "2026-11-09T13:00:39+00:00")
        eq("durov views raw", post.viewsRaw, "551K")
        eq("durov views", post.views, 551000)
        check("durov views approximate", post.viewsApprox is True)
        eq("durov media", post.mediaType, "photo")
        eq("durov media count", post.mediaCount, 1)
        check("durov media url is a link", bool(post.mediaUrl) and post.mediaUrl.startswith("http"))
        check("durov text mentions wallets", "700K+ self-custodial wallets" in post.text)
        stars = [r for r in post.reactions if r["kind"] == "stars"]
        check("durov has a stars reaction", len(stars) >= 1, str(post.reactions[:2]))
        check("durov reactions total positive", (post.reactionsTotal or 0) > 1000, str(post.reactionsTotal))

    # a real post with plain emoji reactions
    block = (FIX / "rian_reactions_block.html").read_text(encoding="utf-8")
    post = tg.parse_post(block)
    check("rian block parsed", post is not None)
    if post is not None:
        emoji = [r for r in post.reactions if r["emoji"]]
        check("rian reactions carry emoji", len(emoji) >= 1, str(post.reactions[:2]))
        check("rian emoji count is a number", all(isinstance(r["count"], int) for r in emoji))
        check("rian reactions are not stars", all(r["kind"] == "emoji" for r in emoji))

    # edited marker
    block = (FIX / "meduza_edited_block.html").read_text(encoding="utf-8")
    post = tg.parse_post(block)
    check("meduza block parsed", post is not None)
    if post is not None:
        check("meduza edited flag", post.edited is True)
        check("meduza has media", post.mediaType in ("document", "photo", "video", "album"),
              str(post.mediaType))

    # header of a channel page
    head = (FIX / "durov_head.html").read_text(encoding="utf-8")
    header = tg.parse_header(head)
    eq("header title", header["title"], "Pavel Durov")
    check("header subscribers", (header["subscribers"] or 0) > 1000000, str(header["subscribers"]))
    check("header subscribers approximate", header["subscribersApprox"] is True)

    # why a page is silent
    status, info = tg.classify_silent_page((FIX / "landing_group.html").read_text(encoding="utf-8"))
    eq("group status", status, tg.ST_NOT_A_CHANNEL)
    eq("group title", info.get("title"), "Python")
    check("group members counted", (info.get("members") or 0) > 1000, str(info.get("members")))

    status, info = tg.classify_silent_page((FIX / "landing_missing.html").read_text(encoding="utf-8"))
    eq("missing status", status, tg.ST_NOT_FOUND)
    eq("missing title", info.get("title"), None)

    status, info = tg.classify_silent_page((FIX / "landing_channel.html").read_text(encoding="utf-8"))
    eq("channel without posts is not 'missing'", status, tg.ST_NO_PREVIEW)
    check("channel subscribers counted", (info.get("subscribers") or 0) > 1000000,
          str(info.get("subscribers")))

    # a page of blocks splits without losing the last one
    page = (FIX / "durov_head.html").read_text(encoding="utf-8") + block
    check("split_blocks finds one block", len(tg.split_blocks(page)) == 1)
    check("parse_page on a single block", len(tg.parse_page(page)) == 1)

    # a row never promises what the preview cannot give
    row = tg.Post(channel="x", id=1, url="https://t.me/x/1", date=None, text="t",
                  views=None, viewsRaw=None, viewsApprox=True).as_row()
    check("row has no forwards field", "forwards" not in row, str(sorted(row)))
    check("row is billable", row["billable"] is True)

    # a custom emoji is painted as a background image, it is not media
    emoji_block = (
        '<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" '
        'data-post="chan/7"><div class="tgme_widget_message_bubble">'
        '<div class="tgme_widget_message_text js-message_text">'
        "<i class=\"emoji\" style=\"background-image:url('//telegram.org/img/emoji/40/F09F9880.png')\"></i>"
        'hello</div></div></div></div>')
    emoji_post = tg.parse_post(emoji_block)
    check("emoji: post parsed", emoji_post is not None)
    if emoji_post is not None:
        eq("emoji: not reported as media", emoji_post.mediaType, None)
        eq("emoji: no media counted", emoji_post.mediaCount, 0)

    # a real photo still counts, an emoji next to it does not
    mixed_block = (
        '<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" '
        'data-post="chan/8"><div class="tgme_widget_message_bubble">'
        "<a class=\"tgme_widget_message_photo_wrap\" style=\"background-image:url('https://cdn4.telesco.pe/file/abc.jpg')\"></a>"
        '<div class="tgme_widget_message_text js-message_text">'
        "<i class=\"emoji\" style=\"background-image:url('//telegram.org/img/emoji/40/F09F9880.png')\"></i>"
        'photo</div></div></div></div>')
    mixed = tg.parse_post(mixed_block)
    check("media: mixed block parsed", mixed is not None)
    if mixed is not None:
        eq("media: real photo kept", mixed.mediaType, "photo")
        eq("media: one photo counted", mixed.mediaCount, 1)

    # an invite link is told apart from a broken line
    check("invite: t.me/+", tg.is_invite_link("https://t.me/+AbCdEf123") is True)
    check("invite: joinchat", tg.is_invite_link("t.me/joinchat/AAAA") is True)
    check("invite: bare plus", tg.is_invite_link("+AbCdEf") is True)
    check("invite: a channel is not an invite", tg.is_invite_link("https://t.me/durov") is False)
    eq("invite status value", tg.ST_PRIVATE, "NOT_CHECKED_PRIVATE_INVITE")

    executed = CHECKS - checks_before
    print(f"test_parse: {executed} checks, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL:", fail)
    if executed < 30:
        print("  FAIL: too few checks ran")
        return 1
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(run_all())
