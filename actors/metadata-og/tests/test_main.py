"""Money-path checks for the metadata actor: charge, cap, billing, cuts, duplicates.

Run: python tests/test_main.py
The platform and the network are replaced by fakes, so no credits are spent and no
request leaves the machine.
"""
import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.main as m
from src import meta

FAILS = []
CHECKS = [0]


def ok(cond, name, extra=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, extra))


class Stop(Exception):
    pass


class FakeActor:
    """Same surface the code touches: input, push_data, pricing, fail, logs, stores."""

    def __init__(self, actor_input, pages, charges=None, pricing=None, write_failures=0,
                 info_fails=False):
        self.input = actor_input
        self.pages = pages
        self.charges = list(charges or [])
        self.rows = []
        self.reads = []
        self.write_failures = write_failures
        self.info_fails = info_fails
        self.charged = 0
        self.charge_calls = 0
        self.failed = None
        self.summary = {}
        self.pricing = pricing
        self.log = types.SimpleNamespace(info=lambda *a, **k: None,
                                         warning=lambda *a, **k: None,
                                         error=lambda *a, **k: None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_input(self):
        return self.input

    async def push_data(self, row, charged_event_name=None):
        """Mirrors apify 4.2.0: the row is written first, the charge happens in the same call.

        A cap that lets nothing through writes no row at all; a failing charge raises after
        the row is already stored, so the buyer has the data and pays nothing.
        """
        if self.write_failures:
            self.write_failures -= 1
            raise RuntimeError("the dataset refused the write")
        if not charged_event_name:
            self.rows.append(row)
            return types.SimpleNamespace(charged_count=0, event_charge_limit_reached=False)
        self.charge_calls += 1
        step = self.charges.pop(0) if self.charges else (1, False)
        if step == (0, True):
            return types.SimpleNamespace(charged_count=0, event_charge_limit_reached=True)
        self.rows.append(row)
        if isinstance(step, Exception):
            raise step
        self.charged += step[0]
        return types.SimpleNamespace(charged_count=step[0], event_charge_limit_reached=step[1])

    def get_charging_manager(self):
        """Mirrors apify 4.x: prices live on the charging manager, not on Actor itself."""
        fake = self

        class Manager:
            def get_pricing_info(self):
                if fake.pricing is None:
                    raise AttributeError("a local run carries no pricing info")
                return fake.pricing

        return Manager()

    async def open_dataset(self):
        fake = self

        class Dataset:
            async def get_metadata(self):
                if fake.info_fails:
                    raise RuntimeError("the dataset metadata is not readable")
                return types.SimpleNamespace(item_count=len(fake.rows),
                                             clean_item_count=len(fake.rows))

        return Dataset()

    async def fail(self, status_message=None, exit_code=1, exception=None):
        self.failed = status_message
        raise Stop(status_message)

    def get_env(self):
        return {"run_id": "test-run"}

    async def open_key_value_store(self, name=None):
        fake = self

        class Store:
            async def get_value(self, key):
                return fake.summary.get(key)

            async def set_value(self, key, value):
                fake.summary[key] = value

        return Store()


def page(status="EXTRACTED", title="Title", description="Desc", image="https://a.test/i.png"):
    row = meta._fail(status, "" if status == "EXTRACTED" else "an answer")
    if status == "EXTRACTED":
        row.update({"status": "EXTRACTED", "title": title, "description": description,
                    "openGraph": {"image": image},
                    "preview": {"ready": True, "issues": []}})
    return row


def run(actor_input, pages, charges=None, pricing=None, write_failures=0, info_fails=False):
    fake = FakeActor(actor_input, pages, charges, pricing, write_failures, info_fails)
    old_actor, old_read, old_client = m.Actor, m.meta.read_page, m.meta.make_client

    async def fake_read(client, url, **kwargs):
        fake.reads.append(url)
        if url not in fake.pages:
            return meta._fail(meta.STATUS_UNREACHABLE, "no page in this stand")
        return dict(fake.pages[url])

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    m.Actor = fake
    m.meta.read_page = fake_read
    m.meta.make_client = lambda **kwargs: DummyClient()
    failed = None
    try:
        asyncio.run(m.main())
    except Stop as exc:
        failed = str(exc)
    finally:
        m.Actor, m.meta.read_page, m.meta.make_client = old_actor, old_read, old_client
    return fake, failed


def test_money_equals_delivery():
    pages = {"https://a.test/1": page(), "https://a.test/2": page(), "https://a.test/3": page("UNREACHABLE")}
    fake, failed = run({"urls": ["https://a.test/1", "https://a.test/2", "https://a.test/3"]},
                       pages, [(1, False), (1, False)])
    statuses = [r["status"] for r in fake.rows]
    ok(statuses == ["EXTRACTED", "EXTRACTED", "UNREACHABLE"], "status per page", statuses)
    ok([r["billable"] for r in fake.rows] == [True, True, False], "only delivered pages are billable")
    ok(fake.summary["RUN_SUMMARY"]["chargedEvents"] == 2, "two charges for two pages")
    ok(fake.summary["RUN_SUMMARY"]["delivered"] == 2, "two rows with metadata")
    ok(fake.summary["RUN_SUMMARY"]["rowsDelivered"] == 3, "every page has a row")
    ok(failed is None, "a readable list is not a failed run", failed)


def test_cap_on_first_charge():
    pages = {"https://a.test/1": page(), "https://a.test/2": page()}
    fake, failed = run({"urls": ["https://a.test/1", "https://a.test/2"]}, pages, [(0, True)])
    ok([r["status"] for r in fake.rows] == ["NOT_CHECKED_CAP", "NOT_CHECKED_CAP"], "cap on the first charge",
       [r["status"] for r in fake.rows])
    summary = fake.summary["RUN_SUMMARY"]
    ok(summary["notChecked"] == 2, "both pages counted as not checked", summary["notChecked"])
    ok(summary["processed"] == 0, "capped pages are not processed")
    ok(summary["stopReason"] == "spending-cap", "stop reason", summary["stopReason"])
    ok(failed and "spending cap" in failed.lower(), "the failure names the cap", failed)


def test_cap_after_the_paid_row():
    pages = {"https://a.test/1": page(), "https://a.test/2": page()}
    fake, _ = run({"urls": ["https://a.test/1", "https://a.test/2"]}, pages, [(1, True)])
    ok(fake.rows[0]["status"] == "EXTRACTED" and fake.rows[0]["billable"] is True, "the paid page is delivered")
    ok(fake.rows[1]["status"] == "NOT_CHECKED_CAP", "the next one is capped", fake.rows[1]["status"])
    ok(fake.summary["RUN_SUMMARY"]["chargedEvents"] == 1, "one charge, not two")


def test_charge_errors_stop_after_two():
    pages = {"https://a.test/%d" % i: page() for i in (1, 2, 3)}
    boom = RuntimeError("billing blew up")
    fake, _ = run({"urls": ["https://a.test/1", "https://a.test/2", "https://a.test/3"]},
                  pages, [boom, boom])
    statuses = [r["status"] for r in fake.rows]
    ok(statuses == ["EXTRACTED", "EXTRACTED", "NOT_CHECKED_BILLING"], "two strikes then stop", statuses)
    summary = fake.summary["RUN_SUMMARY"]
    ok(summary["stopReason"] == "charge-error", "stop reason", summary["stopReason"])
    ok(len(summary["chargeErrors"]) == 2, "errors are listed, not swallowed", summary["chargeErrors"])
    ok(summary["deliveredUncharged"] >= 2, "the free deliveries are counted", summary["deliveredUncharged"])


def test_max_urls_cut_is_reported():
    pages = {"https://a.test/%d" % i: page() for i in (1, 2, 3)}
    fake, _ = run({"urls": ["https://a.test/1", "https://a.test/2", "https://a.test/3"], "maxUrls": 2},
                  pages, [(1, False), (1, False)])
    ok(len(fake.rows) == 3, "every input line has a row", len(fake.rows))
    ok(fake.rows[2]["status"] == "NOT_CHECKED_LIMIT", "the cut one is named", fake.rows[2]["status"])
    summary = fake.summary["RUN_SUMMARY"]
    ok(summary["requested"] == 3, "requested counts what was sent")
    ok(summary["cutByMaxUrls"] == 1, "the cut is counted", summary["cutByMaxUrls"])
    ok(summary["requested"] == summary["processed"] + summary["notChecked"], "books balance",
       (summary["processed"], summary["notChecked"]))


def test_empty_input_fails_loudly():
    fake, failed = run({"urls": []}, {})
    ok(failed is not None and "Nothing was charged" in failed, "empty input stops with a plain message", failed)
    ok(fake.rows == [], "and writes no rows")


def test_all_dead_links_fail():
    pages = {"https://a.test/1": page("UNREACHABLE"), "https://a.test/2": page("HTTP_ERROR")}
    fake, failed = run({"urls": ["https://a.test/1", "https://a.test/2"]}, pages, [])
    ok(failed is not None, "a list of dead links is not a job done", failed)
    ok("UNREACHABLE" in (failed or "") and "HTTP_ERROR" in (failed or ""), "the message lists statuses", failed)
    ok(fake.summary["RUN_SUMMARY"]["chargedEvents"] == 0, "nothing charged")


def test_all_no_metadata_is_an_answer():
    pages = {"https://a.test/1": page("NO_METADATA"), "https://a.test/2": page("NO_METADATA")}
    fake, failed = run({"urls": ["https://a.test/1", "https://a.test/2"]}, pages, [])
    ok(failed is None, "a batch of browser shells does not fail the run", failed)
    ok([r["status"] for r in fake.rows] == ["NO_METADATA", "NO_METADATA"], "each shell is reported")
    ok(fake.summary["RUN_SUMMARY"]["metadataMissing"] == 2, "and counted",
       fake.summary["RUN_SUMMARY"]["metadataMissing"])


def test_duplicate_page_is_free():
    pages = {"https://a.test/x": page()}
    fake, _ = run({"urls": ["https://a.test/x", "a.test/x"]}, pages, [(1, False)])
    ok(fake.reads == ["https://a.test/x"], "the page is read once", fake.reads)
    ok(len(fake.rows) == 2, "both lines have rows", len(fake.rows))
    ok(fake.rows[1]["billable"] is False, "the repeat is not billable")
    ok(fake.rows[1].get("duplicateOf") == 1, "the repeat names the row it repeats", fake.rows[1].get("duplicateOf"))
    summary = fake.summary["RUN_SUMMARY"]
    ok(summary["chargedEvents"] == 1, "one charge for one page", summary["chargedEvents"])
    ok(summary["duplicatesSkipped"] == 1, "the duplicate is counted", summary["duplicatesSkipped"])
    ok(summary["delivered"] == 2, "both rows carry metadata")


def test_non_ppe_delivers_free():
    pricing = types.SimpleNamespace(is_pay_per_event=False, per_event_prices={})
    pages = {"https://a.test/1": page(), "https://a.test/2": page()}
    fake, failed = run({"urls": ["https://a.test/1", "https://a.test/2"]}, pages, [], pricing=pricing)
    summary = fake.summary["RUN_SUMMARY"]
    ok(summary["billingActive"] is False, "the run knows it cannot be billed", summary["billingActive"])
    ok(summary["chargedEvents"] == 0, "nothing is charged")
    ok(summary["deliveredUncharged"] == 2, "both free deliveries are counted", summary["deliveredUncharged"])
    ok(fake.charge_calls == 0, "charge is never called", fake.charge_calls)
    ok(failed is None, "a free run is not a failed run", failed)


def test_dataset_write_error_is_not_a_delivery():
    pages = {"https://a.test/1": page()}
    fake, failed = run({"urls": ["https://a.test/1"]}, pages, [], write_failures=5)
    summary = fake.summary["RUN_SUMMARY"]
    # A refused row never lands in the dataset, so it is counted in the summary, not in rows.
    ok(summary["byStatus"].get("NOT_CHECKED_ERROR") == 1, "the refused row is named in the summary",
       summary["byStatus"])
    ok(summary["stopReason"] == "dataset-write-error", "stop reason", summary["stopReason"])
    ok(summary["datasetWriteErrors"] == 1, "counted once", summary["datasetWriteErrors"])
    ok(summary["requested"] == summary["processed"] + summary["notChecked"], "books balance",
       (summary["processed"], summary["notChecked"]))
    ok(failed is not None and "dataset" in failed.lower(), "the failure says the dataset refused", failed)


def test_summary_has_a_run_key():
    pages = {"https://a.test/1": page()}
    fake, _ = run({"urls": ["https://a.test/1"]}, pages, [(1, False)])
    ok("RUN_SUMMARY" in fake.summary, "the shared key is written")
    ok("RUN_SUMMARY-test-run" in fake.summary, "and the per-run key, because the store is shared")


def test_schema_allows_no_limit():
    schema = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         ".actor", "input_schema.json"), encoding="utf-8"))
    field = schema["properties"]["maxUrls"]
    ok(field["minimum"] == 0, "0 is allowed, because 0 means no limit", field.get("minimum"))
    ok("no limit" in field["description"].lower(), "the schema says what 0 does", field["description"])
    ok(schema.get("required") == [], "no required field, so Run without input must fail loudly, not crash")


