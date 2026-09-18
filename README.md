# jobradar

A job-search tool for Switzerland that reads every posting so you don't have
to. Instead of keyword alerts, it scores each posting on two things: **skill
fit** against your CV, and **interest fit** against a page you write about
what you actually want next. Hard requirements (canton, workload, office days)
are filtered out before any of that. What reaches you is at most one "best"
match a day plus a few "okay" ones, and a day with nothing is a normal day.

It finds postings itself: from company career pages (67 Swiss boards ship
with it, across sixteen applicant-tracking systems), and from Claude's web
search as a fallback. It never scrapes or automates LinkedIn. When you decide
to apply, it drafts a tailored CV and cover letter in the posting's language,
keeps a submission log, and can prepare an interview sheet from your own
stories.

## Who it is for

Anyone looking for a job in Switzerland who would rather read four good
matches than forty alerts. It is not tied to a field: matching is driven by
your CV and your own words, not by job titles, so it works for a product
manager, a lab technician or a controller alike. You need:

- An **Anthropic API key** (Claude does the reading). Nothing is hosted;
  the key stays on your machine or in your own GitHub repo.
- **Python 3.11 or newer** and a terminal you are willing to paste commands
  into. Everything below is copy-paste.
- Optionally a **GitHub account**, so the search runs every morning in the
  cloud and emails you, without your computer being on.

If you are registered with a **RAV** (regional unemployment office), there is
an optional mode that produces the monthly proof-of-applications table from
your submission log. Off by default.

## What it costs

You pay Anthropic for what the tool reads and writes; the tool itself is
free (MIT). Rough figures with the default models, measured in September
2026 (details and how to check your own spend in [docs/COSTS.md](docs/COSTS.md)):

| Activity | About |
| --- | --- |
| One daily search run | $0.50–0.70 |
| One drafted application (CV + cover letter) | $0.25–0.45 |
| A typical month: 22 runs, 15 applications | $17–25 |

Create a dedicated API key named `jobradar` in the Claude Console and set a
monthly spend limit there; then its usage shows on its own and cannot exceed
what you chose.

## Quick start (about ten minutes)

1. **Make your own private copy.** Click **Use this template → Create a new
   repository** on GitHub and choose **Private**. Your CV and application
   drafts will live in that repo, so it must not be public.
2. **Clone and install.**

   ```bash
   git clone https://github.com/<you>/jobradar.git && cd jobradar
   python3 -m venv .venv
   ./.venv/bin/pip install -e ".[dev]"
   cp .env.example .env        # then paste your ANTHROPIC_API_KEY into .env
   ```

3. **Tell it who you are.** Two files, both Markdown:
   - `profile/cvs/` — add your CV as a `.md` file (one per role type if you
     have several), then delete `example.md`.
   - `profile/identity.md` — replace the template with a real page about what
     you are moving toward and away from. This drives interest fit; vague
     text gets vague scores.
4. **Tell it what is non-negotiable** in `config/constraints.yaml`: cantons,
   workload range, office days. The shipped example is three cantons around
   Zürich.
5. **Check the setup**, then do a dry run (real Claude calls on sample
   postings, no live sources), then a real one:

   ```bash
   ./.venv/bin/python -m jobradar.doctor
   ./.venv/bin/python -m jobradar.search.main --dry-run
   ./.venv/bin/python -m jobradar.search.main
   ```

   The report lands in `reports/<date>.md`. Read a few real runs before you
   trust the schedule.

Everything else is optional and explained in the docs below: pruning the
company list, steering how many matches you see, the cloud schedule, the
apply pipeline.

## How it fits together

Three phases, each its own module, sharing the profile you set up once:

1. **Company discovery** (`jobradar.discovery`), occasional: find companies
   worth scanning and feed them into `config/companies.yaml`. Optional; the
   shipped list is a real Swiss watchlist to prune and extend by hand.
2. **Search** (`jobradar.search`), the daily run: fetch postings, filter,
   score, report the few worth your time.
3. **Apply** (`jobradar.apply`), on demand: turn chosen postings into
   tailored CV and cover-letter folders, plus interview prep and a submission
   log.

A posting that came from a daily report reuses that report's analysis when
you apply, so nothing is paid for twice.

## Documentation

- [docs/SETUP.md](docs/SETUP.md) — the profile in full, credentials, getting
  updates from this repo later
- [docs/SEARCH.md](docs/SEARCH.md) — the daily run: configuration dials, how
  a run works, the report and run log
- [docs/CLOUD.md](docs/CLOUD.md) — the GitHub Actions schedule and email
- [docs/APPLY.md](docs/APPLY.md) — drafting applications, linting, PDFs,
  outcomes, the optional RAV table
- [docs/DISCOVERY.md](docs/DISCOVERY.md) — finding more companies to scan
- [docs/COSTS.md](docs/COSTS.md) — what it spends and how to cap it
- [docs/DEVELOPING.md](docs/DEVELOPING.md) — tests, adding a source
- [docs/ROADMAP.md](docs/ROADMAP.md) — what is deliberately not built yet
- [CHANGELOG.md](CHANGELOG.md) — what changed between versions

## Tests

```bash
./.venv/bin/python -m pytest
```

No API key needed: the tests cover the deterministic parts (filters,
ranking, normalisation, config, the apply bookkeeping) with fixtures.

## License

MIT. The name is a description, a radar for jobs; it is not affiliated with
any job board that happens to use a similar one.
