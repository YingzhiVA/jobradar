import io
import zipfile

import pytest

from jobradar.cv import ConversionError, docx_to_markdown, sync_word_cvs, word_files

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml"'
)

# German Word: style ids are localised, the names stay English.
_STYLES = f"""<w:styles {_NS}>
  <w:style w:type="paragraph" w:styleId="Titel"><w:name w:val="Title"/></w:style>
  <w:style w:type="paragraph" w:styleId="berschrift1"><w:name w:val="heading 1"/></w:style>
  <w:style w:type="paragraph" w:styleId="Abschnitt"><w:name w:val="Section"/><w:basedOn w:val="berschrift1"/></w:style>
  <w:style w:type="paragraph" w:styleId="Aufzhlungszeichen"><w:name w:val="List Bullet"/>
    <w:pPr><w:numPr><w:numId w:val="3"/></w:numPr></w:pPr></w:style>
</w:styles>"""

_RELS = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId9" Type="hyperlink" Target="https://example.com/notes" TargetMode="External"/>
  <Relationship Id="rId10" Type="hyperlink" Target="mailto:jane@example.com" TargetMode="External"/>
</Relationships>"""


def _p(text="", style=None, *, runs=None, ppr=""):
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    body = runs if runs is not None else f"<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r>"
    return f"<w:p><w:pPr>{style_xml}{ppr}</w:pPr>{body}</w:p>"


def _r(text, *, b=False, i=False):
    rpr = ("<w:b/>" if b else "") + ("<w:i/>" if i else "")
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'


def _docx(body, *, headers=(), styles=_STYLES, rels=_RELS):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", f"<w:document {_NS}><w:body>{body}</w:body></w:document>")
        if styles:
            zf.writestr("word/styles.xml", styles)
        if rels:
            zf.writestr("word/_rels/document.xml.rels", rels)
        for n, header in enumerate(headers, 1):
            zf.writestr(f"word/header{n}.xml", f"<w:hdr {_NS}>{header}</w:hdr>")
    return buf.getvalue()


def test_headings_follow_style_names_not_localised_ids():
    md = docx_to_markdown(_docx(
        _p("Jane Example", "Titel")
        + _p("Erfahrung", "berschrift1")
        + _p("Inherited", "Abschnitt")
        + _p("By outline", ppr='<w:outlineLvl w:val="2"/>')
    ))
    assert md == "# Jane Example\n\n## Erfahrung\n\n## Inherited\n\n#### By outline\n"


def test_runs_merge_and_markers_hug_the_text():
    # Word splits text into runs at every edit; "Acme " + "AG" in bold is one
    # bold stretch, and the space before the dates must sit outside the **.
    md = docx_to_markdown(_docx(_p(runs=_r("Acme ", b=True) + _r("AG ", b=True) + _r("2022") + _r(" remote", i=True))))
    assert md == "**Acme AG** 2022 *remote*\n"


def test_headings_drop_run_formatting():
    md = docx_to_markdown(_docx(_p("Experience", "berschrift1", runs=_r("Experience", b=True))))
    assert md == "## Experience\n"


def test_lists_by_numbering_by_style_and_by_typed_bullet():
    numbered = '<w:numPr><w:ilvl w:val="{lvl}"/><w:numId w:val="5"/></w:numPr>'
    md = docx_to_markdown(_docx(
        _p("Top", ppr=numbered.format(lvl=0))
        + _p("Nested", ppr=numbered.format(lvl=1))
        + _p("From the style", "Aufzhlungszeichen")
        + _p("• Typed bullet")
        + _p("Switched off", "Aufzhlungszeichen", ppr='<w:numPr><w:numId w:val="0"/></w:numPr>')
    ))
    assert md == "- Top\n  - Nested\n- From the style\n- Typed bullet\n\nSwitched off\n"


def test_layout_table_reads_cell_by_cell_and_nested_tables_once():
    inner = f"<w:tbl><w:tr><w:tc>{_p('Inner cell')}</w:tc></w:tr></w:tbl>"
    table = (
        "<w:tbl>"
        f"<w:tr><w:tc>{_p('2018–2022')}</w:tc><w:tc>{_p('Beispiel GmbH')}{inner}</w:tc></w:tr>"
        f"<w:tr><w:tc>{_p('2016–2018')}</w:tc><w:tc>{_p('Analyst')}</w:tc></w:tr>"
        "</w:tbl>"
    )
    md = docx_to_markdown(_docx(table))
    assert md == "2018–2022\n\nBeispiel GmbH\n\nInner cell\n\n2016–2018\n\nAnalyst\n"


def test_text_box_is_read_once_after_its_paragraph():
    # Word stores a text box twice: DrawingML in mc:Choice, VML in mc:Fallback.
    box = f"<w:txbxContent>{_p('Sidebar: Python, SQL')}</w:txbxContent>"
    anchored = (
        "<w:r><mc:AlternateContent>"
        f"<mc:Choice Requires=\"wps\"><w:drawing><wps:txbx>{box}</wps:txbx></w:drawing></mc:Choice>"
        f"<mc:Fallback><w:pict><v:textbox>{box}</v:textbox></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>"
    )
    md = docx_to_markdown(_docx(_p(runs=_r("Main text") + anchored)))
    assert md == "Main text\n\nSidebar: Python, SQL\n"


def test_links_and_bare_email():
    link = '<w:hyperlink r:id="rId9">' + _r("my notes") + "</w:hyperlink>"
    mail = '<w:hyperlink r:id="rId10">' + _r("jane@example.com") + "</w:hyperlink>"
    md = docx_to_markdown(_docx(_p(runs=_r("Portfolio: ") + link) + _p(runs=mail)))
    assert md == "Portfolio: [my notes](https://example.com/notes)\n\njane@example.com\n"


def test_field_instructions_are_dropped_and_their_result_kept():
    field = (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText> DATE \\@ "yyyy" </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        + _r("2026")
        + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )
    assert docx_to_markdown(_docx(_p(runs=_r("Updated ") + field))) == "Updated 2026\n"


def test_content_controls_and_line_breaks():
    sdt = f"<w:sdt><w:sdtPr/><w:sdtContent>{_p('Inside a control')}</w:sdtContent></w:sdt>"
    broken = _p(runs=_r("Line one") + "<w:r><w:br/></w:r>" + _r("line two") + '<w:r><w:br w:type="page"/></w:r>')
    assert docx_to_markdown(_docx(sdt + broken)) == "Inside a control\n\nLine one\nline two\n"


def test_header_contact_line_goes_after_the_name_once():
    contact = _p("Zürich · jane@example.com")
    md = docx_to_markdown(_docx(
        _p("Jane Example", "Titel") + _p("Summary text"),
        headers=[contact, contact],  # first-page and default headers repeat
    ))
    assert md == "# Jane Example\n\nZürich · jane@example.com\n\nSummary text\n"


def test_works_without_styles_or_relationships():
    assert docx_to_markdown(_docx(_p("Plain"), styles=None, rels=None)) == "Plain\n"


def test_unreadable_files_raise_conversion_errors():
    with pytest.raises(ConversionError, match="could not be read"):
        docx_to_markdown(b"not a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("content.xml", "<x/>")
    with pytest.raises(ConversionError, match="not a Word document"):
        docx_to_markdown(buf.getvalue())


# --- sync: what happens in profile/cvs/ -------------------------------------


def _cv(tmp_path, body="Jane Example", name="pm.docx"):
    (tmp_path / name).write_bytes(_docx(_p(body, "Titel")))
    return tmp_path / name


def test_word_cv_gets_a_markdown_cv_without_being_asked(tmp_path):
    _cv(tmp_path)
    [outcome] = sync_word_cvs(tmp_path)
    assert outcome.status == "made"
    assert "read it through" in outcome.message
    text = (tmp_path / "pm.md").read_text(encoding="utf-8")
    assert text.startswith("<!-- Made by jobradar from pm.docx")
    assert text.endswith("\n\n# Jane Example\n")


def test_unchanged_word_file_is_left_alone(tmp_path):
    _cv(tmp_path)
    sync_word_cvs(tmp_path)
    assert sync_word_cvs(tmp_path) == []


def test_updated_word_file_remakes_an_untouched_cv(tmp_path):
    _cv(tmp_path)
    sync_word_cvs(tmp_path)
    _cv(tmp_path, body="Jane Example-Muster")
    [outcome] = sync_word_cvs(tmp_path)
    assert outcome.status == "remade"
    assert "# Jane Example-Muster" in (tmp_path / "pm.md").read_text(encoding="utf-8")


def test_line_endings_are_not_edits(tmp_path):
    # git on Windows checks the file out with CRLF; that must not freeze it.
    _cv(tmp_path)
    sync_word_cvs(tmp_path)
    md = tmp_path / "pm.md"
    md.write_bytes(md.read_bytes().replace(b"\n", b"\r\n") + b"\r\n")
    _cv(tmp_path, body="Updated")
    assert [o.status for o in sync_word_cvs(tmp_path)] == ["remade"]


def test_edited_cv_is_kept_when_the_word_file_changes(tmp_path):
    _cv(tmp_path)
    sync_word_cvs(tmp_path)
    md = tmp_path / "pm.md"
    md.write_text(md.read_text(encoding="utf-8") + "\nA line I added.\n", encoding="utf-8")
    edited = md.read_text(encoding="utf-8")

    assert sync_word_cvs(tmp_path) == []  # nothing new in Word: nothing to say
    _cv(tmp_path, body="Updated in Word")
    [outcome] = sync_word_cvs(tmp_path)
    assert outcome.status == "kept_edits"
    assert "delete pm.md" in outcome.message
    assert md.read_text(encoding="utf-8") == edited


def test_hand_written_markdown_is_never_touched(tmp_path):
    _cv(tmp_path)
    (tmp_path / "pm.md").write_text("# My own\n", encoding="utf-8")
    assert sync_word_cvs(tmp_path) == []
    assert (tmp_path / "pm.md").read_text(encoding="utf-8") == "# My own\n"


def test_unreadable_or_empty_word_files_are_reported_not_raised(tmp_path):
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    (tmp_path / "scan.docx").write_bytes(_docx(_p("")))
    outcomes = {o.word.name: o for o in sync_word_cvs(tmp_path)}
    assert outcomes["broken.docx"].status == "failed"
    assert "could not be read" in outcomes["broken.docx"].message
    assert "no text" in outcomes["scan.docx"].message
    assert not (tmp_path / "broken.md").exists() and not (tmp_path / "scan.md").exists()


def test_old_doc_asks_for_a_resave_as_docx(tmp_path):
    (tmp_path / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0")
    [outcome] = sync_word_cvs(tmp_path)
    assert outcome.status == "failed"
    assert outcome.message.startswith("old.doc is in the old Word format")
    assert "save it as .docx" in outcome.message
    assert not (tmp_path / "old.md").exists()


def test_docx_wins_over_a_doc_of_the_same_name(tmp_path):
    # Someone who re-saved pm.doc as pm.docx and kept both: pm.doc sorts
    # first, and had it gone first it would fail and block pm.docx.
    _cv(tmp_path, name="pm.docx")
    (tmp_path / "pm.doc").write_bytes(b"x")
    assert [(o.word.name, o.status) for o in sync_word_cvs(tmp_path)] == [("pm.docx", "made")]
    assert sync_word_cvs(tmp_path) == []


def test_word_files_skip_lock_files_and_other_types(tmp_path):
    for name in ("a.docx", "b.DOC", "~$a.docx", "c.md", "d.pdf"):
        (tmp_path / name).write_bytes(b"")
    assert [p.name for p in word_files(tmp_path)] == ["a.docx", "b.DOC"]
    assert word_files(tmp_path / "missing") == []


def test_load_profile_reads_a_word_cv(tmp_path):
    from jobradar.matching import load_profile

    (tmp_path / "cvs").mkdir()
    _cv(tmp_path / "cvs", name="Product Manager.docx")
    cvs, _identity, _stories = load_profile(tmp_path)
    assert list(cvs) == ["Product Manager"]
    assert "# Jane Example" in cvs["Product Manager"]


def test_load_profile_names_the_word_file_it_could_not_read(tmp_path):
    from jobradar.matching import load_profile

    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "cv.docx").write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="cv.docx could not be read"):
        load_profile(tmp_path)
