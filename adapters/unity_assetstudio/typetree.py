# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse AssetStudio `-m dump` text (an indented Unity typetree) into Python dicts/lists.

    MonoBehaviour Base
        string m_Name = "UnitDataTable"
        vector keys
            Array Array
            int size = 2
                [0]
                int data = 1006
    ...

Leaves become values (int/float/str), structs become dicts, arrays become lists.
PPtr structs stay dicts ({"m_FileID": .., "m_PathID": ..}).
"""
import re

_LEAF = re.compile(r"^(.*?) (\S+) = (.*)$")


def _value(raw):
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _lines(text):
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        d = len(line) - len(line.lstrip("\t"))
        out.append((d, line.strip()))
    return out


def _parse(lines, i, depth):
    """Parse the node at lines[i] (which sits at `depth`). Returns (name, value, next_index)."""
    d, s = lines[i]
    m = _LEAF.match(s)
    if m and not s.endswith(" Array"):
        return m.group(2), _value(m.group(3)), i + 1
    name = s.split(" ")[-1]
    i += 1
    if i < len(lines) and lines[i][0] == depth + 1 and lines[i][1] == "Array Array":
        i += 2  # "Array Array", "int size = N"
        items = []
        while i < len(lines) and lines[i][0] >= depth + 2:
            if lines[i][1].startswith("[") and lines[i][1].endswith("]"):
                i += 1
                _, v, i = _parse(lines, i, lines[i][0])
                items.append(v)
            else:
                i += 1  # unexpected line inside an array: skip
        return name, items, i
    node = {}
    while i < len(lines) and lines[i][0] > depth:
        k, v, i = _parse(lines, i, lines[i][0])
        node[k] = v
    return name, node, i


def parse_dump(text):
    """Whole dump file -> dict of the root object's fields."""
    lines = _lines(text)
    _, root, _ = _parse(lines, 0, lines[0][0])
    return root


def parse_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_dump(f.read())