def test_redirect_rows_are_free_and_fail_loudly():
    pages = {"https://a.test/r": page("REDIRECT"), "https://a.test/r2": page("REDIRECT")}
    fake, failed = run({"urls": ["https://a.test/r", "https://a.test/r2"]}, pages, [])
    ok([r["status"] for r in fake.rows] == ["REDIRECT", "REDIRECT"], "a redirect is a row, not a page",
       [r["status"] for r in fake.rows])
    ok(all(r["billable"] is False for r in fake.rows), "a redirect is not billable")
    ok(fake.charge_calls == 0, "no charge is even attempted", fake.charge_calls)
    ok(failed is not None and "REDIRECT" in failed, "a list of redirects is not a job done", failed)


JSONLD_ONLY = ('<html><head><script type="application/ld+json">'
               '{"@context":"https://schema.org","@type":"Organization","name":"x"}</script>'
               '</head><body><div id="root"></div></body></html>')


def jsonld_only_page():
    """A real page read by the real parser: its only metadata is one JSON-LD block."""
    return meta.parse_page(JSONLD_ONLY, "https://a.test/j")


def test_jsonld_only_page_is_free_when_the_block_is_off():
    probe = jsonld_only_page()
    ok(probe["status"] == "EXTRACTED" and probe["jsonLd"]["count"] == 1,
       "the stand really is a page whose only metadata is JSON-LD",
       (probe["status"], probe["jsonLd"], probe["title"], probe["openGraph"]))
    pages = {"https://a.test/j": jsonld_only_page()}
    fake, _ = run({"urls": ["https://a.test/j"], "includeJsonLd": False}, pages, [])
    row = fake.rows[0]
    ok(row["status"] == "NO_METADATA", "a page that only had JSON-LD is not sold when it is dropped",
       row["status"])
    ok(row["billable"] is False, "and it is not billable", row["billable"])
    ok(row["jsonLd"]["count"] == 0, "the block is really dropped", row["jsonLd"])
    ok(fake.charge_calls == 0, "no charge was attempted", fake.charge_calls)
    summary = fake.summary["RUN_SUMMARY"]
    ok(summary["chargedEvents"] == 0, "nothing charged", summary["chargedEvents"])
    ok(summary["delivered"] == 0, "nothing delivered", summary["delivered"])
    ok(summary["metadataMissing"] == 1, "counted as no metadata", summary["metadataMissing"])


