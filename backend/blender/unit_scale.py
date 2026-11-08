# SPDX-License-Identifier: GPL-3.0-or-later
"""Scale stage (stage 3, per unit): the factor from the package model's units to print millimetres.

cfg["scale"]: rule (auto | eye_height | metric | factor), eye_height_mm, mm_per_metre, factor,
eye {method: auto | role | head_fraction | given, head_fraction, eye_line_raw}; cfg["metres_per_unit"] (package hint);
cfg["rig_map"]; cfg["mesh_groups"] (package, for the head mesh) and cfg["show_groups"] (the default loadout).
Rest (bind) pose. Eye height = sole contact plane (lowest vertex) to the pupil line, base excluded.
Writes <out>/scale.json: factor, rule, eye line (raw and mm, method, estimated?), rest size, build-volume fit.
"""
import json
import math
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anim import import_model, is_hidden  # noqa: E402
from common import load_cfg, log, stage  # noqa: E402
import rig as rigmod  # noqa: E402

cfg = load_cfg()
SC = cfg["scale"]
RM = cfg.get("rig_map") or {}
import_model(cfg["anim_fbx"])
arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
show = set(cfg.get("show_groups") or [])
groups = {g["name"]: g for g in cfg.get("mesh_groups") or []}
meshes = []
for o in bpy.data.objects:
    if o.type != "MESH" or o.name.startswith("FX_") or is_hidden(o, cfg.get("hidden", [])):
        continue
    if groups and o.name in groups and o.name not in show:
        continue  # mesh groups the default loadout does not show
    meshes.append(o)


def box(points):
    lo = Vector([min(p[i] for p in points) for i in range(3)])
    hi = Vector([max(p[i] for p in points) for i in range(3)])
    return lo, hi


allp, parts = [], []
for o in meshes:
    pts = [o.matrix_world @ v.co for v in o.data.vertices]  # undeformed data = bind pose
    if pts:
        allp += pts
        parts.append((o, pts))
lo, hi = box(allp)
sole = lo.z

# ---------------------------------------------------------------- eye line (raw units)
eye = SC.get("eye") or {}
method = eye.get("method", "auto")
eye_raw, eye_how, estimated = None, None, True
if eye.get("eye_line_raw") is not None and method in ("auto", "given"):
    eye_raw, eye_how, estimated = float(eye["eye_line_raw"]), "given per unit (eye_line_raw)", False
if eye_raw is None and arm is not None and method in ("auto", "role"):
    b = rigmod.bone(arm, RM, "eye")
    if b:
        eye_raw = (arm.matrix_world @ b.bone.head_local).z
        eye_how, estimated = f"rig role eye ({b.name})", False
head_box = None
if arm is not None:
    hb = rigmod.bone(arm, RM, "head")
    head_pts = []
    if hb:
        names = rigmod.subtree_names(hb)
        for o, pts in parts:
            if groups.get(o.name, {}).get("role") == "head":
                head_pts += pts
                continue
            if not o.vertex_groups:
                if o.parent_type == "BONE" and o.parent_bone in names:
                    head_pts += pts
                continue
            gn = {g.index: g.name for g in o.vertex_groups}
            for v, p in zip(o.data.vertices, pts):
                gs = [(g.weight, gn.get(g.group, "")) for g in v.groups if g.weight > 0]
                if gs and max(gs)[1] in names:
                    head_pts.append(p)
    else:
        head_pts = [p for o, pts in parts if groups.get(o.name, {}).get("role") == "head" for p in pts]
    if head_pts:
        head_box = box(head_pts)
if eye_raw is None and head_box is not None and method in ("auto", "head_fraction"):
    f = float(eye.get("head_fraction", 0.45))
    eye_raw = head_box[0].z + f * (head_box[1].z - head_box[0].z)
    eye_how = f"{f:g} of the head mesh height (estimate: check the eye line on the sheet)"

rule = SC.get("rule", "auto")
if rule == "auto":
    rule = "eye_height" if cfg.get("class") == "infantry" else "metric"
if rule == "eye_height" and eye_raw is None:
    log("WARNING eye-height rule without an eye line (no eye/head role, no head mesh): falling back to metric")
    rule = "metric"
if rule == "eye_height":
    factor = SC["eye_height_mm"] / (eye_raw - sole)
    why = f"eye height {SC['eye_height_mm']} mm (sole to pupil line, {eye_how})"
elif rule == "metric":
    mpu = cfg.get("metres_per_unit")
    if not mpu:
        raise SystemExit("metric scale rule needs the package's units.metres_per_unit")
    factor = SC["mm_per_metre"] * mpu
    why = f"{SC['mm_per_metre']} mm per real metre x {mpu} m per unit"
elif rule == "factor":
    factor = float(SC["factor"])
    why = "fixed factor (mm per imported unit)"
else:
    raise SystemExit(f"unknown scale rule {rule!r}")
S = factor

size = [(hi[i] - lo[i]) * S for i in range(3)]
bv = cfg["printer"]["build_volume_mm"]
bc = cfg.get("base") or {}
base_d = max(bc.get("bottom_diameter_mm", 0), max(bc.get("bottom_mm", [0])))
base_h = bc.get("height_mm", 0) - bc.get("sink_mm", 0)
foot_xy = max(size[0], size[1])
plan = max(math.hypot(size[0], size[1]) if foot_xy > min(bv[:2]) else foot_xy, base_d)
height = size[2] + base_h
fits = plan <= min(bv[0], bv[1]) and height <= bv[2]
low = [p for p in allp if (p.z - lo.z) * S < 3.0]
fx = [p.x * S for p in low]
fy = [p.y * S for p in low]
foot = {"x_mm": round(max(fx) - min(fx), 1), "y_mm": round(max(fy) - min(fy), 1),
        "diag_mm": round(math.hypot(max(fx) - min(fx), max(fy) - min(fy)), 1)}
pinfo = []
for o, pts in parts:
    a, b = box(pts)
    pinfo.append({"object": o.name, "verts": len(pts), "size_mm": [round((b[i] - a[i]) * S, 1) for i in range(3)]})
res = {"factor": S, "rule": rule, "why": why, "rest_size_mm": [round(x, 2) for x in size],
       "rest_height_mm": round(size[2], 2), "rest_size_raw": [round(hi[i] - lo[i], 5) for i in range(3)],
       "sole_raw": round(sole, 6), "rest_footprint": foot,
       "eye": {"raw": None if eye_raw is None else round(eye_raw, 6), "how": eye_how, "estimated": estimated,
               "height_mm": None if eye_raw is None else round((eye_raw - sole) * S, 2)},
       "head_box_raw": None if head_box is None else [[round(x, 5) for x in head_box[0]], [round(x, 5) for x in head_box[1]]],
       "with_base_height_mm": round(height, 1), "plan_mm": round(plan, 1), "build_volume_mm": bv,
       "fits_build_volume": fits, "parts": sorted(pinfo, key=lambda p: -max(p["size_mm"]))}
with open(os.path.join(cfg["out_dir"], "scale.json"), "w") as f:
    json.dump(res, f, indent=2)
log("SCALE " + json.dumps({k: v for k, v in res.items() if k != "parts"}))
stage("scale", fits=fits, factor=round(S, 4), height_mm=res["rest_height_mm"])
