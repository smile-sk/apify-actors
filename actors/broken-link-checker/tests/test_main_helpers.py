import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import main


def test_cache_key_has_no_colon():
    # a colon in an Apify cache key silently breaks the cache - the key must
    # never contain one, and must change with every money-relevant input
    base = ("https://e.com/a", list(range(200, 400)), {}, "ua/1", 2000, 5, 2, 15)
    k1 = main._cache_key(*base)
    k2 = main._cache_key("https://e.com/a", [404], {}, "ua/1", 2000, 5, 2, 15)
    k3 = main._cache_key("https://e.com/a", list(range(200, 400)), {"X": "1"}, "ua/1", 2000, 5, 2, 15)
    k4 = main._cache_key("https://e.com/b", list(range(200, 400)), {}, "ua/1", 2000, 5, 2, 15)
    k5 = main._cache_key("https://e.com/a", list(range(200, 400)), {}, "ua/1", 1, 5, 2, 15)
    k6 = main._cache_key("https://e.com/a", list(range(200, 400)), {}, "ua/1", 2000, 0, 2, 15)
    k7 = main._cache_key("https://e.com/a", list(range(200, 400)), {}, "ua/1", 2000, 5, 0, 15)
    k8 = main._cache_key("https://e.com/a", list(range(200, 400)), {}, "ua/1", 2000, 5, 2, 60)
    assert ":" not in k1
    assert len({k1, k2, k3, k4, k5, k6, k7, k8}) == 8


def test_expected_rule_default_is_200_399():
    assert main._expected_rule(None) == list(range(200, 400))
    assert main._expected_rule([]) == list(range(200, 400))
    assert main._expected_rule([404, 200]) == [200, 404]


def test_sitemap_parse_flat():
    xml = ('<?xml version="1.0"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<url><loc>https://e.com/a</loc></url>'
           '<url><loc>https://e.com/b</loc></url></urlset>')
    links = main.parse_sitemap(xml, 100)
    assert links == ["https://e.com/a", "https://e.com/b"]


def test_sitemap_parse_index_shape():
    xml = ('<?xml version="1.0"?>'
           '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<sitemap><loc>https://e.com/s1.xml</loc></sitemap>'
           '<sitemap><loc>https://e.com/s2.xml</loc></sitemap></sitemapindex>')
    links = main.parse_sitemap(xml, 100)
    assert links == ["https://e.com/s1.xml", "https://e.com/s2.xml"]


def test_sitemap_parse_caps():
    xml = ('<?xml version="1.0"?><urlset>'
           + "".join(f"<url><loc>https://e.com/{i}</loc></url>" for i in range(50))
           + "</urlset>")
    links = main.parse_sitemap(xml, 10)
    assert len(links) == 10


def test_sitemap_parse_zero_cap_returns_empty():
    xml = '<?xml version="1.0"?><urlset><url><loc>https://e.com/a</loc></url></urlset>'
    assert main.parse_sitemap(xml, 0) == []


def test_sitemap_parse_garbage():
    assert main.parse_sitemap("<html>not xml</html>", 10) == []


def test_monetization_off():
    class Charge:
        chargeable_within_limit = None
        event_charge_limit_reached = False

    class ChargeOn:
        chargeable_within_limit = {"url-checked": 1}
        event_charge_limit_reached = False

    assert main._monetization_off(Charge()) is True
    assert main._monetization_off(ChargeOn()) is False


def test_list_of_strings_never_splits_on_comma():
    assert main._list_of_strings(["a", "b"]) == ["a", "b"]
    # a comma stays inside a URL: a query like ?x=1,2 must not become a
    # bogus second target that gets checked and billed
    assert main._list_of_strings("a,b\nc") == ["a,b", "c"]
    assert main._list_of_strings("https://e.com/a?x=1,2") == ["https://e.com/a?x=1,2"]
    assert main._list_of_strings("a;b") == ["a", "b"]
    assert main._list_of_strings(None) == []


def test_int_setting_keeps_explicit_zero():
    assert main._int_setting(0, 2) == 0
    assert main._int_setting(None, 2) == 2
    assert main._int_setting("", 5) == 5
    assert main._int_setting(3, 2) == 3


def test_all_healthy_requires_full_coverage():
    assert main._all_healthy(0, 0, 0, 0, 0, 0, 0) is True
    assert main._all_healthy(0, 0, 0, 0, 3, 0, 0) is False
    assert main._all_healthy(0, 0, 0, 0, 0, 2, 0) is False
    assert main._all_healthy(0, 0, 0, 0, 0, 0, 1) is False
    assert main._all_healthy(1, 0, 0, 0, 0, 0, 0) is False
    assert main._all_healthy(0, 0, 0, 1, 0, 0, 0) is False


def test_coverage_complete_counts_every_row():
    assert main._coverage_complete(3, 0, 0, 3, 0, 0) is True
    assert main._coverage_complete(3, 0, 0, 6, 0, 0) is False
    assert main._coverage_complete(2, 1, 0, 3, 0, 0) is True
    assert main._coverage_complete(2, 0, 1, 3, 0, 0) is True
    assert main._coverage_complete(5, 0, 0, 3, 1, 1) is True


def test_omitted_is_the_unaccounted_rest():
    assert main._omitted(6, 3, 0, 0) == 3
    assert main._omitted(6, 6, 0, 0) == 0
    assert main._omitted(6, 2, 1, 1) == 2
    assert main._omitted(6, 8, 0, 0) == 0


def test_cached_row_keeps_original_checked_at():
    row = {"url": "https://e.com/", "checkedAt": "2026-11-05T18:45:31+00:00", "broken": False}
    out = main._cached_row(row, "2026-11-05T18:46:32+00:00")
    assert out["checkedAt"] == "2026-11-05T18:45:31+00:00"
    assert out["cachedAt"] == "2026-11-05T18:46:32+00:00"
    assert out["fromCache"] is True
    assert out["broken"] is False


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"PASS {fn.__name__}")
    print(f"{passed}/{len(fns)} passed")


if __name__ == "__main__":
    run_all()