def test_jsonld_only_page_is_charged_when_it_is_kept():
    pages = {"https://a.test/j": jsonld_only_page()}
    fake, _ = run({"urls": ["https://a.test/j"]}, pages, [(1, False)])
    ok(fake.rows[0]["status"] == "EXTRACTED", "JSON-LD alone is metadata by default", fake.rows[0]["status"])
    ok(fake.summary["RUN_SUMMARY"]["chargedEvents"] == 1, "and it is charged once",
       fake.summary["RUN_SUMMARY"]["chargedEvents"])


def test_duplicate_dead_link_keeps_the_real_reason():
    pages = {"https://a.test/dead": page("UNREACHABLE")}
    fake, _ = run({"urls": ["https://a.test/dead", "a.test/dead"]}, pages, [])
    ok(len(fake.rows) == 2, "both lines have rows", len(fake.rows))
    ok(fake.rows[1]["status"] == "UNREACHABLE", "the repeat has the same answer", fake.rows[1]["status"])
    ok("delivered free" not in fake.rows[1]["reason"], "a dead link is not called delivered",
       fake.rows[1]["reason"])
    ok("UNREACHABLE" in fake.rows[1]["reason"], "the real diagnosis stays", fake.rows[1]["reason"])


def test_summary_key_names_the_actual_run():
    pages = {"https://a.test/1": page()}
    old = os.environ.get("ACTOR_RUN_ID")
    try:
        os.environ["ACTOR_RUN_ID"] = "env-run-123"
        fake, _ = run({"urls": ["https://a.test/1"]}, pages, [(1, False)])
        ok("RUN_SUMMARY-env-run-123" in fake.summary, "ACTOR_RUN_ID names the per-run key",
           sorted(fake.summary))
        os.environ.pop("ACTOR_RUN_ID", None)
        fake, _ = run({"urls": ["https://a.test/1"]}, pages, [(1, False)])
        ok("RUN_SUMMARY-test-run" in fake.summary, "without env the real get_env key run_id is used",
           sorted(fake.summary))
    finally:
        if old is None:
            os.environ.pop("ACTOR_RUN_ID", None)
        else:
            os.environ["ACTOR_RUN_ID"] = old


def run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                FAILS.append("%s raised %s: %s" % (name, type(exc).__name__, exc))
    print("checks: %d, failed: %d" % (CHECKS[0], len(FAILS)))
    for fail in FAILS:
        print("  FAIL", fail)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(run_all())
