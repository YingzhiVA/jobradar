"""Archive closed, ghosted, and stale applications to applications/archive/.

An application leaves the active tracker when it can no longer matter:

- "outcome":     a terminal status (rejected/withdrawn/offer) was recorded;
- "no-response": submitted long enough ago (retention.ghosted_days) with no
                 outcome — most companies simply never answer;
- "stale-draft": drafted or needs_jd, never submitted, older than
                 retention.stale_draft_days (covers *_pending stub folders,
                 and stops the pipeline's per-run needs_jd retry).

RAV gate (only when ``rav.enabled`` is true in config/search.yaml): a
*submitted* application is proof of a job-search effort until the monthly RAV
report covering its submission month has been filed. Filing is recorded
explicitly (``--rav-filed YYYY-MM``) in applications/rav_filed.json; until then
the application stays active regardless of status or age. With RAV off, the
two age-based reasons above decide on their own.

Archived entries move to applications/archive/applications.json (same schema,
read/written via a second Tracker) with ``archived_on`` set, their folders
move to applications/archive/<folder>/, and any queue.txt line for their URL
is removed. The archive stays committed to git so material can be reused for
future applications.

The policy sweep runs daily in CI (``python -m jobradar.apply --archive``, see
.github/workflows/daily.yml) as well as on demand and implicitly after
``--rav-filed``. Every input it reads is committed state, so a scheduled run
sees exactly what a local one would — and the two age-based reasons below
expire on their actual due date rather than on the next manual sweep.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from ..archive import Retention
from .queue import prune_queue
from .tracker import (
    STATUS_DRAFTED,
    STATUS_NEEDS_JD,
    STATUS_SUBMITTED,
    TERMINAL_STATUSES,
    Application,
    Tracker,
)

logger = logging.getLogger(__name__)

ARCHIVE_DIRNAME = "archive"
RAV_FILED_FILENAME = "rav_filed.json"

REASON_OUTCOME = "outcome"
REASON_NO_RESPONSE = "no-response"
REASON_STALE_DRAFT = "stale-draft"


def archive_tracker_path(applications_dir: Path) -> Path:
    return applications_dir / ARCHIVE_DIRNAME / "applications.json"


def load_rav_filed(applications_dir: Path) -> dict[str, dict]:
    """The filed-months ledger: {"YYYY-MM": {"filed_on": ..., "report": ...}}."""
    path = applications_dir / RAV_FILED_FILENAME
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("filed", {})


def mark_rav_filed(
    applications_dir: Path, month: str, report_relpath: str, today: date
) -> None:
    """Record that the RAV report for ``month`` was filed. Re-running keeps the
    original filed_on date (the report file just gets re-rendered)."""
    filed = load_rav_filed(applications_dir)
    entry = filed.get(month, {})
    filed[month] = {
        "filed_on": entry.get("filed_on", today.isoformat()),
        "report": report_relpath,
    }
    path = applications_dir / RAV_FILED_FILENAME
    payload = {"filed": dict(sorted(filed.items()))}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _days_since(iso_day: str, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(iso_day)).days
    except ValueError:
        return None


def archive_reason(
    app: Application,
    rav_filed: dict[str, dict],
    today: date,
    retention: Retention,
    *,
    rav_enabled: bool = True,
) -> str | None:
    """Why this application should be archived now, or None to keep it active."""
    if app.submitted_on:
        if rav_enabled and app.submitted_on[:7] not in rav_filed:
            return None  # still needed as proof for an unfiled RAV month
        if app.status in TERMINAL_STATUSES:
            return REASON_OUTCOME
        if app.status == STATUS_SUBMITTED:
            age = _days_since(app.submitted_on, today)
            if age is not None and age >= retention.ghosted_days:
                return REASON_NO_RESPONSE
        return None
    if app.status in TERMINAL_STATUSES:
        # Withdrawn/rejected before submission: never proof of anything, archive now.
        return REASON_OUTCOME
    if app.status in (STATUS_NEEDS_JD, STATUS_DRAFTED) and app.prepared_on:
        age = _days_since(app.prepared_on, today)
        if age is not None and age >= retention.stale_draft_days:
            return REASON_STALE_DRAFT
    return None


def sweep(
    tracker: Tracker,
    applications_dir: Path,
    today: date,
    retention: Retention,
    *,
    rav_enabled: bool = True,
) -> list[tuple[Application, str]]:
    """Archive every active application the policy says is done. Returns
    (application, reason) pairs for what was actually archived."""
    rav_filed = load_rav_filed(applications_dir) if rav_enabled else {}
    candidates = [
        (app, reason)
        for app in list(tracker.applications)
        if (
            reason := archive_reason(
                app, rav_filed, today, retention, rav_enabled=rav_enabled
            )
        )
        is not None
    ]
    if not candidates:
        return []
    archived = archive_applications(
        tracker, applications_dir, [app for app, _ in candidates], today
    )
    done = {a.url for a in archived}
    return [(app, reason) for app, reason in candidates if app.url in done]


def archive_applications(
    tracker: Tracker, applications_dir: Path, apps: list[Application], today: date
) -> list[Application]:
    """Move the given applications (folder + tracker row) into the archive.

    Self-healing per app: a folder already moved by a crashed earlier run is
    fine (just move the row); a row whose folder is gone entirely is archived
    anyway (matches lint's dangling-entry semantics). Only a true collision —
    source and destination folders both exist — is refused, never clobbered.
    Saves the archive tracker before the active one, so a crash in between
    leaves a duplicate row that the next run self-heals.
    """
    archive_dir = applications_dir / ARCHIVE_DIRNAME
    archive = Tracker(archive_tracker_path(applications_dir))
    archived: list[Application] = []

    for app in apps:
        src = applications_dir / app.folder
        dst = archive_dir / app.folder
        if src.is_dir() and dst.exists():
            logger.error(
                "Not archiving %s: applications/archive/%s already exists; "
                "resolve the collision manually",
                app.folder, app.folder,
            )
            continue
        if src.is_dir():
            archive_dir.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        elif dst.is_dir():
            logger.info("Folder already archived, moving tracker row: %s", app.folder)
        else:
            logger.warning(
                "No folder on disk for %s; archiving the tracker row anyway", app.folder
            )
        app.archived_on = today.isoformat()
        archive.upsert(app)
        archived.append(app)

    if archived:
        archive.save()
        for app in archived:
            if app in tracker.applications:
                tracker.applications.remove(app)
        tracker.save()
        # An archived application is dealt with; its queue line (if the user
        # left one) would otherwise sit there as stale to-do noise.
        pruned = prune_queue(
            applications_dir / "queue.txt", {app.url for app in archived}
        )
        if pruned:
            logger.info("Removed %d archived URL(s) from queue.txt", pruned)
    return archived
