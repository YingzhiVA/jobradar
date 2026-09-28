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
from .ranking import NEAR_FLOOR_ATTEMPTS

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
        """Postings this run should consider: never seen, or seen but still
        holding a retry budget (see mark_seen's `retryable_ids`).
        """
        return [
            p for p in postings
            if p.id not in self._seen or self._seen[p.id].get("retries_left", 0) > 0
        ]

    def settled_urls(self) -> frozenset[str]:
        """URLs of postings already decided, whose descriptions need not be fetched again.

        A board connector that pays one HTTP request per posting for its
        description (SuccessFactors, Workday, …) otherwise re-downloads every
        posting it lists on every run, only for filter_unseen to drop nearly all
        of them straight after. EY alone re-fetched ~150 pages a run for ~171
        postings already in this store. Handing the connectors this set before
        fetching lets them return a known posting from its listing row alone.

        A posting still holding a near-floor retry budget is left out: it is
        coming back to be scored again, and scoring needs its description.
        """
        return frozenset(
            record["url"]
            for record in self._seen.values()
            if record.get("url") and not record.get("retries_left")
        )

    def mark_seen(
        self,
        postings: list[Posting],
        outcome_by_id: dict[str, str] | None = None,
        retryable_ids: set[str] | None = None,
    ) -> None:
        """Record postings as considered.

        `retryable_ids` are the ones that scored just under the skill floor
        (ranking.near_floor_ids). They are recorded like any other, but carry a
        budget of further attempts: filter_unseen keeps returning them until it
        runs out, so a posting is only written off after the scorer has given a
        below-floor answer several times rather than once.

        Whether a posting gets a budget is decided the first time it is
        recorded; after that every run that marks it again simply spends one,
        wherever it scored. A later run scoring it FURTHER below the floor
        settles nothing in the direction that matters — the question is whether
        it ever clears, and a posting that does clear is eligible, so it is not
        passed here at all and keeps what it has left.
        """
        outcome_by_id = outcome_by_id or {}
        retryable_ids = retryable_ids or set()
        now = datetime.now(timezone.utc).isoformat()
        for p in postings:
            existing = self._seen.get(p.id)
            if existing is None:
                record = {
                    "url": p.url,
                    "title": p.title,
                    "company": p.company,
                    "first_seen_at": now,
                    "outcome": outcome_by_id.get(p.id, "considered"),
                }
                if p.id in retryable_ids:
                    record["retries_left"] = NEAR_FLOOR_ATTEMPTS - 1
                self._seen[p.id] = record
                continue
            # A record already exists. Its first-seen fields are preserved, as
            # they always were; the retry budget is the one thing a later run
            # may change.
            if not existing.get("retries_left"):
                continue
            existing["retries_left"] -= 1
            if not existing["retries_left"]:
                logger.info(
                    "Near-floor posting settled after %d attempts: %s",
                    NEAR_FLOOR_ATTEMPTS, p.url,
                )
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
