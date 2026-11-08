# SPDX-License-Identifier: GPL-3.0-or-later
"""Source 1 export, Blender half (runs in Blender 5.2 through the capped runner): one .mdl -> package glTF.

cfg: addon_parent, game_dir, model (game path), stem, out_dir, clips [{name, anim}] (sequence name -> the anim desc
that holds its frames), weapons [{id, model, bone}], attachments [names to keep], eye_attachment.

1. SourceIO (enabled for this run only) mounts the game from gameinfo.txt and imports the model with its materials.
   Its armature is built from the MDL's own bone matrices, so a Blender bone's frame is the game bone's frame.
2. Body groups: a body part with one choice is always shown; a body part with several choices becomes mesh groups
   (one per choice, object names kept), and the package's loadouts show choice 0, the game's default.
3. Clips: only the requested sequences' animations are loaded (survivors keep ~2,000 in their $includemodel files),
   each -> one action named after the sequence -> one NLA track.
4. Attachments the profile asks for become empties parented to their bone at the rest pose (the MDL's own offset),
   the eye attachment also an extra non-deforming bone so the back end's `eye` rig role resolves.
5. Weapons: each world model (w_*.mdl) is placed by bonemerge in the game: its bones follow the character's bones of
   the same name. The weapon mesh is written in the frame of its mount bone (ValveBiped.weapon_bone), and the
   character carries an empty `bm_<bone>` on that bone, so mounting the weapon at the empty's origin is the bonemerge.
Writes <out>/model/<stem>.glb, <out>/weapons/*.glb, <out>/textures/*.png, <out>/export.json; prints RESULT_JSON.
"""
import json
import os
import pathlib
import re
import sys

import bpy
from mathutils import Euler, Matrix, Vector

assert bpy.app.version[:2] == (5, 2), f"Blender 5.2 needed, this is {bpy.app.version_string}"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sio  # noqa: E402

cfg = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
out = pathlib.Path(cfg["out_dir"])
for d in ("model", "weapons", "textures"):
    (out / d).mkdir(parents=True, exist_ok=True)
warnings = []
sio.enable(cfg["addon_parent"])
cm = sio.mount(cfg["game_dir"])
from SourceIO.blender_bindings.models import import_model  # noqa: E402
from SourceIO.blender_bindings.models.common import put_into_collections  # noqa: E402
from SourceIO.blender_bindings.models.import_animations import import_animations_to_armature  # noqa: E402
from SourceIO.blender_bindings.operators.import_settings_base import ModelOptions  # noqa: E402
from SourceIO.library.models.mdl.load_animations import load_all_animations  # noqa: E402
from SourceIO.library.utils.tiny_path import TinyPath  # noqa: E402

scene = bpy.context.scene
scene.render.fps = 30


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.armatures, bpy.data.actions):
        for x in list(coll):
            if x.users == 0 or coll is bpy.data.actions:
                coll.remove(x)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)


def load(path, textures):
    buf = cm.find_file(TinyPath(path))
    if buf is None:
        raise FileNotFoundError(f"{path} is not in the mounted game")
    opts = ModelOptions.default()
    opts.import_textures = textures
    opts.import_animations = False
    opts.write_qc = False
    opts.create_flex_drivers = False
    cont = import_model(TinyPath(path), buf, cm, opts, None)
    put_into_collections(cont, TinyPath(path).stem, bodygroup_grouping=True)
    return cont, opts.scale


def rest_world_mesh(o):
    """Evaluated mesh at the armature's rest pose, in world space (a new datablock)."""
    dg = bpy.context.evaluated_depsgraph_get()
    ev = o.evaluated_get(dg)
    me = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=False, depsgraph=dg)
    me.transform(o.matrix_world)
    return me


def size_of(co):
    return [round(max(c[i] for c in co) - min(c[i] for c in co), 4) for i in range(3)] if co else [0, 0, 0]


# ---------------------------------------------------------------- weapons (each alone, then cleared)
weapons = []
for w in cfg.get("weapons", []):
    clear()
    cont, _ = load(w["model"], textures=False)
    arm = cont.armature
    ms = [o for o in cont.objects if o.type == "MESH"]
    if not ms:
        warnings.append(f"weapon {w['id']}: no mesh in {w['model']}")
        continue
    if arm is not None:
        arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    B = Matrix.Identity(4)
    if arm is not None and w["bone"] in arm.data.bones:
        B = arm.matrix_world @ arm.data.bones[w["bone"]].matrix_local
    else:
        warnings.append(f"weapon {w['id']}: no bone {w['bone']} (mounted by its model origin)")
    datas = [rest_world_mesh(o) for o in ms]
    clear_names = [o.name for o in bpy.data.objects]
    for nm in clear_names:
        bpy.data.objects.remove(bpy.data.objects[nm], do_unlink=True)
    Binv = B.inverted()
    me = datas[0]
    for extra in datas[1:]:  # join by appending geometry (bmesh-free)
        import bmesh
        bm = bmesh.new()
        bm.from_mesh(me)
        bm.from_mesh(extra)
        bm.to_mesh(me)
        bm.free()
    me.transform(Binv)
    me.materials.clear()
    wo = bpy.data.objects.new(w["id"], me)
    scene.collection.objects.link(wo)
    me.calc_loop_triangles()
    dst = out / "weapons" / f"{w['id']}.glb"
    for o in bpy.data.objects:
        o.select_set(o is wo)
    bpy.ops.export_scene.gltf(filepath=str(dst), export_format="GLB", export_yup=True, use_selection=True,
                              export_materials="NONE", export_animations=False)
    weapons.append({"id": w["id"], "file": f"weapons/{dst.name}", "tris": len(me.loop_triangles),
                    "size_m": size_of([v.co for v in me.vertices]), "bone": w["bone"], "mount_joint": f"bm_{w['bone']}"})

