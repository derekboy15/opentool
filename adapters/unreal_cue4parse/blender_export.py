# SPDX-License-Identifier: GPL-3.0-or-later
"""Unreal export, Blender half (runs in Blender 5.2 through the capped runner): USD from otue -> package glTF.

cfg: mesh_usd, anims [{name, usd}], weapons [{id, usd}], sockets [names to keep as mount points], out_dir, stem.

1. The skeletal mesh USD imports with Blender's own USD importer (no add-on). Scale is applied (metres, identity
   object transforms), so the back end's weapon maths sees an identity armature root.
2. Clips: Blender's importer would give every clip its own armature with the *skeleton asset's* rest pose, which
   differs from the mesh's (face, helmet and hair joints move by up to 15 cm on the Moria orcs). So clips are read
   with USD's own Python API (pxr, bundled with Blender) and retargeted the way Unreal plays them: rotation and
   scale from the clip, translation = mesh rest + (clip - skeleton rest), so a bone the clip moves keeps that
   motion and a bone it doesn't keeps the mesh's proportions. Each clip -> one action -> one NLA track.
3. Sockets: Unreal sockets (bone + offset, USD Xforms under SkelRoot/Sockets) become empties parented to their
   bone, named after the socket, so the back end mounts weapons on them like Battlesector's joint empties.
4. Each weapon static mesh USD -> weapons/<id>.glb, pivot at the origin (Unreal mounts by the mesh pivot).
Writes <out>/model/<stem>.glb, <out>/weapons/*.glb, <out>/export.json; prints RESULT_JSON.
"""
import json
import math
import re
import os
import pathlib
import sys

import bpy
from mathutils import Matrix, Quaternion, Vector
from pxr import Usd, UsdGeom, UsdSkel

assert bpy.app.version[:2] == (5, 2), f"Blender 5.2 needed, this is {bpy.app.version_string}"
cfg = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
out = pathlib.Path(cfg["out_dir"])
(out / "model").mkdir(parents=True, exist_ok=True)
(out / "weapons").mkdir(parents=True, exist_ok=True)
scene = bpy.context.scene
warnings = []


def leaf(path):
    n = str(path).split("/")[-1]
    return ORIG.get(n, n)


def mat_trs(t, q, s, k):
    """Local transform from USD translation (scaled by k metres/unit), quaternion, scale (column-vector maths)."""
    qq = Quaternion((q.GetReal(), *q.GetImaginary()))
    return Matrix.Translation(Vector(t) * k) @ qq.to_matrix().to_4x4() @ Matrix.Diagonal((*s, 1.0))


def gf_to_matrix(m, k):
    """Gf.Matrix4d (row-vector) -> mathutils (column-vector), translation scaled by k."""
    M = Matrix([[m[r][c] for c in range(4)] for r in range(4)]).transposed()
    M.translation = M.translation * k
    return M


SAFE = {}  # original joint name -> USD-safe name (Unreal allows '-', ' ' etc. in bone names; USD prim names do not)


def safe_name(n):
    if n not in SAFE:
        s = re.sub(r"[^A-Za-z0-9_]", "_", n)
        s = "_" + s if s[:1].isdigit() or not s else s
        base, k = s, 1
        while s in SAFE.values():
            s, k = f"{base}_{k}", k + 1
        SAFE[n] = s
    return SAFE[n]


def usd_safe(path):
    """A copy of an otue .usda whose joint tokens are valid USD paths (same folder, '_safe' suffix). Joint arrays
    are the only place bone names become paths; socket bone names stay strings."""
    txt = open(path, encoding="utf-8").read()

    def fix_tokens(m):
        toks = re.findall(r'"([^"]*)"', m.group(2))
        return m.group(1) + "[" + ", ".join('"' + "/".join(safe_name(c) for c in t.split("/")) + '"' for t in toks) + "]"
    new = re.sub(r"(uniform token\[\] joints = )\[([^\]]*)\]", fix_tokens, txt)
    if new == txt:
        return path
    out_p = path[:-5] + "_safe.usda"
    open(out_p, "w", encoding="utf-8").write(new)
    return out_p


def orig(n):
    """USD-safe joint leaf -> original Unreal bone name."""
    return ORIG.get(n, n)


ORIG = {}


