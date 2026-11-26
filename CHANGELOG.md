# Changelog

What changed, newest first. Users run jobradar from a clone, so the practical
way to get these is `git pull upstream master` (see docs/SETUP.md); the
version numbers exist to give changes a name.

## 1.41.1 — Keep write-up bullet points (2026-11-26)

- **`apply` keeps the bullet points of a match's write-up.** When it read a
  match back from the daily report, it skipped every line starting with
  "- " as if it were the Link, Location or score line above the write-up.
  So a write-up that used a bullet list, often for what to highlight when
  applying, reached the CV and cover letter without those points. Only the
  facts under the match's heading are skipped now.

## 1.41.0 — Flag warnings worth a look (2026-11-26)

- **The report flags warnings worth a look.** A run now remembers what went
  wrong, so the next run can tell a new problem, or one that won't go away,
  from the usual noise. When something stands out, a **Run health** section
  at the end of the day's report names it:
  - a warning no earlier run logged;
  - the same warning about the same posting, board or site on three runs or
    more in a row (a Personio posting failed scoring on every run for about
    two weeks before anyone noticed);
  - a warning far more frequent than usual (a site suddenly refusing the
    run).
- **The email says so too, once.** It mentions new warnings, and a recurring
  one on the run it reaches three in a row, even on a day with no matches.
  It doesn't repeat it on later runs, and it leaves out the "more than usual"
  kind, so it doesn't become daily noise. The first new-warning flags appear
  after five runs with this version, which they need as a baseline.
- **`reports/runs.jsonl` counts every warning** of the run, by message, with
  what each was about (`health`). A practice run records and flags nothing.

## 1.40.0 — Read Personio ads from the job page (2026-11-26)

- **Personio postings without a description in the feed are scored now.**
  Some employers' Personio feeds list a job with no description at all: on
  26 November that was all eight of Legartis's postings and one of Unique's.
  These were sent for scoring with only a title, came back unscored, and were
  tried again on every run, so they never reached a report. jobradar now
  reads the description from the job's own page, once per new posting.
- **A posting with no description is no longer sent for scoring.** There is
  nothing in it to check against your CV, so scoring it cost two model calls a
  run and came back empty every time. It now waits, unseen, and is scored on
  the first run that finds a description for it. The run log names it.

## 1.39.0 — Ask each site politely (2026-11-26)

- **jobradar paces its requests to each site.** It used to send a board's
  requests back to back: on 25 November Johnson & Johnson's Workday board got
  20 requests in 3 seconds and refused the posting pages that came right
  after. Requests to one site are now at least a second apart, plus a little
  at random, and boards that share a site share the pace (all Workday boards,
  for example). Most boards take one or two requests, so a normal day's run
  takes a minute or two longer; a first run over many boards takes longer
  than that.
- **Every part of jobradar asks the same way.** Link checks, discovery, the
  monthly board check, the ETH board, Arbeitnow, and `apply`'s posting fetch
  and evidence crawl now go through the same paced connection as the company
  boards. So they too wait and try again when a site says "too many
  requests", and stop at a site whose bot check refuses them.
- **jobradar says who it is.** Its requests now identify as
  `jobradar/<version> (job search tool)`. Before, link checks claimed to be
  Chrome and board fetches sent a half-browser label. A link a site refuses
  (403) is still kept and marked unverified, but jobradar no longer asks a
  second time under a different label.
- **Proxy settings in the environment are no longer used.** jobradar's web
  requests ignore `HTTP_PROXY` and `HTTPS_PROXY` now. The company boards
  already did since 1.37.0. If you need a proxy, open an issue.

## 1.38.0 — Name boards a bot check blocks (2026-11-26)

- **Boards that a bot check blocks are named in the report and the email.**
  The Workday boards' "too many requests" answers on GitHub Actions turned
  out not to be a rate limit: Cloudflare, in front of Workday's job sites,
  answers the run with a bot check that only a web browser can pass. Waiting
  doesn't help, because the check goes by where a request comes from. It
  currently refuses GitHub Actions' servers and lets most home connections
  through (a run from home reached all 11 Workday boards it searched). Until
  now these boards were only skipped in the run's log. Now the report says
  which boards could not be searched and why, the email says so too, and a
  day with no matches but blocked boards reads "No matches today (N boards
  blocked)" and still sends the email, instead of passing for a quiet day.
- **A blocked site is not retried.** When Cloudflare's bot check answers, that
  board is skipped at once and the site's other boards are tried once each,
  so the GitHub run no longer spends about two and a half minutes retrying
  Workday.
- **Refusal pages are logged as readable text.** The run log shows what a
  refusing site's page says, not the first few hundred characters of its
  styling.

## 1.37.0 — Retry boards that say too many requests (2026-11-25)

- **A board that says "too many requests" is retried, not skipped.** Since
  16 November, Workday has answered the daily run on GitHub Actions with
  `429 Too Many Requests` for nearly every board, so Julius Baer, Roche,
  Novartis, Swisscom and the other Workday boards found nothing. A 429 used
  to skip the board at once. Now jobradar waits and tries again: as long as
  the server's `Retry-After` header says, otherwise about 5, 15 and 45
  seconds, then skips the board as before. Once two boards on one site have
  used up their retries, the rest of that site's boards are tried once, so a
  site that refuses everything adds about two minutes to a run, not a minute
  per board. The same applies to `503 Service Unavailable`.
- **The first refusal from each site is logged with its response headers
  and the start of its body.** They show whether the site is limiting how
  fast jobradar asks, which waiting cures, or refusing it outright, which it
  doesn't.

## 1.36.0 — Boards by industry on the setup page (2026-11-25)

- **The setup page lists every board by industry.** Global employers such as
  Google, NVIDIA and Schroders used to sit together under "Global employers"
  at the top, ahead of the Swiss boards' industries. `global.yaml` now uses
  the same industry headings as `ch.yaml`, and the page shows one list, so
  Schroders appears under finance beside UBS. Boards are sorted
  alphabetically within each industry.

## 1.35.0 — Finance and insurance boards (2026-11-25)

