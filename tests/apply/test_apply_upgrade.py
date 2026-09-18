"""--upgrade: re-draft an existing quick-tier application at full tier."""

from datetime import date
from pathlib import Path

import pytest

from jobradar.apply import main as apply_main
from jobradar.apply.extract import JDExtract
from jobradar.apply.queue import QueueEntry
from jobradar.apply.tailor import ApplicationDocs
from jobradar.apply.tracker import (
    STATUS_DRAFTED,
    STATUS_NEEDS_JD,
    STATUS_SUBMITTED,
    Application,
    Tracker,
)

URL = "https://jobs.example.com/pm-1"
FOLDER = "028_2026-09-04_acme_product-manager"


def _tracker(tmp_path: Path, **overrides) -> Tracker:
    tracker = Tracker(tmp_path / "applications.json")
    base = dict(
        url=URL,
        folder=FOLDER,
        uid="028",
        title="Product Manager",
        company="Acme",
        prepared_on="2026-09-04",
        tier="quick",
        base_cv="CV_X",
        cover_letter=False,
        analysis_source="report:2026-09-04",
        status=STATUS_DRAFTED,
    )
    base.update(overrides)
    tracker.applications.append(Application(**base))
    return tracker


def test_upgrade_entries_marks_full_and_redraft(tmp_path):
    tracker = _tracker(tmp_path)
    # Zero-padding is optional, like every other KEY.
    entries = apply_main._upgrade_entries(tracker, ["28"])
    assert entries == [QueueEntry(url=URL, full=True, redraft=True)]


def test_upgrade_entries_refuses_submitted(tmp_path):
    tracker = _tracker(tmp_path, status=STATUS_SUBMITTED, submitted_on="2026-09-05")
    assert apply_main._upgrade_entries(tracker, ["028"]) == []


def test_upgrade_entries_skips_unknown_key(tmp_path):
    tracker = _tracker(tmp_path)
    assert apply_main._upgrade_entries(tracker, ["999"]) == []


def _context(tmp_path: Path, tracker: Tracker) -> apply_main._Context:
    return apply_main._Context(
        client=object(),  # never used: the API calls are monkeypatched away
        profile_block="PROFILE",
        cv_labels=["CV_X"],
        findings={},
        tracker=tracker,
        applications_dir=tmp_path,
        helper_usage={},
        writer_usage={},
    )


@pytest.fixture
def drafted(tmp_path, monkeypatch):
    """A quick-tier application on disk, with the API calls stubbed out."""
    folder = tmp_path / FOLDER
    folder.mkdir()
    (folder / "job_description.md").write_text(
        "# Product Manager at Acme\n\n- URL: %s\n\n---\n\n%s\n" % (URL, "Job duties. " * 40),
        encoding="utf-8",
    )
    (folder / "tailored_cv.md").write_text("# CV\n", encoding="utf-8")
    (folder / "tailored_cv.pdf").write_bytes(b"%PDF-stale")

    monkeypatch.setattr(
        apply_main,
        "extract_jd",
        lambda text, client, usage_acc=None, on_delta=None: JDExtract(
            is_job_posting=True,
            title="Product Manager",
            company="Acme",
            language="en",
            cover_letter_required=False,
            description_markdown="Job duties. " * 40,
        ),
    )
    monkeypatch.setattr(
        apply_main,
        "write_application_docs",
        lambda base_cv, language, include_cover_letter, *a, **kw: ApplicationDocs(
            tailored_cv_markdown="# Tailored CV\n",
            cover_letter_markdown="Dear hiring manager\n" if include_cover_letter else "",
            highlights=["reordered bullets"],
        ),
    )
    monkeypatch.setattr(apply_main, "fetch_page", _fetch_must_not_run)
    monkeypatch.setattr(apply_main.pdf, "convert_folder", lambda path: None)
    monkeypatch.setattr(apply_main, "date", _FixedDate)
    return folder


def _fetch_must_not_run(url):  # pragma: no cover - asserted by failing loudly
    raise AssertionError(f"re-draft should reuse the saved JD, not fetch {url}")


class _FixedDate(date):
    """Freezes date.today(), so the rebuilt folder name stays comparable."""

    @classmethod
    def today(cls) -> "date":
        return cls(2026, 9, 4)


def test_redraft_writes_cover_letter_and_keeps_identity(tmp_path, drafted):
    tracker = _tracker(tmp_path)
    ctx = _context(tmp_path, tracker)

    assert apply_main.process_entry(ctx, QueueEntry(url=URL, full=True, redraft=True))

    app = tracker.find_by_url(URL)
    assert app.uid == "028" and app.folder == FOLDER  # same id, same folder
    assert app.tier == "full" and app.cover_letter is True
    assert app.status == STATUS_DRAFTED
    assert (drafted / "cover_letter.md").read_text(encoding="utf-8").startswith("Dear")
    assert (drafted / "tailored_cv.md").read_text(encoding="utf-8") == "# Tailored CV\n"
    # Full tier: PDFs are the user's --pdf step, so the stale one is left for
    # the linter to flag rather than silently regenerated.
    assert (drafted / "tailored_cv.pdf").exists()


def test_without_redraft_a_drafted_url_is_still_skipped(tmp_path, drafted):
    tracker = _tracker(tmp_path)
    ctx = _context(tmp_path, tracker)

    assert not apply_main.process_entry(ctx, QueueEntry(url=URL, full=True))

    assert tracker.find_by_url(URL).tier == "quick"
    assert not (drafted / "cover_letter.md").exists()


def test_bad_jd_on_redraft_leaves_the_draft_alone(tmp_path, drafted, monkeypatch):
    """A stub folder must never be written over a real application."""
    tracker = _tracker(tmp_path)
    ctx = _context(tmp_path, tracker)
    monkeypatch.setattr(
        apply_main,
        "extract_jd",
        lambda text, client, usage_acc=None, on_delta=None: JDExtract(is_job_posting=False),
    )

    assert not apply_main.process_entry(ctx, QueueEntry(url=URL, full=True, redraft=True))

    app = tracker.find_by_url(URL)
    assert app.status == STATUS_DRAFTED and app.folder == FOLDER
    assert app.tier == "quick"
    assert (drafted / "tailored_cv.md").exists()
    assert not list(tmp_path.glob("*_pending"))


def test_needs_jd_application_can_be_upgraded(tmp_path):
    """A stub waiting for a pasted JD is a draft-to-be: upgrading is allowed."""
    tracker = _tracker(tmp_path, status=STATUS_NEEDS_JD, tier="quick")
    entries = apply_main._upgrade_entries(tracker, ["028"])
    assert entries == [QueueEntry(url=URL, full=True, redraft=True)]
