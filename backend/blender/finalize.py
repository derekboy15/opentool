# SPDX-License-Identifier: GPL-3.0-or-later
"""Finalize (solid, stage 9 end): voxel remesh of the saved union input -> one watertight shell, debris cleanup,
decimate, flying-stand socket cut, STL export + re-import repair loop -> <out>/<output_name>.stl + build.json.

Runs in its own Blender process after build.py (memory: only the union input and the remesh are alive here).
Opened with <out>/union_input.blend; state (stats, socket, scale info, bounds) from <out>/build_part1.json.
"""
import json
import os
import sys
import time

import bmesh
import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import face_shells, load_cfg, log, mem_gb, mesh_object, out_path, shell_volume, stage  # noqa: E402

T0 = time.time()
cfg = load_cfg()
G = cfg["geometry"]
cf = cfg["connect_floating"]
bc = cfg.get("base")
with open(out_path(cfg, "build_part1.json")) as f:
    P1 = json.load(f)
stats, info, socket = P1["stats"], P1["info"], P1["socket"]
lo, hi = Vector(P1["lo"]), Vector(P1["hi"])
T0 -= stats.get("seconds_part1", 0)


def fix(kind, **detail):
    stats["fixes"].append({"fix": kind, **detail})
    log(f"fix {kind}: " + ", ".join(f"{k}={v}" for k, v in detail.items()))


def drop_object(ob):
    """Remove an object and its mesh datablock (Blender keeps orphaned meshes in memory until the end)."""
    me = ob.data if ob.type == "MESH" else None
    bpy.data.objects.remove(ob)
    if me is not None and me.users == 0:
        bpy.data.meshes.remove(me)

def voxel_remesh(bm, label):
    me = bpy.data.meshes.new(label)
    bm.to_mesh(me)
    ob = mesh_object(label, me)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    me.remesh_voxel_size = G["voxel_mm"]
    me.remesh_voxel_adaptivity = 0.0
    bpy.ops.object.voxel_remesh()
    out = bmesh.new()
    out.from_mesh(ob.data)
    bmesh.ops.triangulate(out, faces=out.faces)
    drop_object(ob)
    log(f"{label}: voxel remesh {G['voxel_mm']} mm -> {len(out.faces):,} tris ({time.time() - T0:.0f}s, mem {mem_gb()})")
    return out

def cleanup(bm, min_volume=0.01, report=None):
    """Weld, dissolve degenerates, drop zero-volume sliver shells (and loose bits under min_volume; their
    volume/centre go to report), close pinholes."""
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-4)
    bmesh.ops.dissolve_degenerate(bm, edges=bm.edges, dist=1e-4)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    kill, dropped = [], 0
    for sh in face_shells(bm):
        vol = abs(shell_volume(sh))
        if vol < min_volume:
            kill += sh
            dropped += 1
            if report is not None and vol >= 0.05:
                vs = {v for f in sh for v in f.verts}
                c = sum((v.co for v in vs), Vector()) / len(vs)
                report.append({"volume_mm3": round(vol, 2), "centre_mm": [round(x, 2) for x in c]})
    bmesh.ops.delete(bm, geom=kill, context="FACES")
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    bmesh.ops.holes_fill(bm, edges=[e for e in bm.edges if e.is_boundary], sides=64)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    return dropped


ob_u = bpy.data.objects["union_input"]
pre = bmesh.new()
pre.from_mesh(ob_u.data)
drop_object(ob_u)
log(f"union input: {len(pre.faces):,} faces (mem {mem_gb()})")
final = voxel_remesh(pre, "final")
pre.free()  # everything below works on the remeshed result
stats["tris_remeshed"] = len(final.faces)
debris = max(cf["min_shell_volume_mm3"], cf.get("drop_loose_under_mm3", 0.0)) if cf["enabled"] else 0.01
loose = []
dropped = cleanup(final, debris, loose)
if loose:
    fix("dropped_loose_parts", below_mm3=debris, parts=sorted(loose, key=lambda d: -d["volume_mm3"])[:20])

