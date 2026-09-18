from jobradar.search.filters import company_is_named, could_pass_location, filter_postings
from jobradar.models import Constraints, Posting

CONSTRAINTS = Constraints(
    allowed_countries=["Switzerland"],
    allowed_cities=["Zurich", "Zug", "Basel"],
    allowed_cantons=[],
    remote_ok=True,
    min_percentage=80,
    max_percentage=100,
    max_office_days_per_week=3,
    requires_sponsorship=False,
)


def make_posting(**overrides) -> Posting:
    defaults = dict(
        id="id",
        source="test",
        url="https://example.com/job",
        title="Test Role",
        company="Test Co",
        description="A great role.",
        location_text="Zurich, Switzerland",
        remote=None,
        employment_pct_min=100,
        employment_pct_max=100,
        office_days_per_week=2,
        sponsorship_mentioned=None,
    )
    defaults.update(overrides)
    return Posting(**defaults)


def test_company_is_named_accepts_real_company():
    assert company_is_named(make_posting(company="Swiss Re")) is True


def test_company_is_named_rejects_undisclosed_variants():
    for name in ["Undisclosed", "Undisclosed (appears in multiple Swiss job boards)",
                 "Unknown Banking/Financial Services", "Stealth Startup", "Confidential"]:
        assert company_is_named(make_posting(company=name)) is False, name


def test_company_is_named_rejects_empty_and_placeholders():
    for name in ["", "   ", "n/a", "TBD", "-"]:
        assert company_is_named(make_posting(company=name)) is False, repr(name)


def test_passes_when_everything_within_bounds():
    kept, results = filter_postings([make_posting()], CONSTRAINTS)
    assert len(kept) == 1
    assert results[0].passed


def test_dropped_when_location_not_allowed_and_not_remote():
    posting = make_posting(location_text="Lagos, Nigeria", remote=False)
    kept, results = filter_postings([posting], CONSTRAINTS)
    assert kept == []
    assert results[0].failed_checks == ["location"]


def test_remote_passes_even_outside_allowed_cities_when_remote_ok():
    posting = make_posting(location_text="Remote", remote=True)
    kept, _ = filter_postings([posting], CONSTRAINTS)
    assert kept == [posting]


def test_remote_does_not_help_when_remote_ok_is_false():
    constraints = Constraints(**{**CONSTRAINTS.__dict__, "remote_ok": False})
    posting = make_posting(location_text="Remote", remote=True)
    kept, results = filter_postings([posting], constraints)
    assert kept == []
    assert results[0].failed_checks == ["location"]


def test_employment_pct_range_overlap_passes():
    # Posting offers 60-100%; constraint wants 80-100% -> overlap exists.
    posting = make_posting(employment_pct_min=60, employment_pct_max=100)
    kept, _ = filter_postings([posting], CONSTRAINTS)
    assert kept == [posting]


def test_employment_pct_no_overlap_fails():
    # Posting caps out at 60%; constraint wants at least 80%.
    posting = make_posting(employment_pct_min=20, employment_pct_max=60)
    kept, results = filter_postings([posting], CONSTRAINTS)
    assert kept == []
    assert results[0].failed_checks == ["employment_pct"]


def test_employment_pct_unknown_is_not_dropped():
    posting = make_posting(employment_pct_min=None, employment_pct_max=None)
    kept, _ = filter_postings([posting], CONSTRAINTS)
    assert kept == [posting]


def test_office_days_over_limit_fails():
    posting = make_posting(office_days_per_week=5)
    kept, results = filter_postings([posting], CONSTRAINTS)
    assert kept == []
    assert results[0].failed_checks == ["office_days"]


def test_office_days_unknown_is_not_dropped():
    posting = make_posting(office_days_per_week=None)
    kept, _ = filter_postings([posting], CONSTRAINTS)
    assert kept == [posting]


