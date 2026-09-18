from jobradar.search.dedup import SeenStore
from jobradar.models import Posting


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
