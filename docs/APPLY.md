# Applying

Once the daily report (or your own browsing) surfaces roles worth applying
to, the apply pipeline turns a URL list into ready-to-review application
folders. `applications/README.md` is the short in-folder version of this
page.

## Configure

- **`applications/queue.txt`** — one posting URL per line (append `full`
  after a URL for the few roles that get your full effort). Or skip the queue
  and pass a single URL with `--url`.
- **`profile/evidence/`** (optional) — re-run `python -m jobradar.evidence`
  whenever your site gains a write-up, so cover letters and prep sheets can
  use it. Unchanged files are left alone.

## Run

```bash
# Process everything in applications/queue.txt:
./.venv/bin/python -m jobradar.apply

# Or one ad-hoc URL:
./.venv/bin/python -m jobradar.apply --url https://... [--full]

# Every command below takes the application's short id: the number its
# folder name starts with (007_2026-09-12_acme_product-manager), also printed
# as the "ID:" line in its notes.md. Folder names and posting URLs work too.

# Changed your mind about a quick draft? Re-draft it at full tier, so it
# gets a cover letter (keeps its id, folder and report analysis; costs one
# fresh writing call, so the tailored CV is rewritten too):
./.venv/bin/python -m jobradar.apply --upgrade 7

# Full-tier applications: convert the edited markdown to PDF when done
# (quick-tier ones get their PDFs generated automatically):
./.venv/bin/python -m jobradar.apply --pdf 7

# Pre-submission check: catch mechanical mistakes (missing deliverable, PDF
# left stale after an edit, wrong/leftover company in a cover letter, dropped
# contact line, tracker drift). No argument lints everything; exits non-zero
# on any error. --strict also requires notes.md's checklist to be ticked:
./.venv/bin/python -m jobradar.apply --lint [7 ...] [--strict]

# After submitting an application:
./.venv/bin/python -m jobradar.apply --mark-submitted 7

# Got an interview (or want to gauge one before applying)? Generate an
# interview-prep sheet mapping likely questions to your stories:
./.venv/bin/python -m jobradar.apply --prep 7

# Record outcomes as they arrive:
./.venv/bin/python -m jobradar.apply --mark-rejected 7
./.venv/bin/python -m jobradar.apply --mark-withdrawn 7
./.venv/bin/python -m jobradar.apply --mark-offer 7

# Archive sweep on demand (the cloud run does this daily). With ids,
# archives those applications immediately:
./.venv/bin/python -m jobradar.apply --archive [7 ...]
```

## While it runs

Drafting one posting is a page fetch plus three to five model calls, and the
writing ones are slow: a tailored CV and a cover letter is about 12k output
tokens, and a German posting adds a translation call on top, so a queue of
five can run for many minutes. The pipeline narrates that rather than going
quiet: every step names itself, and the model calls stream, so the live line
counts the text arriving:

```
INFO jobradar.apply.main: [2/5] https://jobs.example.ch/data-analyst-1234
⠹ [2/5] writing the tailored CV + cover letter for Beispiel AG (Sonnet)  1m18s, 7.4k chars
INFO jobradar.apply.progress: finished: [2/5] writing the tailored CV + cover letter for Beispiel AG (Sonnet) (2m04s, 11.9k chars)
```

The live line is transient and only appears on a terminal; each finished
step leaves a permanent one behind, so the scrollback reads as a timeline
with per-step timings. Piped or redirected output gets `starting:` /
`finished:` lines plus a `still running:` heartbeat every 20s instead.
`JOBRADAR_PROGRESS=0` turns the indicator off, `JOBRADAR_PROGRESS_INTERVAL`
sets the heartbeat, and `JOBRADAR_LOG_HTTP=1` restores the HTTP client's
per-call `200 OK` line.

`--upgrade` only accepts a draft: once an application is submitted,
rewriting its documents would leave the folder disagreeing with what the
employer got. Because full tier means "edit before sending", the PDFs are not
regenerated; the old ones go stale until you run `--pdf` (the linter says
so).