def test_multiple_failed_checks_are_all_reported():
    posting = make_posting(
        location_text="Lagos, Nigeria",
        remote=False,
        office_days_per_week=5,
    )
    kept, results = filter_postings([posting], CONSTRAINTS)
    assert kept == []
    assert set(results[0].failed_checks) == {"location", "office_days"}


CANTON_CONSTRAINTS = Constraints(
    **{**CONSTRAINTS.__dict__, "allowed_cantons": ["Zug"], "allowed_cities": [], "allowed_countries": []}
)


def test_canton_match_passes_even_when_city_text_does_not_match():
    # A town within the allowed canton that a plain city allowlist would
    # miss (the original motivation for allowed_cantons).
    posting = make_posting(location_text="Cham, Switzerland", canton="Zug")
    kept, _ = filter_postings([posting], CANTON_CONSTRAINTS)
    assert kept == [posting]


def test_canton_mismatch_fails():
    posting = make_posting(location_text="Lausanne, Switzerland", canton="Vaud")
    kept, results = filter_postings([posting], CANTON_CONSTRAINTS)
    assert kept == []
    assert results[0].failed_checks == ["location"]


def test_canton_naming_variant_still_matches():
    # constraint says "Basel"; resolved canton is "Basel-Stadt" — should
    # still match via substring containment, not exact equality.
    constraints = Constraints(**{**CONSTRAINTS.__dict__, "allowed_cantons": ["Basel"], "allowed_cities": []})
    posting = make_posting(location_text="Basel", canton="Basel-Stadt")
    kept, _ = filter_postings([posting], constraints)
    assert kept == [posting]


def test_canton_punctuation_variant_still_matches():
    # constraints.yaml says "St.Gallen" (no space); normalize.py's canonical
    # name is "St. Gallen" (with space) — should still match.
    constraints = Constraints(**{**CONSTRAINTS.__dict__, "allowed_cantons": ["St.Gallen"], "allowed_cities": []})
    posting = make_posting(location_text="St. Gallen, Switzerland", canton="St. Gallen")
    kept, _ = filter_postings([posting], constraints)
    assert kept == [posting]


def test_unresolved_canton_does_not_satisfy_canton_constraint():
    # canton=None (unresolved) shouldn't accidentally pass — it should fall
    # through to the city/country substring checks instead.
    posting = make_posting(location_text="Lagos, Nigeria", remote=False, canton=None)
    kept, results = filter_postings([posting], CANTON_CONSTRAINTS)
    assert kept == []
    assert results[0].failed_checks == ["location"]


_CANTON_NO_REMOTE = Constraints(
    **{
        **CONSTRAINTS.__dict__,
        "allowed_cantons": ["Zug"],
        "allowed_cities": [],
        "allowed_countries": [],
        "remote_ok": False,
    }
)


def test_could_pass_location_drops_clearly_foreign_without_llm():
    # A Databricks-style foreign role: no Swiss signal, canton unresolved —
    # canton resolution can't rescue it, so don't spend an LLM call.
    p = make_posting(location_text="San Francisco, California, United States", canton=None, remote=None)
    assert could_pass_location(p, _CANTON_NO_REMOTE) is False


def test_could_pass_location_keeps_swiss_signal_for_llm():
    # Town not in the heuristic dict, canton still unresolved, but the text
    # says Switzerland — worth an LLM canton call (it may resolve to Zug).
    p = make_posting(location_text="Cham, Switzerland", canton=None, remote=None, description="A role.")
    assert could_pass_location(p, _CANTON_NO_REMOTE) is True


def test_could_pass_location_keeps_already_passing():
    p = make_posting(location_text="Zug, Switzerland", canton="Zug")
    assert could_pass_location(p, _CANTON_NO_REMOTE) is True


def test_could_pass_location_keeps_unknown_location():
    p = make_posting(location_text=None, canton=None, remote=None, description="No location given.")
    assert could_pass_location(p, _CANTON_NO_REMOTE) is True


