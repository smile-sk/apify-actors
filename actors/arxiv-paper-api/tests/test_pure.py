"""Pure checks: id parsing, one Atom entry, one chunk read. No network, no SDK."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import arxiv

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <updated>2023-08-02T00:41:18Z</updated>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All You Need</title>
    <summary>  The dominant sequence transduction models
      are based on complex recurrent networks.  </summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <arxiv:comment>15 pages</arxiv:comment>
    <arxiv:journal_ref>NeurIPS 2017</arxiv:journal_ref>
    <arxiv:doi>10.1000/xyz</arxiv:doi>
    <arxiv:primary_category term="cs.CL"/>
    <category term="cs.CL"/>
    <category term="cs.LG"/>
    <link href="http://arxiv.org/abs/1706.03762v7" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/1706.03762v7" rel="related" type="application/pdf"/>
  </entry>
</feed>"""

ERROR_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format_for_hello</id>
    <title>Error</title>
    <summary>incorrect id format</summary>
  </entry>
</feed>"""


class Response:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class Client:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout, "headers": headers})
        if self.error is not None:
            raise self.error
        return self.response


def test_new_id():
    paper, why = arxiv.parse_paper("1706.03762")
    return paper is not None and paper.key == "1706.03762" and paper.version is None, why


def test_version_is_read():
    paper, _ = arxiv.parse_paper("1706.03762v5")
    return paper.key == "1706.03762" and paper.version == 5


def test_old_id():
    paper, _ = arxiv.parse_paper("hep-th/9901001")
    return paper.key == "hep-th/9901001"


def test_old_id_with_dots():
    # The Atom API answers only the canonical form: a sub-archive in the key comes back as
    # an empty feed, which used to be sold as "no such paper" for a paper that exists.
    paper, why = arxiv.parse_paper("cond-mat.mes-hall/0211022")
    return paper is not None and paper.key == "cond-mat/0211022", why


def test_both_old_forms_are_one_paper():
    dotted, _ = arxiv.parse_paper("cond-mat.mes-hall/0211022")
    plain, _ = arxiv.parse_paper("cond-mat/0211022")
    link, _ = arxiv.parse_paper("https://arxiv.org/abs/math.GT/0309136")
    canonical, _ = arxiv.parse_paper("math/0309136")
    return (dotted.key == plain.key == "cond-mat/0211022"
            and link.key == canonical.key == "math/0309136")


def test_version_goes_to_the_request():
    paper, _ = arxiv.parse_paper("1706.03762v3")
    newest, _ = arxiv.parse_paper("1706.03762")
    return (paper.request == "1706.03762v3" and newest.request == "1706.03762"
            and paper.key == newest.key)


def test_abs_link():
    paper, _ = arxiv.parse_paper("https://arxiv.org/abs/2103.00020v2")
    return paper.key == "2103.00020" and paper.version == 2


def test_pdf_link():
    paper, _ = arxiv.parse_paper("https://arxiv.org/pdf/2103.00020.pdf")
    return paper.key == "2103.00020"


def test_bare_host_link():
    paper, _ = arxiv.parse_paper("arxiv.org/abs/1706.03762")
    return paper is not None and paper.key == "1706.03762"


def test_foreign_host_is_invalid():
    paper, why = arxiv.parse_paper("https://example.com/abs/1706.03762")
    return paper is None and "not arxiv.org" in why, why


def test_link_without_paper_path_is_invalid():
    paper, why = arxiv.parse_paper("https://arxiv.org/list/cs.CL/recent")
    return paper is None and "does not point at a paper" in why, why


def test_junk_is_invalid():
    paper, why = arxiv.parse_paper("hello world")
    return paper is None and "is not an arXiv id" in why, why


def test_empty_line_is_invalid():
    paper, why = arxiv.parse_paper("   ")
    return paper is None and "empty" in why, why


def test_entry_becomes_a_row():
    root = arxiv.ET.fromstring(FEED)
    row = arxiv.row_from_entry(root.find(arxiv._ATOM + "entry"))
    return (row["arxivId"] == "1706.03762" and row["version"] == 7
            and row["title"] == "Attention Is All You Need"
            and row["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
            and row["primaryCategory"] == "cs.CL" and row["categories"] == ["cs.CL", "cs.LG"]
            and row["comment"] == "15 pages" and row["journalRef"] == "NeurIPS 2017"
            and row["doi"] == "10.1000/xyz"
            and row["abstract"] == ("The dominant sequence transduction models are based on "
                                    "complex recurrent networks.")
            and row["pdfUrl"] == "http://arxiv.org/pdf/1706.03762v7"
            and row["billable"] is True)


def test_error_entry_is_not_a_paper():
    root = arxiv.ET.fromstring(ERROR_FEED)
    return arxiv.row_from_entry(root.find(arxiv._ATOM + "entry")) is None


def test_read_papers_returns_rows_by_id():
    client = Client(Response(200, FEED))
    rows, error, _ = arxiv.read_papers(client, ["1706.03762"], timeout=10.0)
    return (error is None and "1706.03762" in rows
            and client.calls[0]["params"]["id_list"] == "1706.03762"
            and client.calls[0]["timeout"] == 10.0
            and "arxiv-paper-api" in client.calls[0]["headers"]["User-Agent"])


def test_read_papers_without_abstract():
    client = Client(Response(200, FEED))
    rows, _, _ = arxiv.read_papers(client, ["1706.03762"], with_abstract=False)
    return "abstract" not in rows["1706.03762"]


def test_read_papers_keeps_the_abstract_by_default():
    client = Client(Response(200, FEED))
    rows, _, _ = arxiv.read_papers(client, ["1706.03762"])
    return "abstract" in rows["1706.03762"] and rows["1706.03762"]["abstract"].startswith("The dominant")


def test_missing_id_is_not_an_error():
    client = Client(Response(200, FEED))
    rows, error, _ = arxiv.read_papers(client, ["1706.03762", "9999.99999"])
    return error is None and "9999.99999" not in rows and "1706.03762" in rows


def test_http_failure_is_an_error():
    client = Client(Response(503, ""))
    rows, error, _ = arxiv.read_papers(client, ["1706.03762"])
    return rows == {} and "HTTP 503" in error


def test_broken_body_is_an_error():
    client = Client(Response(200, "<html>not a feed"))
    rows, error, _ = arxiv.read_papers(client, ["1706.03762"])
    return rows == {} and "not a feed" in error


def test_network_exception_is_an_error():
    client = Client(error=OSError("socket closed"))
    rows, error, _ = arxiv.read_papers(client, ["1706.03762"])
    return rows == {} and "could not be reached" in error


def test_empty_list_makes_no_call():
    client = Client(Response(200, FEED))
    rows, error, _ = arxiv.read_papers(client, [])
    return rows == {} and error is None and client.calls == []


def test_chunk_size_is_documented():
    return arxiv.MAX_IDS_PER_CALL == 50 and arxiv.POLITE_SECONDS >= 3.0


class RouteClient:
    """Answers by id_list, so a block that fails as a whole can succeed in halves."""

    def __init__(self, routes, default=None):
        self.routes = routes
        self.default = default if default is not None else Response(200, "")
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        key = (params or {}).get("id_list")
        self.calls.append({"url": url, "params": params, "timeout": timeout,
                           "headers": headers})
        return self.routes.get(key, self.default)


def test_one_bad_id_does_not_take_the_block_down():
    # arXiv answers 500 for the whole id_list when one id is bad, and the first version of
    # read_papers turned that into one error for every paper in the chunk.
    client = RouteClient({
        "1706.03762,hep-th/9901001v1": Response(500, ""),
        "1706.03762": Response(200, FEED),
        "hep-th/9901001v1": Response(500, ""),
    })
    rows, error, refused = arxiv.read_papers(client, ["1706.03762", "hep-th/9901001v1"])
    return ("1706.03762" in rows and error == "arXiv answered HTTP 500"
            and list(refused) == ["hep-th/9901001v1"]
            and refused["hep-th/9901001v1"] == "arXiv answered HTTP 500")


def test_the_offender_is_found_in_a_chunk_of_three():
    client = RouteClient({
        "1706.03762,hep-th/9901001v1,2103.00020": Response(500, ""),
        "1706.03762": Response(200, FEED),
        "hep-th/9901001v1,2103.00020": Response(500, ""),
        "hep-th/9901001v1": Response(500, ""),
        "2103.00020": Response(200, FEED),
    })
    rows, error, refused = arxiv.read_papers(
        client, ["1706.03762", "hep-th/9901001v1", "2103.00020"])
    return ("1706.03762" in rows and list(refused) == ["hep-th/9901001v1"]
            and len(client.calls) == 5)


def test_a_network_failure_is_never_split():
    # An unreachable source says nothing about any single paper, so it must stay one error
    # for the block instead of blaming a line.
    client = Client(error=OSError("socket closed"))
    rows, error, refused = arxiv.read_papers(client, ["1706.03762", "2103.00020"])
    return (rows == {} and refused == {} and "could not be reached" in error
            and len(client.calls) == 1)


def test_a_single_refused_id_is_named_by_its_request():
    client = Client(Response(500, ""))
    rows, error, refused = arxiv.read_papers(client, ["hep-th/9901001v1"])
    return (rows == {} and refused == {"hep-th/9901001v1": "arXiv answered HTTP 500"})


CHECKS = [
    ("new style id is read", test_new_id),
    ("a version suffix is kept as a number", test_version_is_read),
    ("old style id is read", test_old_id),
    ("an old id with dots and a dash is read in canonical form", test_old_id_with_dots),
    ("one bad id does not take the block down", test_one_bad_id_does_not_take_the_block_down),
    ("the offender is found in a chunk of three", test_the_offender_is_found_in_a_chunk_of_three),
    ("a network failure is never split", test_a_network_failure_is_never_split),
    ("a single refused id is named by its request",
     test_a_single_refused_id_is_named_by_its_request),
    ("both old forms and a link are one paper", test_both_old_forms_are_one_paper),
    ("a version suffix is sent to the API, not dropped", test_version_goes_to_the_request),
    ("abs link is read", test_abs_link),
    ("pdf link is read", test_pdf_link),
    ("a link without a scheme is read", test_bare_host_link),
    ("a foreign host is a verdict, not a read", test_foreign_host_is_invalid),
    ("a link that is not a paper is a verdict", test_link_without_paper_path_is_invalid),
    ("junk is a verdict", test_junk_is_invalid),
    ("an empty line is a verdict", test_empty_line_is_invalid),
    ("one Atom entry becomes one paid row", test_entry_becomes_a_row),
    ("an error entry is not sold as a paper", test_error_entry_is_not_a_paper),
    ("a chunk read returns rows by id and sends the User-Agent", test_read_papers_returns_rows_by_id),
    ("the abstract is kept by default", test_read_papers_keeps_the_abstract_by_default),
    ("includeAbstract false drops the abstract", test_read_papers_without_abstract),
    ("an id arXiv never saw is not an error", test_missing_id_is_not_an_error),
    ("an HTTP failure is an error, not a verdict", test_http_failure_is_an_error),
    ("a broken body is an error", test_broken_body_is_an_error),
    ("a network exception is an error", test_network_exception_is_an_error),
    ("an empty list makes no call", test_empty_list_makes_no_call),
    ("the politeness constants are the documented ones", test_chunk_size_is_documented),
]


def run_all() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: raised {type(exc).__name__}: {exc}")
            failed += 1
            continue
        ok = result[0] if isinstance(result, tuple) else result
        if not ok:
            print(f"FAIL {name}" + (f": {result[1]}" if isinstance(result, tuple) else ""))
            failed += 1
    total = len(CHECKS)
    print(f"{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_all())
