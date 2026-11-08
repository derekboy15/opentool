"""Attach-weapon stage: mount each weapon of the loadout on its game joint, on the frozen pose.

cfg["pose_blend"]: the unit pose (pose stage). cfg["pose"]["weapons"]: [{fbx, joint, name, mat_key,
keep_root_offset, grip_import_mm, muzzle_import_mm}]. Weapons are mounted like the game does it: by the
mesh pivot on the attach joint (the exported FBX's root offset is dropped, see anim.attach_weapon).
Writes <out>/posed.blend (what prepare/build bake) and adds weapon points to <out>/pose.json.
"""
import hashlib
import json
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anim import attach_weapon, mesh_objects, weapon_point  # noqa: E402
from common import load_cfg, log, stage  # noqa: E402

cfg = load_cfg()
P = cfg["pose"]
S = cfg["scale"]["factor"]
bpy.ops.wm.open_mainfile(filepath=cfg["pose_blend"])
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
with open(os.path.join(cfg["out_dir"], "pose.json")) as f:
    info = json.load(f)

for w in P.get("weapons", []):
    joint = w["joint"]
    if joint not in bpy.data.objects:
        # joints usually import as empties; fall back to a bone of that name
        b = next((b for b in arm.pose.bones if b.name.endswith(joint)), None)
        if b is None:
            raise SystemExit(f"weapon joint {joint!r} not found in the rig")
    weapon = attach_weapon(arm, w["fbx"], joint, w["name"], keep_root_offset=w.get("keep_root_offset", False),
                           hidden=w.get("hidden", []))
    if joint not in bpy.data.objects:
        weapon.parent, weapon.parent_type, weapon.parent_bone = arm, "BONE", b.name
    if w.get("mat_key"):
        for mo in mesh_objects(weapon):
            mo["mat_key"] = w["mat_key"]
    bpy.context.view_layer.update()
    for key in ("grip", "muzzle"):
        if f"{key}_import_mm" in w:
            p = weapon_point(weapon, Vector(w[f"{key}_import_mm"]) / S)
            info["points_rig_mm"][f"{w['name']}_{key}" if len(P["weapons"]) > 1 else key] = [round(x * S, 2) for x in p]
    log(f"attached {w['name']} on {joint} (material {w.get('mat_key')})")
    stage("attach", weapon=w["name"], joint=joint)

info["weapons"] = [w["name"] for w in P.get("weapons", [])]
# what will be printed, as a fingerprint: loadouts that differ only by objects the game hides (the Guardsman
# "LasgunBayonet" keeps its bayonet inactive, so it is the plain Lasgun) give the same signature
dg = bpy.context.evaluated_depsgraph_get()
sig = []
for o in bpy.data.objects:
    if o.type != "MESH":
        continue
    ev = o.evaluated_get(dg)
    me = ev.to_mesh()
    co = [ev.matrix_world @ v.co for v in me.vertices]
    if co:
        sig.append((len(co), *[round(min(c[i] for c in co) * S / 0.05) for i in range(3)],
                    *[round(max(c[i] for c in co) * S / 0.05) for i in range(3)]))
    ev.to_mesh_clear()
info["geometry_sig"] = hashlib.md5(json.dumps(sorted(sig)).encode()).hexdigest()[:16]
log(f"geometry signature {info['geometry_sig']} ({len(sig)} mesh objects)")
with open(os.path.join(cfg["out_dir"], "pose.json"), "w") as f:
    json.dump(info, f, indent=2)
for o in bpy.data.objects:
    o.select_set(False)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(cfg["out_dir"], "posed.blend"))
log("saved posed.blend")
