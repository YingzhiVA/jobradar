"""The company list is opt-in: the template ships with every board commented out,
and people then uncomment the boards they want, three lines at a time."""

import logging

from jobradar.search.sources import company_pages
from jobradar.search.sources.base import RawPosting
from jobradar.search.sources.company_pages import CompanyPagesSource


def _fake_fetcher(name, slug, client):
    return [RawPosting(source="fake", url=f"https://x/{slug}", title="t", company=name, description="d")]


def test_all_entries_commented_out_is_no_boards_not_a_crash():
    # `companies:` with only comments under it parses as None.
    result = CompanyPagesSource(None).fetch()
    assert result.ok
    assert result.postings == []


def test_half_uncommented_entry_costs_one_board_not_all(monkeypatch, caplog):
    monkeypatch.setitem(company_pages._FETCHERS, "fake", _fake_fetcher)
    companies = [
        {"name": "Good AG", "ats": "fake", "slug": "good"},
        {"name": "Half Done AG"},                          # ats/slug left commented
        {"ats": "fake", "slug": "orphan"},                 # name left commented
        {"name": "Also Good AG", "ats": "fake", "slug": "also"},
    ]
    with caplog.at_level(logging.WARNING):
        result = CompanyPagesSource(companies).fetch()
    assert sorted(p.company for p in result.postings) == ["Also Good AG", "Good AG"]
    assert "Half Done AG" in result.detail
    assert "has no ats or slug" in caplog.text
