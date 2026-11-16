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
from ..apply.tracker import render_rav_month
from ..apply.tracker import (
    STATUS_DRAFTED,
    STATUS_NEEDS_JD,
    STATUS_SUBMITTED,
    TERMINAL_STATUSES,
    Tracker,
)
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


def _last_run(root: Path) -> dict | None:
    """The funnel of the most recent run, from the last line of the run log."""
    path = root / "reports" / "runs.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            # One record carries every scored posting; the last megabytes are
            # enough to hold the final line without reading the whole log.
            handle.seek(max(0, size - 4_000_000))
            tail = handle.read().decode("utf-8", errors="replace")
        record = json.loads(tail.rstrip().rsplit("\n", 1)[-1])
    except (OSError, ValueError, IndexError):
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
        # A practice run (--dry-run) reads sample postings from one source
        # called "fixture"; its report must not be taken for real matches.
        "practice": [s.get("name") for s in record.get("sources") or []] == ["fixture"],
    }


_REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_MATCH_HEADING = re.compile(r"^## \[(BEST|OKAY)\] (.+)$", re.MULTILINE)
_FIELD = re.compile(r"^- (Link|Location|Best-fit CV): (.*)$", re.MULTILINE)
_SCORES = re.compile(r"^- Skill score: (\d+)/100, Interest score: (\d+)/100$", re.MULTILINE)


def parse_report(text: str) -> list[dict]:
    """The matches of a daily report, read back from the Markdown that
    search/report.py's render_markdown wrote. The dated report is what every
    run leaves behind, a cloud run included; latest.json stays on the machine
    that made it."""
    parts = _MATCH_HEADING.split(text)
    matches = []
    for tier, heading, body in zip(parts[1::3], parts[2::3], parts[3::3]):
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


def report(root: Path) -> dict:
    """The matches of every daily report not yet archived, newest report
    first, each match with its write-up as HTML and the application already
    drafted for it, if any."""
    reports_dir = root / "reports"
    dated = sorted(
        (p for p in reports_dir.glob("*.md") if _REPORT_NAME.match(p.name)), reverse=True
    ) if reports_dir.is_dir() else []
    tracker = _tracker(root)
    reports = []
    for path in dated[:MAX_REPORTS]:
        text = path.read_text(encoding="utf-8", errors="replace")
        matches = []
        for match in parse_report(text):
            app = tracker.find_by_url(match["url"]) if match["url"] else None
            matches.append({
                **{k: v for k, v in match.items() if k != "writeup"},
                "writeup_html": render_markdown(match["writeup"]),
                "application": app.uid if app else None,
                "application_status": app.status if app else None,
            })
        reports.append({
            "date": path.stem,
            "degraded": "This run was **incomplete**" in text,
            "matches": matches,
        })
    return {
        "reports": reports,
        "hidden": max(0, len(dated) - MAX_REPORTS),
        "last_run": _last_run(root),
    }


# The files of an application folder worth showing, in reading order.
_ORDER = ("notes.md", "tailored_cv", "cover_letter", "interview_prep.md", "job_description.md")
_SHOWN = (".md", ".pdf", ".html")
# What may be edited in the page: the documents the user sends or pastes into.
_EDITABLE = re.compile(r"^(tailored_cv|cover_letter|notes|job_description)[\w-]*\.md$")


def _rank(name: str) -> tuple[int, str]:
    return (next((i for i, prefix in enumerate(_ORDER) if name.startswith(prefix)), len(_ORDER)), name)


def _folder(root: Path, uid: str) -> tuple[object, Path]:
    app = _tracker(root).find_by_uid(str(uid))
    if app is None or app.archived_on:
        raise SetupError(f"No active application {uid}.")
    return app, root / "applications" / app.folder


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
    app, folder = _folder(root, uid)
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


def applications(root: Path) -> list[dict]:
    """Active applications, newest first."""
    out = []
    for app in _tracker(root).applications:
        if app.archived_on:
            continue
        folder = root / "applications" / app.folder
        files = sorted(
            (p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _SHOWN),
            key=_rank,
        ) if folder.is_dir() else []
        out.append({
            **(_waiting_for_jd(folder) if app.status == STATUS_NEEDS_JD else {}),
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
            "skill_score": app.skill_score,
            "interest_score": app.interest_score,
            "files": files,
            "open": app.status in (STATUS_NEEDS_JD, STATUS_DRAFTED, STATUS_SUBMITTED),
        })
    return sorted(out, key=lambda a: a["uid"], reverse=True)


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
        "editable": bool(_EDITABLE.match(name)),
    }


def write_file(root: Path, uid: str, name: str, text: str) -> None:
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
