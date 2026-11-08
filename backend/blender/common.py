# SPDX-License-Identifier: GPL-3.0-or-later
"""Helpers shared by the Blender stages (import, bake to mm, mesh utilities)."""
import fnmatch
import json
import os
import sys

import bmesh
import bpy
from mathutils import Matrix, Vector


def load_cfg():
    with open(sys.argv[sys.argv.index("--") + 1]) as f:
        return json.load(f)


def log(*a):
    print("[pipe]", *a, flush=True)


def mem_gb():
    """Current and peak working set of this Blender process (Windows), GB."""
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t)] +                        [(f"f{i}", ctypes.c_size_t) for i in range(6)]
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return round(c.WorkingSetSize / 2**30, 1), round(c.PeakWorkingSetSize / 2**30, 1)
    except Exception:
        return 0.0, 0.0


def stage(name, state="done", **info):
    """Sub-stage marker for the job runner (parsed from the log)."""
    now, peak = mem_gb()
    print("[stage] " + json.dumps({"stage": name, "state": state, "mem_gb": now, "peak_gb": peak, **info}), flush=True)


def slot_key(cfg, mat):
    """Texture-set key of one material: the normal map its node tree links (the FBX keeps the game's
    material -> texture binding: BLA_INV_MAT_01 -> BLA_INV_1_N.png), else the name rules below."""
    if mat is None:
        return None
    keys = cfg["materials"]
    if mat.use_nodes and mat.node_tree:
        for nd in mat.node_tree.nodes:
            img = getattr(nd, "image", None)
            if img is None:
                continue
            stem = os.path.splitext(os.path.basename(img.filepath or img.name))[0]
            if stem.endswith("_N") and stem[:-2] in keys:
                return stem[:-2]
    n = mat.name
    for key in keys:
        if n == key or n.split(".")[0] == key:
            return key
    hits = [k for k in keys if n.startswith(k + "_") or n.startswith(k + "+")]
    return max(hits, key=len) if hits else None


def material_key(cfg, ob):
    """Texture-set key (cfg['materials']) of a single-material object; see slot_key.
    Weapons without a material are tagged with their texture set by the attach stage ("mat_key")."""
    if ob.get("mat_key"):
        return ob["mat_key"]
    for m in ob.data.materials:
        k = slot_key(cfg, m)
        if k:
            return k
    return cfg.get("default_material")


def split_by_material(cfg, ob, me):
    """[(name, mesh, key)]: one part per texture set when a mesh carries several (Combined Meshes with
    BLA_INV_MAT_01..03). Material seams are UV-island borders, where the height maps fade to zero."""
    keys = [slot_key(cfg, m) for m in ob.data.materials] if not ob.get("mat_key") else []
    if len({k for k in keys if k}) < 2:
        return [(ob.name, me, material_key(cfg, ob))]
    parts = []
    for key in sorted({k for k in keys if k}) + ([None] if None in keys else []):
        idx = {i for i, k in enumerate(keys) if k == key}
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.material_index not in idx], context="FACES")
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
        if bm.faces:
            pm = bpy.data.meshes.new(f"{ob.name}~{key}")
            bm.to_mesh(pm)
            parts.append((f"{ob.name}~{key or 'none'}", pm, key or cfg.get("default_material")))
        bm.free()
    log(f"{ob.name}: split into {len(parts)} parts by texture set ({', '.join(str(k) for k in sorted(set(keys), key=str))})")
    return parts


