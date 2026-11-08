# SPDX-License-Identifier: GPL-3.0-or-later
"""Relic Essence 1 export (runs in Blender 5.2 through the capped runner): one .whm -> glTF package model.

cfg: addon_parent (folder that holds the blender_dow add-on folder), roots [[folder, tag]] (unpacked archives in
load order), whm (game path), default (mesh names of the default appearance), out_dir, fps.
Imports with amorgun's blender_dow (enabled for this run only, never installed; PIL stubbed: only its export
path needs it), then: bind-vs-idle rule (idle frame 0 only when a default mesh floats clear of the bind pose and
idle closes every gap without growing), extraction stats (meshes, tris all / default, bones, height), visibility
animations removed (the default set is the package's mesh groups now), diffuse textures written as PNG, and the
rig + every mesh + every motion clip exported to <out>/model/<stem>.glb. Writes <out>/export.json.
"""
import json
import os
import pathlib
import re
import sys
import types

import bpy
import numpy as np

assert bpy.app.version[:2] == (5, 2), f"Blender 5.2 needed, this is {bpy.app.version_string}"
cfg = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
_pil = types.ModuleType("PIL")
_pil.Image = types.SimpleNamespace(Image=object)
_pil.ImageChops = _pil.ImageOps = None
sys.modules.setdefault("PIL", _pil)
sys.path.insert(0, cfg["addon_parent"])
import addon_utils  # noqa: E402

if not addon_utils.enable("blender_dow", default_set=True, handle_error=None):
    raise RuntimeError("blender_dow failed to enable (is [readers] blender_dow the add-on folder?)")
from blender_dow import dow_layout, importer  # noqa: E402

# ---- guards for broken mod files (patched here at run time; the add-on stays as downloaded)
_find = dow_layout.DowLayout.find


def _safe_find(self, path, default=None):
    """Absolute paths baked into some mod files must never reach the real disk."""
    if pathlib.PureWindowsPath(str(path)).drive or str(path).startswith(("/", "\\")):
        return default
    try:
        return _find(self, path, default)
    except OSError:
        return default


dow_layout.DowLayout.find = _safe_find


def _iter_chunks(self):
    while (h := self.read_header()):
        end = self.stream.tell() + h.size
        yield h
        pos = self.stream.tell()
        if pos < end or (pos > end and h.typeid.startswith("DATA")):
            self.stream.seek(end)


importer.ChunkReader.iter_chunks = _iter_chunks


class _Meshes(dict):
    def __missing__(self, key):
        ob = bpy.data.objects.new(f"__missing__{key}", None)
        ob["dow_missing_anim_target"] = True
        self[key] = ob
        return ob


_reset = importer.WhmLoader._reset


def _patched_reset(self):
    _reset(self)
    self.created_meshes = _Meshes()


importer.WhmLoader._reset = _patched_reset
GAP_FRAC, TALLER_FRAC = 0.05, 1.2

# ---------------------------------------------------------------- import
for ob in list(bpy.data.objects):  # (no read_factory_settings here: it would switch the add-on off again)
    bpy.data.objects.remove(ob, do_unlink=True)
scene = bpy.context.scene
roots = [pathlib.Path(r) for r, _ in cfg["roots"]]
loader = importer.WhmLoader(roots[0], load_wtp=True, stric_mode=False)
loader.layout = dow_layout.DowLayout(sources=[dow_layout.DirectorySource(r, t) for r, t in cfg["roots"]])
src = loader.layout.find(cfg["whm"])
if src is None:
    raise SystemExit(f"{cfg['whm']} not found in the unpacked archives")
with open(src.full_path, "rb") as f:
    try:
        loader.load(importer.ChunkReader(f))
    finally:
        msgs = [f"{lvl}: {m}" for lvl, m in loader.messages]
for lc in scene.view_layers[0].layer_collection.children:
    lc.exclude = False
    lc.hide_viewport = False
for ob in [o for o in bpy.data.objects if o.get("dow_missing_anim_target")]:
    bpy.data.objects.remove(ob, do_unlink=True)
