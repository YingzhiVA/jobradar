from datetime import date

import httpx

from jobradar import board_health
from jobradar.board_health import (
    BROKEN,
    EMPTY,
    HEALTHY,
    OFF_MARKET,
    UNREACHABLE,
    BoardResult,
    append_history,
    check_board,
    classify,
    load_history,
    mark_actionable,
    render_report,
)
from jobradar.markets import CH
from jobradar.search.sources.base import RawPosting
from jobradar.search.sources.company_pages import LISTING_ROWS_ONLY, _fetch_workday

from .search.test_company_pages import _WD_FACETS_CH, _WorkdayClient


def _posting(location=None):
    return RawPosting(source="x", url="u", title="t", company="c", description="", location=location)


def _board(name="Acme", ats="greenhouse", slug="acme", file="config/boards/ch.yaml"):
    return {"name": name, "ats": ats, "slug": slug, "file": file}


# --- counting postings in the market --------------------------------------------


def test_a_country_or_known_town_counts_as_in_the_market():
    postings = [_posting("Zürich"), _posting("Remote, Switzerland"), _posting("London, UK")]
    assert classify(postings, "greenhouse", CH) == (HEALTHY, 3, 2, [])


def test_lausanne_counts_as_switzerland():
    # Neural Concept and Corintis read as off-market before it was in the table.
    assert classify([_posting("Lausanne")], "ashby", CH)[0] == HEALTHY


def test_a_board_posting_only_elsewhere_is_off_market_with_examples():
    postings = [_posting("London, UK"), _posting("New York"), _posting("London, UK"), _posting("Paris"), _posting("Berlin")]
    assert classify(postings, "ashby", CH) == (OFF_MARKET, 5, 0, ["London, UK", "New York", "Paris"])


def test_a_posting_without_a_location_gets_the_benefit_of_the_doubt():
    # Avature and BrassRing listing rows carry no location.
    assert classify([_posting(None)], "avature", CH)[0] == HEALTHY


def test_a_connector_scoped_to_the_market_counts_everything():
    # Workday rows often say only "2 Locations"; the connector already kept the
    # market's postings and nothing else.
    assert classify([_posting("2 Locations")], "workday", CH) == (HEALTHY, 1, 1, [])


def test_no_postings_is_empty():
    assert classify([], "lever", CH) == (EMPTY, 0, 0, [])


# --- checking one board ---------------------------------------------------------


def test_listing_rows_only_answers_every_lookup():
    assert LISTING_ROWS_ONLY.exact("https://x/1") == "https://x/1"
    assert LISTING_ROWS_ONLY.by_segment("x", "abc")
    assert LISTING_ROWS_ONLY.by_leading_id("x", "42")


def test_a_per_posting_connector_makes_no_detail_request_in_listing_mode():
    pages = {0: {"total": 1, "jobPostings": [
        {"title": "PM", "externalPath": "/job/Zurich/PM_R1", "locationsText": "Zurich"},
    ]}}
    client = _WorkdayClient(_WD_FACETS_CH, pages, details={})
    postings = _fetch_workday("Acme", "acme:wd3:Ext", client, known=LISTING_ROWS_ONLY)
    assert len(postings) == 1
    assert client.detail_calls == 0


def _http_error(code):
    request = httpx.Request("GET", "https://elca.recruitee.com/api/offers/")
    response = httpx.Response(code, request=request)

    def fail(*args, **kwargs):
        raise httpx.HTTPStatusError(f"{code} ...", request=request, response=response)

    return fail


def test_a_board_that_answers_404_is_broken_with_a_short_reason(monkeypatch):
    monkeypatch.setattr(board_health, "fetch_board", _http_error(404))
    result = check_board(_board(), client=None, market=CH)
    assert (result.status, result.note) == (BROKEN, "HTTP 404 at https://elca.recruitee.com/api/offers/")
    assert result.where == "config/boards/ch.yaml"


def test_a_server_error_or_rate_limit_is_unreachable_not_broken(monkeypatch):
    for code in (503, 429):
        monkeypatch.setattr(board_health, "fetch_board", _http_error(code))
        assert check_board(_board(), client=None, market=CH).status == UNREACHABLE


