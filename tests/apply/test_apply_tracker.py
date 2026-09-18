from datetime import date

import pytest

from jobradar.apply.tracker import (
    STATUS_DRAFTED,
    STATUS_OFFER,
    STATUS_REJECTED,
    STATUS_SUBMITTED,
    STATUS_WITHDRAWN,
    Application,
    Tracker,
    next_uid,
    render_rav_month,
)


def _app(**overrides) -> Application:
    base = dict(
        url="https://jobs.example.com/pm-1",
        folder="001_2026-07-06_acme_pm",
        uid="001",
        title="Product Manager",
        company="Acme",
        prepared_on="2026-07-06",
        status=STATUS_DRAFTED,
    )
    base.update(overrides)
    return Application(**base)


def test_tracker_roundtrip_and_upsert(tmp_path):
    path = tmp_path / "applications.json"
    tracker = Tracker(path)
    tracker.upsert(_app())
    tracker.save()

    reloaded = Tracker(path)
    assert len(reloaded.applications) == 1
    assert reloaded.find_by_url("https://jobs.example.com/pm-1/").folder == "001_2026-07-06_acme_pm"

    # Upsert with the same URL replaces rather than duplicates.
    reloaded.upsert(_app(folder="renamed"))
    assert len(reloaded.applications) == 1
    assert reloaded.applications[0].folder == "renamed"


def test_tracker_ignores_unknown_json_fields(tmp_path):
    path = tmp_path / "applications.json"
    path.write_text(
        '{"applications": [{"url": "https://x.example/a", "folder": "f", '
        '"future_field": true}]}',
        encoding="utf-8",
    )
    tracker = Tracker(path)
    assert tracker.applications[0].url == "https://x.example/a"


def test_mark_submitted_by_folder_and_url(tmp_path):
    tracker = Tracker(tmp_path / "applications.json")
    tracker.upsert(_app())

    app = tracker.mark_submitted("applications/001_2026-07-06_acme_pm", on=date(2026, 7, 8))
    assert app is not None
    assert app.status == STATUS_SUBMITTED
    assert app.submitted_on == "2026-07-08"

    assert tracker.mark_submitted("https://jobs.example.com/pm-1") is not None
    assert tracker.mark_submitted("no-such-thing") is None


def test_find_by_uid_ignores_zero_padding(tmp_path):
    tracker = Tracker(tmp_path / "applications.json")
    tracker.upsert(_app(uid="042", folder="042_2026-07-06_acme_pm"))

    for key in ("42", "042", "0042"):
        assert tracker.find(key) is not None, key
    assert tracker.find("43") is None


def test_uid_lookup_does_not_shadow_folder_or_url(tmp_path):
    tracker = Tracker(tmp_path / "applications.json")
    tracker.upsert(_app())
    tracker.upsert(
        _app(uid="002", folder="002_2026-07-10_beta_pm", url="https://jobs.example.com/pm-2")
    )

    assert tracker.find("2").folder == "002_2026-07-10_beta_pm"
    assert tracker.find("001_2026-07-06_acme_pm").uid == "001"
    assert tracker.find("https://jobs.example.com/pm-2").uid == "002"


def test_old_json_without_uid_is_still_addressable_by_folder(tmp_path):
    path = tmp_path / "applications.json"
    path.write_text(
        '{"applications": [{"url": "https://x.example/a", "folder": "f"}]}',
        encoding="utf-8",
    )
    tracker = Tracker(path)
    assert tracker.applications[0].uid == ""
    assert tracker.find("f") is not None
    assert tracker.find("1") is None  # an empty uid must never match an id


def test_next_uid_walks_past_archived_ids():
    active = [_app(uid="003")]
    archived = [_app(uid="007"), _app(uid="004")]
    # Ids are never reused, so the counter clears the highest archived one.
    assert next_uid(active, archived) == "008"
    assert next_uid(active) == "004"
    assert next_uid([], []) == "001"
    # Entries predating uids (or a hand-edited blank) are simply skipped.
    assert next_uid([_app(uid="")], [_app(uid="011")]) == "012"


def test_render_rav_month():
    apps = [
        _app(status=STATUS_SUBMITTED, submitted_on="2026-07-08", postcode="8001"),
        _app(
            url="https://jobs.example.com/pm-2",
            folder="002_2026-07-10_beta_pm",
            uid="002",
            company="Beta",
            prepared_on="2026-07-10",
        ),
        _app(
            url="https://jobs.example.com/pm-3",
            folder="003_2026-06-01_old_pm",
            uid="003",
            status=STATUS_SUBMITTED,
            submitted_on="2026-06-02",
        ),
    ]
    out = render_rav_month(apps, "2026-07")
    assert "Submitted applications: 1 (hängig 1)" in out
    assert "| 08.07.2026 | Acme | 8001 | Product Manager | online | hängig |" in out
    assert "2026-07-08" not in out  # dates are rendered Swiss-style, not ISO
    assert "02.06.2026" not in out  # other month excluded
    # The drafted-but-unsubmitted reminder lists the id you would type.
    assert "- 002 (Product Manager at Beta)" in out


