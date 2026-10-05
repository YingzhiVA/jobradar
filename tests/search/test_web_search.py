import pytest

from jobradar.search.sources.web_search import (
    WebSearchSource,
    _extract_json,
    _is_aggregator,
    _is_listing_page,
    _parse_postings,
    _result_urls,
    _search_count,
    _search_queries,
    _url_key,
)
from jobradar.search.sources.base import LIVENESS_CONFIRMED, LIVENESS_UNVERIFIED


class _Block:
    def __init__(self, type, text=""):
        self.type = type
        self.text = text


class _ToolUseBlock:
    """A server_tool_use block carrying the query string web_search issued."""

    def __init__(self, query):
        self.type = "server_tool_use"
        self.input = {"query": query}


class _SearchResult:
    def __init__(self, url):
        self.type = "web_search_result"
        self.url = url


class _SearchResultBlock:
    """A web_search_tool_result block: the pages one search returned."""

    def __init__(self, *urls):
        self.type = "web_search_tool_result"
        self.content = [_SearchResult(u) for u in urls]


class _Citation:
    def __init__(self, url):
        self.type = "web_search_result_location"
        self.url = url


class _ServerToolUse:
    def __init__(self, web_search_requests):
        self.web_search_requests = web_search_requests


class _Usage:
    def __init__(self, web_search_requests):
        self.server_tool_use = _ServerToolUse(web_search_requests)


class _Response:
    def __init__(self, content, web_search_requests=None):
        self.content = content
        self.usage = _Usage(web_search_requests) if web_search_requests is not None else None


class _FakeMessages:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class _FakeClient:
    """Duck-typed Anthropic client: client.with_options(...).messages.create()
    returns a canned response, so WebSearchSource.fetch can be tested without
    a live web_search call.
    """

    def __init__(self, response):
        self.messages = _FakeMessages(response)

    def with_options(self, **kwargs):
        return self


def _source(response):
    return WebSearchSource(_FakeClient(response), profile_intent="x", location_desc="y")


def _all_confirmed(postings):
    """Stand-in for filter_live: keeps everything and stamps it confirmed, the
    same as the real function does for a URL that answers.
    """
    kept = list(postings)
    for p in kept:
        p.liveness = LIVENESS_CONFIRMED
    return kept, []


@pytest.fixture(autouse=True)
def _no_network_liveness(monkeypatch):
    # fetch() runs a real liveness check (HTTP GETs); stub it to all-confirmed so
    # the parser/search-count tests stay hermetic. Liveness is tested in
    # test_liveness.
    monkeypatch.setattr("jobradar.search.sources.web_search.filter_live", _all_confirmed)


def test_extract_plain_json_object():
    assert _extract_json('{"postings": []}') == {"postings": []}


def test_extract_json_embedded_in_prose():
    text = 'Here are the roles I found:\n{"postings": [{"title": "X"}]}\nHope this helps.'
    assert _extract_json(text) == {"postings": [{"title": "X"}]}


def test_extract_json_from_markdown_fence():
    text = 'Sure!\n```json\n{"postings": []}\n```\n'
    assert _extract_json(text) == {"postings": []}


def test_extract_raises_when_no_json():
    with pytest.raises(ValueError):
        _extract_json("I could not find any suitable postings.")


def test_parse_postings_joins_blocks_when_json_is_not_in_last_block():
    # Regression: citations split the answer across text blocks, so the JSON
    # may sit in an earlier block while the last block is a trailing fragment.
    # The old code read only the last block and found no JSON.
    blocks = [
        "Based on the search results, here are the matches:",
        '{"postings": [{"title": "User Journey Strategist", "company": "Acme", '
        '"url": "https://acme.example/jobs/1", "location": "Zurich", "description": "..."}]}',
        " — sourced from the company careers page.",
    ]
    postings = _parse_postings(blocks)
    assert len(postings) == 1
    assert postings[0].title == "User Journey Strategist"
    assert postings[0].company == "Acme"


def test_parse_postings_empty_result():
    assert _parse_postings(['{"postings": []}']) == []


def test_search_count_prefers_usage_counter():
    resp = _Response(content=[_Block("server_tool_use")], web_search_requests=3)
    assert _search_count(resp) == 3


