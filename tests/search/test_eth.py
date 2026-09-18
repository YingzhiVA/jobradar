from jobradar.search.sources.eth import _extract_csrf, _location_from_details, _parse_listing

# A trimmed but faithful copy of the real jobs.ethz.ch listing markup.
_LISTING = """
<ul id="w1" class="job-ad__wrapper">
<div data-key="11781">
<li class="job-ad__item__wrapper" role="article" tabindex="0">
    <a href="/job/view/JOPG_ethz_QFD5UY3P2lUjL0BcCR" class="job-ad__item__link" target="_blank">
        <div>
            <h3 class="job-ad__item__title">
                Head of AI Center Operations            </h3>
            <div class="job-ad__item__details">
                80%-100%, Zurich, fixed-term            </div>
            <div class="job-ad__item__company">
                18.06.2026                 |                 ETH AI Center            </div>
        </div>
    </a>
</li>
</div>
</ul>
"""


def test_extract_csrf():
    html = '<input type="hidden" name="_csrf-frontend" value="abc123token">'
    assert _extract_csrf(html) == "abc123token"


def test_extract_csrf_missing():
    assert _extract_csrf("<html>no token</html>") == ""


def test_location_from_details():
    assert _location_from_details("80%-100%, Zurich, fixed-term") == "Zurich"
    assert _location_from_details("Zurich") == "Zurich"
    assert _location_from_details("") is None


def test_parse_listing_extracts_fields():
    postings = _parse_listing(_LISTING)
    assert len(postings) == 1
    p = postings[0]
    assert p.source == "eth_jobs"
    assert p.url == "https://jobs.ethz.ch/job/view/JOPG_ethz_QFD5UY3P2lUjL0BcCR"
    assert p.title == "Head of AI Center Operations"
    assert p.company == "ETH Zürich"
    assert p.location == "Zurich"
    assert "ETH AI Center" in p.description  # department captured
    assert p.raw["department"] == "ETH AI Center"


def test_parse_listing_empty():
    assert _parse_listing("<html>no jobs</html>") == []
