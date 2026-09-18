# Changelog

What changed, newest first. Users run jobradar from a clone, so the practical
way to get these is `git pull upstream master` (see docs/SETUP.md); the
version numbers exist to give changes a name.

## Unreleased

- **CVs can come from Word.** Put your `.docx` in `profile/cvs/` as it is.
  The setup check and every run make a Markdown CV of the same name from
  it, with your text word for word and the headings, bullets, bold and
  links kept, and make it again whenever you change the Word file. There is
  nothing to run and no model call, so it costs nothing. Once you edit the
  Markdown yourself it is never overwritten. Read the result through once:
  a two-column layout comes out one column after the other. An old-format
  `.doc` is not read, but the setup check names it and asks you to save it
  as `.docx`.

## 1.1.1 — company catalogue grouped by industry (2026-09-18)

- **The company catalogue is grouped by industry.** `config/companies.yaml`
  now sorts its 67 boards into twelve sections, from big tech and AI to
  pharma, medtech, finance and consulting, listed with their board counts at
  the top of the file. No board was added or removed, and each keeps its
  notes. Your own copy is left alone on pull; to see the new layout, run
  `git show upstream/master:config/companies.yaml`.

## 1.1.0 — Google and NVIDIA boards (2026-09-18)

- **Google, YouTube and DeepMind can be scanned.** A new `google` board type
  reads the job feed Google publishes for job aggregators, descriptions
  included, and keeps the Swiss roles (44 when this was written). Its slug
  names the employers to keep: `Google|YouTube|DeepMind`.
- **NVIDIA's Workday board works.** NVIDIA lists its countries in a place the
  Workday connector did not read, so the board was refused as too large to
  scope. It now narrows to the Swiss roles (about 40) before fetching any.
- **Both ship commented out.** Pulling this release does not change your own
  `config/companies.yaml`. To add them, copy the two entries from the
  template: `git show upstream/master:config/companies.yaml`. Adding both
  scores about 80 extra postings once, on the first run.
- **The roadmap names the big-tech sites to come, and one that won't.**
  Microsoft and Amazon are candidates. Meta is not planned: its careers site
  forbids automated collection.

## 1.0.1 — company boards are opt-in (2026-09-18)

- **No board is scanned until you choose it.** 1.0.0 shipped with 67 company
  boards switched on. A new user's first run then scored every open posting on
  all of them at once, costing many times a normal day, and mostly for boards
  chosen for one person's field. `config/companies.yaml` now ships every board
  commented out, as a catalogue to pick from. What a first run costs, and why
  to start with a handful of boards, is in `docs/COSTS.md`.
- **A company list with nothing switched on no longer crashes the run.** With
  every entry commented out the file reads as an empty value, which the
  company-board source did not accept.
- **One half-edited entry no longer silences every board.** An entry with a
  name but no slug, or the reverse, used to raise and take the whole
  company-board source down for the run. Now only that board is skipped, with
  a warning naming it.
- **The setup check reads the company list properly.** `python -m
  jobradar.doctor` fails on an entry uncommented only in part, on an unknown
  applicant-tracking system, and on lines that YAML would silently hand to the
  entry above. It warns before a first run over many boards.
- **Two dead boards are removed.** The boards recorded for DeepMind and
  Parashift return 404, so the catalogue no longer offers them.

## 1.0.0 — first public release (2026-09-18)

Two things are in this release: five weeks of development after 0.5.0, during
which the tool kept running every weekday, and the work of making it somebody
else's tool as well as its author's.

### Search and matching

- **The search is steered from a config file.** Score floors, the bar for a
  headline match, how many matches a run may surface and how often to run at
  all now live in `config/search.yaml` instead of being constants in
  `ranking.py` and a cron line. Every key is optional and ships at its
  default; a value that is present but unusable fails the run with a message
  naming it, rather than reverting silently and searching wrong for days.
  `python -m jobradar.config` prints what the program will actually read.
- **A link that could not be confirmed is no longer reported as if it had
  been.** A run surfaced two closed roles as live matches: the postings
  returned 410 from a normal network, but the employer's site answered the CI
  runner's address with a 403, so nothing detected the closure. Matches now
  carry a liveness state, and a lead from web search is dropped only when two
  independent weak signals agree — the link could not be confirmed *and* the
  employer's own listing has no such role. Either signal alone only annotates,
  because bot protection is routine and an ATS listing can be paginated short.
- **Remote roles scoped to a country** are accepted through
  `remote_countries`, for boards that give a remote posting a country-level
  location and no town at all. Those can never resolve a canton, so a commute
  radius could never pass them however Swiss they were.
