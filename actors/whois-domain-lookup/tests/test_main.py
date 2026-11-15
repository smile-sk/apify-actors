"""Local checks for the run loop and the money path. Run: python tests/test_main.py

The fake Actor repeats the shapes the real SDK returns (a coroutine dataset, a
pydantic DatasetMetadata, a synchronous charging manager, an ActorPricingInfo
dataclass): a fake built for our own convenience has hidden three defects in this
park already, so it is written against the installed apify 4.x, not against what
main.py happens to expect.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apify._charging import ActorPricingInfo
from crawlee.storage_clients.models import DatasetMetadata

from src import main as actor_main

NOW = datetime.now(timezone.utc)
FAILS = []
CHECKS = [0]


def ok(cond, name, extra=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, extra))


class Log:
    def __init__(self):
        self.lines = []

    def _add(self, level, msg, *args):
        self.lines.append("%s %s" % (level, msg % args if args else msg))

    def info(self, msg, *args):
        self._add("INFO", msg, *args)

    def warning(self, msg, *args):
        self._add("WARN", msg, *args)

    def error(self, msg, *args):
        self._add("ERROR", msg, *args)


class ChargeResult:
    def __init__(self, charged_count, limit_reached):
        self.charged_count = charged_count
        self.event_charge_limit_reached = limit_reached


class FakeDataset:
    def __init__(self, host):
        self.host = host   # the FakeActor instance of this run

    async def get_metadata(self):
        return DatasetMetadata(id="ds1", name="default", accessed_at=NOW, created_at=NOW,
                               modified_at=NOW, item_count=len(self.host.rows))


class FakeStore:
    def __init__(self, name):
        self.name = name
        self.data = {}

    async def set_value(self, key, value):
        self.data[key] = value


class FakeChargingManager:
    def __init__(self, pricing):
        self._pricing = pricing

    def get_pricing_info(self):
        return self._pricing


def pricing(event="domain-checked", ppe=True):
    prices = {event: 0.004} if event else {}
    return ActorPricingInfo(pricing_model=None, max_total_charge_usd=None,
                            is_pay_per_event=ppe, per_event_prices=prices)


class FakeActor:
    """One instance per run, mirroring the shapes the real SDK returns."""

    def __init__(self, payload, pricing_info, charge_mode="ok", refuse=0, lookups=None, dns=None):
        self.payload = payload
        self.pricing_info = pricing_info
        self.charge_mode = charge_mode
        self.refuse = refuse
        self.lookups = lookups or {}
        self.dns = dns if dns is not None else {
            "nameservers": [], "mx": [], "txt": [], "a": [], "spf": None, "dmarc": None,
            "dmarcPolicy": None, "dnssecSigned": None, "resolver": "system",
            "exists": True, "error": None}
        self.rows = []
        self.stores = {}
        self.failed = None
        self.log = Log()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_input(self):
        return self.payload

    def get_env(self):
        return {"run_id": "test-run"}

    def get_charging_manager(self):
        return FakeChargingManager(self.pricing_info)

    async def open_dataset(self):
        return FakeDataset(self)

    async def open_key_value_store(self, name=None):
        key = name or "__run__"
        self.stores.setdefault(key, FakeStore(key))
        return self.stores[key]

    async def push_data(self, row, charged_event_name=None):
        if self.refuse > 0:
            self.refuse -= 1
            raise RuntimeError("dataset is busy")
        if charged_event_name is not None and self.charge_mode == "cap":
            # measured: at the spending cap the SDK writes no row at all and says the cap was hit,
            # so the actor has to write its own "not checked" row instead.
            return ChargeResult(0, True)
        self.rows.append(dict(row))
        if charged_event_name is None:
            return None
        if self.charge_mode == "error":
            # the SDK stores the row first and charges after: the row is already here
            raise RuntimeError("charge call failed")
        events = getattr(self.pricing_info, "per_event_prices", {}) or {}
        if self.charge_mode == "noprice" or charged_event_name not in events:
            return ChargeResult(0, False)
        return ChargeResult(1, False)

    async def fail(self, status_message=None):
        self.failed = status_message


def run(payload, **kwargs):
    actor = FakeActor(payload, kwargs.pop("pricing_info", None) or pricing(), **kwargs)
    actor_main.Actor = actor

    async def fake_bootstrap(client):
        return {"com": "https://rdap.verisign.com/com/v1", "io": "https://rdap.identityservices/x"}

    async def fake_lookup(client, domain, mapping):
        answer = actor.lookups.get(domain)
        if answer is None:
            return {"status": "registered", "http": 200, "url": "https://rdap/" + domain,
                    "payload": {"ldhName": domain.upper(),
                                "events": [{"eventAction": "registration",
                                            "eventDate": "2001-01-01T00:00:00Z"},
                                           {"eventAction": "expiration",
                                            "eventDate": "2030-01-01T00:00:00Z"}],
                                "status": ["active"],
                                "nameservers": [{"ldhName": "NS1.EXAMPLE.NET"}],
                                "entities": [], "secureDNS": {"delegationSigned": False}},
                    "reason": ""}
        return answer

    async def fake_dns(domain, want_mail=True, want_dnssec=True):
        return dict(actor.dns)

    actor_main.rdap.bootstrap = fake_bootstrap
    actor_main.rdap.lookup = fake_lookup
    actor_main.dnsrec.lookup = fake_dns
    asyncio.run(actor_main.main())
    return actor


def summary_of(actor, run_id="test-run"):
    store = actor.stores.get("whois-domain-lookup-summary")
    return store.data.get("RUN_SUMMARY-%s" % run_id) if store else None


def test_two_domains_one_free_are_two_charged_rows():
    actor = run({"domains": ["example.com", "free-name.com"],
                 "maxDomains": 10},
                lookups={"free-name.com": {"status": "available", "http": 404, "url": "u",
                                           "payload": None, "reason": "not registered"}})
    summary = summary_of(actor)
    ok(len(actor.rows) == 2, "two rows", len(actor.rows))
    ok(summary["chargedEvents"] == 2, "two charges", summary["chargedEvents"])
    ok(summary["delivered"] == 2, "both are answers", summary["delivered"])
    ok(summary["requested"] == 2 and summary["uniqueDomains"] == 2, "counts", summary)
    ok(summary["requested"] == summary["processed"] + summary["notChecked"],
       "requested equals processed plus notChecked")
    statuses = {row["domain"]: row["status"] for row in actor.rows}
    ok(statuses["example.com"] == "REGISTERED" and statuses["free-name.com"] == "AVAILABLE",
       "statuses", statuses)
    ok(all(row["billable"] is True for row in actor.rows), "both rows carry the billable claim")
    ok(actor.rows[0]["ageDays"] is not None, "age is derived from the registry date")
    ok(actor.stores["__run__"].data.get("RUN_SUMMARY") is not None, "the run store holds a copy")
    ok("RUN_SUMMARY" not in actor.stores["whois-domain-lookup-summary"].data,
       "the named store holds only the run scoped key")


def test_a_repeated_domain_is_answered_and_charged_once():
    actor = run({"domains": ["example.com", "EXAMPLE.com"], "maxDomains": 10})
    summary = summary_of(actor)
    ok(summary["chargedEvents"] == 1, "one charge for the same domain", summary["chargedEvents"])
    ok(summary["duplicates"] == 1, "the repeat is counted", summary["duplicates"])
    ok(actor.rows[1]["status"] == "NOT_CHECKED_DUPLICATE", "second line is a duplicate row",
       actor.rows[1]["status"])
    ok(actor.rows[1]["domain"] == "example.com", "the duplicate still names its domain")
    ok(summary["requested"] == summary["processed"] + summary["notChecked"], "invariant holds", summary["byStatus"])


def test_a_run_that_cannot_bill_delivers_free_and_says_so():
    actor = run({"domains": ["example.com"], "maxDomains": 10}, pricing_info=pricing(event="other-event"))
    summary = summary_of(actor)
    ok(summary["chargedEvents"] == 0, "no money", summary["chargedEvents"])
    ok(summary["billingActive"] is False, "the summary says the run could not bill")
    ok(summary["stopReason"] is None, "a run that cannot bill is not a stopped run", summary["stopReason"])
    ok(summary["delivered"] == 1 and summary["deliveredUncharged"] == 1, "free delivery is counted")
    ok(actor.rows[0].get("billingInactive") is True, "the row says it went out uncharged")


def test_the_spending_cap_stops_the_run_and_costs_nothing_for_the_tail():
    actor = run({"domains": ["a-one.com", "b-two.com", "c-three.com"], "maxDomains": 10},
                charge_mode="cap")
    summary = summary_of(actor)
    ok(summary["stopReason"] == "spending-cap", "stop reason", summary["stopReason"])
    ok(summary["chargedEvents"] == 0, "nothing was charged", summary["chargedEvents"])
    ok(actor.rows[0]["status"] == "NOT_CHECKED_CAP", "first row says cap", actor.rows[0]["status"])
    ok(all(row["status"].startswith("NOT_CHECKED") for row in actor.rows), "no paid rows")
    ok(actor.failed and "spending cap" in actor.failed, "the run stops with a plain reason", actor.failed)
    ok(summary["requested"] == summary["processed"] + summary["notChecked"], "invariant holds", summary["byStatus"])


def test_a_refused_dataset_write_is_not_a_delivery():
    actor = run({"domains": ["example.com"], "maxDomains": 10}, refuse=10)
    summary = summary_of(actor)
    ok(summary["datasetWriteErrors"] == 1, "the refused row is counted", summary["datasetWriteErrors"])
    ok(summary["delivered"] == 0, "a refused row is not a delivery", summary["delivered"])
    ok(summary["stopReason"] == "dataset-write-error", "stop reason", summary["stopReason"])
    ok(actor.failed and "refused" in actor.failed, "the buyer is told", actor.failed)


def test_a_failed_charge_still_delivers_and_is_counted():
    actor = run({"domains": ["example.com", "second.com"], "maxDomains": 10}, charge_mode="error")
    summary = summary_of(actor)
    ok(summary["chargedEvents"] == 0, "no money", summary["chargedEvents"])
    ok(summary["deliveredUncharged"] == 2, "both rows went out free and are counted",
       summary["deliveredUncharged"])
    ok(summary["billingActive"] is False, "two strikes turn billing off")
    ok(summary["stopReason"] == "charge-error", "stop reason", summary["stopReason"])
    ok(len(actor.rows) == 2, "the rows still reached the buyer", len(actor.rows))


def test_a_registry_that_did_not_answer_is_free_and_named():
    actor = run({"domains": ["down.com", "up.com"], "maxDomains": 10},
                lookups={"down.com": {"status": "error", "http": None, "url": "u", "payload": None,
                                      "reason": "the registry did not answer"}})
    summary = summary_of(actor)
    ok(actor.rows[0]["status"] == "NOT_CHECKED_SOURCE", "a silent registry is not AVAILABLE",
       actor.rows[0]["status"])
    ok(actor.rows[0]["billable"] is False, "and it is free")
    ok(summary["chargedEvents"] == 1, "only the real answer is charged", summary["chargedEvents"])


def test_a_tld_without_rdap_is_free_and_still_answers_dns():
    actor = run({"domains": ["example.co"], "maxDomains": 10},
                lookups={"example.co": {"status": "unsupported", "http": None, "url": None,
                                        "payload": None, "reason": "no RDAP for .co"}})
    row = actor.rows[0]
    ok(row["status"] == "NOT_CHECKED_NO_RDAP", "unsupported TLD has its own status", row["status"])
    ok(row["dnsResolver"] == "system", "DNS was still read", row["dnsResolver"])
    ok(row["billable"] is False, "no registry data means no charge")
    ok(actor.failed and "No domain could be read" in actor.failed, "the run stops with the reason",
       actor.failed)


def test_a_bad_line_and_a_cut_tail_are_rows_not_silence():
    actor = run({"domains": ["not a domain", "one.com", "two.com"], "maxDomains": 1})
    statuses = [row["status"] for row in actor.rows]
    ok("NOT_CHECKED_INVALID" in statuses, "the bad line comes back as its own row", statuses)
    ok("NOT_CHECKED_LIMIT" in statuses, "the tail past maxDomains is named", statuses)
    summary = summary_of(actor)
    ok(summary["cutByMaxDomains"] == 1, "the cut is counted", summary["cutByMaxDomains"])
    ok(summary["requested"] == 3 and summary["uniqueDomains"] == 2,
       "the limit does not shrink the request count", summary)


def test_the_platform_deadline_leaves_the_list_for_a_second_run():
    os.environ["ACTOR_TIMEOUT_AT"] = "2020-01-01T00:00:00+00:00"   # already past: no budget left
    try:
        actor = run({"domains": ["a.com", "b.com"], "maxDomains": 10})
    finally:
        os.environ.pop("ACTOR_TIMEOUT_AT", None)
    summary = summary_of(actor)
    ok(summary["deadlineCapped"] is True, "the cap is reported", summary["deadlineCapped"])
    ok(all(row["status"] == "NOT_CHECKED_TIME" for row in actor.rows),
       "every row says why it was not read", [row["status"] for row in actor.rows])
    ok(summary["chargedEvents"] == 0, "no money for work not done")


def test_empty_input_fails_without_charging():
    actor = run({"domains": []})
    ok(actor.failed and "No domains" in actor.failed, "the reason is plain", actor.failed)
    ok(actor.rows == [], "nothing was written")


def test_the_default_limit_is_the_one_the_schema_promises():
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".actor", "input_schema.json"), encoding="utf-8") as fh:
        schema_default = json.load(fh)["properties"]["maxDomains"]["default"]
    actor = run({"domains": ["example.com"]})
    summary = summary_of(actor)
    ok(summary["maxDomains"] == schema_default,
       "the limit applied without an input field is the schema default",
       "%s vs %s" % (summary["maxDomains"], schema_default))


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
