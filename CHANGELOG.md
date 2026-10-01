# Changelog

What changed, newest first. Users run jobradar from a clone, so the practical
way to get these is `git pull upstream master` (see docs/SETUP.md); the
version numbers exist to give changes a name.

## 1.5.2 — CV dates on their own line (2026-10-01)

- **Role dates get their own line in tailored CVs.** A CV exported from Word
  often loses the tab between a job title and its dates, and the tailored CV
  copied that: "Product OwnerMay 2024 - August 2026". Title and dates are now
  split onto two lines whenever a tailored or translated CV is written,
  whatever the separator was (none, spaces, "|" or a comma), and the PDF sets
  the dates line in grey at body-text size. CVs already in `applications/`
  keep their markdown, but `--pdf KEY` renders them with the new layout.

## 1.5.1 — RAV postcode check (2026-09-29)

- **RAV postcodes match the posting's town.** When a posting names only a
  town, the model fills in a postcode from memory, and on postings listing
  several locations it gave Zug roles Lugano's 6900. The postcode is now
  checked against the location for about twenty Swiss towns that postings
  name often: a guess that belongs to none of the named towns is replaced by
  the first one's postcode. Towns outside that list keep the model's guess.
  Postcodes already in `applications.json` are not changed; check the PLZ
  column of `--rav` before filing.

## 1.5.0 — Robotics and industry boards (2026-09-29)

- **Nine more boards in the example watchlist.** Robotics and drones:
  Wingtra, Voliro, Gravis Robotics, Fixposition and the RAI Institute's Zürich
  office. Industry, hardware & quantum: ABB, Hitachi Energy, Mettler-Toledo and
  Zurich Instruments. Leica Geosystems is switched back on, and Stryker
  (Selzach) is listed but switched off.

- **Avature boards that page by offset alone are read in full.** Mettler-Toledo's
  careers site links its result pages as `?jobOffset=10` with no page-size
  parameter, and only its first 10 postings were fetched. The connector now
  reads the page size off those links. A posting whose page has no labelled
  location field takes its location from the page's schema.org data instead of
  arriving without one, which the canton filter would have dropped.

- **Already-seen Avature postings keep their titles.** A seen posting is taken
  from the board's listing without a detail request, and its title was read
  from its web address. Siemens' and Mettler-Toledo's addresses carry only an
  id, so those postings had no title, which weakened matching them against
  web-search leads. The title now comes from the listing itself.

## 1.4.1 — Faster fetching (2026-09-28)

- **Fetching is much faster.** Nine board types — Workday, SmartRecruiters,
  SuccessFactors, Avature, BambooHR, JOIN, BrassRing, Prospective and onlyfy —
  load one page per posting for its description, and until now did so for
  every posting on the board on every run, although nearly all of them had been
  seen before and were dropped straight after. They now skip that page for a
  posting already in `data/seen_postings.json` and take what they need from the
  board's listing. On the author's setup of about 65 boards that is about 950
  fewer page loads a day. Fetching every board took 3 minutes on the first run
  with the change; the day before, one slow careers site had stretched it past 48.
  Model costs do not change, since a posting already seen never reached the
  model. A posting still due another scoring attempt is fetched in full, and
  your first run over a board is unaffected: everything on it is new to you.

