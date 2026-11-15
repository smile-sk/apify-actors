"""Reading public Telegram channels through their web preview.

Telegram shows every public channel at https://t.me/s/<username>: no account, no
phone number, no bot token. One page carries 20 posts; asking for ?before=<id>
walks backwards through the whole history.

What this module refuses to guess:

* a page with no posts is not "the channel is empty". Telegram returns 200 for
  a channel that does not exist, for a group, and for a channel whose public
  preview is switched off. The three are told apart by the landing page.
* view counts arrive rounded by Telegram itself ("1.67M"), so the exact number
  is reported as approximate instead of being passed off as precise.
* a refused request is an error, never an empty result.
"""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

_RE_MESSAGE_START = re.compile(r'<div class="tgme_widget_message_wrap')
_RE_POST = re.compile(r'data-post="([^/"]+)/(\d+)"')
_RE_VIEWS = re.compile(r'<span class="tgme_widget_message_views">([^<]+)</span>')
_RE_TIME = re.compile(r'<time datetime="([^"]+)"')
_RE_SIGNATURE = re.compile(r'<span class="tgme_widget_message_from_author"[^>]*>(.*?)</span>', re.S)
_RE_OWNER = re.compile(r'<span class="tgme_widget_message_owner_name"[^>]*>(?:<span[^>]*>)?(.*?)(?:</span>)?</a>', re.S)
_RE_EDITED = re.compile(r'class="tgme_widget_message_meta"[^>]*>\s*edited')
_RE_FORWARD = re.compile(r'tgme_widget_message_forwarded_from_name"><span[^>]*>(.*?)</span>', re.S)
_RE_REPLY_LINK = re.compile(r'<a class="tgme_widget_message_reply[^"]*" href="https://t\.me/([^/"]+)/(\d+)"')
_RE_PHOTO_BG = re.compile(r'background-image:url\(\'([^\']+)\'\)')
_RE_VIDEO_SRC = re.compile(r'<video[^>]+src="([^"]+)"')
_RE_PREVIEW = re.compile(r'<a class="tgme_widget_message_link_preview" href="([^"]+)"')
_RE_PREVIEW_SITE = re.compile(r'<div class="link_preview_site_name[^"]*"[^>]*>(.*?)</div>', re.S)
_RE_EMOJI_TEXT = re.compile(r'<b>([^<]+)</b>')
_RE_EMOJI_ID = re.compile(r'emoji-id="(\d+)"')
_RE_COUNT = re.compile(r'([\d][\d\s.,]*[KMB]?)\s*$')
_RE_HASHTAG = re.compile(r"(?<![\w&])#([\w]{2,64})", re.UNICODE)
_RE_MENTION = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{3,31})")
_RE_TAG = re.compile(r"<[^>]+>")

# channel header of the /s/ page
_RE_CH_TITLE = re.compile(r'tgme_channel_info_header_title[^>]*>\s*<span[^>]*>(.*?)</span>', re.S)
_RE_CH_DESC = re.compile(r'<div class="tgme_channel_info_description[^"]*"[^>]*>(.*?)</div>', re.S)
_RE_CH_PHOTO = re.compile(r'<i class="tgme_page_photo_image[^"]*"><img src="([^"]+)"')
_RE_COUNTER = re.compile(r'<span class="counter_value">([^<]+)</span>\s*<span class="counter_type">([^<]+)</span>')

# landing page (t.me/<username>) used only to classify a silent /s/ page
_RE_LAND_TITLE = re.compile(r'tgme_page_title[^>]*>\s*<span[^>]*>(.*?)</span>', re.S)
_RE_LAND_EXTRA = re.compile(r'tgme_page_extra">([^<]+)</div>')
_RE_LAND_DESC = re.compile(r'<div class="tgme_page_description[^"]*"[^>]*>(.*?)</div>', re.S)

MULT = {"K": 1000, "M": 1000000, "B": 1000000000}

# statuses for a channel that produced no posts
ST_NOT_FOUND = "NOT_CHECKED_CHANNEL_NOT_FOUND"
ST_NOT_A_CHANNEL = "NOT_CHECKED_NOT_A_CHANNEL"
ST_NO_PREVIEW = "NOT_CHECKED_NO_PUBLIC_PREVIEW"
ST_PRIVATE = "NOT_CHECKED_PRIVATE_INVITE"
ST_INVALID = "NOT_CHECKED_INVALID"
ST_DUPLICATE = "NOT_CHECKED_DUPLICATE"
ST_LIMIT = "NOT_CHECKED_LIMIT"
ST_EMPTY = "NOT_CHECKED_EMPTY"
ST_SOURCE = "NOT_CHECKED_SOURCE"
ST_CAP = "NOT_CHECKED_CAP"
ST_CHARGE_UNVERIFIED = "NOT_CHECKED_CHARGE_UNVERIFIED"
ST_TIME = "NOT_CHECKED_TIME"
ST_ERROR = "NOT_CHECKED_ERROR"