def test_render_rav_month_separates_entries_with_blank_lines():
    apps = [
        _app(status=STATUS_SUBMITTED, submitted_on="2026-07-08"),
        _app(
            url="https://jobs.example.com/pm-2",
            uid="002",
            status=STATUS_SUBMITTED,
            submitted_on="2026-07-09",
        ),
        _app(url="https://jobs.example.com/pm-3", uid="003", prepared_on="2026-07-10"),
        _app(url="https://jobs.example.com/pm-4", uid="004", prepared_on="2026-07-11"),
    ]
    out = render_rav_month(apps, "2026-07")
    assert "| https://jobs.example.com/pm-1 |\n\n| 09.07.2026" in out
    assert "- 003 (Product Manager at Acme)\n\n- 004" in out
    assert not out.endswith("\n\n")  # ...but no gap left dangling at the end


def test_render_rav_month_missing_postcode():
    apps = [_app(status=STATUS_SUBMITTED, submitted_on="2026-07-08")]
    assert "| 08.07.2026 | Acme | ? | Product Manager |" in render_rav_month(apps, "2026-07")


def test_mark_outcome_by_folder_and_url(tmp_path):
    tracker = Tracker(tmp_path / "applications.json")
    tracker.upsert(_app())

    app = tracker.mark_outcome("001_2026-07-06_acme_pm", STATUS_REJECTED, on=date(2026, 7, 20))
    assert app is not None
    assert app.status == STATUS_REJECTED
    assert app.closed_on == "2026-07-20"

    assert tracker.mark_outcome("https://jobs.example.com/pm-1", STATUS_REJECTED) is not None
    assert tracker.mark_outcome("no-such-thing", STATUS_REJECTED) is None


def test_mark_outcome_rejects_non_terminal_status(tmp_path):
    tracker = Tracker(tmp_path / "applications.json")
    tracker.upsert(_app())
    with pytest.raises(ValueError):
        tracker.mark_outcome("001_2026-07-06_acme_pm", STATUS_SUBMITTED)


def test_roundtrip_persists_outcome_fields(tmp_path):
    path = tmp_path / "applications.json"
    tracker = Tracker(path)
    tracker.upsert(_app(status=STATUS_REJECTED, closed_on="2026-07-20", archived_on="2026-08-05"))
    tracker.save()

    reloaded = Tracker(path).applications[0]
    assert (reloaded.closed_on, reloaded.archived_on) == ("2026-07-20", "2026-08-05")


def test_old_json_without_outcome_fields_still_loads(tmp_path):
    path = tmp_path / "applications.json"
    path.write_text(
        '{"applications": [{"url": "https://x.example/a", "folder": "f"}]}',
        encoding="utf-8",
    )
    app = Tracker(path).applications[0]
    assert app.closed_on is None
    assert app.archived_on is None


def test_rejected_application_still_counts_as_rav_proof():
    # An outcome recorded later must not remove the submission from the month's
    # proof table.
    apps = [_app(status=STATUS_REJECTED, submitted_on="2026-07-08", closed_on="2026-07-20")]
    out = render_rav_month(apps, "2026-07")
    assert "Submitted applications: 1 (Absage 1)" in out
    assert "| 08.07.2026 | Acme |" in out
    # ...but the form's Ergebnis column has to say what became of it.
    assert "| online | Absage (20.07.2026) |" in out


def test_render_rav_month_outcome_column_and_breakdown():
    apps = [
        _app(status=STATUS_SUBMITTED, submitted_on="2026-07-08"),
        _app(
            url="https://jobs.example.com/pm-2",
            uid="002",
            status=STATUS_REJECTED,
            submitted_on="2026-07-09",
            closed_on="2026-07-25",
        ),
        _app(
            url="https://jobs.example.com/pm-3",
            uid="003",
            status=STATUS_WITHDRAWN,
            submitted_on="2026-07-10",
        ),
        _app(
            url="https://jobs.example.com/pm-4",
            uid="004",
            status=STATUS_OFFER,
            submitted_on="2026-07-11",
            closed_on="2026-07-28",
        ),
    ]
    out = render_rav_month(apps, "2026-07")
    assert "Submitted applications: 4 (hängig 1, Absage 1, zurückgezogen 1, Angebot 1)" in out
    assert "| Bewerbungsart | Ergebnis | Link |" in out
    assert "| 08.07.2026 | Acme | ? | Product Manager | online | hängig |" in out
    assert "| online | Absage (25.07.2026) |" in out
    # A closed outcome with no recorded date still reads as the outcome.
    assert "| online | zurückgezogen |" in out
    assert "| online | Angebot (28.07.2026) |" in out


def test_archived_draft_is_not_listed_as_pending():
    apps = [_app(status=STATUS_DRAFTED, archived_on="2026-08-30")]
    assert "Drafted this month" not in render_rav_month(apps, "2026-07")
