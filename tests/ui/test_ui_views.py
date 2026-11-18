import json
from datetime import date
from pathlib import Path

import pytest

from jobradar.models import FinalPosting, Posting, ScoredPosting
from jobradar.search.report import render_markdown as render_report
from jobradar.ui import views
from jobradar.ui.setup import SetupError


def _final(tier: str, title: str, company: str, url: str, skill: int, interest: int) -> FinalPosting:
    posting = Posting(
        id=url, title=title, company=company, url=url, source="test",
        location_text="Basel", description="…",
    )
    scored = ScoredPosting(
        posting=posting, skill_score=skill, interest_score=interest,
        best_cv="regulatory", brief_reason="",
    )
    return FinalPosting(scored=scored, tier=tier, writeup="**Why it fits.** Ten years of it.\n\n**Concerns.** None.")


def test_matches_are_read_back_from_the_report_the_search_wrote():
    """parse_report is the inverse of search/report.py's render_markdown; this
    fails if that format moves without this reader following."""
    text = render_report(
        [
            _final("best", "Head of Regulatory — EMEA", "Acme Pharma", "https://acme.example/1", 91, 84),
            _final("okay", "RA Manager", "Biotix", "https://biotix.example/2", 70, 45),
        ],
        date(2026, 11, 10),
    )
    best, okay = views.parse_report(text)
    assert best == {
        "tier": "best", "title": "Head of Regulatory — EMEA", "company": "Acme Pharma",
        "url": "https://acme.example/1", "location": "Basel", "best_cv": "regulatory",
        "source": "your company boards (Test)", "below_floors": "", "skill_score": 91, "interest_score": 84,
        "writeup": "**Why it fits.** Ten years of it.\n\n**Concerns.** None.",
    }
    assert (okay["tier"], okay["title"], okay["skill_score"]) == ("okay", "RA Manager", 70)


def test_a_quiet_day_has_no_matches():
    assert views.parse_report(render_report([], date(2026, 11, 10))) == []


def test_text_from_a_posting_cannot_become_markup():
    html = views.render_markdown(
        '<script>alert(1)</script> <img src=x onerror=alert(1)>\n\n'
        '[click](javascript:alert(1)) and [fine](https://example.org) **bold**'
    )
    assert "<script" not in html and "<img" not in html
    assert "javascript:" not in html
    assert 'href="https://example.org"' in html and "<strong>bold</strong>" in html


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "applications" / "007_2026-11-10_acme_ra-manager"
    folder.mkdir(parents=True)
    for name in ("notes.md", "tailored_cv.md", "tailored_cv_de.md", "job_description.md", "tailored_cv.pdf", "secret.txt"):
        (folder / name).write_text(f"# {name}\n", encoding="utf-8")
    archived = tmp_path / "applications" / "archive" / "003_old"
    archived.mkdir(parents=True)
    (tmp_path / "secret.md").write_text("outside", encoding="utf-8")
    (tmp_path / "applications" / "applications.json").write_text(json.dumps({"applications": [
        {"url": "https://acme.example/1", "folder": folder.name, "uid": "007", "title": "RA Manager",
         "company": "Acme", "status": "drafted", "prepared_on": "2026-11-10"},
        {"url": "https://old.example/3", "folder": "003_old", "uid": "003", "status": "rejected",
         "archived_on": "2026-10-01"},
    ]}), encoding="utf-8")
    (tmp_path / "reports").mkdir()
    return tmp_path


def test_only_active_applications_are_listed_with_their_documents_in_reading_order(root):
    (app,) = views.applications(root)
    assert app["uid"] == "007" and app["open"]
    assert app["files"] == ["notes.md", "tailored_cv.md", "tailored_cv.pdf", "tailored_cv_de.md", "job_description.md"]


@pytest.mark.parametrize("uid, name", [
    ("007", "../../secret.md"), ("007", "/etc/passwd"), ("007", "secret.txt"),
    ("007", "missing.md"), ("003", "notes.md"), ("999", "notes.md"), ("../..", "secret.md"),
])
def test_only_documents_inside_an_active_application_can_be_opened(root, uid, name):
    with pytest.raises(SetupError):
        views.file_path(root, uid, name)


