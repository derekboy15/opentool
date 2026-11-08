# SPDX-License-Identifier: GPL-3.0-or-later
"""Relic Essence 1 adapter (Dawn of War 1, Winter Assault, Dark Crusade, Soulstorm and their mods).

Readers the user downloads: relic-tool-sga-v2 (unpack SGA v2 archives; GPL-3) and amorgun's blender_dow add-on
(import .whm in Blender; GPL-3). Game profile keys (profiles/games/<game>.json "relic"): module (the .module the
game loads, e.g. WXP.module), sources [{"tag", "module"}], model_globs, credits (per archive tag).

list:   unpack the model-relevant archives in .module load order (once), index every
        art/ebps/races/<race>/(troops|structures)/*.whm (first archive in load order wins, identical files deduped
        by hash), read each with the pure-Python reader (whm.py), work out the default appearance from the .whe
        motion tree and the vis_* animations, the class from the unit's ebps armour type, and loadouts as mesh groups.
export: blender_export.py in Blender (capped runner) -> package/model/<unit>.glb + textures + package.json.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import zipfile
from collections import defaultdict

from opentool import CONTRACT_VERSION

from . import archives, whm

ADAPTER = {"name": "relic_essence1", "version": "0.1.0"}
HERE = os.path.dirname(os.path.abspath(__file__))


def addon_parent(ctx):
    """Folder holding the blender_dow add-on folder. A release .zip is extracted once into the workspace."""
    p = ctx.reader("blender_dow")
    if p.lower().endswith(".zip"):
        dest = os.path.join(ctx.gdir, "readers", "addons_" + os.path.splitext(os.path.basename(p))[0])
        if not os.path.exists(os.path.join(dest, "blender_dow", "__init__.py")):
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
                flat = "__init__.py" in names  # release zips hold the add-on's files at their root
                z.extractall(os.path.join(dest, "blender_dow") if flat else dest)
        p = dest
    if os.path.exists(os.path.join(p, "blender_dow", "__init__.py")):
        return p
    if os.path.exists(os.path.join(p, "__init__.py")) and os.path.basename(os.path.normpath(p)) == "blender_dow":
        return os.path.dirname(os.path.normpath(p))
    raise FileNotFoundError(f"[readers] blender_dow = {p}: no blender_dow/__init__.py there")


def roots(ctx):
    R = ctx.profile["relic"]
    arcs = archives.archive_order(ctx.game["install"], R["module"])
    return archives.unpack(ctx, arcs, os.path.join(ctx.gdir, "unpacked")), arcs


def clip_role(name, rules):
    for rx, role in rules:
        if re.search(rx, name, re.I):
            return role
    return "other"


def credits_for(ctx, tag):
    return [c for k, c in (ctx.profile["relic"].get("credits") or {}).items() if tag.startswith(k)]


def list_units(ctx):
    rts, arcs = roots(ctx)
    res = whm.Resolver(rts)
    R = ctx.profile["relic"]
    ebps = whm.ebps_links(res)
    used_by = defaultdict(list)
    armour = {}
    for key, e in ebps.items():
        if e["model"]:
            used_by[e["model"]].append(key)
            if e["armour"]:
                armour.setdefault(e["model"], e["armour"])
    rules = ctx.profile.get("clip_roles") or [[".*", "other"]]
    units, seen = [], {}
    for key, (p, tag) in sorted(res.index.items()):
        m = whm.MODEL_RE.match(key)
        if not m:
            continue
        raw = p.read_bytes()
        fh = hashlib.sha1(raw).hexdigest()
        race, folder, name = m.group(1), m.group(2), m.group(3)
        if fh in seen:  # identical file under another path: one entry, every path in provenance
            seen[fh]["provenance"]["paths"].append(key)
            continue
        try:
            info = whm.read_whm(raw, res)
        except Exception as e:  # noqa: BLE001
            units.append({"id": key, "name": name.replace("/", "_"), "group": race, "class_hint": "other", "confidence": "low",
                          "models": [], "loadouts": [], "default_loadout": "", "clips": [], "credits": [],
                          "provenance": {"archive": tag, "path": key, "sha1": fh}, "error": repr(e)})
            continue
        whe = res.find(key[:-4] + ".whe")
        mot = whm.whe_motions(whe[0].read_bytes()) if whe else {}
        dset, rule, used, conf = whm.default_set(info, mot)
        cls = whm.model_class(name, folder, armour.get(key))
        cls = cls if cls in ("infantry", "walker", "bike", "monster", "vehicle", "flyer") else "other"
        meshes = [x["name"] for x in info["meshes"]]
        groups = [{"name": n, "role": whm.mesh_role(n)} for n in meshes]
        default_weapons = [n for n in dset if whm.mesh_role(n) == "weapon"]
        lo_default = {"id": "default", "mesh_groups": dset,
                      "weapons": [{"slot": i + 1, "weapon_id": n, "mount": f"mesh_group:{n}"} for i, n in enumerate(default_weapons)]}
        loadouts = [lo_default]
        ranged = [n for n in default_weapons if not re.search(r"knife|sword|blade|axe|claw|fist|hammer", n, re.I)]
        for n in meshes:  # each other weapon mesh in place of the default ranged weapon
            if whm.mesh_role(n) != "weapon" or n in dset or not ranged:
                continue
            show = [x for x in dset if x != ranged[0]] + [n]
            loadouts.append({"id": re.sub(r"[^A-Za-z0-9]+", "_", n), "mesh_groups": show,
                             "weapons": [{"slot": i + 1, "weapon_id": w, "mount": f"mesh_group:{w}"}
                                         for i, w in enumerate(x for x in show if whm.mesh_role(x) == "weapon")]})
        anims = sorted(a for a in info["animations"] if not a.lower().startswith("vis"))
        u = {"id": key, "name": name.replace("/", "_"), "group": race, "class_hint": cls,
             "confidence": "medium" if cls == "infantry" and armour.get(key) else "low" if not armour.get(key) else "medium",
             "models": [{"id": key, "meshes": len(meshes), "tris": info["totals"]["tris"], "bones": len(info["bones"]),
                         "markers": info["markers"]}],
             "loadouts": loadouts, "default_loadout": "default",
             "default_rule": {"rule": rule, "vis_anims": used, "confidence": conf, "tris": sum(x["tris"] for x in info["meshes"] if x["name"] in dset)},
             "clips": [{"name": a, "role": clip_role(a, rules)} for a in anims],
             "provenance": {"archive": tag, "path": key, "sha1": fh, "paths": [key], "units": sorted(used_by.get(key, [])),
                            "whe": whe[1] if whe else None, "load_order": [t for _, t in arcs]},
             "credits": credits_for(ctx, tag), "warnings": info["warnings"]}
        seen[fh] = u
        units.append(u)
    return {"contract": CONTRACT_VERSION, "game": ctx.game["id"], "adapter": ADAPTER, "units": units}


def export_unit(ctx, u, out):
    rts, _ = roots(ctx)
    lo = next(l for l in u["loadouts"] if l["id"] == u["default_loadout"])
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    cfg = {"addon_parent": addon_parent(ctx), "roots": [[r, t] for r, t in rts], "whm": u["provenance"]["path"],
           "default": lo["mesh_groups"], "out_dir": out}
    cp = os.path.join(ctx.gdir, "work", u["group"], u["name"], "export_config.json")
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    r = ctx.runner.blender(os.path.join(HERE, "blender_export.py"), [cp], name="relic_export",
                           log_path=os.path.join(ctx.gdir, "logs", "units", u["group"], u["name"], "export.log"))
    ex = json.load(open(os.path.join(out, "export.json"), encoding="utf-8"))
    os.replace(os.path.join(out, "export.json"), os.path.join(ctx.gdir, "work", u["group"], u["name"], "export.json"))
    P = ctx.profile
    rig = dict(P.get("rig_map") or {})
    bones = set(ex["bone_names"])
    missing = sorted(k for k, v in rig.items() if v not in bones)
    rig = {k: v for k, v in rig.items() if v in bones}
    rules = P.get("clip_roles") or [[".*", "other"]]
    clips = [{"name": c["name"], "role": clip_role(c["name"], rules), "frames": c["frames"], "fps": P["relic"].get("fps", 30)}
             for c in ex["clips"]]
    names = {m["name"] for m in ex["meshes"]}
    groups = [{"name": n, "role": whm.mesh_role(n), "default": n in lo["mesh_groups"]} for n in sorted(names)]
    warnings = list(u.get("warnings", [])) + ([f"rig roles not on this rig: {missing}"] if missing else [])
    if ex["pose"]["used"] != "bind":
        warnings.append(f"bind pose: {ex['pose'].get('why')}")
    pkg = {"contract": CONTRACT_VERSION, "adapter": ADAPTER,
           "reader": {"name": "blender_dow + relic-tool-sga-v2", "version": P["relic"].get("reader_versions", "unknown")},
           "game": P.get("name", ctx.game["id"]),
           "unit": {"id": u["id"], "name": u["name"], "group": u["group"], "class_hint": u["class_hint"],
                    "class_confidence": u["confidence"]},
           "model": ex["model"], "format": "glb", "units": {"metres_per_unit": P["relic"].get("metres_per_unit", 1.0)},
           "up_axis": "Z", "handedness": "right", "bind_pose_ok": ex["pose"]["used"] == "bind",
           "rig_map": rig, "clips": clips, "mesh_groups": groups, "hidden": [], "weapons": [],
           "loadouts": u["loadouts"], "default_loadout": u["default_loadout"], "textures": ex["textures"],
           "provenance": dict(u["provenance"], extract_stats=ex["stats"], pose=ex["pose"], default_rule=u.get("default_rule"),
                              mod=bool(u["credits"])),
           "credits": u["credits"], "warnings": warnings}
    with open(os.path.join(out, "package.json"), "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=1)
    ctx.say(f"  extraction: {json.dumps(ex['stats'])}")
    return pkg
