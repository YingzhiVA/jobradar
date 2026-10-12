from jobradar.apply.extract import JDExtract, extract_jd


def _jd() -> JDExtract:
    return JDExtract(is_job_posting=True, title="PM", company="Acme", description_markdown="x" * 300)


def test_extraction_defaults_to_haiku_5_5(fake_client, monkeypatch):
    monkeypatch.delenv("JOBRADAR_APPLY_HELPER_MODEL", raising=False)
    client = fake_client(["chunk"], _jd())
    extract_jd("page text", client)
    assert client.messages.kwargs["model"] == "claude-haiku-5-5"


def test_extraction_leaves_room_for_thinking(fake_client):
    """Haiku 5.5's thinking counts against max_tokens, and a posting near the
    30,000-character input cut reproduces to ~8k tokens on its own; at the old
    8192 a long posting would be cut off mid-description."""
    client = fake_client(["chunk"], _jd())
    extract_jd("page text", client)
    assert client.messages.kwargs["max_tokens"] >= 16000