class SourceError(RuntimeError):
    """The page could not be read at all: timeout, 5xx, repeated 429."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def clean_text(raw: str) -> str:
    """HTML of a post body into plain text, keeping line breaks."""
    text = re.sub(r"<br\s*/?>", "\n", raw)
    text = _RE_TAG.sub("", text)
    text = htmllib.unescape(text)
    text = text.replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def parse_count(raw: str) -> tuple[int | None, bool]:
    """'1.67M' -> (1670000, True) ; '445' -> (445, False) ; junk -> (None, True).

    The second value says the number is approximate: Telegram itself rounds
    views and reactions on the web preview, so we never call such a number exact.
    """
    if raw is None:
        return None, True
    s = htmllib.unescape(str(raw)).replace("\u00a0", " ").strip()
    if not s:
        return None, True
    s = s.replace(" ", "").replace(",", ".")
    mult = 1
    if s[-1].upper() in MULT:
        mult = MULT[s[-1].upper()]
        s = s[:-1]
    try:
        value = float(s)
    except ValueError:
        return None, True
    return int(round(value * mult)), mult != 1


def is_invite_link(raw: str) -> bool:
    """True for t.me/+hash or a joinchat line: a private chat cannot be read."""
    s = (raw or "").strip()
    if s.startswith("@") or s.startswith("$"):
        s = s.lstrip("@$")
    low = s.lower()
    return "joinchat" in low or "/+" in low or low.startswith("+")


def normalize_channel(raw: str) -> tuple[str | None, str | None]:
    """Input line into a channel username, or (None, reason)."""
    s = (raw or "").strip()
    if not s:
        return None, "the line is empty"
    if s.startswith("@") or s.startswith("$"):
        # $ is what a spreadsheet does to a bare username; it is not part of it
        s = s.lstrip("@$")
    if is_invite_link(s):
        return None, "this is an invite link to a private chat, the web preview cannot read it"
    low = s.lower()
    if "t.me/" in low or "telegram.me/" in low:
        parts = re.split(r"(?:t\.me|telegram\.me)/", s, maxsplit=1, flags=re.I)[1]
        parts = parts.split("?", 1)[0].split("#", 1)[0].strip("/")
        if parts.lower().startswith("s/"):
            parts = parts[2:]
        if not parts:
            return None, "the link has no channel name in it"
        s = parts.split("/", 1)[0]
    else:
        s = s.split("/", 1)[0].lstrip("@")
    if not USERNAME_RE.match(s):
        return None, "not a Telegram username: letters, digits and underscore, 4 to 32 characters"
    return s, None


def _inner_html(page: str, start: int) -> str:
    """Inner HTML of the <div> that opens at `start`, honouring nesting."""
    open_tag = page.find(">", start)
    if open_tag < 0:
        return ""
    i = open_tag + 1
    depth = 1
    while i < len(page):
        nxt_open = page.find("<div", i)
        nxt_close = page.find("</div>", i)
        if nxt_close < 0:
            return page[open_tag + 1:]
        if 0 <= nxt_open < nxt_close:
            depth += 1
            i = nxt_open + 4
            continue
        depth -= 1
        if depth == 0:
            return page[open_tag + 1:nxt_close]
        i = nxt_close + 6
    return page[open_tag + 1:]


def _body_text_html(block: str) -> str:
    """HTML of the post body, skipping the quoted text of a reply."""
    for match in re.finditer(r'<div class="tgme_widget_message_text[^"]*"', block):
        tag_end = block.find(">", match.start())
        attrs = block[match.start():tag_end]
        if "js-message_reply_text" in attrs:
            continue
        return _inner_html(block, match.start())
    return ""


def _first_text(pattern: re.Pattern, block: str) -> str | None:
    m = pattern.search(block)
    return clean_text(m.group(1)) if m else None


def parse_reactions(block: str) -> tuple[list[dict], int | None]:
    reactions: list[dict] = []
    total: int | None = 0
    for match in re.finditer(r'<span class="(tgme_reaction[^"]*)"[^>]*>(.*?)</span>', block, re.S):
        classes, inner = match.group(1), match.group(2)
        emoji = _RE_EMOJI_TEXT.search(inner)
        emoji_id = _RE_EMOJI_ID.search(inner)
        plain = clean_text(inner)
        count_raw = None
        m_count = _RE_COUNT.search(plain.replace("\n", " ").strip())
        if m_count:
            count_raw = m_count.group(1).strip()
        count, approx = parse_count(count_raw or plain)
        if count is None and emoji is None and emoji_id is None:
            continue
        reactions.append({
            "emoji": htmllib.unescape(emoji.group(1)).strip() if emoji else None,
            "emojiId": emoji_id.group(1) if emoji_id else None,
            "count": count,
            "approx": approx,
            "paid": "tgme_reaction_paid" in classes,
            "kind": "stars" if "tgme_reaction_paid" in classes else "emoji",
        })
        if count is not None:
            total = (total or 0) + count
    return reactions, (total if reactions else None)


@dataclass
class ChannelHeader:
    username: str
    title: str | None = None
    description: str | None = None
    photoUrl: str | None = None
    subscribers: int | None = None
    subscribersApprox: bool = True
    photos: int | None = None
    videos: int | None = None
    links: int | None = None
    kind: str = "channel"
    status: str | None = None
    reason: str | None = None

    def as_row(self) -> dict:
        return {
            "rowType": "channel",
            "channel": self.username,
            "channelTitle": self.title,
            "channelDescription": self.description,
            "channelPhotoUrl": self.photoUrl,
            "channelKind": self.kind,
            "subscribers": self.subscribers,
            "subscribersApprox": self.subscribersApprox,
            "channelPhotos": self.photos,
            "channelVideos": self.videos,
            "channelLinks": self.links,
            "status": self.status,
            "reason": self.reason,
            "billable": False,
        }


@dataclass
class Post:
    channel: str
    id: int
    url: str
    date: str | None
    text: str
    views: int | None
    viewsRaw: str | None
    viewsApprox: bool
    reactions: list[dict] = field(default_factory=list)
    reactionsTotal: int | None = None
    replyToId: int | None = None
    forwardFrom: str | None = None
    authorSignature: str | None = None
    edited: bool = False
    mediaType: str | None = None
    mediaUrl: str | None = None
    mediaTitle: str | None = None
    mediaCount: int = 0
    linkPreviewUrl: str | None = None
    linkPreviewSite: str | None = None
    links: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        return {
            "rowType": "message",
            "channel": self.channel,
            "messageId": self.id,
            "url": self.url,
            "date": self.date,
            "text": self.text,
            "textLength": len(self.text),
            "views": self.views,
            "viewsRaw": self.viewsRaw,
            "viewsApproximate": self.viewsApprox,
            "reactions": self.reactions,
            "reactionsTotal": self.reactionsTotal,
            "replyToId": self.replyToId,
            "forwardedFrom": self.forwardFrom,
            "authorSignature": self.authorSignature,
            "edited": self.edited,
            "mediaType": self.mediaType,
            "mediaUrl": self.mediaUrl,
            "mediaTitle": self.mediaTitle,
            "mediaCount": self.mediaCount,
            "linkPreviewUrl": self.linkPreviewUrl,
            "linkPreviewSite": self.linkPreviewSite,
            "links": self.links,
            "hashtags": self.hashtags,
            "mentions": self.mentions,
            "billable": True,
        }


def split_blocks(page: str) -> list[str]:
    starts = [m.start() for m in _RE_MESSAGE_START.finditer(page)]
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(page)
        blocks.append(page[start:end])
    return blocks


def parse_header(page: str) -> dict:
    """Channel facts from the top of the /s/ page."""
    out: dict = {"title": None, "description": None, "photoUrl": None,
                 "subscribers": None, "subscribersApprox": True,
                 "photos": None, "videos": None, "links": None}
    title = _RE_CH_TITLE.search(page)
    if title:
        out["title"] = clean_text(title.group(1))
    desc = _RE_CH_DESC.search(page)
    if desc:
        out["description"] = clean_text(desc.group(1))
    photo = _RE_CH_PHOTO.search(page)
    if photo:
        out["photoUrl"] = htmllib.unescape(photo.group(1))
    for value, kind in _RE_COUNTER.findall(page):
        kind = kind.strip().lower()
        number, approx = parse_count(value)
        if kind.startswith("subscriber"):
            out["subscribers"], out["subscribersApprox"] = number, approx
        elif kind.startswith("photo"):
            out["photos"] = number
        elif kind.startswith("video"):
            out["videos"] = number
        elif kind.startswith("link"):
            out["links"] = number
    return out


def parse_post(block: str) -> Post | None:
    post_match = _RE_POST.search(block)
    if not post_match:
        return None
    channel, message_id = post_match.group(1), int(post_match.group(2))
    body_html = _body_text_html(block)
    text = clean_text(body_html)

    views_raw = None
    views = None
    views_approx = True
    m_views = _RE_VIEWS.search(block)
    if m_views:
        views_raw = htmllib.unescape(m_views.group(1)).strip()
        views, views_approx = parse_count(views_raw)

    date = None
    m_time = _RE_TIME.search(block)
    if m_time:
        date = m_time.group(1)

    signature = _first_text(_RE_SIGNATURE, block) or _first_text(_RE_OWNER, block)
    forward_from = _first_text(_RE_FORWARD, block)
    reply_to = None
    m_reply = _RE_REPLY_LINK.search(block)
    if m_reply:
        reply_to = int(m_reply.group(2))

    reactions, reactions_total = parse_reactions(block)

    media_type = None
    media_url = None
    media_title = None
    media_count = 0
    # Telegram paints custom emoji as background images of the same class, so an actor
    # that counts every background url reports media on posts that have none
    photos = [u for u in _RE_PHOTO_BG.findall(block) if "/img/emoji/" not in u]
    if photos:
        media_count = len(photos)
        media_type = "photo" if media_count == 1 else "album"
        media_url = htmllib.unescape(photos[0])
    video = _RE_VIDEO_SRC.search(block)
    if video:
        media_type = "video" if media_count <= 1 else "album"
        media_url = htmllib.unescape(video.group(1))
        media_count = max(media_count, 1)
    if 'tgme_widget_message_document_wrap' in block:
        doc = re.search(r'tgme_widget_message_document_title[^"]*"[^>]*>(.*?)</div>', block, re.S)
        media_type = media_type or "document"
        media_title = clean_text(doc.group(1)) if doc else None
        media_count = max(media_count, 1)
    if 'tgme_widget_message_poll' in block and not media_type:
        media_type = "poll"
    if 'tgme_widget_message_sticker' in block and not media_type:
        media_type = "sticker"

    preview_url = None
    preview_site = None
    m_prev = _RE_PREVIEW.search(block)
    if m_prev:
        preview_url = htmllib.unescape(m_prev.group(1))

    links = []
    for href in re.findall(r'href="([^"]+)"', body_html):
        url = htmllib.unescape(href)
        if url.startswith("http") and url not in links:
            links.append(url)

    return Post(
        channel=channel,
        id=message_id,
        url=f"https://t.me/{channel}/{message_id}",
        date=date,
        text=text,
        views=views,
        viewsRaw=views_raw,
        viewsApprox=views_approx,
        reactions=reactions,
        reactionsTotal=reactions_total,
        replyToId=reply_to,
        forwardFrom=forward_from,
        authorSignature=signature,
        edited=bool(_RE_EDITED.search(block)),
        mediaType=media_type,
        mediaUrl=media_url,
        mediaTitle=media_title,
        mediaCount=media_count,
        linkPreviewUrl=preview_url,
        linkPreviewSite=clean_text(_RE_PREVIEW_SITE.search(block).group(1)) if _RE_PREVIEW_SITE.search(block) else None,
        links=links,
        hashtags=sorted({h.lower() for h in _RE_HASHTAG.findall(text)}),
        mentions=sorted({m.lower() for m in _RE_MENTION.findall(text)}),
    )


def parse_page(page: str) -> list[Post]:
    posts = []
    for block in split_blocks(page):
        post = parse_post(block)
        if post is not None:
            posts.append(post)
    return posts


def classify_silent_page(landing_html: str) -> tuple[str, dict]:
    """Why a /s/ page carried no posts: the landing page names the reason."""
    title = _RE_LAND_TITLE.search(landing_html)
    extra = _RE_LAND_EXTRA.search(landing_html)
    desc = _RE_LAND_DESC.search(landing_html)
    info = {
        "title": clean_text(title.group(1)) if title else None,
        "extra": clean_text(extra.group(1)) if extra else None,
        "description": clean_text(desc.group(1)) if desc else None,
    }
    if not info["title"]:
        return ST_NOT_FOUND, info
    extra = info["extra"] or ""
    number = re.search(r"([\d][\d\s.,]*[KMB]?)\s*(member|subscriber)", extra, re.I)
    count = parse_count(number.group(1))[0] if number else None
    if "member" in extra.lower():
        info["members"] = count
        return ST_NOT_A_CHANNEL, info
    if "subscriber" in extra.lower():
        info["subscribers"] = count
        return ST_NO_PREVIEW, info
    return ST_NO_PREVIEW, info
