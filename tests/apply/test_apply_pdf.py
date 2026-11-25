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


def test_glued_role_heading_is_split_onto_two_lines():
    # A Word export dropped the tab between title and dates; the model copies
    # whatever layout the base CV has, separators or none.
    glued = r"**Acme AG** *\- Product Owner (report to CEO)May 2024 \- August 2026, Zurich*"
    want = "**Acme AG** *\\- Product Owner (report to CEO)*\n*May 2024 \\- August 2026, Zurich*\n"
    assert pdf.split_role_periods(glued + "\n") == want
    for sep in ("  ", " | ", ", "):
        line = rf"**Acme AG** *\- Product Owner (report to CEO){sep}May 2024 \- August 2026, Zurich*"
        assert pdf.split_role_periods(line + "\n") == want
    assert pdf.split_role_periods(want) == want  # idempotent


def test_german_role_heading_is_split():
    line = "**Acme AG** *- Product Owner | Mai 2024 - heute | Zürich*"
    assert pdf.split_role_periods(line) == "**Acme AG** *- Product Owner*\n*Mai 2024 - heute | Zürich*"


def test_non_heading_lines_are_not_split():
    src = (
        "**Domain:** Financial services, Medical devices\n"
        "**jobradar** (personal project): Released in May 2024 as open source.\n"
    )
    assert pdf.split_role_periods(src) == src


def test_period_line_is_styled():
    html = pdf.markdown_to_html(
        "**Acme AG** *- Product Owner*\n*May 2024 - August 2026, Zurich*\n", title="cv"
    )
    assert '<br />\n<span class="cv-period">May 2024 - August 2026, Zurich</span>' in html
    assert ".cv-period" in html
    # A plain (non-italic) period line under a heading gets the same style.
    html = pdf.markdown_to_html("**Acme AG** - *PM*\nMay 2024 - Present, Zurich\n", title="cv")
    assert '<span class="cv-period">May 2024 - Present, Zurich</span>' in html


def test_italic_line_under_heading_that_is_not_a_period_stays_italic():
    html = pdf.markdown_to_html("**Acme AG** *- PM*\n*Led two product initiatives.*\n", title="cv")
    assert "cv-period" not in html.split("<body>")[1]


def test_a_print_that_leaves_the_old_pdf_in_place_is_a_failure(tmp_path, monkeypatch):
    """A browser that exits 0 without writing (a confined snap Chromium) must
    not have the previous run's PDF counted as this run's."""
    import subprocess

    html = tmp_path / "cv.html"
    html.write_text("<p>cv</p>", encoding="utf-8")
    old = tmp_path / "cv.pdf"
    old.write_bytes(b"%PDF old")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    assert pdf._print_to_pdf("chromium", html, old) is False

    def writes(*args, **kwargs):
        old.write_bytes(b"%PDF new")
        import os
        os.utime(old, ns=(old.stat().st_atime_ns, old.stat().st_mtime_ns + 1_000_000))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", writes)
    assert pdf._print_to_pdf("chromium", html, old) is True


def test_a_browser_off_path_is_found_where_windows_installs_it(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf.shutil, "which", lambda name: None)
    monkeypatch.setattr(pdf.sys, "platform", "win32")
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / var))
    assert pdf.find_browser() is None
    edge = tmp_path / "PROGRAMFILES(X86)" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.touch()
    assert pdf.find_browser() == str(edge)
    chrome = tmp_path / "LOCALAPPDATA" / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    assert pdf.find_browser() == str(chrome)  # Chrome first, as on PATH


def test_a_browser_off_path_is_found_in_the_mac_applications_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf.shutil, "which", lambda name: None)
    monkeypatch.setattr(pdf.sys, "platform", "darwin")
    monkeypatch.setattr(pdf.Path, "home", lambda: tmp_path)
    app = tmp_path / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    found = pdf.find_browser()
    if found is None or not found.startswith("/Applications/"):  # a real Mac may have one
        app.parent.mkdir(parents=True)
        app.touch()
        assert pdf.find_browser() == str(app)


# --- CV PDF name and link styling -------------------------------------------


def _cv(tmp_path, name="tailored_cv.md", first_line="**Jane van Doe**"):
    md_path = tmp_path / name
    md_path.write_text(f"{first_line}\n\njane@example.com | [LinkedIn](https://example.com)\n", encoding="utf-8")
    return md_path


def test_cv_pdf_is_named_for_family_name_and_position(tmp_path):
    md_path = _cv(tmp_path)
    assert pdf.pdf_path_for(md_path, "Staff Product Manager, Machine Learning").name == (
        "CV_van_Doe_Staff_Product_Manager_Machine_Learning.pdf"
    )
    # A heading works as well as a bold line.
    assert pdf.pdf_path_for(_cv(tmp_path, first_line="# Jane Doe"), "PM").name == "CV_Doe_PM.pdf"


def test_cv_pdf_name_drops_title_noise_and_folds_umlauts(tmp_path):
    name = pdf.pdf_path_for(_cv(tmp_path), "Data Scientist (m/w/d) 80-100% Zürich & Genève").name
    assert name == "CV_van_Doe_Data_Scientist_Zuerich_Geneve.pdf"


def test_translated_cv_pdf_ends_in_its_language(tmp_path):
    md_path = _cv(tmp_path, "tailored_cv_de.md")
    assert pdf.pdf_path_for(md_path, "Produktmanager").name == "CV_van_Doe_Produktmanager_DE.pdf"


def test_pdf_name_falls_back_to_the_markdown_name(tmp_path):
    md_path = _cv(tmp_path)
    assert pdf.pdf_path_for(md_path, None).name == "tailored_cv.pdf"  # no title known
    contact_first = _cv(tmp_path, first_line="jane@example.com | +41 79 000 00 00")
    assert pdf.pdf_path_for(contact_first, "PM").name == "tailored_cv.pdf"  # no name line
    cover = tmp_path / "cover_letter.md"
    cover.write_text("# Jane Doe\n", encoding="utf-8")
    assert pdf.pdf_path_for(cover, "PM").name == "cover_letter.pdf"  # only the CV is renamed


def test_convert_writes_named_cv_and_removes_the_generic_pdf(tmp_path, monkeypatch):
    md_path = _cv(tmp_path)
    old = md_path.with_suffix(".pdf")
    old.write_bytes(b"%PDF-old")
    printed = []

    def fake_print(browser, html_path, pdf_path):
        assert "<title>CV van Doe PM</title>" in html_path.read_text(encoding="utf-8")
        pdf_path.write_bytes(b"%PDF")
        printed.append(pdf_path)
        return True

    monkeypatch.setattr(pdf, "_print_to_pdf", fake_print)
    out = pdf.convert_markdown_file(md_path, browser="chromium", title="PM")
    assert out == tmp_path / "CV_van_Doe_PM.pdf" == printed[0]
    assert out.exists() and not old.exists()


def test_links_stand_out_in_the_pdf():
    html = pdf.markdown_to_html("[Portfolio](https://example.com/)\n", title="cv")
    style = html.split("<style>")[1].split("</style>")[0]
    rule = next(line for line in style.splitlines() if line.startswith("a {"))
    assert "text-decoration: underline" in rule and "#1a1a1a" not in rule
