from jobradar.models import Constraints
from jobradar.search.normalize import _ExtractedFields, heuristic_normalize, normalize
from jobradar.search.sources.base import RawPosting


def make_raw(description: str, location: str | None = "Zurich") -> RawPosting:
    return RawPosting(
        source="test",
        url="https://example.com/job",
        title="Test Role",
        company="Test Co",
        description=description,
        location=location,
    )


def test_single_percent_sign_range_keeps_both_bounds():
    # Regression test: "80-100%" must not collapse to (100, 100) by only
    # matching the trailing percent sign.
    posting = heuristic_normalize(make_raw("This role is 80-100%."))
    assert (posting.employment_pct_min, posting.employment_pct_max) == (80, 100)


def test_percent_sign_after_both_numbers_still_works():
    posting = heuristic_normalize(make_raw("This role is 80%-100%."))
    assert (posting.employment_pct_min, posting.employment_pct_max) == (80, 100)


def test_single_percentage_value():
    posting = heuristic_normalize(make_raw("This is a 100% role."))
    assert (posting.employment_pct_min, posting.employment_pct_max) == (100, 100)


def test_full_time_keyword_without_percentage():
    posting = heuristic_normalize(make_raw("This is a full-time position."))
    assert (posting.employment_pct_min, posting.employment_pct_max) == (100, 100)


def test_no_percentage_information_is_unknown():
    posting = heuristic_normalize(make_raw("A role with no workload details."))
    assert posting.employment_pct_min is None
    assert posting.employment_pct_max is None


def test_office_days_detected_with_week_keyword():
    posting = heuristic_normalize(make_raw("Hybrid: 2 days a week in our Zurich office."))
    assert posting.office_days_per_week == 2


def test_remote_detected_from_location():
    posting = heuristic_normalize(make_raw("Fully remote role.", location="Remote"))
    assert posting.remote is True


def test_onsite_detected_from_location():
    posting = heuristic_normalize(make_raw("desc", location="Onsite, Berlin"))
    assert posting.remote is False


def test_canton_resolved_from_city_with_same_name():
    posting = heuristic_normalize(make_raw("desc", location="Zug, Switzerland"))
    assert posting.canton == "Zug"


def test_canton_resolved_from_basel_maps_to_basel_stadt():
    posting = heuristic_normalize(make_raw("desc", location="Basel"))
    assert posting.canton == "Basel-Stadt"


def test_canton_resolved_from_winterthur_maps_to_zurich():
    # Winterthur is a city within canton Zurich, not named "Zurich" itself —
    # exactly the case a plain city allowlist would miss.
    posting = heuristic_normalize(make_raw("desc", location="Winterthur"))
    assert posting.canton == "Zurich"


def test_canton_resolved_from_sankt_gallen_metropolitan_area():
    # Regression: Frontify (and others) label the location "Sankt Gallen
    # Metropolitan Area" — the German "Sankt" spelling must resolve via the
    # heuristic instead of forcing an LLM call on every such posting.
    posting = heuristic_normalize(make_raw("desc", location="Sankt Gallen Metropolitan Area"))
    assert posting.canton == "St. Gallen"


def test_canton_unresolved_for_unknown_town():
    # Towns not in the high-confidence dict (e.g. most of Vaud/Aargau/Ticino)
    # fall through to the LLM fallback rather than being guessed here.
    posting = heuristic_normalize(make_raw("desc", location="Nyon"))
    assert posting.canton is None


def test_canton_unresolved_for_non_swiss_location():
    posting = heuristic_normalize(make_raw("desc", location="Berlin"))
    assert posting.canton is None


def test_canton_not_misattributed_from_description_boilerplate():
    # Regression: a multi-office company's description mentioning its Swiss
    # HQ ("headquartered in St. Gallen... offices in London and New York")
    # must not make a New York-based posting resolve to a Swiss canton — the
    # heuristic should only look at the location field, not the description.
    posting = heuristic_normalize(
        make_raw(
            "With headquarters in St. Gallen, Switzerland, and offices in "
            "London and New York City, we're hiring a Director.",
            location="New York, NY",
        )
    )
    assert posting.canton is None


class _FakeParsedResponse:
    parsed_output = None


class _FakeMessages:
    def __init__(self):
        self.called = False

    def parse(self, **kwargs):
        self.called = True
        return _FakeParsedResponse()


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