def test_documents_sent_as_pdf_are_the_ones_saved_with_their_pdf(root):
    made = {name: views.read_file(root, "007", name)["makes_pdf"] for name in ("tailored_cv.md", "tailored_cv_de.md", "notes.md")}
    assert made == {"tailored_cv.md": True, "tailored_cv_de.md": True, "notes.md": False}


def test_a_pdf_older_than_its_text_is_named(root):
    import os

    folder = root / "applications" / "007_2026-11-10_acme_ra-manager"
    assert views.read_file(root, "007", "tailored_cv.md")["pdf_problem"] is None  # the PDF is the newer file
    md = folder / "tailored_cv.md"
    os.utime(md, (md.stat().st_atime, (folder / "tailored_cv.pdf").stat().st_mtime + 60))
    assert "older than tailored_cv.md" in views.read_file(root, "007", "tailored_cv.md")["pdf_problem"]
    assert views.read_file(root, "007", "notes.md")["pdf_problem"] is None


def test_the_job_description_is_read_not_edited(root):
    assert views.read_file(root, "007", "job_description.md")["editable"] is False
    assert views.read_file(root, "007", "tailored_cv.md")["editable"] is True
    with pytest.raises(SetupError):
        views.write_file(root, "007", "job_description.md", "changed")


def test_documents_can_be_edited_but_not_created(root):
    views.write_file(root, "7", "tailored_cv_de.md", "Neu\n\n\n")
    assert views.read_file(root, "007", "tailored_cv_de.md")["text"] == "Neu\n"
    with pytest.raises(SetupError):
        views.write_file(root, "007", "cover_letter.md", "not drafted yet")
    with pytest.raises(SetupError):
        views.write_file(root, "007", "tailored_cv.pdf", "x")


def test_report_links_a_match_to_its_application_and_flags_a_practice_run(root):
    (root / "reports" / "2026-11-09.md").write_text("# Job matches — 2026-11-09\n\nNo matches today.\n", encoding="utf-8")
    (root / "reports" / "2026-11-10.md").write_text(
        render_report([_final("okay", "RA Manager", "Acme", "https://acme.example/1/", 70, 45)], date(2026, 11, 10)),
        encoding="utf-8",
    )
    (root / "reports" / "company_health.md").write_text("# not a daily report\n", encoding="utf-8")
    (root / "reports" / "runs.jsonl").write_text(
        json.dumps({"date": "2026-11-09", "sources": [{"name": "company_pages"}], "funnel": {"raw": 10}}) + "\n"
        + json.dumps({"date": "2026-11-10", "sources": [{"name": "fixture"}], "funnel": {"raw": 5, "surfaced": 1}}) + "\n",
        encoding="utf-8",
    )
    data = views.report(root)
    newest, older = data["reports"]
    assert (newest["date"], older["date"], older["matches"]) == ("2026-11-10", "2026-11-09", [])
    assert (newest["matches"][0]["application"], newest["matches"][0]["application_status"]) == ("007", "drafted")
    assert "<strong>Why it fits.</strong>" in newest["matches"][0]["writeup_html"]
    assert data["last_run"]["found"] == 5 and data["last_run"]["practice"]
    assert data["hidden"] == 0


def test_only_the_newest_reports_are_read_where_nothing_archives_them(root, monkeypatch):
    monkeypatch.setattr(views, "MAX_REPORTS", 3)
    for day in range(1, 8):
        (root / "reports" / f"2026-10-{day:02d}.md").write_text("# Job matches\n\nNo matches today.\n", encoding="utf-8")
    (root / "reports" / "archive").mkdir()
    (root / "reports" / "archive" / "2026-12-31.md").write_text("# archived\n", encoding="utf-8")
    data = views.report(root)
    assert [r["date"] for r in data["reports"]] == ["2026-10-07", "2026-10-06", "2026-10-05"]
    assert data["hidden"] == 4
    assert views.overview(root)["report_date"] == "2026-10-07"


def test_no_report_yet(tmp_path):
    assert views.report(tmp_path) == {"reports": [], "practice": None, "hidden": 0, "archived": 0, "last_run": None}
    assert views.applications(tmp_path) == []


# ── a posting the pipeline could not read ──────────────────────────────────