- **Five new boards in finance and insurance.** Add any of these names to
  your `config/companies.yaml` to scan them:
  - **Vontobel** (Zurich wealth and asset manager) and **Lombard Odier**
    (Geneva private bank, only a few Zurich roles), in `ch.yaml`.
  - **Swiss Life**, in `ch.yaml`. Most of its roughly 100 postings are
    advisor and sales roles at its regional agencies, which each posting
    names as its employer. The head-office roles are in Zurich.
  - **Schroders** (Zurich: Schroders Capital and wealth management) and
    **Partners Group** (Baar), in `global.yaml`, since both boards also
    serve London.
- **Prospective boards in Swiss Life's layout are read.** Prospective career
  centers come in two page layouts. jobradar read only the one Sensirion's
  board uses, so a board in the other found no jobs. Postings from either
  now name their employer at the start of the description when the board
  gives one.
- **Baar is recognised as canton Zug** without asking the model, which saves
  one call per Partners Group posting.

## 1.34.0 — CV PDF named for the recruiter, visible links (2026-11-25)

- **The CV's PDF is named `CV_<FamilyName>_<Position>.pdf`** instead of
  `tailored_cv.pdf`, for example `CV_Doe_Senior_Product_Manager.pdf`, so it
  reads well in a recruiter's downloads folder. The family name is everything
  after the first word of the CV's name line, so a two-part family name stays
  whole; the position is the posting's job title, without "(m/w/d)" or a
  workload percentage. A translated CV ends in its language (`_DE`). The
  markdown keeps its name (`tailored_cv.md`), and the cover letter's PDF is
  unchanged. Making the PDF again removes an old `tailored_cv.pdf`. A folder
  drafted before this has its PDF under the old name, so `--lint` reports it
  missing until you run `--pdf` again.
- **Links in the PDFs are blue and underlined.** They used to look like the
  text around them, so a reader could miss that LinkedIn, a portfolio or a
  GitHub project could be clicked. The underline keeps them visible on a
  black-and-white print.

## 1.33.0 — Two-page tailored CVs (2026-11-24)

- **Tailored CVs now fit on two pages.** They used to run to three: the
  tailoring step cut only bullets it judged irrelevant to the posting, so a
  posting that matched you well kept nearly all of a three-page base CV. It
  now works to a budget of about 700 words. Bullets are a result plus one
  detail, at most two lines, with no long list of activities. A responsibility
  stated with no result ("responsible for stakeholder communication") is cut
  before an achievement, and nothing is said twice. Tried on three earlier applications,
  the CVs went from 1,050 to 617, 1,060 to 625 and 509 to 461 words, all
  on two pages. Your base CVs stay complete; only the tailored copy is
  shortened.
- **Education and certifications are compressed.** Each degree takes one
  line, and all courses and certificates share one line with no description.
- **Rephrasing no longer adds facts.** Bullets are reworded in the posting's
  terms, and that sometimes slipped in things the posting names but your CV
  doesn't, such as a team you never worked with. The tailoring step is now
  told to change only the wording, never a team, function, tool, figure or
  scope. Still read the CV before you submit: this makes such slips rarer,
  but can't rule them out.
- **The pre-submission check warns about a CV over 750 words** (`cv-length`),
  the length at which earlier CVs ran onto a third page.

## 1.32.0 — Ashby locations, Overstory board (2026-11-23)

- **Ashby postings now list every location they are open in.** jobradar
  used to read only an Ashby posting's main location. A remote-first employer
  that lists "United States" first and Switzerland among the other countries
  it hires in was therefore rejected as a US role by `remote_countries`.
  Now all of a posting's locations are read, as for Workday and Oracle.
- **New board: Overstory** (in `global.yaml`), vegetation intelligence for
  electricity utilities, built on satellite imagery and machine learning. It
  has no Swiss office but hires remotely in Switzerland. Add `Overstory` to
  your `config/companies.yaml` to scan it.
- **The README now covers Linux and says where macOS stands.** Most Linux
  distributions don't include Git, so the quick start's first command failed
  with "git: command not found"; the README now says how to install it (and
  what to do without administrator rights), and gives a `wget` line for uv
  where `curl` is missing. Tested on clean Ubuntu 24.04 and 22.04, Debian 12
  and Fedora 41. macOS is untested so far; reports are welcome.
- **The pip route upgrades pip first.** On Python 3.10 the pip that comes with
  it doesn't refuse jobradar, it tries package versions for hours; a current
  pip says at once that 3.11 or newer is needed.
- **Package metadata uses the current licence format**, which setuptools
  will soon require.

## 1.31.0 — Fuller setup: remote options, match volume, web search (2026-11-19)

- **The must-haves form now has every remote option.** It used to offer a
  single "fully remote jobs within Switzerland" box. Now you choose between
  no remote jobs, fully remote jobs based in the countries you list, or
  fully remote jobs anywhere (`remote_ok`). The country list shows what
  `constraints.yaml` says, so countries you added by hand can now be seen and
  edited in the browser.
- **"How much you see" is now a setup step, the last one before the check.**
  The score floors, the number of matches per search, and the RAV switch
  used to sit outside setup, where it was easy to never find them. Saving the
  form unchanged completes the step, since the shipped values suit most
  people. A copy that has already run a search counts it as done. A copy set
  up in the browser that has never searched shows 5 of 6 steps done after
  updating, until that form is saved once.
- **The web search can be switched off.** "Where to search" has a box for
  it, on by default. It is `sources.web_search.enabled` in
  `config/search.yaml`; your copy of that file keeps its own text on update,
  so the key isn't there until you set it, and leaving it out keeps the web
  search on. `--no-web-search` still switches it off for a single run.
  With no company selected and the web search off, a search would find
  nothing, so the form refuses that and the setup check reports it as a
  problem.

## 1.30.0 — Python 3.14 (2026-11-18)

- **jobradar now runs on Python 3.14.** New installs get 3.14 instead of
  3.12, and so do the daily and monthly cloud runs. An existing install
  moves at its next `uv sync` (uv rebuilds `.venv` on 3.14 by itself). The
  full test suite passes on 3.11, 3.12, 3.13 and 3.14, and 3.11 or newer
  stays enough for a pip install.
