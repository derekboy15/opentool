# SPDX-License-Identifier: GPL-3.0-or-later
"""Unity adapter on AssetStudioModCLI (aelurum's AssetStudio fork; MIT; the user downloads it).

Game profile "unity": data_dir_glob, catalog (Addressables catalog.bin), bundle_globs (bundles with unit models),
startup_bundle (game-data tables), prefab_root, fps, metres_per_unit; "data_module": the Python module under
profiles/games/ that turns the game's own data tables into an inventory (units, models, weapons, clips) -
game data layouts are per game, the Unity reading (lists, dumps, catalog, typetree, prefab activity, Animator
export, weapon split) is shared here.

list:   dump asset lists + data tables + prefab components + controllers once (raw/), build the inventory,
        write units.json (loadouts with joint mounts on the default body model).
export: Animator FBX with bound clips (multi-bundle staging when clips live in other bundles), each weapon's
        FBX + normal map, prefab activity (objects the game keeps hidden) -> package.
"""
import ctypes
import glob
import importlib
import json
import os
import re

from opentool import CONTRACT_VERSION

from . import extract, gamedump
from .package import ADAPTER, write_package


class Unity:
    """Where things are for one Unity game (install read-only, everything else in the workspace)."""

    def __init__(self, ctx):
        P = ctx.profile["unity"]
        inst = ctx.game["install"]
        dd = sorted(glob.glob(os.path.join(inst, P.get("data_dir_glob", "*_Data"))))
        if not dd:
            raise FileNotFoundError(f"no {P.get('data_dir_glob', '*_Data')} folder in {inst}: not a Unity game install?")
        self.install = inst
        self.data_dir = dd[0]
        self.streaming = os.path.join(self.data_dir, P.get("streaming", "StreamingAssets"))
        self.catalog = os.path.join(self.data_dir, P["catalog"])
        cli = ctx.reader("assetstudio_cli")
        if os.path.isdir(cli):
            hits = glob.glob(os.path.join(cli, "**", "AssetStudioModCLI.exe"), recursive=True)
            if not hits:
                raise FileNotFoundError(f"no AssetStudioModCLI.exe under {cli}")
            cli = hits[0]
        self.cli = cli
        g = ctx.gdir
        if len(os.path.abspath(g)) > 60:  # AssetStudio (.NET 4.7.2) writes prefab paths ~190 characters deep
            raise RuntimeError(f"workspace path too long for AssetStudio's 260-character limit ({len(os.path.abspath(g))} > 60 "
                               f"characters): use a short workspace such as C:/opentool-work")
        self.raw, self.data, self.work, self.staging = (os.path.join(g, x) for x in ("raw", "data", "extract", "staging"))
        for d in (self.raw, self.data, self.work):
            os.makedirs(d, exist_ok=True)
        self.bundle_globs = P.get("bundle_globs", ["*.bundle"])
        self.startup_bundle = P.get("startup_bundle")
        self.prefab_filter = P.get("prefab_filter", "")  # --filter-by-container for the unit model prefab dumps
        self.say = ctx.say
        self.log = os.path.join(g, "logs", "assetstudio.log")

    def bundle_path(self, name):
        return os.path.join(self.streaming, name)

    def is_in_game_folder(self, path):
        return os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(os.path.abspath(self.install)))


def file_version(path):
    try:
        v = ctypes.windll.version
        n = v.GetFileVersionInfoSizeW(path, None)
        buf = ctypes.create_string_buffer(n)
        v.GetFileVersionInfoW(path, 0, n, buf)
        p, ln = ctypes.c_void_p(), ctypes.c_uint()
        v.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(ln))
        ms, ls = ctypes.cast(p.value + 8, ctypes.POINTER(ctypes.c_uint32))[0:2]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001
        return "unknown"


def data_module(ctx, name):
    return importlib.import_module(f"profiles.games.{ctx.profile['data_module']}.{name}")


def clip_role(name, rules):
    for rx, role in rules:
        if re.search(rx, name, re.I):
            return role
    return "other"


def list_units(ctx):
    U = Unity(ctx)
    gamedump.dump_all(U, U.log)
    inv = data_module(ctx, "inventory").build(U)
    L = data_module(ctx, "loadouts")
    rules = ctx.profile.get("clip_roles") or [[".*", "other"]]
    units = []
    for u in L.merged_units(inv):
        models = {m["id"]: m for m in u["models"]}
        dm = models.get(u["default_model"]) or next(iter(models.values()), None)
        if dm is None or not dm["variants"]:
            continue
        var = dm["variants"][0]
        joints = {str(k): v for k, v in var.get("weapon_joints", {}).items()}
        los, others = [], []
        for lo in L.loadouts(inv, u):
            if lo["model"] != dm["id"]:
                others.append({"id": lo["key"], "model": lo["model"], "weapons": lo["weapons"]})
                continue
            ws, integral = [], []
            for slot, wid, wname in zip(lo["slots"], lo["weapon_ids"], lo["weapons"]):
                w = inv["weapons"].get(str(wid)) or {}
                j = joints.get(str(slot))
                if w.get("meshes") and j:
                    ws.append({"slot": slot, "weapon_id": wname, "mount": f"joint:{j}"})
                else:
                    integral.append(wname)
            los.append({"id": lo["key"], "weapons": ws, "integral": integral, "model": lo["model"],
                        "pose_group": lo.get("pose_group")})
        if not los:
            continue
        verify = "verify" in (u.get("class_reason") or "")
        units.append({
            "id": str(u["id"]), "name": u["name"], "group": u["faction"], "class_hint": u["class"],
            "confidence": "medium" if verify else "high", "class_reason": u.get("class_reason"),
            "models": [{"id": m["id"], "variants": [v["name"] for v in m["variants"]]} for m in u["models"]],
            "loadouts": los, "default_loadout": los[0]["id"], "other_model_loadouts": others,
            "clips": [{"name": c, "role": clip_role(c, rules)} for c in var.get("clips", [])],
            "provenance": {"prefab": var["prefab"], "bundle": var["bundle"], "clip_bundles": var.get("clip_bundles", []),
                           "unit_ids": u.get("merged_ids", [u["id"]])},
            "credits": []})
    return {"contract": CONTRACT_VERSION, "game": ctx.game["id"], "adapter": ADAPTER, "units": units}


def export_unit(ctx, entry, out):
    U = Unity(ctx)
    with open(os.path.join(U.data, "inventory.json")) as f:
        inv = json.load(f)
    L = data_module(ctx, "loadouts")
    unit = next(u for u in L.merged_units(inv) if str(u["id"]) == entry["id"])
    names = {w["weapon_id"] for lo in entry["loadouts"] for w in lo["weapons"]}
    weapons = {wid: w for wid, w in inv["weapons"].items() if w["name"] in names}
    unit = dict(unit, models=[m for m in unit["models"] if m["id"] == unit["default_model"]] or unit["models"][:1])
    man = extract.run(U, unit, weapons, U.log, all_weapons=inv["weapons"])
    model = man["models"][str(unit["models"][0]["id"])]
    wl = {n: {"fbx": w.get("fbx"), "mesh": w.get("mesh"), "hidden": w.get("hidden", [])} for n, w in man["weapons"].items()}
    ver = file_version(U.cli)
    prov = dict(entry["provenance"], game_install_read_only=True, reader=f"AssetStudioModCLI {ver}")
    return write_package(out, entry, model["anim_fbx"], wl, model["clips"], model.get("hidden", []), ctx.profile, ver, prov)
