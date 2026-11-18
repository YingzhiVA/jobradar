"""Read what the commands produced: the latest report, the run log, the
application tracker and the files of one application."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import markdown as markdown_lib

from ..apply.main import _MIN_PASTED_CHARS, STUB_MARKER, _read_pasted_jd, _stub_jd_file
from ..apply import archive as app_archive
from ..apply import lint, pdf
from ..apply.tracker import render_rav_month
from ..apply.tracker import (
    STATUS_DRAFTED,
    STATUS_NEEDS_JD,
    STATUS_SUBMITTED,
    TERMINAL_STATUSES,
    Tracker,
)
from ..schedule import is_practice_run
from ..search.observability import PRACTICE_RECORD
from ..search.report import PRACTICE_REPORT
from .setup import SetupError

_UNSAFE_HREF = re.compile(r'(href|src)="(?!https?://|mailto:|#)[^"]*"', re.IGNORECASE)


def render_markdown(text: str) -> str:
    """Markdown to HTML for display. The text is partly a web page's (a job
    description) and partly a model's, so nothing in it may reach the browser
    as markup: tags are escaped before conversion and links are limited to
    web and mail addresses afterwards."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;")
    html = markdown_lib.markdown(escaped, extensions=["tables", "sane_lists"])
    return _UNSAFE_HREF.sub(r'\1="#"', html)


def _tracker(root: Path) -> Tracker:
    return Tracker(root / "applications" / "applications.json")


def _last_ledger_record(root: Path) -> dict | None:
    path = root / "reports" / "runs.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            # One record carries every scored posting; the last megabytes are
            # enough to hold the final line without reading the whole log.
            handle.seek(max(0, size - 4_000_000))
            tail = handle.read().decode("utf-8", errors="replace")
        return json.loads(tail.rstrip().rsplit("\n", 1)[-1])
    except (OSError, ValueError, IndexError):
        return None


def _when(record: dict) -> str:
    # run_at is an ISO timestamp; a record without one sorts by its date,
    # which as a prefix sorts before any time on that day.
    return str(record.get("run_at") or record.get("date") or "")


def _last_run(root: Path) -> dict | None:
    """The funnel of the most recent run: the last line of the run log, or the
    practice run's record (reports/practice.json, kept out of the tracked log)
    if that is newer."""
    record = _last_ledger_record(root)
    try:
        practice = json.loads((root / "reports" / PRACTICE_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        practice = None
    if isinstance(practice, dict) and (record is None or _when(practice) > _when(record)):
        record = practice
    if record is None:
        return None
    funnel = record.get("funnel") or {}
    return {
        "date": record.get("date"),
        "found": funnel.get("raw"),
        "new": funnel.get("new"),
        "passed": funnel.get("passed_hard_filters"),
        "scored": funnel.get("scored"),
        "surfaced": funnel.get("surfaced"),
        "failed_sources": record.get("failed_sources") or [],
        # A practice run (--dry-run) scores made-up postings; its report is
        # reports/practice.md, shown apart from the real ones.
        "practice": is_practice_run(record),
    }


_REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_MATCH_HEADING = re.compile(r"^## \[(BEST|OKAY)\] (.+)$", re.MULTILINE)
_FIELD = re.compile(r"^- (Link|Location|Best-fit CV|Found by|Below your floors): (.*)$", re.MULTILINE)
# Any other section ends a match: a later run's "## Later run at …" heading
# must not end up inside the write-up of the match above it.
_SECTION = re.compile(r"^## ", re.MULTILINE)
_SCORES = re.compile(r"^- Skill score: (\d+)/100, Interest score: (\d+)/100$", re.MULTILINE)


def parse_report(text: str) -> list[dict]:
    """The matches of a daily report, read back from the Markdown that
    search/report.py's render_markdown wrote. The dated report is what every
    run leaves behind, a cloud run included; latest.json stays on the machine
    that made it."""
    parts = _MATCH_HEADING.split(text)
    matches = []
    for tier, heading, body in zip(parts[1::3], parts[2::3], parts[3::3]):
        body = _SECTION.split(body, 1)[0]
        title, _, company = heading.strip().rpartition(" — ")
        fields = dict(_FIELD.findall(body))
        scores = _SCORES.search(body)
        # The write-up is what follows the list of facts under the heading.
        blocks = body.strip().split("\n\n")
        writeup = "\n\n".join(b for b in blocks if not b.lstrip().startswith("- Link:"))
        matches.append({
            "tier": tier.lower(),
            "title": title or heading.strip(),
            "company": company,
            "url": fields.get("Link", "").strip(),
            "location": fields.get("Location", "").strip(),
            "best_cv": fields.get("Best-fit CV", "").strip(),
            "source": fields.get("Found by", "").strip(),  # absent before 1.28
            "below_floors": fields.get("Below your floors", "").strip(),  # practice runs only
            "skill_score": int(scores.group(1)) if scores else None,
            "interest_score": int(scores.group(2)) if scores else None,
            "writeup": writeup,
        })
    return matches


# How many reports the page reads at most, newest first. Old reports leave
# reports/ on the retention window (config/retention.yaml, a week as shipped),
# so this only bites where archiving never runs: a copy used purely by hand
# would otherwise load every report it ever wrote.
MAX_REPORTS = 30


def _application_finder(root: Path):
    """A lookup from posting URL to its application, active first, then
    archived, so a match already applied to never offers a second draft."""
    active, archived = _tracker(root), _archived_tracker(root)

    def find(url: str) -> dict:
        if not url:
            return {"application": None, "application_status": None, "application_archived": False}
        app = active.find_by_url(url)
        was_archived = False
        if app is None or app.archived_on:
            app, was_archived = archived.find_by_url(url), True
        return {
            "application": app.uid if app else None,
            "application_status": app.status if app else None,
            "application_archived": bool(app) and was_archived,
        }
    return find


def _read_report(path: Path, find) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "date": path.stem,
        "degraded": "This run was **incomplete**" in text,
        "matches": [
            {
                **{k: v for k, v in match.items() if k != "writeup"},
                "writeup_html": render_markdown(match["writeup"]),
                **find(match["url"]),
            }
            for match in parse_report(text)
        ],
    }


def _dated(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.glob("*.md") if _REPORT_NAME.match(p.name)), reverse=True)