@pytest.fixture
def stub(root: Path) -> Path:
    """The placeholder exactly as the apply pipeline leaves it."""
    from jobradar.apply.main import _stub_jd_file

    folder = root / "applications" / "008_2026-11-13_pending"
    folder.mkdir()
    url = "https://careers.example.com/job/R-1"
    (folder / "job_description.md").write_text(_stub_jd_file(url, "page returned too little text"), encoding="utf-8")
    tracker = root / "applications" / "applications.json"
    data = json.loads(tracker.read_text(encoding="utf-8"))
    data["applications"].append({"url": url, "folder": folder.name, "uid": "008", "status": "needs_jd"})
    tracker.write_text(json.dumps(data), encoding="utf-8")
    return root


JD = "Regulatory Affairs Manager. You lead submissions to Swissmedic and the EMA. " * 5


def test_a_placeholder_says_why_and_that_nothing_is_pasted_yet(stub):
    app = next(a for a in views.applications(stub) if a["uid"] == "008")
    assert app["jd_why"] == "page returned too little text"
    assert (app["jd_text"], app["jd_ready"], app["jd_min"]) == ("", False, 200)
    assert "jd_ready" not in next(a for a in views.applications(stub) if a["uid"] == "007")


def test_a_pasted_description_lands_where_the_pipeline_reads_it(stub):
    from jobradar.apply.main import _read_pasted_jd

    views.paste_jd(stub, "8", f"\n\n{JD}\n\n")
    path = stub / "applications" / "008_2026-11-13_pending" / "job_description.md"
    assert _read_pasted_jd(path) == JD.strip()
    assert path.read_text(encoding="utf-8").startswith("# Job description needed")
    app = next(a for a in views.applications(stub) if a["uid"] == "008")
    assert app["jd_ready"] and app["jd_text"] == JD.strip()
    # Pasting again replaces the text; it does not pile up under the marker.
    views.paste_jd(stub, "008", JD + "And more.")
    assert _read_pasted_jd(path) == (JD + "And more.").strip()


def test_a_paste_too_short_to_draft_from_is_refused_and_nothing_is_written(stub):
    path = stub / "applications" / "008_2026-11-13_pending" / "job_description.md"
    before = path.read_text(encoding="utf-8")
    with pytest.raises(SetupError, match="at least 200"):
        views.paste_jd(stub, "008", "Regulatory Affairs Manager, Basel.")
    assert path.read_text(encoding="utf-8") == before


def test_only_a_placeholder_takes_a_pasted_description(stub):
    with pytest.raises(SetupError):
        views.paste_jd(stub, "007", JD)  # already drafted
    with pytest.raises(SetupError):
        views.paste_jd(stub, "999", JD)


def test_a_placeholder_whose_file_was_emptied_by_hand_is_rebuilt(stub):
    from jobradar.apply.main import _read_pasted_jd

    path = stub / "applications" / "008_2026-11-13_pending" / "job_description.md"
    path.write_text("", encoding="utf-8")
    views.paste_jd(stub, "008", JD)
    assert _read_pasted_jd(path) == JD.strip()


# ── RAV ─────────────────────────────────────────────────────────────────────


def test_rav_month_shows_archived_applications_and_suggests_the_month_to_file(root):
    from datetime import date

    tracker = root / "applications" / "applications.json"
    data = json.loads(tracker.read_text(encoding="utf-8"))
    data["applications"][0].update(status="submitted", submitted_on="2026-10-20")
    tracker.write_text(json.dumps(data), encoding="utf-8")
    (root / "applications" / "archive" / "applications.json").write_text(json.dumps({"applications": [
        {"url": "https://old.example/9", "folder": "002_old", "uid": "002", "title": "Old role",
         "company": "Oldco", "status": "rejected", "submitted_on": "2026-09-03", "archived_on": "2026-10-01"},
    ]}), encoding="utf-8")
    (root / "applications" / "rav_filed.json").write_text(
        json.dumps({"filed": {"2026-09": {"filed_on": "2026-09-30", "report": "reports/rav/2026-09.md"}}}), encoding="utf-8"
    )
    today = date(2026, 11, 16)
    october = views.rav(root, today=today)
    assert (october["month"], october["unfiled"], october["filed"]) == ("2026-10", ["2026-10"], ["2026-09"])
    assert october["submitted"] == 1 and october["filed_on"] is None
    assert october["table_html"].count("<table>") == 1 and "| " not in october["table_html"]
    assert views.rav(root, "not-a-month", today=today)["month"] == "2026-10"
    september = views.rav(root, "2026-09", today=today)
    assert "Oldco" in september["table_html"]  # archived since, still in its month
    # Two rows in one month: still one table, not a table and loose text.
    data["applications"][0]["submitted_on"] = "2026-09-20"
    tracker.write_text(json.dumps(data), encoding="utf-8")
    two = views.rav(root, "2026-09", today=today)["table_html"]
    assert two.count("<table>") == 1 and two.count("<tr>") == 3 and "| " not in two
    assert (september["filed_on"], september["report"]) == ("2026-09-30", "reports/rav/2026-09.md")


