# SPDX-License-Identifier: GPL-3.0-or-later
"""Ingest stage (stage 1) and the Blender half of `opentool adapter-check`.

Imports the package model as every later stage will, and reports: meshes, triangles (all and of the default
loadout's mesh groups), bones, clips found, every rig_map role resolved or not, cards (open, paper-thin
sheets: straps, hair, banners) and open shells, rest-pose height, and one clip frame actually posed (the first
idle clip, middle frame: bones must move and no mesh may explode).
cfg: anim_fbx, rig_map, clips [{name, role}], mesh_groups, show_groups, hidden, out_dir.
Writes <out>/ingest.json and prints RESULT_JSON.
"""
import json
import os
import sys

import bmesh
import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anim import find_bone, is_hidden, load_animated, set_clip, clip_range  # noqa: E402
from common import boundary_loops, face_shells, load_cfg, log, shell_volume, stage  # noqa: E402

assert bpy.app.version[:2] == (5, 2), f"Blender 5.2 needed, this is {bpy.app.version_string}"
cfg = load_cfg()
arm, clips = load_animated(cfg["anim_fbx"])
RM = cfg.get("rig_map") or {}
groups = {g["name"]: g for g in cfg.get("mesh_groups") or []}
show = set(cfg.get("show_groups") or groups)
hidden = cfg.get("hidden", [])

meshes = [o for o in bpy.data.objects if o.type == "MESH"]
info = {"model": os.path.basename(cfg["anim_fbx"]), "meshes": [], "bones": len(arm.data.bones) if arm else 0,
        "armatures": sum(1 for o in bpy.data.objects if o.type == "ARMATURE"), "roles": {}, "warnings": [],
        "clips_in_file": sorted(clips), "clips_missing": []}
tot = dflt = 0
for o in meshes:
    me = o.data
    me.calc_loop_triangles()
    tris = len(me.loop_triangles)
    bm = bmesh.new()
    bm.from_mesh(me)
    loops = boundary_loops(bm)
    shells = face_shells(bm)
    cards = sum(1 for sh in shells if abs(shell_volume(sh)) < 1e-9 * max(1, len(sh)))
    bm.free()
    in_default = (o.name in show) if o.name in groups else not is_hidden(o, hidden)
    tot += tris
    dflt += tris if in_default else 0
    info["meshes"].append({"name": o.name, "tris": tris, "verts": len(me.vertices), "group": o.name in groups,
                           "default": in_default, "open_loops": len(loops), "shells": len(shells), "card_shells": cards})
info["totals"] = {"meshes": len(meshes), "tris": tot, "default_tris": dflt}
for role, name in RM.items():
    b = find_bone(arm, name) if arm else None
    info["roles"][role] = b.name if b else None
    if b is None:
        info["warnings"].append(f"rig role {role} -> {name!r}: no such bone")
for c in cfg.get("clips") or []:
    if c["name"] not in clips:
        info["clips_missing"].append(c["name"])
if info["clips_missing"]:
    info["warnings"].append(f"{len(info['clips_missing'])} listed clip(s) not in the model file: {info['clips_missing'][:8]}")

# rest height and one posed frame
dg = bpy.context.evaluated_depsgraph_get()


def extent():
    zs = []
    for o in meshes:
        ev = o.evaluated_get(dg)
        m = ev.to_mesh()
        zs += [(ev.matrix_world @ v.co).z for v in m.vertices]
        ev.to_mesh_clear()
    return (min(zs), max(zs)) if zs else (0.0, 0.0)


z0, z1 = extent()
info["rest_height_raw"] = round(z1 - z0, 6)
idle = [c["name"] for c in cfg.get("clips") or [] if c["role"] == "idle" and c["name"] in clips] or sorted(clips)[:1]
if idle and arm:
    set_clip(clips, idle[0])
    f0, f1 = clip_range(clips, idle[0], arm)
    rest = {pb.name: pb.matrix.copy() for pb in arm.pose.bones}
    bpy.context.scene.frame_set((f0 + f1) // 2)
    dg = bpy.context.evaluated_depsgraph_get()
    moved = sum(1 for pb in arm.pose.bones if (pb.matrix.translation - rest[pb.name].translation).length > 1e-6
                or (pb.matrix.to_quaternion().rotation_difference(rest[pb.name].to_quaternion()).angle > 1e-4))
    p0, p1 = extent()
    info["pose_test"] = {"clip": idle[0], "frame": (f0 + f1) // 2, "bones_moved": moved,
                         "height_raw": round(p1 - p0, 6), "height_ratio": round((p1 - p0) / max(z1 - z0, 1e-9), 3)}
    if moved == 0:
        info["warnings"].append(f"clip {idle[0]} moves no bone")
    if not 0.4 < info["pose_test"]["height_ratio"] < 2.0:
        info["warnings"].append(f"posed height is {info['pose_test']['height_ratio']}x the rest height: rig or clip broken?")
else:
    info["warnings"].append("no clip to test a pose with")
os.makedirs(cfg["out_dir"], exist_ok=True)
with open(os.path.join(cfg["out_dir"], "ingest.json"), "w") as f:
    json.dump(info, f, indent=1)
stage("ingest", meshes=len(meshes), tris=tot, default_tris=dflt, bones=info["bones"])
print("RESULT_JSON:" + json.dumps({"ok": True, "totals": info["totals"], "bones": info["bones"],
                                   "roles_missing": [r for r, b in info["roles"].items() if b is None],
                                   "warnings": info["warnings"][:10]}))
