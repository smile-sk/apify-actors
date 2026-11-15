"""Flow checks for src/main.py with a fake Actor that follows apify 4.2.

Run: python tests/test_main.py   (exit code 0 = every check passed)

The fake repeats the real SDK shapes on purpose: push_data is a coroutine and
returns an object with charged_count / event_charge_limit_reached, open_dataset
and get_metadata are coroutines, get_env carries the key run_id.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import main as actor_main  # noqa: E402

CHECKS = 0
FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILS.append(f"{name}{(' -> ' + detail) if detail else ''}")


def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"got {got!r}, expected {expected!r}")


def block(channel: str, message_id: int, text: str = "hello", views: str = "12",
          date: str = "2026-11-01T10:00:00+00:00") -> str:
    return (
        '<div class="tgme_widget_message_wrap js-widget_message_wrap">'
        f'<div class="tgme_widget_message" data-post="{channel}/{message_id}">'
        '<div class="tgme_widget_message_bubble">'
        f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
        '<div class="tgme_widget_message_footer"><div class="tgme_widget_message_info">'
        f'<span class="tgme_widget_message_views">{views}</span>'
        '<span class="tgme_widget_message_meta">'
        f'<a class="tgme_widget_message_date" href="https://t.me/{channel}/{message_id}">'
        f'<time datetime="{date}"></time></a></span></div></div></div></div></div>'
    )


def page(*blocks: str) -> str:
    return "<html><body>" + "".join(blocks) + "</body></html>"


EMPTY = "<html><body>no messages here</body></html>"


class FakeResult:
    def __init__(self, charged_count: int, limit: bool = False) -> None:
        self.charged_count = charged_count
        self.event_charge_limit_reached = limit


class FakeMeta:
    def __init__(self, count: int) -> None:
        self.item_count = count


class FakeDataset:
    def __init__(self, rows: list, actor=None) -> None:
        self.rows = rows
        self.actor = actor

    async def get_metadata(self):
        if self.actor is not None:
            self.actor.meta_calls += 1
            if self.actor.meta_error:
                raise RuntimeError("metadata is not readable in this run")
        return FakeMeta(len(self.rows))


class FakeStore:
    def __init__(self) -> None:
        self.values: dict = {}

    async def set_value(self, key, value) -> None:
        self.values[key] = value


class FakePricing:
    def __init__(self, is_ppe=True, prices=None) -> None:
        self.is_pay_per_event = is_ppe
        self.per_event_prices = prices if prices is not None else {actor_main.CHARGE_EVENT: 0.0015}


class FakeChargingManager:
    def __init__(self, pricing) -> None:
        self._pricing = pricing

    def get_pricing_info(self):
        return self._pricing


class ActorExit(Exception):
    pass


class FakeActor:
    """One instance per scenario; the class keeps the last one for assertions."""

    last: "FakeActor | None" = None

    def __init__(self, inp, pricing="ok", charge="ok", cap_at=None, write_error=False,
                 meta_error=False):
        self.inp = inp
        self.pricing = pricing
        self.charge = charge
        self.cap_at = cap_at
        self.write_error = write_error
        self.meta_error = meta_error
        self.meta_calls = 0
        self.rows: list = []
        self.charged: list = []
        self.status_message = None
        self.failed_with = None
        self.store = FakeStore()
        self.log = types.SimpleNamespace(info=lambda *a, **k: None,
                                         error=lambda *a, **k: None)
        FakeActor.last = self

    # --- SDK surface -----------------------------------------------------
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_input(self):
        return self.inp

    def get_charging_manager(self):
        if self.pricing == "missing":
            raise RuntimeError("no charging manager in a local run")
        pricing = FakePricing() if self.pricing == "ok" else (
            FakePricing(is_ppe=False) if self.pricing == "flat" else
            FakePricing(prices={"other": 0.01}))
        return FakeChargingManager(pricing)

    async def open_dataset(self):
        return FakeDataset(self.rows, self)

    async def open_key_value_store(self):
        return self.store

    async def push_data(self, row, charged_event_name=None):
        if self.write_error:
            # the dataset refuses the row: nothing reaches the buyer
            raise RuntimeError("dataset write refused")
        if charged_event_name:
            if self.charge == "error":
                # the SDK writes the row first and charges after it: a failed charge
                # leaves the row in the dataset, it only costs us the money
                self.rows.append(dict(row))
                raise RuntimeError("charge refused")
            if self.charge == "cap" or (self.cap_at is not None and len(self.charged) >= self.cap_at):
                return FakeResult(0, True)
            # the row is written before the charge, exactly like the SDK does
            self.rows.append(dict(row))
            self.charged.append((row.get("channel"), row.get("messageId")))
            return FakeResult(1)
        self.rows.append(dict(row))
        return FakeResult(0)

    async def set_status_message(self, message):
        self.status_message = message

    async def fail(self, *, status_message=None, **kwargs):
        self.failed_with = status_message
        raise ActorExit(status_message)

    @staticmethod
    def get_env():
        return {"run_id": "testRun123"}


class FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class FakeHTTPError(Exception):
    pass


class FakeAsyncClient:
    def __init__(self, pages: dict, errors: dict | None = None, **kwargs) -> None:
        self.pages = pages
        self.errors = errors or {}
        self.requested: list[str] = []

    async def get(self, url: str):
        self.requested.append(url)
        if url in self.errors:
            raise FakeHTTPError(self.errors[url])
        if url not in self.pages:
            return FakeResponse(200, EMPTY)
        return FakeResponse(200, self.pages[url])

    async def aclose(self):
        return None


FAKE_HTTPX = types.SimpleNamespace(
    AsyncClient=FakeAsyncClient,
    HTTPError=FakeHTTPError,
    Timeout=lambda *a, **k: None,
)


def run(inp, pages, errors=None, **actor_kwargs):
    """Run main() against one FakeActor. Returns (actor, exit error, http client)."""
    actor = FakeActor(inp, **actor_kwargs)
    original_actor, original_httpx = actor_main.Actor, actor_main.httpx
    original_interval = actor_main.MIN_PAGE_INTERVAL_SEC
    client = FakeAsyncClient(pages or {}, errors=errors)

    actor_main.Actor = actor
    actor_main.MIN_PAGE_INTERVAL_SEC = 0.0
    actor_main.httpx = types.SimpleNamespace(
        AsyncClient=lambda **kwargs: client,
        HTTPError=FakeHTTPError,
        Timeout=lambda *a, **k: None,
    )
    error = None
    try:
        asyncio.run(actor_main.main())
    except ActorExit as exc:
        error = exc
    finally:
        actor_main.Actor = original_actor
        actor_main.httpx = original_httpx
        actor_main.MIN_PAGE_INTERVAL_SEC = original_interval
    return actor, error, client


def summary_of(actor) -> dict:
    for key, value in actor.store.values.items():
        if key.startswith("RUN_SUMMARY-"):
            return value
    return {}


def posts(actor) -> list:
    return [r for r in actor.rows if r.get("rowType") == "message"]


def channels(actor) -> list:
    return [r for r in actor.rows if r.get("rowType") == "channel"]


def run_all() -> int:
    before = CHECKS

    # 1. happy path: one channel, three posts, all charged
    pages = {"https://t.me/s/chan": page(block("chan", 1), block("chan", 2), block("chan", 3))}
    actor, error, _ = run({"channels": ["chan"]}, pages)
    eq("happy: no exit error", error, None)
    eq("happy: 3 posts", len(posts(actor)), 3)
    eq("happy: 3 charges", len(actor.charged), 3)
    sm = summary_of(actor)
    eq("happy: summary key", sm.get("summaryKey"), "RUN_SUMMARY-testRun123")
    eq("happy: charged events", sm.get("chargedEvents"), 3)
    eq("happy: delivered", sm.get("messagesDelivered"), 3)
    eq("happy: stop reason", sm.get("stopReason"), None)
    check("happy: status message set", bool(actor.status_message), str(actor.status_message))
    eq("happy: one channel row", len(channels(actor)), 1)
    eq("happy: channel status", channels(actor)[0]["status"], "READ")
    eq("happy: channel row not billable", channels(actor)[0]["billable"], False)
    eq("happy: posts billable", posts(actor)[0]["billable"], True)
    eq("happy: text keeps content", posts(actor)[0]["text"], "hello")
    eq("happy: pagination stops on empty page", sm.get("pagesFetched"), 2)
    check("happy: asked page 2 with before", any("?before=1" in u for u in _.requested),
          str(_.requested))

    # 2. a group: 200 with no posts, landing page says members
    group_landing = ('<div class="tgme_page_title"><span>Python</span></div>'
                     '<div class="tgme_page_extra">17 031 members, 2 663 online</div>')
    actor, error, _ = run({"channels": ["ru_python"]},
                          {"https://t.me/s/ru_python": EMPTY, "https://t.me/ru_python": group_landing})
    eq("group: no posts delivered", len(posts(actor)), 0)
    eq("group: no charges", len(actor.charged), 0)
    eq("group: status", channels(actor)[0]["status"], "NOT_CHECKED_NOT_A_CHANNEL")
    check("group: reason mentions the preview", "group" in (channels(actor)[0]["reason"] or ""))
    eq("group: title read", channels(actor)[0]["channelTitle"], "Python")
    eq("group: members kept", channels(actor)[0]["subscribers"], 17031)
    check("group: run fails with a human message", bool(actor.failed_with), str(actor.failed_with))
    check("group: failure says nothing was charged",
          "Nothing was charged" in (actor.failed_with or ""))

    # 3. a username that does not exist
    missing = ('<div class="tgme_page_description">If you have Telegram, you can contact '
               '@nope right away.</div>')
    actor, error, _ = run({"channels": ["nope"]},
                          {"https://t.me/s/nope": EMPTY, "https://t.me/nope": missing})
    eq("missing: status", channels(actor)[0]["status"], "NOT_CHECKED_CHANNEL_NOT_FOUND")
    eq("missing: no charges", len(actor.charged), 0)

    # 4. a channel that exists but hides its preview
    hidden = ('<div class="tgme_page_title"><span>Hidden</span></div>'
              '<div class="tgme_page_extra">10 063 603 subscribers</div>')
    actor, error, _ = run({"channels": ["hidden"]},
                          {"https://t.me/s/hidden": EMPTY, "https://t.me/hidden": hidden})
    eq("hidden: status", channels(actor)[0]["status"], "NOT_CHECKED_NO_PUBLIC_PREVIEW")
    eq("hidden: subscribers read", channels(actor)[0]["subscribers"], 10063603)

    # 5. duplicates and invalid lines cost nothing
    actor, error, _ = run({"channels": ["chan", "chan", "bad name"]}, pages)
    statuses = [r["status"] for r in channels(actor)]
    check("dupe: duplicate row", "NOT_CHECKED_DUPLICATE" in statuses, str(statuses))
    check("dupe: invalid row", "NOT_CHECKED_INVALID" in statuses, str(statuses))
    eq("dupe: charges only for unique posts", len(actor.charged), 3)
    eq("dupe: duplicate counter", summary_of(actor).get("duplicateChannels"), 1)

    # 6. the spending cap: the post that hit it is in the dataset, the rest is not charged
    actor, error, _ = run({"channels": ["chan"]}, pages, cap_at=2)
    sm = summary_of(actor)
    eq("cap: stop reason", sm.get("stopReason"), "spending-cap")
    eq("cap: charged only before the cap", sm.get("chargedEvents"), 2)
    eq("cap: cap counter", sm.get("messagesCap"), 1)
    check("cap: a row names the cap", any(r.get("status") == "NOT_CHECKED_CAP" for r in posts(actor)),
          str([r.get("status") for r in posts(actor)]))
    cap_row = next(r for r in posts(actor) if r.get("status") == "NOT_CHECKED_CAP")
    eq("cap: row carries no text", cap_row["text"], None)
    check("cap: row keeps identity", cap_row["url"].startswith("https://t.me/chan/"))

    # 7. a charge that throws: the row is written, money is not
    actor, error, _ = run({"channels": ["chan"]}, pages, charge="error")
    sm = summary_of(actor)
    eq("charge error: delivered uncharged", sm.get("deliveredUncharged"), 3)
    eq("charge error: no charged events", sm.get("chargedEvents"), 0)
    check("charge error: stop reason recorded", sm.get("stopReason") == "charge-error",
          str(sm.get("stopReason")))

    # 8. a run that cannot bill at all: rows are delivered free and say so
    actor, error, _ = run({"channels": ["chan"]}, pages, pricing="flat")
    sm = summary_of(actor)
    eq("flat: billing inactive", sm.get("billingActive"), False)
    eq("flat: rows delivered free", sm.get("deliveredUncharged"), 3)
    check("flat: row marked billingInactive", all(r.get("billingInactive") for r in posts(actor)))
    check("flat: status says billing inactive", "billing inactive" in (actor.status_message or ""))

    # 9. pricing without our event
    actor, error, _ = run({"channels": ["chan"]}, pages, pricing="other")
    eq("no event: billing inactive", summary_of(actor).get("billingActive"), False)
    check("no event: reason names the event",
          "message-delivered" in (summary_of(actor).get("billingReason") or ""))

    # 10. maxPostsPerChannel cuts and says how many
    five = page(*[block("chan", i) for i in range(1, 6)])
    actor, error, _ = run({"channels": ["chan"], "maxPostsPerChannel": 2}, {"https://t.me/s/chan": five})
    eq("limit: two posts", len(posts(actor)), 2)
    eq("limit: cut counter", summary_of(actor).get("messagesCutByLimit"), 3)

    # 11. sinceDate drops older posts without charging
    old = page(block("chan", 1, date="2026-01-01T00:00:00+00:00"),
               block("chan", 2, date="2026-11-05T00:00:00+00:00"))
    actor, error, _ = run({"channels": ["chan"], "sinceDate": "2026-11-01"}, {"https://t.me/s/chan": old})
    eq("since: one post", len(posts(actor)), 1)
    eq("since: skipped counter", summary_of(actor).get("messagesBeforeSinceDate"), 1)

    # 12. an empty input is refused with a human message
    actor, error, _ = run({"channels": []}, {})
    check("empty: refused", isinstance(error, ActorExit))
    check("empty: message mentions channels", "channels" in (actor.failed_with or ""))

    # 13. a silent page is classified, a failed page is a source error
    actor, error, _ = run({"channels": ["quiet"]}, {"https://t.me/s/quiet": EMPTY})
    eq("quiet: unknown landing means not found", channels(actor)[0]["status"],
       "NOT_CHECKED_CHANNEL_NOT_FOUND")

    # 13b. a channel with posts, all older than sinceDate, is EMPTY with a date reason
    old_only = page(block("chan", 1, date="2026-01-01T00:00:00+00:00"))
    actor, error, _ = run({"channels": ["chan"], "sinceDate": "2026-11-01"},
                          {"https://t.me/s/chan": old_only})
    eq("empty range: status", channels(actor)[0]["status"], "NOT_CHECKED_EMPTY")
    check("empty range: reason names the date",
          "2026-11-01" in (channels(actor)[0]["reason"] or ""), str(channels(actor)[0]))

    # 13c. a dataset that refuses rows stops the run with a reason, not silence
    actor, error, _ = run({"channels": ["chan"]}, pages, write_error=True)
    eq("write error: stop reason", summary_of(actor).get("stopReason"), "dataset-write-error")
    check("write error: counted", (summary_of(actor).get("datasetWriteErrors") or 0) >= 3,
          str(summary_of(actor).get("datasetWriteErrors")))
    actor, error, _ = run({"channels": ["boom"]}, {},
                          errors={"https://t.me/s/boom": "connection reset"})
    eq("http failure: source status", channels(actor)[0]["status"], "NOT_CHECKED_SOURCE")
    check("http failure: reason is written", bool(channels(actor)[0]["reason"]),
          str(channels(actor)[0]))
    eq("http failure: no charges", len(actor.charged), 0)

    # 14. counters add up inside the summary
    sm = summary_of(actor)
    check("counters: delivered <= seen", sm.get("messagesDelivered", 0) <= sm.get("messagesSeen", 0),
          str(sm))
    eq("counters: no mismatch", sm.get("counterMismatch"), None)

    # 15. the limit keeps the newest posts, not the top of the page
    newest_pages = {"https://t.me/s/chan": page(*[block("chan", i) for i in range(1, 6)])}
    actor, error, _ = run({"channels": ["chan"], "maxPostsPerChannel": 2}, newest_pages)
    eq("newest: limit keeps the newest ids", [r["messageId"] for r in posts(actor)], [5, 4])

    # 16. across pages: the older page must not win the cut
    p1 = page(*[block("chan", i) for i in range(41, 61)])
    p2 = page(*[block("chan", i) for i in range(21, 41)])
    actor, error, client = run({"channels": ["chan"], "maxPostsPerChannel": 25},
                               {"https://t.me/s/chan": p1, "https://t.me/s/chan?before=41": p2})
    eq("newest: multi-page keeps the newest",
       [r["messageId"] for r in posts(actor)], list(range(60, 35, -1)))
    eq("newest: cut counter counts the rest", summary_of(actor).get("messagesCutByLimit"), 15)

    # 17. rowsDelivered counts every row written, paid ones included
    actor, error, _ = run({"channels": ["chan"]}, pages)
    sm = summary_of(actor)
    eq("rows: delivered equals rows in the dataset", sm.get("rowsDelivered"), len(actor.rows))
    eq("rows: no false lag note", sm.get("datasetItemCountNote"), None)

    # 18. a private invite gets its own status, not "invalid"
    actor, error, _ = run({"channels": ["https://t.me/+AbCdEf123"]}, {})
    eq("invite: status", channels(actor)[0]["status"], "NOT_CHECKED_PRIVATE_INVITE")
    eq("invite: no charges", len(actor.charged), 0)

    # 19. a date the actor cannot read is refused before any request
    actor, error, client = run({"channels": ["chan"], "sinceDate": "01.11.2026"}, pages)
    check("bad date: refused", isinstance(error, ActorExit), str(error))
    check("bad date: message names the format", "YYYY-MM-DD" in (actor.failed_with or ""),
          str(actor.failed_with))
    eq("bad date: not a single request", client.requested, [])
    eq("bad date: no charges", len(actor.charged), 0)

    # 20. the channel row separates seen from delivered and admits the cut
    actor, error, _ = run({"channels": ["chan"], "maxPostsPerChannel": 2}, newest_pages)
    row = channels(actor)[0]
    eq("channel row: seen before the cut", row["messagesSeen"], 5)
    eq("channel row: delivered after the cut", row["messagesDelivered"], 2)
    eq("channel row: truncated flag", row["truncated"], True)
    actor, error, _ = run({"channels": ["chan"]}, pages)
    eq("channel row: not truncated without a cut", channels(actor)[0]["truncated"], False)

    # 21. channel and message statuses are counted apart
    sm = summary_of(actor)
    eq("status: channel bucket", sm.get("byStatusChannels"), {"READ": 1})
    eq("status: message bucket", sm.get("byStatusMessages"), {"DELIVERED": 3})
    check("status: no mixed bucket", "byStatus" not in sm, str(sorted(sm)))

    # 22. the channel counters add up and channelsFailed is gone
    check("counters: channels add up", sm.get("channelsMismatch") is None,
          str(sm.get("channelsMismatch")))
    check("counters: channelsFailed removed", "channelsFailed" not in sm, str(sorted(sm)))

    # 22b. a dataset that refuses the paid row is a write error, not a charge error
    actor, error, _ = run({"channels": ["chan"]}, pages, write_error=True)
    eq("write error: reason", summary_of(actor).get("stopReason"), "dataset-write-error")
    check("write error: not blamed on the charge",
          "charge-error" not in (summary_of(actor).get("stopReason") or ""), str(summary_of(actor)))

    # 23. a failed charge with unreadable metadata must not duplicate the post
    actor, error, _ = run({"channels": ["chan"]}, pages, charge="error", meta_error=True)
    sm = summary_of(actor)
    ids = [r["messageId"] for r in posts(actor)]
    eq("charge unknown: every post appears once", len(ids), len(set(ids)))
    eq("charge unknown: counted", sm.get("messagesChargeUnverified"), 3)
    check("charge unknown: status named",
          sm["byStatusMessages"].get("NOT_CHECKED_CHARGE_UNVERIFIED") == 3,
          str(sm["byStatusMessages"]))

    # 24. the summary is readable by the output schema template and by run id
    actor, error, _ = run({"channels": ["chan"]}, pages)
    keys = sorted(k for k in actor.store.values if "SUMMARY" in k)
    eq("summary: two keys", keys, ["RUN_SUMMARY", "RUN_SUMMARY-testRun123"])

    # 25. the dataset count is not asked once per paid row
    check("count calls: not per row", actor.meta_calls <= 2, str(actor.meta_calls))

    executed = CHECKS - before
    print(f"test_main: {executed} checks, {len(FAILS)} failed")
    for fail in FAILS:
        print("  FAIL:", fail)
    if executed < 40:
        print("  FAIL: too few checks ran")
        return 1
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(run_all())