def test_search_count_falls_back_to_counting_blocks():
    resp = _Response(
        content=[_Block("server_tool_use"), _Block("server_tool_use"), _Block("text", "{}")],
    )
    assert _search_count(resp) == 2


def test_search_queries_extracts_in_order():
    resp = _Response(
        content=[
            _ToolUseBlock("AI product manager Zurich"),
            _Block("text", "some narration"),
            _ToolUseBlock("data platform product owner Switzerland"),
        ],
    )
    assert _search_queries(resp) == [
        "AI product manager Zurich",
        "data platform product owner Switzerland",
    ]


def test_search_queries_empty_when_no_tool_use():
    resp = _Response(content=[_Block("text", "{}")])
    assert _search_queries(resp) == []


def test_fetch_meta_carries_queries_and_funnel():
    resp = _Response(
        content=[
            _ToolUseBlock("associate to the directors fintech"),
            _Block("text", '{"postings": [{"title": "T", "company": "C", "url": "u"}]}'),
        ],
        web_search_requests=1,
    )
    result = _source(resp).fetch()
    assert result.meta["queries"] == ["associate to the directors fintech"]
    assert result.meta["searches"] == 1
    assert result.meta["found"] == 1
    assert result.meta["live"] == 1


def test_is_aggregator_matches_hosts_and_subdomains():
    assert _is_aggregator("https://www.datacareer.ch/job/12051/x/") is True
    assert _is_aggregator("https://jobs.ch/en/vacancies/detail/123/") is True
    assert _is_aggregator("https://de.indeed.com/viewjob?jk=abc") is True
    assert _is_aggregator("https://www.efinancialcareers.com/jobs/ai-product-manager/in-switzerland") is True
    assert _is_aggregator("https://www.glassdoor.sg/Job/switzerland-fintech-product-manager-jobs.htm") is True
    # A direct employer / ATS link must NOT be treated as an aggregator.
    assert _is_aggregator("https://boards.greenhouse.io/acme/jobs/1") is False
    assert _is_aggregator("https://careers.reprisk.com/senior-pm") is False
    # Substring-but-not-suffix must not false-match (datacareer.ch.evil.com).
    assert _is_aggregator("https://datacareer.ch.evil.com/job/1") is False


# Every link in the 2026-10-02 report. All four roles were checked by hand and
# none of them existed: each was a plausible title attached to a careers page.
_REPORT_2026_10_02_LINKS = [
    "https://www.liip.ch/jobs",
    "https://www.jua.ai/careers",
    "https://www.frontify.com/careers",
    "https://www.pricehubble.com/careers",
]


@pytest.mark.parametrize("url", _REPORT_2026_10_02_LINKS)
def test_is_listing_page_catches_the_2026_10_02_links(url):
    assert _is_listing_page(url) is True


def test_is_listing_page_matches_bare_roots():
    assert _is_listing_page("https://jobs.ethz.ch/") is True
    assert _is_listing_page("https://jobs.ethz.ch") is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.frontify.com/en/careers",
        "https://www.acme.ch/de-ch/karriere/offene-stellen/",
        "https://www.acme.ch/company/careers/open-positions",
        "https://www.acme.ch/fr/carrieres/emplois.html",
        "https://www.acme.ch/careers?lang=en&page=2",
        "https://www.acme.ch/jobs?department=product&utm_source=x",
        "https://jobs.lever.co/acme",
        "https://jobs.ashbyhq.com/acme/",
        "https://boards.greenhouse.io/acme",
        "https://boards.greenhouse.io/acme/jobs",
        "https://apply.workable.com/acme/",
    ],
)
def test_is_listing_page_matches_landing_and_board_pages(url):
    assert _is_listing_page(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.ethz.ch/job/view/JOPG_ethz_4qitaLpc1SITqqBSBf",
        "https://boards.greenhouse.io/x/jobs/2",
        "https://jobs.lever.co/acme/1b2c3d4e-0000-4000-8000-000000000000",
        "https://jobs.ashbyhq.com/acme/7f3e",
        "https://apply.workable.com/acme/j/ABC123/",
        "https://www.acme.ch/jobs/senior-product-manager-ai",
        "https://careers.reprisk.com/senior-pm",
        "https://acme.jobs.personio.de/job/1234567",
        # A query key that names one posting makes even a root or /careers deep.
        "https://jobs.example.com/?gh_jid=123",
        "https://www.acme.ch/careers?jobId=42",
        "https://www.acme.ch/karriere?id=7",
    ],
)
def test_is_listing_page_keeps_posting_pages(url):
    assert _is_listing_page(url) is False


