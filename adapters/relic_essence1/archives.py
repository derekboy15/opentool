# SPDX-License-Identifier: GPL-3.0-or-later
"""Relic Essence 1 archives: the .module load order, and unpacking with relic-tool-sga (read-only on the game).

A .module lists its archives (ArchiveFile.N, first = highest priority) and the mods it requires
(RequiredMod.N, searched after it). Placeholders: %LOCALE% (locale archives are skipped), %TEXTURE-LEVEL% = Full,
%MODEL-LEVEL% = High, %SOUND-LEVEL% (sound archives are skipped). Engine archives come last.
Each archive is unpacked to <workspace>/<game>/unpacked/<Folder>__<archive>/ so provenance survives; the game
files' size and mtime are recorded before and after and the run fails loudly if any changed.
"""
import json
import os
import re
import subprocess
import time

SKIP = re.compile(r"(?i)sound|speech|music|loc$|keys$|-whm-(low|medium)$")
LEVELS = {"%TEXTURE-LEVEL%": "Full", "%MODEL-LEVEL%": "High", "%SOUND-LEVEL%": "Full"}


def read_module(path):
    out = {"archives": [], "required": [], "folder": None}
    with open(path, encoding="latin-1") as fh:
        text = fh.read()
    for line in text.splitlines():
        line = line.split(";;")[0].strip()
        if "=" not in line:
            continue
        k, v = (x.strip() for x in line.split("=", 1))
        kl = k.lower()
        if kl == "modfolder":
            out["folder"] = v
        elif kl.startswith("archivefile."):
            out["archives"].append((int(k.split(".")[1]), v))
        elif kl.startswith("requiredmod."):
            out["required"].append((int(k.split(".")[1]), v))
    out["archives"] = [v for _, v in sorted(out["archives"])]
    out["required"] = [v for _, v in sorted(out["required"])]
    return out


def archive_order(install, module, engine=("Engine-New", "Engine"), seen=None):
    """[(path to .sga, tag)] in load order for a module file name (e.g. WXP.module), required mods after it."""
    seen = seen if seen is not None else set()
    mp = os.path.join(install, module)
    if not os.path.exists(mp):
        raise FileNotFoundError(f"no {module} in {install}")
    m = read_module(mp)
    out = []
    folder = m["folder"] or os.path.splitext(module)[0]
    for a in m["archives"]:
        if "%LOCALE%" in a:
            continue
        for k, v in LEVELS.items():
            a = a.replace(k, v)
        if SKIP.search(a):
            continue
        p = os.path.join(install, folder, a + ".sga")
        if os.path.exists(p) and p.lower() not in seen:
            seen.add(p.lower())
            out.append((p, f"{folder}__{a}"))
    for req in m["required"]:
        out += archive_order(install, req + ".module", engine=(), seen=seen)
    for e in engine:
        p = os.path.join(install, "Engine", e + ".sga")
        if os.path.exists(p) and p.lower() not in seen:
            seen.add(p.lower())
            out.append((p, f"Engine__{e}"))
    return out


def relic_exe(relic_python):
    d = os.path.dirname(relic_python)
    for c in (os.path.join(d, "relic.exe"), os.path.join(d, "Scripts", "relic.exe"), os.path.join(d, "relic")):
        if os.path.exists(c):
            return c
    raise FileNotFoundError(f"no relic.exe next to {relic_python} (pip install relic-tool-sga-v2 in that venv)")


def unpack(ctx, archives, dest_root):
    """Unpack each archive once (skips folders already complete). Returns [(folder, tag)] in load order."""
    exe = relic_exe(ctx.reader("relic_python"))
    os.makedirs(dest_root, exist_ok=True)
    log_path = os.path.join(dest_root, "unpack_log.json")
    log = json.load(open(log_path)) if os.path.exists(log_path) else {"archives": {}}
    before = {p: (os.path.getsize(p), os.path.getmtime(p)) for p, _ in archives}
    roots = []
    for i, (p, tag) in enumerate(archives, 1):
        dest = os.path.join(dest_root, tag)
        rec = log["archives"].get(tag)
        if rec and rec.get("status") == "ok" and os.path.isdir(dest) and rec.get("size") == before[p][0]:
            roots.append((dest, tag))
            continue
        if os.path.isdir(dest) and os.listdir(dest):
            raise RuntimeError(f"{dest} is not empty but not recorded as complete: move it away and rerun")
        t0 = time.time()
        ctx.say(f"  unpacking {i}/{len(archives)} {os.path.basename(p)}")
        r = subprocess.run([exe, "sga", "unpack", "--merge", p, dest, "--loglevel", "warning"], capture_output=True,
                           text=True, cwd=dest_root)
        files = sum(len(f) for _, _, f in os.walk(dest)) if os.path.isdir(dest) else 0
        err = "\n".join(l for l in (r.stderr + r.stdout).splitlines() if "pkg_resources" not in l and "declare_namespace" not in l)
        log["archives"][tag] = {"order": i, "archive": os.path.basename(p), "status": "ok" if r.returncode == 0 else "FAILED",
                                "files": files, "seconds": round(time.time() - t0, 1), "size": before[p][0],
                                "stderr": err[-1500:]}
        with open(log_path, "w") as f:
            json.dump(log, f, indent=1)
        if r.returncode != 0:
            raise RuntimeError(f"relic sga unpack failed on {os.path.basename(p)}: {err[-800:]}")
        roots.append((dest, tag))
    after = {p: (os.path.getsize(p), os.path.getmtime(p)) for p, _ in archives}
    if before != after:
        raise RuntimeError("a game archive changed while unpacking (it must only be read)")
    return roots
