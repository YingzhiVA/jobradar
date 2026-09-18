"""Move outdated pipeline output into committed archive/ subdirectories.

Two targets, both handled by ``python -m jobradar.archive`` (run daily by CI
after the search pipeline; safe to run locally too):

- reports/YYYY-MM-DD.md older than ``reports_days``  -> reports/archive/
- data/seen_postings.json entries whose first_seen_at is older than
  ``seen_days``                                      -> data/archive/seen_postings.json

Application archiving lives in jobradar.apply.archive and runs as its own CI
step (``python -m jobradar.apply --archive``) right after this one. It is kept
separate because it sweeps a different kind of state — the tracker — but it is
just as schedulable: the outcomes and RAV filings it keys off are recorded by
hand, yet the policy that acts on them is a pure function of committed state.

Archived data stays in git: the scheduled CI runner starts from a fresh
checkout, so anything moved out of the active files must be committed or it
is lost. The archive is a record only — nothing reads it back: the dedup
store deliberately does not consult the seen archive (a posting still live
after ``seen_days`` may resurface once), and findings reuse only globs the
active reports/ directory.

Retention windows come from config/retention.yaml; missing file or keys fall
back to the defaults on the Retention dataclass.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# This module sits directly in src/jobradar/, one level above the sub-packages
# that use parents[3].
ROOT = Path(__file__).resolve().parents[2]

_DATED_REPORT = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Retention:
    reports_days: int = 90
    seen_days: int = 365
    ghosted_days: int = 60
    stale_draft_days: int = 60


def load_retention(root: Path) -> Retention:
    """config/retention.yaml with per-key fallback to the dataclass defaults."""
    path = root / "config" / "retention.yaml"
    if not path.exists():
        return Retention()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = {f.name for f in fields(Retention)}
    return Retention(**{k: int(v) for k, v in data.items() if k in known})


def archive_reports(
    reports_dir: Path, today: date, keep_days: int, *, dry_run: bool = False
) -> list[Path]:
    """Move dated daily reports older than the window to reports/archive/.

    Only files named YYYY-MM-DD.md move; company_health.md, latest.json,
    runs.jsonl and .gitkeep never match. Returns the moved (or would-move)
    source paths.
    """
    cutoff = today - timedelta(days=keep_days)
    moved: list[Path] = []
    for path in sorted(reports_dir.glob("*.md")):
        if not _DATED_REPORT.fullmatch(path.stem):
            continue
        try:
            report_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if report_date >= cutoff:
            continue
        moved.append(path)
        if dry_run:
            continue
        archive_dir = reports_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        # os.replace so a re-run after a partial crash overwrites the
        # identical copy instead of failing.
        os.replace(path, archive_dir / path.name)
    return moved


def archive_seen(
    data_dir: Path, now: datetime, keep_days: int, *, dry_run: bool = False
) -> int:
    """Split old entries out of seen_postings.json into data/archive/.

    Unlike SeenStore, a corrupt file raises instead of "starting fresh":
    silently wiping the dedup store here would be committed by CI as if it
    were real state. Merge into the archive is insert-or-ignore (an existing
    archive record wins), and the archive is written before the active file
    so a crash between the writes only leaves a duplicate that the next run
    re-archives. Returns the number of archived entries.
    """
    active_path = data_dir / "seen_postings.json"
    if not active_path.exists():
        return 0
    seen: dict[str, dict] = json.loads(active_path.read_text(encoding="utf-8"))

    cutoff = now - timedelta(days=keep_days)
    keep: dict[str, dict] = {}
    expire: dict[str, dict] = {}
    for key, entry in seen.items():
        try:
            first_seen = datetime.fromisoformat(entry["first_seen_at"])
        except (KeyError, TypeError, ValueError):
            keep[key] = entry  # unparseable timestamp: leave it alone
            continue
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        (expire if first_seen < cutoff else keep)[key] = entry

    if not expire or dry_run:
        return len(expire)

    archive_path = data_dir / "archive" / "seen_postings.json"
    archived: dict[str, dict] = {}
    if archive_path.exists():
        archived = json.loads(archive_path.read_text(encoding="utf-8"))
    for key, entry in expire.items():
        archived.setdefault(key, entry)

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    for path, entries in ((archive_path, archived), (active_path, keep)):
        # Same ordering as SeenStore._save: newest first, stable diffs.
        ordered = dict(
            sorted(entries.items(), key=lambda kv: kv[1].get("first_seen_at", ""), reverse=True)
        )
        path.write_text(json.dumps(ordered, indent=2), encoding="utf-8")
    return len(expire)


def run(root: Path, *, today: date | None = None, dry_run: bool = False) -> None:
    today = today or datetime.now(timezone.utc).date()
    retention = load_retention(root)
    verb = "Would archive" if dry_run else "Archived"

    reports = archive_reports(
        root / "reports", today, retention.reports_days, dry_run=dry_run
    )
    if reports:
        logger.info(
            "%s %d report(s) older than %d days: %s",
            verb, len(reports), retention.reports_days,
            ", ".join(p.name for p in reports),
        )
    else:
        logger.info("No reports older than %d days.", retention.reports_days)

    now = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    n_seen = archive_seen(root / "data", now, retention.seen_days, dry_run=dry_run)
    if n_seen:
        logger.info(
            "%s %d seen-store entr(ies) older than %d days.",
            verb, n_seen, retention.seen_days,
        )
    else:
        logger.info("No seen-store entries older than %d days.", retention.seen_days)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would move without moving it"
    )
    args = parser.parse_args(argv)
    run(ROOT, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
