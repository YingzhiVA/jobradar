"""Check a jobradar setup before the first run: ``python -m jobradar.doctor``.

Read-only. Each check prints one line — ``[ok]``, ``[warn]`` or ``[FAIL]`` —
and the command exits non-zero if anything failed. The point is that a
missing key, a template profile or a typo in a YAML file is named here, in one
place, rather than surfacing as a traceback halfway through a run.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml
from dotenv import load_dotenv

from .apply.pdf import find_browser
from .archive import load_retention
from .config import ConfigError, load_search_settings
from .models import Constraints

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

OK = "ok"
WARN = "warn"
FAIL = "FAIL"

# Every shipped template carries this marker; a profile file that still has it
# has not been replaced with the user's own content.
TEMPLATE_MARKER = "jobradar template:"


@dataclass(frozen=True)
class Check:
    name: str
    level: str
    message: str

    def render(self) -> str:
        return f"[{self.level}] {self.name}: {self.message}"


def _has_marker(path: Path) -> bool:
    try:
        return TEMPLATE_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def check_python() -> Check:
    v = sys.version_info
    if (v.major, v.minor) >= (3, 11):
        return Check("Python", OK, f"{v.major}.{v.minor}.{v.micro}")
    return Check("Python", FAIL, f"{v.major}.{v.minor} found; jobradar needs 3.11 or newer")


def check_credentials(root: Path, connect: Callable[[], None]) -> Check:
    """`connect` performs the real credential preflight (a free API call);
    injected so tests never touch the network."""
    load_dotenv(root / ".env")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key == "your_anthropic_api_key_here":
        return Check(
            "Anthropic credentials", FAIL,
            ".env still holds the placeholder key; paste your real key from the Claude Console",
        )
    try:
        connect()
    except Exception as exc:  # noqa: BLE001 - any failure is the finding here
        return Check("Anthropic credentials", FAIL, str(exc).splitlines()[0])
    source = "ANTHROPIC_API_KEY" if key else "an `ant auth login` profile or federation"
    return Check("Anthropic credentials", OK, f"authenticated via {source}")


def check_profile(root: Path) -> list[Check]:
    checks: list[Check] = []
    cvs_dir = root / "profile" / "cvs"
    cvs = [p for p in sorted(cvs_dir.glob("*.md")) if p.name != "README.md"] if cvs_dir.is_dir() else []
    if not cvs:
        checks.append(Check("CVs", FAIL, f"no CV found in {cvs_dir.relative_to(root)}/ — add at least one .md file"))
    else:
        templates = [p.name for p in cvs if _has_marker(p)]
        real = [p.name for p in cvs if not _has_marker(p)]
        if real:
            checks.append(Check("CVs", OK, ", ".join(real) + (f" (delete the template {', '.join(templates)})" if templates else "")))
        else:
            checks.append(Check("CVs", WARN, f"only the shipped template ({', '.join(templates)}) — scoring will be meaningless until you add your own CV"))
    identity = root / "profile" / "identity.md"
    if not identity.exists():
        checks.append(Check("Identity", WARN, "profile/identity.md is missing; interest-fit scoring will have nothing to go on"))
    elif _has_marker(identity):
        checks.append(Check("Identity", WARN, "profile/identity.md is still the template — write your own before trusting any interest score"))
    else:
        checks.append(Check("Identity", OK, "profile/identity.md"))
    return checks


class _NoDuplicateKeysLoader(yaml.SafeLoader):
    """PyYAML keeps the last of two identical keys and says nothing. In
    companies.yaml that turns a half-edited entry into a silent rewrite of the
    entry above it — uncomment `ats:`/`slug:` without `- name:` and the
    previous company is scanned from the wrong board. Here it is an error."""


def _construct_mapping_no_duplicates(loader, node, deep=False):
    loader.flatten_mapping(node)
    first_line: dict = {}
    for key_node, _value in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in first_line:
            raise yaml.constructor.ConstructorError(
                None, None,
                f"key {key!r} appears twice in one entry (lines {first_line[key]} and "
                f"{key_node.start_mark.line + 1}) — a company's lines were probably "
                f"only partly commented or uncommented",
                key_node.start_mark,
            )
        first_line[key] = key_node.start_mark.line + 1
    return loader.construct_mapping(node, deep=deep)


_NoDuplicateKeysLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_duplicates
)

# Above this many boards, the first run is worth a warning: it scans every
# open posting on each of them at once. See docs/COSTS.md.
MANY_BOARDS = 15


def validate_companies(entries: list) -> list[str]:
    """Problems with the entries of config/companies.yaml, one line each."""
    from .search.sources.company_pages import _FETCHERS

    problems = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            problems.append(f"entry {i} is not a name/ats/slug block")
            continue
        label = entry.get("name") or f"entry {i}"
        missing = [k for k in ("name", "ats", "slug") if not entry.get(k)]
        if missing:
            problems.append(f"{label}: missing {', '.join(missing)} (uncomment all of its lines)")
        elif str(entry["ats"]).lower() not in _FETCHERS:
            problems.append(f"{label}: unknown ats {entry['ats']!r}")
    return problems


def check_companies(root: Path) -> Check:
    path = root / "config" / "companies.yaml"
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_NoDuplicateKeysLoader) or {}
    except (OSError, yaml.YAMLError) as exc:
        return Check("companies.yaml", FAIL, " ".join(str(exc).split())[:220])
    entries = (data.get("companies") or []) if isinstance(data, dict) else []
    if not entries:
        return Check(
            "companies.yaml", WARN,
            "no company boards selected yet — uncomment the ones you want in "
            "config/companies.yaml (start with a handful, see docs/COSTS.md). "
            "Until then only web search runs.",
        )
    problems = validate_companies(entries)
    if problems:
        return Check("companies.yaml", FAIL, "; ".join(problems))
    if len(entries) > MANY_BOARDS:
        return Check(
            "companies.yaml", WARN,
            f"{len(entries)} boards selected — the first run scans every open posting "
            f"on all of them at once and costs far more than a normal day; see "
            f"docs/COSTS.md",
        )
    return Check("companies.yaml", OK, f"{len(entries)} boards")


def check_config(root: Path) -> list[Check]:
    checks: list[Check] = [check_companies(root)]
    constraints = root / "config" / "constraints.yaml"
    try:
        Constraints.from_dict(yaml.safe_load(constraints.read_text(encoding="utf-8")) or {})
        checks.append(Check("constraints.yaml", OK, "parses"))
    except Exception as exc:  # noqa: BLE001 - report whatever the parser objected to
        checks.append(Check("constraints.yaml", FAIL, str(exc).splitlines()[0]))
    try:
        settings = load_search_settings(root)
        extras = []
        if settings.rav.enabled:
            extras.append("RAV reporting on")
        if settings.sources.eth_jobs.enabled:
            extras.append("ETH board on")
        checks.append(Check("search.yaml", OK, ", ".join(extras) or "defaults"))
    except ConfigError as exc:
        checks.append(Check("search.yaml", FAIL, str(exc)))
    try:
        load_retention(root)
        checks.append(Check("retention.yaml", OK, "parses"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("retention.yaml", FAIL, str(exc).splitlines()[0]))
    return checks


def check_browser() -> Check:
    browser = find_browser()
    if browser:
        return Check("PDF export", OK, browser)
    return Check(
        "PDF export", WARN,
        "no Chromium/Chrome on PATH — the apply step will write .html files to print yourself",
    )


def check_merge_driver(root: Path) -> Check | None:
    """The shipped .gitattributes keeps your profile, config and state out of
    upstream merges — but only once git knows what `merge=ours` means."""
    if not (root / ".gitattributes").exists() or not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "config", "--get", "merge.ours.driver"],
            cwd=root, capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    if out.returncode == 0 and out.stdout.strip():
        return Check("Git merge driver", OK, "merge=ours configured")
    return Check(
        "Git merge driver", WARN,
        "run `git config merge.ours.driver true` so pulling updates never overwrites your profile or config",
    )


def run_checks(root: Path, connect: Callable[[], None]) -> list[Check]:
    checks = [check_python(), check_credentials(root, connect)]
    checks += check_profile(root)
    checks += check_config(root)
    checks.append(check_browser())
    if (merge := check_merge_driver(root)) is not None:
        checks.append(merge)
    return checks


def _default_connect() -> None:
    import anthropic

    from .search.main import _check_authentication

    _check_authentication(anthropic.Anthropic())


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Check this jobradar setup and report what is missing.")
    parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    checks = run_checks(ROOT, _default_connect)
    for check in checks:
        print(check.render())
    failed = [c for c in checks if c.level == FAIL]
    warned = [c for c in checks if c.level == WARN]
    if failed:
        print(f"\n{len(failed)} problem(s) to fix before the first run.")
        raise SystemExit(1)
    print(f"\nReady to run{' — with ' + str(len(warned)) + ' warning(s) above' if warned else ''}.")


if __name__ == "__main__":
    main()
