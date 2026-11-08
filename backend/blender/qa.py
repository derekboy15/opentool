"""Check / render stages: re-import the exported STL and inspect it as a slicer would see it.

cfg["qa_mode"]: "check" writes qa.json only; "render" also renders (batch set: grey front/back/3-4,
thin-wall heat 3/4, overhang from below; "full" adds every close-up). Close-ups come from qa.heatmap_closeups
and, with qa.auto_closeups, from the pose's key points (hands, knees, weapon grip/muzzle) in pose.json.

Reports shells, non-manifold/boundary edges and local wall thickness (inward ray from every vertex
that hits a surface facing back at it). Thin vertices are clustered along mesh edges; clusters
smaller than qa.detail_max_extent_mm across count as surface detail, larger ones as thin parts.
Renders a red thin-wall heatmap and grey matte previews (front, back, 3/4).
"""
import json
import math
import os
import sys
from collections import defaultdict

import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import face_shells, load_cfg, log, out_path, stage  # noqa: E402

cfg = load_cfg()
Q = cfg["qa"]
limit = Q["min_thickness_mm"]
stl = out_path(cfg, cfg["output_name"] + ".stl")

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.stl_import(filepath=stl)
ob = bpy.context.selected_objects[0]
me = ob.data
bm = bmesh.new()
bm.from_mesh(me)
bm.normal_update()
bm.verts.ensure_lookup_table()
shells = face_shells(bm)
nm = sum(1 for e in bm.edges if not e.is_manifold)
boundary = sum(1 for e in bm.edges if e.is_boundary)

bvh = BVHTree.FromBMesh(bm)
fn = [f.normal.copy() for f in bm.faces]
thick = {}
for v in bm.verts:
    hit, _, fi, d = bvh.ray_cast(v.co - v.normal * 1e-4, -v.normal, limit)
    if hit is not None and fn[fi].dot(v.normal) < -0.5:
        thick[v.index] = d

