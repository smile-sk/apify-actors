"""Money checks: the real main() against a stand that repeats the SDK.

The stand writes rows BEFORE it charges, like Actor.push_data does, and can refuse the write,
fail the charge, lag the counter, refuse the counter and refuse the price - the ways one
exception used to hide the truth. A stand that always answers honestly hides the whole class.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import main as app
from src import pypi


class Meta:
    def __init__(self, item_count):
        self.item_count = item_count


class Result:
    def __init__(self, charged_count, limit_reached=False):
        self.charged_count = charged_count
        self.event_charge_limit_reached = limit_reached


class Pricing:
    def __init__(self, pay_per_event, prices):
        self.is_pay_per_event = pay_per_event
        self.per_event_prices = prices


class Charging:
    def __init__(self, stand):
        self.stand = stand

    def get_pricing_info(self):
        if self.stand["price_raises"]:
            raise RuntimeError("price list is not readable")
        ppe = self.stand["ppe"]
        return Pricing(ppe, {app.EVENT_NAME: 0.0008} if ppe else {})

    def is_event_charge_limit_reached(self, name):
        return bool(self.stand["limit_reached"])

    def compute_push_data_limit(self, count, name, is_default_dataset=True):
        room = self.stand["room"]
        return count if room is None else max(0, min(count, room))


class Dataset:
    def __init__(self, stand):
        self.stand = stand
        self.rows = []

    async def get_metadata(self):
        s = self.stand
        s["meta_reads"] += 1
        if s["meta_raises"] > 0:
            s["meta_raises"] -= 1
            raise RuntimeError("metadata is not readable")
        count = len(self.rows)
        if s["lag_reads"] > 0:
            s["lag_reads"] -= 1
            count = max(0, count - s["lag_size"])
        return Meta(count)

    async def push_data(self, data, charged_event_name=None):
        s = self.stand
        s["push_calls"] += 1
        s["push_sizes"].append(len(data))
        if s["fail_before_write"] or (s["fail_before_write_at"]
                                      and s["push_calls"] == s["fail_before_write_at"]):
            raise RuntimeError("the dataset refused the rows")
        self.rows.extend(data)
        if not s["ppe"]:
            return Result(0)
        if s["fail_charge"]:
            raise RuntimeError("the charge raised")
        room = s["room"]
        charged = len(data) if room is None else max(0, min(len(data), room))
        if room is not None:
            s["room"] = room - charged
        if s["short_by"]:
            charged = max(0, charged - s["short_by"])
        s["charged"] += charged
        return Result(charged, charged < len(data))


class Store:
    def __init__(self, stand, name):
        self.stand = stand
        self.name = name

    async def set_value(self, key, value):
        self.stand["stores"].append((self.name, key))
        if self.name is None:
            self.stand["summary"] = value


class Log:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class FakeActor:
    stand = {}
    log = Log()

    @classmethod
    async def init(cls):
        return None

    @classmethod
    async def get_input(cls):
        return cls.stand["input"]

    @classmethod
    def get_charging_manager(cls):
        return Charging(cls.stand)

    @classmethod
    async def open_dataset(cls):
        if "dataset" not in cls.stand:
            cls.stand["dataset"] = Dataset(cls.stand)
        return cls.stand["dataset"]

    @classmethod
    async def open_key_value_store(cls, name=None):
        return Store(cls.stand, name)

    @classmethod
    async def push_data(cls, data, charged_event_name=None):
        dataset = await cls.open_dataset()
        return await dataset.push_data(data, charged_event_name)

    @classmethod
    async def set_status_message(cls, text):
        cls.stand["status"] = text

    @classmethod
    async def fail(cls, status_message="", **kwargs):
        cls.stand["failed"] = status_message
        raise SystemExit(1)

    @classmethod
    async def exit(cls):
        return None


def stand(**over):
    base = {
        "input": {"packages": []},
        "ppe": True,
        "price_raises": False,
        "room": None,
        "limit_reached": False,
        "fail_before_write": False,
        "fail_before_write_at": 0,
        "fail_charge": False,
        "meta_raises": 0,
        "lag_reads": 0,
        "lag_size": 1,
        "short_by": 0,
        "meta_reads": 0,
        "push_calls": 0,
        "push_sizes": [],
        "charged": 0,
        "stores": [],
        "rows_read": {},
        "refused": {},
        "user_agent": "",
        "no_such_release": [],
        "read_error": None,
        "read_calls": 0,
        "status": "",
        "failed": "",
        "summary": {},
    }
    base.update(over)
    base["datasetId"] = "dataset-under-test"
    return base


def row_for(name):
    return {"name": name, "version": "1.0.0", "summary": f"package {name}",
            "license": "MIT", "requiresPython": ">=3.8", "author": "A. Author",
            "homepage": None, "repository": None, "pythonVersions": ["3.11"],
            "dependencies": ["urllib3<3"], "dependenciesCount": 1,
            "dependenciesTruncated": False, "releaseCount": 3,
            "firstRelease": "2020-01-01T00:00:00Z", "latestRelease": "2024-01-01T00:00:00Z",
            "filesInRelease": 2, "releaseSizeBytes": 2048, "classifiers": ["MIT"],
            "source": "PyPI JSON API (keyless)", "billable": True}


stand_ref = {}


def fake_read(client, targets, *, with_dependencies=True, timeout=None, results=None):
    """PyPI reader stand: one request per name, keyed by the string that was sent.

    rows_read is looked up by the request first and by the project name second, so a test
    can stage two versions of one project without them overwriting each other.
    """
    stand_ref["read_calls"] += 1
    stand_ref.setdefault("timeouts", []).append(timeout)
    # The client is the one main() built, so its headers are the actor's real politeness.
    stand_ref["user_agent"] = client.headers.get("user-agent", "")
    rows, refused, missing = {}, {}, {}
    for t in targets:
        if t.key in stand_ref["refused"]:
            refused[t.request] = stand_ref["refused"][t.key]
            continue
        if t.request in stand_ref["no_such_release"]:
            missing[t.request] = (f"PyPI has no release {t.version} of {t.name} "
                                  f"(latest is 9.9.9): this line was not sold and is not charged")
            continue
        value = stand_ref["rows_read"].get(t.request, stand_ref["rows_read"].get(t.key))
        if value is not None:
            rows[t.request] = dict(value)
    return rows, refused, missing


def run(**over):
    global stand_ref
    data = stand(**over)
    stand_ref = data
    os.environ["ACTOR_RUN_ID"] = "test-run-1"
    FakeActor.stand = data
    app.Actor = FakeActor
    app.pypi.read_packages = fake_read
    try:
        asyncio.run(app.main())
    except SystemExit:
        pass
    return data


def by_status(data):
    return data["summary"].get("byStatus", {})


# ---------------------------------------------------------------- checks

def test_healthy_run_sells_and_writes():
    d = run(input={"packages": ["requests", "numpy"]},
            rows_read={"requests": row_for("requests"), "numpy": row_for("numpy")})
    s = d["summary"]
    return (s["delivered"] == 2 and s["chargedEvents"] == 2 and s["rowsWritten"] == 2
            and s["counterMismatch"] is False and s["stopReason"] == ""
            and by_status(d) == {"OK": 2}
            and "2 package(s) delivered, 2 charged, 0 free" in d["status"])


def test_missing_package_is_free():
    d = run(input={"packages": ["requests", "does-not-exist"]},
            rows_read={"requests": row_for("requests")})
    s = d["summary"]
    free = [r for r in d["dataset"].rows if r.get("status") == "NOT_FOUND"]
    return (s["delivered"] == 1 and s["chargedEvents"] == 1 and s["packagesNotFound"] == 1
            and len(free) == 1 and free[0]["billable"] is False)


def test_duplicate_is_free():
    d = run(input={"packages": ["requests", "Requests"]},
            rows_read={"requests": row_for("requests")})
    s = d["summary"]
    return (s["delivered"] == 1 and s["chargedEvents"] == 1
            and s["packagesDuplicate"] == 1
            and [r.get("status") for r in d["dataset"].rows].count("DUPLICATE") == 1)


def test_no_such_release_does_not_eat_the_real_version():
    # The money blocker: a version PyPI never published was sold as a row, and it ate the
    # honest line for the real version sent in the same batch.
    d = run(input={"packages": ["requests==9.9.9", "requests==2.31.0"]},
            no_such_release=["requests==9.9.9"],
            rows_read={"requests==2.31.0": row_for("requests")})
    s = d["summary"]
    rows = d["dataset"].rows
    paid = [r for r in rows if r.get("billable") is True]
    free = [r for r in rows if r.get("status") == "NOT_FOUND"]
    return (s["delivered"] == 1 and s["chargedEvents"] == 1 and s["packagesNotFound"] == 1
            and len(paid) == 1 and paid[0]["actorInput"] == "requests==2.31.0"
            and len(free) == 1 and free[0]["reason"].startswith("PyPI has no release 9.9.9")
            and s["counterMismatch"] is False)


def test_the_real_client_names_the_actor():
    # README promises a User-Agent that names this actor: the check is on the client main()
    # really built, not on a client the test assembled for itself.
    d = run(input={"packages": ["requests"]}, rows_read={"requests": row_for("requests")})
    return d["user_agent"].startswith("apify-pypi-package-api")


def test_budget_is_shared_between_the_calls_of_one_chunk():
    # One chunk of ten names is ten PyPI requests, not one: giving each the whole remainder
    # overran the platform deadline (measured 30.2 s against 25 s) and was killed after
    # rows were already charged.
    ten = app.request_budget(120.0, 10)
    one = app.request_budget(120.0, 1)
    return (ten * 10 <= 120.0 - app.DEADLINE_MARGIN_SECONDS and ten < one <= 20.0
            and app.request_budget(None) == app.pypi.TIMEOUT)


def test_default_max_packages_matches_the_schema():
    d = run(input={"packages": ["requests"]}, rows_read={"requests": row_for("requests")})
    return d["summary"]["maxPackages"] == 100


def test_junk_line_is_free_and_never_asked():
    d = run(input={"packages": ["hello world", "requests"]},
            rows_read={"requests": row_for("requests")})
    s = d["summary"]
    return (s["delivered"] == 1 and s["packagesInvalid"] == 1
            and [r.get("status") for r in d["dataset"].rows].count("INVALID_INPUT") == 1)


def test_max_packages_cuts_the_tail():
    d = run(input={"packages": ["a", "b", "c"], "maxPackages": 2},
            rows_read={"a": row_for("a"), "b": row_for("b"), "c": row_for("c")})
    s = d["summary"]
    return (s["delivered"] == 2 and s["cutByLimit"] == 1 and s["counterMismatch"] is False)


def test_refused_write_is_not_delivery():
    # The dataset refuses the rows before writing: nothing is sold, and the summary says so.
    d = run(input={"packages": ["requests", "numpy"]}, fail_before_write=True,
            rows_read={"requests": row_for("requests"), "numpy": row_for("numpy")})
    s = d["summary"]
    return (s["delivered"] == 0 and s["rowsWritten"] == 0 and s["chargedEvents"] == 0
            and s["deliveredUnverified"] == 2 and s["stopReason"] == "dataset-write-error")


def test_charge_failing_after_write_is_unknown_not_zero():
    # Rows are in the dataset and the counter cannot be read: the honest answer is "unknown".
    d = run(input={"packages": ["requests", "numpy"]}, fail_charge=True, meta_raises=3,
            rows_read={"requests": row_for("requests"), "numpy": row_for("numpy")})
    s = d["summary"]
    note = (s["datasetNote"] or "").lower()
    return (s["deliveredUnverified"] == 2 and s["chargeUncertain"] == 2
            and "both unknown" in note and "nothing was charged" not in note)


def test_lagging_counter_is_not_reported_as_nothing_delivered():
    # The counter lags past every retry, so the run cannot know how much landed. The one
    # wording it must never use is "nothing was delivered": the rows are physically there.
    d = run(input={"packages": ["requests", "numpy"]}, fail_charge=True, lag_reads=4,
            rows_read={"requests": row_for("requests"), "numpy": row_for("numpy")})
    s = d["summary"]
    note = (s["datasetNote"] or "").lower()
    return ("nothing was delivered" not in note and "nothing was charged" not in note
            and s["deliveredUnverified"] == 2 and s["chargeUncertain"] == 2
            and len(d["dataset"].rows) == 2)


def test_short_charge_is_charged_less_than_delivered():
    d = run(input={"packages": ["a", "b", "c"]}, short_by=1,
            rows_read={"a": row_for("a"), "b": row_for("b"), "c": row_for("c")})
    s = d["summary"]
    return s["delivered"] == 3 and s["chargedEvents"] == 2 and s["stopReason"] != ""


def test_price_unreadable_stops_before_reading():
    # domain-availability was fixed exactly here: an unreadable price must stop the run
    # BEFORE it asks the source, otherwise the buyer pays for reads that cannot be billed.
    d = run(input={"packages": ["requests"]}, price_raises=True,
            rows_read={"requests": row_for("requests")})
    s = d["summary"]
    return (d["read_calls"] == 0 and s["chargedEvents"] == 0
            and s["billingActive"] is not True and s["stopReason"] == "billing-price-unknown")


def test_not_ppe_sells_nothing_and_says_so():
    d = run(input={"packages": ["requests", "numpy"]}, ppe=False,
            rows_read={"requests": row_for("requests"), "numpy": row_for("numpy")})
    s = d["summary"]
    return (s["chargedEvents"] == 0 and s["deliveredUncharged"] == 2
            and s["billingActive"] is False)


def test_cap_marks_the_tail_as_cap_not_error():
    d = run(input={"packages": ["a", "b", "c"]}, room=1,
            rows_read={"a": row_for("a"), "b": row_for("b"), "c": row_for("c")})
    s = d["summary"]
    statuses = [r.get("status") for r in d["dataset"].rows]
    return s["chargedEvents"] == 1 and statuses.count("NOT_CHECKED_CAP") >= 1


def test_refused_read_is_a_free_error_row():
    d = run(input={"packages": ["requests"]}, refused={"requests": "PyPI answered HTTP 500"})
    s = d["summary"]
    rows = d["dataset"].rows
    return (s["delivered"] == 0 and s["packagesRefused"] == 1
            and rows and rows[0].get("status") == "NOT_CHECKED_ERROR"
            and "500" in (rows[0].get("reason") or ""))


def test_summary_is_written_even_when_everything_fails():
    d = run(input={"packages": ["requests"]}, fail_before_write=True,
            rows_read={"requests": row_for("requests")})
    return d["summary"].get("stopReason") == "dataset-write-error" and "RUN_SUMMARY" not in ()


def test_counters_add_up():
    d = run(input={"packages": ["requests", "numpy", "does-not-exist", "hello world"]},
            rows_read={"requests": row_for("requests"), "numpy": row_for("numpy")})
    s = d["summary"]
    return (s["requested"] == 4 and s["rowsWritten"] == 4
            and s["delivered"] == 2 and s["counterMismatch"] is False)


def test_empty_input_is_refused_by_name_without_charges():
    # Park decision for new actors: the field is required and prefill exists, so an empty
    # input is a refusal that NAMES the field, not a silent green run and not a traceback.
    d = run(input={"packages": []})
    return ("packages" in d["failed"] and d["chargedEvents"] == 0
            and not d["dataset"].rows if "dataset" in d else True)


# ------------------------------------------------------------------ runner

CHECKS = [
    test_healthy_run_sells_and_writes, test_missing_package_is_free, test_duplicate_is_free,
    test_no_such_release_does_not_eat_the_real_version,
    test_the_real_client_names_the_actor,
    test_budget_is_shared_between_the_calls_of_one_chunk,
    test_default_max_packages_matches_the_schema, test_junk_line_is_free_and_never_asked,
    test_max_packages_cuts_the_tail, test_refused_write_is_not_delivery,
    test_charge_failing_after_write_is_unknown_not_zero,
    test_lagging_counter_is_not_reported_as_nothing_delivered,
    test_short_charge_is_charged_less_than_delivered,
    test_price_unreadable_stops_before_reading, test_not_ppe_sells_nothing_and_says_so,
    test_cap_marks_the_tail_as_cap_not_error, test_refused_read_is_a_free_error_row,
    test_summary_is_written_even_when_everything_fails, test_counters_add_up,
    test_empty_input_is_refused_by_name_without_charges,
]


def run_all():
    failed = []
    for check in CHECKS:
        try:
            if not check():
                failed.append((check.__name__, "returned False"))
        except Exception as exc:  # noqa: BLE001
            failed.append((check.__name__, f"{type(exc).__name__}: {exc}"))
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
    for name, why in failed:
        print(f"FAIL {name}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_all())
