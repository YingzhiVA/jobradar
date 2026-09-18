from datetime import datetime, timedelta, timezone

from jobradar.discovery.discovery_ledger import (
    active_names,
    evaluated_names,
    load_ledger,
    record,
    reject_board,
    rejected_boards,
    save_ledger,
    should_skip_probe,
)

NOW = datetime(2026, 6, 17, tzinfo=timezone.utc)
TTL = 30


def test_load_missing_file_returns_empty(tmp_path):
    assert load_ledger(tmp_path / "nope.json") == {}


def test_save_and_load_roundtrip(tmp_path):
    ledger = {}
    record(ledger, "Acme Inc", "matched", NOW)
    path = tmp_path / "ledger.json"
    save_ledger(path, ledger)
    assert load_ledger(path) == ledger


def test_record_normalizes_key_and_keeps_display_name():
    ledger = {}
    record(ledger, "  Acme Inc  ", "dropped", NOW)
    assert "acme inc" in ledger
    assert ledger["acme inc"]["name"] == "  Acme Inc  "
    assert ledger["acme inc"]["outcome"] == "dropped"


def test_evaluated_names_returns_display_names():
    ledger = {}
    record(ledger, "Acme", "matched", NOW)
    record(ledger, "Beta", "dropped", NOW)
    assert sorted(evaluated_names(ledger)) == ["Acme", "Beta"]


def test_reject_board_records_and_rejected_boards_reads_it():
    ledger = {}
    reject_board(ledger, "Hamilton", "bamboohr", "hamilton")
    assert rejected_boards(ledger, "Hamilton") == {("bamboohr", "hamilton")}
    assert rejected_boards(ledger, "hamilton") == {("bamboohr", "hamilton")}  # case-insensitive
    assert rejected_boards(ledger, "Someone Else") == set()


def test_reject_board_creates_entry_when_company_is_new():
    ledger = {}
    reject_board(ledger, "Hamilton", "bamboohr", "hamilton")
    assert ledger["hamilton"]["name"] == "Hamilton"
    assert ledger["hamilton"]["rejected"] == [["bamboohr", "hamilton"]]


def test_reject_board_is_idempotent_and_accumulates_distinct_boards():
    ledger = {}
    reject_board(ledger, "Acme", "lever", "acme")
    reject_board(ledger, "Acme", "lever", "acme")  # dup — no-op
    reject_board(ledger, "Acme", "ashby", "acme")
    assert rejected_boards(ledger, "Acme") == {("lever", "acme"), ("ashby", "acme")}


def test_record_preserves_rejected_boards():
    # A rejection is a durable human judgment; a later verdict must not wipe it.
    ledger = {}
    reject_board(ledger, "Hamilton", "bamboohr", "hamilton")
    record(ledger, "Hamilton", "dropped", NOW, "v2")
    assert ledger["hamilton"]["outcome"] == "dropped"
    assert rejected_boards(ledger, "Hamilton") == {("bamboohr", "hamilton")}


def test_rejected_boards_survive_save_load_roundtrip(tmp_path):
    ledger = {}
    reject_board(ledger, "Hamilton", "bamboohr", "hamilton")
    record(ledger, "Hamilton", "dropped", NOW)
    path = tmp_path / "ledger.json"
    save_ledger(path, ledger)
    assert rejected_boards(load_ledger(path), "Hamilton") == {("bamboohr", "hamilton")}


def test_skip_probe_fresh_match_skips():
    ledger = {}
    record(ledger, "Acme", "matched", NOW - timedelta(days=5))
    assert should_skip_probe(ledger, "acme", NOW, TTL) is True


def test_skip_probe_stale_match_rechecks():
    # A match is not forever: a company can leave its ATS or empty its board.
    # 26 companies sat "matched" against join.com boards with no ads, and
    # because matches never expired, nothing could re-examine them.
    ledger = {}
    record(ledger, "Acme", "matched", NOW - timedelta(days=999))
    assert should_skip_probe(ledger, "acme", NOW, TTL) is False


def test_skip_probe_match_outlives_a_drop():
    # A live board rarely moves, so a match is trusted far longer than a drop:
    # at 40 days a drop is due for a re-check while a match still stands.
    ledger = {}
    record(ledger, "Matched", "matched", NOW - timedelta(days=40))
    record(ledger, "Dropped", "dropped", NOW - timedelta(days=40))
    assert should_skip_probe(ledger, "Matched", NOW, TTL) is True
    assert should_skip_probe(ledger, "Dropped", NOW, TTL) is False


def test_skip_probe_match_ttl_is_configurable():
    ledger = {}
    record(ledger, "Acme", "matched", NOW - timedelta(days=100))
    assert should_skip_probe(ledger, "Acme", NOW, TTL, "", 90) is False
    assert should_skip_probe(ledger, "Acme", NOW, TTL, "", 365) is True


def test_skip_probe_fresh_drop_skips():
    ledger = {}
    record(ledger, "Acme", "dropped", NOW - timedelta(days=5))
    assert should_skip_probe(ledger, "Acme", NOW, TTL) is True


def test_skip_probe_stale_drop_rechecks():
    ledger = {}
    record(ledger, "Acme", "dropped", NOW - timedelta(days=40))
    assert should_skip_probe(ledger, "Acme", NOW, TTL) is False


def test_skip_probe_unknown_company():
    assert should_skip_probe({}, "Unknown", NOW, TTL) is False


def test_skip_probe_malformed_entry_does_not_suppress():
    ledger = {"acme": {"name": "Acme", "outcome": "dropped"}}  # no checked_at
    assert should_skip_probe(ledger, "Acme", NOW, TTL) is False


def test_skip_probe_naive_timestamp_rechecks():
    # A legacy/hand-edited naive timestamp must not crash or wrongly suppress.
    ledger = {"acme": {"name": "Acme", "outcome": "dropped", "checked_at": "2026-06-10T00:00:00"}}
    assert should_skip_probe(ledger, "Acme", NOW, TTL) is False


def test_skip_probe_drop_from_older_probe_version_rechecks():
    ledger = {}
    record(ledger, "Acme", "dropped", NOW, probe_version="ats-v1")
    # Same version + fresh -> skip; different version -> re-probe even if fresh.
    assert should_skip_probe(ledger, "Acme", NOW, TTL, "ats-v1") is True
    assert should_skip_probe(ledger, "Acme", NOW, TTL, "ats-v2") is False


def test_skip_probe_matched_ignores_probe_version():
    ledger = {}
    record(ledger, "Acme", "matched", NOW, probe_version="ats-v1")
    # Adding a connector doesn't invalidate a board we already found; only age
    # retires a match.
    assert should_skip_probe(ledger, "Acme", NOW, TTL, "ats-v2") is True


def test_active_names_excludes_stale_verdicts():
    ledger = {}
    record(ledger, "MatchedCo", "matched", NOW - timedelta(days=5))
    record(ledger, "StaleMatch", "matched", NOW - timedelta(days=999))
    record(ledger, "FreshDrop", "dropped", NOW - timedelta(days=5))
    record(ledger, "StaleDrop", "dropped", NOW - timedelta(days=40))

    active = active_names(ledger, NOW, TTL)

    # Fresh match + fresh drop are still authoritative (excluded from re-search);
    # both stale verdicts are omitted so they can be re-suggested and re-probed.
    assert sorted(active) == ["FreshDrop", "MatchedCo"]
    # evaluated_names still returns everything, by contrast
    assert "StaleDrop" in evaluated_names(ledger)
    assert "StaleMatch" in evaluated_names(ledger)
