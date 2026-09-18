"""Parse the user-maintained URL queue (applications/queue.txt).

Format: one URL per line. Append the word "full" after the URL for the
full-effort tier (cover letter always written, deeper notes). Blank lines
and lines starting with # are ignored. The tool only ever *removes* lines,
and only when their application is archived (prune_queue); already-processed
active URLs are skipped via the tracker instead, so the user can otherwise
prune the file whenever they like.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .util import normalize_url


@dataclass
class QueueEntry:
    url: str  # normalized
    full: bool = False  # full-effort tier vs. the default quick tier
    # Re-draft an application that is already tracked, instead of skipping it
    # (--upgrade). Never set by the queue file: the file is a to-do list, and
    # re-spending money on a finished application has to be asked for by id.
    redraft: bool = False


def parse_queue(text: str) -> list[QueueEntry]:
    entries: list[QueueEntry] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        url = normalize_url(parts[0])
        if not url.startswith(("http://", "https://")):
            continue  # not a fetchable posting link; silently skip junk lines
        if url in seen:
            continue
        seen.add(url)
        flags = {p.lower() for p in parts[1:]}
        entries.append(QueueEntry(url=url, full="full" in flags))
    return entries


def load_queue(path: Path) -> list[QueueEntry]:
    if not path.exists():
        return []
    return parse_queue(path.read_text(encoding="utf-8"))


def prune_queue(path: Path, urls: set[str]) -> int:
    """Remove the lines whose URL (normalized, like the tracker keys) is in
    `urls`; every other line — comments, blanks, junk — stays byte-identical.
    Returns the number of lines removed."""
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [
        raw
        for raw in lines
        if not (
            (stripped := raw.strip())
            and not stripped.startswith("#")
            and normalize_url(stripped.split()[0]) in urls
        )
    ]
    removed = len(lines) - len(kept)
    if removed:
        path.write_text("".join(kept), encoding="utf-8")
    return removed
