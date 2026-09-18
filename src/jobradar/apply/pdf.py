"""Convert application markdown deliverables to PDF.

Chain: markdown -> styled HTML (pure-Python `markdown` package) -> PDF via a
headless Chromium/Chrome print, which is the only PDF engine reliably
present on this machine (no pandoc/LaTeX, no weasyprint system libs). When
no browser is found the styled HTML is kept next to the markdown instead,
so the fallback on any machine is: open it and print to PDF. On success the
intermediate HTML is deleted; the PDF is the tracked deliverable.

Used automatically for quick-tier applications (they get submitted as-is)
and exposed as `python -m jobradar.apply --pdf KEY` for full-tier ones,
where the user edits the markdown first and converts when done.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

import markdown as md

logger = logging.getLogger(__name__)

# Order matters: prefer the names most likely to be a real, current browser.
_BROWSERS = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "msedge",
    "brave-browser",
)

_CONVERT_TIMEOUT = 120.0  # snap browsers can be slow to cold-start

# A4, compact but readable: recruiter-friendly, fits the ~2-page CVs the
# tailoring prompt targets. Kept deliberately plain - the content should
# carry the application, not the styling.
_CSS = """\
@page { size: A4; margin: 15mm 17mm; }
* { box-sizing: border-box; }
body {
  font-family: "Helvetica Neue", Helvetica, Arial, "Liberation Sans", sans-serif;
  font-size: 10.5pt; line-height: 1.45; color: #1a1a1a; margin: 0;
}
h1 { font-size: 19pt; margin: 0 0 1mm; letter-spacing: 0.2px; }
h1 + p { margin-top: 0; color: #444; }
h2 {
  font-size: 12pt; text-transform: uppercase; letter-spacing: 0.8px;
  border-bottom: 1px solid #bbb; padding-bottom: 1mm; margin: 6mm 0 2.5mm;
}
h3 { font-size: 11pt; margin: 4mm 0 1mm; }
p { margin: 1.5mm 0; }
ul { margin: 1.5mm 0 2.5mm; padding-left: 5mm; }
li { margin: 0.8mm 0; }
a { color: #1a1a1a; text-decoration: none; }
strong { font-weight: 600; }
hr { border: none; border-top: 1px solid #ccc; margin: 4mm 0; }
li, h2, h3 { page-break-inside: avoid; }
/* One blank line the author left in the markdown for spacing (e.g. between a
   letterhead and the greeting). Markdown collapses runs of blank lines to a
   single block break, so each *extra* blank line is re-emitted as one of these
   spacers to keep the PDF's vertical rhythm matching the source. */
.vspace { height: 1em; }
"""

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
{body}
</body>
</html>
"""


# Matches a run of 2+ consecutive blank lines (3+ newlines, allowing
# whitespace-only lines). Standard markdown collapses these to one block break.
_BLANK_RUN = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def _expand_blank_lines(md_text: str) -> str:
    """Preserve intentional vertical spacing. Markdown treats any run of blank
    lines as a single block separator, so a deliberate gap (a double blank line
    between a letterhead and a greeting, say) renders identically to a single
    one. Re-emit each blank line beyond the first as a spacer div the CSS gives
    height to, keeping the PDF's spacing faithful to the markdown."""

    def repl(match: re.Match[str]) -> str:
        extra = match.group(0).count("\n") - 2  # one blank line = normal break
        return "\n\n" + '<div class="vspace"></div>\n\n' * extra

    return _BLANK_RUN.sub(repl, md_text)


# A bare URL: no markdown link syntax, no angle brackets. The lookbehind keeps
# us off URLs that are already inside a link - "](http...)", "<http...>",
# href="http...", `http...` - so only genuinely naked ones are wrapped.
_BARE_URL = re.compile(r"""(?<![(<\["'=`])\bhttps?://[^\s<>()\[\]"'`]+""")
# Sentence punctuation the model writes after a cited URL; part of the prose,
# not of the link.
_URL_TRAILERS = ".,;:!?"
_FENCE = re.compile(r"^\s*(```|~~~)")
# "[label]: https://..." - a link reference definition, already a link.
_LINK_DEF = re.compile(r"^\s*\[[^\]]+\]:")


def _autolink_bare_urls(md_text: str) -> str:
    """Wrap bare URLs in <> so markdown turns them into real anchors.

    Without this they render as plain text: the printed PDF then carries no
    link annotation, and a reader's viewer is left guessing where the URL ends
    - it guesses wrong the moment the URL wraps across a line, handing them a
    truncated address. The cover-letter prompt asks for an inline source URL,
    so this is the common case, not an edge one.
    """
    out: list[str] = []
    in_fence = False
    for line in md_text.splitlines(keepends=True):
        if _FENCE.match(line):
            in_fence = not in_fence
        if in_fence or _LINK_DEF.match(line):
            out.append(line)
            continue

        def repl(match: re.Match[str]) -> str:
            url = match.group(0).rstrip(_URL_TRAILERS)
            return f"<{url}>" + match.group(0)[len(url):]

        out.append(_BARE_URL.sub(repl, line))
    return "".join(out)


def markdown_to_html(md_text: str, title: str) -> str:
    # nl2br: single newlines become real line breaks - CVs and letters rely on
    # them (role/date lines, address blocks), unlike prose markdown.
    body = md.markdown(
        _autolink_bare_urls(_expand_blank_lines(md_text)),
        extensions=["extra", "sane_lists", "nl2br"],
    )
    return _HTML_TEMPLATE.format(title=title, css=_CSS, body=body)


def find_browser() -> str | None:
    for name in _BROWSERS:
        path = shutil.which(name)
        if path:
            return path
    return None


def _print_to_pdf(browser: str, html_path: Path, pdf_path: Path) -> bool:
    result = subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=_CONVERT_TIMEOUT,
    )
    if result.returncode != 0 or not pdf_path.exists():
        logger.warning(
            "PDF print failed for %s: %s", html_path.name, (result.stderr or "").strip()[-300:]
        )
        return False
    return True


def convert_markdown_file(md_path: Path, browser: str | None = None) -> Path | None:
    """Convert one .md file to a sibling .pdf. Returns the PDF path, or the
    kept .html path (styled, print-ready) when no browser could do the print,
    or None when even writing the HTML failed.
    """
    if browser is None:
        browser = find_browser()
    md_path = md_path.resolve()  # as_uri() below needs an absolute path
    html_path = md_path.with_suffix(".html")
    pdf_path = md_path.with_suffix(".pdf")
    try:
        html = markdown_to_html(
            md_path.read_text(encoding="utf-8"), title=md_path.stem.replace("_", " ")
        )
        html_path.write_text(html, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not render %s to HTML: %s", md_path, exc)
        return None

    if browser is None:
        logger.warning(
            "No Chromium/Chrome found; kept %s - open it in a browser and print to PDF",
            html_path.name,
        )
        return html_path
    try:
        ok = _print_to_pdf(browser, html_path, pdf_path)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("PDF print failed for %s: %s", md_path.name, exc)
        ok = False
    if not ok:
        return html_path  # keep the HTML as the manual-print fallback
    html_path.unlink(missing_ok=True)
    return pdf_path


# The deliverables worth converting; notes.md and job_description.md are
# working documents, not submission material.
_CONVERTIBLE = ("tailored_cv.md", "cover_letter.md")


def convertible_files(folder: Path) -> list[Path]:
    """Submission markdown in a folder: the base CV and cover letter, plus any
    translated CV (tailored_cv_<lang>.md) produced for a non-English posting.

    Public because the linter (`lint.py`) shares this definition of "what gets
    submitted" when it checks that every deliverable has a current PDF."""
    paths: list[Path] = [folder / name for name in _CONVERTIBLE]
    paths += sorted(folder.glob("tailored_cv_*.md"))
    seen: set[Path] = set()
    ordered: list[Path] = []
    for p in paths:
        if p.exists() and p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def convert_folder(folder: Path) -> list[Path]:
    """Convert an application folder's submission documents. Returns the
    produced files (PDFs, or kept HTMLs when printing wasn't possible)."""
    produced: list[Path] = []
    browser = find_browser()
    for md_path in convertible_files(folder):
        out = convert_markdown_file(md_path, browser=browser)
        if out is not None:
            produced.append(out)
            logger.info("Wrote %s", out)
    return produced
