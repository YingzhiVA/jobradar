import json
from datetime import date

import httpx

import jobradar.discovery.discover as discover_mod
from jobradar.discovery.discover import (
    CompanyMatch,
    HealthFinding,
    _slug_candidates,
    diagnose_stale,
    discover,
    load_seed_companies,
    probe_company,
    render_health_report,
    render_suggestions_yaml,
    run_health_check,
    search_company_names,
)
from jobradar.search.company_health import StaleFinding


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | list | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data

    def raise_for_status(self):
        # probe_company branches on status_code itself, but the join probe goes
        # through join_jobs_page, which uses httpx's raise_for_status idiom.
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


class _FakeClient:
    """Duck-typed stand-in for httpx.Client: maps exact URLs to canned
    responses, 404s anything else. Keeps probe_company's tests hermetic
    instead of depending on live ATS APIs staying up/unchanged.
    """

    def __init__(self, responses: dict[str, _FakeResponse]):
        self._responses = responses

    def get(self, url: str, params: dict | None = None) -> _FakeResponse:
        # join's probe passes params (page/pageSize); key those off the bare URL.
        return self._responses.get(url, _FakeResponse(404))


def _join_page(company: dict) -> _FakeResponse:
    """A join.com company page, whose data lives in an embedded Next.js blob."""
    payload = {"props": {"pageProps": {"initialState": {"company": company}}}}
    return _FakeResponse(
        200,
        text='<html><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}</script></html>",
    )


def test_slug_candidates_strips_legal_suffixes_and_dedupes():
    # Real ATS slugs drop legal-entity suffixes entirely (companies use
    # "stripe", not "stripeinc"), so the suffix-stripped core name should be
    # the first (highest-priority) candidate.
    candidates = _slug_candidates("Example Corp Inc.")
    assert candidates[0] == "example"
    assert len(candidates) == len(set(candidates))  # no duplicates


def test_slug_candidates_handles_simple_name():
    assert _slug_candidates("Acme") == ["acme"]


def test_probe_company_finds_greenhouse_match_and_reports_verified_name():
    fake_client = _FakeClient(
        {
            "https://boards-api.greenhouse.io/v1/boards/stripe/jobs": _FakeResponse(
                200, {"jobs": [{"company_name": "Stripe"}]}
            ),
        }
    )
    match = probe_company("Stripe", fake_client)
    assert match == CompanyMatch(
        name="Stripe", ats="greenhouse", slug="stripe", verified_company_name="Stripe"
    )


def test_probe_company_finds_lever_match_with_no_verified_name():
    # Lever doesn't expose a company-name field, so verified_company_name stays None
    # even on a real match.
    fake_client = _FakeClient(
        {"https://api.lever.co/v0/postings/acme?mode=json": _FakeResponse(200, [{"text": "PM"}])}
    )
    match = probe_company("Acme", fake_client)
    assert match == CompanyMatch(name="Acme", ats="lever", slug="acme", verified_company_name=None)


def test_probe_company_rejects_empty_lever_board():
    # An empty board proves nothing and must not match — otherwise it shadows a
    # live board on a later ATS (the generalized join-shadowing bug).
    fake_client = _FakeClient(
        {"https://api.lever.co/v0/postings/acme?mode=json": _FakeResponse(200, [])}
    )
    assert probe_company("Acme", fake_client) is None


def test_probe_company_empty_board_does_not_shadow_a_live_one():
    # lever is probed before smartrecruiters; an empty lever board must not win
    # over a live smartrecruiters board.
    fake_client = _FakeClient(
        {
            "https://api.lever.co/v0/postings/acme?mode=json": _FakeResponse(200, []),
            "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=1": _FakeResponse(
                200, {"totalFound": 2, "content": [{"company": {"name": "Acme AG"}}]}
            ),
        }
    )
    match = probe_company("Acme", fake_client)
    assert match == CompanyMatch(
        name="Acme", ats="smartrecruiters", slug="acme", verified_company_name="Acme AG"
    )


