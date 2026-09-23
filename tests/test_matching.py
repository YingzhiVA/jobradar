import collections
import logging
import threading
import time

from jobradar.matching import (
    _CAP_MANY_UNMET,
    _looks_optional,
    _Requirement,
    build_profile_block,
    compute_skill_score,
    effective_category,
    effective_strength,
    score_postings,
    unmet_gates,
)
from jobradar.models import Posting, ScoredPosting
from jobradar.search.ranking import DEFAULT_MIN_SKILL, is_eligible


def req(quote, category, strength="must_have", verdict="unmet"):
    return _Requirement(
        quote=quote, category=category, strength=strength, verdict=verdict
    )


def met(quote, category, strength="must_have"):
    return req(quote, category, strength, "met")


def _scored(skill):
    """Minimal ScoredPosting for asserting the cap against the real floor —
    the cap's whole point is where it lands relative to is_eligible().
    """
    posting = Posting(
        id="t", source="test", url="https://example.invalid/j",
        title="t", company="c", description="d",
    )
    return ScoredPosting(
        posting=posting, skill_score=skill, interest_score=50,
        best_cv="pm", brief_reason="r",
    )


def test_profile_block_has_identity_and_cvs():
    block = build_profile_block({"pm": "PM CV content"}, "My identity statement")
    assert "## Career identity, motivation, and aspirations" in block
    assert "My identity statement" in block
    assert "## CV: pm" in block
    assert "PM CV content" in block
    # Identity comes first; the CVs elaborate on it.
    assert block.index("CV: pm") > block.index("My identity statement")


def test_missing_identity_falls_back_to_placeholder():
    block = build_profile_block({"pm": "content"}, "   ")
    assert "(not provided)" in block


def test_profile_block_with_stories():
    block = build_profile_block(
        {"pm": "PM CV content"},
        "identity",
        {"incident-turnaround": "S: outage. T: coordinate. A: led. R: fixed."},
    )
    assert "## Interview stories (STAR)" in block
    assert "### Story: incident-turnaround" in block
    assert "S: outage." in block
    # Stories elaborate on the CVs, so they come after them
    assert block.index("Story: incident-turnaround") > block.index("CV: pm")


def test_profile_block_without_stories_omits_section():
    block = build_profile_block({"pm": "content"}, "identity", {})
    assert "Interview stories" not in block


# --- requirement strength ------------------------------------------------


def test_stated_requirement_defaults_to_must_have():
    """The corpus rule: explicit hard markers barely exist (2 of 32 postings
    said "must have"), so an unsoftened bullet has to count as required or
    almost nothing would.
    """
    assert effective_strength(req("5 years of product management", "years_of_experience")) == "must_have"


def test_softened_wording_downgrades_to_preferred():
    for quote in [
        "Experience with R is a plus",
        "Clinical trials experience is a strong plus",
        "Databricks knowledge preferred",
        "Ideally you have worked in insurance",
        "Nice to have: Palantir Foundry",
        "Erfahrung mit Azure von Vorteil",
        "Weiterbildung in Analytics wünschenswert",
        "La connaissance du francais est un atout",
    ]:
        assert _looks_optional(quote), quote
        assert effective_strength(req(quote, "technology")) == "preferred"


def test_plain_requirement_is_not_softened():
    for quote in [
        "Minimum 2 years of relevant experience in banking",
        "Fluent German",
        "MSc in computer science",
    ]:
        assert not _looks_optional(quote), quote


def test_effective_strength_never_promotes():
    """The model can see section headings and blanket "you don't need to tick
    every box" language that a per-line regex cannot, so its downgrade stands.
    """
    plain = req("5 years in banking", "years_of_experience", strength="preferred")
    assert effective_strength(plain) == "preferred"


# --- gates ---------------------------------------------------------------


def test_unmet_gate_categories_are_the_gates():
    gates = unmet_gates([
        req("Must hold an EU work permit", "work_eligibility"),
        req("Active CFA charterholder", "licence_or_certification"),
        req("Manage a team of 8 engineers", "seniority_mismatch"),
        req("Fluent German required", "language_requirement"),
    ])
    assert gates == [
        "Must hold an EU work permit",
        "Active CFA charterholder",
        "Manage a team of 8 engineers",
        "Fluent German required",
    ]


