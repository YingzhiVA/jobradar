"""--respect-schedule must stop the run before it costs anything.

The gate exists for a machine-local cron that fires daily against a slower
configured cadence; if it let the run reach the Anthropic client, a skipped day
would still authenticate, fetch and (worse) overwrite the day's report.
"""

import anthropic
import pytest

from jobradar.search import main as search_main


@pytest.fixture
def no_client(monkeypatch):
    """Make building an Anthropic client an outright failure, so any attempt to
    get past the gate shows up as a test error rather than a network call.
    """

    def explode(*args, **kwargs):
        raise AssertionError("the run continued past the schedule gate")

    monkeypatch.setattr(anthropic, "Anthropic", explode)


def _configure(monkeypatch, tmp_path, yaml_text, runs_dates=()):
    import json

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "search.yaml").write_text(yaml_text, encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    if runs_dates:
        (reports / "runs.jsonl").write_text(
            "".join(json.dumps({"date": d}) + "\n" for d in runs_dates), encoding="utf-8"
        )
    monkeypatch.setattr(search_main, "ROOT", tmp_path)


def test_respect_schedule_skips_a_non_run_day(monkeypatch, tmp_path, no_client, caplog):
    # Weekly cadence with a run recorded today -> nothing more should happen.
    from datetime import date

    _configure(
        monkeypatch,
        tmp_path,
        "schedule:\n  frequency: weekly\n  days_of_week: []\n",
        runs_dates=[date.today().isoformat()],
    )
    with caplog.at_level("INFO"):
        search_main.run(respect_schedule=True)
    assert "Not a scheduled run day" in caplog.text


def test_respect_schedule_off_by_default(monkeypatch, tmp_path, no_client):
    # A run someone typed is a deliberate act: it must NOT be gated, which here
    # means it gets far enough to try (and fail) to build the client.
    from datetime import date

    _configure(
        monkeypatch,
        tmp_path,
        "schedule:\n  frequency: weekly\n  days_of_week: []\n",
        runs_dates=[date.today().isoformat()],
    )
    with pytest.raises(AssertionError, match="past the schedule gate"):
        search_main.run()


def test_invalid_settings_fail_before_any_api_call(monkeypatch, tmp_path, no_client):
    # The config is read first precisely so a typo costs nothing.
    from jobradar.config import ConfigError

    _configure(monkeypatch, tmp_path, "thresholds:\n  min_skill: 500\n")
    with pytest.raises(ConfigError, match="thresholds.min_skill"):
        search_main.run()
