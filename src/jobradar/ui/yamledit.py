"""Change values in a YAML file and leave the rest of its text alone.

The config files are documentation as much as data: the shipped ones explain
every key in comments, and people add their own ("# ~30 min by train"). A
form that loaded the file and dumped it again would delete all of that, which
is not acceptable for files that are edited by hand just as often.

So the two edits the forms need are done on the text, line by line:

- ``set_scalar``: replace the value after ``key:``, keeping a trailing comment;
- ``set_list``: drop the list items no longer wanted, append the new ones, and
  leave every other line (kept items, their comments, the comment rules
  between them) as it was.

This is not a YAML editor. It handles the shapes these files have: block
mappings, block lists of names, a list written inline as ``[a, b]``. Anything
else raises ``Unsupported``, and the caller checks every result by parsing it,
so an edit that didn't produce the intended values is never written.
"""

from __future__ import annotations

import re
from typing import Callable

import yaml

_KEY = r"^(?P<indent>[ ]*)(?P<key>{key})[ ]*:(?P<rest>.*)$"
_ITEM = re.compile(r"^(?P<indent>[ ]*)-(?:[ ]+(?P<rest>.*))?$")
# A scalar value up to an optional trailing comment. Quoted values may hold
# a "#"; unquoted ones end at " #".
_VALUE = re.compile(r"""^[ ]*(?P<value>"[^"]*"|'[^']*'|[^#\s][^#]*?|)(?P<comment>[ ]+#.*)?[ ]*$""")


class Unsupported(Exception):
    """The file has a shape this module doesn't edit."""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _block_end(lines: list[str], start: int, indent: int, limit: int) -> int:
    """Index after the last line belonging to the key on line `start - 1`:
    everything up to the next content line indented no deeper than `indent`.
    Blank lines and comments just before that line belong to what follows."""
    end = start
    for i in range(start, limit):
        if _is_content(lines[i]):
            if _indent(lines[i]) <= indent:
                break
            end = i + 1
    return end


def _find(lines: list[str], path: tuple[str, ...]) -> tuple[int, int, int] | None:
    """(line of the key, its indent, end of its block), or None if any part
    of the path is missing."""
    start, limit, parent_indent = 0, len(lines), -1
    found = None
    for key in path:
        pattern = re.compile(_KEY.format(key=re.escape(key)))
        child_indent = next(
            (_indent(lines[i]) for i in range(start, limit) if _is_content(lines[i])), None
        )
        if child_indent is None or child_indent <= parent_indent:
            return None
        at = next(
            (i for i in range(start, limit)
             if _indent(lines[i]) == child_indent and pattern.match(lines[i])),
            None,
        )
        if at is None:
            return None
        end = _block_end(lines, at + 1, child_indent, limit)
        found = (at, child_indent, end)
        start, limit, parent_indent = at + 1, end, child_indent
    return found


def _scalar(value: object) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10_000).strip()
    return text.removesuffix("\n...").strip()


def _insert_key(lines: list[str], path: tuple[str, ...], rendered: list[str]) -> list[str]:
    """Add a key that isn't in the file yet, under its parent if that exists.
    `rendered` is the key's own lines, unindented."""
    if len(path) == 1:
        tail = [] if not lines or not lines[-1].strip() else [""]
        return lines + tail + rendered
    parent = _find(lines, path[:-1])
    if parent is None:
        if len(path) != 2:
            raise Unsupported(f"{'.'.join(path[:-1])} is missing")
        tail = [] if not lines or not lines[-1].strip() else [""]
        return lines + tail + [f"{path[0]}:"] + ["  " + line for line in rendered]
    at, indent, end = parent
    parts = _VALUE.match(re.match(_KEY.format(key=re.escape(path[-2])), lines[at])["rest"])
    if parts is None or parts["value"]:
        raise Unsupported(f"{'.'.join(path[:-1])} is not a section")
    child = next((_indent(lines[i]) for i in range(at + 1, end) if _is_content(lines[i])), indent + 2)
    return lines[:end] + [" " * child + line for line in rendered] + lines[end:]


def set_scalar(text: str, path: tuple[str, ...], value: object) -> str:
    """`text` with the value at `path` replaced by `value`."""
    lines = text.splitlines()
    found = _find(lines, path)
    if found is None:
        return "\n".join(_insert_key(lines, path, [f"{path[-1]}: {_scalar(value)}"])) + "\n"
    at, indent, end = found
    if any(_is_content(lines[i]) for i in range(at + 1, end)):
        raise Unsupported(f"{'.'.join(path)} holds more than one value")
    match = re.match(_KEY.format(key=re.escape(path[-1])), lines[at])
    parts = _VALUE.match(match["rest"])
    if parts is None:
        raise Unsupported(f"{'.'.join(path)}: can't read the line")
    lines[at] = f"{' ' * indent}{path[-1]}: {_scalar(value)}{parts['comment'] or ''}"
    return "\n".join(lines) + "\n"