def test_non_gate_categories_do_not_gate():
    gates = unmet_gates([
        req("MSc or PhD in engineering", "degree"),
        req("Minimum 2 years in banking", "years_of_experience"),
        req("Experience in asset management", "domain_or_industry"),
        req("Hands-on Databricks experience", "technology"),
        req("Willingness to travel abroad", "other"),
    ])
    assert gates == []


def test_a_met_gate_requirement_does_not_gate():
    assert unmet_gates([met("Eligible to work in Switzerland", "work_eligibility")]) == []


def test_a_preferred_gate_requirement_does_not_gate():
    """A language a posting calls optional is not a gate, however it is tagged."""
    assert unmet_gates([
        req("German is a plus", "language_requirement", strength="preferred")
    ]) == []


def test_softened_gate_wording_does_not_gate():
    """The code-side check applies to gates too: "German is a plus" cannot
    knock a posting down however the model tagged its strength.
    """
    assert unmet_gates([req("German is a plus", "language_requirement")]) == []


def test_encouragement_language_does_not_excuse_a_gate():
    """Blanket "don't worry if you don't meet all the criteria" downgrades
    qualifications, never a work-eligibility gate — the model is told so, and a
    gate that arrives unsoftened still fires here.
    """
    score, gates = compute_skill_score([
        met("Product management experience", "years_of_experience"),
        req("You must hold a Swiss or EU work permit", "work_eligibility"),
    ])
    assert gates == ["You must hold a Swiss or EU work permit"]
    assert score == DEFAULT_MIN_SKILL


# --- seniority: only line management gates -------------------------------
#
# Measured over the first two checklist runs, 13 of 25 decided seniority tags
# were wrong, 8 of them cases the prompt already excludes by name. The model
# keys on "lead"; these pin the code-side check that enforces the definition.
# Each quote below is verbatim from a real run.


def test_line_management_demands_still_gate():
    for quote in [
        "5 years of experience in people management, with technical leadership",
        "hire, mentor, and manage the team",
        "experience leading and managing teams",
        "Lead a dedicated PPMO team",
        "Demonstrated people management skills and experience in staff performance management",
        "1+ years of leadership experience managing, scaling, and developing multidisciplinary technical teams",
        "Experience leading and developing substantial global teams through periods of change",
        "operative und personelle Führung des P&C-Privatkunden-Teams",
        "Du führst, entwickelst und coachst ein engagiertes Team",
    ]:
        assert effective_category(req(quote, "seniority_mismatch")) == "seniority_mismatch", quote
        assert unmet_gates([req(quote, "seniority_mismatch")]) == [quote], quote


def test_leading_work_is_not_managing_people():
    for quote in [
        "leading large-scale learning transformations",
        "substantial leadership of large, complex, cross-functional programs",
        "leading cross-team product alignment and strategic coherence",
    ]:
        assert unmet_gates([req(quote, "seniority_mismatch")]) == [], quote


def test_leading_without_authority_is_not_managing_people():
    for quote in [
        "Capabilities to lead a cross-functional team",
        "lead matrixed, global teams",
        "Experience working and leading in a matrix environment",
        "Lead cross-functional CtQ reviews, aligning R&D, Manufacturing and Service",
    ]:
        assert unmet_gates([req(quote, "seniority_mismatch")]) == [], quote


def test_the_reporting_line_upward_is_not_managing_people():
    """Who the candidate would report to, not who would report to them."""
    for quote in [
        "Reporting to the Portfolio Lead Gastroenterology",
        "Reporting to the Director, Clinical Data Transparency",
    ]:
        assert unmet_gates([req(quote, "seniority_mismatch")]) == [], quote


def test_a_level_mismatch_is_not_a_management_demand():
    """The category's name invites this reading; the interest score, not this
    gate, is what keeps a trainee role away from a senior candidate.
    """
    for quote in ["Trainee", "in your penultimate year of study"]:
        assert unmet_gates([req(quote, "seniority_mismatch")]) == [], quote


def test_a_veto_wins_over_a_marker():
    """Names a team lead, but is satisfiable by leading a project — so it is not
    a demand for line management.
    """
    quote = ("Demonstrated leadership experience, for example by leading projects, "
             "technical workstreams or serving as a (deputy) team lead")
    assert unmet_gates([req(quote, "seniority_mismatch")]) == []


