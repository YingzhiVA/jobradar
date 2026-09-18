from jobradar.search.main import (
    _build_location_desc,
    _build_search_intent,
    _build_sources,
    _drop_uncorroborated,
    _fetch_all,
)
from jobradar.config import EthSource, Sources
from jobradar.models import Constraints
from jobradar.search.sources.base import (
    CORROBORATION_ABSENT,
    CORROBORATION_NONE,
    CORROBORATION_PRESENT,
    LIVENESS_CONFIRMED,
    LIVENESS_LISTED,
    LIVENESS_UNVERIFIED,
    FetchResult,
    RawPosting,
)
from jobradar.search.sources.company_pages import CompanyPagesSource
from jobradar.search.sources.eth import EthJobsSource
from jobradar.search.sources.web_search import WebSearchSource

BASE_CONSTRAINTS = Constraints(
    allowed_countries=[],
    allowed_cities=[],
    allowed_cantons=["Zurich", "Zug", "St.Gallen"],
    remote_ok=False,
    min_percentage=80,
    max_percentage=100,
    max_office_days_per_week=4,
    requires_sponsorship=False,
)


def test_location_desc_names_cantons_and_switzerland():
    desc = _build_location_desc(BASE_CONSTRAINTS)
    assert "Switzerland" in desc
    assert "Zurich" in desc and "St.Gallen" in desc
    # Regression: the old query built region from cities/countries only, which
    # were empty for a canton-based config, producing "anywhere".
    assert "anywhere" not in desc


def test_location_desc_appends_remote_when_allowed():
    constraints = Constraints(**{**BASE_CONSTRAINTS.__dict__, "remote_ok": True})
    assert "remote" in _build_location_desc(constraints).lower()


def test_location_desc_falls_back_when_no_locations():
    constraints = Constraints(**{**BASE_CONSTRAINTS.__dict__, "allowed_cantons": []})
    assert _build_location_desc(constraints) == "any location"


def test_search_intent_includes_identity():
    intent = _build_search_intent(identity="I want data-driven, GenAI-focused product work.")
    assert "GenAI-focused" in intent


def test_search_intent_handles_empty_profile():
    assert _build_search_intent(identity="") == "(no profile information provided)"


_ETH_ON = Sources(eth_jobs=EthSource(enabled=True))


def test_build_sources_omits_arbeitnow_and_includes_web_search():
    sources = _build_sources(
        {"companies": []}, BASE_CONSTRAINTS, identity="x", client=object(),
        use_web_search=True, sources_settings=_ETH_ON,
    )
    types = [type(s) for s in sources]
    assert CompanyPagesSource in types
    assert WebSearchSource in types
    assert EthJobsSource in types
    assert all(type(s).__name__ != "ArbeitnowSource" for s in sources)


def test_build_sources_skips_web_search_when_disabled():
    sources = _build_sources(
        {"companies": []}, BASE_CONSTRAINTS, identity="x", client=object(),
        use_web_search=False, sources_settings=_ETH_ON,
    )
    # company_pages + ETH board stay; only web_search is gated by the flag.
    assert [type(s) for s in sources] == [CompanyPagesSource, EthJobsSource]


def test_build_sources_omits_eth_by_default():
    # The board's categories are role-specific, so nobody gets it unasked.
    sources = _build_sources(
        {"companies": []}, BASE_CONSTRAINTS, identity="x", client=object(), use_web_search=False
    )
    assert [type(s) for s in sources] == [CompanyPagesSource]


def test_build_sources_passes_eth_job_types_from_settings():
    settings = Sources(eth_jobs=EthSource(enabled=True, job_types=(4, 7)))
    sources = _build_sources(
        {"companies": []}, BASE_CONSTRAINTS, identity="x", client=object(),
        use_web_search=False, sources_settings=settings,
    )
    eth = next(s for s in sources if isinstance(s, EthJobsSource))
    assert eth.jobtype_ids == (4, 7)


class _FakeSource:
    def __init__(self, name, result=None, exc=None):
        self.name = name
        self._result = result
        self._exc = exc

    def fetch(self):
        if self._exc is not None:
            raise self._exc
        return self._result


