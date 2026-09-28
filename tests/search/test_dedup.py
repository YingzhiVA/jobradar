from jobradar.search.dedup import SeenStore
from jobradar.models import Posting
from jobradar.search.ranking import NEAR_FLOOR_ATTEMPTS


def make_posting(pid: str) -> Posting:
    return Posting(
        id=pid,
        source="test",
        url=f"https://example.com/{pid}",
        title=f"Role {pid}",
        company="Co",
        description="desc",
    )


def test_unseen_then_seen_roundtrip(tmp_path):
    path = tmp_path / "seen.json"
    a, b = make_posting("a"), make_posting("b")

    with SeenStore(path) as store:
        assert store.filter_unseen([a, b]) == [a, b]
        store.mark_seen([a])

    # New store instance reads the persisted file.
    with SeenStore(path) as store:
        assert store.filter_unseen([a, b]) == [b]


def test_mark_seen_persists_to_disk(tmp_path):
    path = tmp_path / "seen.json"
    with SeenStore(path) as store:
        store.mark_seen([make_posting("a")], outcome_by_id={"a": "best"})
    assert path.exists()
    import json

    data = json.loads(path.read_text())
    assert data["a"]["outcome"] == "best"
    assert data["a"]["company"] == "Co"


def test_mark_seen_preserves_first_outcome(tmp_path):
    path = tmp_path / "seen.json"
    p = make_posting("a")
    with SeenStore(path) as store:
        store.mark_seen([p], outcome_by_id={"a": "best"})
        # Re-marking the same id must NOT overwrite the original outcome.
        store.mark_seen([p], outcome_by_id={"a": "considered"})
    import json

    assert json.loads(path.read_text())["a"]["outcome"] == "best"


def test_missing_file_starts_empty(tmp_path):
    store = SeenStore(tmp_path / "nope.json")
    assert store.filter_unseen([make_posting("a")]) == [make_posting("a")]


def test_corrupt_file_starts_fresh(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{ not valid json")
    store = SeenStore(path)  # should not raise
    assert store.filter_unseen([make_posting("a")]) == [make_posting("a")]


def test_partial_mark_seen_leaves_remainder_visible_next_run(tmp_path):
    # SeenStore marks only the postings it's given; postings not passed to mark_seen
    # remain unseen and re-appear on the next run. main.py uses this to leave
    # eligible-but-capped postings unmarked so they resurface on future runs.
    path = tmp_path / "seen.json"
    all_postings = [make_posting(str(i)) for i in range(10)]
    to_mark = all_postings[:3]
    outcome_by_id = {"0": "okay", "1": "okay", "2": "okay"}

    with SeenStore(path) as store:
        store.mark_seen(to_mark, outcome_by_id=outcome_by_id)

    with SeenStore(path) as store:
        unseen = store.filter_unseen(all_postings)
        assert len(unseen) == 7
        assert {p.id for p in unseen} == {"3", "4", "5", "6", "7", "8", "9"}


# --- the near-floor retry budget -----------------------------------------
#
# Scoring is not repeatable enough to write a posting off on one draw: a
# near-miss gets NEAR_FLOOR_ATTEMPTS chances across runs, then settles. The two
# failure modes are a posting lost on an unlucky draw, and one re-scored daily
# for the weeks it stays open.


def _p(pid="a"):
    return Posting(
        id=pid, source="test", url=f"https://example.invalid/{pid}",
        title="t", company="c", description="d",
    )


def test_a_near_floor_posting_comes_back(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    store.mark_seen([_p()], retryable_ids={"a"})
    assert store.filter_unseen([_p()]) == [_p()]


def test_an_ordinary_posting_does_not_come_back(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    store.mark_seen([_p()])
    assert store.filter_unseen([_p()]) == []


def test_the_budget_runs_out_after_the_configured_attempts(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    for _ in range(NEAR_FLOOR_ATTEMPTS):
        assert store.filter_unseen([_p()]) == [_p()], "should still be in play"
        store.mark_seen([_p()], retryable_ids={"a"})
    assert store.filter_unseen([_p()]) == [], "budget spent, posting settled"


def test_a_worse_score_on_a_later_run_still_spends_only_one_attempt(tmp_path):
    """Scoring further below the floor settles nothing in the direction that
    matters. The question is whether the posting ever clears, so a bad second
    draw costs one attempt, not the whole budget.
    """
    store = SeenStore(tmp_path / "seen.json")
    store.mark_seen([_p()], retryable_ids={"a"})
    store.mark_seen([_p()], outcome_by_id={"a": "below-floor"}, retryable_ids=set())
    assert store.filter_unseen([_p()]) == [_p()], "one attempt should remain"


def test_a_posting_that_never_entered_the_band_gets_no_budget_later(tmp_path):
    """Eligibility for extra draws is decided on first sight."""
    store = SeenStore(tmp_path / "seen.json")
    store.mark_seen([_p()])
    store.mark_seen([_p()], retryable_ids={"a"})
    assert store.filter_unseen([_p()]) == []


def test_the_budget_survives_a_reload(tmp_path):
    path = tmp_path / "seen.json"
    SeenStore(path).mark_seen([_p()], retryable_ids={"a"})
    assert SeenStore(path).filter_unseen([_p()]) == [_p()]


def test_a_retried_posting_keeps_its_first_seen_record(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    store.mark_seen([_p()], outcome_by_id={"a": "below-floor"}, retryable_ids={"a"})
    first = dict(store._seen["a"])
    store.mark_seen([_p()], retryable_ids={"a"})
    assert store._seen["a"]["first_seen_at"] == first["first_seen_at"]
    assert store._seen["a"]["outcome"] == "below-floor"


# --- settled URLs: what connectors may skip the detail call for ------------


def test_settled_urls_lists_decided_postings(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    store.mark_seen([_p("a"), _p("b")])
    assert store.settled_urls() == {"https://example.invalid/a", "https://example.invalid/b"}


def test_a_posting_with_retries_left_is_not_settled(tmp_path):
    """It comes back to be scored again, and scoring needs its description."""
    store = SeenStore(tmp_path / "seen.json")
    store.mark_seen([_p("a")])
    store.mark_seen([_p("b")], retryable_ids={"b"})
    assert store.settled_urls() == {"https://example.invalid/a"}


def test_a_spent_retry_budget_settles_the_posting(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    for _ in range(NEAR_FLOOR_ATTEMPTS):
        store.mark_seen([_p("b")], retryable_ids={"b"})
    assert store.settled_urls() == {"https://example.invalid/b"}