def test_a_rejected_seniority_tag_neither_gates_nor_weighs():
    """Treated as `other`: a mis-tag must not cap the posting, and must not pull
    its fit down either.
    """
    alone, _ = compute_skill_score([met("5 years of product management", "years_of_experience")])
    with_mistag, gates = compute_skill_score([
        met("5 years of product management", "years_of_experience"),
        req("Capabilities to lead a cross-functional team", "seniority_mismatch"),
    ])
    assert gates == []
    assert with_mistag == alone == 100


def test_the_check_touches_only_seniority():
    quote = "lead matrixed, global teams"
    assert effective_category(req(quote, "technology")) == "technology"
    assert effective_category(req(quote, "work_eligibility")) == "work_eligibility"


# --- the computed score --------------------------------------------------


def test_full_checklist_met_scores_high():
    score, gates = compute_skill_score([
        met("5 years of product management", "years_of_experience"),
        met("Python and SQL", "technology"),
        met("MSc in a technical field", "degree"),
        met("Fintech experience", "domain_or_industry", strength="preferred"),
    ])
    assert gates == []
    assert score == 100


def test_nothing_met_scores_at_the_bottom():
    score, _ = compute_skill_score([
        req("10 years in pharmaceutical manufacturing", "domain_or_industry"),
        req("SAP S/4HANA implementation", "technology"),
    ])
    assert score == 0


def test_partial_credit_lands_between():
    score, _ = compute_skill_score([
        _Requirement(
            quote="PySpark at enterprise scale", category="technology",
            strength="must_have", verdict="partial",
        ),
    ])
    assert score == 50


def test_unmet_preferred_costs_less_than_unmet_must_have():
    """The whole point of the strength split: a missing nice-to-have should
    shade a score, a missing requirement should move it.
    """
    as_preferred, _ = compute_skill_score([
        met("Product management experience", "years_of_experience"),
        req("Insurance domain experience", "domain_or_industry", strength="preferred"),
    ])
    as_must_have, _ = compute_skill_score([
        met("Product management experience", "years_of_experience"),
        req("Insurance domain experience", "domain_or_industry"),
    ])
    assert as_preferred == 80
    assert as_must_have == 50
    assert as_preferred > as_must_have


def test_degree_gap_barely_moves_the_score():
    """The original bug, in its new form: a degree requirement the candidate
    plainly meets was capping the score to 50 before any code could intervene.
    Degree now carries a quarter weight, so even a genuine degree gap tilts the
    score rather than deciding it.
    """
    score, gates = compute_skill_score([
        met("5 years of product management", "years_of_experience"),
        req("MSc in Computer Science, Mathematics, or similar", "degree"),
    ])
    assert gates == []
    assert score == 80


def test_other_category_is_ignored_entirely():
    """Travel, commute and on-call are the candidate's call, not the scorer's."""
    score, _ = compute_skill_score([
        met("5 years of product management", "years_of_experience"),
        req("Willingness to travel 30% of the time", "other"),
    ])
    assert score == 100


def test_seniority_gap_still_caps_end_to_end():
    """The signal that must survive: a people-leadership role the candidate has
    no management experience for.
    """
    score, gates = compute_skill_score([
        met("Data product ownership", "domain_or_industry"),
        met("Stakeholder management", "technology"),
        req("Manage and coach 1-3 direct reports", "seniority_mismatch"),
    ])
    assert gates == ["Manage and coach 1-3 direct reports"]
    assert score == DEFAULT_MIN_SKILL
    assert is_eligible(_scored(score))


def test_two_gates_stay_eligible():
    """A strong fit with two gates lands exactly ON the floor: it still
    surfaces, ranked last, and the user judges the gate themselves.
    """
    score, gates = compute_skill_score([
        met("Python", "technology"),
        met("5 years of product management", "years_of_experience"),
        req("EU work permit", "work_eligibility"),
        req("Active CFA charter", "licence_or_certification"),
    ])
    assert len(gates) == 2
    assert score == DEFAULT_MIN_SKILL
    assert is_eligible(_scored(score))