def report(root: Path) -> dict:
    """The matches of every daily report not yet archived, newest report
    first, each match with its write-up as HTML and the application already
    drafted for it, if any."""
    dated = _dated(root / "reports")
    find = _application_finder(root)
    last_run = _last_run(root)
    # The practice report is shown while the practice run is the latest run,
    # above the real reports and apart from them; a real run retires it.
    practice_path = root / "reports" / PRACTICE_REPORT
    practice = (
        _read_report(practice_path, find)
        if last_run and last_run["practice"] and practice_path.is_file()
        else None
    )
    return {
        "reports": [_read_report(path, find) for path in dated[:MAX_REPORTS]],
        "practice": practice,
        "hidden": max(0, len(dated) - MAX_REPORTS),
        "archived": len(_dated(root / "reports" / "archive")),
        "last_run": last_run,
    }


# Archived reports are read this many at a time, newest first. Counting them
# is a directory listing; reading one is parsing its Markdown, so only the
# page asked for is read.
REPORT_ARCHIVE_PAGE = 20


def report_archive(root: Path, offset: int = 0, limit: int = REPORT_ARCHIVE_PAGE) -> dict:
    dated = _dated(root / "reports" / "archive")
    find = _application_finder(root)
    return {
        "total": len(dated),
        "reports": [_read_report(path, find) for path in dated[offset:offset + limit]],
        "next": offset + limit if offset + limit < len(dated) else None,
    }


# The files of an application folder worth showing, in reading order.
_ORDER = ("notes.md", "tailored_cv", "cover_letter", "interview_prep.md", "job_description.md")
_SHOWN = (".md", ".pdf", ".html")
# What may be edited in the page: the documents the user sends or pastes into.
# The documents you send or keep notes in. The job description is the
# posting's own text, kept as the record of what was applied to: it is read
# here, not edited. (A posting that could not be read gets its text through
# paste_jd, not through this.)
_EDITABLE = re.compile(r"^(tailored_cv|cover_letter|notes)[\w-]*\.md$")


def _rank(name: str) -> tuple[int, str]:
    return (next((i for i, prefix in enumerate(_ORDER) if name.startswith(prefix)), len(_ORDER)), name)


def _archived_tracker(root: Path) -> Tracker:
    """applications/archive/applications.json, where archiving moves a row
    (apply/archive.py). Empty when nothing was archived yet."""
    return Tracker(app_archive.archive_tracker_path(root / "applications"))


def _find(root: Path, uid: str) -> tuple[object, Path, bool] | None:
    """An application by its id, active or archived: (application, folder,
    archived). Ids are never reused across the two (tracker.next_uid)."""
    app = _tracker(root).find_by_uid(str(uid))
    if app is not None and not app.archived_on:
        return app, root / "applications" / app.folder, False
    app = _archived_tracker(root).find_by_uid(str(uid))
    if app is not None:
        return app, root / "applications" / app_archive.ARCHIVE_DIRNAME / app.folder, True
    return None


