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
from datetime import date
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


def _degraded_note(failed_sources: list[str]) -> str:
    return (
        "⚠️ This run was **incomplete** — the following source(s) failed or "
        "returned nothing usable: " + ", ".join(failed_sources) + ". Today's "
        "result may be missing matches those sources would have surfaced; treat "
        "it as a degraded run, not a confirmed quiet day."
    )


def render_markdown(
    finals: list[FinalPosting],
    run_date: date,
    *,
    degraded: bool = False,
    failed_sources: list[str] | None = None,
) -> str:
    lines = [f"# Job matches — {run_date.isoformat()}", ""]

    if degraded:
        lines.append(_degraded_note(failed_sources or []))
        lines.append("")

    best = [f for f in finals if f.tier == "best"]
    okay = [f for f in finals if f.tier == "okay"]

    if not finals:
        lines.append(
            "No matches today. This is a valid, expected outcome — not every "
            "day has a posting worth surfacing."
        )
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
            lines.append(
                f"- Skill score: {final.scored.skill_score}/100, "
                f"Interest score: {final.scored.interest_score}/100"
            )
            lines.append(f"- Best-fit CV: {final.scored.best_cv}")
            lines.append("")
            lines.append(final.writeup)
            lines.append("")

    return "\n".join(lines)


def render_summary(
    finals: list[FinalPosting],
    run_date: date,
    *,
    degraded: bool = False,
    failed_sources: list[str] | None = None,
) -> dict:
    failed_sources = failed_sources or []
    best = [f for f in finals if f.tier == "best"]
    okay = [f for f in finals if f.tier == "okay"]

    if not finals and degraded:
        # Don't let a broken run masquerade as a quiet day in the push headline.
        headline = f"No matches — run incomplete ({', '.join(failed_sources)} failed)"
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
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)

    markdown_path = reports_dir / f"{run_date.isoformat()}.md"
    markdown_path.write_text(
        render_markdown(
            finals,
            run_date,
            degraded=degraded,
            failed_sources=failed_sources,
        ),
        encoding="utf-8",
    )

    summary_path = reports_dir / "latest.json"
    summary_path.write_text(
        json.dumps(
            render_summary(finals, run_date, degraded=degraded, failed_sources=failed_sources),
            indent=2,
        ),
        encoding="utf-8",
    )

    return markdown_path, summary_path