def test_tracking_parameters_do_not_count_as_a_posting_id():
    # gclid ends in "id" but is tracking, not a posting id.
    assert _is_listing_page("https://www.acme.ch/careers?gclid=abc") is True


def test_url_key_ignores_cosmetic_differences():
    assert _url_key("https://www.Acme.ch/jobs/pm-ai/") == _url_key("http://acme.ch/jobs/pm-ai")
    assert _url_key("https://acme.ch/jobs/pm?utm_source=g&b=2&a=1#apply") == _url_key(
        "https://acme.ch/jobs/pm?a=1&b=2"
    )
    assert _url_key("https://acme.ch/jobs/pm-ai") != _url_key("https://acme.ch/jobs/pm-ml")


def test_result_urls_reads_search_results_and_citations():
    cited = _Block("text", "Acme is hiring")
    cited.citations = [_Citation("https://acme.ch/jobs/cited")]
    resp = _Response(
        content=[
            _ToolUseBlock("q"),
            _SearchResultBlock("https://acme.ch/jobs/a", "https://www.beta.ch/careers/b/"),
            cited,
        ]
    )
    assert _result_urls(resp) == {"acme.ch/jobs/a", "beta.ch/careers/b", "acme.ch/jobs/cited"}


def test_result_urls_tolerates_an_errored_search():
    errored = _SearchResultBlock()
    errored.content = {"type": "web_search_tool_result_error", "error_code": "unavailable"}
    assert _result_urls(_Response(content=[errored])) == set()


def test_fetch_drops_listing_pages():
    # A bare board root and a /careers landing page are dropped before liveness
    # even runs; the deep permalink survives. filter_live is stubbed all-live.
    resp = _Response(
        content=[
            _Block(
                "text",
                '{"postings": ['
                '{"title": "A", "company": "ETH", "url": "https://jobs.ethz.ch/"},'
                '{"title": "Senior AI Product Manager", "company": "Jua.ai", '
                '"url": "https://www.jua.ai/careers"},'
                '{"title": "B", "company": "ETH", "url": "https://jobs.ethz.ch/job/view/ABC"}'
                "]}",
            )
        ],
        web_search_requests=2,
    )
    result = _source(resp).fetch()
    assert [p.url for p in result.postings] == ["https://jobs.ethz.ch/job/view/ABC"]
    assert "2 listing page dropped" in result.detail
    assert result.meta["listing_dropped"] == 2


def test_fetch_drops_every_2026_10_02_link():
    postings = ",".join(
        f'{{"title": "Role {i}", "company": "C{i}", "url": "{url}"}}'
        for i, url in enumerate(_REPORT_2026_10_02_LINKS)
    )
    resp = _Response(
        content=[
            _SearchResultBlock(*_REPORT_2026_10_02_LINKS),
            _Block("text", f'{{"postings": [{postings}]}}'),
        ],
        web_search_requests=5,
    )
    result = _source(resp).fetch()
    assert result.ok  # the model searched; an empty day is genuine, not degraded
    assert result.postings == []
    assert result.meta["listing_dropped"] == 4


def test_fetch_drops_links_the_search_never_returned():
    # The model returned two deep links, but the search only ever showed it one:
    # the other was recalled or assembled, so it is dropped.
    resp = _Response(
        content=[
            _ToolUseBlock("ai product manager zurich"),
            _SearchResultBlock("https://jobs.lever.co/acme/1111", "https://acme.ch/careers"),
            _Block(
                "text",
                '{"postings": ['
                '{"title": "A", "company": "Acme", "url": "https://jobs.lever.co/acme/1111/"},'
                '{"title": "B", "company": "Acme", "url": "https://jobs.lever.co/acme/2222"}'
                "]}",
            ),
        ],
        web_search_requests=1,
    )
    result = _source(resp).fetch()
    assert [p.url for p in result.postings] == ["https://jobs.lever.co/acme/1111/"]
    assert "1 not in results dropped" in result.detail
    assert result.meta["ungrounded_dropped"] == 1
    assert result.meta["grounding_checked"] is True


