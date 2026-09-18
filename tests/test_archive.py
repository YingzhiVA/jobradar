import json
from datetime import date, datetime, timezone

import pytest

from jobradar.archive import Retention, archive_reports, archive_seen, load_retention


# --- load_retention ----------------------------------------------------------


def test_load_retention_defaults_without_file(tmp_path):
    assert load_retention(tmp_path) == Retention()


def test_load_retention_partial_yaml_overrides_only_named_keys(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "retention.yaml").write_text(
        "reports_days: 7\nunknown_key: 3\n", encoding="utf-8"
    )
    retention = load_retention(tmp_path)
    assert retention.reports_days == 7
    assert retention.seen_days == Retention().seen_days


# --- archive_reports ---------------------------------------------------------


def _reports_dir(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "2026-05-01.md").write_text("old report", encoding="utf-8")
    (reports / "2026-08-10.md").write_text("recent report", encoding="utf-8")
    (reports / "company_health.md").write_text("health", encoding="utf-8")
    (reports / "latest.json").write_text("{}", encoding="utf-8")
    (reports / "runs.jsonl").write_text("{}\n", encoding="utf-8")
    return reports


def test_archive_reports_moves_only_old_dated_files(tmp_path):
    reports = _reports_dir(tmp_path)
    moved = archive_reports(reports, date(2026, 8, 13), 90)

    assert [p.name for p in moved] == ["2026-05-01.md"]
    assert not (reports / "2026-05-01.md").exists()
    assert (reports / "archive" / "2026-05-01.md").read_text(encoding="utf-8") == "old report"
    # Everything that isn't an out-of-window dated report stays put.
    for name in ("2026-08-10.md", "company_health.md", "latest.json", "runs.jsonl"):
        assert (reports / name).exists()

    # Idempotent: a second run finds nothing left to move.
    assert archive_reports(reports, date(2026, 8, 13), 90) == []


def test_archive_reports_dry_run_moves_nothing(tmp_path):
    reports = _reports_dir(tmp_path)
    moved = archive_reports(reports, date(2026, 8, 13), 90, dry_run=True)
    assert [p.name for p in moved] == ["2026-05-01.md"]
    assert (reports / "2026-05-01.md").exists()
    assert not (reports / "archive").exists()


def test_archive_reports_overwrites_leftover_archive_copy(tmp_path):
    # A crash between move and commit can leave the same file on both sides;
    # the re-run must overwrite, not fail.
    reports = _reports_dir(tmp_path)
    (reports / "archive").mkdir()
    (reports / "archive" / "2026-05-01.md").write_text("old report", encoding="utf-8")
    moved = archive_reports(reports, date(2026, 8, 13), 90)
    assert [p.name for p in moved] == ["2026-05-01.md"]
    assert not (reports / "2026-05-01.md").exists()


# --- archive_seen ------------------------------------------------------------

_NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)


def _entry(first_seen: str) -> dict:
    return {
        "url": "https://x.example/job",
        "title": "PM",
        "company": "Acme",
        "first_seen_at": first_seen,
        "outcome": "considered",
    }


def _seen_file(tmp_path, entries: dict):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "seen_postings.json").write_text(json.dumps(entries), encoding="utf-8")
    return data


def test_archive_seen_splits_on_cutoff(tmp_path):
    data = _seen_file(
        tmp_path,
        {
            "old": _entry("2025-01-01T00:00:00+00:00"),
            "new": _entry("2026-08-01T00:00:00+00:00"),
        },
    )
    assert archive_seen(data, _NOW, 365) == 1

    active = json.loads((data / "seen_postings.json").read_text(encoding="utf-8"))
    archived = json.loads((data / "archive" / "seen_postings.json").read_text(encoding="utf-8"))
    assert set(active) == {"new"}
    assert set(archived) == {"old"}


def test_archive_seen_merge_is_insert_or_ignore(tmp_path):
    data = _seen_file(tmp_path, {"old": _entry("2025-01-01T00:00:00+00:00")})
    (data / "archive").mkdir()
    prior = _entry("2024-06-01T00:00:00+00:00")  # existing archive record wins
    (data / "archive" / "seen_postings.json").write_text(
        json.dumps({"old": prior}), encoding="utf-8"
    )
    archive_seen(data, _NOW, 365)
    archived = json.loads((data / "archive" / "seen_postings.json").read_text(encoding="utf-8"))
    assert archived["old"]["first_seen_at"] == "2024-06-01T00:00:00+00:00"


def test_archive_seen_unparseable_timestamp_stays_active(tmp_path):
    data = _seen_file(
        tmp_path,
        {
            "weird": _entry("not-a-date"),
            "old": _entry("2025-01-01T00:00:00+00:00"),
        },
    )
    archive_seen(data, _NOW, 365)
    active = json.loads((data / "seen_postings.json").read_text(encoding="utf-8"))
    assert "weird" in active


def test_archive_seen_dry_run_counts_without_moving(tmp_path):
    data = _seen_file(tmp_path, {"old": _entry("2025-01-01T00:00:00+00:00")})
    assert archive_seen(data, _NOW, 365, dry_run=True) == 1
    assert not (data / "archive").exists()


def test_archive_seen_corrupt_store_raises_untouched(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "seen_postings.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ValueError):
        archive_seen(data, _NOW, 365)
    # Never "start fresh" here: CI would commit the wipe as if it were real.
    assert (data / "seen_postings.json").read_text(encoding="utf-8") == "{corrupt"
    assert not (data / "archive").exists()
