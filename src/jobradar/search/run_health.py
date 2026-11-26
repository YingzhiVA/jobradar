"""Run health: count a run's warnings by message template, and flag the ones
that are new, persistent or spiking against earlier runs.

Why. Personio feeds that left out descriptions (2026-11-11 on) made the
scorer fail on the same posting every run, for about two weeks, and the only
sign was a quiet warning in a run log nobody reads. Workday's 429s from
2026-11-16 were found the same way. board_health checks once a month whether
each board answers; it can't see a scoring loop, refused link checks, or a
feed that drops a field. The run itself can, if it remembers what it logged.

Collect. WarningCounter, a logging handler on the root logger for the length
of a run, counts every WARNING and ERROR by (logger, message template), the
`record.msg` before its arguments are filled in. Grouping by template needs
no list of known problems, so it catches unknown ones too. Per template it
also counts its subjects, what the warning is about: the first argument that
is a URL, else a host name, else a short text (a board's name). The counts go
into the run's record in reports/runs.jsonl (`health.warnings`).

Compare (assess) with the previous runs' records, most recent first:
- new: a template none of the last HISTORY_RUNS runs logged (judged once
  MIN_HISTORY of them have a health record, so the first runs after this
  went in don't flag everything);
- persistent: the same subject under the same template on PERSISTENT_RUNS
  runs in a row, this one included (Legartis 2885065 would have been flagged
  on its third run, not after ~15);
- spike: a template SPIKE_FACTOR times its usual count (the median over the
  history, absent counting as 0) and at least SPIKE_MIN_EXTRA more.

Surface. The report gets a "Run health" section when anything is flagged.
The email mentions only what is worth an interruption: new templates, and a
persistent subject on the run it becomes persistent, not on every run after
(Flag.email). Practice runs record and flag nothing.

Not this: a model reading the run log after every run. That is a call per
run, judgement that varies between runs, and no memory of earlier runs, which
is the part that matters here.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

HISTORY_RUNS = 30
MIN_HISTORY = 5
PERSISTENT_RUNS = 3
SPIKE_FACTOR = 3
SPIKE_MIN_EXTRA = 10
# Per template: how many distinct subjects are kept (a burst of link-check
# warnings can name hundreds of URLs; the record is committed every run), and
# how much of one example message.
MAX_SUBJECTS = 50
EXAMPLE_CHARS = 300

NEW, PERSISTENT, SPIKE = "new", "persistent", "spike"

_URL_RE = re.compile(r"^https?://\S+$")
_HOST_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9-]+(\.[a-z0-9-]+)+$", re.IGNORECASE)
_SUBJECT_TEXT_CHARS = 80


def _logger_name(name: str) -> str:
    # main.py logs as __main__ under `python -m jobradar.search.main`; the key
    # must not depend on how the run was started.
    return "jobradar.search.main" if name == "__main__" else name


def _template(record: logging.LogRecord) -> str:
    msg = record.msg
    return msg if isinstance(msg, str) else f"<{type(msg).__name__}>"


def subject_of(args: object) -> str | None:
    """What a warning is about, from its arguments: the first that is a URL,
    else the first that is a host name, else the first short one-line text.
    None when no argument qualifies (numbers only, or none at all)."""
    if not isinstance(args, tuple):
        args = (args,) if args else ()
    texts = [arg.strip() for arg in args if isinstance(arg, str)]
    for test in (_URL_RE.match, _HOST_RE.match):
        for text in texts:
            if test(text):
                return text
    for text in texts:
        if text and len(text) <= _SUBJECT_TEXT_CHARS and "\n" not in text:
            return text
    return None


class WarningCounter(logging.Handler):
    """Counts WARNING and ERROR records by (logger, template); see the module
    docstring. Thread-safe: scoring logs from a thread pool."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._counts_lock = threading.Lock()
        self._entries: dict[tuple[str, str], dict] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            key = (_logger_name(record.name), _template(record))
            subject = subject_of(record.args)
            with self._counts_lock:
                entry = self._entries.get(key)
                if entry is None:
                    entry = self._entries[key] = {
                        "logger": key[0],
                        "template": key[1],
                        "level": record.levelname,
                        "count": 0,
                        "subjects": {},
                        "example": record.getMessage()[:EXAMPLE_CHARS],
                    }
                entry["count"] += 1
                subjects = entry["subjects"]
                if subject is not None and (subject in subjects or len(subjects) < MAX_SUBJECTS):
                    subjects[subject] = subjects.get(subject, 0) + 1
        except Exception:  # noqa: BLE001 - a logging handler must never raise
            self.handleError(record)

    def snapshot(self) -> list[dict]:
        """The counts so far, most frequent first, as stored in runs.jsonl."""
        with self._counts_lock:
            entries = [{**e, "subjects": dict(e["subjects"])} for e in self._entries.values()]
        return sorted(entries, key=lambda e: (-e["count"], e["logger"], e["template"]))


