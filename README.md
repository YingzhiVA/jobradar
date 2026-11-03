# jobradar

A job-search tool for Switzerland that reads every posting so you don't have
to. Instead of keyword alerts, it scores each posting on two things: **skill
fit** against your CV, and **interest fit** against a page you write about
what you actually want next. Hard requirements (canton, workload, office days)
are filtered out before any of that. What reaches you is at most one "best"
match a day plus a few "okay" ones, and a day with nothing is a normal day.

It finds postings itself: from the company career pages you choose (a
catalogue of 85 Swiss boards across twenty applicant-tracking systems ships
with it, grouped by industry, and none is scanned until you pick it), and from
Claude's web search. It never scrapes or automates LinkedIn. When you decide
to apply, it drafts a tailored CV and cover letter in the posting's language,
keeps a submission log, and can prepare an interview sheet from your own
stories.

## What it produces

One real run, from 2 October 2026. The selected boards and the web search
returned 4,094 open postings. 74 were new since the day before, 54 were in
the right place, and 44 cleared the hard requirements and were read in full.
Four reached the report. This is the top of it, with the employer's name
replaced; the wording is otherwise unchanged:

> **[BEST] Product Owner Generative AI & Data — [a Swiss digital agency]**
>
> - Location: Zurich, ZH, Switzerland
> - Skill score: 100/100, Interest score: 88/100
> - Best-fit CV: CV_AI_GenAI_Product
>
> This role is a strong match on every axis that matters most. [The company]
> is asking for a hands-on Product Owner who straddles GenAI and data —
> exactly the primary track in the career identity, not a stretch or a
> partial fit. The 80–100% engagement and direct involvement with
> engineering teams matches the working style: staying close enough to the
> technology to validate outputs, define technical requirements, and
> prototype rather than delegate the hard parts.

Each match continues with what to lead with from your CV, and the concerns
to expect in a screen — here, that a digital agency means client delivery
rather than owning a product, which none of the candidate's previous roles
had been.

Then there is everything that did **not** reach the report. One from the
same run, which a keyword alert would have been delighted with:

> **Internship: Data Analytics Intern, Product Control — [a Swiss private bank]**
>
> Skill score: 95/100, Interest score: 22/100 — not surfaced
>
> The candidate's technical depth — Python, SQL, data strategy, analytics,
> dashboards — is genuine and overqualified for the role. However, this is
> explicitly a 6–12-month internship programme for current students; the
> candidate is a 12-year veteran product manager. No amount of skill match
> resolves the structural mismatch: she would be radically overqualified
> and unsuitable for a student-track programme, and the role offers no
> scope for the hands-on AI/data leadership or 0-to-1 product building she
> is moving toward.

That is what the second score is for. On skill alone this posting scored 95
and would have sat near the top of the day. The interest score, read off
your own page about what you want next, is what keeps it out — and the
reason it was dropped is written down, so you can tell a bad call from a
correct one and move your floors (`min_skill`, `min_interest`) accordingly.
Every posting scored on a run is kept with its reasoning in
`reports/runs.jsonl`, matched or not.

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
free (MIT). What a run costs depends mostly on how many company boards you
select. Rough figures with the default models, from October 2026:

| Activity | About |
| --- | --- |
| A daily run with no boards selected (web search only) | $0.05–0.10 |
| A daily run on the author's setup of about 80 boards | $0.20 |
| One drafted application (CV + cover letter) | $0.25–0.45 |

**Your first run is the exception.** It treats every posting currently open
on the boards you chose as new, and scores all of those that pass your
constraints in one go. With every board in the catalogue selected that is
roughly $4–10, twenty to fifty times a normal day; with five or ten boards,
usually one to two dollars. That is why no board is selected out of the box:
start with five or ten, and add more a few at a time. [docs/COSTS.md](docs/COSTS.md) has the details and how to check your
own spend.

The quick start below has you make a dedicated API key with a monthly spend
limit, so the tool's usage shows on its own and cannot exceed what you chose.

## Quick start (about ten minutes)

1. **Make your own private copy.** Click **Use this template → Create a new
   repository** on GitHub and choose **Private**. Your CV and application
   drafts will live in that repo, so it must not be public.
2. **Get a Claude API key.** Sign in to the [Claude
   Console](https://platform.claude.com/), add some credit under **Billing**
   (a new account has none, and $10 covers your first run and more than a
   week of daily runs), then create a key under **API keys**.
   Name it `jobradar` and set a monthly spend limit, so its usage shows on
   its own and can never exceed what you chose. Copy the key when it is
   shown: it starts with `sk-ant-`, and the Console won't show it again.
3. **Clone and install.**

   ```bash
   git clone https://github.com/<you>/jobradar.git && cd jobradar
   python3 -m venv .venv
   ./.venv/bin/pip install -e ".[dev]"
   cp .env.example .env        # then paste your key from step 2 into .env
   ```

4. **Tell it who you are.** Two files, both Markdown:
   - `profile/cvs/` — add your CV as a `.md` file (one per role type if you
     have several), then delete `example.md`. A Word CV (`.docx`) works too:
     drop it in and the setup check in step 7 makes the Markdown for you.

     **Use your full base CV, not one you trimmed for an earlier
     application.** Everything reads from it. Skill fit is scored against it.
     When you apply, the tailored CV is made by cutting bullets from it for
     that one posting, and nothing is ever added that it doesn't contain.
     Experience left out here can't earn a match and can't appear in an
     application. So include every role, project and skill you would want
     considered, and write each bullet as well as you can. Tailoring picks
     bullets, reorders them and matches their wording to the posting, but it
     won't fix a weak one. If you keep one CV per role type, each one should
     still cover all your experience. They should differ in emphasis, not in
     what they leave out.
   - `profile/identity.md` — replace the template with a real page about what
     you are moving toward and away from. This drives interest fit; vague
     text gets vague scores.
5. **Tell it what is non-negotiable** in `config/constraints.yaml`: cantons,
   workload range, office days. The shipped example is three cantons around
   Zürich.
6. **Pick the company boards to scan.** None are selected.
   `config/boards.yaml` lists the boards jobradar knows, grouped by industry,
   with the list of sections at the top of the file. Add the name of each
   board you want to `config/companies.yaml`, one per line.
   **Start with five or ten**: your first run scores every open
   posting on the boards you chose, so it costs far more than a normal day
   (see [What it costs](#what-it-costs)). You can add more later, a few at a
   time. With none chosen, only web search runs.
7. **Check the setup**, then do a dry run (real Claude calls on sample
   postings, no live sources), then a real one:

   ```bash
   ./.venv/bin/python -m jobradar.doctor
   ./.venv/bin/python -m jobradar.search.main --dry-run
   ./.venv/bin/python -m jobradar.search.main
   ```

   The report lands in `reports/<date>.md`. Read a few real runs before you
   trust the schedule.

Everything else is optional and explained in the docs below: steering how
many matches you see, the cloud schedule, the apply pipeline.

## How it fits together

Three phases, each its own module, sharing the profile you set up once:

1. **Company discovery** (`jobradar.discovery`), occasional: find companies
   worth scanning and feed them into `config/companies.yaml`. Optional; the
   shipped catalogue in `config/boards.yaml` already covers 85 Swiss boards.
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
