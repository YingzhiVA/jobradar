"""Tracks which postings have already been considered, so the same posting
is never re-scored or re-notified on a later run.

JSON-backed (not SQLite) so the file diffs cleanly and can be committed back as
durable state by the scheduled CI run — the cloud runner starts from a fresh
git checkout each time, and a binary DB would bloat the repo with a new blob
every run. Keyed by posting id; first-seen records are preserved (a later run
never overwrites an earlier outcome), matching the old INSERT-OR-IGNORE store.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..models import Posting

logger = logging.getLogger(__name__)


class SeenStore:
    def __init__(self, path: Path):
        self.path = path
        self._seen: dict[str, dict] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._seen = data
            except (ValueError, OSError) as exc:
                logger.warning("Could not read seen-store %s (%s); starting fresh", path, exc)

    def filter_unseen(self, postings: list[Posting]) -> list[Posting]:
        return [p for p in postings if p.id not in self._seen]

    def mark_seen(self, postings: list[Posting], outcome_by_id: dict[str, str] | None = None) -> None:
        outcome_by_id = outcome_by_id or {}
        now = datetime.now(timezone.utc).isoformat()
        for p in postings:
            if p.id in self._seen:
                continue  # preserve the original first-seen record
            self._seen[p.id] = {
                "url": p.url,
                "title": p.title,
                "company": p.company,
                "first_seen_at": now,
                "outcome": outcome_by_id.get(p.id, "considered"),
            }
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Newest first_seen_at on top, so newly added entries always land at
        # the start of the file. Stable per-run (existing entries' first_seen_at
        # never changes), so this doesn't churn diffs beyond the new lines.
        ordered = dict(
            sorted(self._seen.items(), key=lambda kv: kv[1]["first_seen_at"], reverse=True)
        )
        self.path.write_text(json.dumps(ordered, indent=2), encoding="utf-8")

    def close(self) -> None:
        pass

    def __enter__(self) -> "SeenStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
