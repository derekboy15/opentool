# SPDX-License-Identifier: GPL-3.0-or-later
"""Pose stage: freeze one frame of a game animation clip into a static pose (no weapon yet).

cfg["pose"]: clip, frame, reparent [{object, bone, mirror_of | offset_mm}], loose_items {patterns, to_bone,
hip_offset_mm}, feet {enabled, mode, min_embed_mm}, previews.  cfg["rig_map"]: canonical role -> bone.
cfg["anim_fbx"]: the package model (rig + skinned meshes + clips).
Writes <out>/pose.blend (rig holding the frame, actions removed; IK targets kept so a lowered foot stays
lowered) and <out>/pose.json (clip, frame, feet report, key points in rig mm).

Feet: the lowest skin vertex of each foot is measured; a foot that would not reach min_embed_mm into the base
(top at base.sink_mm) is lowered with leg IK by the minimum (orientation kept). mode "rubble" leaves the pose.
Held props: explicit `reparent` entries first; then any object matching loose_items.patterns that hangs off a
hand bone moves to the belt (mirrored from a matching item already there, else offset to the hip).
"""
import fnmatch
import json
import math
import os
import sys

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anim import apply_mesh_groups, base_name, bone_matches, clip_range, find_bone, is_hidden, load_animated, remove_tree, set_clip  # noqa: E402
from common import load_cfg, log, stage  # noqa: E402
import rig as rigmod  # noqa: E402

cfg = load_cfg()
P = cfg["pose"]
RM = cfg.get("rig_map") or {}
S = cfg["scale"]["factor"]

arm, clips = load_animated(cfg["anim_fbx"])
shown_groups, _ = apply_mesh_groups(cfg, log)
clip, frame = P["clip"], P["frame"]

# objects the prefab keeps inactive / with disabled renderers are not shown in the game: drop them, except
# loose items (grenades...), which may go to the belt below
LI = P.get("loose_items") or {}


def is_loose(o):
    return any(fnmatch.fnmatch(o.name, pat) or fnmatch.fnmatch(base_name(o), pat) for pat in LI.get("patterns", []))


hidden_paths = cfg.get("hidden", [])
explicit = {rp["object"] for rp in P.get("reparent", [])} | {rp.get("mirror_of") for rp in P.get("reparent", [])}
hidden_objs = [o.name for o in bpy.data.objects if is_hidden(o, hidden_paths)]
inactive_loose = {nm for nm in hidden_objs if is_loose(bpy.data.objects[nm]) or nm in explicit}
dropped = []
for nm in hidden_objs:
    if nm not in bpy.data.objects or nm in inactive_loose:
        continue
    o, anc = bpy.data.objects[nm], bpy.data.objects[nm].parent
    while anc is not None and anc.name not in inactive_loose:
        anc = anc.parent
    if anc is not None:
        continue  # part of a kept loose item (the grenade's pull-ring): it moves with it
    dropped.append(nm)
    remove_tree(o)
if dropped:
    log(f"dropped {len(dropped)} object(s) the game keeps hidden: {', '.join(sorted(dropped))}")
if clip not in clips:
    raise SystemExit(f"clip {clip!r} not in the animator FBX (has: {', '.join(sorted(clips))})")
set_clip(clips, clip)
f0, f1 = clip_range(clips, clip, arm)
sc = bpy.context.scene
sc.frame_set(frame)
log(f"clip {clip} frame {frame} (range {f0}-{f1})")

# ---------------------------------------------------------------- freeze the frame
AW = arm.matrix_world.copy()
pose_m = {pb.name: pb.matrix.copy() for pb in arm.pose.bones}
obj_m = {o.name: o.matrix_world.copy() for o in bpy.data.objects if o.type in ("EMPTY", "MESH")}
for o in bpy.data.objects:
    if o.animation_data:
        o.animation_data.action = None


def depth(pb):
    d = 0
    while pb.parent:
        d, pb = d + 1, pb.parent
    return d


for pb in sorted(arm.pose.bones, key=depth):
    pb.matrix = pose_m[pb.name]
    bpy.context.view_layer.update()


