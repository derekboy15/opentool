# SPDX-License-Identifier: GPL-3.0-or-later
"""AssetStudioModCLI wrapper. Reads game bundles, writes only under the workspace.

The CLI takes one input path (a file or a folder). Assets that span bundles (a Ultramarines unit using
Blood Angels clips, a weapon prefab in imperiumshared whose mesh is in a faction bundle) are exported
from a staging folder holding copies of just the bundles needed (<workspace>/<game>/staging, safe to delete).
"""
import os
import re
import shutil
import subprocess
import threading
import time


_locks, _locks_guard = {}, threading.Lock()


def lock_for(key):
    """One lock per staging folder / split bundle, so parallel unit tasks never copy or split twice."""
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class CLIError(RuntimeError):
    pass


def run(U, inp, args, out_dir, log_path=None, timeout=3600):
    """Run the CLI on `inp` (bundle file or folder). Returns the cleaned log text. U: the Unity context."""
    if U.is_in_game_folder(out_dir):
        raise CLIError("refusing to write into the game folder")
    os.makedirs(out_dir, exist_ok=True)
    cmd = [U.cli, inp, *args, "-o", out_dir]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    text = _ANSI.sub("", (r.stdout or "") + (r.stderr or ""))
    text = re.sub(r"(\[\d{3}%\])+", "", text)
    text = re.sub(r"(Exported \[\d+/\d+\])+", "", text)
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"$ {' '.join(cmd)}\n{text}\n({time.time() - t0:.0f}s, exit {r.returncode})\n\n")
    if r.returncode != 0 or "[Error]" in text:
        raise CLIError(f"AssetStudioModCLI failed ({r.returncode}): {text[-800:]}")
    return text


def stage_bundles(U, bundles, key):
    """Folder with copies of the given game bundles (by file name). Reused if already complete."""
    with lock_for("stage:" + key):
        return _stage_bundles(U, bundles, key)


def _stage_bundles(U, bundles, key):
    d = os.path.join(U.staging, key)
    os.makedirs(d, exist_ok=True)
    want = set(bundles)
    for f in os.listdir(d):
        if f not in want:
            os.remove(os.path.join(d, f))
    for b in bundles:
        src, dst = U.bundle_path(b), os.path.join(d, b)
        if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
            shutil.copyfile(src, dst + ".part")
            os.replace(dst + ".part", dst)
    return d


def input_for(U, bundles, key):
    """CLI input path for a set of bundles: the game file itself for one bundle, else a staging folder."""
    bundles = sorted(set(bundles))
    if len(bundles) == 1:
        return U.bundle_path(bundles[0])
    return stage_bundles(U, bundles, key)
