"""Where this copy gets jobradar's updates from, and the commands for the next
step. Read by the setup check (doctor.py) and by the browser page's "Get
updates" task, so both say the same thing.

The README's quick start makes a copy by cloning the public repo and renaming
its remote to `upstream`, so the copy shares the public history and every
update is `git pull upstream master`. Two things make that pull work for
someone who has never used git: their own edits are committed first (git
refuses to pull over uncommitted changes to a file the update also touches;
once committed, the `merge=ours` driver keeps their version), and git has a
name and email to commit with (a fresh machine has neither; a placeholder
local to this repo is set, since it labels local commits only).

A copy made with GitHub's "Use this template" (the README's way until
1.29) has a history of its own,
unrelated to the public repo's. Getting updates into it takes three things,
once: the `merge=ours` driver (so the user's profile and config survive
merges), a remote named `upstream` pointing at the public repo, and a first
merge with `--allow-unrelated-histories -X theirs`. With no history in common
every file that changed upstream since the copy was made is an add/add
conflict, which `-X theirs` settles in the public repo's favour; the driver
still keeps the user's own files. Without the driver, `-X theirs` would
replace them with the templates, so the driver always comes first. After
that first merge, `git pull upstream master` is all it takes.

A clone of the public repo itself needs none of this: its `origin` is the
source, and `git pull` updates it.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # project root, two levels above src/jobradar/

PUBLIC_REPO = "https://github.com/YingzhiVA/jobradar.git"

# How the public repo may be named in a remote URL: https or ssh, with or
# without .git, any case.
_PUBLIC = re.compile(r"github\.com[/:]yingzhiva/jobradar(\.git)?/?$", re.IGNORECASE)

FIRST_MERGE = "git merge --allow-unrelated-histories -X theirs upstream/master"

NO_GIT = "no_git"  # not a git checkout (a downloaded zip, say)
NOT_CONNECTED = "not_connected"  # no `upstream`, and origin is the user's own repo
FIRST_UPDATE = "first_update"  # `upstream` is there, the one-time merge is not done
READY = "ready"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _out(root: Path, *args: str) -> str | None:
    done = _git(root, *args)
    return done.stdout.strip() if done and done.returncode == 0 and done.stdout.strip() else None


def _prepare(root: Path) -> list[str]:
    """What has to be in place before any merge: the settings that keep the
    user's files (the driver must come before every merge), and their own
    edits committed. Only what is missing is listed."""
    commands = []
    if not _out(root, "config", "--get", "merge.ours.driver"):
        commands.append("git config merge.ours.driver true")
    if not _out(root, "config", "--get", "pull.rebase"):
        commands.append("git config pull.rebase false")
    if _out(root, "status", "--porcelain"):
        if not _out(root, "config", "--get", "user.email"):
            commands += ['git config user.name "jobradar"', 'git config user.email "jobradar@localhost"']
        commands += ["git add -A", 'git commit -m "My setup and results"']
    return commands


def update_status(root: Path) -> dict:
    """{"state": one of the constants above, "remote": the remote updates
    come from or None, "commands": what to run next, in order}."""
    if not (root / ".git").exists():
        return {"state": NO_GIT, "remote": None, "commands": []}
    prepare = _prepare(root)
    if _out(root, "remote", "get-url", "upstream"):
        fetched = _git(root, "rev-parse", "--verify", "--quiet", "upstream/master")
        shared = _git(root, "merge-base", "HEAD", "upstream/master") if fetched and fetched.returncode == 0 else None
        if shared is not None and shared.returncode == 0:
            return {"state": READY, "remote": "upstream", "commands": prepare + ["git pull upstream master"]}
        return {
            "state": FIRST_UPDATE,
            "remote": "upstream",
            "commands": prepare + ["git fetch upstream", FIRST_MERGE],
        }
    origin = _out(root, "remote", "get-url", "origin")
    if origin and _PUBLIC.search(origin):
        return {"state": READY, "remote": "origin", "commands": prepare + ["git pull"]}
    return {
        "state": NOT_CONNECTED,
        "remote": None,
        "commands": prepare + [f"git remote add upstream {PUBLIC_REPO}", "git fetch upstream", FIRST_MERGE],
    }


def code_version(root: Path = ROOT) -> str:
    """The version of the code in this folder. Read from pyproject.toml first:
    people run from a clone and update by pulling, which leaves the installed
    package's own record at whatever it was on the day of `pip install`.
    Empty when neither is readable."""
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version("jobradar")
    except metadata.PackageNotFoundError:
        return ""
