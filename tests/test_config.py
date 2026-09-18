import logging

import pytest
import yaml

from jobradar.config import (
    ConfigError,
    EthSource,
    Output,
    Rav,
    Schedule,
    SearchSettings,
    Sources,
    Thresholds,
    load_search_settings,
    replace,
)


def write_config(root, data):
    (root / "config").mkdir(parents=True, exist_ok=True)
    path = root / "config" / "search.yaml"
    path.write_text(yaml.safe_dump(data) if isinstance(data, (dict, list)) else data, encoding="utf-8")
    return path


def test_missing_file_falls_back_to_defaults(tmp_path):
    settings = load_search_settings(tmp_path)
    assert settings == SearchSettings()
    assert settings.thresholds.min_skill == 60
    assert settings.output.max_best == 1
    assert settings.output.max_okay == 3
    assert settings.schedule.interval_days == 1


def test_empty_file_falls_back_to_defaults(tmp_path):
    write_config(tmp_path, "")
    assert load_search_settings(tmp_path) == SearchSettings()


def test_partial_file_keeps_defaults_for_absent_keys(tmp_path):
    # The point of per-key fallback: setting one dial must not silently reset
    # the others to whatever the file doesn't say.
    write_config(tmp_path, {"thresholds": {"min_skill": 75}})
    settings = load_search_settings(tmp_path)
    assert settings.thresholds.min_skill == 75
    assert settings.thresholds.best_threshold == 80
    assert settings.output == Output()
    assert settings.schedule == Schedule()


def test_full_file_is_read(tmp_path):
    write_config(
        tmp_path,
        {
            "thresholds": {"min_skill": 50, "min_interest": 40, "best_threshold": 85},
            "output": {"max_best": 2, "max_okay": 6},
            "schedule": {"frequency": "weekly", "days_of_week": ["mon"]},
        },
    )
    settings = load_search_settings(tmp_path)
    assert settings.thresholds == Thresholds(min_skill=50, min_interest=40, best_threshold=85)
    assert settings.output == Output(max_best=2, max_okay=6)
    assert settings.schedule.frequency == "weekly"
    assert settings.schedule.interval_days == 7
    assert settings.schedule.days_of_week == ("mon",)


def test_unknown_key_warns_but_does_not_fail(tmp_path, caplog):
    # A stray key is worth saying out loud (the user edited it expecting an
    # effect) but is never worth refusing to run a day's search over.
    write_config(tmp_path, {"thresholds": {"min_skil": 70}})
    with caplog.at_level(logging.WARNING):
        settings = load_search_settings(tmp_path)
    assert settings.thresholds.min_skill == 60
    assert "min_skil" in caplog.text


@pytest.mark.parametrize(
    "data",
    [
        {"thresholds": {"min_skill": 101}},
        {"thresholds": {"min_skill": -1}},
        {"thresholds": {"best_threshold": "high"}},
        {"output": {"max_okay": -1}},
        {"schedule": {"frequency": "hourly"}},
        {"schedule": {"min_interval_days": 0}},
        {"schedule": {"days_of_week": ["funday"]}},
        {"schedule": {"days_of_week": "mon"}},
        {"rav": {"enabled": "maybe"}},
        {"rav": {"enabled": 2}},
        {"sources": {"eth_jobs": {"job_types": "4"}}},
        {"sources": {"eth_jobs": {"job_types": ["finance"]}}},
        {"sources": {"eth_jobs": {"job_types": [0]}}},
        {"sources": {"eth_jobs": {"enabled": True, "job_types": []}}},
    ],
)
def test_present_but_invalid_values_fail_loudly(tmp_path, data):
    # Deliberately NOT a silent fallback: these dials change what the search
    # surfaces, so a typo must surface on the first run, not days later.
    write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_search_settings(tmp_path)


def test_error_message_names_the_file_and_the_key(tmp_path):
    write_config(tmp_path, {"thresholds": {"min_skill": 140}})
    with pytest.raises(ConfigError) as excinfo:
        load_search_settings(tmp_path)
    message = str(excinfo.value)
    assert "search.yaml" in message
    assert "thresholds.min_skill" in message


def test_both_caps_zero_is_rejected(tmp_path):
    # Would score every posting and then surface nothing — never intentional.
    write_config(tmp_path, {"output": {"max_best": 0, "max_okay": 0}})
    with pytest.raises(ConfigError):
        load_search_settings(tmp_path)