def test_probe_company_skip_passes_over_the_configured_board():
    # Re-probe use: skip a company's own (empty) configured board to find where
    # it moved.
    fake_client = _FakeClient(
        {
            "https://api.lever.co/v0/postings/acme?mode=json": _FakeResponse(200, [{"text": "PM"}]),
            "https://apply.workable.com/api/v1/widget/accounts/acme?details=true": _FakeResponse(
                200, {"name": "Acme AG", "jobs": [{"title": "PM"}]}
            ),
        }
    )
    # Without skip, lever (earlier, and now non-empty) wins.
    assert probe_company("Acme", fake_client).ats == "lever"
    # Skipping lever/acme finds the workable board instead.
    match = probe_company("Acme", fake_client, skip=("lever", "acme"))
    assert match == CompanyMatch(
        name="Acme", ats="workable", slug="acme", verified_company_name="Acme AG"
    )


def test_probe_company_skip_accepts_a_set_of_rejected_boards():
    # The permanent slug-collision fix: a live collision board (real ads, same
    # slug every run) can only be stopped by an explicit rejection. probe_company
    # takes a set of (ats, slug) pairs to skip; the collision then never matches.
    fake_client = _FakeClient(
        {
            "https://api.ashbyhq.com/posting-api/job-board/hamilton": _FakeResponse(
                200, {"jobs": [{"title": "Underwriter"}]}  # wrong-company collision, has ads
            ),
        }
    )
    # Without the rejection, the collision board matches.
    assert probe_company("Hamilton", fake_client).ats == "ashby"
    # With it rejected, nothing else exists -> no match.
    assert probe_company("Hamilton", fake_client, skip={("ashby", "hamilton")}) is None


def test_probe_company_rejection_does_not_block_the_real_board_on_another_ats():
    # Rejecting the collision must still let a genuine board on a later ATS win.
    fake_client = _FakeClient(
        {
            "https://api.ashbyhq.com/posting-api/job-board/acme": _FakeResponse(
                200, {"jobs": [{"title": "Wrong Co role"}]}
            ),
            "https://apply.workable.com/api/v1/widget/accounts/acme?details=true": _FakeResponse(
                200, {"name": "Acme AG", "jobs": [{"title": "PM"}]}
            ),
        }
    )
    match = probe_company("Acme", fake_client, skip={("ashby", "acme")})
    assert match == CompanyMatch(
        name="Acme", ats="workable", slug="acme", verified_company_name="Acme AG"
    )


def test_probe_company_returns_none_when_no_slug_matches():
    fake_client = _FakeClient({})  # everything 404s
    assert probe_company("Definitely Not A Real Company Xyz123", fake_client) is None


def test_probe_company_finds_join_match_when_the_company_has_ads():
    fake_client = _FakeClient(
        {
            "https://join.com/companies/acme": _join_page(
                {"id": 42, "name": "Acme AG", "domain": "acme"}
            ),
            "https://join.com/api/public/companies/42/jobs": _FakeResponse(
                200, {"items": [{"id": 1, "title": "PM"}], "pagination": {"pageCount": 1}}
            ),
        }
    )
    match = probe_company("Acme", fake_client)
    assert match == CompanyMatch(
        name="Acme", ats="join", slug="acme", verified_company_name="Acme AG"
    )


def test_probe_company_rejects_join_company_page_with_no_ads():
    # join.com serves a profile page for companies that were never customers, so
    # the page's existence proves nothing. Matching on it alone pinned 26 real
    # companies to a board that never returns a posting.
    fake_client = _FakeClient(
        {
            "https://join.com/companies/acme": _join_page(
                {"id": 42, "name": "Acme AG", "domain": "acme"}
            ),
            "https://join.com/api/public/companies/42/jobs": _FakeResponse(
                200, {"items": [], "pagination": {"rowCount": 0, "pageCount": 0}}
            ),
        }
    )
    assert probe_company("Acme", fake_client) is None


