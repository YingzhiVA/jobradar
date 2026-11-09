# The shared board catalogue

Where each company posts its jobs. Nothing here is scanned by itself:
`config/companies.yaml` picks the boards to scan, by name.

One file per market, plus one for every market:

| File | Holds |
| --- | --- |
| `global.yaml` | Employers whose one board serves every market: a global board the connector scopes to the market's country (Google's feed, a Workday board), or one the location filter trims (Greenhouse, Ashby). |
| `ch.yaml` | Switzerland: Swiss employers, and global ones whose board or slug is Swiss-specific (a Swiss careers site, a slug scoped to Swiss sites). |

A search reads `global.yaml` together with the file of the market set in
`config/search.yaml` (`market:`, default `ch`). A company appears in only one
file; the doctor reports a name listed twice. Every market sees
`global.yaml`, so a company there is never repeated in a market's file, and a
board in a market's file that turns out to serve other markets unchanged
moves to `global.yaml`.

These files are maintained upstream and updated by `git pull`, like the code,
so a board that moves to another ATS is fixed here once, for everyone. To scan
a board the list lacks, don't add it here: give it with its ats and slug in
your `config/companies.yaml` (that file shows how). A board that is wrong or
missing for everyone is fixed by a pull request against these files.

What is here is one person's watchlist, kept because each board's quirks are
written down next to it. Each market's file is grouped by industry, with a
section index at the top; a company that could sit in two sections appears
once, so search for its name if it's not where you expect. There is
deliberately no keyword list: what to surface is decided by scoring against
your profile, not by job titles.

## Entry format

Each entry has three fields:

```
name: how the company appears in reports
ats:  greenhouse | lever | ashby | smartrecruiters | personio | recruitee |
      workable | teamtailor | workday | join | bamboohr | avature |
      successfactors | icims | brassring | prospective | google | onlyfy |
      breezy | eightfold
slug: the board token used in that ATS's public job-board URL, e.g.
  greenhouse: https://boards-api.greenhouse.io/v1/boards/<slug>/jobs
  lever:      https://api.lever.co/v0/postings/<slug>
  ashby:      https://api.ashbyhq.com/posting-api/job-board/<slug>
  bamboohr:   https://<slug>.bamboohr.com/careers/list  (slug = careers subdomain)
  breezy:     https://<slug>.breezy.hr/json  (slug = careers subdomain)
```

You can usually find the slug in the company's own "Careers" page URL, or by
viewing the page source for a link to boards.greenhouse.io / jobs.lever.co /
jobs.ashbyhq.com. `python -m jobradar.discovery.discover` guesses slugs for
the simple ATSs; the ones below need a manual read of the careers URL.

Workday needs THREE coordinates, so its slug is `tenant:host:site` (e.g.
`novartis:wd3:Novartis_Careers`), read from the company's
`*.myworkdayjobs.com` careers URL: tenant + datacenter host (wd1/wd3/wd5/
wd103/wd502) + site id. The site id has no naming convention, so open the
careers page and read it off the URL. The connector pre-filters to the
market's country server-side (Workday boards are global and large). An
optional 4th slug segment pipe-separates location-facet descriptors to scope
to instead (`roche:wd3:roche-ext:Basel|Kaiseraugst`), needed for a board
whose location facet has no country in it. It OVERRIDES the country
pre-filter, so the names you list are the only places fetched, and an entry
with one belongs in that market's file. A big enterprise fronting Workday
with a custom portal (Roche and Johnson & Johnson use Phenom) usually still
has the Workday board open underneath: read the tenant off the "Apply" link
in a job page's HTML. Companies with no Workday board at all are reached by
web_search, unless another connector covers them.

avature and successfactors are hand-configured like Workday (their slugs
aren't guessable single tokens). Where a connector filters to "the market's
country", it uses the country named in `src/jobradar/markets/` (Switzerland
for `ch`).

```
avature:        slug is "host:portalPath:searchPathSegment", read from the
                company's careers URL, e.g.
                "jobs.siemens.com:en_US/externaljobs:Switzerland||". The
                third part scopes the listing server-side (Avature's
                term/facet path segment; trailing "||" included), so an entry
                with one belongs in that market's file. Leave it empty for a
                board that's already single-country.
successfactors: slug is the search page's "host[/site]" prefix, e.g.
                "careers.ey.com/ey" or "careers.swissre.com" (job search
                lives at https://<slug>/search/). The connector searches for
                the market's country (locationsearch=Switzerland).
icims:          slug is the Jibe careers host, e.g. "careers.axa.com" (the
                JSON listing lives at https://<slug>/api/jobs, descriptions
                included, so no detail calls). The connector applies the
                market's country facet (country=Switzerland).
brassring:      slug is "host:partnerid:siteid[:Option1|Option2]", read off
                the Talent Gateway URL (jobs.ubs.com/TGnewUI/Search/home/
                HomeWithPreLoad?partnerid=25008&siteid=5012). The optional
                4th segment names facet options (a City, a division) to
                scope the board to server-side; without it a board over 100
                postings is refused, since every posting costs a ~0.4 MB
                detail page. One tenant runs several site ids (UBS:
                professionals / students / apprentices, each in four UI
                languages that share the same reqs); pick the one the
                careers page links to for the audience you want.
prospective:    slug is the Prospective career-center id, optionally with a
                UI language ("1000982" or "1000982:de"). Read it off the
                employer's careers page source: it calls
                ohws.prospective.ch/public/v1/careercenter/<id>/ itself for
                its per-country job counts. Job pages live on the
                employer's own jobs.* domain and carry JSON-LD.
google:         reads the job feed Google publishes for aggregators (one
                ~20 MB download, descriptions included, kept to the market's
                country in code). The slug names the feed's employers to
                keep: "Google|YouTube|DeepMind" for all three.
onlyfy:         slug is the career page's subdomain: "hexagon-robotics"
                for hexagon-robotics.onlyfy.jobs (onlyfy was called
                prescreen; a careers page embedding its job widget loads
                <slug>.onlyfy.jobs/widget/iframe.js). The connector filters
                to the market's country code (country=ch) and pauses a
                second between requests, as the site's robots.txt asks, so a
                board of 25 postings takes about 30 seconds.
eightfold:      slug is "host:domain": the careers site's host and the
                company domain its API calls are keyed by, e.g.
                "jobs.on.com:on.com". Read both off the careers page's own
                request to https://<host>/api/pcsx/search?domain=<domain>
                (the browser's network tab shows it). The connector keeps
                the market's postings in code and makes one detail request
                per new one for its description.
```

join is the one to be careful with: join.com serves a company PROFILE page
for firms that were never its customers (Swisscom has one, with no ads,
while its real board is Workday), and discovery only accepts a join board
that has at least one live ad. So: only add a join entry when the company
genuinely has ads there.
