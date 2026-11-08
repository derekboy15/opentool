# SPDX-License-Identifier: GPL-3.0-or-later
"""Source 1 file system, read-only: gameinfo.txt search paths and VPK directory listings (no file contents).

The game's own mount order comes from gameinfo.txt (FileSystem/SearchPaths, "Game" entries, first wins;
|gameinfo_path| = the folder holding gameinfo.txt). Each search path is a folder that may hold loose files and a
pak01_dir.vpk. A VPK directory (v1 or v2) is a tree of extension -> folder -> file name entries; only the names are
needed here (the Blender step reads the files themselves through SourceIO's own mounting).
"""
import os
import re
import struct

VPK_SIG = 0x55AA1234


def _kv_tokens(text):
    for m in re.finditer(r'"([^"]*)"|([{}])|([^\s"{}]+)', re.sub(r"//[^\n]*", "", text)):
        yield m.group(1) if m.group(1) is not None else (m.group(2) or m.group(3))


def search_paths(game_dir):
    """Folders in mount order (absolute) for the gameinfo.txt in game_dir; only folders that exist."""
    gi = os.path.join(game_dir, "gameinfo.txt")
    with open(gi, encoding="utf-8", errors="replace") as f:
        toks = list(_kv_tokens(f.read()))
    base = os.path.dirname(os.path.abspath(game_dir))  # where the game's exe lives
    out, i, depth, inside = [], 0, 0, None
    while i < len(toks):
        t = toks[i]
        if t == "{":
            depth += 1
        elif t == "}":
            depth -= 1
            if inside is not None and depth < inside:
                inside = None
        elif t.lower() == "searchpaths" and i + 1 < len(toks) and toks[i + 1] == "{":
            inside = depth + 1
        elif inside is not None and depth == inside and i + 1 < len(toks):
            key, val = t, toks[i + 1]
            i += 1
            if "game" in key.lower().split("+"):
                p = val.replace("|gameinfo_path|", os.path.abspath(game_dir) + os.sep)
                p = os.path.normpath(p if os.path.isabs(p) else os.path.join(base, p))
                if os.path.isdir(p) and p.lower() not in {x.lower() for x in out}:
                    out.append(p)
        i += 1
    return out


def _cstr(f):
    b = bytearray()
    while (c := f.read(1)) not in (b"\0", b""):
        b += c
    return b.decode("utf-8", errors="replace")


def vpk_names(path):
    """Paths ('models/survivors/survivor_gambler.mdl', lower case, forward slashes) listed in a *_dir.vpk."""
    out = []
    with open(path, "rb") as f:
        sig, ver, tree = struct.unpack("<III", f.read(12))
        if sig != VPK_SIG or ver not in (1, 2):
            raise ValueError(f"{os.path.basename(path)}: not a VPK v1/v2 directory")
        if ver == 2:
            f.read(16)
        while (ext := _cstr(f)):
            while (folder := _cstr(f)):
                while (name := _cstr(f)):
                    _crc, preload = struct.unpack("<IH", f.read(6))
                    f.read(12)  # archive index, offset, length, terminator
                    f.seek(preload, 1)
                    d = "" if folder.strip() == "" else folder.strip("/") + "/"
                    out.append((d + name + "." + ext).lower())
    return out


def list_files(game_dir, prefixes=("models/",), suffix=".mdl"):
    """{path: source folder name} over every search path, first mount wins (loose files before that folder's VPK)."""
    seen = {}
    for sp in search_paths(game_dir):
        tag = os.path.basename(sp)
        loose = []
        for pre in prefixes:
            root = os.path.join(sp, *pre.strip("/").split("/"))
            for dp, _, fs in os.walk(root):
                for fn in fs:
                    if fn.lower().endswith(suffix):
                        loose.append(os.path.relpath(os.path.join(dp, fn), sp).replace(os.sep, "/").lower())
        packed = []
        d = os.path.join(sp, "pak01_dir.vpk")
        if os.path.exists(d):
            packed = [p for p in vpk_names(d) if p.endswith(suffix) and p.startswith(tuple(prefixes))]
        for p in loose:
            seen.setdefault(p, f"{tag} (loose)")
        for p in packed:
            seen.setdefault(p, f"{tag}/pak01_dir.vpk")
    return seen
