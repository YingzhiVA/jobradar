from jobradar.models import Posting, ScoredPosting
from jobradar.search.ranking import (
    OUTCOME_BELOW_FLOOR,
    OUTCOME_BEST,
    OUTCOME_DEFERRED,
    OUTCOME_OKAY,
    OUTCOME_UNMET_HARD,
    classify_outcome,
    is_eligible,
    select_matches,
)

BEST_THRESHOLD = 80
MIN_SKILL = 60
MAX_OKAY = 3


def make_scored(skill: int, interest: int, label: str) -> ScoredPosting:
    posting = Posting(
        id=label,
        source="test",
        url=f"https://example.com/{label}",
        title=label,
        company="Co",
        description="desc",
    )
    return ScoredPosting(
        posting=posting, skill_score=skill, interest_score=interest, best_cv="cv", brief_reason="why"
    )


def test_empty_input_selects_nothing():
    assert select_matches([]) == []


def test_below_skill_floor_selects_nothing():
    # Interest is irrelevant to the gate; both of these are below the skill floor.
    scored = [make_scored(50, 50, "a"), make_scored(55, 90, "b")]
    assert select_matches(scored) == []


def test_high_skill_low_interest_surfaces():
    # The motivating case for skill-only gating: a role the user is clearly
    # qualified for (skill 95) surfaces even at interest 40, as RAV-quota fodder.
    selected = select_matches([make_scored(95, 40, "qualified")])
    assert len(selected) == 1
    assert selected[0].tier == "okay"
    assert selected[0].scored.posting.id == "qualified"


def test_high_interest_low_skill_is_dropped():
    # Strong interest can't carry a role the user isn't qualified for — skill gates.
    assert select_matches([make_scored(45, 95, "unqualified")]) == []


def test_single_best_clears_best_threshold():
    selected = select_matches([make_scored(90, 90, "a")])
    assert len(selected) == 1
    assert selected[0].tier == "best"
    assert selected[0].scored.posting.id == "a"


def test_low_interest_role_cannot_be_best():
    # Skill alone clears the gate, but "best" needs a high combined score, so a
    # low-interest role surfaces as "okay", never as the headline "best".
    selected = select_matches([make_scored(95, 30, "a")])
    assert len(selected) == 1
    assert selected[0].tier == "okay"


def test_eligible_below_best_threshold_is_okay():
    # Skill clears the floor; combined 66 is below best (80).
    selected = select_matches([make_scored(70, 62, "a")])
    assert len(selected) == 1
    assert selected[0].tier == "okay"


def test_best_plus_multiple_okay():
    scored = [
        make_scored(95, 95, "best"),
        make_scored(70, 62, "okay1"),
        make_scored(64, 60, "okay2"),
        make_scored(72, 40, "qualified"),  # low interest, but skill clears -> okay
    ]
    selected = select_matches(scored)
    tiers = {s.scored.posting.id: s.tier for s in selected}
    assert tiers == {"best": "best", "okay1": "okay", "okay2": "okay", "qualified": "okay"}


def test_okay_results_capped_at_max_okay():
    scored = [make_scored(95, 95, "best")] + [make_scored(70, 62, f"okay{i}") for i in range(5)]
    selected = select_matches(scored)
    assert selected[0].tier == "best"
    assert sum(s.tier == "okay" for s in selected) == MAX_OKAY


def test_okay_without_best_capped_at_max_okay():
    scored = [make_scored(70, 62, f"okay{i}") for i in range(6)]
    selected = select_matches(scored)
    assert len(selected) == MAX_OKAY
    assert all(s.tier == "okay" for s in selected)


def test_results_sorted_best_first_then_okay_by_combined():
    scored = [
        make_scored(62, 62, "lowest_okay"),   # combined 62
        make_scored(95, 95, "best"),          # combined 95
        make_scored(72, 78, "highest_okay"),  # combined 75
    ]
    selected = select_matches(scored)
    ids = [s.scored.posting.id for s in selected]
    assert ids == ["best", "highest_okay", "lowest_okay"]


def test_is_eligible_gates_on_skill_only():
    assert is_eligible(make_scored(60, 55, "a")) is True
    assert is_eligible(make_scored(95, 40, "b")) is True   # low interest no longer gates
    assert is_eligible(make_scored(45, 95, "c")) is False  # skill below floor


def test_classify_surfaced_uses_tier():
    s = make_scored(90, 90, "a")
    assert classify_outcome(s, "best") == OUTCOME_BEST
    assert classify_outcome(s, "okay") == OUTCOME_OKAY