def test_three_gates_drop_below_the_floor():
    score, gates = compute_skill_score([
        met("Python", "technology"),
        met("5 years of product management", "years_of_experience"),
        req("EU work permit", "work_eligibility"),
        req("Active CFA charter", "licence_or_certification"),
        req("Manages a team of 8", "seniority_mismatch"),
    ])
    assert len(gates) == 3
    assert score == _CAP_MANY_UNMET
    assert not is_eligible(_scored(score))


def test_a_met_gate_does_not_inflate_the_score():
    """Gates are eligibility facts, not fit. Being allowed to work here says
    nothing about how well the candidate would do the job, so it must not pull
    a weak fit upwards any more than it drags a strong one down.
    """
    without_gate, _ = compute_skill_score([
        req("10 years in pharmaceutical manufacturing", "domain_or_industry"),
    ])
    with_gate, gates = compute_skill_score([
        req("10 years in pharmaceutical manufacturing", "domain_or_industry"),
        met("Eligible to work in Switzerland", "work_eligibility"),
    ])
    assert gates == []
    assert with_gate == without_gate == 0


def test_a_gate_never_raises_a_low_score():
    """The cap is a ceiling, not a floor: a poor fit that also fails a gate
    must not be lifted to the eligibility floor by it.
    """
    score, gates = compute_skill_score([
        req("10 years in pharma", "domain_or_industry"),
        req("SAP S/4HANA", "technology"),
        req("EU work permit", "work_eligibility"),
    ])
    assert gates == ["EU work permit"]
    assert score == 0


def test_empty_checklist_scores_at_the_floor_rather_than_dropping():
    """search/main.py marks an unscored posting seen, so it never comes back.
    An unscoreable posting surfaces last for the user to judge instead.
    """
    score, gates = compute_skill_score([])
    assert score == DEFAULT_MIN_SKILL
    assert gates == []


def test_checklist_of_only_ignored_categories_scores_at_the_floor():
    score, _ = compute_skill_score([req("Willingness to travel", "other")])
    assert score == DEFAULT_MIN_SKILL


def test_checklist_of_only_gates_scores_at_the_floor():
    """Nothing gradable was stated, so there is no fit to measure — but a met
    gate is not a reason to hide the posting either.
    """
    score, gates = compute_skill_score([met("Eligible to work in Switzerland", "work_eligibility")])
    assert gates == []
    assert score == DEFAULT_MIN_SKILL


def test_no_preferred_requirements_is_not_a_penalty():
    """A posting that lists no nice-to-haves must not lose the 20 points the
    preferred half carries.
    """
    score, _ = compute_skill_score([met("5 years of product management", "years_of_experience")])
    assert score == 100


def test_only_preferred_requirements_are_judged_on_their_own():
    score, _ = compute_skill_score([
        met("Fintech experience", "domain_or_industry", strength="preferred"),
    ])
    assert score == 100


def test_profile_block_without_evidence():
    block = build_profile_block({"pm": "PM CV content"}, "identity")
    assert "## Published technical write-ups" not in block


def test_profile_block_with_evidence():
    block = build_profile_block(
        {"pm": "PM CV content"},
        "identity",
        {"conflict": "Story body"},
        {"holding-still": "# Teaching an Agent to Hold Still\n\n- Source: https://x.invalid/n/"},
    )
    assert "## Published technical write-ups" in block
    assert "### Write-up: holding-still" in block
    assert "https://x.invalid/n/" in block
    # The generalization clause is the one instruction that must survive edits:
    # without it a cover letter can undo what a note deliberately withheld.
    assert "ALREADY GENERALIZED" in block
    # Stories and write-ups are distinct sections, not merged.
    assert "## Interview stories (STAR)" in block


def test_load_evidence_reads_the_evidence_dir(tmp_path):
    from jobradar.matching import load_evidence

    assert load_evidence(tmp_path) == {}
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "README.md").write_text("not evidence", encoding="utf-8")
    (evidence / "one-axis.md").write_text("# One Axis", encoding="utf-8")
    assert load_evidence(tmp_path) == {"one-axis": "# One Axis"}


# --- score_postings concurrency ------------------------------------------
#
# Scoring fans out across a thread pool (see matching._SCORING_WORKERS). These
# cover the three things the fan-out can silently break: result order, the
# shared usage accumulator, and the cache-warming first call.

class _FakeUsage:
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 10
    input_tokens = 5


