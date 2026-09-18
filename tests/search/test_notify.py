from jobradar.search.notify import (
    _smtp_config,
    build_html,
    build_message,
    compose_email,
    should_send,
)


def test_smtp_config_treats_empty_optional_secrets_as_defaults(monkeypatch):
    # GitHub passes UNSET secrets as "" (key present). int("") must not crash;
    # empty host/from must fall back to defaults.
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASS", "pw")
    monkeypatch.setenv("EMAIL_TO", "me@gmail.com")
    monkeypatch.setenv("SMTP_PORT", "")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("EMAIL_FROM", "")
    cfg = _smtp_config()
    assert cfg["port"] == 587
    assert cfg["host"] == "smtp.gmail.com"
    assert cfg["from"] == "me@gmail.com"


def test_smtp_config_none_when_required_missing(monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    assert _smtp_config() is None


def test_email_to_defaults_to_smtp_user(monkeypatch):
    # Self-notification: only SMTP_USER + SMTP_PASS needed; EMAIL_TO defaults.
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASS", "pw")
    monkeypatch.delenv("EMAIL_TO", raising=False)
    cfg = _smtp_config()
    assert cfg is not None
    assert cfg["to"] == "me@gmail.com"


def test_should_send_true_when_matches():
    assert should_send({"matches": [{"title": "X"}]}) is True


def test_should_send_true_when_degraded():
    assert should_send({"matches": [], "degraded": True}) is True


def test_should_send_false_on_quiet_clean_day():
    assert should_send({"matches": [], "degraded": False}) is False


def test_should_send_always_override(monkeypatch):
    monkeypatch.setenv("JOBRADAR_NOTIFY_ALWAYS", "1")
    assert should_send({"matches": [], "degraded": False}) is True


def test_build_message_includes_headline_and_matches():
    summary = {
        "headline": "1 strong match: PM @ Acme",
        "date": "2026-06-17",
        "matches": [
            {
                "tier": "best",
                "title": "PM",
                "company": "Acme",
                "url": "https://a/1",
                "location": "Zurich, Switzerland",
                "skill_score": 85,
                "interest_score": 78,
                "writeup": "Strong fit because...",
            }
        ],
    }
    subject, body = build_message(summary)
    assert subject == "jobradar — 1 strong match: PM @ Acme"
    assert "[BEST] PM @ Acme" in body
    assert "https://a/1" in body
    assert "Zurich, Switzerland" in body
    assert "85/100" in body
    assert "78/100" in body
    assert "Strong fit because..." in body
    assert "Run date: 2026-06-17" in body


def test_build_message_notes_degraded():
    summary = {"headline": "No matches — run incomplete", "matches": [], "degraded": True, "failed_sources": ["web_search"]}
    _subject, body = build_message(summary)
    assert "Degraded run" in body
    assert "web_search" in body


def test_build_message_handles_missing_fields():
    subject, body = build_message({})
    assert subject == "jobradar — jobradar"
    assert isinstance(body, str)


def test_build_message_writeup_omitted_when_absent():
    # Matches without writeup (e.g. old latest.json) degrade gracefully.
    summary = {
        "headline": "1 okay match",
        "matches": [{"tier": "okay", "title": "PM", "company": "Co", "url": "https://x"}],
    }
    _subject, body = build_message(summary)
    assert "[OKAY] PM @ Co" in body
    assert "https://x" in body


def test_build_message_multiple_matches_separated():
    summary = {
        "headline": "2 okay matches",
        "matches": [
            {"tier": "okay", "title": "PM", "company": "A", "url": "https://a", "writeup": "Writeup A"},
            {"tier": "okay", "title": "APM", "company": "B", "url": "https://b", "writeup": "Writeup B"},
        ],
    }
    _subject, body = build_message(summary)
    assert "---" in body
    assert "Writeup A" in body
    assert "Writeup B" in body


def test_build_html_links_urls_and_keeps_line_breaks():
    body = "Headline\n\n[BEST] PM @ Acme\nLink: https://jobs.example.com/pm?id=1\nSkill: 90/100"
    out = build_html("jobradar — Headline", body)
    assert '<a href="https://jobs.example.com/pm?id=1">' in out
    assert "<br" in out  # one fact per line survives the rendering
    assert "<title>jobradar — Headline</title>" in out


def test_build_html_escapes_markup_in_content():
    out = build_html("<s>", "Title <script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_compose_email_has_plain_and_html_parts():
    config = {"from": "me@gmail.com", "to": "me@gmail.com"}
    msg = compose_email("subj", "line one\nhttps://example.com/x", config)
    assert msg["Subject"] == "subj"
    plain = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    assert plain is not None and "line one" in plain.get_content()
    assert html_part is not None and '<a href="https://example.com/x">' in html_part.get_content()