def set_list(
    text: str,
    path: tuple[str, ...],
    wanted: list[str],
    *,
    same: Callable[[str], str] = str,
    keep_entry: Callable[[dict], bool] = lambda entry: True,
) -> str:
    """`text` with the list at `path` holding the names in `wanted`.

    Items already there stay on their lines, in their order, with their
    comments. Items not in `wanted` are removed; names not yet there are
    appended. `same` maps a name to what it is compared by (case, spacing).
    An item that is a mapping rather than a name (a board with its own ats
    and slug) is kept or dropped by `keep_entry`, never rewritten.
    """
    lines = text.splitlines()
    found = _find(lines, path)
    if found is None:
        rendered = [f"{path[-1]}:"] + [f"  - {_scalar(name)}" for name in wanted]
        if not wanted:
            rendered = [f"{path[-1]}: []"]
        return "\n".join(_insert_key(lines, path, rendered)) + "\n"
    at, indent, end = found
    match = re.match(_KEY.format(key=re.escape(path[-1])), lines[at])
    parts = _VALUE.match(match["rest"])
    inline = parts["value"] if parts else None
    if parts is None or (inline and not (inline.startswith("[") or inline in ("null", "~"))):
        raise Unsupported(f"{'.'.join(path)} is not a list")
    comment = parts["comment"] or ""

    # Split the block into items: each starts at a "- " line of the list's own
    # indent and runs over the deeper lines after it (a mapping's other keys).
    item_indent = next(
        (_indent(lines[i]) for i in range(at + 1, end) if _is_content(lines[i])), None
    )
    items: list[tuple[int, int, object]] = []  # (first line, line after the last, value)
    i = at + 1
    while i < end:
        if not _is_content(lines[i]):
            i += 1
            continue
        if _indent(lines[i]) != item_indent or not _ITEM.match(lines[i]):
            raise Unsupported(f"{'.'.join(path)} is not a plain list")
        stop = i + 1
        for j in range(i + 1, end):
            if _is_content(lines[j]):
                if _indent(lines[j]) <= item_indent:
                    break
                stop = j + 1
        try:
            value = yaml.safe_load("\n".join(lines[i:stop]))[0]
        except (yaml.YAMLError, TypeError, IndexError, KeyError) as exc:
            raise Unsupported(f"{'.'.join(path)}: can't read an item") from exc
        items.append((i, stop, value))
        i = stop

    if inline and inline.startswith("["):
        if items:
            raise Unsupported(f"{'.'.join(path)} is written two ways at once")
        existing = yaml.safe_load(inline) or []
        if not all(isinstance(x, str) for x in existing):
            raise Unsupported(f"{'.'.join(path)}: an inline list of more than names")
        # An inline list has no per-item comments to lose: keep its order,
        # drop and append, and write it inline again.
        keys = {same(name) for name in wanted}
        kept = [x for x in existing if same(x) in keys]
        have = {same(x) for x in kept}
        new = kept + [name for name in wanted if same(name) not in have]
        flow = yaml.safe_dump(new, default_flow_style=True, allow_unicode=True, width=10_000).strip()
        lines[at] = f"{' ' * indent}{path[-1]}: {flow}{comment}"
        return "\n".join(lines) + "\n"

    keys = {same(name) for name in wanted}
    drop: set[int] = set()
    have: set[str] = set()
    remaining = 0
    for first, stop, value in items:
        if isinstance(value, dict):
            keep = keep_entry(value)
        else:
            key = same(str(value))
            keep = key in keys and key not in have
            have.add(key)
        if keep:
            remaining += 1
        else:
            drop.update(range(first, stop))
    added = [name for name in wanted if same(name) not in have]
    pad = " " * (item_indent if item_indent is not None and items else indent + 2)
    new_lines = [f"{pad}- {_scalar(name)}" for name in added]
    # New names go after the last item, or where the block ends if it has none
    # (a list whose every entry is commented out, as the template ships).
    insert_at = items[-1][1] if items else end
    empty = remaining + len(new_lines) == 0
    lines[at] = f"{' ' * indent}{path[-1]}:{' []' if empty else ''}{comment}"
    out = [
        line for n, line in enumerate(lines[:insert_at]) if n not in drop
    ] + new_lines + lines[insert_at:]
    return "\n".join(out) + "\n"
