# SPDX-License-Identifier: GPL-3.0-or-later
"""Pose selection (stage 4): pick N visually distinct, printable frames from the unit's own clips.

cfg: anim_fbx, rig, pose_select {...}, pose_count, pinned [{clip, frame}], clips (candidate clip names),
     weapons [{fbx, joint, name}] (the group's loadout, for the clipping test), feet_enabled, out_dir.
Per sampled frame:
  lift_mm      height of the higher foot above the lower one (feet must be planted: big lifts need big IK fixes)
  hover_mm     lower foot above the ground level (flying monsters idle in the air; jump frames)
  lean_deg     side lean of a bike body (lean_bones); big leans overhang the oval base
  pitch_deg    nose up/down of that body: a gentle front-wheel lift is fine, a full wheelie leaves the oval
  below_feet_mm  lowest point (talon, tail tip, weapon) below the lowest foot: the model would stand on it
  clip_mm      deepest weapon vertex inside the body (forearms/hands excluded: they hold it)
  signature    every bone's position relative to the root, body yaw removed, divided by the rest height
               (rig-agnostic, so the same code ranks marines, bugs and walkers)
Selection: pinned poses first; then repeatedly the frame that maximises
  diversity (min signature distance to the chosen ones) - penalty + bonus for a clip not used yet,
skipping frames closer than min_pose_distance to a chosen one or failing the hard limits.
Writes <out>/poses.json and <out>/poses_sheet/<n>_<clip>_f<frame>.png previews (weapon in orange).
"""
import json
import math
import os
import sys

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anim import (apply_mesh_groups, attach_weapon, bone_matches, clip_range, find_bone, is_hidden, load_animated,  # noqa: E402
                  mesh_objects, remove_tree, set_clip)
import base as basemod  # noqa: E402
from common import load_cfg, log, stage  # noqa: E402
import rig as rigmod  # noqa: E402

cfg = load_cfg()
PS, RM = cfg["pose_select"], cfg.get("rig_map") or {}
S = cfg["scale"]["factor"]
N = cfg["pose_count"]
arm, clips = load_animated(cfg["anim_fbx"])
shown_groups, _ = apply_mesh_groups(cfg, log)
weapons = []
for w in cfg.get("weapons", []):
    try:
        weapons += mesh_objects(attach_weapon(arm, w["fbx"], w["joint"], w["name"], hidden=w.get("hidden", [])))
    except Exception as e:  # selection still works without the clipping test
        log(f"weapon {w['name']} not attached for scoring: {e}")
hidden = cfg.get("hidden", [])
for nm in [o.name for o in bpy.data.objects if o not in weapons and is_hidden(o, hidden)]:
    if nm in bpy.data.objects:
        remove_tree(bpy.data.objects[nm])
AW = arm.matrix_world.copy()
sc = bpy.context.scene
body = max((o for o in bpy.data.objects if o.type == "MESH" and o not in weapons), key=lambda o: len(o.data.vertices))
gnames = {g.index: g.name for g in body.vertex_groups}
dom = []
for v in body.data.vertices:
    gs = [(g.weight, gnames[g.group]) for g in v.groups if g.weight > 0]
    dom.append(max(gs)[1] if gs else "")
# hands and forearms hold the weapon: their skin does not count for the weapon-in-body test
hand_set = rigmod.hand_names(arm, RM, (cfg["pose"].get("loose_items") or {}).get("hand_bone_patterns") or ())
for s_ in rigmod.SIDES:
    eb = rigmod.bone(arm, RM, "elbow", s_)
    if eb:
        hand_set.add(eb.name)
sides = list(rigmod.SIDES)


def bone(suffix):
    return find_bone(arm, suffix)


feet = {s: (rigmod.bone(arm, RM, "ankle", s), rigmod.bone(arm, RM, "ball", s)) for s in sides}
has_feet = cfg.get("feet_enabled", True) and all(a for a, b in feet.values())
if cfg.get("feet_enabled", True) and not has_feet:
    log("WARNING foot planting not scored: rig roles ankle.L / ankle.R not both mapped")