def test_a_timeout_is_unreachable(monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ReadTimeout("The read operation timed out")

    monkeypatch.setattr(board_health, "fetch_board", fail)
    result = check_board(_board(), client=None, market=CH)
    assert (result.status, result.note) == (UNREACHABLE, "The read operation timed out")


def test_a_malformed_slug_is_broken(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("Workday slug must be 'tenant:host:site[:Loc1|Loc2]', got 'x'")

    monkeypatch.setattr(board_health, "fetch_board", fail)
    assert check_board(_board(), client=None, market=CH).status == BROKEN


def test_an_unexpected_error_is_broken_not_fatal(monkeypatch):
    def fail(*args, **kwargs):
        raise KeyError("jobs")

    monkeypatch.setattr(board_health, "fetch_board", fail)
    assert check_board(_board(), client=None, market=CH).note == "KeyError: 'jobs'"


def test_an_ats_without_a_connector_is_broken():
    result = check_board(_board(ats="myspace"), client=None, market=CH)
    assert (result.status, result.note) == (BROKEN, "no connector for ATS 'myspace'")


def test_a_board_of_your_own_names_companies_yaml(monkeypatch):
    monkeypatch.setattr(board_health, "fetch_board", lambda *a, **k: [_posting("Bern")])
    board = {"name": "Mine", "ats": "lever", "slug": "mine"}
    result = check_board(board, client=None, market=CH)
    assert (result.status, result.where) == (HEALTHY, "your companies.yaml")


def test_listing_mode_and_market_reach_the_connector(monkeypatch):
    seen = {}

    def fetch(name, ats, slug, client, known, market):
        seen.update(known=known, market=market)
        return []

    monkeypatch.setattr(board_health, "fetch_board", fetch)
    check_board(_board(), client=None, market=CH)
    assert seen == {"known": LISTING_ROWS_ONLY, "market": CH}


# --- two checks in a row --------------------------------------------------------


def _result(status, name="Acme", slug="acme", market="ch"):
    return BoardResult(market=market, name=name, ats="lever", slug=slug, where="config/boards/ch.yaml", status=status)


def test_broken_is_actionable_at_once():
    results = [_result(BROKEN)]
    mark_actionable(results, history=[])
    assert results[0].actionable


def test_empty_off_market_or_unreachable_needs_the_previous_check_to_agree():
    history = [
        {"market": "ch", "ats": "lever", "slug": "quiet", "status": EMPTY},
        {"market": "ch", "ats": "lever", "slug": "away", "status": EMPTY},
        {"market": "ch", "ats": "lever", "slug": "slow", "status": UNREACHABLE},
    ]
    results = [
        _result(EMPTY, slug="quiet"), _result(OFF_MARKET, slug="away"),
        _result(EMPTY, slug="new"), _result(UNREACHABLE, slug="slow"),
        _result(UNREACHABLE, slug="once"),
    ]
    mark_actionable(results, history)
    assert [r.actionable for r in results] == [True, False, False, True, False]


def test_only_the_latest_check_counts():
    history = [
        {"market": "ch", "ats": "lever", "slug": "acme", "status": EMPTY},
        {"market": "ch", "ats": "lever", "slug": "acme", "status": HEALTHY},
    ]
    results = [_result(EMPTY)]
    mark_actionable(results, history)
    assert not results[0].actionable


def test_the_same_board_in_another_market_has_its_own_history():
    history = [{"market": "uk", "ats": "lever", "slug": "acme", "status": EMPTY}]
    results = [_result(EMPTY, market="ch")]
    mark_actionable(results, history)
    assert not results[0].actionable


def test_history_round_trips_and_skips_damaged_lines(tmp_path):
    path = tmp_path / "data" / "board_health.jsonl"
    append_history(path, [_result(EMPTY)], date(2026, 11, 9))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n{\"market\": \"ch\"}\n")
    assert load_history(path) == [{
        "date": "2026-11-09", "market": "ch", "name": "Acme", "ats": "lever",
        "slug": "acme", "status": EMPTY, "total": 0, "in_market": 0,
    }]


# --- the report -----------------------------------------------------------------


def test_report_lists_only_actionable_boards_as_sections():
    broken = _result(BROKEN, name="Gone")
    broken.note, broken.moved_to, broken.actionable = "404 Not Found", "ashby/gone", True
    away = _result(OFF_MARKET, name="Away")
    away.total, away.sample_locations, away.actionable = 4, ["London, UK"], True
    quiet = _result(EMPTY, name="Quiet")  # first sighting: watched, not flagged
    text = render_report([broken, away, quiet, _result(HEALTHY, name="Fine")], "your selection", date(2026, 11, 9))
    assert "1 healthy, 1 off-market, 1 empty, 0 unreachable, 1 broken" in text
    assert "- **Gone** (lever/acme, in config/boards/ch.yaml) — 404 Not Found. **Possibly moved to ashby/gone.**" in text
    assert "- **Away** (lever/acme, in config/boards/ch.yaml) — 4 posting(s), none in Switzerland (e.g. London, UK)." in text
    assert "Watching, first time this way (flagged if the next check agrees): Quiet (empty)." in text
    # notify_discovery counts "- **" lines to decide whether to email.
    assert text.count("\n- **") == 2


def test_report_tags_the_market_when_several_were_checked():
    a, b = _result(BROKEN, market="ch"), _result(BROKEN, name="Other", market="uk")
    a.actionable = b.actionable = True
    text = render_report([a, b], "x", date(2026, 11, 9))
    assert "- **Acme** [ch] (" in text and "- **Other** [uk] (" in text


def test_a_clean_report_says_so_and_has_no_sections():
    text = render_report([_result(HEALTHY)], "your selection", date(2026, 11, 9))
    assert "Nothing to act on." in text
    assert "\n## " not in text