def import_baked(cfg):
    """Import the FBX and return plain meshes in mm (armature/parenting applied, bind pose).

    target_height: helmet top (highest vertex near the head bone) to feet = target_mm, feet on z=0.
    factor_from:   reuse another model's raw->mm factor (weapons keep their owner's scale).
    """
    if cfg["fbx"].lower().endswith(".blend"):  # posed model from the pose stage: bake its evaluated pose
        bpy.ops.wm.open_mainfile(filepath=cfg["fbx"])  # (IK target empties must stay until baked)
    elif cfg["fbx"].lower().endswith((".glb", ".gltf")):
        from anim import import_model
        import_model(cfg["fbx"])
    else:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.fbx(filepath=cfg["fbx"])
    dg = bpy.context.evaluated_depsgraph_get()
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)

    baked = []
    for ob in [o for o in bpy.data.objects if o.type == "MESH"]:
        ev = ob.evaluated_get(dg)
        me = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True, depsgraph=dg)
        me.transform(ev.matrix_world)
        baked += split_by_material(cfg, ob, me)
    head_xy = None
    sc = cfg["scale"]
    if arm and sc["mode"] == "target_height":
        bone = next((b for b in arm.data.bones if sc["top_bone"] in b.name.lower()), None)
        if bone:
            head_xy = (arm.matrix_world @ bone.head_local).xy
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)

    objs = []
    for name, me, mat in baked:
        ob = bpy.data.objects.new(name, me)
        ob["mat"] = mat or ""
        bpy.context.scene.collection.objects.link(ob)
        objs.append(ob)

    allv = [v.co for o in objs for v in o.data.vertices]
    zmin = min(c.z for c in allv)
    cx = (min(c.x for c in allv) + max(c.x for c in allv)) / 2
    cy = (min(c.y for c in allv) + max(c.y for c in allv)) / 2
    if sc["mode"] in ("factor_from", "factor"):
        if sc["mode"] == "factor":
            factor = sc["factor"]
        else:
            with open(sc["factor_from"]) as f:
                factor = json.load(f)["scale_factor"]
        top_raw = max(c.z for c in allv)
    else:
        body = max(objs, key=lambda o: len(o.data.vertices))
        if head_xy is not None:
            r = sc["head_radius_frac"] * (max(v.co.x for v in body.data.vertices) - min(v.co.x for v in body.data.vertices))
            top_raw = max(v.co.z for v in body.data.vertices if (v.co.xy - head_xy).length < r)
        else:
            top_raw = max(c.z for c in allv)
        factor = sc["target_mm"] / (top_raw - zmin)
    xf = Matrix.Scale(factor, 4) @ Matrix.Translation(Vector((-cx, -cy, -zmin)))
    for o in objs:
        o.data.transform(xf)
        weld(o.data, cfg["geometry"]["weld_mm"])
    info = {"scale_factor": factor, "reference_top_mm": (top_raw - zmin) * factor,
            "offset_mm": [-cx * factor, -cy * factor, -zmin * factor],  # model mm = rig mm + offset
            "overall_top_mm": max(v.co.z for o in objs for v in o.data.vertices)}
    log(f"scale x{factor:.3f}; reference top {info['reference_top_mm']:.2f} mm; overall top {info['overall_top_mm']:.2f} mm")
    return objs, info


def weld(me, dist):
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=dist)
    bm.to_mesh(me)
    bm.free()


def boundary_loops(bm):
    """Group boundary edges into loops: [(edges, perimeter, centre)]."""
    seen, loops = set(), []
    for e in bm.edges:
        if not e.is_boundary or e.index in seen:
            continue
        stack, edges = [e], []
        seen.add(e.index)
        while stack:
            cur = stack.pop()
            edges.append(cur)
            for v in cur.verts:
                for f in v.link_edges:
                    if f.is_boundary and f.index not in seen:
                        seen.add(f.index)
                        stack.append(f)
        pts = [v.co for ed in edges for v in ed.verts]
        loops.append((edges, sum(ed.calc_length() for ed in edges), sum(pts, Vector()) / len(pts)))
    return loops


def face_shells(bm):
    """Connected face groups: [[BMFace]]."""
    bm.faces.ensure_lookup_table()
    seen, shells = set(), []
    for f in bm.faces:
        if f.index in seen:
            continue
        stack, shell = [f], []
        seen.add(f.index)
        while stack:
            cur = stack.pop()
            shell.append(cur)
            for e in cur.edges:
                for g in e.link_faces:
                    if g.index not in seen:
                        seen.add(g.index)
                        stack.append(g)
        shells.append(shell)
    return shells


def shell_volume(shell):
    return sum(f.verts[0].co.dot(f.verts[1].co.cross(f.verts[2].co)) for f in shell if len(f.verts) == 3) / 6


def name_matches(name, pattern):
    return fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(name.split(".")[0], pattern)


def mesh_object(name, me):
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def out_path(cfg, *parts):
    return os.path.join(cfg["out_dir"], *parts)