# ---------------------------------------------------------------- the character
clear()
mdl, mbuf = sio.read_mdl(cm, cfg["model"])
cont, SCALE = load(cfg["model"], textures=True)
arm = cont.armature
if arm is None:
    raise SystemExit(f"{cfg['model']} has no skeleton: not a posable unit")
meshes = [o for o in cont.objects if o.type == "MESH"]
# body groups: single-choice parts always stay; multi-choice parts become mesh groups, choice 0 = the default
mesh_groups, default_groups = [], []
for bp_name, objs in (cont.bodygroups or {}).items():
    objs = [o for o in objs if o.type == "MESH"]
    if len(objs) > 1:
        for k, o in enumerate(objs):
            mesh_groups.append({"name": o.name, "role": "head" if re.search(r"head|face|hat|helmet", bp_name + o.name, re.I) else "body",
                                "body_group": bp_name, "choice": k})
        default_groups.append(objs[0].name)
for o in [o for o in bpy.data.objects if o.type not in ("ARMATURE", "MESH")]:
    bpy.data.objects.remove(o, do_unlink=True)  # SourceIO's attachment empties (constraint-driven): rebuilt below
for o in meshes:
    o.shape_key_clear()  # flexes (faces) are not posed
arm.data.pose_position = "REST"
bpy.context.view_layer.update()
dg0 = bpy.context.evaluated_depsgraph_get()
for o in meshes:  # the rest pose must deform to itself, or the skin and the bones disagree
    ev = o.evaluated_get(dg0)
    m = ev.to_mesh()
    d = max(((ev.matrix_world @ a.co) - (o.matrix_world @ b.co)).length for a, b in zip(m.vertices, o.data.vertices)) if len(m.vertices) == len(o.data.vertices) else -1
    ev.to_mesh_clear()
    if d > 1e-3 or d < 0:
        warnings.append(f"{o.name}: rest pose moves the skin by up to {d * 1000:.1f} mm (bind/rest mismatch)")
for o in [arm] + meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)  # metres, identity roots
bpy.context.view_layer.update()
arm.name = "Armature"
bones = arm.data.bones

# ---------------------------------------------------------------- eye bone (rig role `eye`) + attachments
att = {a.name: a for a in mdl.attachments}
eye_name = cfg.get("eye_attachment") or "eyes"
eye_bone = None
if eye_name in att:
    a = att[eye_name]
    pb_name = mdl.bones[a.parent_bone].name
    L = Matrix.Translation(Vector(a.pos) * SCALE) @ Euler(a.rot).to_matrix().to_4x4()
    W = bones[pb_name].matrix_local @ L
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones.new(f"attach_{eye_name}")
    eb.head = W.translation
    eb.tail = W.translation + Vector((0, 0, 0.01))
    eb.parent = arm.data.edit_bones[pb_name]
    eb.use_deform = False
    bpy.ops.object.mode_set(mode="OBJECT")
    eye_bone = f"attach_{eye_name}"
    bones = arm.data.bones
else:
    warnings.append(f"no '{eye_name}' attachment: the eye line will be estimated from the head")


def bone_empty(name, bone, W):
    e = bpy.data.objects.new(name, None)
    e.empty_display_size = 0.05
    scene.collection.objects.link(e)
    e.parent, e.parent_type, e.parent_bone = arm, "BONE", bone
    bpy.context.view_layer.update()
    e.matrix_world = W
    return e


sockets = []
for nm in cfg.get("attachments", []):
    if nm not in att:
        continue
    a = att[nm]
    bn = mdl.bones[a.parent_bone].name
    L = Matrix.Translation(Vector(a.pos) * SCALE) @ Euler(a.rot).to_matrix().to_4x4()
    bone_empty(nm, bn, bones[bn].matrix_local @ L)
    sockets.append({"name": nm, "bone": bn, "offset_m": [round(x, 5) for x in L.translation]})
for w in weapons:  # bonemerge mount points: the character's own bone frame at rest
    if w["bone"] in bones:
        bone_empty(w["mount_joint"], w["bone"], bones[w["bone"]].matrix_local.copy())
    else:
        warnings.append(f"weapon {w['id']}: the character has no bone {w['bone']} to bonemerge onto")

