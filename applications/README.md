# applications/

Git-tracked deliverables of the application-tailoring pipeline
(`python -m jobradar.apply`), so drafts and the submission log stay in sync
across machines.

## Workflow

1. Add posting URLs to [queue.txt](queue.txt), one per line. Append the word
   `full` after a URL for the few roles that get your full effort (cover
   letter always written, even when the posting does not ask for one).
   URLs from the daily jobradar report are ideal: their existing write-up,
   scores, and best-CV choice are reused instead of re-analyzed.
2. Run `python -m jobradar.apply` (add `--commit` to auto commit + push).
3. Each processed URL becomes a folder `NNN_YYYY-MM-DD_company_title/`,
   where `NNN` is the application's short **id** — the handle every command
   below takes, readable straight off the file tree so you never have to open
   a folder to find it. It also appears as the `ID:` line in `notes.md`. Full
   folder names and posting URLs still work anywhere an id does. The folder
   holds:
   - `job_description.md`: the retrieved posting (the ground truth used)
   - `tailored_cv.md`: the base CV tailored to this posting
   - `cover_letter.md`: only when the posting requires one, or on `full` tier
   - `notes.md`: fit assessment, what the tailoring emphasized, gaps, and a
     pre-submission checklist
   - `tailored_cv.pdf` / `cover_letter.pdf`: generated automatically for
     quick-tier applications (they are submitted as-is)
   - `tailored_cv_<lang>.md` (+ `.pdf`): for a non-English posting (German
     is the common Swiss case), the tailored CV translated into the posting's
     language. This is the version you submit; `tailored_cv.md` stays as the
     English fallback. The cover letter is already written in the posting's
     language, so it is not duplicated.
4. Changed your mind after seeing a quick draft? `python -m jobradar.apply
   --upgrade 42` re-drafts it at `full` tier, so it gets a cover letter. The
   id, folder, and reused report analysis stay; the writing call is paid
   again, so the tailored CV is rewritten too, and the PDFs of the replaced
   draft go stale until step 5's `--pdf` (the linter flags them). Only a
   draft can be upgraded — after `--mark-submitted` the folder has to keep
   matching what was actually sent.
5. Review. For `full`-tier applications, edit the markdown first, then
   convert yourself: `python -m jobradar.apply --pdf 42` (also accepts a
   folder name, a URL, or a path to a single `.md` file; re-run it after
   editing a quick-tier draft too, it overwrites the stale PDF).
6. Lint before you submit. `python -m jobradar.apply --lint 42`
   catches the mechanical mistakes a polished-looking draft hides: a missing
   deliverable, a PDF left stale after an edit, a wrong or leftover company in
   the cover letter, a dropped contact line. Add `--strict` to also require
   the `notes.md` review checklist to be ticked. (`--pdf` runs this same check
   and refuses to convert on an error, so this is mainly a final pass once you
   have finished editing.)
7. Submit, then record it:
   `python -m jobradar.apply --mark-submitted 42`. This step is
   deliberately not gated by the linter, so lint in step 6 rather than relying
   on it here.
8. Record outcomes as they arrive:
   `python -m jobradar.apply --mark-rejected 42` (likewise
   `--mark-withdrawn` and `--mark-offer`).
9. Archiving keeps this directory readable: applications with a recorded
   outcome — or submitted ones that stayed unanswered for `ghosted_days`
   (see `config/retention.yaml`) — move to `applications/archive/`;
   never-submitted drafts expire after `stale_draft_days`. Folders keep all
   material for reuse. Run a sweep any time with
   `python -m jobradar.apply --archive`, or archive specific applications
   with `--archive 42`.
10. Registered with a Swiss RAV? With `rav.enabled: true` in
   `config/search.yaml`, preview the month's proof-of-applications table with
   `python -m jobradar.apply --rav 2026-07`; once you have actually handed it
   in, record it with `python -m jobradar.apply --rav-filed 2026-07`. That
   saves the table to `reports/rav/2026-07.md`, marks the month as filed in
   `rav_filed.json`, and runs the archive sweep. Submitted applications then
   stay out of the archive until their month is filed. See
   [docs/APPLY.md](../docs/APPLY.md).

## PDF conversion

Markdown is converted to PDF via a headless Chromium/Chrome print (the
first of `chromium`, `google-chrome`, etc. found on PATH). On a machine
without one, the tool writes a styled `.html` next to the markdown instead:
open it in any browser and print to PDF. The intermediate HTML is deleted
when the PDF succeeds.

## When a page cannot be fetched

JavaScript-only career sites (Workday and similar) return no usable text.
The pipeline then creates a `*_pending` folder whose `job_description.md`
asks you to paste the posting text below a marker line. Paste it, re-run,
and the pipeline continues from there (the folder is renamed properly once
the company and title are known; it keeps its id through the rename).

## Files

- `queue.txt`: your to-apply list. Processed URLs are simply skipped
  (tracked in `applications.json`), so prune it whenever you like; the tool
  itself only removes a line when its application is archived.
- `applications.json`: the tracker behind dedup, `--mark-submitted`, and
  `--rav`. Edit by hand if needed; it is plain JSON.
- `rav_filed.json`: which months' RAV reports have been filed (written by
  `--rav-filed`); only exists, and only gates archiving, with `rav.enabled`.
- `archive/`: archived applications — same folder layout, tracked in
  `archive/applications.json`. Nothing in the pipeline reads them except
  `--rav` (merged rendering of past months) and the dedup guard that stops
  an archived URL from being silently re-drafted from the queue.