# A description that fully resolves employment_pct and office_days via
# heuristics, so these tests isolate the canton-gating condition rather than
# being confounded by needs_llm already being True for other reasons.
_FULLY_RESOLVED_DESCRIPTION = "100% full-time. 2 days a week in the office."

_CANTON_CONSTRAINTS = Constraints(
    allowed_countries=[],
    allowed_cities=[],
    allowed_cantons=["Zug"],
    remote_ok=True,
    min_percentage=80,
    max_percentage=100,
    max_office_days_per_week=3,
    requires_sponsorship=False,
)


class _StubExtractionResponse:
    def __init__(self, extracted):
        self.parsed_output = extracted


class _StubMessages:
    def __init__(self, extracted):
        self._extracted = extracted

    def parse(self, **kwargs):
        return _StubExtractionResponse(self._extracted)


class _StubClient:
    """A client whose extraction call returns a fixed _ExtractedFields, so the
    canton-verification logic can be tested without a live LLM.
    """

    def __init__(self, **extracted_fields):
        self.messages = _StubMessages(_ExtractedFields(**extracted_fields))


# The description that caused the real miss: Frontify's boilerplate names its
# Swiss HQ, and the LLM read the canton off it for a London-based posting.
_SWISS_HQ_BOILERPLATE = (
    "Frontify is a brand-building platform. With headquarters in St. Gallen, "
    "Switzerland, and offices in London and New York City, we share a vibrant "
    "culture. 100% full-time. 2 days a week in the office."
)