def _folder(root: Path, uid: str, *, writable: bool = False) -> tuple[object, Path]:
    """An application's folder. Archived ones can be read, never written:
    they are the record of what was sent."""
    found = _find(root, uid)
    if found is None:
        raise SetupError(f"No application {uid}.")
    app, folder, archived = found
    if archived and writable:
        raise SetupError(f"Application {app.uid} is archived and can't be changed here.")
    return app, folder


_WHY = re.compile(r"^- Why: (.+)$", re.MULTILINE)


def _waiting_for_jd(folder: Path) -> dict:
    """What the page needs to know about a posting the pipeline could not
    read: why, and the job description pasted so far, if any. `jd_ready` is
    the pipeline's own test of whether that text is enough to draft from."""
    path = folder / "job_description.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    why = _WHY.search(text)
    pasted = text.split(STUB_MARKER, 1)[1].strip() if STUB_MARKER in text else ""
    return {
        "jd_why": why.group(1).strip() if why else "",
        "jd_text": pasted,
        "jd_ready": _read_pasted_jd(path) is not None,
        "jd_min": _MIN_PASTED_CHARS,
    }


def paste_jd(root: Path, uid: str, text: str) -> None:
    """Store a job description pasted in the page where the pipeline looks
    for it: under the marker line of the placeholder's job_description.md,
    which is also where the terminal route has it pasted."""
    app, folder = _folder(root, uid, writable=True)
    if app.status != STATUS_NEEDS_JD:
        raise SetupError(f"Application {app.uid} already has its job description.")
    pasted = text.strip()
    if len(pasted) < _MIN_PASTED_CHARS:
        raise SetupError(
            f"That is too short to be a job description ({len(pasted)} characters, "
            f"and at least {_MIN_PASTED_CHARS} are needed). Paste the whole posting: "
            f"the tasks, the requirements and what is said about the company."
        )
    path = folder / "job_description.md"
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if STUB_MARKER not in current:
        current = _stub_jd_file(app.url, "the job description was pasted by hand")
    head = current.split(STUB_MARKER, 1)[0]
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{head}{STUB_MARKER}\n\n{pasted}\n", encoding="utf-8")


def _describe(app, folder: Path, archived: bool) -> dict:
    files = sorted(
        (p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _SHOWN),
        key=_rank,
    ) if folder.is_dir() else []
    return {
        **(_waiting_for_jd(folder) if app.status == STATUS_NEEDS_JD and not archived else {}),
        "uid": app.uid,
        "folder": app.folder,
        "title": app.title or "(waiting for the job description)",
        "company": app.company,
        "url": app.url,
        "status": app.status,
        "tier": app.tier,
        "prepared_on": app.prepared_on,
        "submitted_on": app.submitted_on,
        "closed_on": app.closed_on,
        "prepped_on": app.prepped_on,
        "archived_on": app.archived_on,
        "archived": archived,
        "skill_score": app.skill_score,
        "interest_score": app.interest_score,
        "files": files,
        "open": not archived and app.status in (STATUS_NEEDS_JD, STATUS_DRAFTED, STATUS_SUBMITTED),
    }


def applications(root: Path) -> list[dict]:
    """Active applications, newest first."""
    out = [
        _describe(app, root / "applications" / app.folder, False)
        for app in _tracker(root).applications
        if not app.archived_on
    ]
    return sorted(out, key=lambda a: a["uid"], reverse=True)


def archived_count(root: Path) -> int:
    return len(_archived_tracker(root).applications)


# Archived applications are listed this many at a time. Only their rows are
# read for the list (one JSON file); a folder is read when it is opened.
ARCHIVE_PAGE = 50


def archived_applications(root: Path, offset: int = 0, limit: int = ARCHIVE_PAGE) -> dict:
    """A page of archived applications, newest first, without their files."""
    apps = sorted(_archived_tracker(root).applications, key=lambda a: a.uid, reverse=True)
    page = apps[offset:offset + limit]
    return {
        "total": len(apps),
        "applications": [
            {k: v for k, v in _describe(app, Path("/nonexistent"), True).items() if k != "files"}
            for app in page
        ],
        "next": offset + limit if offset + limit < len(apps) else None,
    }


def application(root: Path, uid: str) -> dict:
    """One application, active or archived, with its files."""
    found = _find(root, uid)
    if found is None:
        raise SetupError(f"No application {uid}.")
    return _describe(*found)