def odepth(o):
    d = 0
    while o.parent:
        d, o = d + 1, o.parent
    return d


for o in sorted((o for o in bpy.data.objects if o.name in obj_m), key=odepth):
    if o.type == "EMPTY":
        o.matrix_world = obj_m[o.name]
        bpy.context.view_layer.update()
for a in list(bpy.data.actions):
    bpy.data.actions.remove(a)
arm.name = "Rig"  # after the actions (matched by object name) are gone
drift = max((arm.pose.bones[n].matrix.translation - m.translation).length for n, m in pose_m.items()) * S
log(f"frozen: {len(pose_m)} bones, max drift {drift:.4f} mm")
stage("pose_freeze", clip=clip, frame=frame)


def bone(suffix, required=True):
    b = find_bone(arm, suffix)
    if b is None and required:
        raise SystemExit(f"rig has no bone ending in {suffix!r}")
    return b


def rbone(key, s=None, required=True):
    b = rigmod.bone(arm, RM, key, s)
    if b is None and required:
        raise SystemExit(f"rig role {key}{'.' + s if s else ''} not mapped / not on the rig")
    return b


def head(pb):
    return (AW @ pb.matrix).translation


def mirror_about_pelvis(src, pel):
    """Mirror src's placement across the pelvis bone's own left/right plane."""
    Mpel = AW @ pel.matrix
    rel = Mpel.inverted() @ src.matrix_world
    rest = (AW @ pel.bone.matrix_local).to_3x3().normalized()
    side_axis = max(range(3), key=lambda i: abs(rest.col[i].x))
    Mx = Matrix.Identity(4)
    Mx[side_axis][side_axis] = -1
    return Mpel @ (Mx @ rel @ Mx)


def reparent(ob, bone_name, W):
    ob.parent = arm
    ob.parent_type = "BONE"
    ob.parent_bone = bone_name
    bpy.context.view_layer.update()
    ob.matrix_world = W
    bpy.context.view_layer.update()
    log(f"re-parented {ob.name} to {ob.parent_bone} at {tuple(round(x * S, 2) for x in W.translation)} mm")


# ---------------------------------------------------------------- props (grenade to the belt)
moved = set()
for rp in P.get("reparent", []):
    ob = bpy.data.objects[rp["object"]]
    tb = bone(rp["bone"]) if rp.get("bone") else rbone(rp.get("role", "pelvis"))
    W = mirror_about_pelvis(bpy.data.objects[rp["mirror_of"]], tb) if "mirror_of" in rp else ob.matrix_world.copy()
    if "offset_mm" in rp:
        W = Matrix.Translation(Vector(rp["offset_mm"]) / S) @ W
    reparent(ob, tb.name, W)
    moved.add(ob.name)

hand_set = rigmod.hand_names(arm, RM, LI.get("hand_bone_patterns") or ())


def on_hand(o):
    p = o
    while p is not None:
        if p.parent_type == "BONE" and p.parent_bone in hand_set:
            return True
        p = p.parent
    return False


for nm in [o.name for o in bpy.data.objects if o.type == "MESH" and o.name not in moved]:
    if nm not in bpy.data.objects:
        continue
    ob = bpy.data.objects[nm]
    if not is_loose(ob):
        continue
    hidden_item = ob.name in inactive_loose
    if not on_hand(ob) and not hidden_item:
        continue
    pel = bone(LI["to_bone"]) if LI.get("to_bone") else rbone(LI.get("to_role", "pelvis"))
    base = ob.name.split(".")[0]
    twin = next((o for o in bpy.data.objects if o is not ob and o.type == "MESH" and o.name.split(".")[0] == base
                 and not on_hand(o)), None)
    if twin:
        W = mirror_about_pelvis(twin, pel)
        how = f"mirrored from {twin.name}"
    elif hidden_item:  # the game only shows it during a throw: without a belt twin to mirror, leave it off
        nm = ob.name
        remove_tree(ob)
        log(f"loose item {nm} removed: hidden in the game, no twin on the belt")
        continue
    else:
        W = Matrix.Translation(head(pel) + Vector(LI.get("hip_offset_mm", [5, 0, -1])) / S) @ Matrix.Diagonal(
            ob.matrix_world.to_scale().to_4d()) @ ob.matrix_world.to_quaternion().to_matrix().to_4x4()
        how = "hip offset (no twin on the belt)"
    reparent(ob, pel.name, W)
    moved.add(ob.name)
    log(f"loose item {ob.name}: {how}")