shadows = {ob.dow_shadow_mesh for ob in bpy.data.objects if ob.type == "MESH" and getattr(ob, "dow_shadow_mesh", None) is not None}
for ob in list(shadows):
    bpy.data.objects.remove(ob, do_unlink=True)
meshes = [ob for ob in bpy.data.objects if ob.type == "MESH" and not ob.name.lower().startswith("shadow")]
for ob in [o for o in bpy.data.objects if o.type == "MESH" and o not in meshes]:
    bpy.data.objects.remove(ob, do_unlink=True)
for ob in meshes:
    ob.hide_set(False)
    ob.hide_viewport = ob.hide_render = False
arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
want = {n.lower() for n in cfg["default"]}
default_objs = [ob for ob in meshes if re.sub(r"\.\d{3}$", "", ob.name).lower() in want]


def boxes(objs):
    dg = bpy.context.evaluated_depsgraph_get()
    out = {}
    for ob in objs:
        ev = ob.evaluated_get(dg)
        me = ev.to_mesh()
        co = np.array([ev.matrix_world @ v.co for v in me.vertices]) if len(me.vertices) else np.zeros((0, 3))
        ev.to_mesh_clear()
        if len(co):
            out[ob.name] = (co.min(0), co.max(0))
    return out


def pose_metrics(bx):
    if not bx:
        return {"height": 0.0, "max_gap": 0.0, "floating": []}
    lo = np.min([b[0] for b in bx.values()], 0)
    hi = np.max([b[1] for b in bx.values()], 0)
    h = float(hi[2] - lo[2])
    gaps = {}
    for a in bx:
        best = None
        for b in bx:
            if a != b:
                d = np.maximum(0, np.maximum(bx[a][0] - bx[b][1], bx[b][0] - bx[a][1]))
                g = float(np.linalg.norm(d))
                best = g if best is None else min(best, g)
        gaps[a] = best or 0.0
    return {"height": round(h, 4), "max_gap": round(max(gaps.values()) if len(gaps) > 1 else 0.0, 4),
            "floating": sorted(n for n, g in gaps.items() if h and g > GAP_FRAC * h)}


def set_rest():
    if arm is None:
        return
    if arm.animation_data:
        arm.animation_data.action = None
    for pb in arm.pose.bones:
        pb.matrix_basis.identity()
    scene.frame_set(0)


set_rest()
rest = pose_metrics(boxes(default_objs))
pose = {"used": "bind", "bind": rest}
idle = [a for a in bpy.data.actions if a.name.lower().startswith("idle")]
idle.sort(key=lambda a: (a.name.lower() != "idle", len(a.name), a.name))
if arm is not None and idle:
    act = idle[0]
    slot = next((s for s in act.slots if s.name_display == arm.name), None) or next((s for s in act.slots if s.target_id_type == "OBJECT"), None)
    if slot is not None:
        arm.animation_data_create()
        arm.animation_data.action, arm.animation_data.action_slot = act, slot
        scene.frame_set(int(act.frame_range[0]))
        im = pose_metrics(boxes(default_objs))
        pose.update(idle_action=act.name, idle_frame0=im)
        if rest["floating"] and not im["floating"] and im["height"] <= TALLER_FRAC * rest["height"]:
            pose.update(used="idle_frame0", why=f"bind pose has floating meshes {rest['floating'][:6]}; idle frame 0 has none")
        set_rest()
if pose["used"] != "bind":
    print("WARNING bind pose floats: the back end uses the clips' own frames, so this only affects rest-pose scaling")

# ---------------------------------------------------------------- stats (as Stage 1 counted them)
for ob in meshes:
    ob.data.calc_loop_triangles()
tri = {ob.name: len(ob.data.loop_triangles) for ob in meshes}
bx = boxes(default_objs or meshes)
lo = np.min([b[0] for b in bx.values()], 0)
hi = np.max([b[1] for b in bx.values()], 0)
stats = {"meshes": len(meshes), "tris_all": sum(tri.values()), "tris_default": sum(tri[o.name] for o in default_objs),
         "default_meshes": len(default_objs), "bones": len(arm.data.bones) if arm else 0,
         "actions": len(bpy.data.actions), "height_units": round(float(hi[2] - lo[2]), 4),
         "size_units": [round(float(x), 4) for x in hi - lo]}