- **The quick start installs before it asks for a Claude key.** The key is
  needed only from step 4, and the install is where something can go wrong,
  so nobody adds credit before the install works. The uv install lines are
  labelled by system, Windows first: running the macOS line in PowerShell
  fails with "sh is not recognized".
- **`uv.lock` no longer changes on its own after you install.** The one in
  1.29.0 still carried the previous version number, so the first `uv sync`
  rewrote it and left your copy with a modified file. If `git status` shows
  `uv.lock` modified, that is all it is: the update commands in the README
  (commit, then pull) take care of it.

## 1.29.0 — Install with uv, and a copy that takes updates (2026-11-18)

- **Install with uv, the same way on macOS, Windows and Linux.** The quick
  start's `python3 -m venv` and `./.venv/bin/pip` worked only where Python
  3.11 or newer was already installed and the shell was a Unix one: a new Mac
  has Python 3.9, Windows has no `python3` and no `bin/`. Now `uv sync`
  installs jobradar and downloads the Python it needs, and every command is
  `uv run python -m jobradar.…` on every system, in the README, the docs and
  the browser page. New installs use Python 3.12, the version the cloud runs
  use; `uv.lock` fixes the package versions everyone gets. pip still works
  (the README says how). `uv sync` uses the same `.venv` folder as before,
  rebuilding it if its Python doesn't match. After an update, run `uv sync`.

- **A new copy is now a clone of the public repo, not a "Use this template"
  copy.** A template copy starts with a history of its own, so it could only
  take updates after a one-time merge that the docs got wrong (it stopped
  with a conflict on every program file changed since the copy was made), and
  a release that renames one of your files would leave your text behind in
  the old one. A clone shares the public history: every update is
  `git pull upstream master`, and your files follow a rename. The quick start
  now clones, and no GitHub account is needed until you want the cloud run;
  docs/CLOUD.md shows how to put your copy in a private repo for it. Not a
  fork: GitHub keeps a fork of a public repo public.
- **How to get updates is in the README**, under "Getting updates", including
  the one-time step for a copy made with "Use this template" (now with the
  `-X theirs` it needed, after the setting that keeps your files). The
  browser page's "Get updates" task, which showed only
  `git pull upstream master`, now shows the commands for where your copy
  stands, committing your own changes first; the setup check says whether
  updates are set up.

## 1.28.0 — Same-day searches and practice runs keep your matches (2026-11-18)

- **A second search on the same day no longer loses the first one's
  matches.** Each run replaced the day's report, `reports/<date>.md`, and the
  first run's postings were already marked as seen, so they never came back:
  they vanished from the report and the Matches page. A later run now adds a
  "Later run at HH:MM" section to the day's report instead.
