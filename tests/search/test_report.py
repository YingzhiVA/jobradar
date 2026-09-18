from datetime import date

from jobradar.models import FinalPosting, Posting, ScoredPosting
from jobradar.search.report import render_markdown, render_summary
from jobradar.search.sources.base import (
    LIVENESS_CONFIRMED,
    LIVENESS_LISTED,
    LIVENESS_UNVERIFIED,
)

RUN_DATE = date(2026, 6, 17)


def _final(*, liveness=LIVENESS_LISTED, tier="best"):
    posting = Posting(
        id="abc",
        source="web_search",
        url="https://www.swissre.com/careers/job/Analytics-Engineer/1275367101",
        title="Analytics Engineer",
        company="Swiss Re",
        description="d",
        location_text="Zurich, Switzerland",
        liveness=liveness,
    )
    scored = ScoredPosting(
        posting=posting,
        skill_score=80,
        interest_score=80,
        best_cv="CV_Data",
        brief_reason="r",
    )
    return FinalPosting(scored=scored, tier=tier, writeup="**Why it fits**\n\nBecause.")


def test_summary_no_matches_not_degraded():
    summary = render_summary([], RUN_DATE)
    assert summary["headline"] == "No matches today"
    assert summary["degraded"] is False
    assert summary["failed_sources"] == []


def test_summary_no_matches_degraded_headline_names_failed_source():
    # A broken run must NOT read as a quiet day in the push headline.
    summary = render_summary([], RUN_DATE, degraded=True, failed_sources=["web_search"])
    assert "incomplete" in summary["headline"]
    assert "web_search" in summary["headline"]
    assert summary["degraded"] is True
    assert summary["failed_sources"] == ["web_search"]


def test_markdown_no_matches_degraded_includes_warning():
    md = render_markdown([], RUN_DATE, degraded=True, failed_sources=["web_search"])
    assert "incomplete" in md.lower()
    assert "web_search" in md
    # The plain quiet-day line should still be there too.
    assert "No matches today" in md


def test_markdown_no_matches_clean_has_no_warning():
    md = render_markdown([], RUN_DATE)
    assert "incomplete" not in md.lower()
    assert "No matches today" in md
    # The near-miss audit moved to the observability artifact; the deliverable
    # no longer carries any operator-facing "dropped" section.
    assert "Dropped this run" not in md


# --- Unverified links must not read like verified ones ------------------------


def test_markdown_flags_a_match_whose_link_was_never_confirmed():
    # The 2026-08-17 regression: a posting kept only because a bot-blocking host
    # refused to answer looked identical to a confirmed-open one in the report.
    md = render_markdown([_final(liveness=LIVENESS_UNVERIFIED)], RUN_DATE)
    assert "Link unverified" in md
    # The caveat has to sit with the link, above the write-up that argues for it.
    assert md.index("Link unverified") < md.index("Why it fits")


def test_unverified_caveat_stays_out_of_the_reusable_writeup():
    # apply/findings.py re-reads this markdown to ground a later cover letter and
    # keeps every non-bullet line as the write-up. Emitting the caveat as a bullet
    # keeps it in front of the reader without leaking into that text.
    from jobradar.apply.findings import parse_report

    md = render_markdown([_final(liveness=LIVENESS_UNVERIFIED)], RUN_DATE)
    finding = parse_report(md, RUN_DATE.isoformat())[0]
    assert "unverified" not in finding.writeup.lower()
    assert "Because." in finding.writeup


def test_markdown_does_not_flag_confirmed_or_listed_matches():
    for liveness in (LIVENESS_CONFIRMED, LIVENESS_LISTED):
        md = render_markdown([_final(liveness=liveness)], RUN_DATE)
        assert "unverified" not in md.lower()


def test_summary_carries_liveness_per_match():
    summary = render_summary([_final(liveness=LIVENESS_UNVERIFIED)], RUN_DATE)
    assert summary["matches"][0]["liveness"] == LIVENESS_UNVERIFIED


def test_summary_headline_counts_several_strong_matches():
    # output.max_best can be raised above 1, so the headline counts rather than
    # assuming a single "1 strong match".
    finals = [_final(tier="best"), _final(tier="best"), _final(tier="okay")]
    summary = render_summary(finals, RUN_DATE)
    assert summary["headline"].startswith("2 strong matches: Analytics Engineer @ Swiss Re")
    assert "+1 more strong" in summary["headline"]
    assert "+1 okay" in summary["headline"]
    assert summary["best_count"] == 2


def test_summary_headline_singular_for_one_strong_match():
    summary = render_summary([_final(tier="best")], RUN_DATE)
    assert summary["headline"] == "1 strong match: Analytics Engineer @ Swiss Re"


def test_markdown_summary_line_counts_both_tiers():
    markdown = render_markdown([_final(tier="best"), _final(tier="best"), _final(tier="okay")], RUN_DATE)
    assert "2 strong matches, 1 okay match" in markdown
