"""Read and write the files a first-time setup touches.

Each function here does by form what the README's quick start has the user do
in a text editor: the API key in ``.env``, a CV in ``profile/cvs/``, the
identity page, ``config/constraints.yaml``, the board selection in
``config/companies.yaml`` and the dials in ``config/search.yaml``.

A YAML file is changed in place (yamledit.py): the values a form shows are
replaced on their own lines and everything else, comments included, stays as
it was written. The files are edited by hand as often as from here.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable

import yaml
from dotenv import dotenv_values

from .. import boards
from ..config import (
    MAX_BEST_RANGE,
    MAX_OKAY_RANGE,
    ConfigError,
    Output,
    SearchSettings,
    Thresholds,
    cost_warnings,
    load_search_settings,
)
from ..cv import WORD_SUFFIXES, sync_word_cvs
from ..doctor import MANY_BOARDS, TEMPLATE_MARKER, _has_marker, _has_run_before, _market
from ..models import Constraints
from ..search.filters import _canton_key
from . import yamledit


class SetupError(ValueError):
    """Input the user can fix; the message is shown to them as it is."""


# ── UI state ────────────────────────────────────────────────────────────────

STATE_FILE = "ui_state.json"  # under data/, which .gitignore keeps local


def _load_state(root: Path) -> dict:
    try:
        data = json.loads((root / "data" / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(root: Path, state: dict) -> None:
    path = root / "data" / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def confirm_step(root: Path, step: str) -> None:
    """Record that the user has been through a step whose file ships with
    working example values, so "done" can't be read off the file itself."""
    state = _load_state(root)
    confirmed = set(state.get("confirmed", []))
    confirmed.add(step)
    state["confirmed"] = sorted(confirmed)
    _save_state(root, state)


# ── API key ─────────────────────────────────────────────────────────────────

KEY_NAME = "ANTHROPIC_API_KEY"
PLACEHOLDER_KEY = "your_anthropic_api_key_here"
_KEY_LINE = re.compile(rf"^\s*{KEY_NAME}\s*=.*$", re.MULTILINE)
# What an Anthropic key is made of. Anything else is refused before it is
# written: python-dotenv expands ${...} in a value and reads # and quotes as
# syntax, so a pasted value outside this set could read as something else.
_KEY_SHAPE = re.compile(r"^sk-ant-[A-Za-z0-9_-]{13,}$")


def key_status(root: Path) -> dict:
    """Whether a key is stored, and its last four characters. The key itself
    never leaves this process."""
    key = (dotenv_values(root / ".env").get(KEY_NAME) or "").strip()
    if not key or key == PLACEHOLDER_KEY:
        return {"set": False, "hint": ""}
    return {"set": True, "hint": f"sk-ant-…{key[-4:]}"}


def save_key(root: Path, key: str, connect: Callable[[], None]) -> dict:
    """Store the key in .env and prove it works with `connect` (a free call).
    A key that fails the check is still stored, so the message can say what
    the Console said about it and the user can fix the cause there."""
    key = key.strip()
    if not _KEY_SHAPE.match(key):
        raise SetupError(
            "That doesn't look like a Claude API key. It starts with sk-ant- and "
            "is letters, digits, - and _ only; copy it again from the Console."
        )
    env = root / ".env"
    if env.exists():
        text = env.read_text(encoding="utf-8")
    elif (root / ".env.example").exists():
        text = (root / ".env.example").read_text(encoding="utf-8")
    else:
        text = ""
    line = f"{KEY_NAME}={key}"
    if _KEY_LINE.search(text):
        text = _KEY_LINE.sub(lambda _m: line, text, count=1)
    else:
        text = f"{text.rstrip()}\n{line}\n".lstrip()
    _write_private(env, text)
    # The commands this UI starts inherit the environment, and a variable
    # already set wins over .env, so a replaced key has to be replaced here too.
    os.environ[KEY_NAME] = key
    try:
        connect()
    except Exception as exc:  # noqa: BLE001 - whatever went wrong is the answer
        return {**key_status(root), "works": False, "error": _key_error(exc)}
    return {**key_status(root), "works": True, "error": ""}