- **Scoring runs in parallel.** It is one independent call per posting and was
  the largest term in a run's wall clock; the calls now fan out across a small
  thread pool, bounded by the account's rate limit rather than the machine.
  The first posting is scored alone so it warms the prompt cache instead of
  every worker paying to write its own copy.

### Company coverage

Six more Swiss employers, reached by three applicant-tracking systems the
connector did not previously speak, taking it to sixteen dialects across 67
boards: AXA Switzerland (iCIMS behind a Jibe front-end), Zurich Insurance and
Sonova (SuccessFactors), Takeda (a Workday board underneath a Radancy site),
UBS (a BrassRing Talent Gateway, scoped by facet because each posting costs a
0.4 MB page) and Sensirion (Prospective). Roche was widened from one Swiss
site to all three. Most of these are large employers whose public careers page
is a front-end over an open board, so the work was finding the board rather
than writing a parser.

### Applying

- **Archiving.** Reports, seen-store entries and applications move out of the
  active views on windows set in `config/retention.yaml`, while staying
  committed. The sweep runs daily in CI rather than whenever it was next run
  by hand, so the age-based rules fire on their due date.
- **Short ids.** Every application has a number that prefixes its folder name
  and appears in its notes, so a command takes `42` rather than a folder name
  or a URL.
- **The pipeline narrates itself.** Drafting one posting is a page fetch and
  three to five model calls, the writing ones slow enough that a queue of five
  runs for many minutes. Every step now names itself and the model calls
  stream, so a terminal shows the text arriving and a log shows a heartbeat.
- **Published write-ups feed cover letters and interview prep.** Technical
  articles from your own site are ingested into `profile/evidence/` and used
  where a "walk me through something you built" question wants depth. They are
  deliberately kept out of daily scoring: several thousand words are worth
  buying once per application, not once per posting per day.
- **The applied-jobs folder is gone.** It seeded interest scoring and company
  discovery when the tool was new; the identity statement and the company list
  carry both signals now.

### Reliability

The identity token the cloud run mints is single-use and expires in about five
minutes, while a run with web search can take fifteen. The refresh loop now
keeps the previous token when a fetch comes back empty, instead of publishing
the empty result over a still-valid one — a real run survived on its cached
token and would have failed the next scoring stage.

### Becoming a public tool

- **Nothing is assumed about your situation.** The monthly Swiss RAV
  proof-of-applications table is `rav.enabled` in `config/search.yaml`, off by
  default, and with it off the application archive follows the retention
  windows alone. ETH Zurich's job board is likewise a switch with configurable
  categories, rather than two categories hardcoded for one person's field.
- **`python -m jobradar.doctor`** checks a setup before the first run and
  names what is missing: credentials that do not work, a profile still holding
  the shipped template, a config file that will not parse, no browser for PDF
  export.
- **The cloud run takes an API key.** Add `ANTHROPIC_API_KEY` as a repository
  secret and the daily GitHub Actions run works; workload identity federation
  remains for those who prefer to store no key at all. With neither configured
  the workflow prints a setup hint and exits green instead of failing.
- **The daily email has an HTML version**, with clickable links, alongside the
  plain text.
- **Model overrides work.** Two of them were documented but never read, and
  the rest were read before `.env` was loaded. Every stage's model variable is
  now resolved when the stage runs.
- **Documentation rebuilt** around a ten-minute quick start, with the
  reference material in `docs/` and measured cost figures in `docs/COSTS.md`.
- **The company list ships as a starting point**, with each board's quirks
  documented and one person's notes about which employers they liked removed.

### Before 1.0.0

The private releases that preceded this one are summarised here because the
public repository starts from a fresh history and cannot carry their tags.

- **0.5.0** — BambooHR, Avature and SuccessFactors connectors, taking the
  company-page source to sixteen ATS dialects; Johnson & Johnson reached
  through the existing Workday connector by reading country out of the flat
  location facet; company board health checks.
- **0.4.0** — the pre-submission linter (`--lint`), which catches the
  mechanical mistakes a polished draft hides: missing deliverables, a PDF
  left stale after an edit, a cover letter naming the wrong company, tracker
  drift. Per-run observability: one structured record per run in
  `reports/runs.jsonl`.
- **0.3.0** — interview preparation (`--prep`) built on STAR stories in
  `profile/stories/`, and the split into the three phases (discovery, search,
  apply).
- **0.2.0** — the apply pipeline: a chosen posting becomes a tailored CV, a
  cover letter in the posting's language, and a tracked application folder.
- **0.1.0** — the daily pipeline: discover postings from ATS boards and web
  search, filter on hard constraints in pure code, score skill fit and
  interest fit against the profile, report the few worth reading.
