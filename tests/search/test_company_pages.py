import json
import logging

import httpx
import pytest

from jobradar.search.sources.base import RawPosting
from jobradar.search.sources.company_pages import (
    _FETCHERS,
    CompanyPagesSource,
    _fetch_avature,
    _fetch_bamboohr,
    _fetch_brassring,
    _fetch_google,
    _fetch_icims,
    _fetch_join,
    _fetch_lever,
    _fetch_onlyfy,
    _fetch_personio,
    _fetch_prospective,
    _fetch_recruitee,
    _fetch_smartrecruiters,
    _fetch_successfactors,
    _fetch_teamtailor,
    _fetch_workable,
    _fetch_workday,
)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)

    def json(self):
        return self._payload


class _SRClient:
    """Fake httpx client for the SmartRecruiters list+detail call pattern.

    A call with `params` is the (paginated) list endpoint; a call without is a
    per-posting detail fetch, keyed by the id at the end of the URL.
    """

    def __init__(self, pages: dict[int, dict], details: dict[str, dict]):
        self._pages = pages  # offset -> list page payload
        self._details = details  # posting id -> detail payload
        self.list_calls = 0
        self.detail_calls = 0

    def get(self, url, params=None):
        if params is not None:
            self.list_calls += 1
            return _Resp(self._pages[params["offset"]])
        self.detail_calls += 1
        posting_id = url.rsplit("/", 1)[1]
        return _Resp(self._details[posting_id])


def _detail(name, *, city, country, desc, url):
    return {
        "name": name,
        "company": {"name": "Acme"},
        "location": {"fullLocation": f"{city}, , {country}"},
        "postingUrl": url,
        "jobAd": {
            "sections": {
                "jobDescription": {"title": "Job Description", "text": f"<p>{desc}</p>"},
                "qualifications": {"title": "Qualifications", "text": "<ul><li>SQL</li></ul>"},
            }
        },
    }


def test_smartrecruiters_fetches_list_and_details():
    pages = {
        0: {
            "totalFound": 2,
            "content": [
                {"id": "1", "name": "PM Zurich", "location": {"fullLocation": "Zurich, , Switzerland"}},
                {"id": "2", "name": "PM Remote", "location": {"fullLocation": "Berlin, , Germany"}},
            ],
        }
    }
    details = {
        "1": _detail("PM Zurich", city="Zurich", country="Switzerland", desc="Own the roadmap",
                     url="https://jobs.smartrecruiters.com/Acme/1-pm-zurich"),
        "2": _detail("PM Remote", city="Berlin", country="Germany", desc="Growth PM",
                     url="https://jobs.smartrecruiters.com/Acme/2-pm-remote"),
    }
    client = _SRClient(pages, details)

    postings = _fetch_smartrecruiters("Acme", "acme", client)

    assert client.list_calls == 1  # single page -> loop stops after offset>=totalFound
    assert client.detail_calls == 2
    assert [p.title for p in postings] == ["PM Zurich", "PM Remote"]
    assert postings[0].source == "smartrecruiters"
    assert postings[0].company == "Acme"
    assert postings[0].url == "https://jobs.smartrecruiters.com/Acme/1-pm-zurich"
    assert postings[0].location == "Zurich, , Switzerland"
    # description joins job description + qualifications, HTML stripped
    assert "Own the roadmap" in postings[0].description
    assert "SQL" in postings[0].description
    assert "<p>" not in postings[0].description


def test_smartrecruiters_paginates():
    pages = {
        0: {"totalFound": 3, "content": [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]},
        2: {"totalFound": 3, "content": [{"id": "3", "name": "C"}]},
    }
    details = {
        i: _detail(i, city="Zug", country="Switzerland", desc="d", url=f"https://x/{i}")
        for i in ("1", "2", "3")
    }
    client = _SRClient(pages, details)

    postings = _fetch_smartrecruiters("Acme", "acme", client)

    assert client.list_calls == 2  # two pages fetched
    assert [p.title for p in postings] == ["A", "B", "C"]


def test_smartrecruiters_empty_board():
    client = _SRClient({0: {"totalFound": 0, "content": []}}, {})
    assert _fetch_smartrecruiters("Acme", "acme", client) == []
    assert client.detail_calls == 0


# --- Personio ---

_PERSONIO_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<workzag-jobs>
  <position>
    <id>123</id>
    <office>Zurich</office>
    <department>Product</department>
    <name>Product Manager</name>
    <jobDescriptions>
      <jobDescription><name>Tasks</name><value>&lt;p&gt;Own the roadmap&lt;/p&gt;</value></jobDescription>
      <jobDescription><name>Profile</name><value>&lt;ul&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;/ul&gt;</value></jobDescription>
    </jobDescriptions>
    <employmentType>permanent</employmentType>
    <schedule>full-time</schedule>
  </position>
  <position>
    <id>456</id>
    <office>Remote</office>
    <name>Data Analyst</name>
    <jobDescriptions>
      <jobDescription><name>Tasks</name><value>Analyze data</value></jobDescription>
    </jobDescriptions>
  </position>
