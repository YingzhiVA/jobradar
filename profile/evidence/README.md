# Published evidence (field notes, project write-ups)

Markdown copies of the technical write-ups published on your portfolio site.
Unlike the rest of `profile/`, **you don't write these by hand** — they are
ingested from the live site:

```bash
# One-off, or whenever the site gains a note:
python -m jobradar.evidence --site https://yourname.github.io/notes/

# Set JOBRADAR_EVIDENCE_SITE in .env and --site can be dropped.
python -m jobradar.evidence

# Preview without touching the filesystem; drop files pulled from pages that
# no longer exist.
python -m jobradar.evidence --dry-run
python -m jobradar.evidence --prune
```

The crawler starts at the URL you give it, never leaves that origin or climbs
above that path, and writes one file per page tagged
`<meta property="og:type" content="article">`. Collection and section indexes
are followed for their links but never written — they are navigation, not
evidence. Files that haven't changed are left alone, so a re-sync that finds
nothing new leaves a clean `git status`.

## Why they're a separate profile source

The CVs and `stories/` cover *what you did and what came of it*. These cover
*how you think about the work* — the decisions, the trade-offs, the things
that went wrong first. They differ from a story in three ways that matter
enough to keep them apart:

- **They're public.** Each one carries its source URL, so a cover letter can
  link it and an interviewer can read it. A prepared story can only be
  asserted; a published note can be checked.
- **They're technical.** The prepared stories in `stories/` are behavioral —
  managing up, conflict, ambiguity. These are the hands-on depth that a
  "walk me through something you built" question wants, and that a terse CV
  line about a side project badly undersells.
- **They're already generalized.** Notes drawn from work published without
  naming the client or employer say so in their `Published as:` line. Anything
  generated from them has to inherit that: see the constraint below.

## Where they're used

Loaded by `matching.load_evidence()` and passed to `build_profile_block()` by
the **apply pipeline only**:

- **Cover letters and CV tailoring** (`python -m jobradar.apply`) — concrete
  technical specifics, plus a link worth including.
- **Interview prep** (`python -m jobradar.apply --prep`) — a technical
  question can now be answered by a note, where before every one of them
  landed in `coverage_gaps` pointing at a CV bullet.

They are deliberately **not** in the daily scoring path: several thousand
words of technical detail would be paid for on every posting scored, every
day, to sharpen a signal the CV's project lines already carry well enough for
a first-pass screen.

## The one hard constraint

A note that says "details generalized" generalized them **on purpose** — the
client, the employer, or the numbers were left out so the public version was
safe to publish. Application documents generated from these notes must not
put any of that back, and must not invent a figure a note deliberately
withheld. The prompts say so explicitly; if you edit them, keep that clause.

## Optional

This directory is optional. Without it, cover letters fall back to the CVs and
stories, and interview prep to the stories alone — you just lose the technical
half of the evidence.

Files here are generated. Edit the source page and re-sync rather than editing
them in place, or the next sync overwrites your changes.