- **A practice run no longer touches your real results.** It differed from a
  real search only in where its postings came from, so it replaced the day's
  report (a practice run after a real one wiped that day's real matches),
  marked its sample postings as seen (a second practice run found nothing),
  and counted as the day's run, which could make the cloud run skip the
  day. A practice run now writes only `reports/practice.md` and
  `reports/practice.json`, both untracked: it marks nothing as seen, writes
  no `latest.json`, adds nothing to the tracked `runs.jsonl`, and never
  counts as a day's run. The Matches page shows it in its own section until
  your next real search.
- **A practice run always shows its sample match.** It used to apply your
  score floors, so with a raised interest floor, or a CV far from the sample's
  product-manager role, it often showed nothing at all. It now shows every
  sample it scores, with a note on any that a real search would leave out
  below your floors.
- **Sample postings no longer link to example.com.** A practice run's
  matches say they are made up instead of offering a link to a placeholder
  page.
- **Each match says where it was found:** one of your company boards, or the
  web search. The web search runs alongside your boards and can find
  companies you never listed, which used to look like a bug. The README's
  board step now says so too.
- **Constraints with non-ASCII text now read correctly on Windows.** The
  search read `config/constraints.yaml` in the system's code page instead of
  UTF-8, so on Windows an umlaut ("Zürich") came out garbled, and some
  characters stopped the run.
- **The browser interface shows Windows commands in Windows form.** Each
  command it displays for copying used to start with `./.venv/bin/python`,
  which doesn't exist on Windows. There it now reads
  `.\.venv\Scripts\python`. The buttons were not affected.
- **PDFs on Windows and macOS without any setup.** The apply step looked for
  Chrome or Chromium only on PATH, where Windows and macOS don't put them, so
  there it wrote `.html` files to print by hand. It now also looks in the
  usual install folders, and so finds the Edge that comes with every Windows
  10 and 11, or Chrome, Edge, Chromium or Brave in `/Applications` on a Mac.

## 1.27.0 — Travel and bonus figures no longer read as the workload (2026-11-18)

- **A percentage counts as the workload only when the posting says it is
  one.** Any "N%" in a description used to be taken as the job's workload,
  so "travel up to 15% of the time" made a full-time role a 15% job, and the
  workload filter dropped it. 172 postings were dropped that way or
  similarly, among them On's LightSpray and Sports Engineer roles, Roche and
  Hitachi Energy roles whose titles say "80-100%", and Google roles read off
  a "15% bonus target". Now:
  - The title is read first, as Swiss boards put the workload there
    ("Tender Specialist 80-100% (f/m/d)"); it was not looked at before.
  - In the description, a figure counts when a workload word is in the same
    sentence (Pensum, Teilzeit, part-time, taux d'activité, "(m/w/d)", …),
    or when it is a range from 20% up ("70-90%"). Never when the sentence
    makes it travel, a bonus, remote work, a pension share and the like, and
    never below 10% or above 100%.
  - Ranges written "80 bis 100%" and "60 à 80%" are read.
  - A figure that can't be placed is left unknown for the gap-fill model to
    read, as a posting with no figure always was. A bare "100% role" is now
    one of those.
- Postings already dropped this way stay settled in
  `data/seen_postings.json`; remove their entries to have them checked again.

## 1.26.0 — Applications page fixes, prep counts as an update (2026-11-17)

- **Browser interface:**
  - While a command runs, the page no longer offers what can't be done
    meanwhile. It is drawn again as soon as a command starts, so its buttons
    (Save and draft, Draft CV, Search now, Make PDFs) are switched off at
    once rather than after the next reload. While a draft for a posting runs,
    the pasted job text can't be changed and the application's status
    buttons are hidden: **Stop**, in the bar at the bottom, calls it off.
  - **Check before sending** reports in the bar at the bottom, as drafting
    and making PDFs do: "No problems found", or its findings unfolded under
    Details. The two boxes that both said there was nothing wrong are gone.
  - A **Draft** pill is an orange outline, so it can no longer be taken for
    the green **Submitted** one, in the dark theme especially.
  - The breadcrumbs above a view leave out the phase ("Apply"), which was
    not a place to go back to: Workflow › Applications › #041.
  - The RAV card has room between the month's table and the reminder of
    drafts not yet submitted.
- **An interview prep sheet counts as an update.** `--prep` records the day
  in the tracker (`prepped_on`), and the Applications list's **Last update**
  shows it when it is later than the status's own date, so an application
  you are preparing an interview for moves to the top of its group. Sheets
  written before this version have no date recorded.

## 1.25.0 — Guided setup, clearer matches and applications (2026-11-16)

- **Browser interface, for someone opening it the first time:**
  - Settings follows the setup. Its tabs are the workflow page's setup
    steps, in that order and under those titles (they were in reverse, with
    the score floors first and the key sixth). While setup is open each tab
    shows its number or a tick, Settings opens on the first open step, and
    saving a step leads straight to the next one and finally to the check,
    instead of back to the workflow page after each.
  - An empty page says what comes next. Matches and Applications, before
    there is anything in them, say what will be there and have a button to
    the next setup step, or to the first search or draft once setup is done.
    Search and draft buttons that need the setup are switched off until then
    and say why; on Matches they used to be live. Before the first search,
    the practice run is offered beside the real one.
  - Matches starts with the matches. The box for a posting you found
    yourself moved to the Applications page, folded behind **Draft a posting
    you found**: it starts an application and never passes through a
    search. An unfolded match sets why it fits and the concerns side by side,
    with what to highlight when applying folded under them, so the page
    is shorter and its lines are half as wide.
  - The Applications list is grouped by what an application asks of you:
    **Needs you** (drafts, and postings waiting for their job text),
    **Awaiting an answer**, and **Closed**. The two date columns became one,
    **Last update**: the day of the step the status names, so a rejection or
    a withdrawal shows the day it was recorded instead of the day it was
    drafted.
  - Two small fixes: the setup check no longer says "no company companies
    selected yet", and the page has its own tab icon, which also ends the
    refused request for one that browsers logged as an error on every load.

## 1.24.1 — UI polish (2026-11-16)

- **A PDF print that writes nothing no longer passes for a fresh one.** The
  PDF step counted a print as done if a PDF existed afterwards, so when the
  browser exited without writing, the previous run's PDF was reported as
  new and drifted from the edited Markdown. That happens with the snap
  build of Chromium, which can only write inside your home folder (a repo
  under /tmp, or on /mnt/c in WSL). Such a print now counts as failed, falls
  back to HTML as any failed print does, and the log says why.
- **Browser interface:**
  - Saving an edited CV or cover letter makes its PDF again in the same
    step, and a document whose PDF is missing, older than its text or only
    an HTML fallback says so above the text.
  - Archived reports and applications are listed under the live ones,
    folded until opened and read a page at a time. Archived applications
    open read-only, and a match you applied to links to its application
    even once that is archived, instead of offering a second draft.
  - An application's documents are tabs, with the PDF link and Edit at the
    end of the tab row; its actions sit in two labelled rows (work on it,
    update status), and a closed application shows none. The posting link
    moved to the line under the title, and the job description is
    read-only.
  - The banner on the workflow page appears only when something is
    waiting for you.

## 1.24.0 — Browser interface (2026-11-16)

Nothing to edit after pulling: every change below is either new or a
warning.

- **A browser interface, beside the terminal.** `python -m jobradar.ui`
  opens jobradar as a page on your own computer, for a copy that is already
  installed. It opens on a map of the workflow (set up, search, apply) that
  shows where each stage stands in your copy: which setup steps are still
  open, how the last search narrowed, how many drafts are waiting, and
  whether the setup check passes right now. From there you can fill in the
  setup files as forms, run a search, read every report not yet archived,
  draft applications, edit their documents, record outcomes and file a RAV
  month. Every button runs the command the docs give for it, and a tick on
  "Show commands" shows that command or file under each stage, so the
  terminal and your editor keep working beside the page. See docs/UI.md.
  - **Forms keep your comments.** Saving a config form changes only the
    lines whose values changed; comments and keys the form doesn't show
    stay as you wrote them.
  - **Nothing is paid for on one click.** A search, a draft, a re-draft and
    the interview sheet each ask first and say roughly what they cost.
  - **Postings the pipeline can't read** open straight away with a box to
    paste the job description into, instead of a placeholder file to edit.
  - **The API key form** stores the key in a `.env` only you can read, in a
    field browsers and password managers don't offer to save.
- **The setup check warns about search settings that raise the cost of a
  run**: a skill floor below 50, a strong-match bar below 70, or more than
  five matches per run, and more strongly when they are combined. Each match
  a run shows is written up (about 3 cents), and an eligible posting that
  misses the per-run cap is scored again on every later run. It also warns
  about a per-run count outside 1–5 strong or 0–10 okay, including
  `max_best: 0`. These are warnings only: a run with such settings works as
  before. The browser interface's settings form warns as you type and keeps
  the counts within those ranges.

## 1.23.1 — Closed postings behind ATS redirects (2026-11-10)

- **Closed postings found by web search are dropped again.** Greenhouse and
  Workable answer a closed posting by redirecting to the company's job list
  (`?error=true`, `?not_found=true`), and that page loads fine, so the link
  check counted it as open. On 2026-11-10 six of nine web-search finds were
  closed this way and all reached the location filter; one was sent to
  scoring and failed. Such a redirect now counts as closed. Nothing to edit.

## 1.23.0 — Complete write-ups, Lever and join descriptions (2026-11-10)

- **Write-ups no longer stop mid-sentence.** About one in twelve ended
  where it was about to quote the posting ("…that is exactly the"), since
  July. The write-up came back as a JSON string, and a plain quotation mark
  from the model ended that string early, so the cut went unnoticed and the
  report's cut-off note never appeared. It is now plain text.
- **Lever postings are scored on the whole posting.** The connector read
  only Lever's `description` field, which on many boards is just the company
  introduction; the responsibilities and requirements sit in its `lists`.
  Every Lever posting was scored without them, and a few with nothing to
  check were dropped (ANYbotics, Wingtra and Gravis Robotics on 2026-11-09).
  Lever postings already settled keep their old score.
- **join.com boards are read again, fully this time.** join.com no longer
  answers its per-job API, so 1.21.0's fix found Optiml's postings and then
  failed on their descriptions. They are now read from each job's page.
  Nothing to edit.

## 1.22.0 — Oracle connector, ELCA found again (2026-11-09)

- **A connector for Oracle Recruiting Cloud** (`ats: oracle`, slug
  `host:site`), the careers sites at `*.oraclecloud.com/hcmUI/
  CandidateExperience/...`. It keeps postings with any site in the
  market's country and fetches descriptions only for new ones.
  `config/boards/README.md` shows how to read the slug off a careers URL.
- **ELCA is found again.** Its Recruitee board answers 404; it moved to
  Oracle Recruiting Cloud. If you scan ELCA by name, the pull fixes it;
  nothing to edit. Its postings have new addresses, so your next run
  treats ELCA's open roles (about 55) as new, once.

## 1.21.0 — Board health check (2026-11-09)

- **A new board health check: `python -m jobradar.board_health`.** It reads
  each board you selected once (no detail pages, no model calls) and reports
  it as broken (gone or misconfigured), unreachable (timed out or failed),
  off-market (postings, but none in the country), empty or healthy. A broken
  board is reported straight away, with a guess at where the company moved;
  the others only once the previous check agrees. It replaces the monthly check of `reports/company_health.md`,
  which read the daily runs' counts: that one recorded a broken board as
  zero postings, so it took four weeks to notice, and couldn't tell a board
  that stopped posting in the country from a live one. The monthly workflow
  runs the new check and the discovery email includes its findings
  (`reports/board_health.md`). `JOBRADAR_HEALTH_STALE_DAYS` is no longer
  read. Nothing to edit.
- **For whoever maintains `config/boards/`:** `--catalog ch` also checks
  every board in a market's shared list, and the repository variable
  `JOBRADAR_CATALOG_MARKETS` makes the monthly workflow do so.
  `python -m jobradar.discovery.discover --market ch` looks for companies in
  another market than the one `config/search.yaml` sets.
- **join.com boards are read again.** join.com moved the company data in its
  careers pages, so every join board read as "no company data found" and
  discovery could no longer find any join company. The first health check
  caught it on Optiml. Nothing to edit.
- **Jobgether is no longer in the shared list.** Its Lever board has grown
  past 200,000 postings and arrives as one response at about 10 KB a second,
  so a run that scans it never finishes. If you selected it, the doctor now
  reports "Jobgether: not in config/boards/…": delete that line from your
  `companies.yaml`.
- **Lausanne is resolved to Vaud without a model call**, like Zurich or
  Geneva. The first catalogue-wide health check found two Lausanne boards
  read as posting outside Switzerland without it.

## 1.20.0 — Board catalogue split by market (2026-11-09)

- **The board catalogue is now a folder, `config/boards/`.** `ch.yaml` holds
  the Swiss boards and `global.yaml` the five employers whose one board
  serves every country (Google, NVIDIA, Anthropic, Databricks, OpenAI); a
  search reads both. The format guide that used to head `boards.yaml` is now
  `config/boards/README.md`. Your `companies.yaml` names boards as before, so
  there is nothing to edit: the pull moves the file. The doctor and the
  run's warnings now name the file a board comes from. (If you edited
  `config/boards.yaml` yourself, which it asked you not to, the pull reports
  a conflict on it: put your entries in `companies.yaml` with their `ats` and
  `slug` and delete the file.)

## 1.19.0 — Swiss specifics in one market layer (2026-11-09)

- **Everything Swiss now sits in one place.** The cantons, the country's
  names, the prompt wording that mentions Switzerland, the Swiss German and
  Swiss French writing rules, and the country filter of the global boards
  (Workday, SuccessFactors, iCIMS, onlyfy, Google, Eightfold) moved from
  across the code into `src/jobradar/markets/`, ahead of support for more
  countries. A new `market:` key in `config/search.yaml` picks the country;
  `ch` is the default and so far the only value, so there is nothing to edit
  and nothing behaves differently.

## 1.18.0 — EthonAI on Ashby and four new boards (2026-11-03)

- **EthonAI is found again.** Its board moved from Recruitee to Ashby, under
  the slug `ethon`. The old Recruitee board still answers but lists nothing,
  so `company_health.md` reported it as dry. If you scan EthonAI by name,
  the pull fixes it; nothing to edit.
- **Proton, Netcetera, Flyability and OpenAI can be scanned.** All four have
  Swiss offices: Proton in Geneva and Zurich (Greenhouse), Netcetera in
  Zurich and Winterthur (Workable), Flyability in Paudex VD (Workable) and
  OpenAI in Zurich (Ashby; a global board of about 800 postings, of which
  the location gate keeps the Swiss few). Add any of them to your
  `config/companies.yaml` to scan it. Your first run after adding all four
  scores up to about 16 postings. The catalogue now lists 85 boards.

## 1.17.0 — Snyk board and lower daily cost (2026-10-30)

- **Snyk can be scanned.** The developer-security company has a Zurich office,
  and its board is on Workday, `snyk:wd103:External`, scoped to its Swiss
  postings. Add `- Snyk` to your `config/companies.yaml` to scan it. Your
  first run after adding it scores up to about 17 postings.
- **The cost figures now match October 2026.** A daily run on the author's
  setup of about 80 boards costs about $0.20, down from the $0.90 that
  README.md still showed and the $0.35 estimated in docs/COSTS.md. The
  breakdown there now uses the average day from the run log, about 40
  postings scored rather than 80. A daily run with no boards selected is
  $0.05–0.10, a typical month $8–12, and the first run over the whole
  catalogue $4–10, which is what docs/COSTS.md already said. Nothing changes
  in what the tool does or what you pay.

## 1.16.0 — Filter drop reasons in the run log (2026-10-13)

- **The run log now says why each new posting was dropped before scoring.**
  `reports/runs.jsonl` has a new `filtered_out` list, with one entry for
  every new posting that never reached scoring. Each entry gives the stage it
  fell out at (no named employer, the location prefilter, or the hard
  filters), the checks it failed, and the values that failed them. It also
  says which of those values the model guessed rather than read from the
  posting, for example an "on-site role" taken to mean five office days.
  Until now the log kept only how many postings each stage dropped, so the
  only way to find out why one went missing was to run it again. The
  record's `schema_version` is now 6, and each dropped posting adds a few
  hundred bytes to the day's line.
- **The run's console output names each posting the hard filters drop**,
  with the check it failed and the value behind it.

## 1.15.0 — Allianz and RetinAI boards (2026-10-13)

- **Allianz Suisse can be scanned.** Its board is the group's SAP
  SuccessFactors site, `successfactors` / `internal-careers.allianz.com`.
  Despite the name it is public, it lists the same Swiss postings as
  careers.allianz.com, and you apply through the normal external form. On
  2026-10-13 it listed 33 Swiss roles, mostly in Wallisellen, Bern, Basel,
  Lausanne and Geneva. These include every Allianz company in Switzerland,
  not only Allianz Suisse: Allianz Trade, Medi24 and CAP Rechtsschutz roles
  are reported under "Allianz Suisse" too. Add `- Allianz Suisse` to your
  `config/companies.yaml` to scan it. Your first run after adding it scores
  up to 33 postings.
- **SuccessFactors boards that show jobs as cards can be read.** Until now
  the connector only read the table layout that EY, Swiss Re, Zurich and
  Sonova use, and it found nothing on Allianz's card layout. Both layouts
  now work, with the same Switzerland filter and the same stop at the
  board's own job count.
- **RetinAI is scanned again, on Workable.** RetinAI Medical, now Ikerian AG,
  moved off Personio, and its Personio board now returns 404, so it found
  nothing. It is now `workable` / `retinai`. If your `config/companies.yaml`
  lists it as `- Retinai`, pulling is enough. If your entry gives its own
  `ats: personio`, change it to `workable`; `python -m jobradar.doctor` points
  that entry out. Expect its current openings in your next report, since
  their Workable links are new to jobradar.
- **The catalogue now holds 80 Swiss boards.**

## 1.14.0 — Haiku 5.5 throughout (2026-10-12)

- **Filling in a posting's missing details runs on Haiku 5.5, so jobradar no
  longer uses Haiku 4.5 anywhere.** This is the step that works out a
  posting's workload, office days and canton when the text rules can't. On 80
  real postings it made the same keep-or-drop decisions as before, at about a
  sixth of the cost. The author's daily run is now estimated at about $0.35.

- **A workload the posting doesn't state stays unknown.** Haiku 4.5 often put
  "100%" on postings that state no workload at all, and the workload filter
  then judged them on a number the posting never gave. If your range excludes
  full-time, for example 60–80%, that wrongly dropped postings: 18 extra out of
  80 in testing. An unknown workload passes the filter, as it was always meant
  to.

## 1.13.0 — Haiku 5.5 job descriptions (2026-10-12)

- **The apply pipeline reads job descriptions with Haiku 5.5.** Turning a
  posting page into the job description your CV is tailored against now costs
  about a fifth as much and finishes sooner (about 8 seconds instead of 12 in
  testing). It also copies more of the posting: on the author's test pages it
  kept about 85% of the posting's text instead of 70%, including requirements
  Haiku 4.5 had dropped, and it found a UBS posting behind a long cookie notice
  that Haiku 4.5 gave up on, which would have meant pasting it yourself. The
  progress bar now waits about 3 seconds before the description starts
  appearing, while the model thinks.

- **What you need to do: check your `.env`.** If it sets
  `JOBRADAR_APPLY_HELPER_MODEL=claude-haiku-4-5`, job descriptions stay on Haiku
  4.5 after you pull. Delete the line to follow the new default.

- **`.env.example` no longer says the helper model does fresh scoring.** A
  posting the daily run never saw is scored with `JOBRADAR_SCORING_MODEL`,
  which has been Haiku 5.5 since 1.11.0.

## 1.12.0 — Haiku 5.5 web search and discovery (2026-10-12)

- **Web search and company discovery run on Haiku 5.5 as well.** Web search
  costs about $0.06 a run instead of $0.09, almost all of it the search fee,
  and returns many more direct links to real postings. Discovery found about
  twice as many companies with a live job board, at under a cent a run. With
  scoring already moved in 1.11.0, the author's daily run is now estimated at
  about $0.40. [docs/COSTS.md](docs/COSTS.md) has the figures.

- **Discovery may now answer without searching.** In every test, Haiku 5.5
  named companies from its own knowledge rather than running its searches,
  and its names were more relevant than Haiku 4.5's searched ones. Every name
  is still checked against the job boards before it is suggested, so a wrong
  name costs nothing. A company newer than the model's knowledge can be
  missed; add it to `config/seed_companies.txt`.

- **Web search leaves out postings it can see are elsewhere.** It used to keep
  a posting in New York or Berlin as "borderline" for the location filter to
  drop later. It now leaves out postings placed outside your area, and keeps
  those that list your area among several places, are remote with your area
  allowed, or state no location. In testing this halved the out-of-area leads.
  It did not raise the number of in-area ones: the search results for most
  profiles hold few Swiss postings, so expect web search to add a lead only
  occasionally.

- **What you need to do: check your `.env`.** If it sets
  `JOBRADAR_WEB_SEARCH_MODEL` or `JOBRADAR_DISCOVERY_MODEL` to
  `claude-haiku-4-5`, those stages stay on Haiku 4.5 after you pull. Delete
  the lines to follow the new defaults.

- **Dynamic filtering stays off.** Haiku 5.5 accepts the web search tool
  version that filters results with code, but in testing it used it in one run
  of three, with no difference in what it found. The basic version stays the
  default for Haiku 5.5.

## 1.11.0 — Haiku 5.5 scoring (2026-10-12)

- **Scoring runs on Haiku 5.5, at about a quarter of the cost.** A scoring
  call costs about $0.0023 instead of $0.0081, so the author's daily run
  drops from about $0.90 to about $0.55. The new model also thinks before it
  answers and follows the scoring rules more closely: across 42 real postings
  scored five times each, a posting's skill score varied less from one
  scoring to the next. [docs/COSTS.md](docs/COSTS.md) has the new figures.

- **What you need to do: check `JOBRADAR_SCORING_MODEL` in your `.env`.**
  `.env.example` used to set it to `claude-haiku-4-5`, uncommented. If you
  copied that line, it keeps scoring on Haiku 4.5 after you pull. Delete the
  line to follow the new default. Your `.env` is never changed by a pull.

- **A requirement with a softened detail is no longer treated as optional.**
  "10 years in industrial automation, *ideally* within manufacturing" used to
  count as a nice-to-have because the line contained "ideally". So did
  "Fluent in English; German is *a plus*", and "MD required; MD/PhD
  *preferred*". A line now counts as optional only when the softener covers
  the whole requirement, as in "Experience with R is a plus". About a hundred
  real requirements had been wrongly softened this way, raising scores.

- **"Don't worry if you don't meet every criterion" no longer makes core
  requirements optional.** That kind of wording still softens the rest of the
  list, but a required number of years, an industry, or a technology stays
  required. A consulting firm saying "the ideal candidate meets several of the
  following" has not made its years of consulting optional.

- **Personal qualities and the role's duties no longer count.** Lines like
  "Excellent communication skills" or "Hohe Eigeninitiative" were nearly
  always judged met, and so raised every score they appeared in. Duties such
  as "Translate the portfolio strategy into a tactical plan" describe the job,
  not you. Both are still listed in `reports/runs.jsonl`, under the new
  categories `personal_quality` and `responsibility`, but carry no weight.

- **Managing people only caps the score when the posting asks for it as a
  qualification.** "Demonstrated experience managing 1–3 direct reports"
  still caps it at the floor. "Lead and develop the transformation team" in
  the role's duties no longer does: an employer can hire someone to manage
  people for the first time. This reverses part of 1.9.0. Leading
  cross-functional teams or projects without direct reports now counts as not
  meeting a people-management requirement, rather than as partly meeting it.

- **Clearer rules for whether you meet a requirement.** "N years of X" counts
  only the years spent doing X, not your whole career, and where a posting
  offers alternatives ("or business analyst roles"), the closest one is used.
  The model now names the evidence from your profile before it decides.

- **Expect your scores to move again, mostly down.** Rescoring the author's 41
  applied postings against 1.10.0: those scoring 90 or more fell from 16 to 9,
  the typical score from 81 to 77, and those landing below the default floor
  of 60 rose from 1 to 7. A posting that lands just under the floor is still
  scored again on later runs before it is dropped. Look at a week of reports
  before retuning `min_skill` or `best_threshold`.

- **`reports/runs.jsonl` gains two category values,** `personal_quality` and
  `responsibility`. The schema version stays at 5.

## 1.10.0 — Eightfold boards (2026-10-09)

- **On is scanned again, on Eightfold.** On moved its careers site
  (jobs.on.com) off Greenhouse. The old `onrunning` board still answers with
  about 220 postings, so it looked healthy, but nothing new has appeared on it
  since 2026-09-25. On 2026-10-09 it shared one title with the 41 roles on
  jobs.on.com. Every On role posted in the last two weeks was missed, among
  them 16 in Zurich. On is now `eightfold` / `jobs.on.com:on.com`. If you scan
  On, expect up to 16 On postings in your first report after pulling, because
  none of them has been scored yet.
- **Eightfold boards can be scanned.** A new `eightfold` board type reads
  careers sites run on Eightfold. The slug is `host:domain`, the site's host
  and the company domain its API is keyed by; the header of
  `config/boards.yaml` explains how to read both off the careers page. Only
  Swiss postings are kept. Each new posting costs one extra request for its
  description, and postings already seen skip it. Discovery does not probe
  Eightfold, since its slugs can't be guessed.
- **On's board count drops from about 225 to about 16** in
  `reports/runs.jsonl` and the monthly board-health check. Only Swiss postings
  are counted now, as for Workday and Google boards. The count has not gone
  down because On is posting less.

## 1.9.0 — Track records count (2026-10-08)

- **A track record you only partly have now lowers the skill score.**
  Requirements like "proven track record of leading large-scale transformation
  programs" or "experience working with senior executives" had no category of
  their own. They were filed with location and travel, which never count, so a
  posting could score 100 on its degree and years lines while you only partly
  met the rest. They now have their own category, `experience`, which counts
  like industry, technology and years.

- **One clear gap caps the score at 70.** If you don't meet a required number
  of years, an industry, or a technology, the skill score stops at 70, however
  many other lines you meet. Before, the lines you met could average away the
  one gap that decides the screen: an engagement-manager role demanding 5–8
  years of strategy consulting scored 80 for a candidate with none. The posting
  still appears, but it can only take the top slot with an interest score of
  90 or more. Partly met, preferred, and "…is a plus" requirements don't trigger
  the cap, and neither do degrees or `experience` lines. The report says which
  requirement capped the score.

- **More people-management demands are recognised.** "Demonstrated people
  leadership", "lead, mentor, and develop a team of managers" and "lead and
  develop the transformation team" now cap the score at the floor when you have
  not managed people, as the line-management check already did for other
  wording. Leading projects, programmes and cross-functional teams still does
  not count as managing people.

- **Expect your scores to move.** Postings that scored near 100 on short
  checklists drop, and postings with one clear gap stop at 70. Across the
  author's applied postings, the typical score went up rather than down, so
  this is not simply a stricter scorer. Look at a week of reports before
  retuning `min_skill` or `best_threshold` in `config/search.yaml`.

- **`reports/runs.jsonl` gains a category value.** Requirements can now be
  tagged `experience`. The schema version stays at 5, and existing lines are
  unchanged.

## 1.8.0 — Shared board list (2026-10-05)

- **Board details now come from one shared list, `config/boards.yaml`.**
  It holds every board jobradar knows (79 Swiss boards), with each one's ATS,
  slug and notes, and `git pull` keeps it up to date. Until now each user's
  `config/companies.yaml` carried its own copy of those details, which a pull
  never updates. So when a company moved to another ATS, its entry quietly
  found nothing until you edited it by hand, as 1.6.0 and 1.7.1 had to ask
  you to.
- **`config/companies.yaml` now names the boards to scan**, one per line:

  ```yaml
  companies:
    - Frontify
    - Verity
  ```

  For a board the shared list lacks, give the entry its own `ats` and `slug`,
  as before. The same form for a board the shared list has scans yours
  instead, and `python -m jobradar.doctor` warns while the two differ.
- **Your existing `companies.yaml` keeps working.** Its full entries count
  as your own boards. To follow the shared list, replace each entry with its
  name; `doctor` names any entry whose ATS or slug differs from the shared
  list, and those are the stale ones.
- **`doctor` checks the shared list too**, and fails on a name it can't
  find there, typically a typo.
- **Discovery skips companies the shared list already has**, and the
  board-health report says which file each flagged board comes from:
  `config/boards.yaml` or your `companies.yaml`.

## 1.7.1 — Board health after 28 days (2026-10-05)

- **The board-health check flags a dry board after 28 days, down from 45.**
  A board that has moved or closed costs every posting it would have found,
  so it should show up within one monthly discovery run rather than two. A
  board that is only quiet costs a glance at the report. The
  `JOBRADAR_HEALTH_STALE_DAYS` environment variable still overrides it.
- **Frontify moved from Lever to Ashby: `ashby` / `frontify`.** Its Lever
  board still answers but lists nothing.
- **If you switched on Frontify, edit your own copy.** Pulling this does not
  change your `config/companies.yaml`, and the old entry quietly finds
  nothing. Change its `ats` to `ashby`; the slug stays `frontify`.

## 1.7.0 — Deep links from web search (2026-10-05)

- **Web-search leads must link to the posting itself.** Every match in the
  2026-10-02 report linked to a careers landing page (`liip.ch/jobs`,
  `jua.ai/careers`, `frontify.com/careers`, `pricehubble.com/careers`), and
  none of the four roles existed: the search model had read that a company
  hires, named a plausible role, and attached the careers page. The page
  answers, so the link check called it live. Three changes close this:
  - A lead whose link is a landing or listing page is dropped before scoring.
    This covers a bare domain, a path made only of careers words in English,
    German, French or Italian (`/careers`, `/de/karriere/offene-stellen`), and
    a hosted ATS board with no posting under it (`jobs.lever.co/acme`). A
    query key that names one posting (`/careers?gh_jid=123`) still counts as a
    posting link. This replaces the old drop of bare domains only.
  - A lead whose link the search never returned is dropped. The model sees
    search results, not the open web, so a link that isn't among them was
    remembered or made up. Matching ignores `www.`, trailing slashes and
    tracking parameters.
  - The search prompt now asks for the posting page, copied from a result,
    with the posting's own title, and says an empty answer is acceptable. It
    also suggests searches naming ATS hosts, which find posting pages more
    often.
- **More job boards are treated as aggregators**: builtin.com, dice.com,
  eFinancialCareers, f6s, freehire.me, frontaliereticino.ch, JobLeads,
  jobmaps.ch, jobsinforex, MeetFrank, Remote Rocketship, WeAreDevelopers,
  weloveproduct.co, and more Glassdoor domains. Each had shown up in web-search
  output, mostly as a search-results page rather than a posting.
- **`reports/runs.jsonl`**: the web-search funnel's `pathless_dropped` is now
  `listing_dropped`, and it gains `ungrounded_dropped` and
  `grounding_checked`. The last one is false when a response carried no
  result links to check against; the check is then skipped with a warning
  rather than dropping every lead.
- **Expect fewer web-search leads.** On 2026-10-02, at least 10 of the 13
  leads would have been dropped as landing pages. Fewer leads here means
  fewer made-up roles in the report, not fewer real ones.

## 1.6.0 — BreezyHR boards (2026-10-01)

- **BreezyHR boards can be scanned.** A new `breezy` board type reads
  career pages hosted on BreezyHR (`<slug>.breezy.hr`, where the slug is the
  page's subdomain). Each new posting costs one extra page load for its
  description; postings already seen skip it, as on the other boards that
  work this way. The monthly discovery run now checks BreezyHR too.
- **Verity is back, on BreezyHR.** The Zurich drone company left Lever, so
  its old entry found nothing. It is now `breezy` / `verity-ag`, with three
  Zurich roles open when this was written.
- **Three more changes to the board list.** The monthly board-health check
  (`reports/company_health.md`) flagged boards that had been empty for 72
  days:
  - OnlineDoctor moved from Personio to Teamtailor: `teamtailor` /
    `onlinedoctorag`.
  - Ubique is removed. It now lists jobs on its own website and takes
    applications by email, which jobradar can't read.
  - Ledgy (equity-management software, Zurich) is added. When this was
    written, Ledgy's open roles were all in London or remote elsewhere in
    Europe, and OnlineDoctor's were all in Hamburg, so neither adds anything
    until a Swiss role opens.
- **If you switched on Verity, OnlineDoctor or Ubique, edit your own copy.**
  Pulling this release does not change your `config/companies.yaml`. The old
  entries don't fail; they quietly find nothing. Change the `ats` and `slug`
  of the two moved boards as above, and delete Ubique. To see the new
  entries, run `git show upstream/master:config/companies.yaml`, then check
  your edit with `python -m jobradar.doctor`.

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