</workzag-jobs>
"""


class _XMLResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class _PersonioClient:
    def __init__(self, content):
        self._content = content
        self.url = None

    def get(self, url):
        self.url = url
        return _XMLResp(self._content)


def test_personio_parses_positions():
    client = _PersonioClient(_PERSONIO_XML)
    postings = _fetch_personio("Planted", "planted", client)

    assert client.url == "https://planted.jobs.personio.de/xml"
    assert [p.title for p in postings] == ["Product Manager", "Data Analyst"]
    p = postings[0]
    assert p.source == "personio"
    assert p.url == "https://planted.jobs.personio.de/job/123"
    assert p.company == "Planted"
    assert p.location == "Zurich"
    # description joins all jobDescription values with HTML stripped
    assert "Own the roadmap" in p.description
    assert "SQL" in p.description
    assert "<p>" not in p.description and "&lt;" not in p.description


def test_personio_empty_feed():
    assert _fetch_personio("Acme", "acme", _PersonioClient(b"<workzag-jobs></workzag-jobs>")) == []


def test_personio_skips_position_without_id():
    xml = b"<workzag-jobs><position><name>No ID</name></position></workzag-jobs>"
    assert _fetch_personio("Acme", "acme", _PersonioClient(xml)) == []


# --- Recruitee ---


class _JsonClient:
    def __init__(self, payload):
        self._payload = payload
        self.url = None

    def get(self, url):
        self.url = url
        return _Resp(self._payload)


def test_recruitee_parses_published_offers():
    payload = {
        "offers": [
            {
                "title": "Lead Engineer",
                "careers_url": "https://acme.recruitee.com/o/lead-engineer",
                "location": "Zurich, Switzerland",
                "city": "Zurich",
                "country": "Switzerland",
                "department": "Engineering",
                "description": "<p>Build things</p>",
                "requirements": "<ul><li>Python</li></ul>",
                "status": "published",
                "id": 42,
                "company_name": "Acme",
            },
            {"title": "Draft role", "status": "draft", "careers_url": "x"},  # skipped
        ]
    }
    client = _JsonClient(payload)
    postings = _fetch_recruitee("Acme", "acme", client)

    assert client.url == "https://acme.recruitee.com/api/offers/"
    assert len(postings) == 1  # the draft is skipped
    p = postings[0]
    assert p.source == "recruitee"
    assert p.url == "https://acme.recruitee.com/o/lead-engineer"
    assert p.title == "Lead Engineer"
    assert p.company == "Acme"
    assert p.location == "Zurich, Switzerland"
    assert "Build things" in p.description and "Python" in p.description
    assert "<p>" not in p.description


def test_recruitee_empty_board():
    assert _fetch_recruitee("Acme", "acme", _JsonClient({"offers": []})) == []


# --- Workable ---


def test_workable_parses_jobs():
    payload = {
        "name": "Acme",
        "jobs": [
            {
                "title": "Product Manager",
                "url": "https://apply.workable.com/j/ABC",
                "city": "Zurich",
                "country": "Switzerland",
                "department": "Product",
                "description": "<p>Lead the roadmap</p>",
                "telecommuting": False,
                "shortcode": "ABC",
            },
            {
                "title": "Remote Engineer",
                "url": "https://apply.workable.com/j/DEF",
                "city": "",
                "state": "",
                "country": "",
                "telecommuting": True,
                "description": "<p>x</p>",
            },
        ],
    }
    client = _JsonClient(payload)
    postings = _fetch_workable("Acme", "acme", client)

    assert client.url == "https://apply.workable.com/api/v1/widget/accounts/acme?details=true"
    assert len(postings) == 2
    assert postings[0].source == "workable"
    assert postings[0].url == "https://apply.workable.com/j/ABC"
    assert postings[0].location == "Zurich, Switzerland"
    assert "Lead the roadmap" in postings[0].description and "<p>" not in postings[0].description
    assert postings[1].location == "Remote"  # telecommuting with no city


def test_workable_empty_account():
    assert _fetch_workable("Acme", "acme", _JsonClient({"name": "Acme", "jobs": []})) == []


# --- Teamtailor ---


def test_teamtailor_parses_feed():
    payload = {
        "items": [
            {
                "title": "Product Manager",
                "url": "https://acme.teamtailor.com/jobs/1-product-manager",
                "content_html": "<p>Own the roadmap</p>",
                "id": "uuid-1",
                "date_published": "2026-06-01T10:00:00+02:00",
                "_jobposting": {
                    "jobLocation": [
                        {"address": {"addressLocality": "Zürich", "addressRegion": "Switzerland", "addressCountry": "CH"}}
                    ],
                    "hiringOrganization": {"name": "Acme"},
                },
            }
        ]
    }
    client = _JsonClient(payload)
    postings = _fetch_teamtailor("Acme", "acme", client)

    assert client.url == "https://acme.teamtailor.com/jobs.json"
    assert len(postings) == 1
    p = postings[0]
    assert p.source == "teamtailor"
    assert p.url == "https://acme.teamtailor.com/jobs/1-product-manager"
    assert p.title == "Product Manager"
    assert p.location == "Zürich, Switzerland"  # locality + region (not the CH code)
    assert "Own the roadmap" in p.description and "<p>" not in p.description


def test_teamtailor_handles_missing_location():
    payload = {"items": [{"title": "Remote Role", "url": "u", "content_html": "x", "_jobposting": {}}]}
    postings = _fetch_teamtailor("Acme", "acme", _JsonClient(payload))
    assert postings[0].location is None


def test_teamtailor_empty_feed():
    assert _fetch_teamtailor("Acme", "acme", _JsonClient({"items": []})) == []


# --- Workday ---

# Switzerland + Germany nested under locationMainGroup -> locationCountry, exactly
# like the live Novartis facet tree.
_WD_FACETS_CH = [
    {
        "facetParameter": "locationMainGroup",
        "values": [
            {
                "facetParameter": "locationCountry",
                "descriptor": "Country",
                "values": [
                    {"descriptor": "Switzerland", "id": "CH1", "count": 2},
                    {"descriptor": "Germany", "id": "DE1", "count": 5},
                ],
            }
        ],
    }
]


def _wd_detail(*, title, desc, location, url, req="REQ-1"):
    return {
        "jobPostingInfo": {
            "title": title,
            "jobDescription": f"<p>{desc}</p>",
            "location": location,
            "externalUrl": url,
            "jobReqId": req,
            "timeType": "Full time",
            "country": {"descriptor": "Switzerland", "id": "CH1"},
        }
    }


class _WorkdayClient:
    """Fake for the Workday probe(POST) + paginated list(POST) + detail(GET) flow.

    A POST with empty appliedFacets is the facet-discovery probe; a POST with
    facets is a (paginated) list call keyed by offset; a GET is a per-posting
    detail call matched by the externalPath suffix of the URL.
    """

    def __init__(self, facets, pages, details, board_total=0):
        self._facets = facets
        self._pages = pages  # offset -> {"total": n, "jobPostings": [...]}
        self._details = details  # externalPath -> detail payload
        # Board-wide posting count the probe reports, i.e. before any location
        # scoping — only the unscopable-board size guard reads it.
        self._board_total = board_total
        self.applied = []
        self.list_calls = 0
        self.detail_calls = 0

    def post(self, url, json):
        # The connector's discovery probe uses limit=1; paginated list calls use
        # the full page size. (Don't key on appliedFacets — a single-country board
        # paginates with an empty {} facet, same as the probe.)
        if json.get("limit") == 1:
            return _Resp({"facets": self._facets, "total": self._board_total})
        self.applied.append(json["appliedFacets"])
        self.list_calls += 1
        return _Resp(self._pages[json["offset"]])

    def get(self, url):
        self.detail_calls += 1
        for path, payload in self._details.items():
            if url.endswith(path):
                # A payload may be a pre-built _Resp (e.g. a 403 for a closed
                # posting) or a plain detail dict to wrap in a 200.
                return payload if isinstance(payload, _Resp) else _Resp(payload)
        raise KeyError(url)


def test_workday_filters_to_switzerland_and_fetches_details():
    pages = {
        0: {
            "total": 2,
            "jobPostings": [
                {"externalPath": "/job/Basel-City/PM_REQ-1", "title": "PM", "locationsText": "Basel (City)"},
                {"externalPath": "/job/Zurich/Eng_REQ-2", "title": "Eng", "locationsText": "Zurich"},
            ],
        }
    }
    details = {
        "/job/Basel-City/PM_REQ-1": _wd_detail(
            title="Product Manager", desc="Own the roadmap", location="Basel (City)",
            url="https://novartis.wd3.myworkdayjobs.com/Novartis_Careers/job/Basel-City/PM_REQ-1",
        ),
        "/job/Zurich/Eng_REQ-2": _wd_detail(
            title="Engineer", desc="Build things", location="Zurich",
            url="https://novartis.wd3.myworkdayjobs.com/Novartis_Careers/job/Zurich/Eng_REQ-2",
        ),
    }
    client = _WorkdayClient(_WD_FACETS_CH, pages, details)

    postings = _fetch_workday("Novartis", "novartis:wd3:Novartis_Careers", client)

    # Switzerland facet discovered and applied server-side (not Germany).
    assert client.applied == [{"locationCountry": ["CH1"]}]
    assert client.list_calls == 1
    assert client.detail_calls == 2
    p = postings[0]
    assert p.source == "workday"
    assert p.company == "Novartis"
    assert p.title == "Product Manager"  # detail title preferred over list title
    assert p.location == "Basel (City)"
    assert p.url == "https://novartis.wd3.myworkdayjobs.com/Novartis_Careers/job/Basel-City/PM_REQ-1"
    assert "Own the roadmap" in p.description and "<p>" not in p.description
    assert p.raw["jobReqId"] == "REQ-1" and p.raw["country"] == "Switzerland"


def test_workday_paginates():
    pages = {
        0: {"total": 3, "jobPostings": [
            {"externalPath": "/job/a", "title": "A"}, {"externalPath": "/job/b", "title": "B"}]},
        2: {"total": 3, "jobPostings": [{"externalPath": "/job/c", "title": "C"}]},
    }
    details = {
        f"/job/{i}": _wd_detail(title=i.upper(), desc="d", location="Basel",
                                url=f"https://x.wd3.myworkdayjobs.com/S/job/{i}")
        for i in ("a", "b", "c")
    }
    # page size is 20, so this exercises the offset>=total stop, not a full page.
    client = _WorkdayClient(_WD_FACETS_CH, pages, details)

    postings = _fetch_workday("Acme", "acme:wd3:S", client)

    assert client.list_calls == 2  # two pages fetched
    assert [p.title for p in postings] == ["A", "B", "C"]


def test_workday_no_target_country_returns_empty():
    # Multinational board (has a locationCountry facet) but no Swiss roles.
    facets_de = [
        {"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
                {"descriptor": "Germany", "id": "DE1", "count": 5}]}]}
    ]
    client = _WorkdayClient(facets_de, pages={}, details={})
    assert _fetch_workday("Acme", "acme:wd3:S", client) == []
    assert client.list_calls == 0  # never paginated
    assert client.detail_calls == 0


def test_workday_single_country_board_fetches_all():
    # A Swiss-only employer (e.g. Swisscom) has no locationCountry facet — its
    # location facet lists cities — so the whole board is fetched unfiltered.
    facets_cities = [
        {"facetParameter": "locationMainGroup", "descriptor": None, "values": [
            {"facetParameter": "locations", "descriptor": "Locations", "values": [
                {"descriptor": "Bern", "id": "b1"}, {"descriptor": "Zurich", "id": "z1"}]}]}
    ]
    pages = {0: {"total": 1, "jobPostings": [{"externalPath": "/job/Bern/Eng", "title": "Eng"}]}}
    details = {"/job/Bern/Eng": _wd_detail(
        title="Engineer", desc="Build", location="Bern",
        url="https://swisscom.wd103.myworkdayjobs.com/S/job/Bern/Eng")}
    client = _WorkdayClient(facets_cities, pages, details)

    postings = _fetch_workday("Swisscom", "swisscom:wd103:S", client)

    assert client.applied == [{}]  # no country filter applied
    assert [p.title for p in postings] == ["Engineer"]


# Johnson & Johnson's shape: no locationCountry facet at all, just a flat
# `locations` list whose descriptors carry the country ("Zug, Switzerland").
# "Alabama (Any City)" is a real J&J entry — a country-less descriptor sitting
# alongside qualified ones.
_WD_FACETS_QUALIFIED_LOCATIONS = [
    {
        "facetParameter": "locationMainGroup",
        "descriptor": None,
        "values": [
            {
                "facetParameter": "locations",
                "descriptor": "Locations",
                "values": [
                    {"descriptor": "Zug, Switzerland", "id": "zug1", "count": 25},
                    {"descriptor": "Allschwil, Basel-Country, Switzerland", "id": "als1", "count": 9},
                    {"descriptor": "Aachen, North Rhine-Westphalia, Germany", "id": "aac1", "count": 15},
                    {"descriptor": "Alabama (Any City)", "id": "ala1", "count": 5},
                ],
            }
        ],
    }
]


def test_workday_scopes_by_country_qualified_locations_facet():
    # A multinational with no country facet is still scoped server-side, off the
    # country suffix of each location descriptor — otherwise its whole (1750+
    # posting) board would be fetched one detail call at a time.
    pages = {0: {"total": 1, "jobPostings": [{"externalPath": "/job/Zug-Switzerland/PM_R-1", "title": "PM"}]}}
    details = {"/job/Zug-Switzerland/PM_R-1": _wd_detail(
        title="Product Owner", desc="Own it", location="Zug, Switzerland",
        url="https://jj.wd5.myworkdayjobs.com/JJ/job/Zug-Switzerland/PM_R-1")}
    client = _WorkdayClient(_WD_FACETS_QUALIFIED_LOCATIONS, pages, details, board_total=1753)

    postings = _fetch_workday("Johnson & Johnson", "jj:wd5:JJ", client)

    # Both Swiss locations applied; Germany and the country-less entry left out.
    assert client.applied == [{"locations": ["zug1", "als1"]}]
    assert [p.title for p in postings] == ["Product Owner"]


# Takeda's shape: a country facet under the tenant-built `Location_Country`
# spelling, plus a `locations` list whose Swiss descriptors are only partly
# country-qualified ("Zurich, Switzerland" is; "CHE - Neuchatel" is not).
_WD_FACETS_UNDERSCORE_COUNTRY = [
    {
        "facetParameter": "Location_Country",
        "descriptor": "Country",
        "values": [
            {"descriptor": "Switzerland", "id": "che1", "count": 26},
            {"descriptor": "Germany", "id": "deu1", "count": 90},
        ],
    },
    {
        "facetParameter": "locationMainGroup",
        "descriptor": None,
        "values": [
            {
                "facetParameter": "locations",
                "descriptor": "Locations",
                "values": [
                    {"descriptor": "Zurich, Switzerland", "id": "zrh1", "count": 17},
                    {"descriptor": "CHE - Neuchatel", "id": "ne1", "count": 2},
                    {"descriptor": "CHE - Glattpark (Opfikon) - Zurich HQ", "id": "hq1", "count": 5},
                ],
            }
        ],
    },
]


def test_workday_reads_underscore_spelled_country_facet():
    # The country facet wins over the partly-qualified `locations` list: scoping
    # off the latter would keep "Zurich, Switzerland" and drop the site-coded
    # Swiss entries (17 of 26 on the real board).
    pages = {0: {"total": 1, "jobPostings": [{"externalPath": "/job/CHE---Neuchatel/Analyst_R-1", "title": "Analyst"}]}}
    details = {"/job/CHE---Neuchatel/Analyst_R-1": _wd_detail(
        title="Data Analyst", desc="Analyse", location="CHE - Neuchatel",
        url="https://takeda.wd502.myworkdayjobs.com/External/job/CHE---Neuchatel/Analyst_R-1")}
    client = _WorkdayClient(_WD_FACETS_UNDERSCORE_COUNTRY, pages, details, board_total=1779)

    postings = _fetch_workday("Takeda", "takeda:wd502:External", client)

    assert client.applied == [{"Location_Country": ["che1"]}]
    assert [p.title for p in postings] == ["Data Analyst"]


# NVIDIA's shape: no country facet of either spelling, countries on the first
# level of the location hierarchy, and `locations` descriptors that lead with
# the country ("Switzerland, Zurich") so the suffix check can't read them.
def _wd_facets_hierarchy(*level1):
    return [
        {
            "facetParameter": "locationMainGroup",
            "descriptor": None,
            "values": [
                {
                    "facetParameter": "locationHierarchy1",
                    "descriptor": "Locations",
                    "values": [
                        {"descriptor": name, "id": f"h{i}", "count": 40}
                        for i, name in enumerate(level1)
                    ],
                },
                {
                    "facetParameter": "locations",
                    "descriptor": "Sites",
                    "values": [
                        {"descriptor": "Switzerland, Zurich", "id": "zrh1", "count": 18},
                        {"descriptor": "Switzerland, Remote", "id": "rem1", "count": 28},
                    ],
                },
            ],
        }
    ]


def test_workday_scopes_by_hierarchy_level_naming_countries():
    pages = {0: {"total": 1, "jobPostings": [{"externalPath": "/job/Switzerland-Zurich/Eng_JR1", "title": "Eng"}]}}
    details = {"/job/Switzerland-Zurich/Eng_JR1": _wd_detail(
        title="Performance Engineer", desc="Tune", location="Switzerland, Zurich",
        url="https://nvidia.wd5.myworkdayjobs.com/S/job/Switzerland-Zurich/Eng_JR1")}
    client = _WorkdayClient(_wd_facets_hierarchy("Germany", "Switzerland"), pages, details, board_total=2000)

    postings = _fetch_workday("NVIDIA", "nvidia:wd5:S", client)

    assert client.applied == [{"locationHierarchy1": ["h1"]}]
    assert [p.title for p in postings] == ["Performance Engineer"]


def test_workday_hierarchy_level_without_a_target_country_is_not_trusted():
    # The level might hold regions on another tenant, so naming no target
    # country is no evidence the board has no Swiss roles: a large board must
    # fail loudly as unscopable, not be skipped as "no Swiss postings".
    client = _WorkdayClient(_wd_facets_hierarchy("EMEA", "Americas"), {}, {}, board_total=2000)

    with pytest.raises(ValueError, match="unscopable"):
        _fetch_workday("Acme", "acme:wd5:S", client)
    assert client.list_calls == 0


# Roche's shape: multinational, but the flat `locations` list mixes cities,
# regions and countries as bare descriptors, with one low-count entry literally
# named "Switzerland". Nothing here carries a readable country dimension.
_WD_FACETS_BARE_MIXED_LOCATIONS = [
    {
        "facetParameter": "locationMainGroup",
        "descriptor": None,
        "values": [
            {
                "facetParameter": "locations",
                "descriptor": "Locations",
                "values": [
                    {"descriptor": "Basel", "id": "bas1", "count": 93},
                    {"descriptor": "Rotkreuz", "id": "rot1", "count": 23},
                    {"descriptor": "Kaiseraugst", "id": "kai1", "count": 14},
                    {"descriptor": "Shanghai", "id": "sha1", "count": 130},
                    {"descriptor": "Switzerland", "id": "ch1", "count": 1},
                ],
            }
        ],
    }
]


def test_workday_bare_country_named_location_does_not_scope_the_board():
    # Regression: "Switzerland" sitting bare among 200-odd cities is ONE location
    # carrying one req, not the board's Swiss dimension. Scoping to it would have
    # cut a 1172-posting board to that single posting and silently dropped the
    # other 125 Swiss ones — and a board still returning 1/day looks alive to
    # company_health. With no country to read, an oversized board must instead
    # fail loudly so someone configures its locations explicitly.
    client = _WorkdayClient(_WD_FACETS_BARE_MIXED_LOCATIONS, {}, {}, board_total=1172)

    with pytest.raises(ValueError, match="unscopable"):
        _fetch_workday("Roche", "roche:wd3:roche-ext", client)

    assert client.detail_calls == 0


def test_workday_slug_locations_scope_the_board():
    # The 4th slug segment names location descriptors directly, for a board with
    # no country dimension to filter on. Only those ids are applied.
    pages = {0: {"total": 1, "jobPostings": [
        {"externalPath": "/job/Rotkreuz/CRM_202607-117886-1", "title": "Team Lead CRM"}]}}
    details = {"/job/Rotkreuz/CRM_202607-117886-1": _wd_detail(
        title="Team Lead Customer Relationship Management", desc="Own the CRM capability",
        location="Rotkreuz",
        url="https://roche.wd3.myworkdayjobs.com/roche-ext/job/Rotkreuz/CRM_202607-117886-1")}
    client = _WorkdayClient(_WD_FACETS_BARE_MIXED_LOCATIONS, pages, details, board_total=1172)

    postings = _fetch_workday("Roche", "roche:wd3:roche-ext:Rotkreuz", client)

    assert client.applied == [{"locations": ["rot1"]}]
    assert [p.title for p in postings] == ["Team Lead Customer Relationship Management"]
    assert postings[0].location == "Rotkreuz"


def test_workday_slug_locations_override_the_country_filter():
    # The named locations win outright: a slug listing a German site fetches it,
    # even though the board also has Swiss ones the auto-filter would prefer.
    client = _WorkdayClient(_WD_FACETS_BARE_MIXED_LOCATIONS, {0: {"total": 0, "jobPostings": []}}, {},
                            board_total=1172)

    _fetch_workday("Roche", "roche:wd3:roche-ext:Shanghai|Rotkreuz", client)

    assert client.applied == [{"locations": ["rot1", "sha1"]}]  # sorted by descriptor


def test_workday_unknown_slug_location_is_reported_not_ignored(caplog):
    # A configured site that has vanished from the board means the run covers
    # less than the config claims — warn, and (nothing else matching) skip the
    # company rather than silently fetch the whole board.
    client = _WorkdayClient(_WD_FACETS_BARE_MIXED_LOCATIONS, {}, {}, board_total=1172)

    with caplog.at_level(logging.WARNING):
        assert _fetch_workday("Roche", "roche:wd3:roche-ext:Burgdorf", client) == []

    assert "Burgdorf" in caplog.text  # the configured spelling, not a lowercased key
    assert client.list_calls == 0 and client.detail_calls == 0


def test_workday_rejects_malformed_slug():
    client = _WorkdayClient(_WD_FACETS_BARE_MIXED_LOCATIONS, {}, {}, board_total=1)

    with pytest.raises(ValueError, match="tenant:host:site"):
        _fetch_workday("Roche", "roche:wd3", client)
    with pytest.raises(ValueError, match="tenant:host:site"):
        _fetch_workday("Roche", "roche:wd3:roche-ext:Rotkreuz:extra", client)


def test_workday_merges_additional_locations():
    # A req open in several countries names only the primary in `location` and
    # the rest in `additionalLocations`, and the list call collapses them to
    # "5 Locations". Without the merge, this Belgium-primary posting loses its
    # Zug location and the Swiss filter drops it downstream.
    detail = _wd_detail(
        title="Lead Data Product Owner", desc="Own the data products",
        location="Beerse, Antwerp, Belgium",
        url="https://jj.wd5.myworkdayjobs.com/JJ/job/Beerse-Antwerp-Belgium/Lead_R-091530",
    )
    detail["jobPostingInfo"]["additionalLocations"] = [
        "Latina, Italy", "Zug, Switzerland", "Beerse, Antwerp, Belgium",
    ]
    pages = {0: {"total": 1, "jobPostings": [
        {"externalPath": "/job/Beerse-Antwerp-Belgium/Lead_R-091530", "title": "Lead",
         "locationsText": "5 Locations"}]}}
    client = _WorkdayClient(
        _WD_FACETS_QUALIFIED_LOCATIONS,
        pages,
        {"/job/Beerse-Antwerp-Belgium/Lead_R-091530": detail},
        board_total=1753,
    )

    postings = _fetch_workday("Johnson & Johnson", "jj:wd5:JJ", client)

    # Primary first, duplicates collapsed, and "5 Locations" never used.
    assert postings[0].location == "Beerse, Antwerp, Belgium; Latina, Italy; Zug, Switzerland"


def test_workday_refuses_large_unscopable_board():
    # Bare-city descriptors mean no country could be read. That's fine for a
    # small single-country board, but on a large one it would mean hundreds of
    # detail calls — so the company is skipped (ValueError, caught per-company)
    # rather than silently fetched whole.
    facets_cities = [
        {"facetParameter": "locationMainGroup", "descriptor": None, "values": [
            {"facetParameter": "locations", "descriptor": "Locations", "values": [
                {"descriptor": "Bern", "id": "b1"}]}]}
    ]
    client = _WorkdayClient(facets_cities, pages={}, details={}, board_total=5000)

    with pytest.raises(ValueError, match="unscopable"):
        _fetch_workday("Acme", "acme:wd3:S", client)
    assert client.list_calls == 0 and client.detail_calls == 0


def test_workday_bad_slug_raises():
    with pytest.raises(ValueError):
        _fetch_workday("Acme", "acme-no-colons", _WorkdayClient(_WD_FACETS_CH, {}, {}))


def test_workday_skips_posting_closed_mid_pagination():
    # The first posting's detail 403s (closed between list and detail); the
    # company is not aborted — the second posting is still returned.
    pages = {0: {"total": 2, "jobPostings": [
        {"externalPath": "/job/Basel/Closed", "title": "Closed"},
        {"externalPath": "/job/Zurich/Open", "title": "Open"}]}}
    details = {
        "/job/Basel/Closed": _Resp({}, status=403),
        "/job/Zurich/Open": _wd_detail(title="Open Role", desc="d", location="Zurich",
                                       url="https://x.wd3.myworkdayjobs.com/S/job/Zurich/Open"),
    }
    client = _WorkdayClient(_WD_FACETS_CH, pages, details)

    postings = _fetch_workday("Acme", "acme:wd3:S", client)

    assert [p.title for p in postings] == ["Open Role"]  # closed one skipped, run continues


def test_workday_skips_non_applyable_posting():
    pages = {0: {"total": 2, "jobPostings": [
        {"externalPath": "/job/Basel/Stale", "title": "Stale"},
        {"externalPath": "/job/Zurich/Open", "title": "Open"}]}}
    stale = _wd_detail(title="Stale Role", desc="d", location="Basel", url="https://x/stale")
    stale["jobPostingInfo"]["canApply"] = False
    details = {
        "/job/Basel/Stale": stale,
        "/job/Zurich/Open": _wd_detail(title="Open Role", desc="d", location="Zurich", url="https://x/open"),
    }
    client = _WorkdayClient(_WD_FACETS_CH, pages, details)

    postings = _fetch_workday("Acme", "acme:wd3:S", client)

    assert [p.title for p in postings] == ["Open Role"]  # canApply=False dropped


# --- join ---


def _join_page_html(company: dict) -> str:
    payload = {"props": {"pageProps": {"initialState": {"company": company}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></html>'


class _HtmlResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


class _JoinClient:
    """Fake for the join.com careers-page-resolve + list + per-job-detail flow."""

    def __init__(self, company: dict, pages: dict[int, dict], details: dict[int, dict]):
        self._html = _join_page_html(company)
        self._pages = pages  # page number -> list payload
        self._details = details  # job id -> detail payload
        self.list_calls = 0
        self.detail_calls = 0
        self.list_params: list[dict] = []

    def get(self, url, params=None):
        if url.endswith("/companies/acme"):
            return _HtmlResp(self._html)
        if params is not None:
            self.list_calls += 1
            self.list_params.append(params)
            # join.com refuses pageSize > 5 with a 422 rather than clamping it,
            # so model that here — a fake that ignores pageSize is what let the
            # stale pageSize=50 sit broken in production.
            if params.get("pageSize", 0) > 5:
                return _Resp(
                    [{"param": "pageSize", "msg": "Invalid value"}], status=422
                )
            return _Resp(self._pages[params["page"]])
        job_id = int(url.rsplit("/", 1)[1])
        self.detail_calls += 1
        return _Resp(self._details[job_id])


def test_join_fetches_list_and_details():
    pages = {
        1: {
            "items": [
                {
                    "id": 1,
                    "idParam": "1-pm-zurich",
                    "title": "PM Zurich",
                    "city": {"cityName": "Zurich"},
                    "country": {"name": "Switzerland"},
                    "workplaceType": "HYBRID",
                }
            ],
            "pagination": {"page": 1, "pageCount": 1, "pageSize": 1, "rowCount": 1},
        }
    }
    details = {1: {"description": "Own the roadmap"}}
    client = _JoinClient({"id": 42, "name": "Acme", "domain": "acme"}, pages, details)

    postings = _fetch_join("Acme", "acme", client)

    assert client.list_calls == 1
    assert client.detail_calls == 1
    assert [p.title for p in postings] == ["PM Zurich"]
    assert postings[0].source == "join"
    assert postings[0].company == "Acme"
    assert postings[0].url == "https://join.com/companies/acme/1-pm-zurich"
    assert postings[0].location == "Zurich, Switzerland"
    assert postings[0].description == "Own the roadmap"


def test_join_requests_a_page_size_the_api_accepts():
    # Regression: join.com's public API rejects pageSize > 5 outright (HTTP 422,
    # verified live 2026-07-17). The connector used to ask for 50, so every join
    # company 422'd and was skipped — silently, since one company failing isn't
    # enough to mark the source degraded.
    pages = {1: {"items": [], "pagination": {"pageCount": 0}}}
    client = _JoinClient({"id": 42, "name": "Acme", "domain": "acme"}, pages, {})

    postings = _fetch_join("Acme", "acme", client)

    assert postings == []
    assert client.list_params == [{"page": 1, "pageSize": 5}]


def test_join_paginates():
    pages = {
        1: {
            "items": [{"id": 1, "idParam": "1-a", "title": "A"}],
            "pagination": {"page": 1, "pageCount": 2, "pageSize": 1, "rowCount": 2},
        },
        2: {
            "items": [{"id": 2, "idParam": "2-b", "title": "B"}],
            "pagination": {"page": 2, "pageCount": 2, "pageSize": 1, "rowCount": 2},
        },
    }
    details = {1: {"description": "d1"}, 2: {"description": "d2"}}
    client = _JoinClient({"id": 42, "name": "Acme", "domain": "acme"}, pages, details)

    postings = _fetch_join("Acme", "acme", client)

    assert client.list_calls == 2
    assert [p.title for p in postings] == ["A", "B"]


def test_join_empty_board():
    pages = {1: {"items": [], "pagination": {"page": 1, "pageCount": 0, "pageSize": 0, "rowCount": 0}}}
    client = _JoinClient({"id": 42, "name": "Acme", "domain": "acme"}, pages, {})

    assert _fetch_join("Acme", "acme", client) == []
    assert client.detail_calls == 0


def test_join_missing_next_data_raises():
    client = _JoinClient({"id": 42, "name": "Acme", "domain": "acme"}, {}, {})
    client._html = "<html>no next data here</html>"

    with pytest.raises(ValueError):
        _fetch_join("Acme", "acme", client)


# --- BambooHR ---


class _BambooClient:
    """Fake for the BambooHR list + per-posting detail flow.

    A GET ending in /list is the (single-page) list endpoint; a GET ending in
    /detail is a per-posting detail fetch, keyed by the id in the URL.
    """

    def __init__(self, listing: dict, details: dict[str, dict]):
        self._listing = listing
        self._details = details  # posting id -> detail payload
        self.list_calls = 0
        self.detail_calls = 0
        self.urls: list[str] = []

    def get(self, url):
        self.urls.append(url)
        if url.endswith("/list"):
            self.list_calls += 1
            return _Resp(self._listing)
        self.detail_calls += 1
        posting_id = url.rsplit("/", 2)[1]  # .../{id}/detail
        return _Resp(self._details[posting_id])


def _bamboo_detail(*, name, share_url, desc, location=None, ats_location=None, compensation=None):
    return {
        "result": {
            "jobOpening": {
                "jobOpeningName": name,
                "jobOpeningShareUrl": share_url,
                "description": desc,
                "location": location or {"city": None, "state": None},
                "atsLocation": ats_location or {"country": None, "state": None, "city": None},
                "compensation": compensation,
                "datePosted": "2026-07-10",
            }
        }
    }


def test_bamboohr_fetches_list_and_details():
    listing = {
        "meta": {"totalCount": 2},
        "result": [
            {
                "id": "110",
                "jobOpeningName": "Knowledge Graph Engineer",
                "departmentLabel": "Delivery",
                "employmentStatusLabel": "Full-Time",
                "location": {"city": None, "state": None},
                "atsLocation": {"country": "United States", "state": "New York", "city": "New York"},
                "isRemote": None,
            },
            {
                "id": "119",
                "jobOpeningName": "Senior Software Engineer",
                "departmentLabel": "Engineering",
                "employmentStatusLabel": "Full-Time",
                "location": {"city": "Kuala Lumpur", "state": None},
                "atsLocation": {"country": None, "state": None, "city": None},
                "isRemote": None,
            },
        ],
    }
    details = {
        "110": _bamboo_detail(
            name="Knowledge Graph Engineer",
            share_url="https://squirro.bamboohr.com/careers/110",
            desc="<p>Design <b>knowledge graphs</b></p>",
            ats_location={"country": "United States", "state": "New York", "city": "New York"},
            compensation="USD 100'000-150'000",
        ),
        "119": _bamboo_detail(
            name="Senior Software Engineer",
            share_url="https://squirro.bamboohr.com/careers/119",
            desc="Python & AI platform",
            location={"city": "Kuala Lumpur", "state": None},
        ),
    }
    client = _BambooClient(listing, details)

    postings = _fetch_bamboohr("Squirro", "squirro", client)

    assert client.list_calls == 1
    assert client.detail_calls == 2
    assert [p.title for p in postings] == ["Knowledge Graph Engineer", "Senior Software Engineer"]
    p = postings[0]
    assert p.source == "bamboohr"
    assert p.company == "Squirro"
    assert p.url == "https://squirro.bamboohr.com/careers/110"
    # atsLocation (city, state, country) merged, HTML stripped from the description
    assert p.location == "New York, New York, United States"
    assert "knowledge graphs" in p.description and "<b>" not in p.description
    assert p.raw["compensation"] == "USD 100'000-150'000"
    # second posting falls back to the free-text `location` when atsLocation is empty
    assert postings[1].location == "Kuala Lumpur"


def test_bamboohr_url_falls_back_to_constructed_form():
    listing = {"result": [{"id": "7", "jobOpeningName": "PM"}]}
    details = {"7": {"result": {"jobOpening": {"description": "d"}}}}  # no jobOpeningShareUrl
    postings = _fetch_bamboohr("Acme", "acme", _BambooClient(listing, details))
    assert postings[0].url == "https://acme.bamboohr.com/careers/7"


def test_bamboohr_empty_board():
    client = _BambooClient({"meta": {"totalCount": 0}, "result": []}, {})
    assert _fetch_bamboohr("Acme", "acme", client) == []
    assert client.detail_calls == 0


def test_bamboohr_skips_posting_without_id():
    client = _BambooClient({"result": [{"jobOpeningName": "No ID"}]}, {})
    assert _fetch_bamboohr("Acme", "acme", client) == []
    assert client.detail_calls == 0


def _working(name, slug, client):
    return [
        RawPosting(
            source="fake", url=f"https://x/{slug}", title="T", company=name, description="d"
        )
    ]


def _companies(working: int, broken: int) -> list[dict]:
    # An unsupported `ats` is skipped before any HTTP call, so these cases need
    # no network stubbing at all.
    return [{"name": f"Ok{i}", "ats": "fake", "slug": f"ok{i}"} for i in range(working)] + [
        {"name": f"Bad{i}", "ats": "no-such-ats", "slug": f"bad{i}"} for i in range(broken)
    ]


def test_company_pages_degrades_when_a_large_share_of_boards_fail(monkeypatch):
    # Regression: this used to report ok unless EVERY company failed, so 26 of
    # 76 join companies failing on every run never surfaced as a degraded run.
    monkeypatch.setitem(_FETCHERS, "fake", _working)

    result = CompanyPagesSource(_companies(working=3, broken=1)).fetch()

    assert len(result.postings) == 3  # the healthy boards still came through
    assert result.ok is False  # 1 of 4 == 25% -> the picture is incomplete
    assert "1 of 4 companies skipped" in result.detail


def test_company_pages_stays_ok_when_a_few_boards_are_flaky(monkeypatch):
    monkeypatch.setitem(_FETCHERS, "fake", _working)

    result = CompanyPagesSource(_companies(working=9, broken=1)).fetch()

    assert result.ok is True  # 1 of 10 is ordinary flakiness, not a bad picture
    assert "1 of 10 companies skipped" in result.detail


def test_company_pages_degrades_when_every_board_fails():
    result = CompanyPagesSource(_companies(working=0, broken=3)).fetch()

    assert result.postings == []
    assert result.ok is False


def test_company_pages_reports_per_company_counts_in_meta(monkeypatch):
    # The health history feeds off this: a skipped/failed board records 0, a
    # working one records its raw count. It must be present for every configured
    # company so a board dropping to 0 is visible run-over-run.
    monkeypatch.setitem(_FETCHERS, "fake", _working)

    result = CompanyPagesSource(_companies(working=2, broken=1)).fetch()

    assert result.meta["company_counts"] == {"Ok0": 1, "Ok1": 1, "Bad0": 0}


def test_company_pages_with_no_companies_is_ok_not_degraded():
    # Must not divide by zero, and an empty list isn't a failure.
    result = CompanyPagesSource([]).fetch()

    assert result.ok is True
    assert result.postings == []


# --- Avature ---


class _AvatureClient:
    """Fake httpx client serving canned HTML keyed by url or (url, params)."""

    def __init__(self, pages: dict):
        self._pages = pages
        self.calls: list = []

    def get(self, url, params=None, headers=None, follow_redirects=False):
        key = (url, tuple(sorted(params.items()))) if params else url
        self.calls.append(key)
        page = self._pages.get(key)
        if page is None:
            return _HtmlResp("", status=404)
        return _HtmlResp(page)


_AVATURE_SEARCH_URL = "https://jobs.example.com/en_US/careers/SearchJobs/Switzerland||/"


def _avature_detail(title, body, fields):
    rows = "".join(
        '<div class="article__content__view__field">'
        f'<div class="article__content__view__field__label">{label}</div>'
        f'<div class="article__content__view__field__value">{value}</div>'
        "</div>"
        for label, value in fields.items()
    )
    # Metadata fields and prose live in separate article--details blocks, as on
    # the real pages; the fetcher must join both.
    return (
        f'<html><head><meta property="og:title" content="{title}" /></head><body>'
        '<article class="article article--details regular-fields--cols-2">'
        f"{rows}</article>"
        f'<article class="article article--details "><h3>Job description</h3><p>{body}</p></article>'
        '<article class="article article--actions">Apply now chrome</article></body></html>'
    )


_AVATURE_PAGE_1 = """
<a href="/en_US/careers/JobDetail/Solution-Engineer/100">Solution Engineer</a>
<a href="/en_US/careers/JobDetail/Data-Product-Owner/101">Data Product Owner</a>
<a href="/en_US/careers/SearchJobs/Switzerland||/?jobRecordsPerPage=2&amp;jobOffset=2">2</a>
"""

# Second page: one new posting plus a repeat of the first — and it is also what
# any further offset serves, which is how the pagination loop learns to stop.
_AVATURE_PAGE_2 = """
<a href="/en_US/careers/JobDetail/Solution-Engineer/100">Solution Engineer</a>
<a href="/en_US/careers/JobDetail/Platform-PM/102">Platform PM</a>
<a href="/en_US/careers/SearchJobs/Switzerland||/?jobRecordsPerPage=2&amp;jobOffset=4">3</a>
"""


def _avature_pages():
    detail = "https://jobs.example.com/en_US/careers/JobDetail"
    page2_key = ("jobOffset", 2), ("jobRecordsPerPage", 2)
    page3_key = ("jobOffset", 4), ("jobRecordsPerPage", 2)
    return {
        _AVATURE_SEARCH_URL: _AVATURE_PAGE_1,
        (_AVATURE_SEARCH_URL, page2_key): _AVATURE_PAGE_2,
        (_AVATURE_SEARCH_URL, page3_key): _AVATURE_PAGE_2,
        f"{detail}/Solution-Engineer/100": _avature_detail(
            "Solution Engineer OT &amp; Infrastruktur",
            "Own the <strong>test lab</strong>",
            {"Location(s)": " Wallisellen - Zuerich - Switzerland ", "Organization": "Smart Infrastructure"},
        ),
        f"{detail}/Data-Product-Owner/101": _avature_detail(
            "Data Product Owner", "Lead the data products", {"City": "Basel, Geneva, Zurich"}
        ),
        f"{detail}/Platform-PM/102": _avature_detail("Platform PM", "Run the platform", {}),
    }


def test_avature_fetches_list_and_details():
    client = _AvatureClient(_avature_pages())

    postings = _fetch_avature(
        "Siemens", "jobs.example.com:en_US/careers:Switzerland||", client
    )

    assert [p.title for p in postings] == [
        "Solution Engineer OT & Infrastruktur",  # og:title, entities unescaped
        "Data Product Owner",
        "Platform PM",
    ]
    assert postings[0].source == "avature"
    assert postings[0].url == "https://jobs.example.com/en_US/careers/JobDetail/Solution-Engineer/100"
    # location from the "Location(s)" / "City" labeled field, trimmed
    assert postings[0].location == "Wallisellen - Zuerich - Switzerland"
    assert postings[1].location == "Basel, Geneva, Zurich"
    assert postings[2].location is None
    # description joins every article--details block: metadata fields + prose —
    # but not page chrome outside them
    assert "Own the test lab" in postings[0].description
    assert "Smart Infrastructure" in postings[0].description
    assert "<strong>" not in postings[0].description
    assert "Apply now chrome" not in postings[0].description
    assert postings[0].raw["Organization"] == "Smart Infrastructure"
    # pages 1, 2 and the repeat page that stopped the loop, then 3 details
    assert len(client.calls) == 6


def test_avature_single_page_without_pagination_links():
    detail = "https://jobs.example.com/en_US/careers/JobDetail"
    client = _AvatureClient(
        {
            _AVATURE_SEARCH_URL: '<a href="/en_US/careers/JobDetail/Only-One/100">Only One</a>',
            f"{detail}/Only-One/100": _avature_detail("Only One", "Do the thing", {}),
        }
    )

    postings = _fetch_avature("Siemens", "jobs.example.com:en_US/careers:Switzerland||", client)

    assert [p.title for p in postings] == ["Only One"]
    assert client.calls == [_AVATURE_SEARCH_URL, f"{detail}/Only-One/100"]


def test_avature_empty_search_segment_and_closed_posting_skipped():
    # Deloitte-style slug with no search segment; one posting 404s mid-run and
    # is skipped without aborting the company.
    search_url = "https://apply.example.ch/CHCareers/SearchJobs/"
    detail = "https://apply.example.ch/CHCareers/JobDetail"
    client = _AvatureClient(
        {
            search_url: (
                '<a href="/CHCareers/JobDetail/Alive/1">Alive</a>'
                '<a href="/CHCareers/JobDetail/Closed/2">Closed</a>'
            ),
            f"{detail}/Alive/1": _avature_detail("Alive", "Still open", {"City": "Zurich"}),
            # no entry for Closed/2 -> fake answers 404
        }
    )

    postings = _fetch_avature("Deloitte", "apply.example.ch:CHCareers:", client)

    assert [p.title for p in postings] == ["Alive"]


def test_avature_bad_slug_raises():
    with pytest.raises(ValueError):
        _fetch_avature("Acme", "just-a-token", _AvatureClient({}))


# --- SuccessFactors ---


def _sf_row(href, title, location):
    return (
        '<tr class="data-row">'
        '<td class="colTitle"><span class="jobTitle hidden-phone">'
        f'<a href="{href}" class="jobTitle-link">{title}</a></span>'
        '<div class="jobdetail-phone visible-phone"><span class="jobTitle visible-phone">'
        f'<a class="jobTitle-link" href="{href}">{title}</a></span></div></td>'
        '<td class="colLocation hidden-phone"><span class="jobLocation"> '
        f'{location} <small class="nobr">+4 more&hellip;</small></span></td>'
        "</tr>"
    )


_SF_DETAIL = (
    '<html><body><span class="jobdescription"><div>'
    "<p>Own the <span style=\"font-size:14px\">roadmap</span> end to end</p>"
    '</div></span><div class="apply">APPLY FOOTER NOISE</div></body></html>'
)


def _sf_key(startrow):
    return (
        "https://careers.example.com/ey/search/",
        (("locationsearch", "Switzerland"), ("q", ""), ("startrow", startrow)),
    )


def test_successfactors_fetches_rows_and_details():
    page_1 = "<table>{}{}</table>".format(
        _sf_row("/ey/job/PM-Zurich/123/", "Product Manager", "Zurich, CH, 8005"),
        _sf_row("/ey/job/Consultant-Bern/456/", "Consultant", "Berne, CH, 3008"),
    )
    client = _AvatureClient(
        {
            _sf_key(0): page_1,
            _sf_key(2): page_1,  # out-of-range startrow re-serves the last page
            "https://careers.example.com/ey/job/PM-Zurich/123/": _SF_DETAIL,
            "https://careers.example.com/ey/job/Consultant-Bern/456/": _SF_DETAIL,
        }
    )

    postings = _fetch_successfactors("EY", "careers.example.com/ey", client)

    # one posting per row, despite the title anchor appearing twice per row
    assert [p.title for p in postings] == ["Product Manager", "Consultant"]
    assert postings[0].source == "successfactors"
    assert postings[0].url == "https://careers.example.com/ey/job/PM-Zurich/123/"
    assert postings[0].location.startswith("Zurich, CH, 8005")
    # description: jobdescription block closed by tag balance (nested spans),
    # page chrome after it excluded, HTML stripped
    assert "Own the roadmap end to end" in postings[0].description
    assert "APPLY FOOTER NOISE" not in postings[0].description
    assert "<p>" not in postings[0].description
    # list page 0, list page 2 (stop), and two details
    assert len(client.calls) == 4


def test_successfactors_stops_at_the_boards_count_not_at_new_rows():
    # Sonova's board: past the last page of the Switzerland search it serves a
    # page of unfiltered (foreign) postings — new hrefs, so "nothing new" alone
    # would keep paging and pull them in. The count in "Results 1 – 25 of 2"
    # is the stop.
    page_1 = '<span class="paginationLabel">Results <b>1 – 2</b> of <b>2</b></span>' + "<table>{}{}</table>".format(
        _sf_row("/ey/job/A/1/", "A", "Zurich, CH"), _sf_row("/ey/job/B/2/", "B", "Zurich, CH")
    )
    overflow = '<span class="paginationLabel">Results <b>3 – 10</b> of <b>2</b></span>' + _sf_row(
        "/ey/job/Foreign/3/", "Foreign", "Chicago, US"
    )
    client = _AvatureClient(
        {
            _sf_key(0): page_1,
            _sf_key(2): overflow,
            "https://careers.example.com/ey/job/A/1/": _SF_DETAIL,
            "https://careers.example.com/ey/job/B/2/": _SF_DETAIL,
            "https://careers.example.com/ey/job/Foreign/3/": _SF_DETAIL,
        }
    )

    postings = _fetch_successfactors("EY", "careers.example.com/ey", client)

    assert [p.title for p in postings] == ["A", "B"]


def test_successfactors_site_mounted_at_host_root():
    page = _sf_row("/job/Analyst/9/", "Analyst", "Zurich, CH")
    client = _AvatureClient(
        {
            (
                "https://careers.example.com/search/",
                (("locationsearch", "Switzerland"), ("q", ""), ("startrow", 0)),
            ): page,
            (
                "https://careers.example.com/search/",
                (("locationsearch", "Switzerland"), ("q", ""), ("startrow", 1)),
            ): page,
            "https://careers.example.com/job/Analyst/9/": _SF_DETAIL,
        }
    )

    postings = _fetch_successfactors("Swiss Re", "careers.example.com", client)

    assert [p.url for p in postings] == ["https://careers.example.com/job/Analyst/9/"]


def test_successfactors_skips_closed_posting():
    page = "<table>{}{}</table>".format(
        _sf_row("/ey/job/Alive/1/", "Alive", "Zurich, CH"),
        _sf_row("/ey/job/Closed/2/", "Closed", "Zurich, CH"),
    )
    client = _AvatureClient(
        {
            _sf_key(0): page,
            _sf_key(2): page,
            "https://careers.example.com/ey/job/Alive/1/": _SF_DETAIL,
            # no entry for Closed/2 -> fake answers 404
        }
    )

    postings = _fetch_successfactors("EY", "careers.example.com/ey", client)

    assert [p.title for p in postings] == ["Alive"]


def test_successfactors_empty_board():
    client = _AvatureClient({_sf_key(0): "<html>no results</html>"})
    assert _fetch_successfactors("EY", "careers.example.com/ey", client) == []


def test_successfactors_bad_slug_raises():
    with pytest.raises(ValueError):
        _fetch_successfactors("EY", "https://careers.example.com/ey", _AvatureClient({}))


# --- iCIMS/Jibe: JSON listing with descriptions inline, paged by number ---


class _IcimsClient:
    """Fake httpx client serving one canned JSON payload per page number."""

    def __init__(self, pages: dict[int, dict], status=200):
        self._pages = pages
        self._status = status
        self.calls: list = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        payload = self._pages.get(params["page"], {"jobs": []})
        return _Resp(payload, status=self._status)


def _icims_job(req_id, title, full_location, description="<p>Own the &amp; roadmap</p>", **extra):
    data = {
        "req_id": req_id,
        "slug": req_id,
        "title": title,
        "description": description,
        "full_location": full_location,
        "city": "WINTERTHUR",
        "country": "Switzerland",
        "meta_data": {"canonical_url": f"https://careers.example.com/jobs/{req_id}?lang=en-us"},
    }
    data.update(extra)
    return {"data": data}


def test_icims_fetches_pages_until_short_page():
    page_1 = {"jobs": [_icims_job(str(i), f"Role {i}", "WINTERTHUR, Switzerland") for i in range(100)]}
    page_2 = {"jobs": [_icims_job("200", "Product Owner", "ZURICH, Switzerland")]}
    client = _IcimsClient({1: page_1, 2: page_2})

    postings = _fetch_icims("AXA Switzerland", "careers.example.com", client)

    assert len(postings) == 101
    last = postings[-1]
    assert last.source == "icims"
    assert last.title == "Product Owner"
    assert last.company == "AXA Switzerland"
    assert last.url == "https://careers.example.com/jobs/200?lang=en-us"
    assert last.location == "ZURICH, Switzerland"
    # description HTML-stripped and entity-unescaped
    assert last.description == "Own the & roadmap"
    assert last.raw["req_id"] == "200"
    # two pages: the short second page is the last, no third call
    assert [c[1]["page"] for c in client.calls] == [1, 2]
    assert client.calls[0][0] == "https://careers.example.com/api/jobs"
    assert client.calls[0][1]["country"] == "Switzerland"
    assert client.calls[0][1]["limit"] == 100


def test_icims_stops_when_a_full_page_repeats():
    # A tenant that re-serves the last page for an out-of-range page number.
    page = {"jobs": [_icims_job(str(i), f"Role {i}", "BERN, Switzerland") for i in range(100)]}
    client = _IcimsClient({1: page, 2: page, 3: page})

    postings = _fetch_icims("AXA Switzerland", "careers.example.com", client)

    assert len(postings) == 100
    assert [c[1]["page"] for c in client.calls] == [1, 2]


def test_icims_multi_site_posting_keeps_every_site():
    job = _icims_job(
        "7",
        "Head of Technology",
        "DUBLIN, Ireland; PARIS, France; ZURICH, Switzerland",
        city="DUBLIN",
        country="Ireland",
    )
    postings = _fetch_icims("AXA", "careers.example.com", _IcimsClient({1: {"jobs": [job]}}))
    assert postings[0].location == "DUBLIN, Ireland; PARIS, France; ZURICH, Switzerland"


def test_icims_url_falls_back_to_jobs_path():
    job = _icims_job("9", "Analyst", None, city="BASEL", country="Switzerland")
    job["data"]["meta_data"] = {}
    postings = _fetch_icims("AXA", "careers.example.com", _IcimsClient({1: {"jobs": [job]}}))
    assert postings[0].url == "https://careers.example.com/jobs/9"
    # no full/short location -> assembled from city + country
    assert postings[0].location == "BASEL, Switzerland"


def test_icims_empty_board():
    assert _fetch_icims("AXA", "careers.example.com", _IcimsClient({1: {"jobs": []}})) == []


def test_icims_bad_slug_raises():
    with pytest.raises(ValueError):
        _fetch_icims("AXA", "careers.example.com/careers-home", _IcimsClient({}))


def test_icims_registered():
    assert _FETCHERS["icims"] is _fetch_icims


# --- BrassRing Talent Gateway: pre-rendered JSON on the page + token-guarded paging ---


class _BrassRingClient:
    """Fake for the search page (GET) + MatchedJobs (POST) + detail page (GET) flow.

    GETs are keyed by PageType (+ jobid for details) and answer canned HTML;
    POSTs are keyed by PageNumber and answer canned JSON.
    """

    def __init__(self, search_html, pages, details):
        self._search_html = search_html
        self._pages = pages  # PageNumber -> MatchedJobs payload
        self._details = details  # jobid -> detail page HTML
        self.posts: list = []
        self.gets: list = []

    def get(self, url, params=None, headers=None, follow_redirects=False):
        self.gets.append(dict(params))
        if params.get("PageType") == "searchResults":
            return _HtmlResp(self._search_html)
        page = self._details.get(params.get("jobid"))
        return _HtmlResp(page) if page is not None else _HtmlResp("", status=404)

    def post(self, url, json=None, headers=None):
        self.posts.append((json, headers))
        return _Resp(self._pages[json["PageNumber"]])


def _br_page(preload: dict, rft="tok-1", session="sess-1") -> str:
    import html as _html
    import json as _json

    return (
        "<html><body>"
        f'<input name="__RequestVerificationToken" type="hidden" value="{rft}" />'
        f'<input id="CookieValue" type="hidden" value="{session}" />'
        f'<input id="preLoadJSON" type="hidden" value="{_html.escape(_json.dumps(preload), quote=True)}" />'
        "</body></html>"
    )


def _br_list_job(req_id, title, location, city="Zürich", department="Group Functions"):
    return {
        "Questions": [
            {"QuestionName": "reqid", "Value": req_id},
            {"QuestionName": "siteid", "Value": "5012"},
            {"QuestionName": "jobtitle", "Value": title},
            {"QuestionName": "formtext23", "Value": location},
            {"QuestionName": "formtext2", "Value": city},
            {"QuestionName": "department", "Value": department},
            {"QuestionName": "jobdescription", "Value": "<p>Summary only</p>"},
        ],
        "Link": f"https://jobs.example.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25008&siteid=5012&PageType=JobDetails&jobid={req_id}",
    }


def _br_detail(title, sections: dict, active=True):
    questions = [{"QuestionName": "", "AnswerValue": "345580"}] + [
        {"QuestionName": label, "AnswerValue": value} for label, value in sections.items()
    ]
    return _br_page({"Jobdetails": {"Title": title, "isActive": active, "JobDetailQuestions": questions}})


_BR_SEARCH = _br_page(
    {
        "SmartSearchJSONValue": '{"KeywordCustomSolrFields":"FORMTEXT21,JobTitle","LocationCustomSolrFields":"FORMTEXT2,Location"}',
        "searchResultsResponse": {
            "JobsCount": 530,
            "Facets": {
                "Facet": [
                    {
                        "Name": "formtext2",
                        "Description": "City",
                        "Options": [
                            {"OptionName": "Zürich", "OptionValue": "Zurich", "Count": 14, "Selected": False},
                            {"OptionName": "London", "OptionValue": "London", "Count": 90, "Selected": False},
                        ],
                    },
                    {"Name": "department", "Description": "Business Divisions", "Options": [{"OptionName": "Group Functions", "OptionValue": "N1", "Count": 200, "Selected": False}]},
                ]
            },
            "Jobs": {"Job": []},
        },
    }
)


def test_brassring_scopes_by_facet_and_reads_detail_sections():
    pages = {
        1: {"JobsCount": 2, "Jobs": {"Job": [
            _br_list_job("1", "Product Owner", "Switzerland - Zürich"),
            _br_list_job("2", "Data Lead", "Switzerland - Zürich"),
        ]}},
    }
    details = {
        "1": _br_detail("Product Owner", {
            "Job Reference #": "1BR", "City": "Zürich ", "Country / State": "Switzerland - Zürich ",
            "Key responsibilities": "<p>Own the <b>roadmap</b></p>", "Your skills and experience": "• 5 years PM",
            "Disclaimer / Policy statements": "UBS is an Equal Opportunity Employer.",
            "About us": "UBS is a leading wealth manager.",
        }),
        # A German-language req labels its sections in German.
        "2": _br_detail("Data Lead", {"Stadt": "Zürich", "Land": "Schweiz - Zürich", "Aufgaben": "Lead data", "Über uns": "Bei UBS"}),
    }
    client = _BrassRingClient(_BR_SEARCH, pages, details)

    postings = _fetch_brassring("UBS", "jobs.example.com:25008:5012:Zürich", client)

    assert [p.title for p in postings] == ["Product Owner", "Data Lead"]
    p = postings[0]
    assert p.source == "brassring"
    assert p.url.endswith("PageType=JobDetails&jobid=1")
    # City leads, then the country/state field; trailing whitespace trimmed
    assert p.location == "Zürich; Switzerland - Zürich"
    assert "Key responsibilities: Own the roadmap" in p.description
    assert "Your skills and experience: • 5 years PM" in p.description
    assert "<b>" not in p.description
    # boilerplate sections dropped, and "statements" is not a state
    assert "Equal Opportunity" not in p.description and "leading wealth manager" not in p.description
    assert postings[1].location == "Zürich; Schweiz - Zürich"
    assert "Aufgaben: Lead data" in postings[1].description
    assert "Bei UBS" not in postings[1].description
    assert p.raw["department"] == "Group Functions"
    # One list POST, scoped to the Zürich option (posted under its OptionValue
    # spelling), carrying the page's anti-forgery token and session value.
    body, headers = client.posts[0]
    assert len(client.posts) == 1
    assert body["FacetFilterFields"] == {"Facet": [{"Name": "formtext2", "Options": [
        {"OptionName": "Zürich", "OptionValue": "Zurich", "Count": 14, "Selected": True}]}]}
    assert body["SiteId"] == 5012 and body["PartnerId"] == 25008
    assert body["KeywordCustomSolrFields"] == "FORMTEXT21,JobTitle"
    assert body["encryptedsessionvalue"] == "sess-1"
    assert headers["RFT"] == "tok-1"
    # search page + two detail pages
    assert [g["PageType"] for g in client.gets] == ["searchResults", "JobDetails", "JobDetails"]


def test_brassring_location_falls_back_to_list_facet_field():
    pages = {1: {"JobsCount": 1, "Jobs": {"Job": [_br_list_job("1", "PM", "Switzerland - Zürich", city="Zürich")]}}}
    details = {"1": _br_detail("PM", {"Key responsibilities": "x"})}
    postings = _fetch_brassring("UBS", "jobs.example.com:25008:5012:Zürich", _BrassRingClient(_BR_SEARCH, pages, details))
    # formtext2 is the facet the search page describes as "City"
    assert postings[0].location == "Zürich"


def test_brassring_paginates_on_the_boards_count():
    pages = {
        1: {"JobsCount": 3, "Jobs": {"Job": [_br_list_job("1", "A", "CH"), _br_list_job("2", "B", "CH")]}},
        2: {"JobsCount": 0, "Jobs": {"Job": [_br_list_job("3", "C", "CH")]}},
    }
    details = {i: _br_detail(t, {"City": "Zürich", "Key responsibilities": "x"}) for i, t in (("1", "A"), ("2", "B"), ("3", "C"))}
    client = _BrassRingClient(_BR_SEARCH, pages, details)

    postings = _fetch_brassring("UBS", "jobs.example.com:25008:5012:Zürich", client)

    assert [p.title for p in postings] == ["A", "B", "C"]
    assert [b["PageNumber"] for b, _ in client.posts] == [1, 2]


def test_brassring_skips_closed_and_missing_details():
    pages = {1: {"JobsCount": 3, "Jobs": {"Job": [
        _br_list_job("1", "Alive", "CH"), _br_list_job("2", "Closed", "CH"), _br_list_job("3", "Gone", "CH")]}}}
    details = {
        "1": _br_detail("Alive", {"City": "Zürich", "Key responsibilities": "x"}),
        "2": _br_detail("Closed", {"City": "Zürich"}, active=False),
        # no entry for 3 -> 404
    }
    postings = _fetch_brassring("UBS", "jobs.example.com:25008:5012:Zürich", _BrassRingClient(_BR_SEARCH, pages, details))
    assert [p.title for p in postings] == ["Alive"]


def test_brassring_unknown_scope_option_returns_empty(caplog):
    client = _BrassRingClient(_BR_SEARCH, {}, {})
    with caplog.at_level(logging.WARNING):
        postings = _fetch_brassring("UBS", "jobs.example.com:25008:5012:Atlantis", client)
    assert postings == []
    assert client.posts == []
    assert "Atlantis" in caplog.text


def test_brassring_refuses_large_unscoped_board():
    with pytest.raises(ValueError):
        _fetch_brassring("UBS", "jobs.example.com:25008:5012", _BrassRingClient(_BR_SEARCH, {}, {}))


def test_brassring_small_board_fetches_unscoped():
    search = _BR_SEARCH.replace("530", "1")
    pages = {1: {"JobsCount": 1, "Jobs": {"Job": [_br_list_job("1", "Only", "CH")]}}}
    details = {"1": _br_detail("Only", {"City": "Bern", "Key responsibilities": "x"})}
    client = _BrassRingClient(search, pages, details)
    postings = _fetch_brassring("Small", "jobs.example.com:1:2", client)
    assert [p.title for p in postings] == ["Only"]
    assert client.posts[0][0]["FacetFilterFields"] is None


def test_brassring_page_without_preload_raises():
    client = _BrassRingClient("<html>blocked</html>", {}, {})
    with pytest.raises(ValueError):
        _fetch_brassring("UBS", "jobs.example.com:25008:5012:Zürich", client)


def test_brassring_bad_slug_raises():
    with pytest.raises(ValueError):
        _fetch_brassring("UBS", "jobs.example.com:5012", _BrassRingClient("", {}, {}))
    with pytest.raises(ValueError):
        _fetch_brassring("UBS", "jobs.example.com:abc:5012", _BrassRingClient("", {}, {}))


def test_brassring_registered():
    assert _FETCHERS["brassring"] is _fetch_brassring


# --- Prospective career center: paged HTML list + JSON-LD on the job page ---


def _pros_item(url, title, workload, work, location):
    return (
        '<div class="platform-item"><div class="itemlist_content"><div class="section group">'
        f'<div class="itemlist_jobtitle"> <a href="{url}" target="_blank" title="{title}">{title}<span>{workload}</span></a> </div>'
        '</div><div class="section group"><div class="itemlist_text"> </div>'
        f'<div class="work"> {work} <div id="workplace"> <img src="x.svg" alt="Icon Map"> {location} </div></div>'
        "</div></div></div>"
    )


def _pros_job_page(title, description, extra=None):
    import json as _json

    posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": title, "description": description, "datePosted": "2026-09-04", "employmentType": "FULL_TIME"}
    posting.update(extra or {})
    return f'<html><head><script type="application/ld+json">{_json.dumps(posting)}</script></head><body>chrome</body></html>'


def _pros_key(offset):
    return ("https://ohws.prospective.ch/public/v1/careercenter/1000982/", (("lang", "en"), ("limit", 100), ("offset", offset)))


def test_prospective_lists_and_reads_json_ld():
    page = _pros_item("https://jobs.example.com/job-vacancies/professionals/pm/abc", "Product Manager", "80-100%", "Full-time, Half-time possible <span>Permanent</span>", "Stäfa, Schweiz, Switzerland") + _pros_item(
        "https://jobs.example.com/job-vacancies/professionals/fw/def", "Firmware Engineer", "100%", "Full-time <span>Permanent</span>", "Debrecen, Hungary, Hungary"
    )
    client = _AvatureClient(
        {
            _pros_key(0): page,
            "https://jobs.example.com/job-vacancies/professionals/pm/abc": _pros_job_page("Product Manager", "<p>Own the &amp; roadmap</p>", {"qualifications": "<ul><li>5 years</li></ul>"}),
            "https://jobs.example.com/job-vacancies/professionals/fw/def": "<html><body><h1>Firmware Engineer</h1><p>No JSON-LD here</p></body></html>",
        }
    )

    postings = _fetch_prospective("Sensirion", "1000982", client)

    assert [p.title for p in postings] == ["Product Manager", "Firmware Engineer"]
    p = postings[0]
    assert p.source == "prospective"
    assert p.url == "https://jobs.example.com/job-vacancies/professionals/pm/abc"
    assert p.location == "Stäfa, Schweiz, Switzerland"
    # workload + contract line lead, then the JSON-LD description, then the
    # qualifications block it doesn't already contain
    assert p.description.startswith("80-100% | Full-time, Half-time possible Permanent")
    assert "Own the & roadmap" in p.description and "5 years" in p.description
    assert "<p>" not in p.description
    assert p.raw["workload"] == "80-100%" and p.raw["datePosted"] == "2026-09-04"
    # a page without JSON-LD falls back to its text
    assert "No JSON-LD here" in postings[1].description
    # one short list page: no second list call
    assert len(client.calls) == 3


def test_prospective_pages_by_offset():
    full = "".join(_pros_item(f"https://jobs.example.com/j/{i}", f"Role {i}", "100%", "Full-time", "Stäfa, Switzerland") for i in range(100))
    last = _pros_item("https://jobs.example.com/j/100", "Role 100", "100%", "Full-time", "Stäfa, Switzerland")
    pages = {_pros_key(0): full, _pros_key(100): last}
    pages.update({f"https://jobs.example.com/j/{i}": _pros_job_page(f"Role {i}", "x") for i in range(101)})
    client = _AvatureClient(pages)

    postings = _fetch_prospective("Sensirion", "1000982:en", client)

    assert len(postings) == 101
    assert [c for c in client.calls if isinstance(c, tuple)] == [_pros_key(0), _pros_key(100)]


def test_prospective_skips_closed_posting():
    page = _pros_item("https://jobs.example.com/j/1", "Alive", "100%", "Full-time", "Stäfa") + _pros_item("https://jobs.example.com/j/2", "Closed", "100%", "Full-time", "Stäfa")
    client = _AvatureClient({_pros_key(0): page, "https://jobs.example.com/j/1": _pros_job_page("Alive", "x")})
    assert [p.title for p in _fetch_prospective("Sensirion", "1000982", client)] == ["Alive"]


def test_prospective_empty_center():
    assert _fetch_prospective("Sensirion", "1000982", _AvatureClient({_pros_key(0): "<html>no jobs</html>"})) == []


def test_prospective_bad_slug_raises():
    with pytest.raises(ValueError):
        _fetch_prospective("Sensirion", "sensirion", _AvatureClient({}))


def test_prospective_registered():
    assert _FETCHERS["prospective"] is _fetch_prospective


# --- Lever: workplaceType carries the remote flag the location text lacks ---


def test_lever_reads_remote_from_workplace_type():
    payload = [
        {
            "text": "Remote Role",
            "hostedUrl": "https://jobs.lever.co/acme/1",
            "descriptionPlain": "A remote role.",
            "categories": {"location": "Switzerland"},
            "workplaceType": "remote",
        },
        {
            "text": "Hybrid Role",
            "hostedUrl": "https://jobs.lever.co/acme/2",
            "descriptionPlain": "A hybrid role.",
            "categories": {"location": "Zurich"},
            "workplaceType": "hybrid",
        },
        {
            "text": "Unstated Role",
            "hostedUrl": "https://jobs.lever.co/acme/3",
            "descriptionPlain": "A role.",
            "categories": {"location": "Zurich"},
        },
    ]
    postings = _fetch_lever("Acme", "acme", _JsonClient(payload))

    assert [p.remote for p in postings] == [True, False, None]
    # The remote one's location says nothing but the country — which is exactly
    # why the board's own flag has to be carried through.
    assert postings[0].location == "Switzerland"


# --- Google job feed ---


def _google_job(*, jobid, title, employer="Google", locations, remote="onsite"):
    where = "".join(
        f"<location><city>{city}</city><state /><country>{country}</country></location>"
        for city, country in locations
    )
    return (
        f"<job><jobid>{jobid}</jobid><title>{title}</title>"
        f"<description>&lt;h3&gt;About&lt;/h3&gt;&lt;p&gt;Build {title}.&lt;/p&gt;</description>"
        f"<url>https://careers.google.com/jobs/results/{jobid}-role/</url>"
        f"<jobtype>FULL_TIME</jobtype><employer>{employer}</employer>"
        f"<remote>{remote}</remote><locations>{where}</locations></job>"
    )


def _google_feed(*jobs):
    return f'<?xml version="1.0" encoding="UTF-8"?><jobs>{"".join(jobs)}</jobs>'.encode()


def test_google_keeps_swiss_roles_of_the_configured_employers():
    feed = _google_feed(
        _google_job(jobid="1", title="Product Manager", locations=[("Zürich", "Switzerland")]),
        _google_job(jobid="2", title="Research Engineer", employer="DeepMind",
                    locations=[("London", "UK"), ("Zürich", "Switzerland")]),
        _google_job(jobid="3", title="Sales Lead", locations=[("Munich", "Germany")]),
        _google_job(jobid="4", title="Creator Partner", employer="YouTube",
                    locations=[("Zürich", "Switzerland")]),
        _google_job(jobid="5", title="Solutions Engineer", locations=[("Zürich", "Switzerland")],
                    remote="remote"),
    )
    client = _PersonioClient(feed)

    postings = _fetch_google("Google", "Google|DeepMind", client)

    assert client.url == "https://www.google.com/about/careers/applications/jobs/feed.xml"
    # Germany-only and the unconfigured YouTube role are left out.
    assert [p.title for p in postings] == ["Product Manager", "Research Engineer", "Solutions Engineer"]
    pm, multi, remote = postings
    assert pm.source == "google" and pm.company == "Google"
    assert pm.url == "https://careers.google.com/jobs/results/1-role/"
    assert "Build Product Manager." in pm.description and "<p>" not in pm.description
    assert pm.location == "Zürich, Switzerland"
    assert pm.remote is False and remote.remote is True
    assert pm.raw["jobid"] == "1" and multi.raw["employer"] == "DeepMind"
    # Every site of a multi-location role stays visible to the location filter.
    assert multi.location == "London, UK; Zürich, Switzerland"


def test_google_employer_match_ignores_case_and_spaces():
    feed = _google_feed(_google_job(jobid="1", title="PM", employer="DeepMind",
                                    locations=[("Zürich", "Switzerland")]))
    assert len(_fetch_google("DeepMind", " deepmind ", _PersonioClient(feed))) == 1


def test_google_bad_slug_raises():
    with pytest.raises(ValueError):
        _fetch_google("Google", " | ", _PersonioClient(_google_feed()))


def test_google_unreadable_feed_skips_the_company_not_the_source():
    # ValueError is what CompanyPagesSource catches per company; a ParseError
    # would escape it and end every other board's fetch too.
    with pytest.raises(ValueError, match="not readable XML"):
        _fetch_google("Google", "Google", _PersonioClient(b"<html>Service unavailable"))


# --- onlyfy ------------------------------------------------------------------

_ONLYFY_BASE = "https://hexagon-robotics.onlyfy.jobs"


def _onlyfy_list(cards, first, last, total):
    body = "".join(
        f'<li><a class="group flex" data-testid="job-card" aria-label="{title}" '
        f'href="/en/job/{job_id}?career_page_search=country_code%3Dch">'
        f'<div><h3 class="x" data-testid="job-title">{title}</h3></div>'
        f'<div class="y" data-testid="job-more-info">{info}</div></a></li>'
        for job_id, title, info in cards
    )
    count = (
        f'<span data-testid="pagination-items-count"><b>{first}-{last}</b> out of <b>{total} jobs</b></span>'
    )
    return f"<html><body><ul data-testid=\"jobs-list\">{body}</ul>{count}</body></html>"


def _onlyfy_detail(text):
    # Both halves of a real page that must not reach the description: the
    # scripts, and the hidden StepStone copy of the ad (a nested div block).
    return (
        "<html><head><title>t</title></head><body>"
        "<script>window.__ignored = 1</script>"
        f'<div class="job-ad-component"><h1>Title</h1><p>{text}</p></div>'
        '<div class="step-stone-job-ad" style="display:none;"><div><p>DUPLICATE COPY</p></div>'
        "<div>still hidden</div></div>"
        "<p>Visible after</p></body></html>"
    )


def _onlyfy_key(page):
    return (f"{_ONLYFY_BASE}/en", (("country", "ch"), ("page", page)))


def _onlyfy_detail_key(job_id):
    return (f"{_ONLYFY_BASE}/job/show/{job_id}/full", (("lang", "en"), ("mode", "candidate")))


@pytest.fixture
def _no_crawl_delay(monkeypatch):
    import jobradar.search.sources.company_pages as cp

    sleeps = []
    monkeypatch.setattr(cp.time, "sleep", sleeps.append)
    return sleeps


def test_onlyfy_pages_to_the_boards_count_and_reads_each_posting(_no_crawl_delay):
    client = _AvatureClient(
        {
            _onlyfy_key(1): _onlyfy_list(
                [("aaa1", "Robotics Engineer", "Zürich | Full-time employee | 16.09.2026"),
                 ("bbb2", "Test Engineer", "Zürich | Full-time employee | 10.09.2026")],
                1, 2, 3,
            ),
            _onlyfy_key(2): _onlyfy_list([("ccc3", "Intern", "Zürich | Intern | 01.09.2026")], 3, 3, 3),
            _onlyfy_detail_key("aaa1"): _onlyfy_detail("Build humanoids &amp; more."),
            _onlyfy_detail_key("bbb2"): _onlyfy_detail("Own the test strategy."),
            _onlyfy_detail_key("ccc3"): _onlyfy_detail("Learn things."),
        }
    )

    postings = _fetch_onlyfy("Hexagon Robotics", "hexagon-robotics", client)

    assert [p.title for p in postings] == ["Robotics Engineer", "Test Engineer", "Intern"]
    first = postings[0]
    assert first.source == "onlyfy"
    assert first.url == f"{_ONLYFY_BASE}/en/job/aaa1"
    assert first.location == "Zürich"
    assert first.raw == {"employment_type": "Full-time employee", "posted": "16.09.2026"}
    assert "Build humanoids & more." in first.description
    assert "Visible after" in first.description
    assert "DUPLICATE COPY" not in first.description and "still hidden" not in first.description
    assert "__ignored" not in first.description
    # the count (3) is reached after page 2: no third list request
    assert _onlyfy_key(3) not in client.calls
    # robots.txt Crawl-delay: one pause before every request but the first
    assert len(_no_crawl_delay) == len(client.calls) - 1
    assert set(_no_crawl_delay) == {1.0}


def test_onlyfy_stops_when_a_page_brings_nothing_new(_no_crawl_delay):
    # No count on the page (markup changed): stop on a page of known jobs
    # rather than paging forever.
    page = _onlyfy_list([("aaa1", "A", "Zürich | Full-time employee | 16.09.2026")], 1, 1, 1)
    page = page.replace('data-testid="pagination-items-count"', 'data-testid="gone"')
    client = _AvatureClient(
        {_onlyfy_key(1): page, _onlyfy_key(2): page, _onlyfy_detail_key("aaa1"): _onlyfy_detail("x")}
    )
    assert [p.title for p in _fetch_onlyfy("H", "hexagon-robotics", client)] == ["A"]


def test_onlyfy_skips_a_closed_posting(_no_crawl_delay):
    client = _AvatureClient(
        {
            _onlyfy_key(1): _onlyfy_list(
                [("aaa1", "Open", "Zürich | Full-time employee | 16.09.2026"),
                 ("bbb2", "Closed", "Zürich | Full-time employee | 16.09.2026")],
                1, 2, 2,
            ),
            _onlyfy_detail_key("aaa1"): _onlyfy_detail("x"),
            # no entry for bbb2 -> fake answers 404
        }
    )
    assert [p.title for p in _fetch_onlyfy("H", "hexagon-robotics", client)] == ["Open"]


def test_onlyfy_empty_board(_no_crawl_delay):
    client = _AvatureClient({_onlyfy_key(1): "<html><body>No jobs</body></html>"})
    assert _fetch_onlyfy("H", "hexagon-robotics", client) == []
    assert len(client.calls) == 1


@pytest.mark.parametrize("slug", ["https://hexagon-robotics.onlyfy.jobs", "hexagon-robotics.onlyfy.jobs", "", "Hexagon Robotics"])
def test_onlyfy_slug_must_be_the_subdomain(slug):
    with pytest.raises(ValueError, match="subdomain"):
        _fetch_onlyfy("H", slug, _AvatureClient({}))
