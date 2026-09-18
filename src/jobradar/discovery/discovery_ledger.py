"""Persistent record of every company name discovery has already evaluated, so
repeated runs stop re-suggesting and re-probing the same companies.

This is the discovery-side analog of the seen-postings dedup store. Without it,
the web company search keeps spending its (token-capped) output budget
re-listing the same well-known companies we've already configured or dropped,
crowding genuinely-new long-tail names past the cap. Feeding the ledger back
into the search prompt as an exclusion list frees that budget for new names;
skipping re-probes of known results saves HTTP calls.

Neither verdict is permanent. A "dropped" one (no ATS board found) is the most
likely to change — a company may adopt a supported ATS later, or our
slug-guessing may improve — so drops are re-checked once they age past a short
TTL. A "matched" one is far more stable, but it does expire too, on a much
longer TTL: a company can leave an ATS or empty its board, and a match that
never aged out had no way of ever being re-examined (26 companies sat "matched"
against join.com boards carrying no ads at all).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

Ledger = dict[str, dict]


def _norm(name: str) -> str:
    return name.strip().lower()


def load_ledger(path: Path) -> Ledger:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError) as exc:
        logger.warning("Could not read discovery ledger %s (%s); starting fresh", path, exc)
        return {}


def save_ledger(path: Path, ledger: Ledger) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")


def record(ledger: Ledger, name: str, outcome: str, now: datetime, probe_version: str = "") -> None:
    """Upsert a company's evaluation outcome ("matched" | "dropped").

    `probe_version` records which set of ATS connectors the verdict was made
    under, so a "dropped" entry can be re-probed once that set grows (a company
    we couldn't place may be on a newly-added ATS). See _is_active.

    A company's `rejected` boards (see reject_board) survive the upsert: they're
    a durable human judgment, not a probe outcome, so re-recording a fresh
    verdict must not erase them.
    """
    entry = {
        "name": name,
        "outcome": outcome,
        "checked_at": now.isoformat(),
        "probe_version": probe_version,
    }
    existing = ledger.get(_norm(name))
    if existing and existing.get("rejected"):
        entry["rejected"] = existing["rejected"]
    ledger[_norm(name)] = entry


def reject_board(ledger: Ledger, name: str, ats: str, slug: str) -> None:
    """Record that (ats, slug) is NOT this company's board — a slug-guess that
    collides with an unrelated, similarly-named company (e.g. "Hamilton"
    resolving to Hamilton Insurance Group's board). Persisted so probe_company
    can be told to skip it forever; a live collision board otherwise re-matches
    on every probe, which no TTL or probe-version bump can fix.

    Creates a minimal entry if the company isn't in the ledger yet. The caller
    is expected to re-probe (with the rejection applied) and record the real
    resulting outcome.
    """
    entry = ledger.setdefault(_norm(name), {"name": name})
    entry["name"] = name  # refresh display name if the entry was bare
    rejected = entry.setdefault("rejected", [])
    pair = [ats, slug]
    if pair not in rejected:
        rejected.append(pair)


def rejected_boards(ledger: Ledger, name: str) -> set[tuple[str, str]]:
    """The (ats, slug) pairs a human has flagged as wrong-company collisions for
    this name — for feeding to probe_company's `skip`. Empty set if none."""
    entry = ledger.get(_norm(name)) or {}
    return {(ats, slug) for ats, slug in entry.get("rejected", [])}


def evaluated_names(ledger: Ledger) -> list[str]:
    """Display names of every company ever evaluated."""
    return [entry.get("name", key) for key, entry in ledger.items()]


# How long a "matched" verdict stays trusted. Much longer than the drop TTL —
# a live board rarely moves, and re-probing one usually yields nothing new — but
# not forever, so a board that dies can eventually be re-examined.
DEFAULT_MATCH_TTL_DAYS = 90


def _within_ttl(entry: dict, now: datetime, ttl_days: int) -> bool:
    try:
        checked_at = datetime.fromisoformat(entry["checked_at"])
    except (KeyError, ValueError):
        return False  # malformed entry — treat as due for re-check
    if checked_at.tzinfo is None:
        return False  # naive/legacy timestamp — don't trust it; re-check
    return checked_at >= now - timedelta(days=ttl_days)


def _is_active(
    entry: dict,
    now: datetime,
    ttl_days: int,
    probe_version: str,
    match_ttl_days: int = DEFAULT_MATCH_TTL_DAYS,
) -> bool:
    """A ledger entry is 'active' (still authoritative) if it's a still-fresh
    match, or a still-fresh drop made under the CURRENT probe version. A stale
    verdict of either kind — or a drop made under an older ATS set — is NOT
    active: it's due for a re-check.
    """
    if entry.get("outcome") == "matched":
        # probe_version is deliberately not consulted here: adding a connector
        # doesn't invalidate a board we already found. Only age retires a match.
        return _within_ttl(entry, now, match_ttl_days)
    if entry.get("probe_version") != probe_version:
        return False  # dropped under a different ATS set — re-probe against the new one
    return _within_ttl(entry, now, ttl_days)


def active_names(
    ledger: Ledger,
    now: datetime,
    ttl_days: int,
    probe_version: str = "",
    match_ttl_days: int = DEFAULT_MATCH_TTL_DAYS,
) -> list[str]:
    """Display names whose verdict still stands — for the search-prompt exclusion.

    Deliberately excludes stale verdicts (and drops from an older ATS set) so
    they can be re-suggested and re-probed; using evaluated_names() here would
    suppress them forever.
    """
    return [
        e.get("name", k)
        for k, e in ledger.items()
        if _is_active(e, now, ttl_days, probe_version, match_ttl_days)
    ]


def should_skip_probe(
    ledger: Ledger,
    name: str,
    now: datetime,
    ttl_days: int,
    probe_version: str = "",
    match_ttl_days: int = DEFAULT_MATCH_TTL_DAYS,
) -> bool:
    """True if we already know this company's outcome and needn't probe again:
    a still-fresh match, or a still-fresh drop made under the current ATS set.
    """
    entry = ledger.get(_norm(name))
    if not entry:
        return False
    return _is_active(entry, now, ttl_days, probe_version, match_ttl_days)
