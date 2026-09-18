import httpx
import pytest

from jobradar.evidence import (
    Note,
    _in_scope,
    _normalize_url,
    _root_path,
    _slug_for,
    crawl,
    parse_page,
    sync,
)

# One fixture exercising the whole semantic subset the converter claims to
# handle: the standfirst slots, a callout, a section kicker, a table, a code
# block with its language label, a figure whose only prose is its aria-label,
# a definition list, a takeaways list, and the two things that must be dropped
# (the byline and the next/previous card).
ARTICLE = """<!doctype html>
<html><head>
<meta property="og:type" content="article">
<meta name="description" content="Social card blurb.">
<title>Say It Once</title>
</head><body>
<div><a href="../">&larr; The series</a><a href="../../">site root</a></div>
<header class="hero">
  <span class="eyebrow">Field Notes &middot; No. 02 &middot; Single Source</span>
  <h1>Say It Once</h1>
  <p class="dek">One home per fact.</p>
  <div class="meta"><span>Field note</span><span>&middot;</span><span>~6 min read</span></div>
  <figure aria-label="Three copies of a fact drift apart.">
    <canvas id="c" width="100" height="50"></canvas>
    <div class="tags"><span class="tag">drift</span><span class="tag">stable</span></div>
    <figcaption>The same value in three homes.</figcaption>
  </figure>
</header>
<main><div class="col">
  <div class="callout"><span class="tag">Scope</span><p>Interpretation, not prediction.</p></div>
  <p class="lead">Facts live in <strong>three</strong> kinds of <em>places</em>.</p>
  <hr class="tick" />
  <span class="snum">01 &middot; The failure mode</span>
  <h2>Two copies are a contradiction</h2>
  <blockquote>They wait for one edit.</blockquote>
  <p>Use the <code>edit test</code>, or read <a href="https://93.184.216.34/x">the paper</a>.</p>
  <table>
    <caption>Which home a fact belongs in.</caption>
    <thead><tr><th>Home</th><th>What belongs there</th></tr></thead>
    <tbody><tr><td>code</td><td>Anything computed.</td></tr></tbody>
  </table>
  <div class="code"><span class="lang">python</span><pre><code>x = 1
if x:
    print(x)</code></pre></div>
  <ul class="defs">
    <li><span class="k">Row order</span><span class="v">Total impact.</span></li>
  </ul>
  <ol class="takes">
    <li><div class="body"><h4>Every fact gets one home</h4><p>Code or prompt.</p></div></li>
    <li><div class="body"><h4>Reference, don't restate</h4><p>Point at the source.</p></div></li>
  </ol>
  <footer><div class="col">
    <a class="nextup" href="../encoder-first/"><span class="ttl">&larr; Previously</span></a>
    <p>The least heroic habit in the series.</p>
    <p class="sig">Field notes &middot; details generalized</p>
  </div></footer>
</div></main>
</body></html>
"""

INDEX = """<!doctype html>
<html><head>
<meta property="og:type" content="website">
<title>Field Notes</title>
</head><body>
<main><h1>Field notes</h1>
<a href="/notes/say-it-once/">Read it</a>
<a href="/about/">About me</a>
<a href="https://1.1.1.1/">Elsewhere</a>
</main></body></html>
"""

# A public IP literal keeps the real SSRF guard in apply/fetch.py satisfied
# without any DNS lookup, so these tests exercise it while staying offline -
# same convention as tests/search/test_liveness.py.
URL = "https://8.8.8.8/notes/say-it-once/"


@pytest.fixture
def note() -> Note:
    return parse_page(ARTICLE, URL, "/notes/")


def test_article_is_detected_and_slugged(note):
    assert note.is_article
    assert note.slug == "say-it-once"
    assert note.title == "Say It Once"


def test_index_page_is_not_an_article():
    assert not parse_page(INDEX, "https://8.8.8.8/notes/", "/notes/").is_article


def test_standfirst_goes_to_the_header_not_the_body(note):
    rendered = note.render()
    assert "- Source: https://8.8.8.8/notes/say-it-once/" in rendered
    assert "- Series: Field Notes · No. 02 · Single Source" in rendered
    # The meta line's spans butt together in the source; they must not merge.
    assert "- Published as: Field note · ~6 min read" in rendered
    assert "> One home per fact." in rendered
    # The <h1> is the document title, so it must not also appear as a body
    # heading - that duplication was the first bug this converter had.
    assert rendered.count("Say It Once") == 1


