"""Money-path checks for the PDF extractor: charge, cap, billing, cuts.

Run: python tests/test_main.py
The platform is replaced by a fake Actor, so no credits are spent.
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf

from src import pdf
import src.main as m

FAILS = []
CHECKS = [0]


def ok(cond, name, extra=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, extra))


def make_pdf(pages=("page one text", "page two text")):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 100), text, fontsize=14)
    return doc.tobytes()


class Stop(Exception):
    pass


class FakeActor:
    """Same surface the code touches: input, push_data, charge, fail, logs, stores."""

    def __init__(self, actor_input, files, charges=None, pricing=None, write_failures=0,
                 info_fails=False):
        self.input = actor_input
        self.files = files
        self.charges = list(charges or [])
        self.rows = []
        self.write_failures = write_failures
        self.info_fails = info_fails
        self.charging_calls = 0
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
        self.charging_calls += 1
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
            # apify 4.2.0 has no Dataset.get_info; the real getter is get_metadata().
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
                return fake.files.get(key)

            async def set_value(self, key, value):
                fake.summary[key] = value

        return Store()


def run(actor_input, files, charges=None, pricing=None, write_failures=0, info_fails=False):
    fake = FakeActor(actor_input, files, charges, pricing, write_failures, info_fails)
    old = m.Actor
    m.Actor = fake
    failed = None
    try:
        asyncio.run(m.main())
    except Stop as exc:
        failed = str(exc)
    finally:
        m.Actor = old
    return fake, failed


def test_money_equals_delivery():
    files = {"ok.pdf": make_pdf(), "html.txt": b"<html><body>hello</body></html>", "scan.pdf": make_pdf(("", ""))}
    fake, failed = run({"urls": ["ok.pdf", "html.txt", "scan.pdf"]}, files, [(1, False)] * 3)
    statuses = [r["status"] for r in fake.rows]
    ok(statuses == ["EXTRACTED", "INVALID_PDF", "NO_TEXT_LAYER"], "statuses per row", statuses)
    ok(sum(r["billable"] for r in fake.rows) == 1, "only the read one is charged")
    ok(fake.summary["RUN_SUMMARY"]["chargedEvents"] == 1, "summary charged", fake.summary.get("RUN_SUMMARY"))
    ok(fake.summary["RUN_SUMMARY"]["delivered"] == 1, "summary delivered")
    ok("is not a PDF" in fake.rows[1]["reason"], "html says plainly it is not a PDF", fake.rows[1]["reason"])
    ok(failed is None, "a readable document does not fail the run", failed)


def test_cap_before_delivery_counts_as_not_checked():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf()}
    fake, failed = run({"urls": ["a.pdf", "b.pdf"]}, files, [(0, True), (1, False)])
    ok([r["status"] for r in fake.rows] == ["NOT_CHECKED_CAP", "NOT_CHECKED_CAP"], "cap on the first charge",
       [r["status"] for r in fake.rows])
    s = fake.summary["RUN_SUMMARY"]
    ok(s["notChecked"] == 2, "both unread rows counted as not checked", s["notChecked"])
    ok(s["processed"] == 0, "capped rows are not processed", s["processed"])
    ok(s["stopReason"] == "spending-cap", "stop reason")
    ok(failed and "spending cap" in failed.lower(), "the failure names the cap", failed)


def test_charge_errors_stop_after_two():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf(), "c.pdf": make_pdf()}
    boom = RuntimeError("billing blew up")
    fake, _ = run({"urls": ["a.pdf", "b.pdf", "c.pdf"]}, files, [boom, boom])
    ok([r["status"] for r in fake.rows] == ["EXTRACTED", "EXTRACTED", "NOT_CHECKED_BILLING"], "two strikes then stop",
       [r["status"] for r in fake.rows])
    ok(fake.summary["RUN_SUMMARY"]["stopReason"] == "charge-error", "stop reason")
    ok(len(fake.summary["RUN_SUMMARY"]["chargeErrors"]) == 2, "errors are listed, not swallowed")


def test_max_documents_cut_is_reported():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf(), "c.pdf": make_pdf()}
    fake, _ = run({"urls": ["a.pdf", "b.pdf", "c.pdf"], "maxDocuments": 2}, files, [(1, False), (1, False)])
    ok(len(fake.rows) == 3, "every link has a row", len(fake.rows))
    ok(fake.rows[2]["status"] == "NOT_CHECKED_LIMIT", "the cut one is named", fake.rows[2]["status"])
    ok(fake.summary["RUN_SUMMARY"]["requested"] == 3, "requested counts what was sent")


def test_cap_on_delivery_stops_after_the_paid_row():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf()}
    fake, _ = run({"urls": ["a.pdf", "b.pdf"]}, files, [(1, True), (1, False)])
    ok(fake.rows[0]["billable"] is True, "the paid row is delivered")
    ok(fake.rows[1]["status"] == "NOT_CHECKED_CAP", "the next one is capped", fake.rows[1]["status"])


def test_empty_input_fails_loudly():
    fake, failed = run({"urls": []}, {})
    ok(failed is not None and "Nothing was charged" in failed, "empty input stops with a plain message", failed)
    ok(fake.rows == [], "and writes no rows")



def test_all_scans_is_an_answer_not_a_failure():
    files = {"a.pdf": make_pdf(("",)), "b.pdf": make_pdf(("",))}
    fake, failed = run({"urls": ["a.pdf", "b.pdf"]}, files, [])
    ok(failed is None, "a batch of scans does not fail the run", failed)
    ok([r["status"] for r in fake.rows] == ["NO_TEXT_LAYER", "NO_TEXT_LAYER"], "each scan is reported",
       [r["status"] for r in fake.rows])
    ok(fake.summary["RUN_SUMMARY"]["chargedEvents"] == 0, "nothing is charged for scans")


def test_unreadable_batch_names_the_dominant_status():
    # Private targets are a refusal, not an answer about a document: a batch of them is a run
    # that could not read anything, and the message has to name every status it saw.
    fake, failed = run({"urls": ["http://127.0.0.1/a.pdf", "http://10.1.2.3/b.pdf"]}, {}, [])
    ok(failed is not None and "No document could be read" in failed, "nothing readable fails the run", failed)
    ok("PRIVATE_TARGET" in (failed or ""), "the message names the status", failed)
    ok(len(fake.rows) == 2, "both links still have rows", len(fake.rows))
    ok(not any(r["billable"] for r in fake.rows), "and nothing is charged")


def test_cut_rows_keep_their_own_link():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf(), "c.pdf": make_pdf()}
    fake, _ = run({"urls": ["a.pdf", "b.pdf", "c.pdf"], "maxDocuments": 2}, files, [(1, False), (1, False)])
    ok(fake.rows[2]["source"] == "c.pdf", "the cut row names its own link", fake.rows[2]["source"])
    ok(fake.summary["RUN_SUMMARY"]["rowsDelivered"] == 3, "every input got a row",
       fake.summary["RUN_SUMMARY"]["rowsDelivered"])


def test_billing_off_keeps_delivering_for_free():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf()}
    fake, failed = run({"urls": ["a.pdf", "b.pdf"]}, files, [(0, False)])
    ok([r["status"] for r in fake.rows] == ["EXTRACTED", "EXTRACTED"], "billing off does not stop the text",
       [r["status"] for r in fake.rows])
    ok(all(r["billable"] is True for r in fake.rows), "every delivered row is billable")
    ok(fake.rows[1].get("billingInactive") is True, "later rows say billing was inactive")
    ok(fake.charge_calls == 1, "billing is probed once, not per row", fake.charge_calls)
    ok(fake.summary["RUN_SUMMARY"]["deliveredUncharged"] >= 1, "the free row is counted")
    ok(fake.summary["RUN_SUMMARY"]["stopReason"] is None, "a free run is not a stopped run")
    ok(fake.summary["RUN_SUMMARY"]["billingActive"] is False, "the summary says billing is off")
    ok(failed is None, "a free run is not a failure", failed)

def test_charge_failing_keeps_the_row_delivered():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf()}
    boom = RuntimeError("billing blew up")
    fake, _ = run({"urls": ["a.pdf", "b.pdf"]}, files, [boom])
    ok(len(fake.rows) == 2, "a failed charge does not swallow the row", len(fake.rows))
    ok(fake.summary["RUN_SUMMARY"]["deliveredUncharged"] == 1, "the free delivery is counted",
       fake.summary["RUN_SUMMARY"]["deliveredUncharged"])
    ok(fake.summary["RUN_SUMMARY"]["chargedEvents"] == 1,
       "the next document is still charged: one failure does not stop billing",
       fake.summary["RUN_SUMMARY"]["chargedEvents"])


def test_cut_of_zero_means_no_limit():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf(), "c.pdf": make_pdf()}
    fake, _ = run({"urls": ["a.pdf", "b.pdf", "c.pdf"], "maxDocuments": 0}, files, [(1, False)] * 3)
    s = fake.summary["RUN_SUMMARY"]
    ok([r["status"] for r in fake.rows] == ["EXTRACTED"] * 3, "0 is not a cut", [r["status"] for r in fake.rows])
    ok(s["maxDocuments"] == 3 and s["cutByMaxDocuments"] == 0, "the summary shows no limit", s["maxDocuments"])


def test_a_typo_list_is_not_green():
    # Orestes 0.9: a batch of links that are not PDFs used to stay SUCCEEDED with zero
    # documents, so a list of typos read as a job done. A green run must mean at least one
    # document, and the reason still names every link.
    files = {"html.txt": b"<html><body>hi</body></html>"}
    fake, failed = run({"urls": ["html.txt", "no-such-file.pdf"]}, files, [])
    statuses = [r["status"] for r in fake.rows]
    ok(statuses == ["INVALID_PDF", "UNREACHABLE"], "each link gets its own answer", statuses)
    ok(failed is not None and "No document could be read" in failed,
       "a batch of links that are not PDFs stops the run", failed)
    ok("INVALID_PDF" in (failed or "") and "UNREACHABLE" in (failed or ""),
       "the message names both statuses", failed)
    ok(not any(r["billable"] for r in fake.rows), "and nothing is charged for it")


def test_refused_paid_write_retries_and_keeps_the_text():
    # Orestes 0.9: one refused write lost the text already extracted. The SDK stores a row
    # before it charges, so a refusal took no money and the same row can be retried.
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf()}
    fake, failed = run({"urls": ["a.pdf", "b.pdf"]}, files, [(1, False), (1, False)], write_failures=1)
    s = fake.summary["RUN_SUMMARY"]
    statuses = [r["status"] for r in fake.rows]
    ok(statuses == ["EXTRACTED", "EXTRACTED"], "both documents still come back with text", statuses)
    ok(all(r["pages"] and r["pages"][0]["text"] for r in fake.rows), "the retried row kept its text")
    ok(s["chargedEvents"] == 1, "the refused charge is not billed on the retry", s["chargedEvents"])
    ok(s["deliveredUncharged"] == 1, "and the row that lost its charge goes out as free", s["deliveredUncharged"])
    ok(s["datasetWriteErrors"] == 0, "the retry is not an error", s["datasetWriteErrors"])
    ok(failed is None, "the run finishes", failed)


def test_pricing_without_our_event_runs_free():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf()}
    pricing = types.SimpleNamespace(is_pay_per_event=True, per_event_prices={})
    fake, failed = run({"urls": ["a.pdf", "b.pdf"]}, files, [], pricing=pricing)
    ok([r["status"] for r in fake.rows] == ["EXTRACTED", "EXTRACTED"], "text is still delivered",
       [r["status"] for r in fake.rows])
    ok(fake.charge_calls == 0, "no charge is attempted when the event is not priced", fake.charge_calls)
    ok(fake.summary["RUN_SUMMARY"]["billingActive"] is False, "the summary says why it is free")
    ok(fake.summary["RUN_SUMMARY"]["stopReason"] is None, "an unpriced event is not a failure")
    ok(failed is None, "a free run is not a failure", failed)


def test_pricing_is_read_from_the_charging_manager():
    files = {"a.pdf": make_pdf()}
    pricing = types.SimpleNamespace(is_pay_per_event=True,
                                    per_event_prices={m.CHARGE_EVENT: 0.01})
    fake, failed = run({"urls": ["a.pdf"]}, files, [(1, False)], pricing=pricing)
    ok(fake.charging_calls == 1, "the price is asked once, through the charging manager", fake.charging_calls)
    ok(fake.charge_calls == 1, "a priced run is charged", fake.charge_calls)
    ok(failed is None, "a priced run finishes", failed)


def test_dataset_refusal_keeps_every_row():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf()}
    fake, failed = run({"urls": ["a.pdf", "b.pdf"]}, files, [(1, False), (1, False)], write_failures=99)
    s = fake.summary["RUN_SUMMARY"]
    ok(fake.rows == [], "nothing reached the dataset", len(fake.rows))
    ok(s["chargedEvents"] == 0, "nothing was charged for a row that never landed", s["chargedEvents"])
    ok(s["deliveredUncharged"] == 0, "a lost row is not a free delivery", s["deliveredUncharged"])
    ok(s["notChecked"] == 2, "both links are answered in the summary", s["notChecked"])
    ok(s["datasetWriteErrors"] == 2, "one refusal per row, not two", s.get("datasetWriteErrors"))
    ok(s["stopReason"] == "dataset-write-error", "the summary names the dataset", s["stopReason"])
    ok(s["delivered"] == 0 and s["rowsDelivered"] == 0, "delivery counts what the dataset holds",
       (s["delivered"], s["rowsDelivered"]))
    ok(s["datasetItemCount"] == 0, "the summary reports the dataset size", s.get("datasetItemCount"))
    ok(s["requested"] == s["processed"] + s["notChecked"], "the books balance",
       (s["requested"], s["processed"], s["notChecked"]))
    ok(failed and "dataset refused" in failed.lower(), "the run fails with a plain reason", failed)


def test_refused_tail_is_counted_once_not_twice():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf(), "c.pdf": make_pdf(), "d.pdf": make_pdf()}
    fake, _ = run({"urls": ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]}, files, [], write_failures=99)
    s = fake.summary["RUN_SUMMARY"]
    ok(s["notChecked"] == 4, "each of the four links is counted once", s["notChecked"])
    ok(s["datasetWriteErrors"] == 4, "four refused rows, four counts, no doubles", s["datasetWriteErrors"])
    ok(s["processed"] == 0, "a row nobody read is not processed", s["processed"])
    ok(s["requested"] == s["processed"] + s["notChecked"],
       "requested == processed + notChecked", (s["requested"], s["processed"], s["notChecked"]))


def test_transient_write_refusal_is_retried_and_not_lost():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf()}
    fake, failed = run({"urls": ["a.pdf", "b.pdf"]}, files, [(1, False), (1, False)], write_failures=1)
    statuses = [r["status"] for r in fake.rows]
    s = fake.summary["RUN_SUMMARY"]
    ok(len(statuses) == 2, "both links have a row", statuses)
    ok(s["rowsDelivered"] == 2, "the retried row still lands", s["rowsDelivered"])
    ok(s["datasetWriteErrors"] == 0, "a write that succeeded on the retry is not an error",
       s["datasetWriteErrors"])
    ok(s["stopReason"] is None, "one transient refusal is not a stopped run", s["stopReason"])
    ok(failed is None, "the run finishes", failed)


def test_one_lost_row_does_not_lose_the_tail():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf(), "c.pdf": make_pdf()}
    fake, failed = run({"urls": ["a.pdf", "b.pdf", "c.pdf"]}, files, [(1, False)] * 3, write_failures=3)
    statuses = [r["status"] for r in fake.rows]
    s = fake.summary["RUN_SUMMARY"]
    ok(statuses == ["EXTRACTED", "EXTRACTED"], "the rest is still read and delivered", statuses)
    ok(s["delivered"] == 2, "two documents reached the buyer", s["delivered"])
    ok(s["datasetWriteErrors"] == 1, "one lost row, one count", s["datasetWriteErrors"])
    ok(s["notChecked"] == 1, "the lost row is still answered in the summary", s["notChecked"])
    ok(s["stopReason"] == "dataset-write-error", "the summary names the dataset", s["stopReason"])
    ok(s["requested"] == s["processed"] + s["notChecked"], "the books balance",
       (s["requested"], s["processed"], s["notChecked"]))
    ok(failed is None, "a run that delivered is not failed", failed)


def test_dataset_size_comes_from_get_metadata():
    files = {"a.pdf": make_pdf()}
    fake, _ = run({"urls": ["a.pdf"]}, files, [(1, False)])
    s = fake.summary["RUN_SUMMARY"]
    ok(s["datasetItemCount"] == 1, "the size is read, not guessed", s["datasetItemCount"])
    ok(s["datasetItemCount"] is not None, "a missing get_info must not read as no rows")


def test_every_refused_write_still_names_every_link():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf(), "c.pdf": make_pdf()}
    fake, failed = run({"urls": ["a.pdf", "b.pdf", "c.pdf"]}, files, [], write_failures=99)
    s = fake.summary["RUN_SUMMARY"]
    ok(s["notChecked"] == 3, "three links, three answers in the summary", s["notChecked"])
    ok(s["requested"] == s["processed"] + s["notChecked"], "the books balance",
       (s["requested"], s["processed"], s["notChecked"]))
    ok(s["datasetWriteErrors"] == 3, "three rows, three refusals, no double count", s["datasetWriteErrors"])
    ok(failed is not None, "a run that stored nothing fails loudly", failed)


def test_unreadable_dataset_count_is_not_called_lost():
    files = {"a.pdf": make_pdf()}
    fake, _ = run({"urls": ["a.pdf"]}, files, [(1, False)], write_failures=1, info_fails=True)
    s = fake.summary["RUN_SUMMARY"]
    ok(s["datasetWriteUnverified"] == 1, "the unverifiable write is named", s.get("datasetWriteUnverified"))
    ok(s["stopReason"] is None, "a row is not declared lost on a guess", s["stopReason"])
    ok(s["deliveredUncharged"] == 1, "the row is treated as delivered and unpaid", s["deliveredUncharged"])
    ok(s["billingActive"] is True, "one failure does not stop billing", s["billingActive"])


def test_free_run_counts_every_delivered_row():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf(), "c.pdf": make_pdf()}
    pricing = types.SimpleNamespace(is_pay_per_event=False, per_event_prices={})
    fake, _ = run({"urls": ["a.pdf", "b.pdf", "c.pdf"]}, files, [], pricing=pricing)
    s = fake.summary["RUN_SUMMARY"]
    ok(s["delivered"] == 3, "all three documents are delivered", s["delivered"])
    ok(s["deliveredUncharged"] == 3, "every free delivery is counted", s["deliveredUncharged"])
    ok(all(r.get("billingInactive") is True for r in fake.rows),
       "a row written while billing was known off says so", [r.get("billingInactive") for r in fake.rows])
    ok(s["chargedEvents"] == 0, "nothing is charged")


def test_charge_failure_is_counted_in_the_summary_not_on_the_row():
    files = {"a.pdf": make_pdf()}
    boom = RuntimeError("billing blew up")
    fake, _ = run({"urls": ["a.pdf"]}, files, [boom])
    s = fake.summary["RUN_SUMMARY"]
    ok(s["delivered"] == 1, "the row is delivered", s["delivered"])
    ok(s["deliveredUncharged"] == 1, "and counted as unpaid", s["deliveredUncharged"])
    ok(fake.rows[0].get("billingInactive") is None,
       "the row was stored before the failure, so the summary carries the truth")
    ok(s["billingActive"] is True, "one failure does not switch billing off", s["billingActive"])


def test_books_balance_across_scenarios():
    files = {"a.pdf": make_pdf(), "b.pdf": make_pdf(), "c.pdf": make_pdf()}
    scenarios = [
        ("cap", {"urls": ["a.pdf", "b.pdf", "c.pdf"]}, [(1, False), (0, True)]),
        ("limit", {"urls": ["a.pdf", "b.pdf", "c.pdf"], "maxDocuments": 2}, [(1, False)]),
        ("plain", {"urls": ["a.pdf", "b.pdf", "c.pdf"]}, [(1, False)] * 3),
    ]
    for name, actor_input, charges in scenarios:
        fake, _ = run(actor_input, files, charges)
        s = fake.summary["RUN_SUMMARY"]
        ok(s["requested"] == s["processed"] + s["notChecked"],
           "%s: requested == processed + notChecked" % name,
           (s["requested"], s["processed"], s["notChecked"]))
        ok(s["rowsDelivered"] <= s["requested"], "%s: no invented rows" % name, s["rowsDelivered"])


def test_max_documents_zero_is_reachable_in_the_schema():
    import json
    schema = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         ".actor", "input_schema.json"), encoding="utf-8"))
    field = schema["properties"]["maxDocuments"]
    ok(field["minimum"] == 0, "0 is allowed, because 0 means no limit", field.get("minimum"))
    ok("no limit" in field["description"].lower(), "the schema says what 0 does", field["description"])


def test_summary_key_names_the_actual_run():
    files = {"a.pdf": make_pdf()}
    old = os.environ.get("ACTOR_RUN_ID")
    try:
        os.environ["ACTOR_RUN_ID"] = "env-run-123"
        fake, _ = run({"urls": ["a.pdf"]}, files, [(1, False)])
        ok("RUN_SUMMARY-env-run-123" in fake.summary, "ACTOR_RUN_ID names the per-run key",
           sorted(fake.summary))
        os.environ.pop("ACTOR_RUN_ID", None)
        fake, _ = run({"urls": ["a.pdf"]}, files, [(1, False)])
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
    for f in FAILS:
        print("  FAIL", f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(run_all())
