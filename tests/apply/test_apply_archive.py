from datetime import date
from pathlib import Path

from jobradar.apply import archive as app_archive
from jobradar.apply.tracker import (
    STATUS_DRAFTED,
    STATUS_NEEDS_JD,
    STATUS_OFFER,
    STATUS_REJECTED,
    STATUS_SUBMITTED,
    STATUS_WITHDRAWN,
    Application,
    Tracker,
)
from jobradar.archive import Retention

_TODAY = date(2026, 8, 30)
_RETENTION = Retention()  # ghosted_days=60, stale_draft_days=60
_JULY_FILED = {"2026-07": {"filed_on": "2026-08-05", "report": "reports/rav/2026-07.md"}}


def _app(**overrides) -> Application:
    base = dict(
        url="https://jobs.example.com/pm-1",
        folder="2026-07-01_acme_pm",
        title="Product Manager",
        company="Acme",
        prepared_on="2026-07-01",
        status=STATUS_DRAFTED,
    )
    base.update(overrides)
    return Application(**base)


def _reason(app: Application, rav_filed: dict = _JULY_FILED) -> str | None:
    return app_archive.archive_reason(app, rav_filed, _TODAY, _RETENTION)


# --- RAV filed ledger ---------------------------------------------------------


def test_rav_filed_roundtrip(tmp_path):
    assert app_archive.load_rav_filed(tmp_path) == {}
    app_archive.mark_rav_filed(tmp_path, "2026-07", "reports/rav/2026-07.md", date(2026, 8, 5))
    filed = app_archive.load_rav_filed(tmp_path)
    assert filed["2026-07"] == {"filed_on": "2026-08-05", "report": "reports/rav/2026-07.md"}


def test_rav_refiled_keeps_original_filed_on(tmp_path):
    app_archive.mark_rav_filed(tmp_path, "2026-07", "reports/rav/2026-07.md", date(2026, 8, 5))
    app_archive.mark_rav_filed(tmp_path, "2026-07", "reports/rav/2026-07.md", date(2026, 8, 20))
    assert app_archive.load_rav_filed(tmp_path)["2026-07"]["filed_on"] == "2026-08-05"


# --- archive_reason policy ----------------------------------------------------


def test_submitted_is_kept_while_month_unfiled_whatever_the_status():
    for status in (STATUS_SUBMITTED, STATUS_REJECTED, STATUS_WITHDRAWN, STATUS_OFFER):
        app = _app(status=status, submitted_on="2026-07-01")
        assert _reason(app, rav_filed={}) is None


def test_rejected_after_submission_archives_once_month_filed():
    app = _app(status=STATUS_REJECTED, submitted_on="2026-07-01", closed_on="2026-07-15")
    assert _reason(app) == "outcome"


def test_offer_is_terminal_like_any_other_outcome():
    app = _app(status=STATUS_OFFER, submitted_on="2026-07-01", closed_on="2026-07-15")
    assert _reason(app) == "outcome"


def test_ghosted_archives_at_exactly_ghosted_days():
    app = _app(status=STATUS_SUBMITTED, submitted_on="2026-07-01")  # 60 days before _TODAY
    assert _reason(app) == "no-response"
    fresh = _app(status=STATUS_SUBMITTED, submitted_on="2026-07-02")  # 59 days
    assert _reason(fresh) is None


def test_withdrawn_before_submission_archives_immediately():
    app = _app(status=STATUS_WITHDRAWN, closed_on="2026-08-29")
    assert _reason(app, rav_filed={}) == "outcome"


def test_stale_draft_archives_at_exactly_stale_draft_days():
    for status in (STATUS_DRAFTED, STATUS_NEEDS_JD):
        assert _reason(_app(status=status, prepared_on="2026-07-01"), rav_filed={}) == "stale-draft"
        assert _reason(_app(status=status, prepared_on="2026-07-02"), rav_filed={}) is None


def test_fresh_draft_is_kept():
    assert _reason(_app(prepared_on="2026-08-28")) is None


# --- RAV disabled: no filed-month gate at all ---------------------------------


def _reason_no_rav(app: Application) -> str | None:
    return app_archive.archive_reason(app, {}, _TODAY, _RETENTION, rav_enabled=False)


def test_rav_disabled_archives_outcomes_without_a_filed_month():
    for status in (STATUS_REJECTED, STATUS_WITHDRAWN, STATUS_OFFER):
        app = _app(status=status, submitted_on="2026-07-01", closed_on="2026-07-15")
        assert _reason_no_rav(app) == "outcome"


def test_rav_disabled_ghosts_at_exactly_ghosted_days():
    assert _reason_no_rav(_app(status=STATUS_SUBMITTED, submitted_on="2026-07-01")) == "no-response"
    assert _reason_no_rav(_app(status=STATUS_SUBMITTED, submitted_on="2026-07-02")) is None


def test_rav_enabled_is_the_default_so_existing_callers_keep_the_gate():
    app = _app(status=STATUS_REJECTED, submitted_on="2026-07-01", closed_on="2026-07-15")
    assert app_archive.archive_reason(app, {}, _TODAY, _RETENTION) is None