root = rigmod.bone(arm, RM, "pelvis") or arm.pose.bones[0]
rest_lo = min((body.matrix_world @ v.co).z for v in body.data.vertices)
rest_hi = max((body.matrix_world @ v.co).z for v in body.data.vertices)
H = max(rest_hi - rest_lo, 1e-6)
deform = [pb for pb in arm.pose.bones if pb.bone.use_deform] or list(arm.pose.bones)


# side lean of a bike/vehicle body (pose_select.lean_bones: name substrings). Its sideways axis is the bone axis
# closest to world x in the rest pose (game models face +-y); lean = that axis' tilt out of the horizontal.
# Pitch (a lifted front wheel) is not penalised.
lean_bone, lean_axis, pitch_axis = None, 0, 1
for key in PS.get("lean_bones", []):
    lean_bone = next((b for b in arm.pose.bones if key.lower() in b.name.lower()), None)
    if lean_bone:
        R0 = (AW @ lean_bone.bone.matrix_local).to_3x3()
        lean_axis = max(range(3), key=lambda i: abs(R0.col[i].normalized().x))
        pitch_axis = max((i for i in range(3) if i != lean_axis), key=lambda i: abs(R0.col[i].normalized().y))
        log(f"lean measured on {lean_bone.name} (sideways axis {'xyz'[lean_axis]})")
        break


def lean_deg():
    if lean_bone is None:
        return 0.0
    lat = (AW @ lean_bone.matrix).to_3x3().col[lean_axis].normalized()
    return math.degrees(math.asin(min(1.0, abs(lat.z))))


def pitch_deg():
    """Nose up/down of the bike body: its forward axis (closest to world y at rest) out of the horizontal."""
    if lean_bone is None:
        return 0.0
    fwd = (AW @ lean_bone.matrix).to_3x3().col[pitch_axis].normalized()
    return math.degrees(math.asin(min(1.0, abs(fwd.z))))


def signature():
    M = AW @ root.matrix
    fwd = M.to_3x3() @ Vector((0, 1, 0))
    yaw = math.atan2(fwd.x, fwd.y)
    c, s = math.cos(-yaw), math.sin(-yaw)
    o = M.translation
    out = []
    for pb in deform:
        p = (AW @ pb.matrix).translation - o
        out += [(p.x * c - p.y * s) / H, (p.x * s + p.y * c) / H, p.z / H]
    return np.array(out)


# foot skin: vertices weighted to an ankle bone or anything below it (ball, toes, claws), in every mesh (some rigs
# split the body: the Hive Tyrant's legs are their own mesh)
foot_bones = {b.name for a, _ in feet.values() if a for b in [a.bone] + list(a.bone.children_recursive)} if has_feet else set()
foot_idx = {}
for o in bpy.data.objects:
    if o.type != "MESH" or o in weapons or not o.vertex_groups:
        continue
    names = {g.index: g.name for g in o.vertex_groups}
    idx = []
    for v in o.data.vertices:
        gs = [(g.weight, names.get(g.group, "")) for g in v.groups if g.weight > 0]
        if gs and max(gs)[1] in foot_bones:
            idx.append(v.index)
    if idx:
        foot_idx[o.name] = idx


def below_feet():
    """How far the lowest point of the model (talon, arm gun, tail tip, weapon) hangs below the lowest foot skin,
    in mm: the model rests on its lowest point, so anything well below the feet leaves them in the air."""
    if not foot_idx:
        return 0.0
    dg = bpy.context.evaluated_depsgraph_get()
    lowest, feet_low = 1e9, 1e9
    for o in [o for o in bpy.data.objects if o.type == "MESH"]:
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        zs = [(ev.matrix_world @ v.co).z for v in me.vertices]
        if zs:
            lowest = min(lowest, min(zs))
            if o.name in foot_idx:
                feet_low = min(feet_low, min(zs[i] for i in foot_idx[o.name] if i < len(zs)))
        ev.to_mesh_clear()
    return max(0.0, (feet_low - lowest) * S) if feet_low < 1e9 else 0.0


