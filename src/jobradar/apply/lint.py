"""Deterministic pre-submission linter for application folders.

Every check here is true-or-false and needs no API call: it cross-checks a
drafted application folder against its tracker entry (applications.json) and the
files on disk, catching the mechanical mistakes a polished-looking draft hides -
a missing deliverable, a PDF left stale after the markdown was edited, a German
posting whose translated CV never got written.

This is the *linter*: quality judgements (is a claim supported, is the tone
right) are deliberately out of scope - they are probabilistic and would belong
in a separate tool. The rules here are deterministic, so they are unit-testable;
see tests/apply/test_apply_lint.py for a fixture per failure mode.

Scope so far: Group A (folder <-> tracker integrity), Group B (PDF staleness),
and Group C (content mechanics: target company named, no leftover placeholders
or cross-application contamination, contact block intact, style-rule slips,
each CV in the language its filename promises).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import pdf
from .tailor import language_name
from .tracker import (
    STATUS_DRAFTED,
    STATUS_NEEDS_JD,
    STATUS_SUBMITTED,
    TERMINAL_STATUSES,
    Application,
    Tracker,
)


class Severity(str, Enum):
    ERROR = "error"  # a real mistake; blocks submission once the gate is wired
    WARN = "warn"  # worth a look, not necessarily wrong
    INFO = "info"


_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARN: 1, Severity.INFO: 2}

# Group B findings a PDF conversion resolves, so the --pdf gate excludes them:
# blocking conversion on "no PDF yet" would be circular.
PDF_STALENESS_CODES = frozenset({"missing-pdf", "stale-pdf", "html-leftover"})

# Git does not preserve or order mtimes, so a fresh clone can leave a committed
# PDF looking a hair older than its (also committed) markdown. A real "edited the
# markdown, forgot to regenerate" gap is minutes or more; a checkout race is
# sub-second. Only flag staleness beyond this grace window.
_PDF_STALE_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class LintFinding:
    severity: Severity
    code: str  # stable slug, e.g. "stale-pdf" - lets tests assert on the rule
    message: str
    folder: str  # application folder name the finding is about


def has_errors(findings: list[LintFinding]) -> bool:
    return any(f.severity is Severity.ERROR for f in findings)


def blocking_errors(findings: list[LintFinding]) -> list[LintFinding]:
    """Error-severity findings that should stop an action."""
    return [f for f in findings if f.severity is Severity.ERROR]


def drop_codes(findings: list[LintFinding], codes: frozenset[str]) -> list[LintFinding]:
    """Findings minus the ones the action about to run resolves by itself. The
    --pdf gate drops PDF_STALENESS_CODES so conversion neither blocks on nor
    reports the very PDFs it is about to write."""
    return [f for f in findings if f.code not in codes]


def sort_findings(findings: list[LintFinding]) -> list[LintFinding]:
    """Group by folder, most severe first - the order to print them in."""
    return sorted(findings, key=lambda f: (f.folder, _SEVERITY_ORDER[f.severity]))


def lint_folder(
    folder: Path,
    app: Application | None,
    *,
    other_companies: set[str] | None = None,
    strict: bool = False,
    cvs_dir: Path | None = None,
) -> list[LintFinding]:
    """Group A (integrity) + Group B (staleness) + Group C (content) checks.

    `app` is the tracker entry for this folder, or None when the folder has no
    tracker row (lint_repo flags that separately); app-dependent checks are
    simply skipped in that case. `other_companies` is the set of companies from
    *other* applications, used to catch a cover letter contaminated with text
    left over from a different posting. `strict` adds the notes.md checklist
    check - meaningful only right before submitting, so it is opt-in. `cvs_dir`
    is profile/cvs, needed to compare a tailored CV against the base CV it came
    from; omit it to skip that comparison.
    """
    findings: list[LintFinding] = []
    if app is not None:
        findings += _check_required_files(folder, app)
        findings += _check_translation(folder, app)
        findings += _check_status_coherence(folder, app)
    findings += _check_pdfs(folder, app)

    # Group C reads the submission documents once (the same set that gets a PDF).
    docs = {p: p.read_text(encoding="utf-8") for p in pdf.convertible_files(folder)}
    cover = folder / "cover_letter.md"
    cover_text = docs.get(cover)
    cv_docs = {p: t for p, t in docs.items() if p.name.startswith("tailored_cv")}
    if app is not None and cover_text is not None:
        findings += _check_company(folder, cover_text, app, other_companies or set())
    findings += _check_contact(folder, cv_docs)
    findings += _check_cv_language(folder, cv_docs)
    findings += _check_placeholders(folder, docs)
    findings += _check_style(folder, docs)
    if app is not None and cvs_dir is not None:
        findings += _check_bullet_cuts(folder, app, cvs_dir)
    if cover_text is not None:
        findings += _check_cover_letter_length(folder, cover_text)
    if strict and app is not None:
        findings += _check_checklist(folder)
    return findings


def lint_repo(
    applications_dir: Path,
    tracker: Tracker,
    *,
    strict: bool = False,
    cvs_dir: Path | None = None,
) -> list[LintFinding]:
    """Cross-folder integrity plus a full lint_folder pass over every tracked
    application. Catches tracker/filesystem drift that no single folder can see."""
    findings: list[LintFinding] = []
    by_folder = {app.folder: app for app in tracker.applications}
    all_companies = {a.company for a in tracker.applications if a.company}

    # Dangling entries: a tracker row whose folder is gone.
    for app in tracker.applications:
        if not (applications_dir / app.folder).is_dir():
            findings.append(
                LintFinding(
                    Severity.ERROR,
                    "dangling-entry",
                    f"tracker entry points to applications/{app.folder}, which is not "
                    "on disk",
                    app.folder,
                )
            )

    if not applications_dir.is_dir():
        return findings

    for child in sorted(applications_dir.iterdir()):
        if not child.is_dir():
            continue  # applications.json, queue.txt, README.md, etc.
        if child.name == "archive":
            continue  # archived applications; real folders are uid-prefixed
        app = by_folder.get(child.name)
        if app is None:
            findings.append(
                LintFinding(
                    Severity.WARN,
                    "orphan-folder",
                    "folder has no entry in applications.json; it is invisible to "
                    "--rav, --pdf, and --mark-submitted",
                    child.name,
                )
            )
            continue
        findings += lint_folder(
            child,
            app,
            other_companies=all_companies - {app.company},
            strict=strict,
            cvs_dir=cvs_dir,
        )
    return findings


# --- Group A: folder <-> tracker integrity ---------------------------------


def _check_required_files(folder: Path, app: Application) -> list[LintFinding]:
    if app.status != STATUS_DRAFTED:
        return []
    findings: list[LintFinding] = []
    required = ["tailored_cv.md", "notes.md"]
    if app.cover_letter:
        required.append("cover_letter.md")
    for name in required:
        if not (folder / name).exists():
            extra = " (tracker records a cover letter for this posting)" if name == "cover_letter.md" else ""
            findings.append(
                LintFinding(
                    Severity.ERROR,
                    "missing-deliverable",
                    f"{name} is missing but the tracker marks this application drafted{extra}",
                    folder.name,
                )
            )
    return findings


def _check_translation(folder: Path, app: Application) -> list[LintFinding]:
    findings: list[LintFinding] = []
    lang = app.cv_translation_language
    if lang:
        expected = folder / f"tailored_cv_{lang}.md"
        if not expected.exists():
            findings.append(
                LintFinding(
                    Severity.ERROR,
                    "translation-mismatch",
                    f"tracker records a {lang} CV translation but {expected.name} is "
                    f"missing; {expected.name} is the version submitted for this posting",
                    folder.name,
                )
            )
    else:
        for stray in sorted(folder.glob("tailored_cv_*.md")):
            findings.append(
                LintFinding(
                    Severity.WARN,
                    "translation-mismatch",
                    f"{stray.name} exists but the tracker records no CV translation "
                    "(cv_translation_language is unset): stale file or lost tracker state",
                    folder.name,
                )
            )
    return findings


def _check_status_coherence(folder: Path, app: Application) -> list[LintFinding]:
    findings: list[LintFinding] = []
    if app.status == STATUS_SUBMITTED and not app.submitted_on:
        findings.append(
            LintFinding(
                Severity.ERROR,
                "status-timestamp",
                "status is submitted but submitted_on is empty; it will not count in --rav",
                folder.name,
            )
        )
    if app.status in (STATUS_NEEDS_JD, STATUS_DRAFTED) and app.submitted_on:
        # Terminal statuses legitimately keep their submitted_on (it is still
        # the RAV proof date); only pre-submission statuses make it incoherent.
        findings.append(
            LintFinding(
                Severity.WARN,
                "status-timestamp",
                f"has a submitted_on date ({app.submitted_on}) but status is {app.status}",
                folder.name,
            )
        )
    if app.status in TERMINAL_STATUSES and not app.closed_on:
        findings.append(
            LintFinding(
                Severity.WARN,
                "status-timestamp",
                f"status is {app.status} but closed_on is empty; use --mark-{app.status}",
                folder.name,
            )
        )
    if app.closed_on and app.status not in TERMINAL_STATUSES:
        findings.append(
            LintFinding(
                Severity.WARN,
                "status-timestamp",
                f"has a closed_on date ({app.closed_on}) but status is {app.status}, "
                "which is not a terminal outcome",
                folder.name,
            )
        )
    if app.status == STATUS_NEEDS_JD and (folder / "tailored_cv.md").exists():
        findings.append(
            LintFinding(
                Severity.WARN,
                "status-timestamp",
                "status is needs_jd (waiting for a pasted job description) yet a "
                "tailored_cv.md already exists",
                folder.name,
            )
        )
    return findings


# --- Group B: PDF staleness -------------------------------------------------


def _check_pdfs(folder: Path, app: Application | None) -> list[LintFinding]:
    findings: list[LintFinding] = []
    tier = app.tier if app is not None else None
    # What the user would type to fix it: the short id when we know it.
    key = app.uid if app is not None and app.uid else folder.name
    for md_path in pdf.convertible_files(folder):
        pdf_path = md_path.with_suffix(".pdf")
        html_path = md_path.with_suffix(".html")
        if html_path.exists():
            findings.append(
                LintFinding(
                    Severity.ERROR,
                    "html-leftover",
                    f"{html_path.name} is present, so the PDF print fell back to HTML: "
                    f"there is no submittable {pdf_path.name}. Re-run --pdf on a machine "
                    "with Chromium/Chrome, or print the HTML by hand.",
                    folder.name,
                )
            )
            continue
        if not pdf_path.exists():
            # Full-tier drafts are edited before conversion, so a missing PDF is
            # expected until --pdf is run; quick-tier PDFs are generated
            # automatically, so a missing one is a real anomaly.
            severity = Severity.WARN if tier == "full" else Severity.ERROR
            findings.append(
                LintFinding(
                    severity,
                    "missing-pdf",
                    f"{md_path.name} has no {pdf_path.name}; run --pdf {key} "
                    "before submitting",
                    folder.name,
                )
            )
            continue
        if md_path.stat().st_mtime - pdf_path.stat().st_mtime > _PDF_STALE_GRACE_SECONDS:
            findings.append(
                LintFinding(
                    Severity.ERROR,
                    "stale-pdf",
                    f"{pdf_path.name} is older than {md_path.name}: the markdown was "
                    f"edited after the PDF was generated. Re-run --pdf {key}.",
                    folder.name,
                )
            )
    return findings


# --- Group C: content mechanics ---------------------------------------------

# Legal-form and generic suffixes stripped before matching a company name, so a
# "Deloitte AG" tracker entry still matches a letter that just says "Deloitte".
_CORP_SUFFIXES = {
    "ag", "gmbh", "sa", "sarl", "inc", "ltd", "llc", "plc", "co", "corp", "se",
    "bv", "nv", "oy", "kg", "srl", "spa", "group", "holding", "holdings",
}
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# A bracketed token NOT immediately followed by "(" - i.e. not markdown link
# syntax [text](url), which is how the CVs render LinkedIn/Portfolio links.
_BRACKET_RE = re.compile(r"\[[^\]\n]{1,40}\](?!\()")
_MARKER_RE = re.compile(r"(?<![A-Za-z])(TODO|FIXME|XXX|lorem ipsum)(?![A-Za-z])", re.IGNORECASE)

# Filler the tailoring prompt bans; kept to multi-word phrases that are almost
# never backed by a concrete fact, so single ambiguous words (a legitimate
# "dynamic pricing", "passionate about X") do not trip a false warning.
_FILLER_PHRASES = (
    "proven track record", "results-driven", "team player", "hit the ground running",
    "think outside the box", "go-getter", "detail-oriented", "self-starter", "synergy",
)
_COVER_LETTER_WORD_CAP = 400  # ~300-word body plus salutation/sign-off, with headroom


def _company_tokens(name: str) -> list[str]:
    """Significant name tokens (len >= 3, not a legal suffix) to look for in a
    letter - lenient, so a genuinely-named company is never falsely flagged."""
    return [t for t in _WORD_RE.findall(name) if len(t) >= 3 and t.lower() not in _CORP_SUFFIXES]


def _company_core(name: str) -> str:
    """Suffix-stripped name for a strict contamination substring match."""
    return " ".join(t for t in _WORD_RE.findall(name) if t.lower() not in _CORP_SUFFIXES)


def _contains_word(haystack_lower: str, needle_lower: str) -> bool:
    if not needle_lower:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle_lower)}(?![a-z0-9])", haystack_lower) is not None


def _check_company(
    folder: Path, cover_text: str, app: Application, other_companies: set[str]
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    lower = cover_text.lower()
    tokens = _company_tokens(app.company)
    if tokens and not any(_contains_word(lower, t.lower()) for t in tokens):
        # A WARN, not an ERROR: absence of the tracked name is a soft signal, not
        # proof - the letter may legitimately use an acronym or short form the
        # tracker doesn't hold (ZKB for "Zuercher Kantonalbank"). A letter that
        # names a *different* applied-to company is the strong signal, and
        # company-contamination below catches that. So don't hard-block --pdf here.
        findings.append(
            LintFinding(
                Severity.WARN,
                "company-missing",
                f"cover letter never names the target company ({app.company!r}); it may "
                "be a letter left over from another posting",
                folder.name,
            )
        )
    current_core = _company_core(app.company).lower()
    for other in sorted(other_companies):
        core = _company_core(other).lower()
        if not core or core == current_core:
            continue
        if _contains_word(lower, core):
            findings.append(
                LintFinding(
                    Severity.WARN,
                    "company-contamination",
                    f"cover letter mentions {other!r}, another company you applied to; "
                    "confirm this is a deliberate reference, not leftover text",
                    folder.name,
                )
            )
    return findings


# A markdown list item: "* " or "- " and then something. Deliberately not
# matching "**Bold:**" (no space after the first star) or a "---" rule, both of
# which the CVs use outside of any list.
_BULLET_RE = re.compile(r"^\s*[*-]\s+\S", re.MULTILINE)


def _count_bullets(text: str) -> int:
    return len(_BULLET_RE.findall(text))


def _check_bullet_cuts(folder: Path, app: Application, cvs_dir: Path) -> list[LintFinding]:
    """Did tailoring actually subtract anything?

    Judging whether the *right* bullets were cut is a quality question and out
    of this module's scope. Whether any were cut at all is not: it is two file
    reads and a comparison. It earns a check because the failure it catches is
    silent and was the normal case before the tailoring prompt was rewritten to
    force a cut - a CV that reads beautifully because every strong bullet
    survived, including the ones about work the posting has no use for.

    Only the English original is counted. A translated CV is a structural copy
    of it, so counting that too would just report the same finding twice.
    """
    tailored_path = folder / "tailored_cv.md"
    if not app.base_cv or not tailored_path.exists():
        return []
    base_path = cvs_dir / f"{app.base_cv}.md"
    if not base_path.is_file():
        return []  # base CV renamed or removed since drafting; nothing to compare
    base_bullets = _count_bullets(base_path.read_text(encoding="utf-8"))
    tailored_bullets = _count_bullets(tailored_path.read_text(encoding="utf-8"))
    if not base_bullets or tailored_bullets < base_bullets:
        return []
    return [
        LintFinding(
            Severity.WARN,
            "no-bullets-cut",
            f"tailored_cv.md keeps all {base_bullets} bullets of {app.base_cv} "
            f"({tailored_bullets} in the tailored CV): it was reordered and rephrased "
            "but never trimmed. Check it for bullets this posting has no use for, "
            "and see the 'What the tailored CV leaves out' section of notes.md",
            folder.name,
        )
    ]


def _check_contact(folder: Path, cv_docs: dict[Path, str]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path, text in cv_docs.items():
        if not _EMAIL_RE.search(text):
            findings.append(
                LintFinding(
                    Severity.ERROR,
                    "missing-contact",
                    f"{path.name} contains no email address; the contact header may have "
                    "been dropped in tailoring",
                    folder.name,
                )
            )
    return findings


def _check_placeholders(folder: Path, docs: dict[Path, str]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for path, text in docs.items():
        for match in _BRACKET_RE.finditer(text):
            token = match.group(0)
            if "hiring manager" in token.lower():
                findings.append(
                    LintFinding(
                        Severity.WARN,
                        "unresolved-hiring-manager",
                        f'{path.name} still addresses a generic "{token}"; the prompt uses '
                        "this only when the posting names no contact, so confirm that holds",
                        folder.name,
                    )
                )
            else:
                findings.append(
                    LintFinding(
                        Severity.ERROR,
                        "placeholder",
                        f"{path.name} contains an unresolved placeholder {token}",
                        folder.name,
                    )
                )
        for match in _MARKER_RE.finditer(text):
            findings.append(
                LintFinding(
                    Severity.ERROR,
                    "placeholder",
                    f"{path.name} contains a leftover marker {match.group(0)!r}",
                    folder.name,
                )
            )
    return findings


# Function words that mark a document's language, matched whole-word. A CV runs
# to hundreds of them, so a plain majority is decisive: the German place names
# and employer names in an English CV never outvote its articles.
_LANGUAGE_MARKERS = {
    "en": ("the", "and", "with", "for", "from", "of", "in", "at", "as", "on", "by"),
    "de": ("der", "die", "das", "und", "mit", "für", "von", "bei", "auf", "im", "zur"),
    "fr": ("le", "les", "et", "des", "pour", "avec", "dans", "sur", "du", "au", "une"),
    "it": ("il", "lo", "gli", "di", "con", "per", "dei", "nel", "su", "delle", "una"),
}
# Below this many marker hits the document is too short to call (a stub CV, a
# heading-only draft), and a close top-two margin means a mixed document - a
# German CV that kept its English section headings. Silence beats a false flag.
_LANGUAGE_MIN_MARKERS = 20
_LANGUAGE_MARGIN = 1.5
_WORDS_RE = re.compile(r"[A-Za-zÀ-ÿ]+")


def _dominant_language(text: str) -> str | None:
    """Best guess at which language a document is written in, or None when it
    is too short or too close to call. Deterministic, so it is unit-testable
    and belongs here rather than behind a model call."""
    words = [w.lower() for w in _WORDS_RE.findall(text)]
    scores = sorted(
        ((sum(words.count(m) for m in markers), code) for code, markers in _LANGUAGE_MARKERS.items()),
        reverse=True,
    )
    (top, best), (second, _) = scores[0], scores[1]
    if top < _LANGUAGE_MIN_MARKERS or top < second * _LANGUAGE_MARGIN:
        return None
    return best


def _expected_cv_language(name: str) -> str:
    """tailored_cv.md is the base CV's language (English); tailored_cv_de.md is
    the translated copy, and says so in its own name."""
    return name[len("tailored_cv") :].removesuffix(".md").lstrip("_") or "en"


def _check_cv_language(folder: Path, cv_docs: dict[Path, str]) -> list[LintFinding]:
    """Each CV in the language its filename promises. The tailoring call writes
    the CV and the cover letter in one go, and when the posting is German it has
    written the CV in German too - after which the translation step "translates"
    German into German, leaving two German CVs and no English original. Silent
    to read past, since both files look perfectly good on their own.

    A warning, not an error: the guess is a heuristic, and a heuristic should
    not block a submission on its own say-so.
    """
    findings: list[LintFinding] = []
    for path, text in sorted(cv_docs.items()):
        expected = _expected_cv_language(path.name)
        if expected not in _LANGUAGE_MARKERS:
            continue  # no marker set for it; nothing to check against
        actual = _dominant_language(text)
        if actual is None or actual == expected:
            continue
        findings.append(
            LintFinding(
                Severity.WARN,
                "cv-language",
                f"{path.name} reads as {language_name(actual)}, but should be in "
                f"{language_name(expected)}: the tailored CV keeps the base CV's "
                "language and the translated copy is a separate file",
                folder.name,
            )
        )
    return findings


def _check_style(folder: Path, docs: dict[Path, str]) -> list[LintFinding]:
    """Style-rule slips the tailoring prompt tries to avoid: em-dashes (a common
    AI-writing tell) and generic filler phrases."""
    findings: list[LintFinding] = []
    for path, text in docs.items():
        if "—" in text:
            findings.append(
                LintFinding(
                    Severity.WARN,
                    "em-dash",
                    f"{path.name} contains an em-dash; the style rules call for hyphens, "
                    "commas, or parentheses instead (em-dashes read as AI-written)",
                    folder.name,
                )
            )
        lower = text.lower()
        for phrase in _FILLER_PHRASES:
            if phrase in lower:
                findings.append(
                    LintFinding(
                        Severity.WARN,
                        "filler",
                        f"{path.name} uses the filler phrase {phrase!r}, which the style "
                        "rules avoid unless backed by a concrete fact",
                        folder.name,
                    )
                )
    return findings


def _check_cover_letter_length(folder: Path, cover_text: str) -> list[LintFinding]:
    words = len(cover_text.split())
    if words > _COVER_LETTER_WORD_CAP:
        return [
            LintFinding(
                Severity.WARN,
                "cover-letter-length",
                f"cover_letter.md is {words} words; the target is a one-page letter of "
                f"~300 (soft cap {_COVER_LETTER_WORD_CAP})",
                folder.name,
            )
        ]
    return []


# --- strict-only (opt-in) ---------------------------------------------------

_UNTICKED_RE = re.compile(r"^\s*- \[ \]", re.MULTILINE)


def _check_checklist(folder: Path) -> list[LintFinding]:
    """Every `- [ ]` in notes.md still unticked. Off by default (a fresh draft
    is all-unticked by design); only meaningful as a pre-submission gate."""
    notes = folder / "notes.md"
    if not notes.exists():
        return []
    unticked = len(_UNTICKED_RE.findall(notes.read_text(encoding="utf-8")))
    if unticked:
        return [
            LintFinding(
                Severity.ERROR,
                "checklist-incomplete",
                f"notes.md has {unticked} unticked checklist item(s); work through them "
                "before submitting",
                folder.name,
            )
        ]
    return []
