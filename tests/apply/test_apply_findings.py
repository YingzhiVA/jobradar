from jobradar.apply.findings import load_findings, parse_report

_REPORT = """\
# Job matches — 2026-06-26

1 strong match, 1 okay match

## [BEST] AI Product Manager — UBS

- Link: https://jobs.example.com/ubs-ai-pm
- Location: Zurich
- Skill score: 82/100, Interest score: 78/100
- Best-fit CV: CV_Example_AI_Product

**Why it fits**

Great overlap with the candidate's fintech background.

**Concerns**

Scale differs from previous employers.

## [OKAY] Product Manager Data & AI — Swisscom

- Link: https://jobs.example.com/swisscom-pm/
- Location: Zurich
- Skill score: 72/100, Interest score: 55/100
- Best-fit CV: CV_Example_Data_Product

Telecom is outside the comfort zone.

## Dropped this run — top 2 near-misses

- **Something — Somewhere** (skill 50, interest 40) — not relevant
"""


def test_parse_report_extracts_matches():
    findings = parse_report(_REPORT, "2026-06-26")
    assert len(findings) == 2

    best = findings[0]
    assert best.tier == "best"
    assert best.title == "AI Product Manager"
    assert best.company == "UBS"
    assert best.url == "https://jobs.example.com/ubs-ai-pm"
    assert best.skill_score == 82
    assert best.interest_score == 78
    assert best.best_cv == "CV_Example_AI_Product"
    assert "Great overlap" in best.writeup
    assert "Concerns" in best.writeup
    # The dropped-section content must not bleed into the last match.
    okay = findings[1]
    assert okay.url == "https://jobs.example.com/swisscom-pm"  # trailing slash normalized
    assert "near-miss" not in okay.writeup
    assert "Somewhere" not in okay.writeup


def test_load_findings_latest_report_wins(tmp_path):
    old = _REPORT.replace("82/100", "60/100")
    (tmp_path / "2026-06-01.md").write_text(old, encoding="utf-8")
    (tmp_path / "2026-06-26.md").write_text(_REPORT, encoding="utf-8")
    findings = load_findings(tmp_path)
    assert findings["https://jobs.example.com/ubs-ai-pm"].skill_score == 82
    assert findings["https://jobs.example.com/ubs-ai-pm"].report_date == "2026-06-26"


def test_load_findings_missing_dir(tmp_path):
    assert load_findings(tmp_path / "nope") == {}