def _write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read. The file is made with that mode
    rather than tightened after writing, so the key is never readable by others
    even for a moment, and a .env that was readable before is tightened too.
    (On Windows the mode has no effect; the file is private to the user's
    profile there.)"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _key_error(exc: Exception) -> str:
    text = str(exc)
    if "authentication failed" in text.lower():
        return (
            "Claude did not accept this key. Check that you copied all of it, and "
            "that it has not been deleted in the Console."
        )
    return text.splitlines()[0] if text else type(exc).__name__


# ── CVs ─────────────────────────────────────────────────────────────────────

MAX_CV_BYTES = 8 * 1024 * 1024


def _cvs_dir(root: Path) -> Path:
    return root / "profile" / "cvs"


def _cv_files(root: Path) -> list[Path]:
    directory = _cvs_dir(root)
    if not directory.is_dir():
        return []
    return [p for p in sorted(directory.glob("*.md")) if p.name != "README.md"]


def list_cvs(root: Path) -> list[dict]:
    out = []
    for path in _cv_files(root):
        word = next((path.with_suffix(s) for s in WORD_SUFFIXES if path.with_suffix(s).exists()), None)
        text = path.read_text(encoding="utf-8", errors="replace")
        out.append({
            "name": path.stem,
            "template": TEMPLATE_MARKER in text,
            "words": len(text.split()),
            "from_word": word.name if word else None,
        })
    return out


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^\w\- ]+", "", Path(name).stem, flags=re.UNICODE).strip().replace(" ", "_")
    if not stem or stem.lower() == "readme":
        raise SetupError("Give the CV a name made of letters and numbers, e.g. product-manager.")
    return stem[:80]


def _drop_template_cv(root: Path) -> None:
    """The shipped example.md exists to show the format. Once a real CV is
    there it would only be scored against as if it were the user's."""
    cvs = _cv_files(root)
    if any(not _has_marker(p) for p in cvs):
        for path in cvs:
            if _has_marker(path):
                path.unlink()


def save_cv_file(root: Path, filename: str, data: bytes) -> dict:
    """Store an uploaded CV. Word files are converted by the same code every
    run uses; Markdown and plain text are stored as they are."""
    if len(data) > MAX_CV_BYTES:
        raise SetupError("That file is larger than 8 MB, which is too big for a CV.")
    suffix = Path(filename).suffix.lower()
    stem = _safe_stem(filename)
    directory = _cvs_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    if suffix == ".pdf":
        raise SetupError(
            "A PDF can't be read reliably. Upload the Word file (.docx) it was made "
            "from, or copy the text of the PDF and paste it instead."
        )
    if suffix == ".doc":
        raise SetupError("Old .doc files can't be read. Open it in Word and save it as .docx first.")
    if suffix == ".docx":
        target = directory / f"{stem}.docx"
        target.write_bytes(data)
        md = target.with_suffix(".md")
        if md.exists():
            md.unlink()  # a new upload replaces the copy made from the old one
        outcomes = sync_word_cvs(directory)
        failed = [o for o in outcomes if o.word == target and o.status == "failed"]
        if failed or not md.exists():
            target.unlink(missing_ok=True)
            detail = failed[0].detail if failed else "it could not be read"
            raise SetupError(f"{filename}: {detail}. Try pasting the text instead.")
    elif suffix in (".md", ".txt", ".markdown"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SetupError(f"{filename} is not plain text.") from exc
        return save_cv_text(root, stem, text)
    else:
        raise SetupError("Upload a Word file (.docx) or a Markdown/text file (.md, .txt).")
    _drop_template_cv(root)
    return {"name": stem, "text": read_cv(root, stem)}


def save_cv_text(root: Path, name: str, text: str) -> dict:
    stem = _safe_stem(name)
    if len(text.split()) < 30:
        raise SetupError(
            "That is too short to be a CV. Paste the whole thing: every role, "
            "project and skill you would want considered."
        )
    directory = _cvs_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.md").write_text(text.strip() + "\n", encoding="utf-8")
    _drop_template_cv(root)
    return {"name": stem, "text": read_cv(root, stem)}


def _cv_path(root: Path, name: str) -> Path:
    path = _cvs_dir(root) / f"{_safe_stem(name)}.md"
    if not path.exists():
        raise SetupError(f"No CV called {name}.")
    return path


def read_cv(root: Path, name: str) -> str:
    return _cv_path(root, name).read_text(encoding="utf-8", errors="replace")


def delete_cv(root: Path, name: str) -> None:
    path = _cv_path(root, name)
    for suffix in WORD_SUFFIXES:
        path.with_suffix(suffix).unlink(missing_ok=True)
    path.unlink()


# ── Identity ────────────────────────────────────────────────────────────────

IDENTITY_TITLE = "# Career identity and motivation"

# The questions of the shipped template, as (heading, prompt). They are here
# and not read from the template because the user's copy of that file is the
# one this form overwrites.
IDENTITY_SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "Who I am professionally",
        "Two or three paragraphs. What you do, for how long, in which industries, "
        "and what you are really good at. Say what you are known for, not what a "
        "job title says.",
    ),
    (
        "My academic background",
        "Degrees, the field, and anything from it you still use. One paragraph is plenty.",
    ),
    (
        "What I'm moving toward",
        "The kind of work, team and company you want next. Concrete beats "
        "aspirational: name the problems you want to work on, the size of company, "
        "the culture, the tools if they matter to you.",
    ),
    (
        "What I'm moving away from",
        "The roles, industries or setups you no longer want, and why. This is what "
        "stops the tool surfacing a role you are qualified for but would never take.",
    ),
    (
        "Non-negotiables that aren't captured in constraints.yaml",
        "Anything a location, workload or office-days filter can't express: travel "
        "share, on-call, managing people or not, language of the workplace. One "
        "line each.",
    ),
)
# What the form calls the last one; the file keeps the template's heading.
IDENTITY_LABELS = {IDENTITY_SECTIONS[-1][0]: "Other things I won't compromise on"}

