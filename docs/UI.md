# The browser interface

jobradar can be worked from a terminal and an editor, as the rest of these
docs describe, or from a page in your browser. The page is a convenience for
an instance that is already installed: it does not install anything, and it
does nothing the commands don't. Use whichever suits the moment; the two can
be mixed freely.

```bash
uv run python -m jobradar.ui
```

This serves the page on your own computer (`http://127.0.0.1:8765/`) and
opens it. It is reachable from that computer only. Keep the terminal open
while you use it; Ctrl+C stops it. `--port 9000` picks another port,
`--no-browser` prints the address without opening it.

You need the quick start's steps 1 to 3 first (your copy of the repo,
`uv sync`, a Claude API key from the Console). Everything after that,
storing the key included, can be done on the page.

## Workflow

The page opens on a map of how jobradar works, which is the part every new
user has to learn whichever way they work. It is the root of the page: the
bar at the top shows it first, then the three phases it leads to, each with
the view that phase is worked in.

| Phase | Stages | View |
| --- | --- | --- |
| **Set up** | the key, your CV, the identity page, your must-haves, the companies to scan, the setup check | Settings |
| **Search** | the run itself, then reviewing what it surfaced | Matches |
| **Apply** | draft, edit and check, send and record, outcome | Applications |

A fourth list, **Now and then**, names what sits outside the daily loop
(finding more companies, archiving, the cloud schedule, updates).

Each stage has a number, which is only its place in the order, and a colour
with a label beside the title, which is how it stands in your copy:

- **green, "Done"**: a setup step that is filled in, or the check passing;
- **orange**: something there is waiting for you, and the label says what
  ("Next", "1 draft not sent", "1 problem");
- **grey**: nothing to do; a label only reports ("Last run 2026-11-12",
  "3 matches in the newest report", "3 awaiting an answer"). Reviewing
  matches is never marked as waiting, because nothing records that you read
  them.

A line at the top names the one thing to do next; when nothing is waiting
for you, there is none. Before your first search it also offers a practice
run, which scores made-up postings against your real profile for a few
cents, to show the whole loop before a real search is paid for.

**Show commands**, a switch at the top right, shows under each stage the file it
lives in or the command that runs it, with a button to copy it. The same
switch shows a "Terminal / editor" box on each settings form and the
commands for an application. It is off to begin with, to keep the page
quiet; switch it on while you are learning the terminal side or working both
ways. The choice is remembered in your browser.

The setup check is run by the page itself each time you open the map, so
its stage says whether the setup holds now, however it was last checked. It
is the same check as `python -m jobradar.doctor`, including the free call
that tests the key.

Once every setup step is done and the check passes, the seven setup stages
fold into one line, since they are done once; **Show the steps** unfolds
them. On a new copy they are open and the first one to do is marked. Its
button opens that step in Settings, and from there saving a step leads
straight to the next one, and the last to the check, which points back here
for the first search. Until the steps are done, the search buttons are
switched off and say why.

## The other views

- **Matches**: every report not yet archived, newest first. The matches of
  the newest are unfolded, each with both scores and the reasoning; earlier
  ones are a line each (title, company, scores, and the application if you
  drafted one) until you click them. An unfolded match sets why it fits and
  the concerns side by side, which is what deciding takes; what to highlight
  when applying is folded under them, for once you have decided. **Draft
  CV** starts the apply pipeline for a match, and **CV + cover letter** does
  the same at full tier.
  **Search now**, here and on the map, first asks you to confirm: a search
  is billed to your API key, and if the cloud schedule runs for the same
  copy the dialogue reminds you to pull first and push afterwards. Reports leave the list when they
  are archived (`reports_days` in `config/retention.yaml`); where nothing
  archives them, the page reads the newest 30. Archived reports sit in a
  folded section at the bottom, read twenty at a time when you unfold it;
  a match you applied to shows its application, archived or not.
- **Applications**: what you drafted, in up to three groups: **Needs you**
  (drafts not sent yet, and postings waiting for their job text),
  **Awaiting an answer** (submitted) and **Closed** (rejected, withdrawn or
  an offer, until the archive takes them). **Last update** is the day of the
  step the status names (drafted, submitted, or closed), or the day an
  interview sheet was last prepared, if that came later. Open one to read and
  edit its documents, run the pre-submission check, make the PDFs, and
  record that you submitted it or what came back. **Draft a posting you found**,
  beside the title, unfolds a box for the address of a posting the search
  didn't bring you; it is drafted the same way as a match. Archived applications sit in a folded
  section at the bottom, below the RAV card when RAV is on, read fifty at a
  time when you unfold it. They open read-only: what was sent, kept for
  reuse.

  Some career sites only show a posting to a browser, and the pipeline
  cannot read them. A draft for such a posting ends with a placeholder
  application, which the page opens straight away and says so. Paste the job
  description into the box there and press **Save and draft**; the draft is
  written from your copy. While it runs, the box can't be changed and the
  application's status can't be either: **Stop**, in the bar at the bottom,
  is the way to call it off. The box refuses text too short to draft from (under
  200 characters) before anything is paid for. This is the same placeholder
  the terminal route uses: pasting under the marker line of its
  `job_description.md` and re-running `jobradar.apply` works as before.