def test_site_chrome_before_the_article_is_skipped(note):
    assert "The series" not in note.body
    assert "site root" not in note.body


def test_byline_and_navigation_card_are_dropped(note):
    assert "Previously" not in note.body
    assert "Field notes · details generalized" not in note.body
    assert "The least heroic habit in the series." in note.body


def test_prose_structure_survives(note):
    assert "**01 · The failure mode**" in note.body
    assert "## Two copies are a contradiction" in note.body
    assert "> They wait for one edit." in note.body
    assert "Facts live in **three** kinds of *places*." in note.body
    assert "`edit test`" in note.body
    assert "[the paper](https://93.184.216.34/x)" in note.body


def test_callout_becomes_a_labelled_aside(note):
    assert "> **Scope** - Interpretation, not prediction." in note.body


def test_table_renders_with_caption(note):
    assert "*Which home a fact belongs in.*" in note.body
    assert "| Home | What belongs there |" in note.body
    assert "| --- | --- |" in note.body
    assert "| code | Anything computed. |" in note.body


def test_code_block_keeps_language_and_indentation(note):
    assert "```python\nx = 1\nif x:\n    print(x)\n```" in note.body


def test_figure_keeps_its_accessible_description(note):
    # The figure is drawn in <canvas>, so the aria-label is its only prose.
    assert "> Figure: The same value in three homes." in note.body
    assert "> Figure: Three copies of a fact drift apart." in note.body


def test_figure_legend_chips_are_not_prose(note):
    # Legend labels sit loose in the <figure> beside the canvas they annotate.
    assert "drift stable" not in note.body


def test_definition_and_takeaway_lists_are_tight(note):
    assert "- **Row order** - Total impact." in note.body
    assert "1. **Every fact gets one home** - Code or prompt." in note.body
    assert "2. **Reference, don't restate** - Point at the source." in note.body
    # Consecutive list items get one newline, not a blank line between them.
    assert "1. **Every fact gets one home** - Code or prompt.\n2. " in note.body


def test_slug_flattens_the_path_below_the_crawl_root():
    assert _slug_for("https://8.8.8.8/notes/a/b/", "/notes/") == "a-b"
    assert _slug_for("https://8.8.8.8/notes/", "/notes/") == "index"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://8.8.8.8/n/index.html", "https://8.8.8.8/n/"),
        ("https://8.8.8.8/n/#top", "https://8.8.8.8/n/"),
        ("https://8.8.8.8/n/?utm=x", "https://8.8.8.8/n/"),
    ],
)
def test_urls_reaching_one_page_normalize_together(url, expected):
    assert _normalize_url(url) == expected


def _pages() -> dict[str, str]:
    """A complete site: every link the fixtures carry resolves, so the crawl
    finishes clean and --prune is allowed to run."""
    return {
        "https://8.8.8.8/notes/": INDEX,
        "https://8.8.8.8/notes/say-it-once/": ARTICLE,
        # ARTICLE's next/previous card links here; serving it keeps the crawl
        # error-free, which is what --prune requires.
        "https://8.8.8.8/notes/encoder-first/": INDEX,
    }