def test_one_cap_zero_is_allowed(tmp_path):
    write_config(tmp_path, {"output": {"max_best": 0, "max_okay": 4}})
    assert load_search_settings(tmp_path).output == Output(max_best=0, max_okay=4)


def test_malformed_yaml_fails_with_the_path(tmp_path):
    write_config(tmp_path, "thresholds: [unclosed\n")
    with pytest.raises(ConfigError) as excinfo:
        load_search_settings(tmp_path)
    assert "search.yaml" in str(excinfo.value)


def test_non_mapping_file_is_rejected(tmp_path):
    write_config(tmp_path, ["min_skill", 70])
    with pytest.raises(ConfigError):
        load_search_settings(tmp_path)


def test_weekday_names_are_normalized_and_ordered(tmp_path):
    write_config(tmp_path, {"schedule": {"days_of_week": ["Friday", "MON", "fri"]}})
    # Full names and abbreviations both work, duplicates collapse, and the
    # result reads in calendar order however it was listed.
    assert load_search_settings(tmp_path).schedule.days_of_week == ("mon", "fri")


def test_empty_days_of_week_means_any_day(tmp_path):
    write_config(tmp_path, {"schedule": {"days_of_week": []}})
    schedule = load_search_settings(tmp_path).schedule
    assert schedule.days_of_week == ()
    assert all(schedule.allows_weekday(d) for d in range(7))


def test_custom_frequency_uses_min_interval_days(tmp_path):
    write_config(tmp_path, {"schedule": {"frequency": "custom", "min_interval_days": 4}})
    assert load_search_settings(tmp_path).schedule.interval_days == 4


def test_min_interval_days_ignored_for_named_frequency(tmp_path):
    # The named cadence wins; min_interval_days is only consulted for "custom",
    # so a leftover value can't quietly override a frequency the user set.
    write_config(tmp_path, {"schedule": {"frequency": "weekly", "min_interval_days": 2}})
    assert load_search_settings(tmp_path).schedule.interval_days == 7


def test_shipped_config_matches_the_documented_defaults():
    # config/search.yaml ships with every dial spelled out at its default, so
    # the file doubles as the documentation. If a default moves in code without
    # the file following, this catches it.
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    shipped = load_search_settings(root)
    # The two situation switches (rav, sources) are legitimately flipped in a
    # private copy, so only the dials are pinned to their defaults here.
    assert replace(shipped, rav=Rav(), sources=Sources()) == SearchSettings()


def test_describe_mentions_every_dial():
    text = SearchSettings().describe()
    for dial in (
        "min_skill", "min_interest", "best_threshold", "max_best", "max_okay",
        "frequency", "rav", "eth_jobs",
    ):
        assert dial in text


# --- rav ----------------------------------------------------------------------


def test_rav_defaults_off(tmp_path):
    assert load_search_settings(tmp_path).rav == Rav(enabled=False)


@pytest.mark.parametrize("value", [True, "true", "Yes", "on"])
def test_rav_enabled_accepts_bools_and_their_words(tmp_path, value):
    write_config(tmp_path, {"rav": {"enabled": value}})
    assert load_search_settings(tmp_path).rav.enabled is True


def test_rav_describe_says_what_off_means():
    assert "--rav" in SearchSettings().describe()


# --- sources.eth_jobs ----------------------------------------------------------


def test_eth_source_defaults_off_with_the_two_known_categories(tmp_path):
    eth = load_search_settings(tmp_path).sources.eth_jobs
    assert eth == EthSource(enabled=False, job_types=(4, 5))


def test_eth_job_types_accept_names_and_ints(tmp_path):
    write_config(
        tmp_path,
        {"sources": {"eth_jobs": {"enabled": True, "job_types": ["Management", 7, "management"]}}},
    )
    eth = load_search_settings(tmp_path).sources.eth_jobs
    assert eth.enabled is True
    assert eth.job_types == (4, 7)  # names resolve, duplicates collapse


def test_eth_empty_job_types_is_fine_while_disabled(tmp_path):
    write_config(tmp_path, {"sources": {"eth_jobs": {"job_types": []}}})
    assert load_search_settings(tmp_path).sources.eth_jobs.job_types == ()