stage("pose_props", moved=sorted(moved))

# ---------------------------------------------------------------- feet
body = max((o for o in bpy.data.objects if o.type == "MESH"), key=lambda o: len(o.data.vertices))
gnames = {g.index: g.name for g in body.vertex_groups}
FT = P.get("feet", {})
foot_report = {"action": "none"}
sides = list(rigmod.SIDES)
has_feet = FT.get("enabled", True) and all(rbone("ankle", s, False) for s in sides)
if FT.get("enabled", True) and not has_feet:
    log("WARNING feet onto the base switched off: rig roles ankle.L / ankle.R are not both mapped")


def foot_lows():
    """Lowest foot skin per side, over every mesh (some rigs keep the legs in their own mesh)."""
    dg = bpy.context.evaluated_depsgraph_get()
    lows = {s: 1e9 for s in sides}
    keys = {s: (rigmod.role_name(RM, "ball", s) or "<no ball role>", rigmod.role_name(RM, "ankle", s)) for s in sides}
    for o in [o for o in bpy.data.objects if o.type == "MESH" and o.vertex_groups]:
        names = {g.index: g.name for g in o.vertex_groups}
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        for v, sv in zip(me.vertices, o.data.vertices):
            gs = [(g.weight, names.get(g.group, "")) for g in sv.groups if g.weight > 0]
            if not gs:
                continue
            g = max(gs)[1]
            for s in sides:
                if bone_matches(g, keys[s][0]) or bone_matches(g, keys[s][1]):
                    lows[s] = min(lows[s], (ev.matrix_world @ v.co).z)
        ev.to_mesh_clear()
    return lows


if has_feet:
    lows = foot_lows()
    floor = min(lows.values())
    gaps = {s: (z - floor) * S for s, z in lows.items()}
    log("feet: lowest skin above the floor " + ", ".join(f"{s} {g:.2f} mm" for s, g in gaps.items()))
    sink = (cfg.get("base") or {}).get("sink_mm", 0.3)
    need = FT.get("min_embed_mm", 0.15)
    lifted = {s: g for s, g in gaps.items() if g > sink - need}
    foot_report = {"gaps_mm": {s: round(g, 3) for s, g in gaps.items()}, "action": "none"}
    if lifted and FT.get("mode", "lower") == "lower":
        for s, g in lifted.items():
            drop = (g - (sink - need) + 0.02) / S
            tgt = bpy.data.objects.new(f"IK_foot_{s.upper()}", None)
            tgt.empty_display_size = 0.0005
            sc.collection.objects.link(tgt)
            tgt.location = head(rbone("ankle", s)) - Vector((0, 0, drop))
            # IK on the knee; rigs without one (Tyranid Knee1/Knee2 legs) use the ankle's parent bone
            kn = rbone("knee", s, False) or rbone("ankle", s).parent
            c = kn.constraints.new("IK")
            c.name, c.target, c.chain_count = f"IK_leg_{s.upper()}", tgt, 2
            ank = rbone("ankle", s)
            fe = bpy.data.objects.new(f"foot_{s.upper()}_orient", None)
            fe.empty_display_size = 0.0005
            sc.collection.objects.link(fe)
            fe.matrix_world = Matrix.LocRotScale(tgt.location, (AW @ ank.matrix).to_quaternion(), Vector((1, 1, 1)))
            cr = ank.constraints.new("COPY_ROTATION")
            cr.name, cr.target = f"foot_{s.upper()}_keep", fe
            bpy.context.view_layer.update()
            foot_report["action"] = "lower"
            foot_report.setdefault("lowered_mm", {})[s] = round(drop * S, 3)
            log(f"foot {s}: {g:.2f} mm up would not reach {need} mm into the base (sink {sink} mm): lowered {drop * S:.2f} mm with leg IK")
        lows = foot_lows()
        floor = min(lows.values())
        foot_report["gaps_after_mm"] = {s: round((z - floor) * S, 3) for s, z in lows.items()}
        log("feet after: " + ", ".join(f"{s} {g} mm" for s, g in foot_report["gaps_after_mm"].items()))
    elif lifted:
        foot_report["action"] = "rubble"
        foot_report["rubble_under"] = {s: [round(x * S, 2) for x in head(rbone("ball", s))] for s in lifted}
