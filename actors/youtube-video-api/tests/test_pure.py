"""The reader and the id parser, against real YouTube answers captured on 2026-11-13.

What is checked here is what the money depends on: a link becomes a video id, a payload
becomes a row with the numbers a buyer paid for, and a payload without details is a
not-found instead of a half empty row.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src import yt  # noqa: E402

PASSED = 0
FAILED: list = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(name)


PAYLOAD = {
    "playabilityStatus": {"status": "UNPLAYABLE", "reason": "Video unavailable"},
    "videoDetails": {
        "videoId": "jNQXAC9IVRw", "title": "Me at the zoo", "lengthSeconds": "19",
        "viewCount": "448439214", "author": "jawed", "channelId": "UC4QobU6STFB0P71PMvOGN5A",
        "keywords": ["me at the zoo", "jawed karim"], "isLiveContent": False,
        "shortDescription": "The first video on YouTube.",
        "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/vi/jNQXAC9IVRw/hq.jpg",
                                      "width": 480, "height": 360}]},
    },
    "microformat": {"playerMicroformatRenderer": {
        "publishDate": "2005-04-23T20:31:52-07:00", "category": "Film & Animation"}},
}


def run_all() -> int:
    # 1. every shape of input gives the same id
    for raw in ["jNQXAC9IVRw",
                "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                "https://youtu.be/jNQXAC9IVRw",
                "https://www.youtube.com/shorts/jNQXAC9IVRw?feature=share",
                "https://www.youtube.com/embed/jNQXAC9IVRw"]:
        found, why = yt.video_id(raw)
        check(f"id from {raw[:38]}: got the right id", found == "jNQXAC9IVRw")
        check(f"id from {raw[:38]}: no reason", why == "")

    # 2. junk is refused with a reason, never guessed
    for raw in ["", "   ", "hello world", "https://vimeo.com/12345",
                "https://www.youtube.com/watch?v=short"]:
        found, why = yt.video_id(raw)
        check(f"junk {raw[:30]!r}: no id", found is None)
        check(f"junk {raw[:30]!r}: a reason is named", bool(why))

    # 2b. M3: a twelve character id in a link is not the first eleven characters of it.
    # Truncating it would sell a paid row for a different video.
    found, why = yt.video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQQ")
    check("long id: refused, not cut to eleven", found is None)
    check("long id: a reason is named", bool(why))
    found, _ = yt.video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    check("eleven id: still read", found == "dQw4w9WgXcQ")

    # 2c. M6: /live/ links are real, and a non YouTube host is not.
    found, _ = yt.video_id("https://www.youtube.com/live/jNQXAC9IVRw?feature=share")
    check("live link: read", found == "jNQXAC9IVRw")
    found, why = yt.video_id("https://vimeo.com/watch?v=jNQXAC9IVRw")
    check("vimeo link: refused even with a v= parameter", found is None)
    check("vimeo link: a reason is named", bool(why))
    found, _ = yt.video_id("https://music.youtube.com/watch?v=jNQXAC9IVRw")
    check("music host: read", found == "jNQXAC9IVRw")

    # 3. a real payload becomes a full row
    row = yt.row(PAYLOAD, raw="jNQXAC9IVRw", index=0)
    check("row: video id", row["videoId"] == "jNQXAC9IVRw")
    check("row: the watch url is built", row["url"].endswith("watch?v=jNQXAC9IVRw"))
    check("row: title", row["title"] == "Me at the zoo")
    check("row: views are a number", row["views"] == 448439214)
    check("row: length is a number", row["lengthSeconds"] == 19)
    check("row: channel", row["channelTitle"] == "jawed")
    check("row: publish date", str(row["publishedAt"]).startswith("2005-04-23"))
    check("row: category", row["category"] == "Film & Animation")
    check("row: keywords", row["keywords"] == ["me at the zoo", "jawed karim"])
    check("row: thumbnail", str(row["thumbnailUrl"]).startswith("https://i.ytimg.com/"))
    check("row: unplayable is reported, not hidden", row["playability"] == "UNPLAYABLE")
    check("row: status is OK", row["status"] == "OK")
    check("row: no description by default", "description" not in row)
    check("row: the playability reason is a field",
          "playabilityReason" in row)

    # 4. the description is added only when asked
    with_desc = yt.row(PAYLOAD, raw="x", index=1, with_description=True)
    check("row: description added on request", with_desc["description"].startswith("The first"))

    # 5. fetch: a payload without details is a not-found with the store reason, not a row
    class Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        def __init__(self, code=200, payload=None, boom=None):
            self.code, self.payload, self.boom = code, payload, boom
            self.calls = 0
            self.kwargs = {}

        def post(self, *_a, **_k):
            self.calls += 1
            self.kwargs = _k
            if self.boom:
                raise self.boom
            return Resp(self.code, self.payload)

    # 2d. M7: the request carries the time the run has left, so a slow answer cannot
    # outlive the deadline and get the run killed without a summary.
    slow = Client(payload=PAYLOAD)
    yt.fetch(slow, "jNQXAC9IVRw", timeout=4.5)
    check("fetch: the request timeout is the remaining budget",
          slow.kwargs.get("timeout") == 4.5)
    plain = Client(payload=PAYLOAD)
    yt.fetch(plain, "jNQXAC9IVRw")
    check("fetch: no deadline means no per-request timeout", "timeout" not in plain.kwargs)

    empty = {"playabilityStatus": {"status": "ERROR", "reason": "Video unavailable"}}
    payload, why = yt.fetch(Client(payload=empty), "jNQXAC9IVRw")
    check("fetch: no details means no payload", payload is None)
    check("fetch: the reason names the store answer", "Video unavailable" in why)

    bot = {"playabilityStatus": {"status": "LOGIN_REQUIRED",
                                 "reason": "Sign in to confirm you\u2019re not a bot"}}
    payload, why = yt.fetch(Client(payload=bot), "jNQXAC9IVRw")
    check("fetch: a bot check is not called a missing video",
          payload is None and "has no video" not in why and "bot check" in why)

    payload, why = yt.fetch(Client(payload=PAYLOAD), "jNQXAC9IVRw")
    check("fetch: a real payload comes back", isinstance(payload, dict))
    check("fetch: no reason on success", why == "")

    payload, why = yt.fetch(Client(code=429), "jNQXAC9IVRw")
    check("fetch: a bad status is named", payload is None and "429" in why)

    payload, why = yt.fetch(Client(boom=TimeoutError("slow")), "jNQXAC9IVRw")
    check("fetch: a network error is named, not swallowed",
          payload is None and "TimeoutError" in why)

    # 6. the endpoint is asked once per video and never with a key from the buyer
    client = Client(payload=PAYLOAD)
    yt.fetch(client, "jNQXAC9IVRw")
    check("fetch: exactly one request per video", client.calls == 1)
    check("fetch: the public endpoint is used", "youtubei/v1/player" in yt.ENDPOINT)

    # 7. the relay: a narrow transport, and its failure is not a verdict about the video
    relay = yt.Relay("https://relay.example/ytrelay/", "secret")
    check("relay: the endpoint is built from the base address",
          relay.endpoint() == "https://relay.example/ytrelay/player")
    client = Client(payload=PAYLOAD)
    payload, why, kind = relay.fetch(client, "jNQXAC9IVRw")
    check("relay: a good answer comes back with no reason",
          isinstance(payload, dict) and why == "" and kind == "")
    check("relay: the token travels in the header, not in the url",
          client.kwargs.get("headers", {}).get("X-Relay-Token") == "secret"
          and "secret" not in relay.endpoint())
    payload, why, kind = relay.fetch(Client(code=401), "jNQXAC9IVRw")
    check("relay: a refused token is a relay fault",
          payload is None and kind == "relay" and "token" in why)
    payload, why, kind = relay.fetch(Client(code=429), "jNQXAC9IVRw")
    check("relay: a busy relay is a relay fault", payload is None and kind == "relay")
    payload, why, kind = relay.fetch(Client(boom=TimeoutError("slow")), "jNQXAC9IVRw")
    check("relay: a dead relay is named, not swallowed",
          payload is None and kind == "relay" and "TimeoutError" in why)
    payload, why, kind = relay.fetch(Client(payload=empty), "jNQXAC9IVRw")
    check("relay: the store verdict stays a store verdict",
          payload is None and kind == "" and "Video unavailable" in why)
    payload, why, kind = yt.Relay("", "").fetch(Client(payload=PAYLOAD), "jNQXAC9IVRw")
    check("relay: no configuration means no request", payload is None and kind == "relay")

    print("PASSED", PASSED, "FAILED", len(FAILED))
    for name in FAILED:
        print("  FAIL:", name)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
