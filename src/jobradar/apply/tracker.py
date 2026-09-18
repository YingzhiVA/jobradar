"""Track prepared/submitted applications in applications/applications.json.

The tracker is the pipeline's dedup store (a URL with a drafted/submitted
entry is skipped on later runs), the pointer from URL to deliverables
folder, and the data behind the monthly proof-of-applications table that
unemployment insurance asks for (--rav).

It is git-tracked on purpose: together with the per-application folders it
makes the whole application state portable across machines.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .util import normalize_url

STATUS_NEEDS_JD = "needs_jd"  # folder created, waiting for a manually pasted JD
STATUS_DRAFTED = "drafted"  # documents generated, ready for review/submission
STATUS_SUBMITTED = "submitted"
# Terminal outcomes, recorded with --mark-rejected/--mark-withdrawn/--mark-offer.
STATUS_REJECTED = "rejected"
STATUS_WITHDRAWN = "withdrawn"
STATUS_OFFER = "offer"
TERMINAL_STATUSES = frozenset({STATUS_REJECTED, STATUS_WITHDRAWN, STATUS_OFFER})


@dataclass
class Application:
    url: str  # normalized
    folder: str  # folder name under applications/
    # Short sequential handle ("001", "002", ...), assigned once at draft time
    # and never reused. It prefixes the folder name so it can be read straight
    # off the file tree, and every CLI KEY accepts it in place of the folder.
    uid: str = ""
    title: str = ""
    company: str = ""
    # Postal code of the work location, for the RAV form. Approximate is fine
    # (any postcode of the right town); empty when it could not be determined.
    postcode: str = ""
    prepared_on: str = ""  # YYYY-MM-DD
    submitted_on: str | None = None
    tier: str = "quick"  # "quick" | "full"
    base_cv: str = ""
    skill_score: int | None = None
    interest_score: int | None = None
    cover_letter: bool = False
    # ISO 639-1 code of the language a translated CV was produced in (e.g.
    # "de" for a German posting), or None for English postings. The English
    # tailored_cv.md is always written; this flags the extra tailored_cv_<x>.md.
    cv_translation_language: str | None = None
    # Where the fit analysis came from: "report:YYYY-MM-DD" or "fresh".
    analysis_source: str = ""
    status: str = STATUS_DRAFTED
    closed_on: str | None = None  # date a terminal outcome was recorded
    archived_on: str | None = None  # set when the entry moves to applications/archive/


class Tracker:
    def __init__(self, path: Path):
        self.path = path
        self.applications: list[Application] = []
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in Application.__dataclass_fields__.values()}
            self.applications = [
                Application(**{k: v for k, v in entry.items() if k in known})
                for entry in data.get("applications", [])
            ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"applications": [asdict(app) for app in self.applications]}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def find_by_url(self, url: str) -> Application | None:
        url = normalize_url(url)
        return next((a for a in self.applications if a.url == url), None)

    def find_by_uid(self, uid: str) -> Application | None:
        """Look up by short id, ignoring zero-padding ("42" == "042")."""
        if not uid.isdigit():
            return None
        n = int(uid)
        return next(
            (a for a in self.applications if a.uid.isdigit() and int(a.uid) == n), None
        )

    def find(self, key: str) -> Application | None:
        """Look up by short id, exact folder name, or URL. All three are
        accepted wherever the CLI takes a KEY; the id is what notes.md and the
        folder name advertise, the other two are kept working for old habits."""
        key = key.strip().rstrip("/")
        folder_key = key.split("/")[-1]  # accept "applications/<folder>" too
        by_uid = self.find_by_uid(folder_key)
        if by_uid is not None:
            return by_uid
        for app in self.applications:
            if app.folder == folder_key:
                return app
        return self.find_by_url(key)

    def upsert(self, app: Application) -> None:
        existing = self.find_by_url(app.url)
        if existing is not None:
            self.applications.remove(existing)
        self.applications.append(app)

    def mark_submitted(self, key: str, on: date | None = None) -> Application | None:
        app = self.find(key)
        if app is None:
            return None
        app.status = STATUS_SUBMITTED
        app.submitted_on = (on or date.today()).isoformat()
        return app

    def mark_outcome(self, key: str, status: str, on: date | None = None) -> Application | None:
        """Record a terminal outcome (rejected/withdrawn/offer) for --mark-*."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"not a terminal status: {status!r}")
        app = self.find(key)
        if app is None:
            return None
        app.status = status
        app.closed_on = (on or date.today()).isoformat()
        return app