else:
    foot_report["action"] = "skipped (no feet for this class/rig)"
stage("pose_feet", **{k: v for k, v in foot_report.items() if k in ("action", "lowered_mm")})

# ---------------------------------------------------------------- oval base: long axis front-back
# the oval's long axis runs along model y (base.py); turn the posed model about z so its plan-view long axis
# (principal axis of every mesh vertex) lies along y. Root objects turn together: rig, IK targets, props.
yaw_deg = 0.0
if (cfg.get("base") or {}).get("shape") == "oval" and P.get("align_oval", True):
    dg = bpy.context.evaluated_depsgraph_get()
    xy = []
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        xy += [(ev.matrix_world @ v.co).xy for v in me.vertices]
        ev.to_mesh_clear()
    n = len(xy)
    cx, cy = sum(v.x for v in xy) / n, sum(v.y for v in xy) / n
    sxx = sum((v.x - cx) ** 2 for v in xy) / n
    syy = sum((v.y - cy) ** 2 for v in xy) / n
    sxy = sum((v.x - cx) * (v.y - cy) for v in xy) / n
    ang = 0.5 * math.atan2(2 * sxy, sxx - syy)  # major axis angle from +x
    l1 = (sxx + syy) / 2 + math.sqrt(((sxx - syy) / 2) ** 2 + sxy ** 2)
    l2 = (sxx + syy) / 2 - math.sqrt(((sxx - syy) / 2) ** 2 + sxy ** 2)
    rot = math.pi / 2 - ang  # bring the major axis onto +y ...
    rot = (rot + math.pi / 2) % math.pi - math.pi / 2  # ... or -y, whichever is the smaller turn
    if l1 > 1.3 * max(l2, 1e-12) and abs(math.degrees(rot)) > 1.0:
        Rz = Matrix.Rotation(rot, 4, "Z")
        for o in [o for o in bpy.data.objects if o.parent is None]:
            o.matrix_world = Rz @ o.matrix_world
        bpy.context.view_layer.update()
        AW = arm.matrix_world.copy()
        yaw_deg = round(math.degrees(rot), 2)
        log(f"oval base: turned the model {yaw_deg} deg about z (plan-view long axis onto y; axis ratio {math.sqrt(l1 / max(l2, 1e-12)):.2f})")
stage("pose_align", yaw_deg=yaw_deg)

# ---------------------------------------------------------------- key points for QA close-ups (rig mm)
pts = {}
for s, side in zip(sides, ("left", "right")):
    for key, label in (("wrist", "hand"), ("knee", "knee")):
        b = rbone(key, s, required=False)
        if b:
            pts[f"{side}_{label}"] = head(b)
for name in sorted(moved):
    pts[name] = bpy.data.objects[name].matrix_world.translation
info = {"clip": clip, "frame": frame, "feet": foot_report, "props_moved": sorted(moved), "yaw_deg": yaw_deg,
        "points_rig_mm": {k: [round(x * S, 2) for x in v] for k, v in pts.items()}}
with open(os.path.join(cfg["out_dir"], "pose.json"), "w") as f:
    json.dump(info, f, indent=2)
log("POSE " + json.dumps(info))

blend = os.path.join(cfg["out_dir"], "pose.blend")
for o in bpy.data.objects:
    o.select_set(False)
bpy.ops.wm.save_as_mainfile(filepath=blend)
log("saved", blend)
stage("pose_saved")