def test_classify_eligible_but_unsurfaced_is_deferred():
    # Cleared the skill floor but no tier assigned -> quota-deferred, resurfaces.
    assert classify_outcome(make_scored(70, 62, "a"), None) == OUTCOME_DEFERRED


def test_classify_below_floor():
    assert classify_outcome(make_scored(50, 50, "a"), None) == OUTCOME_BELOW_FLOOR


def test_classify_deferred_takes_precedence_over_unmet_hard():
    # An eligible posting resurfaces regardless of unmet flags, so the label must
    # stay DEFERRED to match the dedup "leave unmarked" behavior.
    s = make_scored(70, 62, "a")
    s.unmet_hard_requirements = ["needs a PhD"]
    assert classify_outcome(s, None) == OUTCOME_DEFERRED


def test_classify_unmet_hard_when_below_floor():
    s = make_scored(40, 30, "a")
    s.unmet_hard_requirements = ["leads a PM team"]
    assert classify_outcome(s, None) == OUTCOME_UNMET_HARD


# --- Configurable dials (config/search.yaml -> jobradar.config) ---------------
# The defaults above must keep behaving exactly as before; these cover what
# changes when the user retunes them.


def test_max_best_defaults_to_one_headline():
    scored = [make_scored(95, 95, "a"), make_scored(92, 92, "b")]
    selected = select_matches(scored)
    assert [s.tier for s in selected] == ["best", "okay"]


def test_max_best_above_one_promotes_several():
    scored = [make_scored(95, 95, "a"), make_scored(92, 92, "b"), make_scored(70, 62, "c")]
    selected = select_matches(scored, max_best=2)
    assert [(s.scored.posting.id, s.tier) for s in selected] == [
        ("a", "best"),
        ("b", "best"),
        ("c", "okay"),
    ]


def test_best_slots_still_require_the_best_threshold():
    # A raised max_best widens the headline only for postings that earn it; an
    # unused best slot is not handed down as an extra okay one.
    scored = [make_scored(95, 95, "a"), make_scored(70, 62, "b")]
    selected = select_matches(scored, max_best=3)
    assert [(s.scored.posting.id, s.tier) for s in selected] == [("a", "best"), ("b", "okay")]


def test_max_best_zero_surfaces_everything_as_okay():
    selected = select_matches([make_scored(95, 95, "a")], max_best=0)
    assert [s.tier for s in selected] == ["okay"]


def test_max_okay_is_respected_alongside_max_best():
    scored = [make_scored(95, 95, "a")] + [make_scored(70, 62, f"o{i}") for i in range(5)]
    selected = select_matches(scored, max_best=1, max_okay=1)
    assert [s.tier for s in selected] == ["best", "okay"]


def test_min_interest_defaults_to_off():
    # The RAV-quota case: interest 10 still surfaces, because skill alone gates.
    assert len(select_matches([make_scored(95, 10, "a")])) == 1


def test_min_interest_gates_when_raised():
    scored = [make_scored(95, 40, "lukewarm"), make_scored(95, 80, "keen")]
    selected = select_matches(scored, min_interest=60)
    assert [s.scored.posting.id for s in selected] == ["keen"]


def test_is_eligible_honours_a_raised_interest_floor():
    assert is_eligible(make_scored(95, 40, "a")) is True
    assert is_eligible(make_scored(95, 40, "a"), min_interest=60) is False


def test_classify_outcome_uses_the_configured_floors():
    # A posting below a RAISED floor is gone (below-floor), not deferred — the
    # run log has to say which, since the two have opposite dedup behaviour.
    s = make_scored(65, 65, "a")
    assert classify_outcome(s, None) == OUTCOME_DEFERRED
    assert classify_outcome(s, None, min_skill=70) == OUTCOME_BELOW_FLOOR


def test_select_with_settings_matches_explicit_arguments():
    from jobradar.config import Output, Thresholds
    from jobradar.search.ranking import select_with_settings

    scored = [make_scored(95, 95, "a"), make_scored(92, 92, "b"), make_scored(70, 62, "c")]
    thresholds = Thresholds(min_skill=65, min_interest=0, best_threshold=85)
    output = Output(max_best=2, max_okay=1)
    assert select_with_settings(scored, thresholds, output) == select_matches(
        scored, best_threshold=85, min_skill=65, max_okay=1, max_best=2, min_interest=0
    )