def _raw(name):
    return RawPosting(source=name, url="u", title="t", company="c", description="d")


def test_fetch_all_collects_results_and_marks_healthy():
    good = _FakeSource("company_pages", FetchResult("company_pages", [_raw("a")], ok=True))
    results = _fetch_all([good])
    assert len(results) == 1
    assert results[0].ok
    assert len(results[0].postings) == 1


def test_fetch_all_records_degraded_result_from_source():
    degraded = _FakeSource("web_search", FetchResult("web_search", [], ok=False, detail="0 searches"))
    results = _fetch_all([degraded])
    assert results[0].name == "web_search"
    assert results[0].ok is False


def test_fetch_all_turns_a_raised_exception_into_a_degraded_result():
    # A source that blows up (e.g. web_search timeout) shouldn't kill the run;
    # it becomes a degraded FetchResult so the report knows it's incomplete.
    boom = _FakeSource("web_search", exc=RuntimeError("timeout"))
    results = _fetch_all([boom])
    assert len(results) == 1
    assert results[0].name == "web_search"
    assert results[0].ok is False
    assert "timeout" in results[0].detail


# --- The drop rule: two weak signals agreeing, never one alone ----------------


def _lead(*, liveness, corroboration, title="Analytics Engineer"):
    return RawPosting(
        source="web_search",
        url="https://www.swissre.com/careers/job/Analytics-Engineer/1275367101",
        title=title,
        company="Swiss Re",
        description="d",
        liveness=liveness,
        corroboration=corroboration,
    )


def test_unverified_and_absent_is_dropped():
    # 2026-08-17 exactly: a host that wouldn't answer about a role its own
    # careers site doesn't list.
    lead = _lead(liveness=LIVENESS_UNVERIFIED, corroboration=CORROBORATION_ABSENT)
    kept, dropped = _drop_uncorroborated([lead])
    assert kept == []
    assert dropped == [lead]


def test_unverified_alone_is_kept():
    # Bot protection is routine; on its own it only annotates the report.
    lead = _lead(liveness=LIVENESS_UNVERIFIED, corroboration=CORROBORATION_NONE)
    kept, dropped = _drop_uncorroborated([lead])
    assert kept == [lead]
    assert dropped == []


def test_absent_alone_is_kept():
    # An ATS listing can be paginated short or miss a board, so absence from it
    # cannot drop a link we independently confirmed is reachable.
    lead = _lead(liveness=LIVENESS_CONFIRMED, corroboration=CORROBORATION_ABSENT)
    kept, dropped = _drop_uncorroborated([lead])
    assert kept == [lead]
    assert dropped == []


def test_corroborated_and_confirmed_lead_is_kept():
    lead = _lead(liveness=LIVENESS_CONFIRMED, corroboration=CORROBORATION_PRESENT)
    assert _drop_uncorroborated([lead]) == ([lead], [])


def test_listing_postings_are_never_dropped_by_the_rule():
    # An ATS posting is LISTED/NONE by default and must sail through untouched.
    listed = RawPosting(
        source="successfactors", url="u", title="t", company="Swiss Re", description="d"
    )
    assert listed.liveness == LIVENESS_LISTED
    assert _drop_uncorroborated([listed]) == ([listed], [])


def test_location_desc_mentions_country_scoped_remote_when_configured():
    constraints = Constraints(
        **{**BASE_CONSTRAINTS.__dict__, "remote_ok": False, "remote_countries": ["Switzerland"]}
    )
    desc = _build_location_desc(constraints)
    assert "remote" in desc.lower()
    assert "Switzerland" in desc


def test_location_desc_prefers_unrestricted_remote_when_remote_ok():
    # remote_ok already accepts remote roles anywhere, so the narrower
    # country-scoped phrasing would only contradict it.
    constraints = Constraints(
        **{**BASE_CONSTRAINTS.__dict__, "remote_ok": True, "remote_countries": ["Switzerland"]}
    )
    assert "based in" not in _build_location_desc(constraints)