def test_fetch_skips_the_results_check_when_no_result_urls_came_back():
    # No result blocks at all (an SDK shape we don't read): keep the links rather
    # than empty the run, and say the check didn't happen.
    resp = _Response(
        content=[_Block("text", '{"postings": [{"title": "A", "company": "C", "url": "https://c.example/jobs/a"}]}')],
        web_search_requests=1,
    )
    result = _source(resp).fetch()
    assert len(result.postings) == 1
    assert result.meta["grounding_checked"] is False
    assert result.meta["ungrounded_dropped"] == 0


def test_fetch_drops_aggregator_links(monkeypatch):
    # An aggregator link is dropped before liveness even runs; a direct link
    # survives. filter_live is stubbed all-live by the autouse fixture.
    resp = _Response(
        content=[
            _Block(
                "text",
                '{"postings": ['
                '{"title": "A", "company": "C", "url": "https://www.datacareer.ch/job/1/"},'
                '{"title": "B", "company": "D", "url": "https://boards.greenhouse.io/x/jobs/2"}'
                "]}",
            )
        ],
        web_search_requests=2,
    )
    result = _source(resp).fetch()
    assert [p.url for p in result.postings] == ["https://boards.greenhouse.io/x/jobs/2"]
    assert "2 found" in result.detail
    assert "1 aggregator dropped" in result.detail


def test_fetch_ok_with_postings():
    resp = _Response(
        content=[_Block("text", '{"postings": [{"title": "T", "company": "C", "url": "u"}]}')],
        web_search_requests=2,
    )
    result = _source(resp).fetch()
    assert result.ok
    assert len(result.postings) == 1
    assert "2 searches, 1 found, 1 live" in result.detail


def test_fetch_genuine_empty_after_searching_is_ok():
    # The crucial case: model searched (>=1) and found nothing -> a real quiet
    # day, NOT degraded.
    resp = _Response(content=[_Block("text", '{"postings": []}')], web_search_requests=3)
    result = _source(resp).fetch()
    assert result.ok
    assert result.postings == []
    assert "3 searches, 0 found, 0 live" in result.detail


def test_fetch_empty_without_searching_is_degraded():
    # 0 searches AND 0 postings -> model declined to search; flag as degraded so
    # it isn't reported as a genuine quiet day.
    resp = _Response(content=[_Block("text", '{"postings": []}')], web_search_requests=0)
    result = _source(resp).fetch()
    assert not result.ok
    assert "did not search" in result.detail


def test_fetch_unparseable_output_is_degraded():
    resp = _Response(content=[_Block("text", "I could not find anything useful.")], web_search_requests=2)
    result = _source(resp).fetch()
    assert not result.ok
    assert "unparseable" in result.detail


def test_fetch_no_text_content_is_degraded():
    resp = _Response(content=[_Block("server_tool_use")], web_search_requests=1)
    result = _source(resp).fetch()
    assert not result.ok
    assert "no text content" in result.detail


def test_fetch_reports_unverified_links_apart_from_confirmed_ones(monkeypatch):
    # "Kept" and "confirmed live" were the same number in the old detail string,
    # which is how 2026-08-17's two bot-blocked Swiss Re links read as live.
    def _one_of_each(postings):
        kept = list(postings)
        for i, p in enumerate(kept):
            p.liveness = LIVENESS_CONFIRMED if i == 0 else LIVENESS_UNVERIFIED
        return kept, []

    monkeypatch.setattr("jobradar.search.sources.web_search.filter_live", _one_of_each)
    resp = _Response(
        content=[
            _Block(
                "text",
                '{"postings": ['
                '{"title": "A", "company": "C", "url": "https://c.example/a"},'
                '{"title": "B", "company": "C", "url": "https://c.example/b"}]}',
            )
        ],
        web_search_requests=1,
    )
    result = _source(resp).fetch()
    assert len(result.postings) == 2  # recall bias: the unverified one survives
    assert result.meta["live"] == 1
    assert result.meta["unverified"] == 1
    assert "1 live, 1 unverified" in result.detail
