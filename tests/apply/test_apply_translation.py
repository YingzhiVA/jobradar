from jobradar.apply.main import _notes_md
from jobradar.apply.tailor import language_name, swiss_conventions, swiss_spelling
from jobradar.apply.tracker import Application


def test_language_name():
    assert language_name("de") == "German"
    assert language_name("DE") == "German"
    assert language_name("en") == "English"
    assert language_name("fr") == "French"
    # Unknown code falls through to the code itself.
    assert language_name("xx") == "xx"
    assert language_name("") == "the posting's language"


def _app(**overrides) -> Application:
    base = dict(
        url="https://jobs.example.com/pm-1",
        folder="2026-07-06_acme_pm",
        title="Product Manager",
        company="Acme",
        prepared_on="2026-07-06",
        base_cv="CV_X",
        cover_letter=True,
    )
    base.update(overrides)
    return Application(**base)


def test_notes_mentions_translated_cv_when_present():
    app = _app(cv_translation_language="de")
    notes = _notes_md(app, "fits well", ["did X"], [], [], "de")
    assert "German CV" in notes
    assert "tailored_cv_de.md" in notes
    assert "Posting language: German" in notes
    # Checklist tells the user which file to submit.
    assert "Review tailored_cv_de.md" in notes


def test_notes_no_translation_for_english_posting():
    app = _app(cv_translation_language=None)
    notes = _notes_md(app, "fits well", ["did X"], [], [], "en")
    assert "tailored_cv_de.md" not in notes
    assert "Posting language: English" in notes
    assert "German CV" not in notes


def test_swiss_spelling_replaces_eszett_in_german():
    assert swiss_spelling("Die größte Straße", "de") == "Die grösste Strasse"
    assert swiss_spelling("ẞBUNG", "de") == "SSBUNG"
    assert swiss_spelling("Grösse", "de") == "Grösse"


def test_swiss_spelling_leaves_other_languages_alone():
    # Not German, so nothing to enforce - and no other language uses the letter.
    assert swiss_spelling("groß", "fr") == "groß"
    assert swiss_spelling("groß", "en") == "groß"
    assert swiss_spelling("groß", "") == "groß"


def test_swiss_conventions_only_for_languages_with_a_swiss_variety():
    assert "ß" in swiss_conventions("de")
    assert "septante" in swiss_conventions("fr")
    assert swiss_conventions("en") == ""
    assert swiss_conventions("it") == ""
    assert swiss_conventions("") == ""