def open_skel(path):
    st = Usd.Stage.Open(path)
    k = UsdGeom.GetStageMetersPerUnit(st) or 0.01
    sk = next(UsdSkel.Skeleton(p) for p in st.Traverse() if p.IsA(UsdSkel.Skeleton))
    return st, k, sk


# ---------------------------------------------------------------- weapons first (each exported alone)
weapons = []
for w in cfg.get("weapons", []):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.usd_import(filepath=w["usd"], import_materials=False, import_skeletons=False, scale=1.0)
    ms = [o for o in bpy.data.objects if o.type == "MESH"]
    if not ms:
        warnings.append(f"weapon {w['id']}: no mesh in {w['usd']}")
        continue
    for o in bpy.data.objects:
        o.select_set(True)
    bpy.context.view_layer.objects.active = ms[0]
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    for o in [o for o in bpy.data.objects if o.type != "MESH"]:
        bpy.data.objects.remove(o, do_unlink=True)
    if len(ms) > 1:
        bpy.context.view_layer.objects.active = ms[0]
        for o in ms:
            o.select_set(True)
        bpy.ops.object.join()
    wo = next(o for o in bpy.data.objects if o.type == "MESH")
    wo.name = w["id"]
    wo.data.calc_loop_triangles()
    co = [v.co for v in wo.data.vertices]
    size = [round(max(c[i] for c in co) - min(c[i] for c in co), 4) for i in range(3)]
    dst = out / "weapons" / f"{w['id']}.glb"
    bpy.ops.export_scene.gltf(filepath=str(dst), export_format="GLB", export_yup=True, export_apply=True,
                              export_materials="NONE", export_animations=False)
    weapons.append({"id": w["id"], "file": f"weapons/{dst.name}", "tris": len(wo.data.loop_triangles), "size_m": size})

# ---------------------------------------------------------------- the skeletal mesh
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene  # the reset replaced the scene
scene.render.fps = 30
mesh_usd = usd_safe(cfg["mesh_usd"])
ORIG.update({v: k for k, v in SAFE.items()})
bpy.ops.wm.usd_import(filepath=mesh_usd, import_skeletons=True, import_blendshapes=False,
                      import_materials=True, scale=1.0)
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
for b in arm.data.bones:  # back to the game's own bone names (rig_map, sockets and clips use them)
    if b.name in ORIG and ORIG[b.name] != b.name:
        b.name = ORIG[b.name]
for o in bpy.data.objects:
    if o.type == "MESH":
        for vg in o.vertex_groups:
            if vg.name in ORIG and ORIG[vg.name] != vg.name and ORIG[vg.name] not in o.vertex_groups:
                vg.name = ORIG[vg.name]
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
# the importer puts metersPerUnit (0.01) on the SkelRoot empty above the armature: unparent keeping the world
# transform, then apply it, so the package is in metres with identity object transforms
for o in [arm] + meshes:
    W = o.matrix_world.copy()
    if o.parent is not None and o.parent.type != "ARMATURE":
        o.parent = None
        o.matrix_world = W
bpy.context.view_layer.update()
for o in [o for o in bpy.data.objects if o.type not in ("ARMATURE", "MESH")]:
    bpy.data.objects.remove(o, do_unlink=True)
for o in bpy.data.objects:
    o.select_set(True)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)  # metres, identity roots
bpy.context.view_layer.update()
dg0 = bpy.context.evaluated_depsgraph_get()
for o in meshes:  # rest pose must deform to itself, or the skin and the bones disagree
    ev = o.evaluated_get(dg0)
    m = ev.to_mesh()
    d = max(((ev.matrix_world @ a.co) - (o.matrix_world @ b.co)).length for a, b in zip(m.vertices, o.data.vertices)) if len(m.vertices) == len(o.data.vertices) else -1
    ev.to_mesh_clear()
    if d > 1e-3:
        warnings.append(f"{o.name}: rest pose moves the skin by up to {d * 1000:.1f} mm (bind/rest mismatch)")
arm.name = "Armature"
bones = arm.data.bones

st, K, msk = open_skel(mesh_usd)
joints = [str(j) for j in msk.GetJointsAttr().Get()]
names = [leaf(j) for j in joints]
parent = [names.index(leaf(j.rsplit("/", 1)[0])) if "/" in j else -1 for j in joints]
rest_local = [gf_to_matrix(m, K) for m in msk.GetRestTransformsAttr().Get()]
bind = msk.GetBindTransformsAttr().Get()
bind_skel = [gf_to_matrix(m, K) for m in bind] if bind else None
rest_skel = []
for i, L in enumerate(rest_local):
    rest_skel.append(L if parent[i] < 0 else rest_skel[parent[i]] @ L)