def test_probe_company_adless_join_page_does_not_shadow_a_real_board():
    # The failure that mattered: join is probed BEFORE smartrecruiters and the
    # first hit wins, so an empty join profile used to mask a live board.
    fake_client = _FakeClient(
        {
            "https://join.com/companies/acme": _join_page(
                {"id": 42, "name": "Acme AG", "domain": "acme"}
            ),
            "https://join.com/api/public/companies/42/jobs": _FakeResponse(
                200, {"items": [], "pagination": {"rowCount": 0, "pageCount": 0}}
            ),
            "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=1": _FakeResponse(
                200, {"totalFound": 3, "content": [{"company": {"name": "Acme AG"}}]}
            ),
        }
    )
    match = probe_company("Acme", fake_client)
    assert match == CompanyMatch(
        name="Acme", ats="smartrecruiters", slug="acme", verified_company_name="Acme AG"
    )


def test_probe_company_finds_recruitee_match_and_reports_verified_name():
    fake_client = _FakeClient(
        {
            "https://acme.recruitee.com/api/offers/": _FakeResponse(
                200, {"offers": [{"company_name": "Acme Inc"}]}
            ),
        }
    )
    match = probe_company("Acme", fake_client)
    assert match == CompanyMatch(
        name="Acme", ats="recruitee", slug="acme", verified_company_name="Acme Inc"
    )


def test_probe_company_finds_teamtailor_match_and_reports_verified_name():
    fake_client = _FakeClient(
        {
            "https://acme.teamtailor.com/jobs.json": _FakeResponse(
                200, {"items": [{"_jobposting": {"hiringOrganization": {"name": "Acme Inc"}}}]}
            ),
        }
    )
    match = probe_company("Acme", fake_client)
    assert match == CompanyMatch(
        name="Acme", ats="teamtailor", slug="acme", verified_company_name="Acme Inc"
    )


def test_probe_company_finds_workable_match_and_reports_verified_name():
    fake_client = _FakeClient(
        {
            "https://apply.workable.com/api/v1/widget/accounts/acme?details=true": _FakeResponse(
                200, {"name": "Acme Inc", "jobs": [{"title": "PM"}]}
            ),
        }
    )
    match = probe_company("Acme", fake_client)
    assert match == CompanyMatch(
        name="Acme", ats="workable", slug="acme", verified_company_name="Acme Inc"
    )


def test_probe_company_ignores_workable_200_with_no_jobs():
    # Workable answers 200 for any slug; jobs=[] means not a real/active account.
    fake_client = _FakeClient(
        {
            "https://apply.workable.com/api/v1/widget/accounts/acme?details=true": _FakeResponse(
                200, {"name": "Acme", "jobs": []}
            ),
        }
    )
    assert probe_company("Acme", fake_client) is None


def test_probe_company_finds_bamboohr_match():
    # BambooHR exposes no company_name, so verified_company_name stays None.
    fake_client = _FakeClient(
        {
            "https://acme.bamboohr.com/careers/list": _FakeResponse(
                200, {"meta": {"totalCount": 1}, "result": [{"id": "1"}]}
            ),
        }
    )
    match = probe_company("Acme", fake_client)
    assert match == CompanyMatch(name="Acme", ats="bamboohr", slug="acme", verified_company_name=None)


def test_probe_company_rejects_empty_bamboohr_board():
    # A BambooHR customer with no current openings returns `result: []`; that's
    # a real board but nothing to act on, so it must not match (and must not
    # shadow a live board elsewhere).
    fake_client = _FakeClient(
        {"https://acme.bamboohr.com/careers/list": _FakeResponse(200, {"result": []})}
    )
    assert probe_company("Acme", fake_client) is None