# --- sweep / archive_applications ----------------------------------------------


def _tracked_dir(tmp_path, *apps: Application) -> tuple[Tracker, Path]:
    applications_dir = tmp_path / "applications"
    applications_dir.mkdir()
    tracker = Tracker(applications_dir / "applications.json")
    for app in apps:
        (applications_dir / app.folder).mkdir()
        (applications_dir / app.folder / "notes.md").write_text("notes", encoding="utf-8")
        tracker.upsert(app)
    tracker.save()
    return tracker, applications_dir


def test_sweep_moves_folder_and_tracker_row(tmp_path):
    app = _app(status=STATUS_REJECTED, submitted_on="2026-07-01", closed_on="2026-07-15")
    tracker, applications_dir = _tracked_dir(tmp_path, app)
    app_archive.mark_rav_filed(applications_dir, "2026-07", "reports/rav/2026-07.md", _TODAY)
    (applications_dir / "queue.txt").write_text(
        f"# to apply\n{app.url}\nhttps://jobs.example.com/other\n", encoding="utf-8"
    )

    archived = app_archive.sweep(tracker, applications_dir, _TODAY, _RETENTION)

    assert [(a.folder, reason) for a, reason in archived] == [(app.folder, "outcome")]
    assert tracker.applications == []
    assert not (applications_dir / app.folder).exists()
    assert (applications_dir / "archive" / app.folder / "notes.md").exists()
    archive_tracker = Tracker(app_archive.archive_tracker_path(applications_dir))
    assert archive_tracker.applications[0].archived_on == _TODAY.isoformat()
    # Re-loading the active tracker shows the row is really gone from disk.
    assert Tracker(applications_dir / "applications.json").applications == []
    # The archived URL's queue line is pruned; everything else survives.
    assert (applications_dir / "queue.txt").read_text(encoding="utf-8") == (
        "# to apply\nhttps://jobs.example.com/other\n"
    )

    # Same-day re-sweep is a no-op.
    assert app_archive.sweep(tracker, applications_dir, _TODAY, _RETENTION) == []


def test_sweep_without_rav_archives_ghosted_with_no_ledger_on_disk(tmp_path):
    ghosted = _app(status=STATUS_SUBMITTED, submitted_on="2026-07-01")
    tracker, applications_dir = _tracked_dir(tmp_path, ghosted)
    assert not (applications_dir / app_archive.RAV_FILED_FILENAME).exists()

    archived = app_archive.sweep(
        tracker, applications_dir, _TODAY, _RETENTION, rav_enabled=False
    )

    assert [(a.folder, reason) for a, reason in archived] == [(ghosted.folder, "no-response")]
    assert (applications_dir / "archive" / ghosted.folder / "notes.md").exists()


def test_sweep_keeps_active_applications(tmp_path):
    keep = _app(status=STATUS_SUBMITTED, submitted_on="2026-08-20")
    tracker, applications_dir = _tracked_dir(tmp_path, keep)
    app_archive.mark_rav_filed(applications_dir, "2026-08", "reports/rav/2026-08.md", _TODAY)
    assert app_archive.sweep(tracker, applications_dir, _TODAY, _RETENTION) == []
    assert (applications_dir / keep.folder).is_dir()


def test_collision_is_skipped_and_stays_active(tmp_path):
    app = _app(status=STATUS_WITHDRAWN, closed_on="2026-08-01")
    tracker, applications_dir = _tracked_dir(tmp_path, app)
    (applications_dir / "archive" / app.folder).mkdir(parents=True)
    (applications_dir / "queue.txt").write_text(f"{app.url}\n", encoding="utf-8")

    archived = app_archive.archive_applications(tracker, applications_dir, [app], _TODAY)

    assert archived == []
    assert (applications_dir / app.folder).is_dir()  # never clobbered
    assert tracker.applications == [app]
    # Still active, so its queue line stays too.
    assert (applications_dir / "queue.txt").read_text(encoding="utf-8") == f"{app.url}\n"


def test_already_moved_folder_self_heals(tmp_path):
    app = _app(status=STATUS_WITHDRAWN, closed_on="2026-08-01")
    tracker, applications_dir = _tracked_dir(tmp_path, app)
    # Simulate a crash after the folder moved but before the trackers saved.
    (applications_dir / "archive").mkdir()
    (applications_dir / app.folder).rename(applications_dir / "archive" / app.folder)

    archived = app_archive.archive_applications(tracker, applications_dir, [app], _TODAY)

    assert [a.folder for a in archived] == [app.folder]
    assert tracker.applications == []


def test_dangling_row_is_archived_anyway(tmp_path):
    applications_dir = tmp_path / "applications"
    applications_dir.mkdir()
    tracker = Tracker(applications_dir / "applications.json")
    app = _app(status=STATUS_WITHDRAWN, closed_on="2026-08-01")
    tracker.upsert(app)  # no folder on disk

    archived = app_archive.archive_applications(tracker, applications_dir, [app], _TODAY)

    assert [a.folder for a in archived] == [app.folder]
    archive_tracker = Tracker(app_archive.archive_tracker_path(applications_dir))
    assert archive_tracker.applications[0].folder == app.folder
