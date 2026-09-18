"""The relevance cut: tailoring has to subtract, and has to say what it cut.

Reordering and rephrasing show up in the tailored CV itself. A deleted bullet
leaves nothing behind, so the only way the user can review the cuts - or notice
that none were made - is the list the writing call reports back and notes.md
renders. These tests pin that path; the deterministic backstop that catches a
CV where nothing was cut lives in test_apply_lint.py.
"""

from jobradar.apply.main import _notes_md
from jobradar.apply.tailor import ApplicationDocs
from jobradar.apply.tracker import Application


def _app(**overrides) -> Application:
    base = dict(
        url="https://jobs.example.com/ai-eng-1",
        folder="031_2026-09-09_axa_ai-engineer",
        title="AI Engineer",
        company="AXA",
        prepared_on="2026-09-09",
        base_cv="CV_Example_AI_Product",
    )
    base.update(overrides)
    return Application(**base)


def test_docs_default_to_no_cuts_reported():
    docs = ApplicationDocs(tailored_cv_markdown="# CV")
    assert docs.dropped == []


def test_notes_list_the_dropped_bullets():
    notes = _notes_md(
        _app(),
        "fits well",
        ["led with the RAG project"],
        [],
        [],
        "de",
        ["'Restructuring meetings around clear objectives' - operating cadence, "
         "not the hands-on AI work this posting hires for"],
    )
    assert "## What the tailored CV leaves out" in notes
    assert "Restructuring meetings" in notes


def test_notes_call_out_a_cv_that_was_never_trimmed():
    # An empty list is the interesting case, not a boring one: it means the
    # relevance filter did not fire, so notes.md says so instead of rendering
    # a silent empty section.
    notes = _notes_md(_app(), "fits well", ["reordered bullets"], [], [], "en", [])
    assert "## What the tailored CV leaves out" in notes
    assert "nothing dropped" in notes


def test_notes_treat_an_unreported_cut_list_as_none_dropped():
    # dropped is optional so older callers keep working; they get the same
    # warning as an explicitly empty list rather than a missing section.
    notes = _notes_md(_app(), "fits well", ["reordered bullets"], [], [], "en")
    assert "nothing dropped" in notes
