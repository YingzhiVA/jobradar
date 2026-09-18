"""Recover prior analysis for a URL from the daily reports (reports/*.md).

Postings that surfaced in a daily report already carry a Sonnet write-up,
scores, and a best-fit CV choice - reusing that means the apply pipeline
skips its own scoring call and grounds the tailoring in analysis the user
has already read. The daily CI workflow force-adds reports/*.md past the
.gitignore rule, so the reports are normally present on every machine (after
a pull); still, this is best-effort enrichment: a URL with no finding just
goes through the fresh scoring path instead.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .util import normalize_url

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^## \[(BEST|OKAY)\] (.+?) — (.+)$")
_LINK_RE = re.compile(r"^- Link: (\S+)")
_SCORES_RE = re.compile(r"^- Skill score: (\d+)/100, Interest score: (\d+)/100")
_CV_RE = re.compile(r"^- Best-fit CV: (.+)$")


@dataclass
class Finding:
    url: str  # normalized
    report_date: str  # YYYY-MM-DD, from the report filename
    tier: str  # "best" | "okay"
    title: str
    company: str
    skill_score: int | None
    interest_score: int | None
    best_cv: str | None
    writeup: str


def parse_report(text: str, report_date: str) -> list[Finding]:
    """Extract every match section from one daily report's markdown."""
    findings: list[Finding] = []
    current: dict | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None and current.get("url"):
            findings.append(
                Finding(
                    url=current["url"],
                    report_date=report_date,
                    tier=current["tier"],
                    title=current["title"],
                    company=current["company"],
                    skill_score=current.get("skill"),
                    interest_score=current.get("interest"),
                    best_cv=current.get("best_cv"),
                    writeup="\n".join(body).strip(),
                )
            )

    for line in text.splitlines():
        section = _SECTION_RE.match(line)
        if section:
            flush()
            current = {
                "tier": section.group(1).lower(),
                "title": section.group(2).strip(),
                "company": section.group(3).strip(),
            }
            body = []
            continue
        if line.startswith("## "):  # any other section ends the match block
            flush()
            current = None
            body = []
            continue
        if current is None:
            continue
        if match := _LINK_RE.match(line):
            current["url"] = normalize_url(match.group(1))
        elif match := _SCORES_RE.match(line):
            current["skill"] = int(match.group(1))
            current["interest"] = int(match.group(2))
        elif match := _CV_RE.match(line):
            current["best_cv"] = match.group(1).strip()
        elif line.startswith("- "):
            continue  # other metadata bullets (Location etc.)
        else:
            body.append(line)
    flush()
    return findings


def load_findings(reports_dir: Path) -> dict[str, Finding]:
    """URL -> most recent Finding across all local daily reports."""
    findings: dict[str, Finding] = {}
    if not reports_dir.exists():
        return findings
    # Sorted by filename = chronological, so later reports overwrite earlier
    # ones and the freshest analysis wins for a re-surfaced posting.
    for path in sorted(reports_dir.glob("*.md")):
        report_date = path.stem
        try:
            for finding in parse_report(path.read_text(encoding="utf-8"), report_date):
                findings[finding.url] = finding
        except OSError as exc:
            logger.warning("Could not read report %s: %s", path, exc)
    return findings
