"""Where each company's job board is, and which boards to scan.

Two places in ``config/`` share the job, so that a board that moves is fixed
in one place for everyone:

- ``boards/`` is the shared catalogue: every known board with its ``ats`` and
  ``slug``, in ``global.yaml`` (boards that serve every market) and one file
  per market (``ch.yaml``). A search sees ``global.yaml`` plus its market's
  file. It is maintained upstream and updated by ``git pull``, like the code.
- ``companies.yaml`` is the user's selection: a list of names looked up in the
  catalogue. An entry may instead give its own ``ats`` and ``slug``, for a
  board the catalogue doesn't have, or to scan a catalogue board differently.

Before this split, both facts lived in ``companies.yaml``. The user's copy
never merges with upstream (``merge=ours``), so a board fixed in the template
stayed broken in every copy until its owner edited it by hand.

``load_companies`` is the one entry point: it reads both files and returns the
boards to scan, with notes on anything in the selection that needs attention.
The search logs the notes and scans what it can; ``doctor`` reports them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .markets import CH, Market

BOARDS_DIR = "boards"
GLOBAL_BOARDS = "global"
COMPANIES_FILE = "companies.yaml"

ERROR = "error"
WARNING = "warning"


class BoardsError(Exception):
    """A board file is missing or isn't valid YAML."""


class _NoDuplicateKeysLoader(yaml.SafeLoader):
    """PyYAML keeps the last of two identical keys and says nothing. In a board
    list that turns a half-edited entry into a silent rewrite of the entry
    above it: uncomment `ats:`/`slug:` without `- name:` and the previous
    company is scanned from the wrong board. Here it is an error."""


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


def _read_list(path: Path, key: str) -> list:
    """The list under `key` in a YAML file. A key whose every entry is
    commented out reads as None, which is how the template ships, so that is
    an empty list rather than an error."""
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_NoDuplicateKeysLoader) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BoardsError(f"{path.name}: {' '.join(str(exc).split())}") from exc
    if not isinstance(data, dict):
        raise BoardsError(f"{path.name}: expected a `{key}:` list at the top")
    entries = data.get(key) or []
    if not isinstance(entries, list):
        raise BoardsError(f"{path.name}: `{key}:` must be a list")
    return entries


def _key(name: str) -> str:
    """How names are matched: case and spacing don't count."""
    return " ".join(str(name).split()).casefold()


def _known_ats() -> set[str]:
    from .search.sources.company_pages import _FETCHERS

    return set(_FETCHERS)


def catalog_files(config: Path, market: Market = CH) -> list[Path]:
    """The catalogue files a search in `market` reads, in order."""
    return [config / BOARDS_DIR / f"{name}.yaml" for name in (GLOBAL_BOARDS, market.code)]


def read_catalog(config: Path, market: Market = CH) -> list:
    """The raw entries of the market's catalogue: config/boards/global.yaml,
    then the market's own file. Each dict entry gains a `file` key naming the
    file it came from (config/boards/ch.yaml), for messages that say where to
    fix it."""
    entries: list = []
    for path in catalog_files(config, market):
        where = f"{config.name}/{BOARDS_DIR}/{path.name}"
        entries += [
            {**entry, "file": where} if isinstance(entry, dict) else entry
            for entry in _read_list(path, "boards")
        ]
    return entries


def _where(entry: dict) -> str:
    return entry.get("file") or f"config/{BOARDS_DIR}/"


def catalog_problems(entries: list) -> list[str]:
    """What is wrong with the catalogue's entries, one line each."""
    known = _known_ats()
    problems = []
    seen: dict[str, dict] = {}
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            problems.append(f"entry {i} is not a name/ats/slug block")
            continue
        label = entry.get("name") or f"entry {i}"
        if entry.get("file"):
            label = f"{entry['file']}: {label}"
        missing = [k for k in ("name", "ats", "slug") if not entry.get(k)]
        if missing:
            problems.append(f"{label}: missing {', '.join(missing)}")
            continue
        if str(entry["ats"]).lower() not in known:
            problems.append(f"{label}: unknown ats {entry['ats']!r}")
        key = _key(entry["name"])
        if key in seen:
            first = seen[key]
            also = repr(first["name"])
            if _where(first) != _where(entry):
                also += f" in {_where(first)}"
            problems.append(f"{label}: listed twice (also as {also})")
        seen.setdefault(key, entry)
    return problems


