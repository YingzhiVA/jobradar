"""updates.update_status against real git repositories: the README's
clone-based copy, a "Use this template" copy (unrelated history) going from
not connected to ready, and a plain clone of the public repo. Each flow runs
the commands the page would show."""

import os
import subprocess

import pytest

from jobradar import doctor, updates
from jobradar.doctor import OK, WARN


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for var, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,  # no user settings (a default branch name, hooks)
    }.items():
        monkeypatch.setenv(var, value)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(path, files):
    path.mkdir()
    _git(path, "init", "-q", "-b", "master")
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def test_not_a_git_checkout(tmp_path):
    assert updates.update_status(tmp_path)["state"] == updates.NO_GIT
    assert doctor.check_updates(tmp_path) is None


def test_a_template_copy_from_not_connected_to_ready(tmp_path):
    public = _repo(tmp_path / "public", {
        ".gitattributes": "profile/** merge=ours\n", "src/a.py": "v1\n", "profile/identity.md": "template\n",
    })
    copy = _repo(tmp_path / "copy", {
        ".gitattributes": "profile/** merge=ours\n", "src/a.py": "v1\n", "profile/identity.md": "mine\n",
    })
    (public / "src" / "a.py").write_text("v2\n", encoding="utf-8")
    _git(public, "commit", "-q", "-am", "v2")

    status = updates.update_status(copy)
    assert status["state"] == updates.NOT_CONNECTED
    assert status["commands"][0] == "git config merge.ours.driver true"  # before the merge, always
    assert f"git remote add upstream {updates.PUBLIC_REPO}" in status["commands"]
    assert status["commands"][-1] == updates.FIRST_MERGE
    assert doctor.check_updates(copy).level == WARN

    _git(copy, "remote", "add", "upstream", str(public))
    assert updates.update_status(copy)["state"] == updates.FIRST_UPDATE  # not fetched yet
    _git(copy, "fetch", "-q", "upstream")
    assert updates.update_status(copy)["state"] == updates.FIRST_UPDATE  # no history in common

    # Run what the page says, with the local public repo standing in for GitHub.
    for command in updates.update_status(copy)["commands"]:
        subprocess.run(command, shell=True, cwd=copy, check=True, capture_output=True)
    assert (copy / "src" / "a.py").read_text(encoding="utf-8") == "v2\n"
    assert (copy / "profile" / "identity.md").read_text(encoding="utf-8") == "mine\n"

    status = updates.update_status(copy)
    assert status == {"state": updates.READY, "remote": "upstream", "commands": ["git pull upstream master"]}
    assert doctor.check_updates(copy).level == OK


def _run(root, commands):
    for command in commands:
        subprocess.run(command, shell=True, cwd=root, check=True, capture_output=True)


def test_the_readme_copy_takes_an_update_and_keeps_the_users_files(tmp_path):
    public = _repo(tmp_path / "public", {
        ".gitattributes": "profile/** merge=ours\n", "src/a.py": "v1\n", "profile/identity.md": "template\n",
    })
    subprocess.run(["git", "clone", "-q", str(public), str(tmp_path / "copy")], check=True)
    copy = tmp_path / "copy"
    # Quick start, step 1.
    _run(copy, ["git remote rename origin upstream", "git config merge.ours.driver true",
                "git config pull.rebase false"])
    # The user writes their identity and never commits; a release rewords the
    # template and changes the code.
    (copy / "profile" / "identity.md").write_text("mine\n", encoding="utf-8")
    (public / "src" / "a.py").write_text("v2\n", encoding="utf-8")
    (public / "profile" / "identity.md").write_text("template, reworded\n", encoding="utf-8")
    _git(public, "commit", "-q", "-am", "v2")
    _git(copy, "fetch", "-q", "upstream")

    status = updates.update_status(copy)
    assert status["state"] == updates.READY and status["remote"] == "upstream"
    assert status["commands"][-3:] == ["git add -A", 'git commit -m "My setup and results"', "git pull upstream master"]
    _run(copy, status["commands"])
    assert (copy / "src" / "a.py").read_text(encoding="utf-8") == "v2\n"
    assert (copy / "profile" / "identity.md").read_text(encoding="utf-8") == "mine\n"
    assert updates.update_status(copy)["commands"] == ["git pull upstream master"]


def test_a_clone_of_the_public_repo_just_pulls(tmp_path):
    clone = _repo(tmp_path / "clone", {"a.txt": "x\n"})
    _git(clone, "remote", "add", "origin", "git@github.com:YingzhiVA/jobradar.git")
    status = updates.update_status(clone)
    assert (status["state"], status["remote"]) == (updates.READY, "origin")
    assert status["commands"] == ["git config merge.ours.driver true", "git config pull.rebase false", "git pull"]