`--pdf` runs the linter first and aborts on any error (ignoring "PDF
missing/stale", since converting is what fixes that); pass `--force` to
proceed anyway. `--mark-submitted` is deliberately **not** gated: you record
a submission after it has already gone out, so a block there would only
desync the tracker. Make a habit of running `--lint` before you submit.

## What you get

Each URL becomes a git-tracked folder under `applications/` with the
retrieved job description, a tailored CV, a cover letter (when the posting
asks for one, or always on `full` tier), and a notes file with the fit
assessment, tailoring decisions, the base-CV bullets the tailoring cut and
why, honest gaps, and a pre-submission checklist. Tailoring subtracts as well
as reorders: a bullet that does not help someone hiring for *this* posting is
deleted however well it reads, and `--lint` warns when a tailored CV came
back with every base-CV bullet still in it.

The cover letter is written in the posting's language, with Swiss spelling
conventions (no ß). For a non-English posting the tailored CV is also
translated into that language as `tailored_cv_<lang>.md` (the version you
submit), with the English CV kept as a fallback. URLs that already surfaced
in a daily report reuse that report's write-up, scores, and best-CV choice
instead of being re-analyzed. Pages that can't be scraped (JavaScript-only
career sites like Workday) get a stub folder to paste the posting text into;
the next run picks it up from there.

PDFs are produced by a headless Chromium/Chrome print (the first of
`chromium`, `google-chrome` etc. found on PATH). Without one, the tool writes
a styled `.html` next to the Markdown instead: open it in any browser and
print to PDF.

Interview prep (`--prep`) maps the posting's likely questions onto your
prepared material and names what none of it covers. Behavioural questions
lead with a STAR story from `profile/stories/`; technical ones lead with a
published write-up from `profile/evidence/`, and the sheet ends with the
write-ups worth re-reading before you walk in.

Cost design mirrors the daily run: Haiku extracts and scores, Sonnet writes
the documents in a single call per posting, and the profile block (CVs,
identity, stories, and published write-ups) is a cached system prefix shared
across every posting in a batch. Deliverables are committed to git (use
`--commit` to do it automatically) so drafts and the submission log stay in
sync across machines.

## Archiving

Outdated data moves out of the active views automatically, on the retention
windows in `config/retention.yaml`, but stays committed in the repo:

- Daily reports older than `reports_days` and seen-store entries older than
  `seen_days` move to `reports/archive/` and `data/archive/`; the cloud run
  does this via `python -m jobradar.archive` (safe to run locally, `--dry-run`
  to preview).
- Applications move to `applications/archive/` once their outcome is recorded,
  or once they were submitted `ghosted_days` ago with no response; drafts
  never submitted expire after `stale_draft_days`. Archived folders keep all
  material for reuse, and archiving drops the application's line from
  `applications/queue.txt` if one is still there.

## Swiss RAV registrants (optional)

If you are registered with a RAV, you hand in a monthly proof of your
applications («Nachweis der persönlichen Arbeitsbemühungen»). jobradar can
produce that table from the submission log. Turn it on in
`config/search.yaml`:

```yaml
rav:
  enabled: true
```

Then:

```bash
# The month's table: date, company, postcode, role, how applied, result
# (hängig / Absage / zurückgezogen / Angebot), and the link:
./.venv/bin/python -m jobradar.apply --rav 2026-09

# After actually handing the month in: saves the table to
# reports/rav/2026-09.md, records the month as filed, and runs the archive
# sweep:
./.venv/bin/python -m jobradar.apply --rav-filed 2026-09
```

With RAV on, a submitted application stays out of the archive until the
month it was submitted in is recorded as filed, so the proof is never
archived away early; `--archive <id>` refuses for such an application until
then. With RAV off (the default), those two commands refuse to run and
archiving follows the retention windows alone. Turning RAV on later does not
bring back applications already archived; move a folder back by hand if you
need it in a table.