# cluster thin vertices: connected through mesh edges between thin vertices
seen, clusters = set(), []
for i in thick:
    if i in seen:
        continue
    stack, mem = [bm.verts[i]], []
    seen.add(i)
    while stack:
        cur = stack.pop()
        mem.append(cur)
        for e in cur.link_edges:
            o = e.other_vert(cur)
            if o.index in thick and o.index not in seen:
                seen.add(o.index)
                stack.append(o)
    c = sum((v.co for v in mem), Vector()) / len(mem)
    ext = max((v.co - c).length for v in mem)
    # protrusion: how far the thin area reaches beyond the thick material it hangs off. A displacement fringe
    # on a plate rim is a lip of a few tenths of a mm (the nozzle merges it); a spike tip or antenna reaches out
    ring = {o for v in mem for e in v.link_edges for o in (e.other_vert(v),) if o.index not in thick}
    if ring:
        kd = KDTree(len(ring))
        for k, o in enumerate(ring):
            kd.insert(o.co, k)
        kd.balance()
        protrusion = max(kd.find(v.co)[2] for v in mem)
    else:
        protrusion = 2 * ext  # thin all over (a loose sliver)
    nrm = sum((v.normal for v in mem), Vector())
    nrm = nrm.normalized() if nrm.length > 1e-9 else Vector((0, -1, 0))
    clusters.append({"verts": len(mem), "normal": [round(x, 3) for x in nrm], "min_mm": round(min(thick[v.index] for v in mem), 3),
                     "median_mm": round(sorted(thick[v.index] for v in mem)[len(mem) // 2], 3),
                     "centre_mm": [round(x, 2) for x in c], "extent_mm": round(ext, 2), "protrusion_mm": round(protrusion, 2)})
clusters.sort(key=lambda c: -c["verts"])
parts = [c for c in clusters if 2 * c["extent_mm"] >= Q["detail_max_extent_mm"]]
detail = [c for c in clusters if 2 * c["extent_mm"] < Q["detail_max_extent_mm"]]

# Overhangs: downward faces steeper than qa.overhang_deg from vertical (FDM needs support there),
# ignoring the bottom of the model/base. Connected faces form regions; report the sizeable ones.
cos_lim = math.cos(math.radians(90 - Q["overhang_deg"]))
zmin_all = min(v.co.z for v in bm.verts)
bm.faces.ensure_lookup_table()
over = set()
for f in bm.faces:
    if f.normal.z < -cos_lim and f.calc_center_median().z > zmin_all + 0.3:
        c = f.calc_center_median()
        hit = bvh.ray_cast(c + Vector((0, 0, -1e-4)), Vector((0, 0, -1)), Q["overhang_supported_gap_mm"])
        if hit[0] is None:  # surface right below (e.g. a sole hovering over the base) needs no support
            over.add(f.index)
seen_f, regions = set(), []
for i in over:
    if i in seen_f:
        continue
    stack, mem = [bm.faces[i]], []
    seen_f.add(i)
    while stack:
        cur = stack.pop()
        mem.append(cur)
        for e in cur.edges:
            for g in e.link_faces:
                if g.index in over and g.index not in seen_f:
                    seen_f.add(g.index)
                    stack.append(g)
    area = sum(f.calc_area() for f in mem)
    if area >= Q["overhang_min_area_mm2"]:
        c = sum((f.calc_center_median() for f in mem), Vector()) / len(mem)
        regions.append({"area_mm2": round(area, 2), "centre_mm": [round(x, 2) for x in c],
                        "lowest_z_mm": round(min(v.co.z for f in mem for v in f.verts), 2)})
regions.sort(key=lambda r: -r["area_mm2"])
off = None
if os.path.exists(out_path(cfg, "build.json")):
    with open(out_path(cfg, "build.json")) as f:
        off = json.load(f).get("stl_offset_mm")
if off:
    for r in regions:  # also give the position in rig mm (pose config frame)
        r["centre_rig_mm"] = [round(a - b, 2) for a, b in zip(r["centre_mm"], off)]

def shell_info(sh):
    vs = {v for f in sh for v in f.verts}
    lo_ = Vector([min(v.co[i] for v in vs) for i in range(3)])
    hi_ = Vector([max(v.co[i] for v in vs) for i in range(3)])
    vol = abs(sum(f.verts[0].co.dot(f.verts[1].co.cross(f.verts[2].co)) for f in sh) / 6)
    return {"volume_mm3": round(vol, 2), "centre_mm": [round(x, 2) for x in (lo_ + hi_) / 2],
            "size_mm": [round(x, 2) for x in hi_ - lo_]}


extra_shells = sorted((shell_info(sh) for sh in shells), key=lambda d: -d["volume_mm3"])[1:21]
dims = [round(d, 2) for d in ob.dimensions]
report = {"extra_shells": extra_shells, "stl": os.path.basename(stl), "triangles": len(bm.faces), "vertices": len(bm.verts), "shells": len(shells),
          "shell_volumes_mm3": sorted([round(abs(sum(f.verts[0].co.dot(f.verts[1].co.cross(f.verts[2].co))
                                                      for f in s) / 6), 2) for s in shells], reverse=True)[:10],
          "non_manifold_edges": nm, "boundary_edges": boundary, "dims_mm": dims, "min_thickness_mm": limit,
          "thin_vertices": len(thick), "thin_parts": parts, "thin_detail_clusters": len(detail),
          "thin_detail_vertices": sum(c["verts"] for c in detail),
          "overhang_deg": Q["overhang_deg"], "overhang_area_mm2": round(sum(r["area_mm2"] for r in regions), 1),
          "overhang_regions": regions}
mode = cfg.get("qa_mode", "check")
if mode == "check":
    with open(out_path(cfg, "qa.json"), "w") as f:
        json.dump(report, f, indent=2)
    stage("check", shells=len(shells), non_manifold=nm, thin_parts=len(parts))
log("QA " + json.dumps({k: v for k, v in report.items() if k not in ("thin_parts", "overhang_regions")}))
for c in parts:
    log(f"thin part: {c}")
for r in regions[:12]:
    log(f"overhang: {r}")

if mode == "render" and Q["renders"]:
    attr = me.color_attributes.new("thin", "FLOAT_COLOR", "POINT")
    for v in me.vertices:
        attr.data[v.index].color = (0.85, 0.1, 0.1, 1) if v.index in thick else (0.6, 0.6, 0.6, 1)
    me.color_attributes.active_color = attr
    over_v = {v.index for i in over for v in bm.faces[i].verts}
    attr_o = me.color_attributes.new("overhang", "FLOAT_COLOR", "POINT")
    for v in me.vertices:
        attr_o.data[v.index].color = (0.15, 0.35, 0.9, 1) if v.index in over_v else (0.6, 0.6, 0.6, 1)
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_WORKBENCH"
    sh = sc.display.shading
    sh.light, sh.show_specular_highlight, sh.show_cavity = "STUDIO", False, False
    sh.show_shadows, sh.shadow_intensity = True, 0.35
    sc.display.render_aa = "16"
    w = bpy.data.worlds.new("W")
    w.color = (0.92, 0.92, 0.92)
    sc.world = w
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    cam.data.type = "ORTHO"
    cam.data.clip_start, cam.data.clip_end = 0.1, 1000
    sc.collection.objects.link(cam)
    sc.camera = cam
    lo = Vector([min(v.co[i] for v in me.vertices) for i in range(3)])
    hi = Vector([max(v.co[i] for v in me.vertices) for i in range(3)])
    centre, size = (lo + hi) / 2, hi - lo
    full = max(size.z, max(size.x, size.y) * 1100 / 900) * 1.08
    shots = [("front", centre, 0, 8, full), ("back", centre, 180, 12, full), ("three_quarter", centre, 40, 15, full)]
    closeups = list(Q["heatmap_closeups"])
    # issue close-ups: every separate shell and every thin part large enough to be flagged
    fl = cfg.get("flag", {}).get("thin_part_min_extent_mm", 0.5)
    mid = (Vector([min(v.co[i] for v in me.vertices) for i in range(3)]) + Vector([max(v.co[i] for v in me.vertices) for i in range(3)])) / 2

    def view_from(dvec):
        """Camera yaw/elev looking along -dvec (camera sits on the dvec side of the target)."""
        dvec = Vector(dvec).normalized()
        return math.degrees(math.atan2(dvec.x, -dvec.y)), math.degrees(math.asin(max(-1.0, min(1.0, dvec.z))))
    for i, e in enumerate(report["extra_shells"][:8]):
        out = Vector(e["centre_mm"]) - mid + Vector((0, 0, 0.3 * max(1.0, (Vector(e["centre_mm"]) - mid).length)))
        yaw, elev = view_from(out if out.length > 1e-6 else (0.5, -1, 0.4))
        closeups.append({"name": f"issue_shell{i + 1}", "target_mm": e["centre_mm"], "yaw": yaw, "elev": elev,
                         "scale": max(8.0, 4 * max(e["size_mm"]))})
    fp = cfg.get("flag", {}).get("thin_part_min_protrusion_mm", 0.0)
    for i, t in enumerate([t for t in parts if 2 * t["extent_mm"] >= fl and t.get("protrusion_mm", 1e9) >= fp][:8]):
        yaw, elev = view_from(t.get("normal", (0.5, -1, 0.4)))
        closeups.append({"name": f"issue_thin{i + 1}", "target_mm": t["centre_mm"], "yaw": yaw, "elev": elev,
                         "scale": max(6.0, 8 * t["extent_mm"])})
    if Q.get("auto_closeups") and off and os.path.exists(out_path(cfg, "pose.json")):
        with open(out_path(cfg, "pose.json")) as f:
            pts = json.load(f).get("points_rig_mm", {})
        views = {"left_hand": (25, 10), "right_hand": (-30, 10), "left_knee": (70, 0), "right_knee": (-70, 0),
                 "muzzle": (0, 10), "grip": (-30, 10)}
        for k, p in pts.items():
            yaw, elev = views.get(k, (40, 10))
            closeups.append({"name": f"auto_{k}", "rig_mm": p, "yaw": yaw, "elev": elev, "scale": 14 if k == "muzzle" else 9})
    for c in closeups:  # target_mm in STL coordinates, or rig_mm (pose frame) via build offset
        t = Vector(c["target_mm"]) if "target_mm" in c else Vector(c["rig_mm"]) + Vector(off)
        shots.append((c["name"], t, c["yaw"], c["elev"], c["scale"]))
    batch = Q.get("render_set", "batch") == "batch"
    shots.append(("from_below", centre, 20, -50, full))
    os.makedirs(out_path(cfg, "renders"), exist_ok=True)
    for kind in ("heat", "grey", "overhang"):
        sh.color_type = "SINGLE" if kind == "grey" else "VERTEX"
        sh.single_color = (0.6, 0.6, 0.6)
        me.color_attributes.active_color = attr_o if kind == "overhang" else attr
        for name, target, yaw, elev, scale in shots:
            if kind in ("grey", "overhang") and name not in ("front", "back", "three_quarter", "from_below"):
                continue
            if kind == "heat" and name == "from_below":
                continue
            if batch and not ((kind == "grey" and name in ("front", "back", "three_quarter"))
                              or (kind == "heat" and name in ("three_quarter",) or (kind == "heat" and name.startswith(("auto_", "issue_"))))
                              or (kind == "overhang" and name == "from_below")):
                continue
            a, e = math.radians(yaw), math.radians(elev)
            d = Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)))
            cam.location = target + d * 200
            cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
            cam.data.ortho_scale = scale
            sc.render.resolution_x, sc.render.resolution_y = 900, 1100
            sc.render.filepath = out_path(cfg, "renders", f"{kind}_{name}.png")
            bpy.ops.render.render(write_still=True)
    log("renders written to", out_path(cfg, "renders"))
    stage("render", shots=len(os.listdir(out_path(cfg, "renders"))))
