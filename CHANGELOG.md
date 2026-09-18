# Changelog

What changed, newest first. Users run jobradar from a clone, so the practical
way to get these is `git pull upstream master` (see docs/SETUP.md); the
version numbers exist to give changes a name.

## 1.0.0 — first public release (2026-09-18)

jobradar had run privately every weekday for three months before this. The
1.0.0 work was about making it somebody else's tool as well:

- **Nothing is assumed about your situation.** The monthly Swiss RAV
  proof-of-applications table is now `rav.enabled` in `config/search.yaml`,
  off by default, and with it off the application archive follows the
  retention windows alone. ETH Zurich's job board is likewise a switch with
  configurable categories, rather than two categories hardcoded for one
  person's field.
- **`python -m jobradar.doctor`** checks a setup before the first run and
  names what is missing: credentials that don't work, a profile still holding
  the shipped template, a config file that won't parse, no browser for PDF
  export.
- **The cloud run takes an API key.** Add `ANTHROPIC_API_KEY` as a repository
  secret and the daily GitHub Actions run works; workload identity federation
  remains for those who prefer to store no key at all. With neither
  configured the workflow prints a setup hint and exits green instead of
  failing.
- **The daily email has an HTML version**, with clickable links, alongside
  the plain text.
- **Model overrides work.** `JOBRADAR_SCORING_MODEL` and
  `JOBRADAR_WRITEUP_MODEL` were documented but never read; every stage's
  model variable is now resolved when the stage runs, so a value in `.env` is
  honoured.
- **Documentation rebuilt** around a ten-minute quick start, with the
  reference material in `docs/` and honest cost figures in `docs/COSTS.md`.
- **The company list ships as a starting point**: 67 Swiss employers across
  sixteen applicant-tracking systems, with each board's quirks documented.

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