class _FakeParsed:
    def __init__(self, title):
        self.interest_score = 50
        self.best_cv = "pm"
        self.brief_reason = title
        # One met requirement, so compute_skill_score has something to grade
        # and these tests exercise the real scoring path rather than the
        # empty-checklist fallback.
        self.requirements = [
            _Requirement(
                quote="5 years of product management",
                category="years_of_experience",
                strength="must_have",
                verdict="met",
            )
        ]


class _FakeResponse:
    def __init__(self, title):
        self.usage = _FakeUsage()
        self.parsed_output = _FakeParsed(title)


class _FakeMessages:
    """Records concurrency and can fail/stall specific postings by title.

    `fail_once_titles` fails only a posting's FIRST attempt, which is how the
    retry in score_posting is exercised: the real failures it exists for (an API
    500, a response that runs away and never closes its JSON) are stochastic,
    so the second draw lands.
    """

    def __init__(self, delays=None, fail_titles=(), fail_once_titles=()):
        self.delays = delays or {}
        self.fail_titles = set(fail_titles)
        self.fail_once_titles = set(fail_once_titles)
        self.attempts = collections.Counter()
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.call_titles = []
        self.spans = []  # (title, started, finished) in start order

    def parse(self, **kwargs):
        title = kwargs["messages"][0]["content"].split("Title: ")[1].split("\n")[0]
        started = time.monotonic()
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.call_titles.append(title)
            slot = len(self.spans)
            self.spans.append([title, started, None])
        try:
            time.sleep(self.delays.get(title, 0.01))
            with self.lock:
                self.attempts[title] += 1
                first_try = self.attempts[title] == 1
            if title in self.fail_titles:
                raise RuntimeError("boom")
            if title in self.fail_once_titles and first_try:
                raise RuntimeError("transient")
            return _FakeResponse(title)
        finally:
            with self.lock:
                self.spans[slot][2] = time.monotonic()
                self.in_flight -= 1


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages


def _postings(n):
    return [
        Posting(
            id=str(i), source="test", url=f"https://example.invalid/{i}",
            title=f"job-{i}", company="c", description="d",
        )
        for i in range(n)
    ]


def test_score_postings_preserves_input_order():
    """Reversed delays: if results came back in completion order the list
    would be inverted. select_matches sorts stably, so input order is what
    breaks ties for the surfaced slots.
    """
    postings = _postings(12)
    delays = {f"job-{i}": (12 - i) * 0.01 for i in range(12)}
    messages = _FakeMessages(delays=delays)
    scored = score_postings(postings, "profile", _FakeClient(messages), workers=6)
    assert [s.posting.title for s in scored] == [f"job-{i}" for i in range(12)]


def test_score_postings_actually_runs_concurrently():
    messages = _FakeMessages(delays={f"job-{i}": 0.05 for i in range(10)})
    score_postings(_postings(10), "profile", _FakeClient(messages), workers=5)
    assert messages.max_in_flight > 1


def test_score_postings_warms_the_cache_before_fanning_out():
    """The first call must complete before any other starts, otherwise every
    worker misses the profile-block cache at once and pays to write its own
    copy — turning one cache-creation into `workers` of them.
    """
    messages = _FakeMessages(delays={f"job-{i}": 0.05 for i in range(8)})
    score_postings(_postings(8), "profile", _FakeClient(messages), workers=4)
    first, rest = messages.spans[0], messages.spans[1:]
    assert first[0] == "job-0"
    assert rest, "expected the remaining postings to be scored too"
    assert first[2] <= min(span[1] for span in rest), (
        "the warming call must finish before the pool starts"
    )


def test_score_postings_counts_every_call_in_the_usage_accumulator(caplog):
    """End-to-end check that the shared accumulator survives the fan-out.
    The race itself is pinned deterministically in test_usage.py.
    """
    messages = _FakeMessages(delays={f"job-{i}": 0 for i in range(60)})
    with caplog.at_level(logging.INFO, logger="jobradar.matching"):
        score_postings(_postings(60), "profile", _FakeClient(messages), workers=8)
    assert "scoring cache: 60 calls" in caplog.text
    assert "600 cache-read" in caplog.text  # 60 x 10, nothing lost to a race