me_hi = None  # full-resolution remesh, kept (as a light mesh datablock) for a re-decimation if export stays dirty
if len(final.faces) > G["max_tris"]:
    me_hi = bpy.data.meshes.new("remeshed_full")
    final.to_mesh(me_hi)


def decimate_to(target):
    """Collapse-decimate the full-resolution remesh to ~target triangles."""
    ob = mesh_object("dec", me_hi.copy())
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    dec = ob.modifiers.new("Decimate", "DECIMATE")
    dec.decimate_type, dec.ratio = "COLLAPSE", target / len(me_hi.polygons)
    bpy.ops.object.modifier_apply(modifier=dec.name)
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    drop_object(ob)
    return bm, cleanup(bm, debris)


def cut_socket(bm):
    """Flying-stand socket: cut after remesh/decimate (the voxel remesh can only add material)."""
    me = bpy.data.meshes.new("sock_model")
    bm.to_mesh(me)
    bm.free()
    ob = mesh_object("sock_model", me)
    cut = bmesh.new()
    r = bc["socket_diameter_mm"] / 2 + bc.get("clearance_mm", 0.15)
    d = bc["socket_depth_mm"]
    bmesh.ops.create_cone(cut, cap_ends=True, segments=48, radius1=r, radius2=r, depth=d + 1.0,
                          matrix=Matrix.Translation((socket["centre"][0], socket["centre"][1], socket["z_under"] + (d + 1.0) / 2 - 1.0)))
    me_c = bpy.data.meshes.new("sock_cut")
    cut.to_mesh(me_c)
    cut.free()
    ob_c = mesh_object("sock_cut", me_c)
    bo = ob.modifiers.new("Socket", "BOOLEAN")
    bo.operation, bo.solver, bo.object = "DIFFERENCE", "EXACT", ob_c
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.modifier_apply(modifier=bo.name)
    out = bmesh.new()
    out.from_mesh(ob.data)
    bmesh.ops.triangulate(out, faces=out.faces)
    drop_object(ob)
    drop_object(ob_c)
    return out, round(2 * r, 2), d


def finish(bm):
    """Socket, then sit the model on z = 0 (returns the mesh and the z it was moved by)."""
    if socket:
        bm, dia, depth = cut_socket(bm)
        fix("flying_socket", diameter_mm=dia, depth_mm=depth)
    zmin = min(v.co.z for v in bm.verts)
    bmesh.ops.translate(bm, verts=bm.verts, vec=(0, 0, -zmin))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm, zmin


if me_hi is not None:
    final.free()
    final, n = decimate_to(G["max_tris"])
    dropped += n
stats["dropped_debris_shells"] = dropped
final, zmin = finish(final)
stage("finalize_remesh", tris=len(final.faces))
stats["stl_offset_mm"] = [info["offset_mm"][0], info["offset_mm"][1], info["offset_mm"][2] - zmin]  # rig -> STL
stl = out_path(cfg, cfg["output_name"] + ".stl")


def export_stl(bm):
    me = bpy.data.meshes.new(cfg["output_name"])
    bm.to_mesh(me)
    ob = mesh_object(cfg["output_name"], me)
    for o in bpy.context.scene.objects:
        o.select_set(o is ob)
    bpy.ops.wm.stl_export(filepath=stl, export_selected_objects=True, ascii_format=False, apply_modifiers=False)
    dims = list(ob.dimensions)
    drop_object(ob)
    return dims


def reimport():
    """What a slicer sees: STL re-imported (coincident vertices merged by the importer)."""
    before = set(bpy.data.objects)
    bpy.ops.wm.stl_import(filepath=stl)
    ob = next(o for o in bpy.data.objects if o not in before)
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    drop_object(ob)
    return bm