_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _identity_path(root: Path) -> Path:
    return root / "profile" / "identity.md"


def _section(heading: str, body: str) -> dict:
    hints = dict(IDENTITY_SECTIONS)
    return {
        "heading": heading,
        "label": IDENTITY_LABELS.get(heading, heading),
        "hint": hints.get(heading, ""),
        "body": body,
    }


def _preamble(text: str) -> str:
    """Everything before the first `## ` section: the page's title and any
    note written under it."""
    return _HEADING.split(text, maxsplit=1)[0].strip()


def read_identity(root: Path) -> dict:
    """The identity page as its `## ` sections. A page that is still the
    template, or has no sections yet, comes back as the template's questions
    with empty answers.

    What stands above the first section (the title, often a note on how to
    write the page, or a page written before it had sections) is never a
    field: it answers none of the questions, and the form has its own
    explanation. It is kept as written when the form saves."""
    path = _identity_path(root)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    parts = _HEADING.split(text)
    if not text.strip() or TEMPLATE_MARKER in text or len(parts) == 1:
        return {"template": TEMPLATE_MARKER in text or not text.strip(),
                "sections": [_section(h, "") for h, _ in IDENTITY_SECTIONS]}
    return {
        "template": False,
        "sections": [_section(heading, body.strip()) for heading, body in zip(parts[1::2], parts[2::2])],
    }


def save_identity(root: Path, sections: list[dict]) -> None:
    blocks = []
    for section in sections:
        heading = str(section.get("heading") or "").strip()
        body = str(section.get("body") or "").strip()
        if TEMPLATE_MARKER in body:
            raise SetupError("Remove the template marker line from your text.")
        if body and heading:
            blocks.append(f"## {heading}\n\n{body}")
    if sum(len(b.split()) for b in blocks) < 40:
        raise SetupError(
            "This is too thin to score against. Write at least a few sentences on "
            "what you do and what you want next; vague text gets vague scores."
        )
    path = _identity_path(root)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    # Everything above the first section stays as the user wrote it; the
    # template's own preamble is the template's, and is replaced.
    head = _preamble(current) if current.strip() and TEMPLATE_MARKER not in current else ""
    if not head.startswith("# "):
        head = f"{IDENTITY_TITLE}\n\n{head}".strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join([head or IDENTITY_TITLE, *blocks]) + "\n", encoding="utf-8")


