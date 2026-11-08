# SPDX-License-Identifier: GPL-3.0-or-later
"""Template for a new engine adapter (see skill/new-engine.md). Copy this folder to adapters/<engine>/.

An adapter offers two functions. The tool validates their output against opentool/schema/*.json before the back
end touches it (`ot.py adapter-check`).

ctx (opentool.cli.Ctx): ctx.s (settings), ctx.game {"id", "install", "profile", ...}, ctx.profile (the game
profile JSON), ctx.gdir (<workspace>/<game>, write everything here), ctx.runner (capped runner: .blender(script,
args), .python(args)), ctx.reader(key) (a path from opentool.toml [readers]), ctx.say(msg).
"""
import json
import os

from opentool import CONTRACT_VERSION

ADAPTER = {"name": "template", "version": "0.0.1"}


def list_units(ctx):
    """Every unit/model the game has -> units.json (contract 1.x, opentool/schema/units.schema.json).

    One entry per unit: id (stable), name, group (faction/race), class_hint (infantry | walker | bike | monster |
    vehicle | flyer | other) + confidence (high | medium | low), models [], loadouts [{id, weapons: [{slot,
    weapon_id, mount}], mesh_groups?}], default_loadout, clips [{name, role}], provenance {archive, path, hash...},
    credits []. Mounts: joint:<bone> | socket:<name> | bonemerge | mesh_group:<name>.
    Read the install only; never write into it."""
    raise NotImplementedError("read the game's index here (never unpack everything up front if the game is big)")
    return {"contract": CONTRACT_VERSION, "game": ctx.game["id"], "adapter": ADAPTER, "units": []}


def export_unit(ctx, unit, out):
    """One unit -> package folder `out` (opentool/schema/package.schema.json):
        package.json, model/<name>.glb|.fbx (rig + skinned meshes + clips), weapons/<id>/..., textures/...
    Paths inside package.json are relative with forward slashes. Fill rig_map with canonical roles (pelvis, head,
    eye, wrist.L, knee.R, ankle.L, ball.R, ...; see opentool/contract.py) -> the game's bone names. Give each
    normal map its encoding (rgb | dxt5nm | bc5) and green convention (opengl | directx). If Blender has to do
    the reading (a Blender add-on reader), run your script with ctx.runner.blender(...), never blender.exe."""
    os.makedirs(out, exist_ok=True)
    pkg = {"contract": CONTRACT_VERSION, "adapter": ADAPTER, "reader": {"name": "?", "version": "?"},
           "game": ctx.game["id"], "unit": {"id": unit["id"], "name": unit["name"], "group": unit["group"],
                                            "class_hint": unit["class_hint"], "class_confidence": unit["confidence"]},
           "model": "model/model.glb", "format": "glb", "units": {"metres_per_unit": 1.0}, "up_axis": "Z",
           "handedness": "right", "bind_pose_ok": True, "rig_map": {}, "clips": [], "mesh_groups": [], "hidden": [],
           "weapons": [], "loadouts": unit["loadouts"], "default_loadout": unit["default_loadout"], "textures": [],
           "provenance": unit["provenance"], "credits": unit["credits"], "warnings": []}
    with open(os.path.join(out, "package.json"), "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=1)
    raise NotImplementedError("write the model file and fill the package")
