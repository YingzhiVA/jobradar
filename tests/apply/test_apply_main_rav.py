"""The rav.enabled switch as seen from the apply CLI's bookkeeping path."""

import pytest
import yaml

from jobradar.apply import main as apply_main


def _root(tmp_path, monkeypatch, *, rav_enabled: bool):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "search.yaml").write_text(
        yaml.safe_dump({"rav": {"enabled": rav_enabled}}), encoding="utf-8"
    )
    (tmp_path / "applications").mkdir()
    monkeypatch.setattr(apply_main, "ROOT", tmp_path)
    return tmp_path


@pytest.mark.parametrize("argv", [["--rav", "2026-07"], ["--rav-filed", "2026-07"]])
def test_rav_commands_refuse_when_disabled(tmp_path, monkeypatch, argv):
    _root(tmp_path, monkeypatch, rav_enabled=False)
    with pytest.raises(SystemExit) as excinfo:
        apply_main.main(argv)
    assert "RAV reporting is off" in str(excinfo.value)
    assert "rav.enabled" in str(excinfo.value)
    assert not (tmp_path / "reports").exists()


def test_rav_table_renders_when_enabled(tmp_path, monkeypatch, capsys):
    _root(tmp_path, monkeypatch, rav_enabled=True)
    with pytest.raises(SystemExit) as excinfo:
        apply_main.main(["--rav", "2026-07"])
    assert excinfo.value.code == 0
    assert "Arbeitsbemühungen 2026-07" in capsys.readouterr().out


def test_archive_sweep_runs_without_rav(tmp_path, monkeypatch, capsys):
    # The sweep must not need a filed-month ledger when RAV is off.
    _root(tmp_path, monkeypatch, rav_enabled=False)
    with pytest.raises(SystemExit) as excinfo:
        apply_main.main(["--archive"])
    assert excinfo.value.code == 0
    assert "Nothing to archive." in capsys.readouterr().out


def test_bad_search_yaml_names_the_key(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch, rav_enabled=False)
    (root / "config" / "search.yaml").write_text("rav:\n  enabled: maybe\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        apply_main.main(["--archive"])
    assert "rav.enabled" in str(excinfo.value)