# ---------------------------------------------------------------- clips
want = {c["anim"]: c["name"] for c in cfg.get("clips", []) if c.get("anim")}
clips = []
if want:
    anims = [a for a in load_all_animations(mdl, mbuf, cm, TinyPath(cfg["model"])) if a.name in want]
    before = set(bpy.data.actions)
    arm.data.pose_position = "POSE"
    import_animations_to_armature(arm, anims, SCALE)
    for act in [a for a in bpy.data.actions if a not in before]:
        seq = want.get(act.name)
        if seq is None:
            continue
        act.name = seq
        if not act.slots:
            warnings.append(f"clip {seq}: action has no slot")
            continue
        nkeys = 0
        for lay in act.layers:  # SourceIO keys frames 1..n; the package counts frames from 0 like every adapter
            for strip in lay.strips:
                for bag in strip.channelbags:
                    for fc in bag.fcurves:
                        for kp in fc.keyframe_points:
                            kp.co.x -= 1
                            kp.handle_left.x -= 1
                            kp.handle_right.x -= 1
                        fc.update()
                        nkeys += 1
        if nkeys == 0:
            warnings.append(f"clip {seq}: no keys decoded")
            continue
        tr = arm.animation_data.nla_tracks.new()
        tr.name = seq
        fr = act.frame_range
        st = tr.strips.new(seq, int(fr[0]), act)
        st.action_slot = act.slots[0]
        clips.append({"name": seq, "frames": [int(fr[0]), int(fr[1])]})
    if arm.animation_data:
        arm.animation_data.action = None
    missing = sorted(set(want.values()) - {c["name"] for c in clips})
    if missing:
        warnings.append(f"{len(missing)} clips not loaded: {missing[:8]}")

# ---------------------------------------------------------------- textures (images the materials use)
textures = []
for o in meshes:
    for mat in o.data.materials:
        if not mat or not mat.node_tree:
            continue
        for n in mat.node_tree.nodes:
            if n.type != "TEX_IMAGE" or n.image is None or not n.image.has_data and n.image.size[0] == 0:
                continue
            img = n.image
            to = {l.to_node.type for s in n.outputs for l in s.links}
            labels = " ".join([n.label, n.name, img.name]).lower()
            role = "normal" if ("NORMAL_MAP" in to or re.search(r"bump|normal|_nrm|_n\b", labels)) else \
                "diffuse" if re.search(r"base|diffuse|albedo|color", labels) or "BSDF_PRINCIPLED" in to else None
            if role is None or any(t["material"] == mat.name and t["role"] == role for t in textures):
                continue
            stem = re.sub(r"[^A-Za-z0-9_.-]", "_", img.name.rsplit(".", 1)[0])
            dst = out / "textures" / f"{stem}.png"
            try:
                img.filepath_raw = str(dst)
                img.file_format = "PNG"
                img.save()
            except Exception as e:  # noqa: BLE001
                warnings.append(f"texture {img.name}: {e!r}")
                continue
            textures.append({"file": f"textures/{dst.name}", "role": role, "material": mat.name, "image": img.name})

# ---------------------------------------------------------------- stats + export
arm.data.pose_position = "REST"
bpy.context.view_layer.update()
for o in meshes:
    o.data.calc_loop_triangles()
co = [v.co for o in meshes for v in o.data.vertices]
default_meshes = [o for o in meshes if o.name not in {g["name"] for g in mesh_groups} or o.name in default_groups]
stats = {"meshes": len(meshes), "tris_all": sum(len(o.data.loop_triangles) for o in meshes),
         "tris_default": sum(len(o.data.loop_triangles) for o in default_meshes), "bones": len(bones),
         "clips": len(clips), "size_m": size_of(co), "height_m": size_of(co)[2],
         "materials": sorted({m.name for o in meshes for m in o.data.materials if m}), "attachments": len(mdl.attachments)}
arm.data.pose_position = "POSE"
for pb in arm.pose.bones:
    pb.custom_shape = None
glb = out / "model" / f"{cfg['stem']}.glb"
bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", export_animations=True,
                          export_animation_mode="NLA_TRACKS", export_skins=True, export_yup=True,
                          export_apply=False, export_materials="PLACEHOLDER", export_def_bones=False,
                          export_morph=False, export_force_sampling=True, export_frame_range=False)
info = {"stats": stats, "clips": clips, "sockets": sockets, "weapons": weapons, "model": f"model/{glb.name}",
        "bone_names": [b.name for b in bones], "mesh_names": [o.name for o in meshes], "mesh_groups": mesh_groups,
        "default_groups": default_groups, "eye_bone": eye_bone, "textures": textures, "warnings": warnings,
        "scale_m_per_unit": SCALE}
(out / "export.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
print("RESULT_JSON:" + json.dumps({"ok": True, "stats": stats, "clips": len(clips), "weapons": len(weapons),
                                   "textures": len(textures), "warnings": warnings[:5]}))
