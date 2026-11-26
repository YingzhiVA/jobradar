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


def test_bullets_in_the_write_up_are_kept_and_metadata_bullets_are_not():
    # A write-up with a bullet list, as 13 matches had from 2026-07 to 2026-10:
    # every write-up bullet used to be dropped as if it were metadata.
    report = """\
# Job matches — 2026-10-16

## [BEST] Data Product Manager — Acme

- Link: https://jobs.example.com/acme-dpm
- ⚠️ **Link unverified** — this posting's page did not answer when checked.
- Location: Zurich
- Found by: web search
- Skill score: 81/100, Interest score: 74/100
- Best-fit CV: CV_Example_Data_Product

**What to highlight**
- The analytics framework at a previous employer.
- Translating model outputs for executives.

**Concerns**

Regulated industry is new.
"""
    [finding] = parse_report(report, "2026-10-16")
    assert finding.url == "https://jobs.example.com/acme-dpm"
    assert (finding.skill_score, finding.interest_score) == (81, 74)
    assert finding.best_cv == "CV_Example_Data_Product"
    assert finding.writeup == (
        "**What to highlight**\n"
        "- The analytics framework at a previous employer.\n"
        "- Translating model outputs for executives.\n\n"
        "**Concerns**\n\n"
        "Regulated industry is new."
    )


def test_a_write_up_that_opens_with_a_bullet_keeps_it():
    report = """\
## [OKAY] Analyst — Acme

- Link: https://jobs.example.com/acme-analyst
- Best-fit CV: CV_Example

- Strong SQL overlap.
"""
    [finding] = parse_report(report, "2026-10-16")
    assert finding.best_cv == "CV_Example"
    assert finding.writeup == "- Strong SQL overlap."