# ── archives ────────────────────────────────────────────────────────────────


@pytest.fixture
def archived(root: Path) -> Path:
    """Application 003 archived, as apply/archive.py leaves it: its row in
    the archive tracker and its folder under applications/archive/."""
    folder = root / "applications" / "archive" / "003_old"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tailored_cv.md").write_text("# Sent CV\n", encoding="utf-8")
    (root / "applications" / "archive" / "applications.json").write_text(json.dumps({"applications": [
        {"url": "https://old.example/3", "folder": "003_old", "uid": "003", "title": "Old role", "company": "Oldco",
         "status": "rejected", "submitted_on": "2026-08-01", "archived_on": "2026-10-01"},
    ]}), encoding="utf-8")
    # The active tracker of the root fixture still has a stale row for 003;
    # the archive is what counts for an archived id.
    return root


def test_an_archived_application_opens_read_only(archived):
    app = views.application(archived, "3")
    assert (app["archived"], app["open"], app["files"]) == (True, False, ["tailored_cv.md"])
    assert views.read_file(archived, "003", "tailored_cv.md")["editable"] is False
    with pytest.raises(SetupError, match="archived"):
        views.write_file(archived, "003", "tailored_cv.md", "changed")
    assert (archived / "applications" / "archive" / "003_old" / "tailored_cv.md").read_text(encoding="utf-8") == "# Sent CV\n"
    assert views.application(archived, "7")["archived"] is False
    with pytest.raises(SetupError):
        views.application(archived, "999")


def test_archived_applications_come_a_page_at_a_time_newest_first(archived):
    rows = [{"url": f"https://x.example/{n}", "folder": f"{n:03d}_x", "uid": f"{n:03d}", "status": "rejected",
             "archived_on": "2026-10-01"} for n in range(1, 8)]
    (archived / "applications" / "archive" / "applications.json").write_text(json.dumps({"applications": rows}), encoding="utf-8")
    first = views.archived_applications(archived, 0, 3)
    assert [a["uid"] for a in first["applications"]] == ["007", "006", "005"]
    assert (first["total"], first["next"]) == (7, 3)
    last = views.archived_applications(archived, 6, 3)
    assert [a["uid"] for a in last["applications"]] == ["001"] and last["next"] is None
    assert views.archived_count(archived) == 7


def test_a_match_already_applied_to_and_archived_links_to_it(archived):
    (archived / "reports" / "2026-11-10.md").write_text(
        render_report([_final("okay", "Old role", "Oldco", "https://old.example/3", 70, 45)], date(2026, 11, 10)),
        encoding="utf-8",
    )
    (match,) = views.report(archived)["reports"][0]["matches"]
    assert (match["application"], match["application_status"], match["application_archived"]) == ("003", "rejected", True)


def test_archived_reports_are_counted_up_front_and_read_a_page_at_a_time(root, monkeypatch):
    archive = root / "reports" / "archive"
    archive.mkdir(parents=True)
    for day in range(1, 6):
        (archive / f"2026-10-{day:02d}.md").write_text(
            render_report([_final("okay", f"Role {day}", "Acme", f"https://acme.example/{day}", 70, 45)], date(2026, 10, day)),
            encoding="utf-8",
        )
    (archive / "notes.md").write_text("not a report\n", encoding="utf-8")
    assert views.report(root)["archived"] == 5
    first = views.report_archive(root, 0, 2)
    assert [r["date"] for r in first["reports"]] == ["2026-10-05", "2026-10-04"]
    assert (first["total"], first["next"]) == (5, 2)
    assert first["reports"][0]["matches"][0]["title"] == "Role 5"
    assert views.report_archive(root, 4, 2)["next"] is None