- **Settings**: the setup steps as forms, in the order and under the titles
  the map gives them, then the check. While setup is still open, each
  step's tab shows its number, or a tick once it is done; Settings opens on
  the first open step, and a line above the tabs counts the steps done. The
  last step, "How much you see", sets the score floors and the number of
  matches per run. It ships with values that suit most people, so saving it
  unchanged completes it. The form keeps strong matches to 1–5 and okay
  ones to 0–10 per search, and warns as you type when a value will make
  searches noticeably dearer (a low skill floor, a low strong-match bar,
  many matches per search); saving such values asks first.

A view with nothing in it yet says what will be there and has a button to
the next thing: the next setup step while setup is open, the first search or
a draft once it is done.

Every action that calls Claude asks before it starts and says roughly what
it costs: a search, a practice run, a draft, re-drafting with a cover letter
and the interview sheet. Checking a draft, making PDFs and recording a
status are free and start at once.

A search or a draft takes minutes. A bar at the bottom shows what it is
doing, with **Stop**, and **Details** for the command that was run and the
same lines a terminal would show. The pre-submission check reports there
too: "No problems found", or its findings unfolded under Details. One
command runs at a time, so while one does, the buttons that would start
another are switched off.

## Reading the buttons

- A **filled** button is the one action in its card, form or dialogue that
  does something: it runs a command or saves. "Search now" on the map,
  "Draft CV" on a match, "Save" on a form, "Mark as submitted" on a draft.
  There is at most one per area.
- An **outlined** button is everything else: an alternative to the main
  action ("Practice run", "CV + cover letter"), or a way to another view
  ("Open applications", "Change").
- A **text** button only changes what the page shows ("Fold", "Show the
  steps").

Colour is separate from this. Orange on the map says where something is
waiting for you; a filled button says what acts.

## Mixing it with the terminal and your editor

Every button runs the documented command (`jobradar.search.main`,
`jobradar.apply --url`, `--mark-submitted` and so on) and every form writes
the file you would otherwise edit:

| Form | File |
| --- | --- |
| Connect Claude | `.env` |
| Your CV | `profile/cvs/` |
| What you want | `profile/identity.md` |
| Must-haves | `config/constraints.yaml` |
| Where to search | `config/companies.yaml`; the web-search box writes `sources.web_search.enabled` in `config/search.yaml` |
| How much you see | `config/search.yaml` |

Saving a form changes only the lines whose values changed. Comments, the
order of a list, and keys the form doesn't show (the schedule, the ETH
board, `allowed_cities`, a board with its own `ats` and `slug`) stay exactly
as written, and saving a form you didn't change leaves the file untouched.
If a file is laid out in a way the page can't edit line by line, it is
written out fresh from its values instead, without its comments; the shipped
files and ordinary edits to them never reach that case.

Two things to know when working both ways:

- Only one command runs at a time from the page, but the page doesn't know
  about a run you started in a terminal. Don't run two searches at once.
- The page reads files when it shows them. After editing a file in your
  editor, reload the page before saving the same form.
- The page leaves Git alone: it never pulls, commits or pushes, and works
  the same on a copy that is never committed anywhere. If the cloud schedule
  runs your searches, `git pull` first to see the newest report on the page;
  what you draft or record on the page stays uncommitted until you commit it
  yourself.

## What stays in the terminal

Company discovery, the board health check, stories and evidence, archiving,
the cloud schedule and updating your copy. The Workflow page lists each with
its command and the doc that explains it.

With RAV switched on (Settings → How much you see), the Applications page
has a card for the monthly proof: pick a month to see its table, and press
**Mark as filed** once you have handed it in. That runs `jobradar.apply
--rav-filed`, which saves the table to `reports/rav/`, records the month,
and moves applications that were kept only for that month's proof to the
archive. Past months not yet filed are named on the card and in the
Workflow page's "Now and then" list.

## How it is built

`src/jobradar/ui/`: a standard-library HTTP server bound to `127.0.0.1`, and
one page of plain HTML, CSS and JavaScript with no build step. Requests must
carry a token made at start-up and come from the page itself, so another
site open in the same browser cannot reach it. The module docstrings in
`server.py`, `jobs.py` and `yamledit.py` have the details.
