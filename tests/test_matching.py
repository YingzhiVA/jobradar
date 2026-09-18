import logging
import threading
import time

from jobradar.matching import (
    _cap_skill_score,
    _filter_gaps,
    _Gap,
    build_profile_block,
    score_postings,
)
from jobradar.models import Posting, ScoredPosting
from jobradar.search.ranking import DEFAULT_MIN_SKILL, is_eligible


def gap(requirement, category):
    return _Gap(requirement=requirement, category=category)


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


def test_filter_gaps_keeps_the_three_knockout_categories():
    gaps = _filter_gaps(
        [
            gap("Must hold an EU work permit", "work_eligibility"),
            gap("Active CFA charterholder", "licence_or_certification"),
            gap("Role manages a team of 8 engineers", "seniority_mismatch"),
        ]
    )
    assert gaps == [
        "Must hold an EU work permit",
        "Active CFA charterholder",
        "Role manages a team of 8 engineers",
    ]


def test_filter_gaps_drops_the_non_knockout_categories():
    gaps = _filter_gaps(
        [
            gap("MSc or PhD in engineering", "degree"),
            gap("Minimum 2 years in banking", "years_of_experience"),
            gap("Experience in asset management", "domain_or_industry"),
            gap("Hands-on Databricks experience", "technology"),
            gap("Willingness to travel abroad", "other"),
        ]
    )
    assert gaps == []


def test_filter_gaps_keeps_only_the_knockout_entries_in_a_mixed_list():
    gaps = _filter_gaps(
        [
            gap("Hands-on Microsoft Fabric experience", "technology"),
            gap("Must hold a US security clearance", "work_eligibility"),
            gap("MSc in computer science", "degree"),
        ]
    )
    assert gaps == ["Must hold a US security clearance"]


def test_filter_gaps_categorises_by_kind_not_by_severity():
    """A technology or experience requirement stays non-knockout even when the
    posting calls it essential — that judgement is the model's to report and
    ours to weigh.
    """
    gaps = _filter_gaps(
        [
            gap("5 years as a SOC analyst (must-have)", "years_of_experience"),
            gap("MITRE ATT&CK expertise (must-have)", "technology"),
        ]
    )
    assert gaps == []


def test_cap_skill_score_uncapped_when_no_gaps_survive():
    assert _cap_skill_score(78, []) == 78


def test_cap_skill_score_one_gap_stays_eligible():
    """One knockout costs the posting its ranking, not its place in the run."""
    capped = _cap_skill_score(78, ["Must hold an EU work permit"])
    assert capped == DEFAULT_MIN_SKILL
    assert is_eligible(_scored(capped))


def test_cap_skill_score_two_gaps_stays_eligible():
    capped = _cap_skill_score(78, ["EU work permit", "Active CFA charter"])
    assert capped == DEFAULT_MIN_SKILL
    assert is_eligible(_scored(capped))


def test_cap_skill_score_three_gaps_drops_below_floor():
    capped = _cap_skill_score(
        78, ["EU work permit", "Active CFA charter", "Manages a team of 8"]
    )
    assert capped < DEFAULT_MIN_SKILL
    assert not is_eligible(_scored(capped))


def test_cap_skill_score_never_raises_a_low_score():
    assert _cap_skill_score(22, ["Must hold an EU work permit"]) == 22
    assert _cap_skill_score(41, ["a", "b", "c", "d"]) == 41


def test_degree_gap_leaves_score_uncapped_end_to_end():
    """The original bug: a degree requirement the candidate plainly meets was
    capping the score to 50 before any code could intervene.
    """
    surviving = _filter_gaps([gap("MSc or PhD in engineering", "degree")])
    assert surviving == []
    assert _cap_skill_score(72, surviving) == 72


def test_seniority_gap_still_caps_end_to_end():
    """The signal that must survive: a people-leadership role the candidate has
    no management experience for.
    """
    surviving = _filter_gaps(
        [gap("Manages 1-3 direct reports", "seniority_mismatch")]
    )
    assert surviving == ["Manages 1-3 direct reports"]
    assert _cap_skill_score(72, surviving) == DEFAULT_MIN_SKILL


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
        self.skill_score = 60
        self.interest_score = 50
        self.best_cv = "pm"
        self.brief_reason = title
        self.unmet_hard_requirements = []


class _FakeResponse:
    def __init__(self, title):
        self.usage = _FakeUsage()
        self.parsed_output = _FakeParsed(title)


class _FakeMessages:
    """Records concurrency and can fail/stall specific postings by title."""

    def __init__(self, delays=None, fail_titles=()):
        self.delays = delays or {}
        self.fail_titles = set(fail_titles)
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
            if title in self.fail_titles:
                raise RuntimeError("boom")
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