def test_could_pass_location_false_when_no_canton_list_and_location_fails():
    constraints = Constraints(
        **{**CONSTRAINTS.__dict__, "allowed_cantons": [], "allowed_cities": ["Zurich"], "remote_ok": False}
    )
    p = make_posting(location_text="Berlin, Germany", canton=None, remote=None)
    assert could_pass_location(p, constraints) is False


def test_allowed_countries_for_the_same_country_bypasses_canton_restriction():
    # Known gotcha (documented in config/constraints.yaml): allowed_cantons
    # and allowed_countries are OR'd like every other location check. Setting
    # allowed_countries: ["Switzerland"] alongside allowed_cantons makes any
    # Swiss posting pass on the country check alone, regardless of canton —
    # so leave allowed_countries empty for any country you're restricting by
    # canton, or the canton restriction is effectively a no-op.
    constraints = Constraints(**{**CANTON_CONSTRAINTS.__dict__, "allowed_countries": ["Switzerland"]})
    posting = make_posting(location_text="Lausanne, Switzerland", canton="Vaud")
    kept, _ = filter_postings([posting], constraints)
    assert kept == [posting]  # passes despite Vaud not being in allowed_cantons=["Zug"]


# --- remote_countries: remote roles a canton can never be resolved for ---

# Mirrors the real config: commute-scoped cantons, remote_ok off (a remote role
# in the US is not wanted), but remote-within-Switzerland accepted.
_REMOTE_CH_CONSTRAINTS = Constraints(
    allowed_countries=[],
    allowed_cities=[],
    allowed_cantons=["Zurich", "Zug"],
    remote_ok=False,
    min_percentage=80,
    max_percentage=100,
    max_office_days_per_week=3,
    requires_sponsorship=False,
    remote_countries=["Switzerland"],
)


def test_remote_country_posting_passes_without_a_canton():
    # The Jobgether shape: remote, country-level location, no town anywhere.
    posting = make_posting(location_text="Switzerland", canton=None, remote=True)
    kept, _ = filter_postings([posting], _REMOTE_CH_CONSTRAINTS)
    assert kept == [posting]


def test_remote_country_does_not_admit_other_countries():
    posting = make_posting(location_text="US", canton=None, remote=True)
    kept, results = filter_postings([posting], _REMOTE_CH_CONSTRAINTS)
    assert kept == []
    assert "location" in results[0].failed_checks


def test_remote_country_does_not_admit_non_remote_swiss_postings():
    # The whole point of the narrow option: an office-based role still has to
    # clear the canton list, so the commute scoping stays intact.
    posting = make_posting(location_text="Lausanne, Switzerland", canton="Vaud", remote=False)
    kept, _ = filter_postings([posting], _REMOTE_CH_CONSTRAINTS)
    assert kept == []


def test_remote_country_reads_location_not_description():
    # A remote US role whose blurb mentions a Swiss head office must not pass.
    posting = make_posting(
        location_text="Austin, Texas, United States",
        canton=None,
        remote=True,
        description="We are headquartered in Zurich, Switzerland.",
    )
    kept, _ = filter_postings([posting], _REMOTE_CH_CONSTRAINTS)
    assert kept == []


def test_remote_country_accepts_local_language_spelling():
    posting = make_posting(location_text="Schweiz", canton=None, remote=True)
    kept, _ = filter_postings([posting], _REMOTE_CH_CONSTRAINTS)
    assert kept == [posting]


def test_remote_country_needs_an_actual_location():
    # Unknown location is treated as no match, like every other location check.
    posting = make_posting(location_text=None, canton=None, remote=True)
    kept, _ = filter_postings([posting], _REMOTE_CH_CONSTRAINTS)
    assert kept == []


def test_remote_country_is_off_when_unconfigured():
    constraints = Constraints(**{**_REMOTE_CH_CONSTRAINTS.__dict__, "remote_countries": []})
    posting = make_posting(location_text="Switzerland", canton=None, remote=True)
    kept, _ = filter_postings([posting], constraints)
    assert kept == []