def file_path(root: Path, uid: str, name: str) -> Path:
    """A file directly inside an application's folder, and nothing else."""
    _app, folder = _folder(root, uid)
    if Path(name).name != name or Path(name).suffix.lower() not in _SHOWN:
        raise SetupError("No such file.")
    path = folder / name
    if not path.is_file():
        raise SetupError("No such file.")
    return path


def read_file(root: Path, uid: str, name: str) -> dict:
    path = file_path(root, uid, name)
    if path.suffix.lower() != ".md":
        raise SetupError("Only the text files can be shown here.")
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "name": name,
        "text": text,
        "html": render_markdown(text),
        "editable": bool(_EDITABLE.match(name)) and not _find(root, uid)[2],
        # A document that is sent as a PDF: saving it is followed by making
        # the PDF again, so the two never disagree. pdf.convertible_files is
        # what `jobradar.apply --pdf` converts and what the linter checks.
        "makes_pdf": path in pdf.convertible_files(path.parent),
        "pdf_problem": _pdf_problem(root, uid, path),
    }


def _pdf_problem(root: Path, uid: str, md_path: Path) -> str | None:
    """Why this document's PDF is not what would be sent, in the linter's own
    words: missing, older than the text, or a print that fell back to HTML.
    None when the PDF is current, or the document has none."""
    if md_path not in pdf.convertible_files(md_path.parent):
        return None
    app = _folder(root, uid)[0]
    # A finding names its file first: the .md, its .pdf, or the .html left
    # by a print that fell back.
    own = {md_path.name, md_path.with_suffix(".pdf").name, md_path.with_suffix(".html").name}
    for finding in lint._check_pdfs(md_path.parent, app):
        if finding.code in lint.PDF_STALENESS_CODES and finding.message.split()[0] in own:
            return finding.message
    return None


def write_file(root: Path, uid: str, name: str, text: str) -> None:
    _folder(root, uid, writable=True)
    path = file_path(root, uid, name)
    if not _EDITABLE.match(name):
        raise SetupError(f"{name} can't be edited here.")
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def overview(root: Path) -> dict:
    """Where the work stands, as counts: what the workflow page hangs on each
    stage of the pipeline."""
    apps = applications(root)
    data = report(root)
    newest = data["reports"][0] if data["reports"] else None
    matches = newest["matches"] if newest else []

    def count(status: str) -> int:
        return sum(a["status"] == status for a in apps)

    return {
        "last_run": data["last_run"],
        "report_date": newest["date"] if newest else None,
        "reports": len(data["reports"]),
        "matches": len(matches),
        "undrafted": sum(1 for m in matches if not m["application"]),
        "needs_jd": count(STATUS_NEEDS_JD),
        "drafted": count(STATUS_DRAFTED),
        "submitted": count(STATUS_SUBMITTED),
        "closed": sum(a["status"] in TERMINAL_STATUSES for a in apps),
    }


_MONTH = re.compile(r"^\d{4}-\d{2}$")
_TABLE_GAP = re.compile(r"(?<=\|)\n\n(?=\|)")


def _close_table_gaps(text: str) -> str:
    """render_rav_month leaves a blank line after each row, so the long rows
    stay apart when the table is read as plain text. In Markdown a blank line
    ends a table, so for display the rows are put back together."""
    return _TABLE_GAP.sub("\n", text)


def rav(root: Path, month: str | None = None, today: date | None = None) -> dict:
    """One month of the RAV proof: its table, and whether it was filed.

    The table is rendered by the same function as `jobradar.apply --rav`, over
    active and archived applications alike, since a month's applications may
    have been archived since. Filing a month is not done here: the page runs
    `--rav-filed`, which also saves the table and runs the archive sweep.
    """
    applications_dir = root / "applications"
    apps = list(_tracker(root).applications)
    archived = app_archive.archive_tracker_path(applications_dir)
    if archived.exists():
        apps += Tracker(archived).applications
    filed = app_archive.load_rav_filed(applications_dir)
    current = (today or date.today()).strftime("%Y-%m")
    with_submissions = sorted({a.submitted_on[:7] for a in apps if a.submitted_on})
    # Months that are over, had submissions, and were not handed in yet.
    unfiled = [m for m in with_submissions if m < current and m not in filed]
    if month is None or not _MONTH.match(month):
        month = unfiled[-1] if unfiled else current
    entry = filed.get(month)
    return {
        "month": month,
        "current": current,
        "submitted": sum(1 for a in apps if (a.submitted_on or "").startswith(month)),
        "filed_on": entry.get("filed_on") if entry else None,
        "report": entry.get("report") if entry else None,
        "filed": sorted(filed),
        "unfiled": unfiled,
        "table_html": render_markdown(_close_table_gaps(render_rav_month(apps, month))),
    }
