# SPDX-License-Identifier: GPL-3.0-or-later
"""Dump the game data the inventory needs (read-only on the game; output under data/raw).

  lists/<bundle>/assets.xml       every asset (type, name, container, PathID) per faction/shared bundle
  runtimedata/                    startup bundle's RuntimeData.prefab: Unit/UnitModel/Weapon data tables
  dumps/<bundle>/unitmodels/...   MonoBehaviours on unit model prefabs (UnitModel, WeaponSlot, ...)
  dumps/<bundle>/weapons/...      everything on weapon prefabs (mesh renderers -> mesh PathIDs)
  dumps/<bundle>/controllers/...  AnimatorControllers (their clip lists)
"""
import glob
import os
import re
import xml.etree.ElementTree as ET

import fnmatch

from . import assetstudio

def faction_bundles(U):
    """The bundles that hold unit models (game profile unity.bundle_globs)."""
    return sorted(f for f in os.listdir(U.streaming) if any(fnmatch.fnmatch(f, g) for g in U.bundle_globs))


def bundle_key(bundle):
    return re.sub(r"(_assets_all)?\.bundle$", "", bundle)


_lists = {}


def read_list(U, bundle):
    """[(type, name, container, pathid)] from the bundle's asset list."""
    if bundle in _lists:
        return _lists[bundle]
    p = os.path.join(U.raw, "lists", bundle_key(bundle), "assets.xml")
    out = []
    for a in ET.parse(p).getroot():
        out.append((a.find("Type").text, a.find("Name").text or "", a.find("Container").text or "", int(a.find("PathID").text)))
    _lists[bundle] = out
    return out


def make_lists(U, log):
    for b in faction_bundles(U):
        out = os.path.join(U.raw, "lists", bundle_key(b))
        if os.path.exists(os.path.join(out, "assets.xml")):
            continue
        U.say(f"  asset list {b}")
        assetstudio.run(U, U.bundle_path(b), ["-m", "info", "--load-all", "--export-asset-list", "xml"], out, log)


def dump_runtimedata(U, log):
    out = os.path.join(U.raw, "runtimedata")
    if glob.glob(os.path.join(out, "UnitDataTable @*.txt")):
        return
    assetstudio.run(U, U.bundle_path(U.startup_bundle),
                    ["-m", "dump", "-t", "monoBehaviour", "--filter-by-container", "RuntimeData", "-g", "none",
                     "-f", "assetName_pathID"], out, log)


def dump_bundle(U, bundle, log):
    base = os.path.join(U.raw, "dumps", bundle_key(bundle))
    src = U.bundle_path(bundle)
    jobs = [
        ("unitmodels", ["-m", "dump", "-t", "monoBehaviour", "--filter-by-container", U.prefab_filter,
                        "-g", "containerFull", "-f", "assetName_pathID"]),
    ]
    ctrl = sorted({n for t, n, c, p in read_list(U, bundle) if t == "AnimatorController" and n})
    if ctrl:
        # dump mode cannot print AnimatorControllers; their raw bytes hold the clip PPtrs (scanned later)
        rx = "^(" + "|".join(re.escape(n) for n in ctrl) + ")$"
        jobs.append(("controllers", ["-m", "exportRaw", "--load-all", "--filter-by-name", rx, "--filter-with-regex",
                                     "-g", "none", "-f", "assetName_pathID"]))
    for sub, args in jobs:
        out = os.path.join(base, sub)
        done = os.path.join(out, ".done")
        if os.path.exists(done):
            continue
        try:
            U.say(f"  dump {bundle_key(bundle)} {sub}")
            assetstudio.run(U, src, args, out, log)
        except assetstudio.CLIError as e:
            if "Found [0/" not in str(e) and "No assets" not in str(e):
                raise
        open(done, "w").close()


def dump_all(U, log):
    make_lists(U, log)
    dump_runtimedata(U, log)
    for b in faction_bundles(U):
        dump_bundle(U, b, log)
