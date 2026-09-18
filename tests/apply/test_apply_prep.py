from jobradar.apply.prep import PrepQuestion, PrepSheet, render_prep_md


def _sheet() -> PrepSheet:
    return PrepSheet(
        positioning="I ship data products.",
        likely_questions=[
            PrepQuestion(
                question="Tell me about a time you handled conflict.",
                recommended_material="stakeholder-standoff",
                angle="Emphasize the compromise you brokered.",
            ),
            PrepQuestion(
                question="Have you run ML experiments at scale?",
                angle="Nothing prepared fits; build from the analytics-platform CV role.",
            ),
        ],
        coverage_gaps=["Nothing prepared covers people management."],
        questions_to_ask=["What does success look like in month six?"],
    )


def test_render_prep_md_full():
    md = render_prep_md(_sheet(), "Senior PM", "Acme", "https://acme.example/job")
    assert "# Interview prep: Senior PM at Acme" in md
    assert "- Posting: https://acme.example/job" in md
    assert "### Tell me about a time you handled conflict." in md
    assert "**stakeholder-standoff**" in md
    assert "(nothing prepared fits)" in md
    assert "- Nothing prepared covers people management." in md
    assert "- What does success look like in month six?" in md


def test_render_prep_md_write_ups_section():
    sheet = _sheet()
    assert "Write-ups to re-read first" not in render_prep_md(sheet, "Senior PM")
    sheet.write_ups_to_revisit = ["holding-still - the posting asks for LLM evals"]
    md = render_prep_md(sheet, "Senior PM")
    assert "## Write-ups to re-read first" in md
    assert "- holding-still - the posting asks for LLM evals" in md


def test_render_prep_md_without_company_or_url():
    md = render_prep_md(_sheet(), "2026-07-09_acme_senior-pm")
    assert md.startswith("# Interview prep: 2026-07-09_acme_senior-pm\n")
    assert "Posting:" not in md


def test_render_prep_md_no_gaps_flagged():
    sheet = _sheet()
    sheet.coverage_gaps = []
    md = render_prep_md(sheet, "Senior PM", "Acme")
    assert "- none flagged" in md


def test_render_prep_md_uses_posting_language_headings():
    sheet = _sheet()
    sheet.language = "de"
    sheet.write_ups_to_revisit = ["holding-still - das Inserat fragt nach LLM-Evals"]
    md = render_prep_md(sheet, "Senior PM", "Acme", "https://acme.example/job")
    assert md.startswith("# Interview-Vorbereitung: Senior PM bei Acme\n")
    assert "- Inserat: https://acme.example/job" in md
    assert "## Wahrscheinliche Fragen" in md
    assert "- Damit einsteigen: **stakeholder-standoff**" in md
    assert "- Damit einsteigen: (nichts Vorbereitetes passt)" in md
    assert "- Ansatz: Emphasize the compromise you brokered." in md
    assert "## Fragen an das Gegenüber" in md
    # Prepared-material labels name files in the profile: never translated.
    assert "**stakeholder-standoff**" in md


def test_render_prep_md_unknown_language_falls_back_to_english_headings():
    sheet = _sheet()
    sheet.language = "sv"
    md = render_prep_md(sheet, "Senior PM", "Acme")
    assert md.startswith("# Interview prep: Senior PM at Acme\n")
    assert "## Likely questions" in md


def test_generate_prep_names_the_language_it_is_given():
    from jobradar.apply import prep

    captured = {}

    class _FakeMessages:
        def stream(self, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            raise RuntimeError("stop here - only the prompt is under test")

    class _FakeClient:
        messages = _FakeMessages()

    assert prep.generate_prep("profile", "jd", _FakeClient(), language="de") is None
    assert "Write the sheet in German" in captured["prompt"]

    assert prep.generate_prep("profile", "jd", _FakeClient()) is None
    assert "the language the posting itself is written in" in captured["prompt"]


def test_render_prep_md_german_is_swiss_german():
    sheet = _sheet()
    sheet.language = "de"
    sheet.positioning = "Ich maß den Lift auf der Straße."
    sheet.coverage_gaps = ["Nichts deckt Personalführung ab, größte Lücke."]
    md = render_prep_md(sheet, "Senior PM", "Acme")
    assert "ß" not in md
    assert "Ich mass den Lift auf der Strasse." in md
    assert "grösste" in md
    # Swiss German quotes with guillemets, not German quotation marks.
    assert "Positionierung («Erzählen Sie von sich»)" in md
    assert "\u201e" not in md


def test_generate_prep_asks_for_swiss_german():
    from jobradar.apply import prep

    captured = {}

    class _FakeMessages:
        def stream(self, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            raise RuntimeError("stop here - only the prompt is under test")

    class _FakeClient:
        messages = _FakeMessages()

    prep.generate_prep("profile", "jd", _FakeClient(), language="de")
    assert "Schweizer Hochdeutsch" in captured["prompt"]

    prep.generate_prep("profile", "jd", _FakeClient(), language="fr")
    assert "Suisse romande" in captured["prompt"]

    # Language unknown at call time: the catch-all covers both varieties.
    prep.generate_prep("profile", "jd", _FakeClient())
    assert "Every posting here is Swiss" in captured["prompt"]

    # English posting: nothing Swiss to say about it.
    prep.generate_prep("profile", "jd", _FakeClient(), language="en")
    assert "Swiss" not in captured["prompt"]