def test_probe_company_ignores_bamboohr_redirect_for_unknown_slug():
    # A non-customer BambooHR subdomain 302-redirects to the marketing site;
    # status != 200 -> not a match.
    fake_client = _FakeClient({"https://bogus.bamboohr.com/careers/list": _FakeResponse(302)})
    assert probe_company("bogus", fake_client) is None


def test_probe_company_finds_personio_match():
    xml = "<workzag-jobs><position><id>1</id></position></workzag-jobs>"
    fake_client = _FakeClient(
        {"https://planted.jobs.personio.de/xml": _FakeResponse(200, text=xml)}
    )
    match = probe_company("Planted", fake_client)
    assert match == CompanyMatch(name="Planted", ats="personio", slug="planted")


def test_probe_company_rejects_empty_personio_board():
    # Valid <workzag-jobs> root but no <position> ads -> not a match.
    fake_client = _FakeClient(
        {"https://planted.jobs.personio.de/xml": _FakeResponse(200, text="<workzag-jobs></workzag-jobs>")}
    )
    assert probe_company("Planted", fake_client) is None


def test_probe_company_ignores_personio_redirect_for_unknown_slug():
    # A non-existent Personio slug 307-redirects; status != 200 -> not a match.
    fake_client = _FakeClient({"https://bogus.jobs.personio.de/xml": _FakeResponse(307)})
    assert probe_company("bogus", fake_client) is None


def test_probe_company_finds_smartrecruiters_match_and_reports_verified_name():
    fake_client = _FakeClient(
        {
            "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=1": _FakeResponse(
                200, {"totalFound": 5, "content": [{"company": {"name": "Acme Inc"}}]}
            ),
        }
    )
    match = probe_company("Acme", fake_client)
    assert match == CompanyMatch(
        name="Acme", ats="smartrecruiters", slug="acme", verified_company_name="Acme Inc"
    )


def test_probe_company_ignores_smartrecruiters_200_with_zero_totalfound():
    # SmartRecruiters answers 200 for any slug; totalFound=0 means it doesn't
    # actually exist and must NOT be treated as a match.
    fake_client = _FakeClient(
        {
            "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=1": _FakeResponse(
                200, {"totalFound": 0, "content": []}
            ),
        }
    )
    assert probe_company("Acme", fake_client) is None


def test_probe_company_skips_ashby_response_missing_jobs_key():
    # A 200 with an unexpected shape shouldn't be treated as a match.
    fake_client = _FakeClient(
        {"https://api.ashbyhq.com/posting-api/job-board/acme": _FakeResponse(200, {"unexpected": True})}
    )
    assert probe_company("Acme", fake_client) is None


# --- web_search company-name discovery ---


class _LLMBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _LLMResponse:
    def __init__(self, blocks):
        self.content = blocks


class _LLMClient:
    """Duck-typed Anthropic client: with_options().messages.create() returns a
    canned response, or raises if `exc` is set.
    """

    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc

    def with_options(self, **kwargs):
        return self

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return _LLMResponse([_LLMBlock(self._text)])


def test_search_company_names_parses_json():
    client = _LLMClient(text='Here you go:\n{"companies": ["Acme", "Beta AG"]}')
    assert search_company_names(client, intent="x") == ["Acme", "Beta AG"]


def test_search_company_names_unparseable_returns_empty():
    assert search_company_names(_LLMClient(text="I found nothing useful."), intent="x") == []


def test_search_company_names_degrades_on_exception():
    assert search_company_names(_LLMClient(exc=RuntimeError("timeout")), intent="x") == []


