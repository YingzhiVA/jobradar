from jobradar.apply import pdf


def test_markdown_to_html_renders_content_and_style():
    html = pdf.markdown_to_html("# Jane Doe\n\n- Shipped **things**\n", title="tailored cv")
    assert "<!DOCTYPE html>" in html
    assert "<title>tailored cv</title>" in html
    assert "<h1>Jane Doe</h1>" in html
    assert "<strong>things</strong>" in html
    assert "@page" in html  # print stylesheet embedded


def test_markdown_to_html_preserves_single_line_breaks():
    # Role/date lines and letter address blocks rely on soft line breaks.
    html = pdf.markdown_to_html("**Acme** - *PM*\nMay 2024, Zurich\n", title="cv")
    assert "<br" in html


def test_markdown_to_html_preserves_extra_blank_lines():
    # A double blank line (letterhead -> greeting) is an intentional gap that
    # plain markdown would collapse to a normal paragraph break.
    html = pdf.markdown_to_html("Name\ncontact\n\n\nDear team,\n", title="cl")
    assert html.count('<div class="vspace"></div>') == 1
    assert ".vspace" in html  # spacer has height in the stylesheet


def test_markdown_to_html_single_blank_line_has_no_spacer():
    # One blank line is an ordinary paragraph break - no extra spacing.
    html = pdf.markdown_to_html("Para one.\n\nPara two.\n", title="cl")
    assert "vspace" not in html.split("<body>")[1]


def test_bare_url_becomes_a_real_link():
    # The cover-letter prompt asks for an inline source URL, which arrives
    # bare. Left as plain text it carries no PDF link annotation, so a viewer
    # guesses where it ends - and guesses wrong once the URL wraps a line.
    html = pdf.markdown_to_html(
        "See https://example.com/notes/a-b/, where I explain.\n", title="cl"
    )
    assert '<a href="https://example.com/notes/a-b/">' in html
    # Sentence punctuation stays prose, outside the link.
    assert "a-b/</a>," in html


def test_existing_markdown_links_are_left_alone():
    html = pdf.markdown_to_html(
        "[Portfolio](https://example.com/) and <https://example.org/>\n", title="cv"
    )
    assert '<a href="https://example.com/">Portfolio</a>' in html
    assert html.count("<a href=") == 2  # not double-wrapped
    assert "&lt;" not in html


def test_autolink_skips_code_and_reference_definitions():
    src = "```\nhttps://example.com/in-code\n```\n\n[ref]: https://example.com/ref\n"
    assert pdf._autolink_bare_urls(src) == src
    # Inline code spans too.
    assert pdf._autolink_bare_urls("`https://example.com/x`") == "`https://example.com/x`"


def test_convert_keeps_html_when_no_browser(tmp_path, monkeypatch):
    md_path = tmp_path / "tailored_cv.md"
    md_path.write_text("# Jane\n\ncontent\n", encoding="utf-8")
    monkeypatch.setattr(pdf, "find_browser", lambda: None)
    out = pdf.convert_markdown_file(md_path)
    assert out == md_path.with_suffix(".html")
    assert out.exists()
    assert not md_path.with_suffix(".pdf").exists()


def test_convert_folder_targets_submission_docs_only(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "find_browser", lambda: None)
    for name in ("tailored_cv.md", "cover_letter.md", "notes.md", "job_description.md"):
        (tmp_path / name).write_text("# x\n\ny\n", encoding="utf-8")
    produced = pdf.convert_folder(tmp_path)
    assert sorted(p.name for p in produced) == ["cover_letter.html", "tailored_cv.html"]
    assert not (tmp_path / "notes.html").exists()


def test_convert_folder_includes_translated_cv(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "find_browser", lambda: None)
    for name in ("tailored_cv.md", "tailored_cv_de.md", "cover_letter.md", "notes.md"):
        (tmp_path / name).write_text("# x\n\ny\n", encoding="utf-8")
    produced = sorted(p.name for p in pdf.convert_folder(tmp_path))
    assert produced == ["cover_letter.html", "tailored_cv.html", "tailored_cv_de.html"]


def test_convert_folder_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "find_browser", lambda: None)
    assert pdf.convert_folder(tmp_path) == []


def test_convert_falls_back_to_html_when_print_fails(tmp_path, monkeypatch):
    md_path = tmp_path / "tailored_cv.md"
    md_path.write_text("# Jane\n\ncontent\n", encoding="utf-8")
    monkeypatch.setattr(pdf, "_print_to_pdf", lambda browser, h, p: False)
    out = pdf.convert_markdown_file(md_path, browser="/usr/bin/false-browser")
    assert out == md_path.with_suffix(".html")
    assert out.exists()