print("[export] STATS " + json.dumps(stats))

# ---------------------------------------------------------------- clean for export
def bone_slot(act):
    """The action's slot that moves bones (blender_dow names it after the skeleton, not after the object)."""
    from bpy_extras import anim_utils
    for sl in act.slots:
        bag = anim_utils.action_get_channelbag_for_slot(act, sl)
        if bag and any(fc.data_path.startswith("pose.bones") for fc in bag.fcurves):
            return sl
    return None


clips = []
for a in list(bpy.data.actions):
    if a.name.lower().startswith("vis"):
        bpy.data.actions.remove(a)
        continue
    sl = bone_slot(a)
    if sl is None or arm is None:
        bpy.data.actions.remove(a)  # no bone motion: a visibility-only clip
        continue
    # one NLA track per clip: the glTF exporter writes each track as one animation, with the bone slot only
    # (mesh visibility / colour keys stay behind: the loadout decides what shows, not the clip)
    arm.animation_data_create()
    tr = arm.animation_data.nla_tracks.new()
    tr.name = a.name
    st = tr.strips.new(a.name, int(a.frame_range[0]), a)
    st.action_slot = sl
    tr.mute = False
    clips.append({"name": a.name, "frames": [int(a.frame_range[0]), int(a.frame_range[1])]})
for ob in meshes:
    if ob.animation_data:
        ob.animation_data_clear()
    for k in [k for k in ob.keys() if "invisible" in k.lower()]:
        del ob[k]
if arm and arm.animation_data:
    arm.animation_data.action = None
if arm:
    for pb in arm.pose.bones:  # bone display shapes (an icosphere object) are not part of the model
        pb.custom_shape = None
keep = set(meshes) | ({arm} if arm else set())
for ob in [o for o in bpy.data.objects if o not in keep]:
    bpy.data.objects.remove(ob, do_unlink=True)
out = pathlib.Path(cfg["out_dir"])
(out / "model").mkdir(parents=True, exist_ok=True)
(out / "textures").mkdir(parents=True, exist_ok=True)
textures = []
seen = set()
for ob in meshes:
    for m in ob.data.materials:
        if not m or m.name in seen or not m.node_tree:
            continue
        seen.add(m.name)
        imgs = [n.image for n in m.node_tree.nodes if n.bl_idname == "ShaderNodeTexImage" and n.image and n.image.size[0] > 1]
        img = next((n.image for n in m.node_tree.nodes if n.bl_idname == "ShaderNodeTexImage" and n.image
                    and n.image.size[0] > 1 and "diffuse" in (n.label or "").lower()), imgs[0] if imgs else None)
        if img is None:
            continue
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", pathlib.PurePosixPath(m.name).name)[:60]
        dst = out / "textures" / f"{stem}_D.png"
        img.file_format = "PNG"
        img.save(filepath=str(dst))
        textures.append({"file": f"textures/{dst.name}", "role": "diffuse", "material": m.name,
                         "note": "team colour / spec mask in the alpha channel"})
stem = pathlib.PurePosixPath(cfg["whm"]).stem
glb = out / "model" / f"{stem}.glb"
bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", export_animations=True,
                          export_animation_mode="NLA_TRACKS", export_skins=True, export_yup=True,
                          export_apply=False, export_materials="NONE", export_def_bones=False,
                          export_force_sampling=True, export_frame_range=False)
info = {"stats": stats, "pose": pose, "clips": clips, "textures": textures, "model": f"model/{glb.name}",
        "meshes": [{"name": ob.name, "tris": tri[ob.name], "default": ob in default_objs,
                    "parent_bone": ob.parent_bone if ob.parent_type == "BONE" else None,
                    "vertex_groups": len(ob.vertex_groups)} for ob in meshes],
        "bone_names": [b.name for b in arm.data.bones] if arm else [], "armature": arm.name if arm else None,
        "messages": msgs[:40], "source_file_tag": getattr(src, "source", None) and str(src.source)}
(out / "export.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
print("RESULT_JSON:" + json.dumps({"ok": True, "stats": stats, "pose": pose["used"], "clips": len(clips)}))