def test_discover_merges_and_dedupes_both_sources(monkeypatch):
    monkeypatch.setattr(
        discover_mod, "search_company_names", lambda c, intent, exclude=None: ["beta", "Gamma"]
    )
    probed = []

    def fake_probe(name, client, skip=None):
        probed.append(name)
        if name == "Gamma":
            return None  # not on any ATS
        return CompanyMatch(name=name, ats="greenhouse", slug=name.lower())

    monkeypatch.setattr(discover_mod, "probe_company", fake_probe)

    matches = discover(
        [{"name": "Acme"}],
        client=object(),
        use_web_search=True,
        web_intent="",
        seed_names=["Acme", "Beta"],
    )

    # Acme already configured -> dropped; "beta" is a case-dup of "Beta" -> deduped.
    assert probed == ["Beta", "Gamma"]
    assert [m.name for m in matches] == ["Beta"]


def test_load_seed_companies_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / "seed.txt"
    path.write_text("# header\n\nScandit\nWingtra  # ETH spin-off\n\n  \n# trailing\n")
    assert load_seed_companies(path) == ["Scandit", "Wingtra"]


def test_load_seed_companies_missing_file(tmp_path):
    assert load_seed_companies(tmp_path / "nope.txt") == []


def test_discover_probes_seed_names(monkeypatch):
    probed = []

    def fake_probe(name, client, skip=None):
        probed.append(name)
        return CompanyMatch(name=name, ats="lever", slug=name.lower())

    monkeypatch.setattr(discover_mod, "probe_company", fake_probe)

    matches = discover(
        [], client=object(), use_web_search=False, seed_names=["Scandit", "Wingtra"]
    )
    assert probed == ["Scandit", "Wingtra"]
    assert [m.name for m in matches] == ["Scandit", "Wingtra"]


def test_discover_skips_web_search_when_disabled(monkeypatch):
    called = {"search": False}

    def fake_search(c, intent, exclude=None):
        called["search"] = True
        return ["ShouldNotAppear"]

    monkeypatch.setattr(discover_mod, "search_company_names", fake_search)
    monkeypatch.setattr(discover_mod, "probe_company", lambda n, c, skip=None: None)

    discover([], client=object(), use_web_search=False, seed_names=["Acme"])
    assert called["search"] is False