if bind_skel:
    d = max((rest_skel[i].translation - bind_skel[i].translation).length for i in range(len(names)))
    if d > 1e-4:
        warnings.append(f"mesh rest pose differs from its bind pose by up to {d * 1000:.1f} mm; bones follow the bind pose")
ref_skel = bind_skel or rest_skel
missing = [n for n in names if n not in bones]
if missing:
    warnings.append(f"{len(missing)} skeleton joints have no Blender bone: {missing[:6]}")
# constant per-bone correction between the USD joint frame and the Blender bone frame (bone axes are Blender's own)
C = {n: ref_skel[i].inverted() @ bones[n].matrix_local for i, n in enumerate(names) if n in bones}
order = [i for i, n in enumerate(names) if n in bones]


def key_clip(name, anim_usd):
    anim_usd = usd_safe(anim_usd)
    ORIG.update({v: k for k, v in SAFE.items()})
    ast, ak, ask = open_skel(anim_usd)
    an = next(UsdSkel.Animation(p) for p in ast.Traverse() if p.IsA(UsdSkel.Animation))
    aj = [leaf(j) for j in an.GetJointsAttr().Get()]
    aidx = {n: i for i, n in enumerate(aj)}
    sj = [leaf(j) for j in ask.GetJointsAttr().Get()]
    srest = ask.GetRestTransformsAttr().Get()
    srest_t = {n: Vector(gf_to_matrix(srest[i], ak).translation) for i, n in enumerate(sj)}
    times = an.GetRotationsAttr().GetTimeSamples() or [0.0]
    tps = ast.GetTimeCodesPerSecond() or 30.0
    act = bpy.data.actions.new(name)
    slot = act.slots.new(id_type="OBJECT", name=arm.name)
    layer = act.layers.new("Layer")
    strip = layer.strips.new(type="KEYFRAME")
    bag = strip.channelbag(slot, ensure=True)
    curves = {}
    for n in names:
        if n not in bones:
            continue
        pb = f'pose.bones["{n}"]'
        curves[n] = ([bag.fcurves.new(f"{pb}.location", index=i) for i in range(3)],
                     [bag.fcurves.new(f"{pb}.rotation_quaternion", index=i) for i in range(4)],
                     [bag.fcurves.new(f"{pb}.scale", index=i) for i in range(3)])
    data = {n: ([], [], []) for n in curves}
    frames = []
    for t in times:
        T = an.GetTranslationsAttr().Get(t)
        R = an.GetRotationsAttr().Get(t)
        S = an.GetScalesAttr().Get(t)
        skel = [None] * len(names)
        for i, n in enumerate(names):
            j = aidx.get(n)
            if j is None:
                L = rest_local[i]
            else:
                tr = rest_local[i].translation + (Vector(T[j]) * ak - srest_t.get(n, Vector(T[j]) * ak))
                L = mat_trs(tr / ak, R[j], S[j] if S else (1, 1, 1), ak)
            skel[i] = L if parent[i] < 0 else skel[parent[i]] @ L
        posed = {}
        for i in order:
            n = names[i]
            B = skel[i] @ C[n]  # bone matrix in armature space
            b = bones[n]
            if b.parent is None:
                basis = b.matrix_local.inverted() @ B
            else:
                Pp = posed[b.parent.name] if b.parent.name in posed else b.parent.matrix_local
                basis = (b.parent.matrix_local.inverted() @ b.matrix_local).inverted() @ Pp.inverted() @ B
            posed[n] = B
            loc, rot, sca = basis.decompose()
            data[n][0].append(loc)
            data[n][1].append(rot)
            data[n][2].append(sca)
        frames.append(round(t))
    for n, (lc, rc, sc) in curves.items():
        L, Rq, S = data[n]
        for k in range(1, len(Rq)):  # keep quaternions on one hemisphere so interpolation does not flip
            if Rq[k].dot(Rq[k - 1]) < 0:
                Rq[k] = -Rq[k]
        for fcs, vals, dim in ((lc, L, 3), (rc, Rq, 4), (sc, S, 3)):
            for i, fc in enumerate(fcs):
                fc.keyframe_points.add(len(frames))
                flat = []
                for f, v in zip(frames, vals):
                    flat += [f, v[i]]
                fc.keyframe_points.foreach_set("co", flat)
                fc.keyframe_points.foreach_set("interpolation", [1] * len(frames))  # LINEAR
                fc.update()
    act.frame_range = (frames[0], frames[-1])
    return act, slot, {"name": name, "frames": [frames[0], frames[-1]], "fps": round(tps, 3), "joints_in_clip": len(aj),
                       "joints_matched": sum(1 for n in names if n in aidx)}


