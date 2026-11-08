# SPDX-License-Identifier: GPL-3.0-or-later
"""Assemble an export package (contract 1.x) from AssetStudio output: the Animator FBX (rig + skinned meshes +
bound clips) with its textures, each weapon's FBX with its normal map, and what the game data says
(loadouts with joint mounts, clips with roles, objects the prefab keeps hidden)."""
import glob
import hashlib
import json
import os
import re
import shutil

from opentool import CONTRACT_VERSION

ADAPTER = {"name": "unity_assetstudio", "version": "0.1.0"}


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clip_role(name, rules):
    for rx, role in rules:
        if re.search(rx, name, re.I):
            return role
    return "other"


def texture_entries(folder, dest_rel, out):
    """<KEY>_N.png normal maps (Unity DXT5nm/BC5 layout, OpenGL green) -> package textures."""
    ents = []
    os.makedirs(os.path.join(out, dest_rel), exist_ok=True)
    for f in sorted(glob.glob(os.path.join(folder, "*.png"))):
        stem = os.path.splitext(os.path.basename(f))[0]
        role = "normal" if stem.endswith("_N") else "diffuse" if stem.endswith("_D") else "other"
        if role == "other":
            continue
        shutil.copy2(f, os.path.join(out, dest_rel, os.path.basename(f)))
        e = {"file": f"{dest_rel}/{os.path.basename(f)}", "role": role, "material": stem[:-2]}
        if role == "normal":
            e.update(encoding="auto", green="opengl")
        ents.append(e)
    return ents


def write_package(out, unit, model_fbx, weapons, clips, hidden, profile, reader_version, provenance, warnings=()):
    """unit: units.json entry; model_fbx: the Animator FBX; weapons: {weapon_id: {"fbx", "mesh", "hidden"}};
    clips: clip names in the FBX; hidden: object paths the prefab keeps inactive / renderer off."""
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "model"), exist_ok=True)
    mname = os.path.basename(model_fbx)
    shutil.copy2(model_fbx, os.path.join(out, "model", mname))
    textures = texture_entries(os.path.dirname(model_fbx), "textures", out)
    wlist = []
    for wid, w in weapons.items():
        if not w.get("fbx"):
            continue
        rel = f"weapons/{wid}"
        os.makedirs(os.path.join(out, rel), exist_ok=True)
        shutil.copy2(w["fbx"], os.path.join(out, rel, os.path.basename(w["fbx"])))
        tex = texture_entries(os.path.dirname(w["fbx"]), rel, out)
        textures += tex
        mat = next((t["material"] for t in tex if t["role"] == "normal"), None)
        wlist.append({"id": wid, "file": f"{rel}/{os.path.basename(w['fbx'])}", "mesh": w.get("mesh"), "mat_key": mat,
                      "hidden": w.get("hidden", [])})
    rules = profile.get("clip_roles") or [[".*", "other"]]
    pkg = {
        "contract": CONTRACT_VERSION, "adapter": ADAPTER, "reader": {"name": "AssetStudioModCLI", "version": reader_version},
        "game": profile.get("name", ""),
        "unit": {"id": unit["id"], "name": unit["name"], "group": unit["group"], "class_hint": unit["class_hint"],
                 "class_confidence": unit["confidence"]},
        "model": f"model/{mname}", "format": "fbx",
        "units": {"metres_per_unit": profile["unity"].get("metres_per_unit")},
        "up_axis": "Z", "handedness": "right", "bind_pose_ok": True,
        "rig_map": profile.get("rig_map", {}),
        "clips": [{"name": c, "role": clip_role(c, rules), "fps": profile["unity"].get("fps", 25)} for c in sorted(clips)],
        "mesh_groups": [], "hidden": hidden,
        "weapons": wlist,
        "loadouts": unit["loadouts"], "default_loadout": unit["default_loadout"],
        "textures": textures,
        "provenance": dict(provenance, model_sha1=sha1(os.path.join(out, "model", mname))),
        "credits": unit.get("credits", []), "warnings": list(warnings),
    }
    with open(os.path.join(out, "package.json"), "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=1)
    return pkg