def _client(pages: dict[str, str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, html=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_crawl_follows_indexes_but_only_returns_articles():
    pages = _pages()
    result = crawl("https://8.8.8.8/notes/", client=_client(pages))
    assert [n.slug for n in result.notes] == ["say-it-once"]
    assert result.complete


def test_crawl_stays_under_the_starting_path_and_origin():
    # INDEX links to /about/ and to another host; neither may be fetched.
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        body = {"https://8.8.8.8/notes/": INDEX}.get(str(request.url))
        return httpx.Response(200, html=body) if body else httpx.Response(404)

    crawl("https://8.8.8.8/notes/", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "https://8.8.8.8/about/" not in fetched
    assert "https://1.1.1.1/" not in fetched


def test_sync_writes_once_and_leaves_unchanged_files_alone(tmp_path):
    pages = _pages()
    written, pruned = sync("https://8.8.8.8/notes/", tmp_path, client=_client(pages))
    assert [n.slug for n in written] == ["say-it-once"]
    assert (tmp_path / "say-it-once.md").read_text(encoding="utf-8").startswith("# Say It Once")
    assert pruned == []

    stamp = (tmp_path / "say-it-once.md").stat().st_mtime_ns
    written, _ = sync("https://8.8.8.8/notes/", tmp_path, client=_client(pages))
    assert written == []
    assert (tmp_path / "say-it-once.md").stat().st_mtime_ns == stamp


def test_sync_prune_removes_orphans_but_never_the_readme(tmp_path):
    (tmp_path / "README.md").write_text("keep me", encoding="utf-8")
    (tmp_path / "deleted-note.md").write_text("stale", encoding="utf-8")
    pages = _pages()
    _, pruned = sync("https://8.8.8.8/notes/", tmp_path, prune=True, client=_client(pages))
    assert [p.name for p in pruned] == ["deleted-note.md"]
    assert (tmp_path / "README.md").exists()
    assert not (tmp_path / "deleted-note.md").exists()


def test_sync_dry_run_touches_nothing(tmp_path):
    pages = _pages()
    written, _ = sync("https://8.8.8.8/notes/", tmp_path, dry_run=True, client=_client(pages))
    assert [n.slug for n in written] == ["say-it-once"]
    assert list(tmp_path.iterdir()) == []


# --- regressions found in review -------------------------------------------


def test_root_path_survives_a_site_url_without_a_trailing_slash():
    # "/notes" used to be read as a filename, leaving root_path "/" - i.e. the
    # whole domain in scope.
    assert _root_path("https://8.8.8.8/notes") == "/notes/"
    assert _root_path("https://8.8.8.8/notes/") == "/notes/"
    assert _root_path("https://8.8.8.8/notes/index.html") == "/notes/"
    assert _root_path("https://8.8.8.8") == "/"


def test_in_scope_rejects_other_origins_and_partial_segment_matches():
    origin = __import__("urllib.parse", fromlist=["urlparse"]).urlparse("https://8.8.8.8/notes/")
    assert _in_scope("https://8.8.8.8/notes/a/", origin, "/notes/")
    assert not _in_scope("https://8.8.8.8/notes-archive/a/", origin, "/notes/")
    assert not _in_scope("https://8.8.8.8/about/", origin, "/notes/")
    assert not _in_scope("https://1.1.1.1/notes/a/", origin, "/notes/")
    assert not _in_scope("http://8.8.8.8/notes/a/", origin, "/notes/")


def test_crawl_without_trailing_slash_stays_in_the_section():
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        body = {"https://8.8.8.8/notes": INDEX}.get(str(request.url))
        return httpx.Response(200, html=body) if body else httpx.Response(404)

    crawl("https://8.8.8.8/notes", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "https://8.8.8.8/about/" not in fetched


def test_a_redirect_out_of_scope_is_not_ingested():
    # An in-scope URL that 302s to another host must not be written out as the
    # candidate's own evidence.
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://8.8.8.8/notes/":
            return httpx.Response(200, html=INDEX)
        if url == "https://8.8.8.8/notes/say-it-once/":
            return httpx.Response(302, headers={"Location": "https://1.1.1.1/elsewhere/"})
        if url == "https://1.1.1.1/elsewhere/":
            return httpx.Response(200, html=ARTICLE)
        return httpx.Response(404)

    result = crawl("https://8.8.8.8/notes/", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert result.notes == []


def test_a_page_reached_twice_via_a_redirect_is_ingested_once():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://8.8.8.8/notes/":
            # Links to the note both bare and with its trailing slash.
            return httpx.Response(200, html=(
                '<html><head><meta property="og:type" content="website"></head><body><main>'
                '<a href="/notes/say-it-once">bare</a>'
                '<a href="/notes/say-it-once/">slashed</a>'
                "</main></body></html>"
            ))
        if url == "https://8.8.8.8/notes/say-it-once":
            return httpx.Response(301, headers={"Location": "https://8.8.8.8/notes/say-it-once/"})
        if url == "https://8.8.8.8/notes/say-it-once/":
            return httpx.Response(200, html=ARTICLE)
        return httpx.Response(404)

    result = crawl("https://8.8.8.8/notes/", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert [n.slug for n in result.notes] == ["say-it-once"]


def test_prune_is_skipped_when_a_page_failed_to_fetch(tmp_path):
    # Absent from this run != removed from the site. A 503 on one note must not
    # cost that note its evidence file.
    (tmp_path / "encoder-first.md").write_text("still live on the site", encoding="utf-8")
    pages = _pages()
    del pages["https://8.8.8.8/notes/encoder-first/"]  # now 404s

    _, pruned = sync("https://8.8.8.8/notes/", tmp_path, prune=True, client=_client(pages))
    assert pruned == []
    assert (tmp_path / "encoder-first.md").exists()


def test_truncated_pages_are_not_ingested_as_whole_articles(monkeypatch):
    # A body cut off at the byte cap would otherwise be written out as if it
    # were the complete note.
    from jobradar.apply import fetch as fetch_mod

    monkeypatch.setattr(fetch_mod, "_MAX_BYTES", 10)
    result = crawl("https://8.8.8.8/notes/", client=_client(_pages()))
    assert result.notes == []
    assert not result.complete


def test_colliding_slugs_are_disambiguated_rather_than_overwritten():
    # /a/b/ and /a-b/ both flatten to "a-b"; neither may silently win.
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://8.8.8.8/notes/":
            return httpx.Response(200, html=(
                '<html><head><meta property="og:type" content="website"></head><body><main>'
                '<a href="/notes/a/b/">one</a><a href="/notes/a-b/">two</a>'
                "</main></body></html>"
            ))
        if url in ("https://8.8.8.8/notes/a/b/", "https://8.8.8.8/notes/a-b/"):
            return httpx.Response(200, html=ARTICLE)
        return httpx.Response(404)

    result = crawl("https://8.8.8.8/notes/", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert sorted(n.slug for n in result.notes) == ["a-b", "a-b-2"]


def _body(fragment: str) -> str:
    return parse_page(
        '<html><head><meta property="og:type" content="article"></head>'
        f"<body><main>{fragment}</main></body></html>",
        URL,
        "/notes/",
    ).body


def test_a_nested_list_keeps_its_parent_bullet():
    # The parent's text used to flush as a stray paragraph, losing its marker.
    body = _body("<ul><li>Item one<ul><li>Nested A</li></ul></li><li>Item two</li></ul>")
    assert body == "- Item one\n  - Nested A\n- Item two"


def test_a_blockquote_of_several_paragraphs_keeps_its_marker():
    body = _body("<blockquote><p>First para.</p><p>Second para.</p></blockquote><p>After.</p>")
    assert body == "> First para. Second para.\n\nAfter."


def test_a_self_closed_anchor_does_not_corrupt_later_links():
    # handle_startendtag used to run only the open half, leaving a dangling "[".
    body = _body('<p>Before <a id="x" href="https://93.184.216.34/"/> after '
                 '<a href="https://93.184.216.34/r">real link</a> here.</p>')
    assert "[real link](https://93.184.216.34/r)" in body
    assert body.count("[") == 1


def test_an_anchor_without_href_contributes_nothing():
    body = _body('<p>See the diagram<a id="fig-1"></a> below.</p>')
    assert body == "See the diagram below."


def test_sibling_divs_do_not_run_together():
    # The .eq-row equation rows used to collapse onto one line.
    body = _body('<div class="eq">\n<div class="eq-row">\n<span>a</span>\n<span>=</span>\n'
                 '<span>b</span>\n</div>\n<div class="eq-row">\n<span>c</span>\n</div>\n'
                 '<div class="eq-note">A note.</div>\n</div>')
    assert body == "a = b\n\nc\n\nA note."


def test_a_label_value_grid_stays_on_one_line_per_pair():
    # .readout pairs are the shape a .callout has, without the aside framing.
    body = _body('<div class="readouts">\n<div class="readout">\n'
                 '<div class="lbl">Decision stability</div>\n'
                 '<div class="val"><span>40%</span><span>&rarr;</span><span>100%</span></div>\n'
                 '</div>\n</div>')
    assert body == "**Decision stability** - 40%→100%"
