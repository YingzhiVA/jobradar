from jobradar.apply.fetch import FetchOutcome, MIN_USABLE_CHARS, html_to_text


def test_html_to_text_strips_script_and_style():
    html = """
    <html><head><title>t</title><style>.x{color:red}</style></head>
    <body>
      <script>var x = "hidden";</script>
      <h1>Product Manager</h1>
      <div>We are <b>hiring</b>.</div>
      <ul><li>Own the roadmap</li><li>Ship things</li></ul>
      <noscript>enable js</noscript>
    </body></html>
    """
    text = html_to_text(html)
    assert "Product Manager" in text
    assert "We are hiring ." in text or "We are hiring." in text.replace(" .", ".")
    assert "Own the roadmap" in text
    assert "hidden" not in text
    assert "color:red" not in text
    assert "enable js" not in text


def test_html_to_text_collapses_blank_lines():
    text = html_to_text("<div>a</div><div></div><div></div><div>b</div>")
    assert "\n\n\n" not in text


def test_fetch_outcome_usable_threshold():
    assert not FetchOutcome("u", text="short").usable
    assert FetchOutcome("u", text="x" * MIN_USABLE_CHARS).usable
    assert not FetchOutcome("u", error="boom").usable
