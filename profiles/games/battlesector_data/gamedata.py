# SPDX-License-Identifier: GPL-3.0-or-later
"""Join the game's own data into one structure (no guessing where the game says it).

Sources (see gamedump.py): RuntimeData tables (units, unit models, weapons, weapon assets), the
Addressables catalog (AssetReference GUID -> prefab path + bundle), MonoBehaviours on unit model prefabs
(UnitModel: unit/model ids, member mesh index, weapon slots; WeaponSlot: slot number + joint), the asset
lists (PathID -> name/type/container/bundle) and raw AnimatorControllers (clip PPtrs).
"""
import glob
import os
import re
import struct
from collections import defaultdict

from adapters.unity_assetstudio import catalog as catalog_mod
from adapters.unity_assetstudio import gamedump
from adapters.unity_assetstudio.typetree import parse_file

PREFAB_RX = re.compile(r"Assets/_Prefabs/UnitModels/Built/([^/]+)/([^/]+)\.prefab")


def table(U, name):
    f = glob.glob(os.path.join(U.raw, "runtimedata", f"{name} @*.txt"))
    t = parse_file(f[0])
    return dict(zip(t["keys"], t["values"]))


class GameData:
    def __init__(self, U):
        self.U = U
        self.units = table(U, "UnitDataTable")
        self.models = table(U, "UnitModelDataTable")
        self.weapons = table(U, "WeaponDataTable")
        self.weapon_assets = table(U, "WeaponAssetsDataTable")
        self.cat = catalog_mod.Catalog(U.catalog)
        self._index_lists()
        self._index_prefabs()
        self._index_controllers()

    # ------------------------------------------------------------------ asset lists
    def _index_lists(self):
        self.by_pathid = {}                       # pathid -> (type, name, container, bundle)
        self.by_container = defaultdict(list)     # container -> [(type, name, pathid, bundle)]
        self.meshes_by_name = defaultdict(list)   # mesh name -> [(container, bundle, pathid)]
        self.bundles = gamedump.faction_bundles(self.U)
        for b in self.bundles:
            for t, n, c, p in gamedump.read_list(self.U, b):
                self.by_pathid.setdefault(p, (t, n, c, b))
                if c:
                    self.by_container[c].append((t, n, p, b))
                if t == "Mesh":
                    self.meshes_by_name[n].append((c, b, p))

    # ------------------------------------------------------------------ unit model prefabs
    def _index_prefabs(self):
        """UnitModel / WeaponSlot components per prefab path."""
        self.prefabs = {}
        for b in self.bundles:
            root = os.path.join(self.U.raw, "dumps", gamedump.bundle_key(b), "unitmodels")
            for f in glob.glob(os.path.join(root, "**", "*.txt"), recursive=True):
                rel = os.path.relpath(os.path.dirname(f), root).replace("\\", "/")
                prefab = rel + ".prefab" if not rel.endswith(".prefab") else rel
                with open(f, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(4000)
                if "int unitType" not in head and "int weaponSlotNumber" not in head:
                    continue
                d = parse_file(f)
                pid = int(re.search(r"@(-?\d+)\.txt$", f).group(1))
                p = self.prefabs.setdefault(prefab, {"bundle": b, "unit_model": None, "slots": {}})
                if "unitType" in d:
                    p["unit_model"] = {"unit_type": d["unitType"], "model_type": d["modelType"],
                                       "member_mesh_index": d.get("unitMemberMeshIndex", 0),
                                       "slot_ids": [s["m_PathID"] for s in d.get("weaponSlots", [])]}
                else:
                    go = self.by_pathid.get(d["m_GameObject"]["m_PathID"])
                    p["slots"][pid] = {"slot": d["weaponSlotNumber"], "joint": go[1] if go else None}

    def prefab_info(self, prefab):
        p = self.prefabs.get(prefab)
        if not p or not p["unit_model"]:
            return None
        um = p["unit_model"]
        slots = [p["slots"].get(s) for s in um["slot_ids"]]
        return {**um, "weapon_joints": {s["slot"]: s["joint"] for s in slots if s}, "bundle": p["bundle"]}

    # ------------------------------------------------------------------ animator controllers
    def _index_controllers(self):
        clip_ids = {p for p, v in self.by_pathid.items() if v[0] == "AnimationClip"}
        self.controller_clips = {}  # controller name -> [(clip name, pathid, bundle)]
        for b in self.bundles:
            for f in glob.glob(os.path.join(self.U.raw, "dumps", gamedump.bundle_key(b), "controllers", "*.dat")):
                name = os.path.basename(f).rsplit(" @", 1)[0]
                data = open(f, "rb").read()
                seen, clips = set(), []
                for o in range(0, len(data) - 7):
                    v = struct.unpack_from("<q", data, o)[0]
                    if v in clip_ids and v not in seen:
                        seen.add(v)
                        t, n, c, cb = self.by_pathid[v]
                        clips.append((n, v, cb))
                self.controller_clips.setdefault(name, clips)

    def _siblings(self, prefab):
        """Other ModelNN prefabs of the same unit (asset lists file shared objects under one of them)."""
        stem = re.sub(r"Model\d+\.prefab$", "", prefab)
        return [c for c in self.prefab_paths if c.startswith(stem) and re.match(r"Model\d+\.prefab$", c[len(stem):])]

    @property
    def prefab_paths(self):
        if not hasattr(self, "_prefab_paths"):
            self._prefab_paths = sorted(c for c in self.by_container if PREFAB_RX.match(c))
        return self._prefab_paths

    def prefab_controller(self, prefab):
        """AnimatorController of a unit prefab: in its container, else named like the unit, else a sibling's."""
        names = [n for t, n, p, b in self.by_container.get(prefab, []) if t == "AnimatorController"]
        if names:
            return names[0]
        unit = re.sub(r"Model\d+$", "", os.path.splitext(os.path.basename(prefab))[0])
        if unit in self.controller_clips:
            return unit
        for s in self._siblings(prefab):
            names = [n for t, n, p, b in self.by_container.get(s, []) if t == "AnimatorController"]
            if names:
                return names[0]
        cands = [c for c in self.controller_clips if unit.startswith(c) or c.startswith(unit)]
        return max(cands, key=len) if cands else None

    def prefab_bones(self, prefab):
        """GameObject names of the prefab (and its sibling variants: shared rigs are filed under one)."""
        names = set()
        for c in [prefab] + self._siblings(prefab):
            names |= {n for t, n, p, b in self.by_container.get(c, []) if t == "GameObject"}
        return names

    def prefab_animators(self, prefab):
        return [n for t, n, p, b in self.by_container.get(prefab, []) if t == "Animator"]

    def prefab_meshes(self, prefab):
        return [(n, p, b) for t, n, p, b in self.by_container.get(prefab, []) if t == "Mesh"]

    # ------------------------------------------------------------------ weapons
    def weapon_prefab(self, wid):
        a = self.weapon_assets.get(wid)
        if not a:
            return None
        guid = a["PrefabReference"]["m_AssetGUID"]
        res = self.cat.asset(guid) if guid else None
        return {"guid": guid, "path": res[0], "bundle": res[1][0] if res[1] else None} if res else None

    def weapon_meshes(self, prefab_path):
        """Mesh candidates for a weapon prefab: Mesh assets filed under the prefab, and GameObjects named like a
        mesh (case-insensitive; the model asset may live in another bundle). [] = the weapon has no model of
        its own (it is modelled into the unit's body, e.g. dreadnought arms, terminator claws)."""
        name = os.path.splitext(os.path.basename(prefab_path))[0]
        entries = self.by_container.get(prefab_path, [])
        cands = [{"mesh": n, "container": prefab_path, "bundle": b, "pathid": p, "via": "prefab"}
                 for t, n, p, b in entries if t == "Mesh"]
        for g in (n for t, n, p, b in entries if t == "GameObject" and n != name):
            for key in {g, g.lower()}:
                for c, b, p in self.meshes_by_name_ci.get(key.lower(), []):
                    cands.append({"mesh": c[1], "container": c[0], "bundle": b, "pathid": p, "via": "model"})
        seen, out = set(), []
        for c in cands:
            if c["pathid"] not in seen:
                seen.add(c["pathid"])
                out.append(c)

        def score(m):  # containers overlap between prefabs sharing objects: rank by name similarity
            a, s = name.lower(), m["mesh"].lower()
            lcs = max((len(x) for x in _common_substrings(a, s)), default=0)
            return (-lcs, len(s) - lcs, m["via"] != "model")  # closest name, then fewest extra characters
        out.sort(key=score)
        return out

    @property
    def meshes_by_name_ci(self):
        if not hasattr(self, "_mbn_ci"):
            self._mbn_ci = defaultdict(list)
            for n, lst in self.meshes_by_name.items():
                for c, b, p in lst:
                    self._mbn_ci[n.lower()].append(((c, n), b, p))
        return self._mbn_ci

    def model_prefabs(self, mid):
        m = self.models.get(mid)
        out = []
        for v in (m or {}).get("ModelVariations", []):
            res = self.cat.asset(v["m_AssetGUID"]) if v["m_AssetGUID"] else None
            if res and "Debug" not in os.path.basename(res[0]):  # BloodAngelsDebugModel00: test model
                out.append({"guid": v["m_AssetGUID"], "path": res[0], "bundle": res[1][0] if res[1] else None})
        return out


def _common_substrings(a, b, min_len=4):
    out = []
    for i in range(len(a)):
        for j in range(i + min_len, len(a) + 1):
            if a[i:j] in b:
                out.append(a[i:j])
    return out
