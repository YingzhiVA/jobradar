"""Orchestrates the application-tailoring pipeline: URL queue -> fetch ->
extract JD -> reuse prior report analysis (or one fresh scoring call) ->
tailored CV + cover letter -> git-trackable application folder.

Usage:
    python -m jobradar.apply                          # process applications/queue.txt
    python -m jobradar.apply --url URL [--full]       # one ad-hoc URL instead of the queue
    python -m jobradar.apply --upgrade KEY            # re-draft a quick draft at full tier
    python -m jobradar.apply --commit                 # git-commit + push the results
    python -m jobradar.apply --mark-submitted KEY     # record a submission
    python -m jobradar.apply --prep KEY               # interview-prep sheet for a drafted folder
    python -m jobradar.apply --lint [KEY ...]         # check drafted folders before submitting
    python -m jobradar.apply --mark-rejected KEY      # record an outcome (also --mark-withdrawn/--mark-offer)
    python -m jobradar.apply --archive [KEY ...]      # archive closed/ghosted/stale applications
    python -m jobradar.apply --rav 2026-07            # monthly proof-of-applications table (Swiss RAV registrants;
    python -m jobradar.apply --rav-filed 2026-07      # record it as filed, then archive      needs rav.enabled)

KEY is an application's short id - the number its folder name starts with, and
the "ID:" line in its notes.md, so `--pdf 42` needs nothing looked up. The full
folder name and the posting URL are still accepted everywhere a KEY is.

Postings whose page can't be fetched or extracted (e.g. JS-only ATSs like
Workday) get a stub folder: paste the full job description into its
job_description.md below the marker line and re-run - the pipeline picks it
up from there, no URL fetch needed.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from .. import usage
from ..archive import load_retention
from ..config import ConfigError, load_search_settings
from ..search.main import AuthenticationConfigError, _check_authentication
from ..matching import build_profile_block, load_evidence, load_profile, score_posting
from ..models import Posting, posting_id
from . import archive as app_archive
from . import lint, pdf, progress
from .extract import JDExtract, extract_jd
from .fetch import MIN_USABLE_CHARS, fetch_page
from .findings import Finding, load_findings
from .prep import generate_prep, render_prep_md
from .queue import QueueEntry, load_queue
from .tailor import build_jd_block, language_name, translate_cv, write_application_docs
from .tracker import (
    STATUS_DRAFTED,
    STATUS_NEEDS_JD,
    STATUS_OFFER,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
    Application,
    Tracker,
    next_uid,
    render_rav_month,
)
from .util import normalize_url, slugify

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
# httpx logs one "HTTP Request: POST .../v1/messages 200 OK" line per call. It
# used to be the only thing a drafting run printed, which made it read as a
# progress report it never was: it fires when the response headers arrive, so
# on a streaming call it says "started", and the minutes of silence after it
# were the actual work. progress.py reports the stages properly now, so the
# line is demoted to keep the timeline clean; JOBRADAR_LOG_HTTP=1 brings it
# back for debugging the transport itself.
logging.getLogger("httpx").setLevel(
    logging.INFO if os.environ.get("JOBRADAR_LOG_HTTP") else logging.WARNING
)
progress.install()
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]  # project root, three levels above src/jobradar/apply/
CVS_DIR = ROOT / "profile" / "cvs"  # the base CVs a tailored CV is cut down from

STUB_MARKER = "<!-- Paste the full job description BELOW this line, then re-run: python -m jobradar.apply -->"
# Anything shorter below the marker is treated as "still waiting for the paste".
_MIN_PASTED_CHARS = 200


@dataclass
class _Context:
    client: anthropic.Anthropic
    profile_block: str
    cv_labels: list[str]
    findings: dict[str, Finding]
    tracker: Tracker
    applications_dir: Path
    helper_usage: dict
    writer_usage: dict
    # URLs already archived to applications/archive/: never silently re-draft
    # them (that would spend API money on a posting already dealt with).
    archived_urls: set[str] = field(default_factory=set)
    # Archived applications, kept only so uid allocation walks past their ids.
    archived: list[Application] = field(default_factory=list)

    def next_uid(self) -> str:
        return next_uid(self.tracker.applications, self.archived)


def _stub_jd_file(url: str, error: str) -> str:
    return (
        f"# Job description needed\n\n"
        f"- URL: {url}\n"
        f"- Why: {error}\n\n"
        f"Open the posting in a browser, copy the full job description, and "
        f"paste it below the marker line. Then re-run the pipeline.\n\n"
        f"{STUB_MARKER}\n"
    )


def _read_pasted_jd(jd_path: Path) -> str | None:
    """Content the user pasted below the stub marker, or None if still empty."""
    if not jd_path.exists():
        return None
    text = jd_path.read_text(encoding="utf-8")
    if STUB_MARKER in text:
        pasted = text.split(STUB_MARKER, 1)[1].strip()
        return pasted if len(pasted) >= _MIN_PASTED_CHARS else None
    # Marker removed entirely: treat the whole file as the JD if substantial.
    return text.strip() if len(text.strip()) >= _MIN_PASTED_CHARS else None


def _needs_jd(ctx: _Context, entry: QueueEntry, error: str) -> None:
    """Create (or keep) a stub folder waiting for a manually pasted JD."""
    existing = ctx.tracker.find_by_url(entry.url)
    if existing is not None and existing.status == STATUS_NEEDS_JD:
        logger.info("Still waiting for a pasted JD: applications/%s", existing.folder)
        return
    if existing is not None:
        # A re-draft (--upgrade) whose JD went bad: the application already has
        # real deliverables, so stubbing it would orphan the folder and drop its
        # tracker row. Fail loudly and change nothing instead.
        logger.error(
            "%s: %s. applications/%s keeps its existing draft.",
            entry.url,
            error,
            existing.folder,
        )
        return
    uid = ctx.next_uid()
    folder = f"{uid}_{date.today().isoformat()}_pending"
    folder_path = ctx.applications_dir / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    (folder_path / "job_description.md").write_text(
        _stub_jd_file(entry.url, error), encoding="utf-8"
    )
    ctx.tracker.upsert(
        Application(
            url=entry.url,
            folder=folder,
            uid=uid,
            prepared_on=date.today().isoformat(),
            tier="full" if entry.full else "quick",
            status=STATUS_NEEDS_JD,
        )
    )
    logger.warning(
        "%s: %s. Paste the JD into applications/%s/job_description.md and re-run.",
        entry.url,
        error,
        folder,
    )


def _fresh_analysis(
    ctx: _Context, entry: QueueEntry, jd: JDExtract
) -> tuple[str, int | None, int | None, str, str]:
    """One Haiku scoring call (same prompt+cached profile as the daily run) for
    URLs the daily report never analyzed. Returns (best_cv, skill, interest,
    analysis_text, source_label).
    """
    posting = Posting(
        id=posting_id(entry.url, jd.title, jd.company, "apply"),
        source="apply",
        url=entry.url,
        title=jd.title,
        company=jd.company,
        description=jd.description_markdown,
        location_text=jd.location or None,
    )
    scored = score_posting(
        posting, ctx.profile_block, ctx.client, usage_acc=ctx.helper_usage
    )
    if scored is None:
        # Scoring is enrichment, not a gate: fall back to the first CV so the
        # documents still get produced.
        logger.warning("Fresh scoring failed for %s; defaulting to CV %s", entry.url, ctx.cv_labels[0])
        return ctx.cv_labels[0], None, None, "(scoring unavailable for this run)", "fresh"
    return (
        scored.best_cv,
        scored.skill_score,
        scored.interest_score,
        scored.brief_reason,
        "fresh",
    )


def _resolve_base_cv(ctx: _Context, label: str) -> str:
    if label in ctx.cv_labels:
        return label
    # The scorer/report may phrase the label loosely; try a substring match
    # before falling back so a rename doesn't silently mis-tailor.
    for known in ctx.cv_labels:
        if known.lower() in label.lower() or label.lower() in known.lower():
            return known
    logger.warning("Unknown base CV %r; falling back to %r", label, ctx.cv_labels[0])
    return ctx.cv_labels[0]


def _notes_md(
    app: Application,
    analysis_text: str,
    highlights: list[str],
    gaps: list[str],
    open_questions: list[str],
    language: str,
    dropped: list[str] | None = None,
) -> str:
    lines = [
        f"# {app.title} at {app.company}",
        "",
        f"- ID: {app.uid} (use it as the KEY in any jobradar.apply command)",
        f"- URL: {app.url}",
        f"- Prepared: {app.prepared_on} (tier: {app.tier})",
        f"- Base CV: {app.base_cv}",
        f"- Scores: skill {app.skill_score if app.skill_score is not None else '?'}"
        f"/100, interest {app.interest_score if app.interest_score is not None else '?'}/100"
        f" ({app.analysis_source})",
        f"- Posting language: {language_name(language)}",
        f"- Cover letter: {'included' if app.cover_letter else 'not required, not generated'}",
    ]
    if app.cv_translation_language:
        lang_name = language_name(app.cv_translation_language)
        cv_file = f"tailored_cv_{app.cv_translation_language}.md"
        lines.append(
            f"- {lang_name} CV: **{cv_file}** is the version to submit for this "
            f"{lang_name}-language posting; tailored_cv.md is the English original, "
            f"kept as a fallback."
        )
    lines += [
        "",
        "## Fit assessment",
        "",
        analysis_text.strip() or "(none)",
        "",
        "## What the tailored CV emphasizes",
        "",
    ]
    lines += [f"- {h}" for h in highlights] or ["- (not reported)"]
    # The cuts are the half of tailoring that the tailored CV cannot show: a
    # bullet that is gone leaves nothing behind to notice. Listing them here
    # is what makes the relevance filter reviewable - and an empty list is
    # itself the signal that it did not fire.
    lines += ["", "## What the tailored CV leaves out", ""]
    lines += [f"- {d}" for d in (dropped or [])] or [
        "- nothing dropped: every bullet of the base CV survived, so check "
        "the CV for material this posting has no use for"
    ]
    lines += ["", "## Gaps to be aware of", ""]
    lines += [f"- {g}" for g in gaps] or ["- none flagged"]
    if open_questions:
        lines += ["", "## Decide/verify before submitting", ""]
        lines += [f"- {q}" for q in open_questions]
    key = app.uid or app.folder
    if app.tier == "full":
        pdf_line = f"- [ ] Export to PDF when done editing: python -m jobradar.apply --pdf {key}"
    else:
        pdf_line = (
            "- [ ] PDFs were generated automatically; if you edit the markdown, "
            f"regenerate with: python -m jobradar.apply --pdf {key}"
        )
    lines += [
        "",
        "## Checklist",
        "",
        "- [ ] Review tailored_cv.md (facts, tone, length)",
    ]
    if app.cv_translation_language:
        lang_name = language_name(app.cv_translation_language)
        lines.append(
            f"- [ ] Review tailored_cv_{app.cv_translation_language}.md "
            f"({lang_name} translation: check terminology and that no facts "
            "drifted) - this is the one you submit"
        )
    lines += [
        "- [ ] Review cover_letter.md"
        if app.cover_letter
        else "- [ ] Decide whether to add a cover letter anyway; to get one, "
        f"re-draft at full tier: python -m jobradar.apply --upgrade {key}",
        pdf_line,
        "- [ ] Submit, then run: "
        f"python -m jobradar.apply --mark-submitted {key}",
        "",
    ]
    return "\n".join(lines)


def process_entry(
    ctx: _Context, entry: QueueEntry, *, index: int = 0, total: int = 0
) -> bool:
    """Returns True when application documents were produced for this entry.

    `index`/`total` only label the progress lines ("[2/5] writing ..."), so a
    caller processing a single entry can leave them out.
    """
    pre = f"[{index}/{total}] " if total > 1 else ""
    if entry.url in ctx.archived_urls:
        logger.info(
            "Skipping (already archived under applications/archive/): %s", entry.url
        )
        return False
    existing = ctx.tracker.find_by_url(entry.url)
    if existing is not None and existing.status != STATUS_NEEDS_JD and not entry.redraft:
        logger.info("Skipping (already %s): %s", existing.status, entry.url)
        return False

    # 1. Get the raw posting text: a previously stubbed folder with a pasted
    #    JD wins, then the JD already saved in a re-drafted folder; otherwise
    #    fetch the URL.
    page_text: str | None = None
    if existing is not None and existing.status == STATUS_NEEDS_JD:
        page_text = _read_pasted_jd(ctx.applications_dir / existing.folder / "job_description.md")
        if page_text is None:
            logger.info("Still waiting for a pasted JD: applications/%s", existing.folder)
            return False
    elif entry.redraft and existing is not None:
        # Re-use the JD saved on the first pass: same posting text as the draft
        # being replaced, no refetch of a page that may since have moved or
        # started refusing the fetcher. A failure here must never fall through
        # to _needs_jd - that would stub over a real application folder.
        page_text = _read_pasted_jd(
            ctx.applications_dir / existing.folder / "job_description.md"
        )
        if page_text is None:
            with progress.step(f"{pre}re-fetching the posting page"):
                outcome = fetch_page(entry.url)
            if outcome.error is not None or not outcome.usable:
                logger.error(
                    "Cannot re-draft %s: applications/%s has no usable "
                    "job_description.md and the posting could not be re-fetched "
                    "(%s). Leaving the existing draft untouched.",
                    existing.uid or existing.folder,
                    existing.folder,
                    outcome.error or f"under {MIN_USABLE_CHARS} characters of text",
                )
                return False
            page_text = outcome.text
    else:
        with progress.step(f"{pre}fetching the posting page"):
            outcome = fetch_page(entry.url)
        if outcome.error is not None:
            _needs_jd(ctx, entry, outcome.error)
            return False
        if not outcome.usable:
            _needs_jd(
                ctx,
                entry,
                f"page returned under {MIN_USABLE_CHARS} characters of text "
                "(likely a JavaScript-rendered page)",
            )
            return False
        page_text = outcome.text

    # 2. Haiku: clean the text into a JD + structured facts.
    with progress.step(f"{pre}reading the job description (Haiku)") as stage:
        jd = extract_jd(
            page_text, ctx.client, usage_acc=ctx.helper_usage, on_delta=stage.advance
        )
    if jd is None or not jd.is_job_posting or len(jd.description_markdown) < 200:
        _needs_jd(ctx, entry, "no usable job posting found in the page text")
        return False

    # 3. Fit analysis: reuse the daily report's write-up when we have one for
    #    this URL, otherwise one fresh Haiku scoring call.
    finding = ctx.findings.get(entry.url)
    if finding is not None and finding.best_cv:
        best_cv = finding.best_cv
        skill, interest = finding.skill_score, finding.interest_score
        analysis_text = finding.writeup
        analysis_source = f"report:{finding.report_date}"
        logger.info("Reusing analysis from daily report %s for %s", finding.report_date, entry.url)
    else:
        with progress.step(f"{pre}scoring the fit (Haiku)"):
            best_cv, skill, interest, analysis_text, analysis_source = _fresh_analysis(
                ctx, entry, jd
            )
    base_cv = _resolve_base_cv(ctx, best_cv)

    # 4. Sonnet: one call producing the tailored CV and (when the posting asks
    #    for one, or the entry is full-effort) the cover letter.
    today = date.today().isoformat()
    jd_block = build_jd_block(
        jd.description_markdown, entry.url, today, prior_analysis=analysis_text
    )
    want_cover_letter = jd.cover_letter_required or entry.full
    writing = "tailored CV + cover letter" if want_cover_letter else "tailored CV"
    with progress.step(f"{pre}writing the {writing} for {jd.company} (Sonnet)") as stage:
        tailored = write_application_docs(
            base_cv,
            jd.language,
            want_cover_letter,
            ctx.profile_block,
            jd_block,
            ctx.client,
            usage_acc=ctx.writer_usage,
            on_delta=stage.advance,
        )
    if tailored is None:
        logger.error("Document writing failed for %s; leaving it in the queue for a re-run", entry.url)
        return False
    cover_letter = tailored.cover_letter_markdown.strip() or None
    if want_cover_letter and cover_letter is None:
        logger.warning("Cover letter missing for %s; CV and notes still written", entry.url)

    # 5. Write the deliverables folder (renaming a stub folder now that the
    #    company/title are known).
    # A stub folder keeps the uid it was given; only its company/title tail
    # changes now that they are known.
    uid = existing.uid if existing is not None and existing.uid else ctx.next_uid()
    folder = f"{uid}_{today}_{slugify(jd.company)}_{slugify(jd.title)}"
    folder_path = ctx.applications_dir / folder
    if existing is not None and existing.folder != folder:
        old_path = ctx.applications_dir / existing.folder
        if old_path.exists() and not folder_path.exists():
            old_path.rename(folder_path)
    folder_path.mkdir(parents=True, exist_ok=True)

    (folder_path / "job_description.md").write_text(
        f"# {jd.title} at {jd.company}\n\n- URL: {entry.url}\n- Retrieved: {today}\n\n---\n\n"
        + jd.description_markdown.strip()
        + "\n",
        encoding="utf-8",
    )
    (folder_path / "tailored_cv.md").write_text(
        tailored.tailored_cv_markdown.strip() + "\n", encoding="utf-8"
    )
    if cover_letter:
        (folder_path / "cover_letter.md").write_text(cover_letter.strip() + "\n", encoding="utf-8")

    # For a non-English posting (German being the common Swiss case), also
    # render the tailored CV in the posting's language. The cover letter is
    # already written in it, so only the CV needs translating.
    cv_translation_language: str | None = None
    lang = (jd.language or "en").lower()
    if lang != "en":
        with progress.step(
            f"{pre}translating the CV to {language_name(lang)} (Sonnet)"
        ) as stage:
            translated = translate_cv(
                tailored.tailored_cv_markdown,
                lang,
                ctx.profile_block,
                jd_block,
                ctx.client,
                usage_acc=ctx.writer_usage,
                on_delta=stage.advance,
            )
        if translated:
            (folder_path / f"tailored_cv_{lang}.md").write_text(
                translated.strip() + "\n", encoding="utf-8"
            )
            cv_translation_language = lang
        else:
            logger.warning(
                "CV translation to %s failed for %s; English CV still written",
                language_name(lang),
                entry.url,
            )

    app = Application(
        url=entry.url,
        folder=folder,
        uid=uid,
        title=jd.title,
        company=jd.company,
        postcode=jd.postcode.strip(),
        prepared_on=today,
        tier="full" if entry.full else "quick",
        base_cv=base_cv,
        skill_score=skill,
        interest_score=interest,
        cover_letter=bool(cover_letter),
        cv_translation_language=cv_translation_language,
        analysis_source=analysis_source,
        status=STATUS_DRAFTED,
    )
    (folder_path / "notes.md").write_text(
        _notes_md(
            app,
            analysis_text,
            tailored.highlights,
            tailored.gaps,
            tailored.open_questions,
            jd.language,
            tailored.dropped,
        ),
        encoding="utf-8",
    )
    ctx.tracker.upsert(app)

    # Quick-tier applications get submitted as-is, so produce the PDFs right
    # away; full-tier ones are edited first, then converted via --pdf.
    if app.tier == "quick":
        with progress.step(f"{pre}rendering the PDFs"):
            pdf.convert_folder(folder_path)

    logger.info("Drafted %s: applications/%s", uid, folder)
    return True


def _git_commit_and_push(paths: list[Path], message: str) -> None:
    """Best-effort: the deliverables are already on disk either way."""
    try:
        existing = [str(p) for p in paths if p.exists()]
        subprocess.run(
            ["git", "add", *existing], cwd=ROOT, check=True, capture_output=True
        )
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.info("Nothing to commit: %s", result.stdout.strip() or result.stderr.strip())
            return
        push = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
        if push.returncode != 0:
            logger.warning("Committed locally but push failed: %s", push.stderr.strip())
        else:
            logger.info("Committed and pushed %s", ", ".join(existing))
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("git commit failed: %s", exc)


def _upgrade_entries(tracker: Tracker, keys: list[str]) -> list[QueueEntry]:
    """Resolve --upgrade KEYs to full-tier re-draft entries.

    Only a draft can be upgraded: once an application has gone out, rewriting
    its documents would leave the folder disagreeing with what the employer
    received.
    """
    entries: list[QueueEntry] = []
    for key in keys:
        app = tracker.find(key)
        if app is None:
            logger.error("No application found for %r - skipping", key)
            continue
        if app.status not in (STATUS_DRAFTED, STATUS_NEEDS_JD):
            logger.error(
                "%s is %s, not a draft: its documents are already out, so "
                "re-drafting them would desync the folder from what was sent. "
                "Skipping.",
                app.uid or app.folder,
                app.status,
            )
            continue
        if app.tier == "full":
            logger.info(
                "%s is already full tier; re-drafting it anyway (--upgrade).",
                app.uid or app.folder,
            )
        entries.append(QueueEntry(url=app.url, full=True, redraft=True))
    if not entries:
        logger.info("Nothing to upgrade.")
    return entries


def run(
    *,
    queue_path: Path | None = None,
    adhoc_url: str | None = None,
    full: bool = False,
    upgrade_keys: list[str] | None = None,
    limit: int | None = None,
    commit: bool = False,
) -> None:
    load_dotenv(ROOT / ".env")
    client = anthropic.Anthropic()
    _check_authentication(client)

    cvs, identity, stories = load_profile(ROOT / "profile")
    evidence = load_evidence(ROOT / "profile")
    profile_block = build_profile_block(cvs, identity, stories, evidence)

    applications_dir = ROOT / "applications"
    tracker = Tracker(applications_dir / "applications.json")
    archive_tracker = Tracker(app_archive.archive_tracker_path(applications_dir))
    ctx = _Context(
        client=client,
        profile_block=profile_block,
        cv_labels=list(cvs),
        findings=load_findings(ROOT / "reports"),
        tracker=tracker,
        applications_dir=applications_dir,
        helper_usage={},
        writer_usage={},
        archived_urls={a.url for a in archive_tracker.applications},
        archived=archive_tracker.applications,
    )

    if upgrade_keys:
        entries = _upgrade_entries(tracker, upgrade_keys)
        if not entries:
            return
    elif adhoc_url:
        entries = [QueueEntry(url=normalize_url(adhoc_url), full=full)]
    else:
        entries = load_queue(queue_path or applications_dir / "queue.txt")
        # Stubbed postings waiting for a pasted JD are retried on every run,
        # even after the user pruned their URL from the queue file.
        queued_urls = {e.url for e in entries}
        entries += [
            QueueEntry(url=app.url, full=app.tier == "full")
            for app in tracker.applications
            if app.status == STATUS_NEEDS_JD and app.url not in queued_urls
        ]
        if not entries:
            logger.info(
                "Queue is empty. Add posting URLs to %s (one per line; append "
                "'full' for full-effort applications).",
                queue_path or applications_dir / "queue.txt",
            )
            return
    if limit is not None:
        entries = entries[:limit]

    # The plan, before the first slow call: a run that is about to spend ten
    # minutes on five postings should say so up front, not at the end. The
    # list is capped because a long queue would otherwise open the run with a
    # wall of URLs; each one is announced again as its turn comes.
    shown = [e.url for e in entries[:5]]
    if len(entries) > len(shown):
        shown.append(f"and {len(entries) - len(shown)} more")
    logger.info(
        "Processing %d entr%s: %s",
        len(entries),
        "y" if len(entries) == 1 else "ies",
        ", ".join(shown),
    )

    drafted = 0
    run_started = time.monotonic()
    for index, entry in enumerate(entries, start=1):
        entry_started = time.monotonic()
        if len(entries) > 1:
            logger.info("[%d/%d] %s", index, len(entries), entry.url)
        try:
            if process_entry(ctx, entry, index=index, total=len(entries)):
                drafted += 1
                logger.info(
                    "[%d/%d] finished %s in %s",
                    index,
                    len(entries),
                    entry.url,
                    progress.fmt_duration(time.monotonic() - entry_started),
                )
        except Exception as exc:  # noqa: BLE001 - one bad URL shouldn't kill the batch
            logger.error("Unexpected failure for %s: %s", entry.url, exc)
        finally:
            # Persist after every entry so a crash mid-batch loses nothing.
            tracker.save()

    usage.log_summary(logger, "apply/helper", ctx.helper_usage)
    usage.log_summary(logger, "apply/writer", ctx.writer_usage)
    logger.info(
        "Done in %s: %d application(s) drafted, %d entries processed",
        progress.fmt_duration(time.monotonic() - run_started),
        drafted,
        len(entries),
    )
    if upgrade_keys and drafted:
        # Full tier means no automatic PDFs, so an upgraded quick-tier folder
        # is left holding the PDFs of the draft that was just replaced.
        logger.info(
            "Upgraded to full tier: review the new markdown, then regenerate the "
            "PDFs (the old ones are now stale) with: python -m jobradar.apply "
            "--pdf %s",
            " ".join(upgrade_keys),
        )
    if commit and drafted:
        _git_commit_and_push(
            [applications_dir],
            f"applications: {drafted} draft(s) prepared {date.today().isoformat()}",
        )


def run_prep(keys: list[str]) -> None:
    """Generate interview_prep.md for already-drafted application folders."""
    load_dotenv(ROOT / ".env")
    client = anthropic.Anthropic()
    _check_authentication(client)

    cvs, identity, stories = load_profile(ROOT / "profile")
    evidence = load_evidence(ROOT / "profile")
    # Two independent checks, not a chain: the two sources cover different
    # halves of an interview, so an empty one is worth saying regardless of
    # whether the other has content.
    if not stories:
        logger.info(
            "No stories in profile/stories/ - behavioural questions will map to "
            "CV experience rather than to a prepared story. Add your STAR "
            "stories there; see profile/stories/README.md."
        )
    if not evidence:
        logger.info(
            "No write-ups in profile/evidence/ - technical questions will map "
            "to CV experience rather than to something published. Run "
            "python -m jobradar.evidence to ingest them."
        )
    profile_block = build_profile_block(cvs, identity, stories, evidence)
    tracker = Tracker(ROOT / "applications" / "applications.json")
    usage_acc: dict = {}
    for index, key in enumerate(keys, start=1):
        _prep_key(
            tracker, key, profile_block, client, usage_acc, index=index, total=len(keys)
        )
    usage.log_summary(logger, "apply/prep", usage_acc)


def _prep_key(
    tracker: Tracker,
    key: str,
    profile_block: str,
    client: anthropic.Anthropic,
    usage_acc: dict,
    *,
    index: int = 0,
    total: int = 0,
) -> None:
    """Resolve a --prep KEY (id, tracked folder name, URL, or filesystem path)
    and write interview_prep.md into its application folder.

    `index`/`total` only label the progress line, as in process_entry."""
    pre = f"[{index}/{total}] " if total > 1 else ""
    app = tracker.find(key)
    if app is not None:
        folder_path = ROOT / "applications" / app.folder
    elif Path(key).is_dir():
        folder_path = Path(key)
    else:
        logger.error(
            "No tracked application (or existing folder) matches %r; KEY is the "
            "id the folder name starts with, a folder name, or a URL",
            key,
        )
        return
    jd_path = folder_path / "job_description.md"
    if not jd_path.exists():
        logger.error("%s has no job_description.md", folder_path)
        return
    jd_text = jd_path.read_text(encoding="utf-8")
    if STUB_MARKER in jd_text and _read_pasted_jd(jd_path) is None:
        logger.error(
            "%s is still waiting for a pasted job description; prep needs one", folder_path
        )
        return

    # The drafted notes (fit assessment, tailoring highlights, honest gaps)
    # are exactly the context the prep sheet should build on, when they exist.
    notes_path = folder_path / "notes.md"
    prior = notes_path.read_text(encoding="utf-8") if notes_path.exists() else None

    url = app.url if app is not None else ""
    retrieved_on = app.prepared_on if app is not None else date.today().isoformat()
    jd_block = build_jd_block(jd_text, url or folder_path.name, retrieved_on, prior_analysis=prior)

    # The posting's language, when drafting recorded one (it only sets this
    # field for non-English postings). Left None for an untracked folder, and
    # the prep call reads the language off the posting itself.
    language = app.cv_translation_language if app is not None else None
    with progress.step(
        f"{pre}writing the interview prep sheet for {folder_path.name} (Sonnet)"
    ) as stage:
        sheet = generate_prep(
            profile_block,
            jd_block,
            client,
            usage_acc=usage_acc,
            language=language,
            on_delta=stage.advance,
        )
    if sheet is None:
        logger.error("Interview prep failed for %s", folder_path)
        return
    title = (app.title if app is not None and app.title else None) or folder_path.name
    company = (app.company if app is not None else None) or ""
    (folder_path / "interview_prep.md").write_text(
        render_prep_md(sheet, title, company, url), encoding="utf-8"
    )
    logger.info("Wrote %s", folder_path / "interview_prep.md")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, help="Queue file (default: applications/queue.txt)")
    parser.add_argument("--url", help="Process one ad-hoc URL instead of the queue")
    parser.add_argument(
        "--full", action="store_true", help="With --url: full-effort tier (cover letter always)"
    )
    parser.add_argument(
        "--upgrade",
        nargs="+",
        metavar="KEY",
        help="Re-draft already-drafted application(s) at the full-effort tier - "
        "a cover letter is always written (KEY = id, folder name, or URL). Costs "
        "a fresh writing call: the tailored CV is rewritten too, and the PDFs "
        "then need --pdf",
    )
    parser.add_argument("--limit", type=int, help="Process at most N queue entries")
    parser.add_argument(
        "--commit", action="store_true", help="git add/commit/push applications/ after drafting"
    )
    parser.add_argument(
        "--mark-submitted",
        nargs="+",
        metavar="KEY",
        help="Mark application(s) submitted today (KEY = id, folder name, or URL)",
    )
    parser.add_argument(
        "--mark-rejected",
        nargs="+",
        metavar="KEY",
        help="Record a rejection (KEY = id, folder name, or URL)",
    )
    parser.add_argument(
        "--mark-withdrawn",
        nargs="+",
        metavar="KEY",
        help="Record a withdrawal (KEY = id, folder name, or URL)",
    )
    parser.add_argument(
        "--mark-offer",
        nargs="+",
        metavar="KEY",
        help="Record an offer (KEY = id, folder name, or URL)",
    )
    parser.add_argument(
        "--rav-filed",
        metavar="YYYY-MM",
        help="(Swiss RAV registrants; needs rav.enabled in config/search.yaml) "
        "Record that the month's RAV report was filed: writes the rendered "
        "table to reports/rav/YYYY-MM.md, updates applications/rav_filed.json, "
        "then runs the archive sweep",
    )
    parser.add_argument(
        "--archive",
        nargs="*",
        metavar="KEY",
        help="Archive closed/ghosted/stale applications to applications/archive/. "
        "No KEY sweeps by retention policy; a KEY archives that application now "
        "(with RAV reporting enabled, refused until its submitted month's report "
        "is filed)",
    )
    parser.add_argument(
        "--lint",
        nargs="*",
        metavar="KEY",
        help="Lint drafted application folder(s) for mechanical mistakes (missing "
        "deliverables, stale PDFs, tracker drift) and exit non-zero on any error. "
        "No KEY lints every tracked application plus repo-level integrity; a KEY "
        "is an id, folder name, URL, or folder path.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="With --lint: also require notes.md's review checklist to be fully ticked",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the pre-conversion lint gate on --pdf despite errors",
    )
    parser.add_argument(
        "--rav",
        metavar="YYYY-MM",
        help="(Swiss RAV registrants; needs rav.enabled in config/search.yaml) "
        "Print the monthly proof-of-applications table and exit",
    )
    parser.add_argument(
        "--pdf",
        nargs="+",
        metavar="KEY",
        help="Convert application markdown to PDF and exit (KEY = id, folder name, "
        "URL, or a path to a folder or .md file)",
    )
    parser.add_argument(
        "--prep",
        nargs="+",
        metavar="KEY",
        help="Write an interview_prep.md into drafted application folder(s), "
        "mapping likely questions to your STAR stories in profile/stories/ and "
        "published write-ups in profile/evidence/ "
        "(KEY = id, folder name, URL, or folder path)",
    )
    args = parser.parse_args(argv)

    outcome_marks = [
        (args.mark_rejected, STATUS_REJECTED),
        (args.mark_withdrawn, STATUS_WITHDRAWN),
        (args.mark_offer, STATUS_OFFER),
    ]
    # Bookkeeping modes need no API client.
    if (
        args.rav
        or args.mark_submitted
        or any(keys for keys, _ in outcome_marks)
        or args.pdf
        or args.lint is not None
        or args.rav_filed
        or args.archive is not None
    ):
        applications_dir = ROOT / "applications"
        tracker = Tracker(applications_dir / "applications.json")
        # The RAV switch: read once here, since only bookkeeping consults it.
        try:
            rav_enabled = load_search_settings(ROOT).rav.enabled
        except ConfigError as exc:
            raise SystemExit(str(exc)) from exc
        if (args.rav or args.rav_filed) and not rav_enabled:
            raise SystemExit(
                "RAV reporting is off. It is for job seekers registered with a Swiss "
                "RAV; set `rav.enabled: true` in config/search.yaml to use --rav and "
                "--rav-filed."
            )
        if args.lint is not None:
            raise SystemExit(_run_lint(tracker, args.lint, strict=args.strict))
        blocked = False
        changed_paths: list[Path] = []
        if args.pdf:
            # Gate on content/integrity errors, but not on PDF-staleness ones -
            # producing the PDF is exactly what resolves those.
            if _lint_gate(
                tracker, args.pdf, "PDF conversion", force=args.force,
                skip_codes=lint.PDF_STALENESS_CODES,
            ):
                for key in args.pdf:
                    _convert_key(tracker, key)
            else:
                blocked = True
        if args.mark_submitted:
            # Deliberately not gated: you record a submission after it has
            # already gone out, so a block here would only desync the tracker.
            # Run --lint before submitting instead.
            for key in args.mark_submitted:
                app = tracker.mark_submitted(key)
                if app is None:
                    logger.error("No tracked application matches %r", key)
                else:
                    logger.info("Marked submitted: %s (%s at %s)", app.folder, app.title, app.company)
            tracker.save()
            changed_paths.append(applications_dir)
        if any(keys for keys, _ in outcome_marks):
            # Recorded before the archive sweep below, so a fresh outcome is
            # immediately eligible for archiving in the same invocation.
            for keys, status in outcome_marks:
                for key in keys or []:
                    app = tracker.mark_outcome(key, status)
                    if app is None:
                        logger.error("No tracked application matches %r", key)
                    else:
                        logger.info(
                            "Marked %s: %s (%s at %s)", status, app.folder, app.title, app.company
                        )
            tracker.save()
            changed_paths.append(applications_dir)
        if args.rav_filed:
            if not re.fullmatch(r"\d{4}-\d{2}", args.rav_filed):
                raise SystemExit(f"--rav-filed expects YYYY-MM, got {args.rav_filed!r}")
            month = args.rav_filed
            rav_path = ROOT / "reports" / "rav" / f"{month}.md"
            rav_path.parent.mkdir(parents=True, exist_ok=True)
            rav_path.write_text(
                render_rav_month(_all_applications(tracker), month), encoding="utf-8"
            )
            app_archive.mark_rav_filed(
                applications_dir, month, str(rav_path.relative_to(ROOT)), date.today()
            )
            logger.info(
                "RAV report for %s recorded as filed; table saved to %s",
                month, rav_path.relative_to(ROOT),
            )
            changed_paths += [applications_dir, rav_path.parent]
        if args.rav_filed or args.archive is not None:
            retention = load_retention(ROOT)
            today = date.today()
            archived: list[tuple[Application, str]] = []
            if args.archive:  # explicit KEYs: archive now, but honor the RAV gate
                rav_filed = (
                    app_archive.load_rav_filed(applications_dir) if rav_enabled else {}
                )
                to_archive = []
                for key in args.archive:
                    app = tracker.find(key)
                    if app is None:
                        logger.error("No tracked application matches %r", key)
                        blocked = True
                    elif (
                        rav_enabled
                        and app.submitted_on
                        and app.submitted_on[:7] not in rav_filed
                    ):
                        logger.error(
                            "Not archiving %s: submitted %s and the RAV report for "
                            "%s is not filed yet (run --rav-filed %s first)",
                            app.folder, app.submitted_on,
                            app.submitted_on[:7], app.submitted_on[:7],
                        )
                        blocked = True
                    else:
                        reason = app_archive.archive_reason(
                            app, rav_filed, today, retention, rav_enabled=rav_enabled
                        )
                        to_archive.append((app, reason or "manual"))
                done = app_archive.archive_applications(
                    tracker, applications_dir, [a for a, _ in to_archive], today
                )
                done_urls = {a.url for a in done}
                archived = [(a, r) for a, r in to_archive if a.url in done_urls]
            else:  # policy sweep (also runs implicitly after --rav-filed)
                archived = app_archive.sweep(
                    tracker, applications_dir, today, retention, rav_enabled=rav_enabled
                )
            for app, reason in archived:
                print(f"Archived ({reason}): {app.folder}  [status={app.status}]")
                if app.status == STATUS_OFFER:
                    logger.warning(
                        "%s had status=offer - archived like any terminal outcome; "
                        "unarchive by moving it back manually if that was premature",
                        app.folder,
                    )
            print(f"{len(archived)} application(s) archived." if archived
                  else "Nothing to archive.")
            if archived:
                changed_paths.append(applications_dir)
        if args.rav:
            print(render_rav_month(_all_applications(tracker), args.rav))
        if changed_paths:
            if args.commit:
                _git_commit_and_push(
                    sorted(set(changed_paths), key=str),
                    f"applications: bookkeeping {date.today().isoformat()}",
                )
            elif args.rav_filed:
                logger.info(
                    "Remember to commit applications/ and reports/rav/ (or re-run with --commit)."
                )
        raise SystemExit(1 if blocked else 0)

    try:
        if args.upgrade and args.url:
            raise SystemExit("--upgrade re-drafts a tracked application; drop --url")
        if args.prep:
            run_prep(args.prep)
            return
        run(
            queue_path=args.queue,
            adhoc_url=args.url,
            full=args.full,
            upgrade_keys=args.upgrade,
            limit=args.limit,
            commit=args.commit,
        )
    except AuthenticationConfigError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc


def _all_applications(tracker: Tracker) -> list[Application]:
    """Active plus archived applications, so --rav and --rav-filed can render
    past months fully even after their applications were archived."""
    apps = list(tracker.applications)
    archive_path = app_archive.archive_tracker_path(ROOT / "applications")
    if archive_path.exists():
        apps += Tracker(archive_path).applications
    return apps


def _resolve_lint_target(
    tracker: Tracker, key: str, applications_dir: Path
) -> tuple[Path, Application | None] | None:
    """Resolve a --lint KEY to (folder, tracker entry). KEY is a tracked folder
    name/URL, or a filesystem path to a folder; returns None if nothing matches."""
    app = tracker.find(key)
    if app is not None:
        return applications_dir / app.folder, app
    path = Path(key)
    if path.is_dir():
        return path, next((a for a in tracker.applications if a.folder == path.name), None)
    return None


def _lint_keys(tracker: Tracker, keys: list[str], *, strict: bool) -> list[lint.LintFinding]:
    """Lint the given folders; unresolvable keys are skipped silently (the
    calling action reports bad keys itself)."""
    applications_dir = ROOT / "applications"
    all_companies = {a.company for a in tracker.applications if a.company}
    findings: list[lint.LintFinding] = []
    for key in keys:
        target = _resolve_lint_target(tracker, key, applications_dir)
        if target is None:
            continue
        folder, app = target
        others = all_companies - ({app.company} if app is not None else set())
        findings += lint.lint_folder(
            folder, app, other_companies=others, strict=strict, cvs_dir=CVS_DIR
        )
    return findings


def _print_findings(findings: list[lint.LintFinding]) -> None:
    label = {lint.Severity.ERROR: "ERROR", lint.Severity.WARN: "WARN ", lint.Severity.INFO: "INFO "}
    for f in lint.sort_findings(findings):
        print(f"{label[f.severity]} {f.folder}: {f.message} [{f.code}]")
    errors = sum(1 for f in findings if f.severity is lint.Severity.ERROR)
    print(f"\n{errors} error(s), {len(findings) - errors} warning(s)")


def _run_lint(tracker: Tracker, keys: list[str], *, strict: bool) -> int:
    """Lint the given folders (or the whole repo when keys is empty). Prints the
    findings and returns a process exit code: 1 if any error-severity finding."""
    if keys:
        # Report keys that resolve to nothing (unlike the gate, which stays quiet).
        applications_dir = ROOT / "applications"
        for key in keys:
            if _resolve_lint_target(tracker, key, applications_dir) is None:
                logger.error("No tracked application (or existing folder) matches %r", key)
        findings = _lint_keys(tracker, keys, strict=strict)
    else:
        findings = lint.lint_repo(
            ROOT / "applications", tracker, strict=strict, cvs_dir=CVS_DIR
        )

    if not findings:
        print("No issues found.")
        return 0
    _print_findings(findings)
    return 1 if lint.has_errors(findings) else 0


def _lint_gate(
    tracker: Tracker,
    keys: list[str],
    action: str,
    *,
    force: bool,
    skip_codes: frozenset[str] = frozenset(),
) -> bool:
    """Lint the target folders before producing the submittable PDF. Prints any
    findings; returns False (abort) when blocking errors remain and --force was
    not given. `skip_codes` drops findings the action itself resolves, before
    printing - reporting the missing PDFs this run is about to write is pure
    noise. The gate never runs the strict checklist check - that is a --lint
    modifier."""
    findings = lint.drop_codes(_lint_keys(tracker, keys, strict=False), skip_codes)
    if not findings:
        return True
    _print_findings(findings)
    blocking = lint.blocking_errors(findings)
    if blocking and not force:
        logger.error(
            "%s aborted: %d lint error(s) above. Fix them, or re-run with --force.",
            action,
            len(blocking),
        )
        return False
    return True


def _convert_key(tracker: Tracker, key: str) -> None:
    """Resolve a --pdf KEY to a folder or file and convert it. Accepts a
    tracked folder name or URL, a filesystem path to an application folder,
    or a path to a single .md file."""
    path = Path(key)
    if path.suffix == ".md" and path.exists():
        out = pdf.convert_markdown_file(path)
        if out is None:
            logger.error("Conversion failed for %s", path)
        return
    if path.is_dir():
        if not pdf.convert_folder(path):
            logger.error("Nothing convertible in %s (expected tailored_cv.md / cover_letter.md)", path)
        return
    app = tracker.find(key)
    if app is None:
        logger.error("No tracked application (or existing path) matches %r", key)
        return
    folder = ROOT / "applications" / app.folder
    if not folder.is_dir():
        logger.error("Tracked folder is missing on disk: %s", folder)
        return
    if not pdf.convert_folder(folder):
        logger.error("Nothing convertible in %s", folder)


if __name__ == "__main__":
    main()