- **The seniority check reads German postings.** A requirement to manage
  people caps the skill score, but in German only the informal "du führst …
  Team" and a few set phrases counted. So "Sie führen direkt 4 regionale Sales
  Manager", "Erste Erfahrungen im Führen von Mitarbeitern" or "Leitung eines
  Vertriebsteams" could slip past for a candidate who has never managed anyone.
  They are now recognised. Leading without line authority ("fachliche
  Führung") and a stated willingness to lead still do not count.

- **The search log shows the time of each line**, so a slow board can be read
  off the log instead of guessed at.

- **`reports/runs.jsonl`: the company boards record `listing_only`,** the number
  of postings a run took from the listing without loading their page, under
  that source's `meta`. The record's schema version is unchanged.

## 1.4.0 — Checklist scoring (2026-09-23)

- **Skill scoring works from a checklist of the posting's requirements.** The
  model used to return a single skill score, and nearly everything landed on
  the same two numbers: measured against 32 real applications, three scores in
  four were 72 or 78 and none fell below the floor. The model now lists each
  requirement the posting states — quoted word for word, with its category,
  whether it is required or preferred, and whether you meet it — and the skill
  score is computed from that list in code. Scores spread across the whole
  range again, and `reports/runs.jsonl` records the checklist behind every one,
  so you can see which line cost a posting its place. Because scores now run
  lower as well as higher, look at a week of runs before retuning `min_skill`
  or `best_threshold` in `config/search.yaml`.

- **A requirement the posting calls optional no longer counts against you.**
  "…is a plus", "preferred", "von Vorteil", a "Nice to have" heading: about a
  quarter of the gaps the old scorer found were on lines like these. It is now
  about one in twenty-five.

- **Four kinds of requirement cap a score.** An unmet requirement for a work
  permit, a licence or certification, a working language, or line-management
  experience caps the skill score at the floor, so the posting still appears,
  ranked last, for you to judge. Working language is new: a posting demanding
  fluent German used to carry no penalty at all. Line management only counts
  when the posting asks for managing people; leading projects, workstreams or
  cross-functional teams does not.

- **What you need to do: say in `profile/identity.md` whether you can work in
  Switzerland.** Work-permit requirements are judged against your profile, and
  nothing in it said so, so a posting asking for the right to work here could be
  capped as a requirement you do not meet. A pull leaves your `identity.md`
  alone, so add a sentence yourself — that you are authorised to work here, or
  that you need sponsorship. The template has a "My work eligibility" section
  to copy: `git show upstream/master:profile/identity.md`.

- **A posting scoring just under the floor gets two more tries.** Scores for the
  same posting vary between runs, by around 16 points, so a posting within 10
  points below `min_skill` is scored again on up to two later runs before it is
  written off, rather than dropped after one unlucky draw.
  `data/seen_postings.json` records the tries left as `retries_left`.

- **A posting that fails to score is no longer lost.** A timeout, an API error
  or an answer that could not be read used to mark the posting as seen, so it
  never came back. It is now scored again on the next run, and each scoring
  call gets one retry within the run.

- **A remote posting in a canton you left out no longer gets through.** A
  posting marked remote, with Switzerland in its location, passed
  `remote_countries` whatever its town, so a role in Chiasso TI could reach a
  search set to the cantons around Zürich. Whenever the location names a town,
  your canton list now decides. `remote_countries` still admits roles listed
  only as "Switzerland".

- **A write-up that runs too long is retried, and marked if it stays cut off.**
  The occasional long write-up used to stop mid-sentence in the report. It is
  now retried once, and if it is still cut off the report says so under it.

- **Scoring costs more: about $1.03 a day instead of $0.66** on the author's
  setup of about 65 boards. A checklist is about 840 output tokens a posting,
  against about 230 for a single score, and the scorer now reads a posting up
  to 12,000 characters instead of 6,000, so requirements near the end of a long
  posting are no longer cut off. A first run over the whole catalogue is about
  $8–15. [docs/COSTS.md](docs/COSTS.md) has the breakdown; the token summary in
  the run log now shows output tokens too.

- **`reports/runs.jsonl` records are at schema version 5.** Each scored posting
  carries its `requirements`: every entry has the requirement as quoted, its
  category and strength as scored, the model's own `model_category` and
  `model_strength` before the code-side checks, a verdict, and a short piece of
  evidence. Existing lines keep the version they were written with.

- **Optional: keep what a run scored, to work on the scorer.** With
  `JOBRADAR_CORPUS_DIR` set, a run also writes every posting it scored,
  description included, to a dated JSONL file in that directory. It is off
  unless set; point it somewhere your `.gitignore` covers.

## 1.3.0 — Hexagon boards (2026-09-18)

- **Hexagon Robotics and Leica Geosystems can be scanned.** A new `onlyfy`
  board type reads career pages on onlyfy (formerly prescreen), keeping
  the Swiss postings. Hexagon Robotics (Zürich, the AEON humanoid) had 25
  open when this was written, Leica Geosystems (Heerbrugg SG) 25 of its
  37. The board asks for a second between requests, so each takes about
  half a minute a day. Both ship commented out: copy the two entries from
  `git show upstream/master:config/companies.yaml`.

## 1.2.0 — CVs from Word (2026-09-18)

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