def clip_depth():
    if not weapons:
        return 0.0
    dg = bpy.context.evaluated_depsgraph_get()
    ev = body.evaluated_get(dg)
    me = ev.to_mesh()
    co = [ev.matrix_world @ v.co for v in me.vertices]
    polys = [tuple(p.vertices) for p in me.polygons if not any(dom[i] in hand_set for i in p.vertices)]
    ev.to_mesh_clear()
    if not polys:
        return 0.0
    bvh = BVHTree.FromPolygons(co, polys)
    worst = 0.0
    for w in weapons:
        wev = w.evaluated_get(dg)
        wme = wev.to_mesh()
        for v in list(wme.vertices)[::3]:
            p = wev.matrix_world @ v.co
            loc, n, _, d = bvh.find_nearest(p, 3 / S)
            if loc is not None and (p - loc).dot(n) < 0:
                worst = max(worst, d * S)
        wev.to_mesh_clear()
    return worst


# Base fit (scale.fit_to_base): how much this pose must shrink for its footprint (everything within
# footprint_height_mm of its lowest point) to fit the GW base top with margin_mm to spare. The unit stage takes the
# smallest factor over all poses, so every pose and loadout of a unit prints at one size.
FB = (cfg.get("scale") or {}).get("fit_to_base") or {}
BC = cfg.get("base") or {}


def base_fit_factor():
    if not FB.get("enabled") or cfg.get("class") not in FB.get("classes", []) or BC.get("shape") not in ("round", "oval"):
        return None
    dg = bpy.context.evaluated_depsgraph_get()
    pts = []
    for o in [o for o in bpy.data.objects if o.type == "MESH"]:
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        pts += [(ev.matrix_world @ v.co) * S for v in me.vertices]
        ev.to_mesh_clear()
    z0 = min(p.z for p in pts)
    fh = BC.get("footprint_height_mm", 3.0)
    xy = np.array([(p.x, p.y) for p in pts if p.z < z0 + fh])
    m = FB.get("margin_mm", 1.0)
    if BC["shape"] == "round":
        _, r = basemod.min_circle(basemod.hull2d([(round(x, 3), round(y, 3)) for x, y in xy]))
        return min(1.0, (BC["top_diameter_mm"] / 2 - m) / max(r, 1e-6))
    # oval: the pose stage turns the model so its long axis runs along the oval: test in principal axes
    c = xy.mean(axis=0)
    _, _, vt = np.linalg.svd(xy - c, full_matrices=False)
    q = (xy - c) @ vt.T
    q -= (q.min(axis=0) + q.max(axis=0)) / 2
    L, W = BC["top_mm"]
    worst = np.sqrt((q[:, 0] / (L / 2 - m)) ** 2 + (q[:, 1] / (W / 2 - m)) ** 2).max()
    return min(1.0, 1.0 / max(worst, 1e-6))


cands = []
for clip in cfg["clips"]:
    if clip not in clips:
        continue
    set_clip(clips, clip)
    f0, f1 = clip_range(clips, clip, arm)
    n = min(PS.get("samples_per_clip", 24), f1 - f0 + 1)
    for fr in sorted({round(f0 + i * (f1 - f0) / max(n - 1, 1)) for i in range(n)}):
        sc.frame_set(fr)
        lift, foot_z = 0.0, None
        if has_feet:
            fz = [min((AW @ a.matrix).translation.z, (AW @ b.matrix).translation.z) for a, b in feet.values()]
            lift = (max(fz) - min(fz)) * S
            foot_z = min(fz) * S
        cands.append({"clip": clip, "frame": fr, "lift_mm": round(lift, 2), "clip_mm": round(clip_depth(), 2),
                      "lean_deg": round(lean_deg(), 1), "pitch_deg": round(pitch_deg(), 1), "foot_z": foot_z,
                      "below_feet_mm": round(below_feet(), 2), "base_fit": base_fit_factor(),
                      "sig": signature()})
log(f"scored {len(cands)} frames from {len(set(c['clip'] for c in cands))} clips")
if not cands:
    raise SystemExit("no usable clips for pose selection")

W = PS.get("weights", {})
max_lift = PS.get("max_foot_lift_mm", 0.8)

# hovering: both feet off the ground together (flying monsters idle in the air, jump frames). lift_mm cannot
# see it, so measure the lower foot against the ground level: the rest-pose foot height (bind poses stand on
# the origin) or, if lower, the 10th percentile over all frames
ground = None
if has_feet:
    rest = min(min((AW @ a.bone.head_local).z, (AW @ b.bone.head_local).z) for a, b in feet.values()) * S
    ground = min(rest, float(np.percentile([c["foot_z"] for c in cands], 10)))