def test_llm_canton_rejected_when_not_traceable_to_location():
    # Regression (report 2026-07-17): a "London Area" posting surfaced because
    # the LLM resolved canton "St. Gallen" from the company's HQ boilerplate,
    # which then satisfied allowed_cantons and passed the hard location filter.
    # A canton the model can't point at in the location line must be discarded.
    client = _StubClient(canton="St. Gallen", canton_source="St. Gallen")
    posting = normalize(
        make_raw(_SWISS_HQ_BOILERPLATE, location="London Area"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert posting.canton is None


def test_llm_canton_rejected_when_source_field_missing():
    # No canton_source at all means nothing to verify against — don't take the
    # canton on trust, since that's the exact path the boilerplate leaks through.
    client = _StubClient(canton="St. Gallen", canton_source=None)
    posting = normalize(
        make_raw(_SWISS_HQ_BOILERPLATE, location="London Area"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert posting.canton is None


def test_llm_canton_accepted_when_traceable_to_location():
    # The path the LLM fallback exists for: a Swiss town the heuristic dict
    # doesn't know, resolved from the location line and verifiable against it.
    client = _StubClient(canton="Vaud", canton_source="Nyon")
    posting = normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION, location="Nyon, Switzerland"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert posting.canton == "Vaud"


def test_llm_canton_source_match_is_case_insensitive():
    client = _StubClient(canton="Vaud", canton_source="nyon")
    posting = normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION, location="Nyon, Switzerland"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert posting.canton == "Vaud"


def test_llm_canton_accepted_from_description_when_no_location_given():
    # With no location line there's nothing to verify against, and the
    # description is the only signal there is — could_pass_location() lets
    # these reach the LLM precisely so they aren't dropped unseen.
    client = _StubClient(canton="Zug", canton_source=None)
    posting = normalize(
        make_raw("Based in our Zug office. " + _FULLY_RESOLVED_DESCRIPTION, location=None),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert posting.canton == "Zug"


def test_heuristic_canton_is_not_overwritten_by_llm():
    # heuristic_normalize() already read the canton off the location field, so
    # the LLM's answer must not get a chance to override it.
    client = _StubClient(canton="Vaud", canton_source="Lausanne")
    posting = normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION, location="Zug"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert posting.canton == "Zug"


def test_rotkreuz_resolves_to_zug_without_the_llm():
    # The Roche board is scoped to Rotkreuz, so this town arrives daily and on
    # multi-site reqs whose primary location is abroad ("Indianapolis; Rotkreuz;
    # ..."). Resolving it in the heuristic keeps those postings off the LLM path.
    client = _FakeClient()
    posting = normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION, location="Indianapolis; Rotkreuz; Mannheim"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert posting.canton == "Zug"
    assert client.messages.called is False


def test_kaiseraugst_resolves_to_aargau_without_the_llm():
    # Roche's second Swiss site, in scope since the board was widened
    # (2026-09-07); same daily-recurrence argument as Rotkreuz.
    client = _FakeClient()
    posting = normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION, location="Kaiseraugst"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert posting.canton == "Aargau"
    assert client.messages.called is False


def test_staefa_resolves_to_zurich_without_the_llm():
    # Sonova's board spells its Stäfa headquarters "Staefa, Switzerland".
    client = _FakeClient()
    posting = normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION, location="Staefa, Switzerland"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert posting.canton == "Zurich"
    assert client.messages.called is False


def test_normalize_skips_llm_when_constraints_not_given():
    # Town not in the heuristic dict, so canton stays unresolved — but
    # nothing downstream needs it (no constraints passed), so no LLM call.
    client = _FakeClient()
    normalize(make_raw(_FULLY_RESOLVED_DESCRIPTION, location="Nyon"), client, constraints=None)
    assert client.messages.called is False


def test_normalize_skips_llm_when_allowed_cantons_not_configured():
    constraints = Constraints(**{**_CANTON_CONSTRAINTS.__dict__, "allowed_cantons": []})
    client = _FakeClient()
    normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION, location="Nyon"), client, constraints=constraints
    )
    assert client.messages.called is False


def test_normalize_calls_llm_when_canton_resolution_requested_and_unresolved():
    client = _FakeClient()
    normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION, location="Nyon"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert client.messages.called is True


def test_normalize_skips_llm_when_canton_already_resolved_by_heuristic():
    client = _FakeClient()
    normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION, location="Zug"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert client.messages.called is False


def test_normalize_skips_llm_when_posting_already_confirmed_remote_and_remote_ok():
    # The short-circuit: a confirmed-remote posting passes filters.py's
    # location check on the remote branch alone (remote_ok=True), so
    # resolving canton would be wasted work regardless of allowed_cantons.
    client = _FakeClient()
    normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION + " Fully remote.", location="Remote"),
        client,
        constraints=_CANTON_CONSTRAINTS,
    )
    assert client.messages.called is False


def test_normalize_still_resolves_canton_for_remote_when_remote_ok_is_false():
    # If remote_ok is false, a "remote" posting doesn't auto-pass location,
    # so canton still might matter (e.g. "Remote (Switzerland only)").
    constraints = Constraints(**{**_CANTON_CONSTRAINTS.__dict__, "remote_ok": False})
    client = _FakeClient()
    normalize(
        make_raw(_FULLY_RESOLVED_DESCRIPTION + " Fully remote.", location="Remote"),
        client,
        constraints=constraints,
    )
    assert client.messages.called is True


# --- structured remote flag from the source outranks the text heuristic ---


def test_source_remote_flag_wins_over_location_text():
    # The Jobgether shape: the board states workplaceType "remote" while its
    # location line is a bare country the heuristic can read nothing out of.
    raw = RawPosting(
        source="lever",
        url="https://example.com/job",
        title="Test Role",
        company="Test Co",
        description="A great role.",
        location="Switzerland",
        remote=True,
    )
    assert heuristic_normalize(raw).remote is True


def test_source_remote_flag_can_say_not_remote():
    raw = RawPosting(
        source="lever",
        url="https://example.com/job",
        title="Test Role",
        company="Test Co",
        description="A great role.",
        location="Remote-first company, Zurich",
        remote=False,
    )
    assert heuristic_normalize(raw).remote is False


def test_falls_back_to_heuristic_when_source_says_nothing():
    assert heuristic_normalize(make_raw("A role.", location="Remote")).remote is True
    assert heuristic_normalize(make_raw("A role.", location="Zurich")).remote is None


def test_remote_country_posting_skips_the_canton_llm_call():
    # These postings carry a country-level location by definition, so a canton
    # call could never resolve one — it would be spent for nothing on every
    # posting the board returns.
    constraints = Constraints(
        allowed_countries=[],
        allowed_cities=[],
        allowed_cantons=["Zug"],
        remote_ok=False,
        min_percentage=80,
        max_percentage=100,
        max_office_days_per_week=3,
        requires_sponsorship=False,
        remote_countries=["Switzerland"],
    )
    raw = RawPosting(
        source="lever",
        url="https://example.com/job",
        title="Test Role",
        company="Test Co",
        description=_FULLY_RESOLVED_DESCRIPTION,
        location="Switzerland",
        remote=True,
    )
    client = _StubClient(canton="Zug", canton_source="Zug")
    posting = normalize(raw, client, constraints=constraints)
    assert posting.canton is None  # the stub was never consulted
