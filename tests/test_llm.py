"""Model selection: defaults, environment overrides, and when they are read."""

import pytest

from jobradar import matching
from jobradar.llm import MODEL_DEFAULTS, model_for, web_search_tool_type
from jobradar.models import Posting


def test_default_when_unset(monkeypatch):
    monkeypatch.delenv("JOBRADAR_SCORING_MODEL", raising=False)
    assert model_for("JOBRADAR_SCORING_MODEL") == "claude-haiku-4-5"


def test_override_when_set(monkeypatch):
    monkeypatch.setenv("JOBRADAR_SCORING_MODEL", "claude-sonnet-4-6")
    assert model_for("JOBRADAR_SCORING_MODEL") == "claude-sonnet-4-6"


def test_blank_override_falls_back(monkeypatch):
    # An unset CI secret arrives as "", which must not become the model id.
    monkeypatch.setenv("JOBRADAR_WRITEUP_MODEL", "")
    assert model_for("JOBRADAR_WRITEUP_MODEL") == MODEL_DEFAULTS["JOBRADAR_WRITEUP_MODEL"]


def test_unknown_variable_is_a_programming_error():
    with pytest.raises(KeyError):
        model_for("JOBRADAR_NO_SUCH_MODEL")


def test_web_search_tool_type_follows_the_model():
    assert web_search_tool_type("claude-haiku-4-5") == "web_search_20250305"
    assert web_search_tool_type("claude-sonnet-4-6") == "web_search_20260209"


class _Messages:
    def __init__(self):
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("stop here")  # score_posting logs this and returns None


class _Client:
    def __init__(self):
        self.messages = _Messages()


def test_scoring_reads_the_override_at_call_time(monkeypatch):
    # .env is loaded after imports, so the override must be read per call, not
    # once at import — the bug this module replaced.
    client = _Client()
    posting = Posting(id="p1", source="fixture", url="https://x", title="t", company="c", description="d")
    monkeypatch.setenv("JOBRADAR_SCORING_MODEL", "claude-sonnet-4-6")
    matching.score_posting(posting, "profile", client)
    monkeypatch.delenv("JOBRADAR_SCORING_MODEL")
    matching.score_posting(posting, "profile", client)
    assert [c["model"] for c in client.messages.calls] == ["claude-sonnet-4-6", "claude-haiku-4-5"]