def test_discover_skips_ledger_known_and_records_outcomes(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from jobradar.discovery import discovery_ledger

    now = datetime(2026, 6, 17, tzinfo=timezone.utc)
    monkeypatch.setattr(
        discover_mod,
        "search_company_names",
        lambda c, intent, exclude=None: ["Matched Co", "FreshDrop", "StaleDrop", "NewCo"],
    )

    probed = []

    def fake_probe(name, client, skip=None):
        probed.append(name)
        return CompanyMatch(name=name, ats="lever", slug=name.lower()) if name == "NewCo" else None

    monkeypatch.setattr(discover_mod, "probe_company", fake_probe)

    v = discover_mod._PROBE_VERSION
    ledger = {}
    discovery_ledger.record(ledger, "Matched Co", "matched", now, v)
    discovery_ledger.record(ledger, "FreshDrop", "dropped", now - timedelta(days=5), v)
    discovery_ledger.record(ledger, "StaleDrop", "dropped", now - timedelta(days=40), v)

    matches = discover(
        [], client=object(), use_web_search=True, ledger=ledger, now=now, ttl_days=30
    )

    # Matched + fresh drop are skipped; stale drop is re-probed; NewCo is probed.
    assert probed == ["StaleDrop", "NewCo"]
    assert [m.name for m in matches] == ["NewCo"]
    # Outcomes recorded: NewCo matched, StaleDrop refreshed as dropped at `now`.
    assert ledger["newco"]["outcome"] == "matched"
    assert ledger["staledrop"]["checked_at"] == now.isoformat()


def test_discover_passes_known_set_as_exclusion(monkeypatch):
    captured = {}

    def fake_search(c, intent, exclude=None):
        captured["exclude"] = exclude
        return []

    monkeypatch.setattr(discover_mod, "search_company_names", fake_search)
    monkeypatch.setattr(discover_mod, "probe_company", lambda n, c, skip=None: None)

    from datetime import datetime, timezone

    from jobradar.discovery import discovery_ledger

    now = datetime(2026, 6, 17, tzinfo=timezone.utc)
    ledger = {}
    discovery_ledger.record(ledger, "DroppedCo", "dropped", now, discover_mod._PROBE_VERSION)

    discover([{"name": "ConfiguredCo"}], client=object(), ledger=ledger, now=now)

    assert "ConfiguredCo" in captured["exclude"]
    assert "DroppedCo" in captured["exclude"]


def test_discover_reprobes_drops_from_an_older_ats_set(monkeypatch):
    # A company dropped under an OLDER probe version must be re-probed (and
    # re-suggested) once a new ATS connector is added — not suppressed by TTL.
    from datetime import datetime, timezone

    from jobradar.discovery import discovery_ledger

    now = datetime(2026, 6, 17, tzinfo=timezone.utc)
    monkeypatch.setattr(discover_mod, "search_company_names", lambda c, intent, exclude=None: ["OldDrop"])
    probed = []
    monkeypatch.setattr(discover_mod, "probe_company", lambda n, c, skip=None: probed.append(n) or None)

    ledger = {}
    discovery_ledger.record(ledger, "OldDrop", "dropped", now, "old-ats-set")  # different version

    discover([], client=object(), use_web_search=True, ledger=ledger, now=now)
    assert probed == ["OldDrop"]  # re-probed despite being a fresh drop


def test_render_suggestions_yaml_empty():
    text = render_suggestions_yaml([], date(2026, 6, 16))
    assert "No new companies found" in text


def test_render_suggestions_yaml_with_matches():
    matches = [
        CompanyMatch(name="Stripe", ats="greenhouse", slug="stripe", verified_company_name="Stripe"),
        CompanyMatch(name="Acme Co", ats="lever", slug="acme", verified_company_name=None),
    ]
    text = render_suggestions_yaml(matches, date(2026, 6, 16))
    assert "name: Stripe" in text
    assert "ats: greenhouse" in text
    assert "slug: stripe" in text
    assert "company_name: 'Stripe'" in text
    assert "name: Acme Co" in text
    assert "ats: lever" in text


# --- Board health diagnosis (Part 3) ---


def _fake_fetcher(result):
    """Build a (name, slug, client) fetcher that returns `result` or raises it."""
    def fetch(name, slug, client):
        if isinstance(result, Exception):
            raise result
        return result
    return fetch


def _stale(name, dry=60):
    return StaleFinding(name=name, dry_days=dry, last_active=None, last_count=0)


def test_diagnose_stale_classifies_empty_gone_and_has_ads(monkeypatch):
    monkeypatch.setitem(discover_mod._FETCHERS, "empty_ats", _fake_fetcher([]))
    monkeypatch.setitem(discover_mod._FETCHERS, "gone_ats",
                        _fake_fetcher(httpx.ConnectError("boom")))
    monkeypatch.setitem(discover_mod._FETCHERS, "live_ats", _fake_fetcher(["ad"]))
    monkeypatch.setattr(discover_mod, "probe_company", lambda *a, **k: None)

    companies = [
        {"name": "Empty", "ats": "empty_ats", "slug": "e"},
        {"name": "Gone", "ats": "gone_ats", "slug": "g"},
        {"name": "Live", "ats": "live_ats", "slug": "l"},
    ]
    stale = [_stale("Empty"), _stale("Gone"), _stale("Live")]

    out = {f.name: f.status for f in diagnose_stale(stale, companies, client=object())}
    assert out == {"Empty": "empty", "Gone": "gone", "Live": "has_ads"}


def test_diagnose_stale_reports_a_migration(monkeypatch):
    monkeypatch.setitem(discover_mod._FETCHERS, "empty_ats", _fake_fetcher([]))
    moved = CompanyMatch(name="Squirro", ats="bamboohr", slug="squirro")
    calls = {}

    def fake_probe(name, client, skip=None):
        calls["skip"] = skip
        return moved

    monkeypatch.setattr(discover_mod, "probe_company", fake_probe)

    companies = [{"name": "Squirro", "ats": "empty_ats", "slug": "squirro"}]
    findings = diagnose_stale([_stale("Squirro")], companies, client=object())

    assert findings[0].moved_to == moved
    assert calls["skip"] == {("empty_ats", "squirro")}  # its own board is skipped


def test_diagnose_stale_migration_search_skips_ledger_rejections(monkeypatch):
    from jobradar.discovery import discovery_ledger

    monkeypatch.setitem(discover_mod._FETCHERS, "empty_ats", _fake_fetcher([]))
    calls = {}

    def fake_probe(name, client, skip=None):
        calls["skip"] = skip
        return None

    monkeypatch.setattr(discover_mod, "probe_company", fake_probe)

    ledger = {}
    discovery_ledger.reject_board(ledger, "Squirro", "ashby", "squirro")  # a known collision

    companies = [{"name": "Squirro", "ats": "empty_ats", "slug": "squirro"}]
    diagnose_stale([_stale("Squirro")], companies, client=object(), ledger=ledger)

    # The migration search avoids both the configured board and the rejection.
    assert calls["skip"] == {("empty_ats", "squirro"), ("ashby", "squirro")}


def test_diagnose_stale_flags_unsupported_ats(monkeypatch):
    monkeypatch.setattr(discover_mod, "probe_company", lambda *a, **k: None)
    companies = [{"name": "Old", "ats": "deadats", "slug": "old"}]
    findings = diagnose_stale([_stale("Old")], companies, client=object())
    assert findings[0].status == "unsupported"


def test_render_health_report_groups_by_status():
    findings = [
        HealthFinding(name="G", ats="lever", slug="g", dry_days=90, status="gone",
                      note="board unreachable: 404"),
        HealthFinding(name="E", ats="greenhouse", slug="e", dry_days=60, status="empty",
                      moved_to=CompanyMatch(name="E", ats="bamboohr", slug="e")),
    ]
    text = render_health_report(findings, date(2026, 7, 1))
    assert "Board gone" in text and "Empty — verify" in text
    assert "Possibly moved to bamboohr/e" in text
    assert "dry 90 days" in text


def test_render_health_report_empty_is_reassuring():
    text = render_health_report([], date(2026, 7, 1))
    assert "No configured board" in text


def test_run_health_check_writes_report_for_a_dry_board(tmp_path, monkeypatch):
    runs = tmp_path / "runs.jsonl"
    runs.write_text(
        json.dumps({
            "date": "2026-05-01",
            "sources": [{"name": "company_pages", "meta": {"company_counts": {"Dry": 0}}}],
        }) + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "company_health.md"
    monkeypatch.setitem(discover_mod._FETCHERS, "empty_ats", _fake_fetcher([]))
    monkeypatch.setattr(discover_mod, "probe_company", lambda *a, **k: None)

    findings = run_health_check(
        [{"name": "Dry", "ats": "empty_ats", "slug": "d"}],
        runs_path=runs, report_path=report,
        today=date(2026, 7, 1), stale_days=45,
    )

    assert [f.name for f in findings] == ["Dry"]
    assert "Dry" in report.read_text(encoding="utf-8")


def test_run_health_check_writes_clean_report_when_all_healthy(tmp_path):
    runs = tmp_path / "runs.jsonl"
    runs.write_text(
        json.dumps({
            "date": "2026-06-28",
            "sources": [{"name": "company_pages", "meta": {"company_counts": {"Ok": 5}}}],
        }) + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "company_health.md"
    findings = run_health_check(
        [{"name": "Ok", "ats": "greenhouse", "slug": "ok"}],
        runs_path=runs, report_path=report,
        today=date(2026, 7, 1), stale_days=45,
    )
    assert findings == []
    assert "No configured board" in report.read_text(encoding="utf-8")
