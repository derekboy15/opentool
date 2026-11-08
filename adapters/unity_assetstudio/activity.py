# SPDX-License-Identifier: GPL-3.0-or-later
"""Which objects of a prefab the game actually shows.

AssetStudio exports every GameObject of a prefab, including ones Unity keeps inactive (a spare weapon copy
under weapon_1_fake, the bayonet variant of a lasgun, the grenade that only appears during a throw) and meshes
whose renderer is disabled. This reads the raw serialized objects (`-m exportRaw --load-all`) and returns the
hierarchy paths of inactive GameObjects and of GameObjects whose (Skinned)MeshRenderer is disabled. Paths (root
first) disambiguate duplicate names: the held grenade's pull-ring and the belt grenades' rings are all called
BLA_GND_Ring. The pose/attach stages drop those objects and everything under them (loose items such as grenades
go to the belt instead).

Raw layouts (Unity 6):
  GameObject = int count, count x PPtr(int32 fileID, int64 pathID) components (Transform first), uint32 layer,
               string name (int32 length + bytes, 4-aligned), uint16 tag, bool isActive
  Transform  = PPtr m_GameObject, quat rotation, vec3 position, vec3 scale, vector<PPtr> children, PPtr father
  Renderer   = PPtr m_GameObject (12 bytes), uint8 m_Enabled, ...
"""
import glob
import os
import re
import shutil
import struct

from . import assetstudio, gamedump

_types = {}


def _bundle_types(U, bundle):
    if bundle not in _types:
        _types[bundle] = {p: (t, n) for t, n, c, p in gamedump.read_list(U, bundle)}
    return _types[bundle]


def _go(data):
    n = struct.unpack_from("<i", data, 0)[0]
    comps = [struct.unpack_from("<q", data, 4 + 12 * i + 4)[0] for i in range(n)]
    o = 4 + n * 12 + 4
    ln = struct.unpack_from("<i", data, o)[0]
    name = data[o + 4:o + 4 + ln].decode("utf-8", "replace")
    o = (o + 4 + ln + 3) & ~3
    return name, data[o + 2], comps


def _transform(data):
    go = struct.unpack_from("<q", data, 4)[0]
    o = 12 + 16 + 12 + 12
    n = struct.unpack_from("<i", data, o)[0]
    o += 4 + 12 * n
    father = struct.unpack_from("<q", data, o + 4)[0]
    return go, father


def prefab_activity(U, bundle, prefab_name, work_dir, log):
    """{"inactive": [[path]], "renderer_off": [[path]]}: GameObject paths (names, root first) of one prefab."""
    out = os.path.join(work_dir, "_raw_" + os.path.splitext(os.path.basename(prefab_name))[0])
    shutil.rmtree(out, ignore_errors=True)
    try:
        assetstudio.run(U, U.bundle_path(bundle), ["-m", "exportRaw", "--load-all", "--filter-by-container", prefab_name,
                                                    "-g", "none", "-f", "assetName_pathID"], out, log)
    except assetstudio.CLIError:
        return {"inactive": [], "renderer_off": [], "error": "raw export failed"}
    types = _bundle_types(U, bundle)
    gos, trs, inactive, off = {}, {}, [], []
    for f in glob.glob(os.path.join(out, "*.dat")):
        m = re.search(r"@(-?\d+)\.dat$", f)
        if not m:
            continue
        pid = int(m.group(1))
        t = types.get(pid, ("", ""))[0]
        data = open(f, "rb").read()
        try:
            if t == "GameObject":
                name, active, comps = _go(data)
                gos[pid] = (name, comps)
                if not active:
                    inactive.append(pid)
            elif t == "Transform":
                trs[pid] = _transform(data)
            elif t in ("MeshRenderer", "SkinnedMeshRenderer") and len(data) > 12 and data[12] == 0:
                off.append(struct.unpack_from("<q", data, 4)[0])
        except (struct.error, IndexError):
            continue
    shutil.rmtree(out, ignore_errors=True)
    tr_of = {go: tr for tr, (go, father) in trs.items()}

    def path(go):
        names, tr, seen = [], tr_of.get(go), set()
        if tr is None:
            return [gos[go][0]] if go in gos else []
        while tr in trs and tr not in seen:
            seen.add(tr)
            g, father = trs[tr]
            names.append(gos.get(g, ("?",))[0])
            tr = father
        return names[::-1]
    return {"inactive": [path(g) for g in inactive if g in gos], "renderer_off": [path(g) for g in off if g in gos]}