def test_score_postings_drops_failures_and_keeps_the_rest_in_order():
    messages = _FakeMessages(fail_titles={"job-2", "job-5"})
    scored = score_postings(_postings(8), "profile", _FakeClient(messages), workers=4)
    assert [s.posting.title for s in scored] == [
        "job-0", "job-1", "job-3", "job-4", "job-6", "job-7"
    ]


def test_score_postings_survives_a_failing_first_posting():
    """The cache-warming call is outside the pool — a failure there must not
    abort the run or drop the remaining postings.
    """
    messages = _FakeMessages(fail_titles={"job-0"})
    scored = score_postings(_postings(4), "profile", _FakeClient(messages), workers=2)
    assert [s.posting.title for s in scored] == ["job-1", "job-2", "job-3"]


def test_score_postings_empty_input_makes_no_calls():
    messages = _FakeMessages()
    assert score_postings([], "profile", _FakeClient(messages)) == []
    assert messages.call_titles == []


def test_score_postings_single_worker_stays_sequential():
    messages = _FakeMessages(delays={f"job-{i}": 0.02 for i in range(5)})
    scored = score_postings(_postings(5), "profile", _FakeClient(messages), workers=1)
    assert messages.max_in_flight == 1
    assert [s.posting.title for s in scored] == [f"job-{i}" for i in range(5)]


def test_score_postings_warns_once_with_the_total_dropped(caplog):
    messages = _FakeMessages(fail_titles={"job-1", "job-2"})
    with caplog.at_level(logging.WARNING, logger="jobradar.matching"):
        score_postings(_postings(6), "profile", _FakeClient(messages), workers=3)
    assert "Scoring dropped 2/6 postings" in caplog.text


def test_score_postings_silent_when_nothing_dropped(caplog):
    messages = _FakeMessages()
    with caplog.at_level(logging.WARNING, logger="jobradar.matching"):
        score_postings(_postings(4), "profile", _FakeClient(messages), workers=2)
    assert "Scoring dropped" not in caplog.text


def test_a_transient_failure_is_retried_and_the_posting_survives():
    """An API error or a runaway response must not cost a posting its place in
    the run — search/main.py leaves an unscored posting to come back, but a
    retry means it usually does not have to.
    """
    messages = _FakeMessages(fail_once_titles={"job-1"})
    scored = score_postings(_postings(3), "profile", _FakeClient(messages), workers=1)
    assert [s.posting.title for s in scored] == ["job-0", "job-1", "job-2"]
    assert messages.attempts["job-1"] == 2


def test_a_posting_failing_every_attempt_is_still_dropped():
    messages = _FakeMessages(fail_titles={"job-1"})
    scored = score_postings(_postings(3), "profile", _FakeClient(messages), workers=1)
    assert [s.posting.title for s in scored] == ["job-0", "job-2"]
    assert messages.attempts["job-1"] == 2  # tried twice, then given up on


def test_a_successful_posting_is_scored_once():
    messages = _FakeMessages()
    score_postings(_postings(3), "profile", _FakeClient(messages), workers=1)
    assert set(messages.attempts.values()) == {1}


def test_score_posting_records_the_models_own_call_alongside_the_correction():
    """runs.jsonl must carry what the model said, not only what the checks made
    of it — otherwise a wrongly dismissed management demand would read as
    `other` and the line-management check's misses could never be counted.
    """
    class _Parsed:
        interest_score = 50
        best_cv = "pm"
        brief_reason = "r"
        requirements = [
            _Requirement(quote="Capabilities to lead a cross-functional team",
                         category="seniority_mismatch", strength="must_have", verdict="unmet"),
            _Requirement(quote="Databricks is a plus",
                         category="technology", strength="must_have", verdict="unmet"),
        ]

    class _Resp:
        usage = _FakeUsage()
        parsed_output = _Parsed()

    class _Msgs:
        def parse(self, **kwargs):
            return _Resp()

    posting = _postings(1)[0]
    scored = score_postings([posting], "profile", _FakeClient(_Msgs()), workers=1)[0]
    lead, plus = scored.requirements
    assert (lead.category, lead.model_category) == ("other", "seniority_mismatch")
    assert (plus.strength, plus.model_strength) == ("preferred", "must_have")
    assert scored.unmet_hard_requirements == []