UID_WIDTH = 3  # zero-padded, so uid-prefixed folder names sort lexically


def next_uid(*application_groups: list[Application]) -> str:
    """The next free short id across every group passed in.

    Pass the archived applications alongside the active ones: ids are never
    reused, so an archived 007 keeps 007 forever and the counter walks past it.
    """
    used = [
        int(app.uid)
        for group in application_groups
        for app in group
        if app.uid.isdigit()
    ]
    return f"{max(used, default=0) + 1:0{UID_WIDTH}d}"


def _swiss_date(iso: str | None) -> str:
    """YYYY-MM-DD (how dates are stored) to dd.mm.yyyy (how the RAV form and
    every other Swiss form wants them)."""
    try:
        return date.fromisoformat(iso or "").strftime("%d.%m.%Y")
    except ValueError:
        return iso or "?"


# How each status reads in the form's "Ergebnis" column. A submission still
# waiting for an answer is a perfectly good outcome to report ("hängig").
_RAV_OUTCOMES = {
    STATUS_SUBMITTED: "hängig",
    STATUS_REJECTED: "Absage",
    STATUS_WITHDRAWN: "zurückgezogen",
    STATUS_OFFER: "Angebot",
}


def _rav_label(app: Application) -> str:
    """The status as the form words it. Anything not terminal reads as pending:
    a submission is proof of effort whatever state the tracker left it in."""
    return _RAV_OUTCOMES.get(app.status, _RAV_OUTCOMES[STATUS_SUBMITTED])


def _rav_outcome(app: Application) -> str:
    """The label for the table cell, with the date a closed one closed on."""
    label = _rav_label(app)
    if app.status in TERMINAL_STATUSES and app.closed_on:
        return f"{label} ({_swiss_date(app.closed_on)})"
    return label


def render_rav_month(applications: list[Application], month: str) -> str:
    """Markdown table of applications for one month (YYYY-MM), in the shape of
    the RAV "Nachweis der persönlichen Arbeitsbemühungen" form: copy the rows
    into the official form (or attach this table where the RAV accepts one).

    An application counts in the month it was submitted; drafted-but-not-yet-
    submitted ones are listed separately as a reminder, not as proof.
    """
    submitted = sorted(
        (a for a in applications if a.submitted_on and a.submitted_on.startswith(month)),
        key=lambda a: a.submitted_on or "",
    )
    pending = [
        a
        for a in applications
        # An archived draft was given up on, not forgotten - no reminder.
        if a.status == STATUS_DRAFTED and not a.archived_on and a.prepared_on.startswith(month)
    ]

    counts = Counter(_rav_label(a) for a in submitted)
    breakdown = ", ".join(
        f"{label} {counts[label]}" for label in _RAV_OUTCOMES.values() if counts[label]
    )
    total = f"Submitted applications: {len(submitted)}"
    if breakdown:
        total += f" ({breakdown})"

    lines = [
        f"# Arbeitsbemühungen {month}",
        "",
        total,
        "",
        "| Datum | Unternehmen | PLZ | Stelle | Bewerbungsart | Ergebnis | Link |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for a in submitted:
        lines.append(
            f"| {_swiss_date(a.submitted_on)} | {a.company or '?'} "
            f"| {a.postcode or '?'} | {a.title or '?'} | online "
            f"| {_rav_outcome(a)} | {a.url} |"
        )
        # One blank line per row: the posting URL makes a row long enough that
        # rows run into each other when the table is read as text.
        lines.append("")
    if not submitted:
        lines.append("")
    if pending:
        lines += [
            f"Drafted this month but not yet marked submitted ({len(pending)}) - "
            "submit them or mark them with --mark-submitted:",
            "",
        ]
        for a in pending:
            lines += [f"- {a.uid or a.folder} ({a.title} at {a.company})", ""]
    return "\n".join(lines)