# ── YAML config ─────────────────────────────────────────────────────────────

_HEADER = "# Saved from the jobradar UI (python -m jobradar.ui). {about}\n\n"


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    except yaml.YAMLError as exc:
        raise SetupError(f"{path.name} can't be read: {' '.join(str(exc).split())}") from exc
    if not isinstance(data, dict):
        raise SetupError(f"{path.name} doesn't hold what it should; fix or delete it.")
    return data


def _write_yaml(path: Path, data: dict, about: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.write_text(_HEADER.format(about=about) + body, encoding="utf-8")


def _unordered(value: object) -> object:
    """A value with its lists sorted, for comparing two files' content when
    the order of a list's items carries no meaning."""
    if isinstance(value, dict):
        return {k: _unordered(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((_unordered(v) for v in value), key=repr)
    return value


def _get(data: dict, path: tuple[str, ...]) -> object:
    for key in path:
        data = data.get(key) if isinstance(data, dict) else None
    return data


def _set(data: dict, path: tuple[str, ...], value: object) -> None:
    for key in path[:-1]:
        if not isinstance(data.get(key), dict):
            data[key] = {}
        data = data[key]
    data[path[-1]] = value


def _merged(existing: list, wanted: list[str], same: Callable[[str], str], keep_entry: Callable[[dict], bool]) -> list:
    """What yamledit.set_list leaves in a list, worked out on the values."""
    keys = {same(w) for w in wanted}
    out, have = [], set()
    for item in existing:
        if isinstance(item, dict):
            if keep_entry(item):
                out.append(item)
            continue
        key = same(str(item))
        if key in keys and key not in have:
            out.append(item)
        have.add(key)
    return out + [w for w in wanted if same(w) not in have]


def _save_yaml(path: Path, data: dict, changes: list[tuple], about: str) -> bool:
    """Apply `changes` to the file at `path`, whose parsed content is `data`.

    Each change is (path, value) for a scalar, or (path, names, same,
    keep_entry) for a list of names. The file's text is edited in place so its
    comments survive; the result is parsed and compared with the same changes
    made to `data`, and only written if the two agree. Otherwise, or if there
    is no file yet, the values are written out fresh. Returns whether the
    text was kept.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = None
    edited = text
    for change in changes:
        where = change[0]
        if len(change) == 2:
            parent = _get(data, where[:-1])
            if isinstance(parent, dict) and where[-1] in parent and parent[where[-1]] == change[1]:
                continue  # already says so: leave the line exactly as written
            _set(data, where, change[1])
            edit = lambda t, c=change: yamledit.set_scalar(t, c[0], c[1])  # noqa: E731
        else:
            _, names, same, keep_entry = change
            current = _get(data, where)
            current = current if isinstance(current, list) else []
            merged = _merged(current, names, same, keep_entry)
            if merged == current:
                continue  # nothing to add or drop: leave the list as written
            _set(data, where, merged)
            edit = lambda t, c=change: yamledit.set_list(t, c[0], c[1], same=c[2], keep_entry=c[3])  # noqa: E731
        if edited is not None:
            try:
                edited = edit(edited)
            except yamledit.Unsupported:
                edited = None
    if edited is not None:
        try:
            agrees = _unordered(yaml.safe_load(edited)) == _unordered(data)
        except yaml.YAMLError:
            agrees = False
        if agrees:
            if edited != text:
                path.write_text(edited, encoding="utf-8")
            return True
    _write_yaml(path, data, about)
    return False


def _keep(_entry: dict) -> bool:
    return True


def _int(value: object, label: str, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SetupError(f"{label} must be a whole number.") from exc
    if not low <= number <= high:
        raise SetupError(f"{label} must be between {low} and {high}.")
    return number


def _lines(value: object) -> list[str]:
    items = value.splitlines() if isinstance(value, str) else (value or [])
    return [str(item).strip() for item in items if str(item).strip()]


def _known_regions(regions: tuple[str, ...], written: list[str]) -> list[str]:
    """The market's regions a hand-written list names, compared the way the
    filter compares them, so "St.Gallen" is the "St. Gallen" checkbox."""
    keys = {_canton_key(str(w)) for w in written}
    return [r for r in regions if _canton_key(r) in keys]


REMOTE_CHOICES = ("none", "countries", "anywhere")


def _country_key(name: str) -> str:
    return name.strip().casefold()


def _countries(value: object) -> list[str]:
    """Country names from the form: a list, or text split on commas and
    lines, without blanks or repeats."""
    items = re.split(r"[,\n]", value) if isinstance(value, str) else (value or [])
    out, seen = [], set()
    for item in items:
        name = str(item).strip()
        if name and _country_key(name) not in seen:
            seen.add(_country_key(name))
            out.append(name)
    return out


def read_constraints(root: Path) -> dict:
    market = _market(root)
    c = Constraints.from_dict(_read_yaml(root / "config" / "constraints.yaml"))
    return {
        "regions": list(market.regions),
        "regions_word": market.regions_word,
        "country": market.country,
        "allowed_regions": _known_regions(market.regions, c.allowed_cantons),
        "remote": "anywhere" if c.remote_ok else ("countries" if c.remote_countries else "none"),
        # Shown as written, so a hand-typed "Schweiz" stays "Schweiz". Offered
        # pre-filled with the market's country when the list is empty.
        "remote_countries": [str(x) for x in c.remote_countries] or [market.country],
        "min_percentage": c.min_percentage,
        "max_percentage": c.max_percentage,
        "max_office_days": c.max_office_days_per_week,
        "dealbreakers": list(c.other_dealbreakers),
    }


def save_constraints(root: Path, form: dict) -> None:
    market = _market(root)
    chosen = set(form.get("allowed_regions") or [])
    regions = [r for r in market.regions if r in chosen]
    remote = form.get("remote") or "none"
    if remote not in REMOTE_CHOICES:
        raise SetupError(f"Unknown remote choice: {remote}.")
    remote_countries = _countries(form.get("remote_countries"))
    if remote == "countries" and not remote_countries:
        raise SetupError("Name at least one country for remote jobs, or pick another remote option.")
    if not chosen and remote == "none":
        raise SetupError(
            f"Choose at least one of the {market.regions_word}, or allow remote "
            f"roles. With neither, no posting can pass."
        )
    low = _int(form.get("min_percentage"), "Lowest workload", 10, 100)
    high = _int(form.get("max_percentage"), "Highest workload", 10, 100)
    if low > high:
        raise SetupError("The lowest workload is above the highest.")
    days = form.get("max_office_days")
    days = None if days in (None, "") else _int(days, "Office days", 0, 5)

    path = root / "config" / "constraints.yaml"
    data = _read_yaml(path)
    locations = data.get("locations") if isinstance(data.get("locations"), dict) else {}
    # An entry the form has no checkbox for ("Basel", which the filter reads
    # as both Basel cantons) was written by hand and is kept.
    known = {_canton_key(r) for r in market.regions}
    by_hand = [str(w) for w in locations.get("allowed_cantons") or [] if _canton_key(str(w)) not in known]
    # "Anywhere" supersedes the country list without clearing it, so switching
    # back later finds the countries still there. "None" empties it: a list
    # left in place would still let those countries' remote roles through.
    countries = [] if remote == "none" else remote_countries
    changes = [
        (("locations", "allowed_cantons"), regions + by_hand, _canton_key, _keep),
        (("locations", "remote_countries"), countries, _country_key, _keep),
        (("employment", "min_percentage"), low),
        (("employment", "max_percentage"), high),
        (("office_days", "max_per_week"), days),
        (("other_dealbreakers",), _lines(form.get("dealbreakers")), str, _keep),
    ]
    # An absent remote_ok already means false: only write the line when the
    # answer changes, so an untouched file stays byte for byte.
    if bool(locations.get("remote_ok")) != (remote == "anywhere"):
        changes.append((("locations", "remote_ok"), remote == "anywhere"))
    _save_yaml(path, data, changes, "Hard requirements: a posting that fails one is never scored.")
    Constraints.from_dict(_read_yaml(path))
    confirm_step(root, "constraints")


# ── Boards ──────────────────────────────────────────────────────────────────

OTHER_SECTION = "Other"


def _board_sections(config: Path, market: boards.Market) -> tuple[dict[str, str], list[str]]:
    """Each catalogue board's industry section, and the sections in display
    order. global.yaml and the market's file share one set of headings, so a
    global employer is listed under its industry beside the market's own: the
    UI makes no distinction between the two files. The market file is read
    first, so its order of industries leads; a section only global.yaml has
    follows, and boards under no heading come last, as "Other"."""
    section_of: dict[str, str] = {}
    order: list[str] = []
    for path in reversed(boards.catalog_files(config, market)):
        for key, section in boards.catalog_sections(path).items():
            section = section or OTHER_SECTION
            section_of.setdefault(key, section)
            if section not in order:
                order.append(section)
    order.sort(key=lambda name: name == OTHER_SECTION)  # stable: only moves Other last
    return section_of, order


def read_boards(root: Path) -> dict:
    """The catalogue grouped by section, with the user's selection marked."""
    market = _market(root)
    config = root / "config"
    try:
        entries = boards.read_catalog(config, market)
        selection = boards.load_companies(root, market)
    except boards.BoardsError as exc:
        raise SetupError(str(exc)) from exc
    section_of, order = _board_sections(config, market)
    chosen = {boards._key(b["name"]) for b in selection.boards if b["origin"] != "local"}
    groups: dict[str, list[dict]] = {name: [] for name in order}
    for entry in boards.index_catalog(entries).values():
        key = boards._key(entry["name"])
        groups.setdefault(section_of.get(key, OTHER_SECTION), []).append(
            {"name": entry["name"], "selected": key in chosen}
        )
    own = [b["name"] for b in selection.boards if b["origin"] == "local"]
    return {
        # Alphabetical within a section, so a global employer doesn't sort
        # ahead of the market's own just because its file is read first.
        "sections": [
            {"name": name, "boards": sorted(items, key=lambda b: b["name"].casefold())}
            for name, items in groups.items()
            if items
        ],
        "own": own,
        "problems": selection.errors + selection.warnings,
        "selected": len(selection.boards),
        "many": MANY_BOARDS,
        "first_run": not _has_run_before(root),
        **read_boards_sources(root),
    }


def read_boards_sources(root: Path) -> dict:
    """Whether the sources that need no board selection are on."""
    try:
        sources = load_search_settings(root).sources
    except ConfigError as exc:
        raise SetupError(str(exc)) from exc
    return {"web_search": sources.web_search.enabled, "eth_jobs": sources.eth_jobs.enabled}


def save_boards(root: Path, names: list[str], web_search: bool | None = None) -> int:
    """Make companies.yaml scan `names` from the catalogue, and switch the web
    search on or off in search.yaml (None leaves it as it is). Entries that
    carry their own ats and slug are the user's and stay as they are, except
    one for a catalogue board that was deselected."""
    market = _market(root)
    current_sources = read_boards_sources(root)
    web = current_sources["web_search"] if web_search is None else bool(web_search)
    config = root / "config"
    path = config / boards.COMPANIES_FILE
    try:
        catalog = boards.index_catalog(boards.read_catalog(config, market))
    except boards.BoardsError as exc:
        raise SetupError(str(exc)) from exc
    wanted = {boards._key(n) for n in names}
    unknown = sorted(n for n in names if boards._key(n) not in catalog)
    if unknown:
        raise SetupError(f"Not in the board list: {', '.join(unknown)}")
    data = _read_yaml(path)
    current = data.get("companies") if isinstance(data.get("companies"), list) else []

    def keep_entry(entry: dict) -> bool:
        key = boards._key(entry.get("name") or "")
        return key not in catalog or key in wanted

    # A board the user scans from their own ats and slug is already listed,
    # as that entry: its name must not be added a second time beside it.
    own = {boards._key(e.get("name") or "") for e in current if isinstance(e, dict) and keep_entry(e)}
    listed = [catalog[k]["name"] for k in catalog if k in wanted and k not in own]
    kept_own = [e for e in current if isinstance(e, dict) and keep_entry(e)]
    if not (listed or kept_own or web or current_sources["eth_jobs"]):
        raise SetupError("With no company selected and the web search off, a search would find nothing.")
    _save_yaml(
        path, data, [(("companies",), listed, boards._key, keep_entry)],
        "The company boards to scan, by the name config/boards/ gives them.",
    )
    if web != current_sources["web_search"]:
        search = root / "config" / "search.yaml"
        _save_yaml(
            search, _read_yaml(search), [(("sources", "web_search", "enabled"), web)],
            "How the search is steered; docs/SEARCH.md explains each value.",
        )
    confirm_step(root, "boards")
    return len(_read_yaml(path).get("companies") or [])


# ── Search dials ────────────────────────────────────────────────────────────


def read_search(root: Path) -> dict:
    try:
        settings = load_search_settings(root)
    except ConfigError as exc:
        raise SetupError(str(exc)) from exc
    return {
        "min_skill": settings.thresholds.min_skill,
        "min_interest": settings.thresholds.min_interest,
        "best_threshold": settings.thresholds.best_threshold,
        "max_best": settings.output.max_best,
        "max_okay": settings.output.max_okay,
        "rav": settings.rav.enabled,
    }


def search_warnings(form: dict) -> list[dict]:
    """The cost warnings for values typed into the form, before they are
    saved. Values that can't be read yet (a field mid-edit) give none."""
    try:
        thresholds = Thresholds(
            min_skill=int(form.get("min_skill")),
            min_interest=int(form.get("min_interest")),
            best_threshold=int(form.get("best_threshold")),
        )
        output = Output(max_best=int(form.get("max_best")), max_okay=int(form.get("max_okay")))
    except (TypeError, ValueError):
        return []
    return [{"key": w.key, "message": w.message} for w in cost_warnings(thresholds, output)]


def save_search(root: Path, form: dict) -> None:
    path = root / "config" / "search.yaml"
    values = {
        ("thresholds", "min_skill"): _int(form.get("min_skill"), "Skill floor", 0, 100),
        ("thresholds", "min_interest"): _int(form.get("min_interest"), "Interest floor", 0, 100),
        ("thresholds", "best_threshold"): _int(form.get("best_threshold"), "Best-match bar", 0, 100),
        ("output", "max_best"): _int(form.get("max_best"), "Strong matches per search", *MAX_BEST_RANGE),
        ("output", "max_okay"): _int(form.get("max_okay"), "Okay matches per search", *MAX_OKAY_RANGE),
        ("rav", "enabled"): bool(form.get("rav")),
    }
    # Checked on a copy first: a combination the search would refuse must not
    # reach the file.
    data = _read_yaml(path)
    trial = _read_yaml(path)
    for where, value in values.items():
        _set(trial, where, value)
    try:
        SearchSettings.from_dict(trial)
    except ConfigError as exc:
        raise SetupError(str(exc)) from exc
    # A dial left at its built-in default in a file that doesn't mention it
    # stays unmentioned, so the file keeps following the defaults.
    current = read_search(root)
    names = {"enabled": "rav"}
    changes = [
        (where, value) for where, value in values.items()
        if _get(data, where) is not None or current[names.get(where[-1], where[-1])] != value
    ]
    _save_yaml(path, data, changes, "How the search is steered; docs/SEARCH.md explains each value.")
    confirm_step(root, "search")


# ── Where setup stands ──────────────────────────────────────────────────────

STEPS = ("key", "cv", "identity", "constraints", "boards", "search")


def status(root: Path) -> dict:
    """Which setup steps are done. No network: the key is only checked for
    being there; `save_key` and the doctor are what prove it works."""
    state = _load_state(root)
    ran = _has_run_before(root)
    confirmed = set(state.get("confirmed", []))
    cvs = _cv_files(root)
    identity = _identity_path(root)
    done = {
        # A run that happened had credentials, wherever they came from
        # (`ant auth login` leaves nothing in .env).
        "key": ran or key_status(root)["set"],
        "cv": any(not _has_marker(p) for p in cvs),
        "identity": identity.exists() and not _has_marker(identity),
        # These three ship with values that parse, so only the user going
        # through the step, or a run having happened, says they are theirs.
        "constraints": ran or "constraints" in confirmed,
        "boards": ran or "boards" in confirmed,
        "search": ran or "search" in confirmed,
    }
    return {"steps": done, "ready": all(done.values()), "has_run": ran}
