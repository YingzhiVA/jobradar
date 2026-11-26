# Setup

The README's quick start gets you to a first run. This page is the rest:
what the profile can hold, how credentials work, and how to keep your copy
up to date.

## Your private copy

jobradar runs from a Git repository that holds your profile, your
application drafts and the state between runs: a clone of the public repo,
made as in the README's quick start, step 1. It shares the public repo's
history, which is what makes every update a plain pull. It needs no GitHub
account; for the cloud schedule you put it in a **private** repo of your own
([CLOUD.md](CLOUD.md#your-private-repo)). Not a fork: GitHub keeps a fork of
a public repo public.

## Credentials

Create an API key in the [Claude Console](https://platform.claude.com/).
Naming it `jobradar` and giving it a monthly spend limit keeps its usage
separate and capped. Put it in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Alternatives, if you'd rather not keep a key on disk: `ant auth login` stores
a short-lived profile the SDK picks up automatically; leave `ANTHROPIC_API_KEY`
out of `.env` in that case, because a set key always wins over a profile,
even an empty one. The cloud run has its own two options, see
[CLOUD.md](CLOUD.md).

`python -m jobradar.doctor` checks the key actually works, with a free
metadata call, and reports every other setup problem it can find.

## The profile

Everything under `profile/` is Markdown you write. None of it is functional
with the templates that ship in the repo.

1. **`profile/cvs/`** — one CV per role category, as Markdown (not PDF: the
   text goes straight to the model). The filename without extension is the
   CV's label in reports, so name it after the role, e.g. `product-manager.md`.
   Delete `example.md` once you have your own. One CV is fine; every posting
   is then scored against it. Have it in Word? Put the `.docx` in the folder
   as it is: `python -m jobradar.doctor`, and every run after it, writes a
   `.md` of the same name beside it and keeps it up to date when you change
   the Word file. Read it through once, since a two-column layout comes out
   one column after the other. See `profile/cvs/README.md`.
2. **`profile/identity.md`** — your career identity: who you are
   professionally, what you are moving toward and away from, the
   non-negotiables a filter can't express. Read on every scoring call; it is
   what interest fit means.
3. **`profile/stories/`** (optional) — prepared STAR interview stories. They
   sharpen skill scoring (evidence a terse CV bullet undersells), ground cover
   letters in concrete results, and power the interview-prep sheet. See
   `profile/stories/README.md`.
4. **`profile/evidence/`** (optional) — technical write-ups published on your
   own site, ingested rather than written by hand:

   ```bash
   uv run python -m jobradar.evidence --site https://you.github.io/notes/
   ```

   Used by the apply pipeline only (cover letters and prep sheets), never by
   daily scoring. Only pages tagged `og:type=article` are ingested. See
   `profile/evidence/README.md`.
5. **`profile/talking-points/`** (optional) — answers to the questions every
   interview asks. Read by no pipeline; it is there to rehearse from.

## The config files

| File | Holds | Docs |
| --- | --- | --- |
| `config/constraints.yaml` | Hard requirements: cantons, workload, office days | comments in the file |
| `config/companies.yaml` | Where to look: the boards to scan, by name — none until you add them | [SEARCH.md](SEARCH.md) |
| `config/boards/` | The shared catalogue of boards, per country plus `global.yaml`; maintained upstream, updated by `git pull` | [SEARCH.md](SEARCH.md) |
| `config/search.yaml` | How the search is steered, the RAV and ETH switches | [SEARCH.md](SEARCH.md) |
| `config/retention.yaml` | How long reports and applications stay active | [APPLY.md](APPLY.md) |

Every value in `search.yaml` and `retention.yaml` is optional; a missing key
means the built-in default. `python -m jobradar.config` prints what the
program will actually read.

## Getting updates

Code improvements land in the public jobradar repo; your copy pulls them.
Your own files are protected: the shipped `.gitattributes` marks `profile/`,
`config/`, `applications/`, `data/` and `reports/` as *yours* in a merge, so
an upstream change to a template or a config comment never overwrites what
you wrote. The one exception is `config/boards/`, the shared catalogue of
company boards: it merges like code, so a board that moved to another ATS is
fixed in your copy by the next pull. Don't edit it for your own needs; a board
of your own goes in `config/companies.yaml` with its `ats` and `slug`.
The commands are in the README, under [Getting
updates](../README.md#getting-updates). What the pieces are for:

- `git config merge.ours.driver true` makes git honour those `.gitattributes`
  rules. Without it a merge treats your files like any other.
- `git config pull.rebase false` makes a pull merge; without it, git refuses
  a pull with "Need to specify how to reconcile divergent branches".
- `git remote rename origin upstream` names the public repo as the source of
  updates, and leaves `origin` free for a private repo of your own.
- Committing before each pull: the protection works on what is committed.
  Git refuses to pull over an uncommitted change to a file the update also
  touches.

A copy made with GitHub's *Use this template*, the README's way up to 1.28,
has no history in common with the public repo, so it needs a one-time merge
first: `git merge --allow-unrelated-histories -X theirs upstream/master`,
after the two settings above and `git remote add upstream …` (README, under
Getting updates). With no common history git compares every file with
nothing, so each program file changed since the copy was made would be a
conflict; `-X theirs` settles those in the public version's favour, and the
merge driver still keeps your own files. Without the driver, `-X theirs`
would replace them with the templates. From then on the histories are joined
and `git pull upstream master` merges normally.

`python -m jobradar.doctor` reports whether updates are set up ("Getting
updates") and whether the merge driver is configured.

## Where things end up

| Path | What | Tracked in git |
| --- | --- | --- |
| `reports/<date>.md`, `reports/latest.json` | The daily report (a section per run on a day with several) and the latest run's summary | reports: only by the cloud run |
| `reports/practice.md`, `reports/practice.json` | The latest practice run's report and run record | no |
| `reports/runs.jsonl` | One line per run: sources, funnel, outcomes, warnings | yes |
| `data/seen_postings.json` | What has already been seen, so nothing is re-scored | yes |
| `applications/<id>_<date>_<company>_<title>/` | One folder per application | yes |
| `applications/applications.json` | The submission log | yes |

Reports and seen postings older than the windows in `config/retention.yaml`
move to `reports/archive/` and `data/archive/` automatically.