for c in cands:
    c["hover_mm"] = round(max(0.0, c.pop("foot_z") - ground), 2) if ground is not None else 0.0
max_hover = PS.get("max_hover_mm", 1.5)


# weapons that rest against the body (rifle stock on the shoulder) always "clip" a little: measure clipping
# above this unit's own typical contact depth (median over all frames), not from zero
base_clip = float(np.median([c["clip_mm"] for c in cands]))


def penalty(c):
    extra = max(0.0, c["clip_mm"] - base_clip - 0.1)
    return (W.get("planted", 3.0) * max(0.0, c["lift_mm"] - max_lift) + W.get("clipping", 2.0) * extra * 5
            + W.get("grounded", 3.0) * max(0.0, c["hover_mm"] - max_hover)
            + W.get("lean", 0.3) * max(0.0, c.get("lean_deg", 0.0) - PS.get("max_lean_deg", 6.0))
            + W.get("pitch", 0.3) * max(0.0, c.get("pitch_deg", 0.0) - PS.get("max_pitch_deg", 90.0))
            + W.get("grounded", 3.0) * max(0.0, c.get("below_feet_mm", 0.0) - PS.get("max_below_feet_mm", 0.5)))


def stance_ok(c):
    return (c["lift_mm"] <= PS.get("hard_max_foot_lift_mm", 4 * max_lift)
            and c["clip_mm"] - base_clip <= PS.get("hard_max_clip_mm", 0.6)
            and c["hover_mm"] <= PS.get("hard_max_hover_mm", 3 * max_hover)
            and c.get("lean_deg", 0.0) <= PS.get("hard_max_lean_deg", 90.0)
            and c.get("pitch_deg", 0.0) <= PS.get("hard_max_pitch_deg", 90.0)
            and c.get("below_feet_mm", 0.0) <= PS.get("hard_max_below_feet_mm", 1.5))


# base fit: frames that would need much more shrinking than this unit's typical (printable) stance - a tail
# stretched along the ground - are left out, so the unit's one size follows its usual footprint
fits = [c["base_fit"] for c in cands if c["base_fit"] is not None and stance_ok(c)]
fit_floor = FB.get("min_relative", 0.85) * float(np.median(fits)) if fits else 0.0


def hard_ok(c):
    return stance_ok(c) and (c.get("base_fit") is None or c["base_fit"] >= fit_floor)


CLIP_ROLE = cfg.get("clip_roles") or {}


def prio(c):
    """Clip preference by role (earlier in role_priority = better), small bonus."""
    roles = PS.get("role_priority", [])
    r = CLIP_ROLE.get(c["clip"], "other")
    if r in roles:
        return 0.05 * (len(roles) - roles.index(r)) / max(len(roles), 1)
    return 0.0


chosen = []
for p in cfg.get("pinned", []):
    hit = next((c for c in cands if c["clip"] == p["clip"] and c["frame"] == p["frame"]), None)
    if hit is None:  # pinned frame not on the sampling grid: evaluate it
        set_clip(clips, p["clip"])
        sc.frame_set(p["frame"])
        hit = {"clip": p["clip"], "frame": p["frame"], "lift_mm": 0.0, "hover_mm": 0.0, "lean_deg": round(lean_deg(), 1),
               "base_fit": base_fit_factor(),
               "pitch_deg": round(pitch_deg(), 1),
               "clip_mm": round(clip_depth(), 2),
               "sig": signature()}
    chosen.append(dict(hit, source="pinned", note=p.get("note", "")))
pool = [c for c in cands if hard_ok(c)]
if len(pool) < N + 4:  # too few clean frames: add the least-penalised others so N poses can always be picked
    pool += sorted((c for c in cands if not hard_ok(c)), key=penalty)[:N + 4 - len(pool)]
min_d = PS.get("min_pose_distance", 0.12)
while len(chosen) < N and pool:
    best, best_v = None, -1e9
    used = {c["clip"] for c in chosen}
    for c in pool:
        if any(c is x for x in chosen):
            continue
        d = min((float(np.sqrt(np.mean((c["sig"] - x["sig"]) ** 2))) for x in chosen), default=1.0)
        if chosen and d < min_d:
            continue
        v = W.get("diversity", 1.0) * d - 0.1 * penalty(c) + prio(c) + (0.05 if c["clip"] not in used else 0.0)
        if v > best_v:
            best, best_v = c, v
    if best is None:
        min_d *= 0.6  # everything is close (e.g. a vehicle with little motion): relax and continue
        if min_d < 1e-3:
            break
        continue
    chosen.append(dict(best, source="auto"))

