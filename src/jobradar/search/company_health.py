"""Detect configured company boards that have gone quietly dry.

A board that answers 200 with an empty posting list fails silently — no error,
no skip, nothing in the report. The daily search run records each company's raw
posting count in its `company_pages` source meta (which `observability.py`
persists into `reports/runs.jsonl`), so the run history is a per-company
liveness signal over time. This module reads that history and flags boards that
have produced nothing for a sustained stretch.

Sustained is the point: a single 0 is normal (a small company simply isn't
hiring this week). Only a board dry for many days is suspect — that's the one
thing that separates "quiet" from "abandoned", and it only exists across
multiple runs, which is why this reads history rather than a single snapshot.

Pure functions (they take already-parsed run records); `load_runs` is the one
I/O boundary. The monthly discovery run consumes these; see discovery/discover.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_SOURCE_NAME = "company_pages"


def load_runs(path: Path) -> list[dict]:
    """Parse reports/runs.jsonl into run records (newest last). Missing file or
    unreadable lines yield what's parseable rather than raising — health is
    best-effort and must never break the discovery run."""
    if not path.exists():
        return []
    runs: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except ValueError:
            continue
    return runs


def _company_counts(run: dict) -> dict[str, int]:
    """The per-company raw counts a run recorded, or {} for runs from before the
    feature shipped (their company_pages source has no company_counts meta)."""
    for source in run.get("sources", []):
        if source.get("name") == _SOURCE_NAME:
            counts = (source.get("meta") or {}).get("company_counts")
            return counts if isinstance(counts, dict) else {}
    return {}


def _run_date(run: dict) -> date | None:
    try:
        return date.fromisoformat(run["date"])
    except (KeyError, ValueError, TypeError):
        return None


def last_active_dates(runs: list[dict]) -> dict[str, date]:
    """Most recent run date each company returned >=1 posting."""
    out: dict[str, date] = {}
    for run in runs:
        d = _run_date(run)
        if d is None:
            continue
        for name, count in _company_counts(run).items():
            if count and (name not in out or d > out[name]):
                out[name] = d
    return out


def first_seen_dates(runs: list[dict]) -> dict[str, date]:
    """Earliest run date each company appears at all (regardless of count), so a
    newly-added company's dry clock starts when it was added — not at epoch —
    and it isn't flagged until it has genuinely been dry for the threshold."""
    out: dict[str, date] = {}
    for run in runs:
        d = _run_date(run)
        if d is None:
            continue
        for name in _company_counts(run):
            if name not in out or d < out[name]:
                out[name] = d
    return out


def latest_counts(runs: list[dict]) -> dict[str, int]:
    """Each company's count in the most recent run that recorded it."""
    out: dict[str, int] = {}
    latest: dict[str, date] = {}
    for run in runs:
        d = _run_date(run)
        if d is None:
            continue
        for name, count in _company_counts(run).items():
            if name not in latest or d > latest[name]:
                latest[name] = d
                out[name] = count
    return out


@dataclass(frozen=True)
class StaleFinding:
    name: str
    dry_days: int  # days since the board last had an ad (or since first seen, if never)
    last_active: date | None  # None if the board has never had an ad in our history
    last_count: int  # count in the most recent run (0 for a currently-dry board)


def stale_companies(
    runs: list[dict],
    company_names: list[str],
    today: date,
    stale_days: int,
) -> list[StaleFinding]:
    """Configured companies whose board has been dry for >= stale_days.

    Anchored to the last date the board had an ad, or — for a board never seen
    with an ad — the date it first appeared (its add date), so a fresh entry
    gets a full grace window before it can be flagged. A company with no history
    at all (feature just shipped, or added since the last run) is skipped: no
    data means no verdict. Returned most-stale first.
    """
    active = last_active_dates(runs)
    first = first_seen_dates(runs)
    counts = latest_counts(runs)
    findings: list[StaleFinding] = []
    for name in company_names:
        anchor = active.get(name) or first.get(name)
        if anchor is None:
            continue  # never observed yet — nothing to judge
        dry_days = (today - anchor).days
        if dry_days >= stale_days:
            findings.append(
                StaleFinding(
                    name=name,
                    dry_days=dry_days,
                    last_active=active.get(name),
                    last_count=counts.get(name, 0),
                )
            )
    findings.sort(key=lambda f: f.dry_days, reverse=True)
    return findings
