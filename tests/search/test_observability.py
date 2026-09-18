import json
from datetime import date

from jobradar.models import Posting, ScoredPosting
from jobradar.search.observability import (
    SCHEMA_VERSION,
    append_run_record,
    build_run_record,
)
from jobradar.search.sources.base import FetchResult

RUN_DATE = date(2026, 6, 17)


def _scored(label, *, skill, interest, unmet=None):
    return ScoredPosting(
        posting=Posting(
            id=label, source="web_search", url=f"https://x/{label}",
            title=label, company="Co", description="desc",
        ),
        skill_score=skill,
        interest_score=interest,
        best_cv="cv",
        brief_reason="why",
        unmet_hard_requirements=unmet or [],
    )


def _record():
    scored = [
        _scored("surfaced", skill=90, interest=90),
        _scored("deferred", skill=70, interest=62),
        _scored("weak", skill=40, interest=30),
    ]
    fetch_results = [
        FetchResult(
            "web_search", [], ok=True, detail="2 searches, 5 found, 3 live",
            meta={"queries": ["ai pm zurich", "data product owner"], "searches": 2},
        ),
    ]
    funnel = {"raw": 12, "scored": 3, "surfaced": 1}
    return build_run_record(
        RUN_DATE, fetch_results, funnel, scored, {"surfaced": "best"},
        degraded=False, failed_sources=[],
    )


def test_build_record_shape_and_outcomes():
    rec = _record()
    assert rec["schema_version"] == SCHEMA_VERSION
    assert rec["date"] == "2026-06-17"
    assert rec["degraded"] is False
    outcomes = {o["id"]: o["outcome"] for o in rec["outcomes"]}
    assert outcomes == {
        "surfaced": "surfaced-best",
        "deferred": "deferred-capped",
        "weak": "below-floor",
    }


def test_build_record_captures_queries_from_source_meta():
    rec = _record()
    ws = next(s for s in rec["sources"] if s["name"] == "web_search")
    assert ws["meta"]["queries"] == ["ai pm zurich", "data product owner"]
    assert rec["funnel"]["raw"] == 12


def test_append_writes_one_json_line_per_run(tmp_path):
    append_run_record(_record(), tmp_path)
    append_run_record(_record(), tmp_path)
    lines = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # Each line is a standalone, parseable record.
    parsed = json.loads(lines[0])
    assert parsed["date"] == "2026-06-17"


def test_record_carries_the_floors_the_run_used():
    # The log has to be readable after a retune: the same counts under a
    # different configuration are a different run.
    rec = build_run_record(
        RUN_DATE, [], {"scored": 1}, [_scored("a", skill=70, interest=62)], {},
        degraded=False, failed_sources=[], min_skill=75, min_interest=50,
    )
    assert rec["settings"] == {"min_skill": 75, "min_interest": 50}


def test_outcomes_are_labelled_with_the_configured_floors():
    # skill 70 is above the default floor (deferred) but below a raised one,
    # where the posting is instead dropped for good — opposite dedup behaviour,
    # so the label must follow the run's own setting.
    scored = [_scored("a", skill=70, interest=62)]
    default = build_run_record(
        RUN_DATE, [], {}, scored, {}, degraded=False, failed_sources=[],
    )
    raised = build_run_record(
        RUN_DATE, [], {}, scored, {}, degraded=False, failed_sources=[], min_skill=75,
    )
    assert default["outcomes"][0]["outcome"] == "deferred-capped"
    assert raised["outcomes"][0]["outcome"] == "below-floor"
