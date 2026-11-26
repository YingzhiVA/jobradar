"""Renders the daily output: a detailed markdown report (reports/YYYY-MM-DD.md)
and a short summary (reports/latest.json) that the scheduled routine reads
to compose the push notification headline.

This is purely the deliverable — the few roles worth the job-seeker's time.
The operator-facing quality signal that used to ride along here (the "dropped
this run" near-miss audit) now lives in the per-run observability artifact
(search/observability.py), which also correctly distinguishes a quota-deferred
role from one that fell below a floor — a line this report conflated.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from ..models import FinalPosting
from .sources.base import LIVENESS_UNVERIFIED

# Shown against the link of a posting whose URL nothing in this run could confirm
# is still open (see search/liveness.py). The pipeline keeps such postings on
# purpose — a live role shouldn't be discarded over a 403 or a network blip — but
# the reader is the one spending an hour on a cover letter, so they get told which
# links are a guess. Without this, a bot-blocked host's 403 is indistinguishable
# from a healthy 200 by the time a role reaches this page.
#
# Emitted as a metadata bullet, not free text, so it stays with the link it's
# about and so apply/findings.py's report parser skips it along with the other
# bullets — the caveat belongs to the reader, not in the write-up text that
# grounds a later cover letter.
_UNVERIFIED_NOTE = (
    "- ⚠️ **Link unverified** — this posting's page did not answer when checked "
    "(bot protection or a network error), so it could not be confirmed still open. "
    "Open it before spending time on this one."
)


# The practice run's report: one file, replaced by each practice run, never
# dated, so it is not a day's report, not archived, and not read for findings.
PRACTICE_REPORT = "practice.md"

# Where a match came from, as the reader should see it. The board connectors
# name their applicant-tracking system (greenhouse, lever, …); those are the
# boards chosen in companies.yaml. The web search is not limited to them, so a
# match from a company never chosen comes from there, and says so.
_SOURCE_LABELS = {
    "web_search": "web search",
    "eth_jobs": "ETH Zurich job board",
    "arbeitnow": "Arbeitnow job board",
    "fixture": "practice sample",
}


def source_label(source: str) -> str:
    if source in _SOURCE_LABELS:
        return _SOURCE_LABELS[source]
    return f"your company boards ({source.replace('_', ' ').title()})"


def _degraded_note(failed_sources: list[str]) -> str:
    return (
        "⚠️ This run was **incomplete** — the following source(s) failed or "
        "returned nothing usable: " + ", ".join(failed_sources) + ". Today's "
        "result may be missing matches those sources would have surfaced; treat "
        "it as a degraded run, not a confirmed quiet day."
    )


def blocked_note(blocked_boards: list[str]) -> str:
    """Why some boards found nothing: Cloudflare's bot protection refused the
    run (see sources/backoff.is_bot_challenge). It is said apart from the
    degraded note because a handful of blocked boards doesn't degrade the run,
    and because no retry cures it: it depends on where the run comes from."""
    count = len(blocked_boards)
    return (
        f"⚠️ {count} board{'s' if count != 1 else ''} could not be searched: "
        + ", ".join(blocked_boards)
        + ". Cloudflare's bot check, in front of these job sites, refused this "
        "run's requests, so their postings are missing from it. Waiting doesn't "
        "help: the check goes by where a request comes from. It currently "
        "refuses GitHub Actions' servers and answers most home connections."
    )


def render_markdown(
    finals: list[FinalPosting],
    run_date: date,
    *,
    degraded: bool = False,
    failed_sources: list[str] | None = None,
    floors: tuple[int, int] | None = None,
    blocked_boards: list[str] | None = None,
) -> str:
    lines = [f"# Job matches — {run_date.isoformat()}", ""]
    lines += _render_run(
        finals, degraded=degraded, failed_sources=failed_sources, floors=floors,
        blocked_boards=blocked_boards,
    )
    return "\n".join(lines)


def below_floors(skill: int, interest: int, floors: tuple[int, int]) -> str:
    """Which of your floors a score misses, in words; empty if none. Only a
    practice run shows a posting below them (see search/main.py)."""
    min_skill, min_interest = floors
    misses = []
    if skill < min_skill:
        misses.append(f"skill {skill} is under your floor of {min_skill}")
    if interest < min_interest:
        misses.append(f"interest {interest} is under your floor of {min_interest}")
    return " and ".join(misses)


def render_later_run(
    finals: list[FinalPosting],
    run_at: datetime,
    *,
    degraded: bool = False,
    failed_sources: list[str] | None = None,
    blocked_boards: list[str] | None = None,
) -> str:
    """A further run on a day that already has a report, as a section to add
    to it. Its own "## " heading ends the last match above it for every
    reader of the report (apply/findings.py, ui/views.py)."""
    lines = ["", f"## Later run at {run_at.strftime('%H:%M')}", ""]
    lines += _render_run(
        finals, degraded=degraded, failed_sources=failed_sources,
        blocked_boards=blocked_boards, empty="No new matches in this run.",
    )
    return "\n".join(lines)


def _render_run(
    finals: list[FinalPosting],
    *,
    degraded: bool = False,
    failed_sources: list[str] | None = None,
    floors: tuple[int, int] | None = None,
    blocked_boards: list[str] | None = None,
    empty: str = (
        "No matches today. This is a valid, expected outcome — not every "
        "day has a posting worth surfacing."
    ),
) -> list[str]:
    lines: list[str] = []
    if degraded:
        lines.append(_degraded_note(failed_sources or []))
        lines.append("")
    if blocked_boards:
        lines.append(blocked_note(blocked_boards))
        lines.append("")

    best = [f for f in finals if f.tier == "best"]
    okay = [f for f in finals if f.tier == "okay"]

    if not finals:
        lines.append(empty)
    else:
        # Counted rather than assumed: output.max_best in config/search.yaml can
        # be raised above 1, so "1 strong match" is no longer a given.
        summary_bits = []
        if best:
            summary_bits.append(f"{len(best)} strong match{'es' if len(best) != 1 else ''}")
        if okay:
            summary_bits.append(f"{len(okay)} okay match{'es' if len(okay) != 1 else ''}")
        lines.append(", ".join(summary_bits))
        lines.append("")

        for final in best + okay:
            posting = final.scored.posting
            lines.append(f"## [{final.tier.upper()}] {posting.title} — {posting.company}")
            lines.append("")
            lines.append(f"- Link: {posting.url}")
            if posting.liveness == LIVENESS_UNVERIFIED:
                lines.append(_UNVERIFIED_NOTE)
            lines.append(f"- Location: {posting.location_text or 'n/a'}")
            lines.append(f"- Found by: {source_label(posting.source)}")
            miss = floors and below_floors(final.scored.skill_score, final.scored.interest_score, floors)
            if miss:
                lines.append(f"- Below your floors: {miss}, so a real search would not show it.")
            lines.append(
                f"- Skill score: {final.scored.skill_score}/100, "
                f"Interest score: {final.scored.interest_score}/100"
            )
            lines.append(f"- Best-fit CV: {final.scored.best_cv}")
            lines.append("")
            lines.append(final.writeup)
            lines.append("")

    return lines


def render_summary(
    finals: list[FinalPosting],
    run_date: date,
    *,
    degraded: bool = False,
    failed_sources: list[str] | None = None,
    blocked_boards: list[str] | None = None,
) -> dict:
    failed_sources = failed_sources or []
    blocked_boards = blocked_boards or []
    best = [f for f in finals if f.tier == "best"]
    okay = [f for f in finals if f.tier == "okay"]

    if not finals and degraded:
        # Don't let a broken run masquerade as a quiet day in the push headline.
        headline = f"No matches — run incomplete ({', '.join(failed_sources)} failed)"
    elif not finals and blocked_boards:
        # Not a confirmed quiet day either: say so where it is read first.
        count = len(blocked_boards)
        headline = f"No matches today ({count} board{'s' if count != 1 else ''} blocked)"
    elif not finals:
        headline = "No matches today"
    elif best:
        # The top pick is named in full; any further ones (possible once
        # output.max_best > 1) are counted, to keep the push/email subject line
        # short enough to read on a lock screen.
        headline = (
            f"{len(best)} strong match{'es' if len(best) != 1 else ''}: "
            f"{best[0].scored.posting.title} @ {best[0].scored.posting.company}"
        )
        extra = [f"+{len(best) - 1} more strong"] if len(best) > 1 else []
        if okay:
            extra.append(f"+{len(okay)} okay")
        if extra:
            headline += f" ({', '.join(extra)})"
    else:
        headline = f"{len(okay)} okay match{'es' if len(okay) != 1 else ''}, no standout today"

    return {
        "date": run_date.isoformat(),
        "headline": headline,
        "degraded": degraded,
        "failed_sources": failed_sources,
        "blocked_boards": blocked_boards,
        "best_count": len(best),
        "okay_count": len(okay),
        "matches": [
            {
                "tier": f.tier,
                "title": f.scored.posting.title,
                "company": f.scored.posting.company,
                "url": f.scored.posting.url,
                # So the push notification / any other consumer can carry the
                # same caveat the markdown shows, instead of re-deriving it.
                "liveness": f.scored.posting.liveness,
                "location": f.scored.posting.location_text,
                "source": f.scored.posting.source,
                "skill_score": f.scored.skill_score,
                "interest_score": f.scored.interest_score,
                "writeup": f.writeup,
            }
            for f in best + okay
        ],
    }


def write_report(
    finals: list[FinalPosting],
    run_date: date,
    reports_dir: Path,
    *,
    degraded: bool = False,
    failed_sources: list[str] | None = None,
    practice: bool = False,
    run_at: datetime | None = None,
    floors: tuple[int, int] | None = None,
    blocked_boards: list[str] | None = None,
) -> tuple[Path, Path | None]:
    """Write the day's report and latest.json.

    A second run on the same day adds a section to the day's report instead of
    replacing it: the first run's matches are already marked seen, so they
    would not come back, and the report is the only place they are shown.

    A practice run writes reports/practice.md, replaced each time, and no
    latest.json (the daily email reads that), so its sample postings never
    stand in for a real day's matches. Returns the summary path as None then.
    `floors` (skill, interest) are the real ones, for a practice run's note on
    each sample a real search would have left out.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)

    if practice:
        markdown_path = reports_dir / PRACTICE_REPORT
        markdown_path.write_text(
            render_markdown(
                finals, run_date, degraded=degraded, failed_sources=failed_sources, floors=floors,
            ),
            encoding="utf-8",
        )
        return markdown_path, None

    markdown_path = reports_dir / f"{run_date.isoformat()}.md"
    if markdown_path.exists():
        with markdown_path.open("a", encoding="utf-8") as handle:
            handle.write(
                render_later_run(
                    finals,
                    run_at or datetime.now(),
                    degraded=degraded,
                    failed_sources=failed_sources,
                    blocked_boards=blocked_boards,
                )
            )
    else:
        markdown_path.write_text(
            render_markdown(
                finals,
                run_date,
                degraded=degraded,
                failed_sources=failed_sources,
                blocked_boards=blocked_boards,
            ),
            encoding="utf-8",
        )

    summary_path = reports_dir / "latest.json"
    summary_path.write_text(
        json.dumps(
            render_summary(
                finals, run_date, degraded=degraded, failed_sources=failed_sources,
                blocked_boards=blocked_boards,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )

    return markdown_path, summary_path