def repair(bm, rings=2):
    """Cut out the faces around non-manifold edges (grown by `rings`) and refill the holes."""
    verts = {v for e in bm.edges if not e.is_manifold for v in e.verts}
    for _ in range(rings):
        verts |= {o for v in verts for e in v.link_edges for o in e.verts}
    bmesh.ops.delete(bm, geom=list({f for v in verts for f in v.link_faces}), context="FACES")
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    bmesh.ops.holes_fill(bm, edges=[e for e in bm.edges if e.is_boundary], sides=0)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)


def export_clean(bm, label=""):
    """Export, then verify on the re-imported STL; repair leftover non-manifold edges (decimation and float32
    vertex merging leave a few) and re-export until clean. A repair that makes things worse (a big hole refilled
    badly) is thrown away: the best version found is what stays exported. Returns (mesh, dims, nm)."""
    dims = export_stl(bm)
    best = None  # (nm, mesh, dims)
    for attempt in range(4):
        check = reimport()
        nm = sum(1 for e in check.edges if not e.is_manifold)
        if best is None or nm < best[0]:
            if best is not None and best[1] is not bm:
                best[1].free()
            best = (nm, check.copy(), dims)
        if nm == 0 or nm > 200:
            check.free()
            break
        repair(check, rings=2 + attempt)
        after = sum(1 for e in check.edges if not e.is_manifold)
        fix("repair_non_manifold", attempt=attempt + 1, before=nm, after_in_memory=after, **({"pass": label} if label else {}))
        dims = export_stl(check)
        check.free()
    else:
        check = reimport()
        nm = sum(1 for e in check.edges if not e.is_manifold)
        if nm < best[0]:
            best[1].free()
            best = (nm, check, dims)
        else:
            check.free()
    bm.free()
    nm, mesh, dims = best
    dims = export_stl(mesh)  # the best version is the one on disk
    return mesh, dims, nm


final, dims, nm_best = export_clean(final)
if nm_best and me_hi is not None:
    # decimation defects depend on where the collapse lands: a slightly different triangle budget usually
    # comes out clean
    for k, f in enumerate(G.get("redecimate_factors", [0.97, 1.03])):
        cand, _ = decimate_to(G["max_tris"] * f)
        cand, z2 = finish(cand)
        keep = (final, dims, zmin)
        cand, dims2, nm2 = export_clean(cand, label=f"redecimate {f}")
        fix("redecimate", factor=f, non_manifold_before=nm_best, non_manifold_after=nm2)
        if nm2 < nm_best:
            keep[0].free()
            final, dims, zmin, nm_best = cand, dims2, z2, nm2
            stats["stl_offset_mm"] = [info["offset_mm"][0], info["offset_mm"][1], info["offset_mm"][2] - zmin]
        else:
            cand.free()
            final, dims, zmin = keep
            dims = export_stl(final)
        if nm_best == 0:
            break
if me_hi is not None:
    bpy.data.meshes.remove(me_hi)
final.free()

check = reimport()
nm = sum(1 for e in check.edges if not e.is_manifold)
shells = len(face_shells(check))
tris = len(check.faces)
check.free()
growth = max(d - (h - l) for d, h, l in zip(dims, hi, lo))
if not bc and growth > 2 * G["displace_mm"] + 0.5:
    log(f"WARNING bounding box grew {growth:.2f} mm over the subdivided model: spikes?")
stage("export", non_manifold=nm, shells=shells)
stats.update({"stl": os.path.basename(stl), "tris_final": tris, "non_manifold_edges_reimported": nm,
              "shells": shells, "dims_mm": [round(d, 2) for d in dims],
              "stl_mb": round(os.path.getsize(stl) / 2**20, 1), "seconds": round(time.time() - T0)})
with open(out_path(cfg, "build.json"), "w") as f:
    json.dump(stats, f, indent=2)
log("BUILD " + json.dumps({k: v for k, v in stats.items() if k not in ("fixes", "scale")}))