# a pose that still hovers (too few grounded frames) goes on a flying stand instead of balancing on a toe
fallback = PS.get("hover_fallback_base")
for c in chosen:
    if fallback and c["hover_mm"] > PS.get("hard_max_hover_mm", 3 * max_hover):
        c["overrides"] = {"base_size": fallback}
        c["note"] = (c.get("note", "") + f" hovering {c['hover_mm']} mm: {fallback} stand").strip()
for i, c in enumerate(chosen):
    c["min_distance"] = round(min((float(np.sqrt(np.mean((c["sig"] - x["sig"]) ** 2))) for j, x in enumerate(chosen) if j != i), default=0.0), 3)
    c["penalty"] = round(penalty(c), 2)

# previews of the chosen frames
os.makedirs(os.path.join(cfg["out_dir"], "poses_sheet"), exist_ok=True)
sc.render.engine = "BLENDER_WORKBENCH"
sh = sc.display.shading
sh.light, sh.color_type, sh.show_shadows, sh.shadow_intensity = "STUDIO", "OBJECT", True, 0.3
sc.display.render_aa = "8"
wd = bpy.data.worlds.new("W")
wd.color = (0.92, 0.92, 0.92)
sc.world = wd
for o in bpy.data.objects:
    if o.type == "MESH":
        o.color = (0.78, 0.36, 0.25, 1) if o in weapons else (0.62, 0.62, 0.62, 1)
    if o.type == "EMPTY":
        o.hide_render = True
cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
cam.data.type, cam.data.clip_start, cam.data.clip_end = "ORTHO", 1e-4, 100
rest_xy = [(body.matrix_world @ v.co).xy for v in body.data.vertices]
span = max(H, max(v.x for v in rest_xy) - min(v.x for v in rest_xy), max(v.y for v in rest_xy) - min(v.y for v in rest_xy))
cam.data.ortho_scale = span * 1.35  # long models (bikes, tanks) are framed by their length, not their height
sc.collection.objects.link(cam)
sc.camera = cam
sc.render.resolution_x, sc.render.resolution_y = 360, 440
for i, c in enumerate(chosen):
    set_clip(clips, c["clip"])
    sc.frame_set(c["frame"])
    tgt = (AW @ root.matrix).translation
    tgt = Vector((tgt.x, tgt.y, rest_lo + H * 0.5))
    a, e = math.radians(-30), math.radians(10)
    cam.location = tgt + Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e))) * span * 4
    cam.rotation_euler = (tgt - cam.location).to_track_quat("-Z", "Y").to_euler()
    c["preview"] = os.path.join(cfg["out_dir"], "poses_sheet", f"{i + 1}_{c['clip']}_f{c['frame']}.png")
    sc.render.filepath = c["preview"]
    bpy.ops.render.render(write_still=True)

out = [{k: v for k, v in c.items() if k != "sig"} for c in chosen]
with open(os.path.join(cfg["out_dir"], "poses.json"), "w") as f:
    json.dump({"poses": out, "candidates": len(cands), "clean_candidates": sum(1 for c in cands if hard_ok(c)),
               "typical_clip_mm": round(base_clip, 2), "clips": sorted({c["clip"] for c in cands}),
               "feet_scored": has_feet, "ground_mm": None if ground is None else round(ground, 2),
               "base_fit_factor": min((round(c["base_fit"], 4) for c in chosen if c.get("base_fit") is not None), default=None),
               "base_fit_median": round(float(np.median(fits)), 4) if fits else None,
               "grounded_candidates": sum(1 for c in cands if c["hover_mm"] <= max_hover)}, f, indent=1)
log("POSES " + json.dumps([(c["clip"], c["frame"], c["source"], c["lift_mm"], c["hover_mm"], c.get("lean_deg"), c["clip_mm"],
                            c["min_distance"]) for c in out]))
stage("pose_select", n=len(out))