for pb in arm.pose.bones:
    pb.rotation_mode = "QUATERNION"
clips = []
arm.animation_data_create()
for c in cfg.get("anims", []):
    try:
        act, slot, info = key_clip(c["name"], c["usd"])
    except Exception as e:  # noqa: BLE001
        warnings.append(f"clip {c['name']}: {e!r}")
        continue
    tr = arm.animation_data.nla_tracks.new()
    tr.name = act.name
    s = tr.strips.new(act.name, int(act.frame_range[0]), act)
    s.action_slot = slot
    clips.append(info)
arm.animation_data.action = None

# ---------------------------------------------------------------- sockets -> bone-parented empties
sockets = []
want = set(cfg.get("sockets") or [])
arm.data.pose_position = "REST"  # place sockets against the rest pose (the NLA clips would pose the bones now)
bpy.context.view_layer.update()
for p in st.Traverse():
    if not p.IsA(UsdGeom.Xform) or p.GetParent().GetName() != "Sockets":
        continue
    nm = p.GetName()
    bone_attr = p.GetAttribute("unrealBoneName")
    bn = bone_attr.Get() if bone_attr else None
    if want and nm not in want or bn not in bones:
        continue
    local = UsdGeom.Xformable(p).GetLocalTransformation()
    S_local = gf_to_matrix(local, K)
    i = names.index(bn)
    W = ref_skel[i] @ S_local  # rest pose, armature space (= world: identity root)
    e = bpy.data.objects.new(nm, None)
    e.empty_display_size = 0.05
    scene.collection.objects.link(e)
    e.parent, e.parent_type, e.parent_bone = arm, "BONE", bn
    bpy.context.view_layer.update()
    e.matrix_world = W
    sockets.append({"name": nm, "bone": bn, "offset_m": [round(x, 5) for x in S_local.translation]})

arm.data.pose_position = "POSE"
bpy.context.view_layer.update()

# ---------------------------------------------------------------- stats + export
for o in meshes:
    o.data.calc_loop_triangles()
    if o.animation_data:
        o.animation_data_clear()
dg = bpy.context.evaluated_depsgraph_get()
zs = [v.co.z for o in meshes for v in o.data.vertices]
xs = [v.co.x for o in meshes for v in o.data.vertices]
ys = [v.co.y for o in meshes for v in o.data.vertices]
stats = {"meshes": len(meshes), "tris_all": sum(len(o.data.loop_triangles) for o in meshes), "bones": len(bones),
         "clips": len(clips), "height_m": round(max(zs) - min(zs), 4),
         "size_m": [round(max(xs) - min(xs), 4), round(max(ys) - min(ys), 4), round(max(zs) - min(zs), 4)],
         "materials": sorted({m.name for o in meshes for m in o.data.materials if m})}
for pb in arm.pose.bones:
    pb.custom_shape = None
glb = out / "model" / f"{cfg['stem']}.glb"
bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", export_animations=True,
                          export_animation_mode="NLA_TRACKS", export_skins=True, export_yup=True,
                          export_apply=False, export_materials="PLACEHOLDER", export_def_bones=False,
                          export_force_sampling=True, export_frame_range=False)  # material names = texture-set keys
info = {"stats": stats, "clips": clips, "sockets": sockets, "weapons": weapons, "model": f"model/{glb.name}",
        "bone_names": [b.name for b in bones], "mesh_names": [o.name for o in meshes], "warnings": warnings,
        "meters_per_unit_source": K}
(out / "export.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
print("RESULT_JSON:" + json.dumps({"ok": True, "stats": stats, "clips": len(clips), "sockets": len(sockets),
                                   "weapons": len(weapons), "warnings": warnings[:5]}))
