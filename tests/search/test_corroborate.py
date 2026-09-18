from jobradar.search.corroborate import _titles_match, build_listings, corroborate
from jobradar.search.sources.base import (
    CORROBORATION_ABSENT,
    CORROBORATION_NONE,
    CORROBORATION_PRESENT,
    FetchResult,
    RawPosting,
)


def _posting(company, title, *, source="successfactors", url="https://x/1"):
    return RawPosting(
        source=source, url=url, title=title, company=company, description="d"
    )


def _listing(*postings, name="company_pages", ok=True):
    return FetchResult(name, list(postings), ok=ok)


def _leads(*postings):
    return FetchResult("web_search", list(postings))


# --- Title matching: the trap is the near miss, not the mismatch --------------


def test_identical_titles_match():
    assert _titles_match("Analytics Engineer", "Analytics Engineer") is True


def test_titles_match_through_punctuation_and_seniority_noise():
    assert _titles_match(
        "Senior AI/ML Product Manager (Hybrid; m/f/x/d; 80-100%)",
        "AI ML Product Manager",
    ) is True


def test_adjacent_but_different_roles_do_not_match():
    # Swiss Re's real listing on 2026-08-17 held both of these; a looser matcher
    # would have accepted either as corroborating the dead lead, which is exactly
    # how this check would fail silently.
    assert _titles_match("AI & Analytics Product Engineer", "Analytics Product Expert") is False
    assert _titles_match("AI & Analytics Product Engineer", "AI Engineer") is False
    assert _titles_match("Analytics Engineer", "Senior Data Architect") is False


def test_empty_title_never_matches():
    assert _titles_match("", "Analytics Engineer") is False
    assert _titles_match("Analytics Engineer", "") is False


# --- Verdicts ----------------------------------------------------------------


def test_lead_absent_from_employer_listing_is_absent():
    results = [
        _listing(
            _posting("Swiss Re", "Analytics Product Expert"),
            _posting("Swiss Re", "AI Engineer"),
        ),
        _leads(_posting("Swiss Re", "AI & Analytics Product Engineer", source="web_search")),
    ]
    assert corroborate(results)[CORROBORATION_ABSENT] == 1
    assert results[1].postings[0].corroboration == CORROBORATION_ABSENT


def test_lead_present_in_employer_listing_is_present():
    results = [
        _listing(_posting("Swiss Re", "Senior Data Architect (m/f/x/d)")),
        _leads(_posting("Swiss Re", "Data Architect", source="web_search")),
    ]
    assert corroborate(results)[CORROBORATION_PRESENT] == 1
    assert results[1].postings[0].corroboration == CORROBORATION_PRESENT


def test_company_with_no_listing_this_run_gets_no_opinion():
    # UBS isn't on the scanned company list, so its absence says nothing at all.
    results = [
        _listing(_posting("Swiss Re", "AI Engineer")),
        _leads(_posting("UBS", "Product Manager, Digital Solutions & AI", source="web_search")),
    ]
    assert corroborate(results)[CORROBORATION_NONE] == 1
    assert results[1].postings[0].corroboration == CORROBORATION_NONE


def test_company_name_matched_across_legal_suffixes():
    results = [
        _listing(_posting("Onedot AG", "AI Platform Product Manager")),
        _leads(_posting("Onedot", "AI Platform Product Manager", source="web_search")),
    ]
    corroborate(results)
    assert results[1].postings[0].corroboration == CORROBORATION_PRESENT


def test_degraded_listing_source_is_not_used_as_evidence():
    # A source that failed may have fetched a partial set of a company's roles;
    # treating that as a complete listing manufactures false absences.
    results = [
        _listing(_posting("Swiss Re", "AI Engineer"), ok=False),
        _leads(_posting("Swiss Re", "Analytics Engineer", source="web_search")),
    ]
    assert corroborate(results)[CORROBORATION_NONE] == 1


def test_multi_employer_board_is_not_a_company_listing():
    # eth_jobs lists many employers' roles, so it is not ETH's own full listing
    # and cannot establish that a role is absent.
    results = [
        FetchResult("eth_jobs", [_posting("ETH Zürich", "Postdoc", source="eth_jobs")]),
        _leads(_posting("ETH Zürich", "Product Manager AI Center", source="web_search")),
    ]
    assert corroborate(results)[CORROBORATION_NONE] == 1


def test_listing_source_postings_are_not_themselves_judged():
    results = [_listing(_posting("Swiss Re", "AI Engineer"))]
    assert corroborate(results) == {
        CORROBORATION_NONE: 0,
        CORROBORATION_PRESENT: 0,
        CORROBORATION_ABSENT: 0,
    }
    assert results[0].postings[0].corroboration == CORROBORATION_NONE


def test_build_listings_indexes_only_listing_sources():
    results = [
        _listing(_posting("Swiss Re AG", "AI Engineer")),
        _leads(_posting("Nowhere Inc", "Something", source="web_search")),
    ]
    listings = build_listings(results)
    assert set(listings) == {"re swiss"}