@contextmanager
def counting_warnings() -> Iterator[WarningCounter]:
    """A WarningCounter on the root logger for the length of the block."""
    counter = WarningCounter()
    root = logging.getLogger()
    root.addHandler(counter)
    try:
        yield counter
    finally:
        root.removeHandler(counter)


@dataclass
class Flag:
    kind: str  # NEW, PERSISTENT or SPIKE
    logger: str
    template: str
    count: int
    example: str
    # PERSISTENT: the subjects that persist; otherwise the run's most frequent.
    subjects: list[str] = field(default_factory=list)
    # PERSISTENT: the longest run of consecutive runs among those subjects.
    runs: int = 0
    # SPIKE: the template's usual count (median over the history).
    usual: float = 0.0
    # Whether the email mentions it: see the module docstring.
    email: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _index(warnings: list[dict]) -> dict[tuple[str, str], dict]:
    return {(w.get("logger", ""), w.get("template", "")): w for w in warnings}


def _streak(key: tuple[str, str], subject: str | None, history: list[dict | None]) -> int:
    """Consecutive previous runs, most recent first, that logged this
    template (and this subject, when there is one)."""
    runs = 0
    for previous in history:
        if previous is None:
            break
        entry = previous.get(key)
        if entry is None or (subject is not None and subject not in entry.get("subjects", {})):
            break
        runs += 1
    return runs


def assess(current: list[dict], history: list[list[dict] | None]) -> list[Flag]:
    """Flags for this run's warnings (`current`, a WarningCounter snapshot)
    against `history`: earlier runs' snapshots, most recent first, None for a
    run that has no health record. New first, then persistent, then spikes."""
    indexed = [None if h is None else _index(h) for h in history[:HISTORY_RUNS]]
    recorded = [h for h in indexed if h is not None]
    judged = len(recorded) >= MIN_HISTORY
    flags: list[Flag] = []
    for warning in current:
        key = (warning["logger"], warning["template"])
        subjects: dict[str, int] = warning.get("subjects") or {}
        top = [s for s, _ in sorted(subjects.items(), key=lambda kv: -kv[1])[:3]]
        base = dict(
            logger=key[0], template=key[1], count=warning["count"], example=warning.get("example", "")
        )
        is_new = judged and all(key not in h for h in recorded)
        if is_new:
            flags.append(Flag(NEW, subjects=top, email=True, **base))

        persistent = {}
        for subject in subjects or [None]:
            runs = 1 + _streak(key, subject, indexed)
            if runs >= PERSISTENT_RUNS:
                persistent[subject] = runs
        if persistent:
            longest = max(persistent.values())
            flags.append(Flag(
                PERSISTENT,
                subjects=sorted((s for s in persistent if s is not None), key=lambda s: -persistent[s]),
                runs=longest,
                # Mentioned once, on the run a subject becomes persistent.
                email=any(runs == PERSISTENT_RUNS for runs in persistent.values()),
                **base,
            ))

        if judged and not is_new:
            usual = statistics.median(h[key]["count"] if key in h else 0 for h in recorded)
            if warning["count"] >= SPIKE_FACTOR * max(usual, 1) and warning["count"] - usual >= SPIKE_MIN_EXTRA:
                flags.append(Flag(SPIKE, subjects=top, usual=usual, **base))
    order = {NEW: 0, PERSISTENT: 1, SPIKE: 2}
    return sorted(flags, key=lambda f: order[f.kind])


def load_history(runs_path: Path, limit: int = HISTORY_RUNS) -> list[list[dict] | None]:
    """The last `limit` runs' health snapshots from runs.jsonl, most recent
    first; None for a run recorded before health was. An unreadable line
    counts as a run without one."""
    if not runs_path.exists():
        return []
    lines = runs_path.read_text(encoding="utf-8").splitlines()[-limit:]
    history: list[list[dict] | None] = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            history.append(None)
            continue
        health = record.get("health") if isinstance(record, dict) else None
        warnings = health.get("warnings") if isinstance(health, dict) else None
        history.append(warnings if isinstance(warnings, list) else None)
    return history