def index_catalog(entries: list) -> dict[str, dict]:
    """Complete catalogue entries by matching key. Incomplete ones are left
    out here; catalog_problems names them."""
    index: dict[str, dict] = {}
    for entry in entries:
        if isinstance(entry, dict) and all(entry.get(k) for k in ("name", "ats", "slug")):
            index.setdefault(_key(entry["name"]), entry)
    return index


@dataclass(frozen=True)
class Note:
    level: str  # ERROR: the entry is not scanned. WARNING: scanned, but look.
    message: str


@dataclass
class Resolution:
    # Each board is {"name", "ats", "slug", "origin"}; origin says where its
    # ats/slug came from, and so which file to fix when it breaks: "catalog"
    # (config/boards/), "override" (companies.yaml, for a name the catalogue
    # also has) or "local" (companies.yaml only). A catalog board also carries
    # "file", the catalogue file it came from.
    boards: list[dict] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    selected: int = 0  # entries in companies.yaml, scannable or not

    @property
    def errors(self) -> list[str]:
        return [n.message for n in self.notes if n.level == ERROR]

    @property
    def warnings(self) -> list[str]:
        return [n.message for n in self.notes if n.level == WARNING]


def resolve(selection: list, catalog: dict[str, dict], market: Market = CH) -> Resolution:
    """Turn the entries of companies.yaml into boards to scan, using the
    catalogue for any entry that gives only a name. `market` only names the
    catalogue in messages."""
    files = f"config/{BOARDS_DIR}/{GLOBAL_BOARDS}.yaml or {market.code}.yaml"
    known = _known_ats()
    result = Resolution(selected=len(selection))
    scanned: set[str] = set()
    for i, entry in enumerate(selection, 1):
        if isinstance(entry, str):
            name, ats, slug = entry, None, None
        elif isinstance(entry, dict):
            name, ats, slug = entry.get("name"), entry.get("ats"), entry.get("slug")
        else:
            result.notes.append(Note(ERROR, f"entry {i} is neither a name nor a name/ats/slug block"))
            continue
        if not name or not str(name).strip():
            result.notes.append(Note(ERROR, f"entry {i}: missing name"))
            continue
        name = str(name).strip()
        if bool(ats) != bool(slug):
            result.notes.append(
                Note(ERROR, f"{name}: give both ats and slug, or neither (uncomment all of its lines)")
            )
            continue
        shared = catalog.get(_key(name))

        if ats:
            ats, slug = str(ats).lower(), str(slug)
            if ats not in known:
                result.notes.append(Note(ERROR, f"{name}: unknown ats {ats!r}"))
                continue
            if shared is None:
                board = {"name": name, "ats": ats, "slug": slug, "origin": "local"}
            else:
                board = {"name": shared["name"], "ats": ats, "slug": slug, "origin": "override"}
                shared_ats, shared_slug = str(shared["ats"]).lower(), str(shared["slug"])
                if (ats, slug) != (shared_ats, shared_slug):
                    result.notes.append(Note(
                        WARNING,
                        f"{name}: {_where(shared)} has it on {shared_ats}/{shared_slug}, "
                        f"your companies.yaml on {ats}/{slug} — delete your ats and slug "
                        f"lines to follow the shared list",
                    ))
        elif shared is None:
            result.notes.append(Note(
                ERROR,
                f"{name}: not in {files} — check the spelling, or give its ats and slug",
            ))
            continue
        else:
            board = {
                "name": shared["name"],
                "ats": str(shared["ats"]).lower(),
                "slug": str(shared["slug"]),
                "origin": "catalog",
                **({"file": shared["file"]} if shared.get("file") else {}),
            }

        key = _key(board["name"])
        if key in scanned:
            result.notes.append(Note(WARNING, f"{name}: listed twice; scanned once"))
            continue
        scanned.add(key)
        result.boards.append(board)
    return result


def load_companies(root: Path, market: Market = CH) -> Resolution:
    """The boards to scan, from config/companies.yaml resolved against the
    market's catalogue in config/boards/. Raises BoardsError if a file is
    missing or unreadable."""
    config = root / "config"
    catalog = index_catalog(read_catalog(config, market))
    return resolve(_read_list(config / COMPANIES_FILE, "companies"), catalog, market)
