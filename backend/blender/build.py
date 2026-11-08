# SPDX-License-Identifier: GPL-3.0-or-later
"""Build stage: displaced, repaired, based, watertight print mesh -> <out>/<output_name>.stl + build.json.

One Blender process runs the sub-stages displace -> repair -> base -> finalize (each logged as a [stage]
marker for the job runner): the base must be fused into the model by the same voxel remesh, so splitting
them into separate processes would cost a second full-resolution remesh per job.
  displace   subdivide + height-map displacement per material, inflate, solidify
  repair     cap open loops, bridge weakly-joined parts, auto-thicken thin walls
  base       round/oval base with terrain + ground fill under the feet, none (vehicles), or flying socket
  finalize   (finalize.py, its own process) voxel remesh union, debris cleanup, decimate, socket cut, export +
             re-import repair loop
"""
import json
import math
import os
import sys
import time

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import base as basemod  # noqa: E402
from common import (boundary_loops, face_shells, import_baked, load_cfg, log, mesh_object,  # noqa: E402
                    mem_gb, name_matches, out_path, shell_volume, stage)

T0 = time.time()
cfg = load_cfg()
G = cfg["geometry"]
stats = {"name": cfg["name"], "fixes": []}


def fix(kind, **detail):
    stats["fixes"].append({"fix": kind, **detail})
    log(f"fix {kind}: " + ", ".join(f"{k}={v}" for k, v in detail.items()))


# ---------------------------------------------------------------- low-poly fixes
def cap_open_loops(ob):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    capped = 0
    limit = cfg["materials"].get(ob["mat"], {}).get("cap_open_loops_max_perimeter_mm",
                                                     G["cap_open_loops_max_perimeter_mm"])
    for edges, per, c in boundary_loops(bm):
        if per > limit:
            continue
        if any((c - Vector(e[:3])).length < e[3] for e in G["cap_exclude_near"]):
            continue
        bmesh.ops.holes_fill(bm, edges=edges, sides=len(edges) + 1)
        capped += 1
        fix("cap_open_loop", object=ob.name, perimeter_mm=round(per, 2), centre_mm=[round(x, 2) for x in c])
    bm.to_mesh(ob.data)
    bm.free()
    return capped


def region_group(ob, name, centre, radius):
    """Vertex group: weight 1 within half the radius, smooth falloff to 0 at the radius."""
    vg = ob.vertex_groups.new(name=name)
    c = Vector(centre)
    hit = 0
    for v in ob.data.vertices:
        d = (v.co - c).length
        if d < radius:
            t = min(1.0, max(0.0, (radius - d) / (radius * 0.5)))
            vg.add([v.index], t * t * (3 - 2 * t), "REPLACE")
            hit += 1
    return vg, hit


def height_texture(mat):
    path = out_path(cfg, f"height_{mat}.png")
    if not os.path.exists(path):
        return None
    img = bpy.data.images.load(path, check_existing=True)
    img.colorspace_settings.name = "Non-Color"
    tex = bpy.data.textures.new(f"height_{mat}", "IMAGE")
    tex.image = img
    tex.extension = "REPEAT"  # Unity textures repeat; mirrored islands live in UV tile u=1..2
    tex.use_interpolation = True
    if hasattr(tex, "use_mipmap"):
        tex.use_mipmap = False
    return tex


def add_offset(ob, name, offset, group=None):
    d = ob.modifiers.new(name, "DISPLACE")  # no texture: uniform push along normals
    d.direction = "NORMAL"
    d.mid_level = 0.0
    d.strength = offset
    if group:
        d.vertex_group = group


# ---------------------------------------------------------------- mesh helpers
def drop_object(ob):
    """Remove an object and its mesh datablock (Blender keeps orphaned meshes in memory until the end)."""
    me = ob.data if ob.type == "MESH" else None
    bpy.data.objects.remove(ob)
    if me is not None and me.users == 0:
        bpy.data.meshes.remove(me)


def bm_from_objects(objs):
    dg = bpy.context.evaluated_depsgraph_get()
    bm = bmesh.new()
    for ob in objs:
        me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg), depsgraph=dg)
        bm.from_mesh(me)
        bpy.data.meshes.remove(me)
    return bm


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


def voxel_remesh_plus(bm, extra_bm, label):
    """voxel_remesh of bm + extra_bm without copying bm: extra's geometry is appended, remeshed with it, and
    removed again (the mesh handed to the remesher is the same as pre.copy() + extra)."""
    if not len(extra_bm.faces):
        return voxel_remesh(bm, label)
    for v in bm.verts:
        v.tag = True
    me_x = bpy.data.meshes.new("extra_probe")
    extra_bm.to_mesh(me_x)
    bm.from_mesh(me_x)
    bpy.data.meshes.remove(me_x)
    out = voxel_remesh(bm, label)
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.tag], context="VERTS")
    for v in bm.verts:
        v.tag = False
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


def capsule(bm, a, b, r):
    axis = b - a
    L = axis.length
    rot = axis.to_track_quat("Z", "Y").to_matrix().to_4x4()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=20, radius1=r, radius2=r, depth=L,
                          matrix=Matrix.Translation((a + b) / 2) @ rot)
    for p in (a, b):
        bmesh.ops.create_uvsphere(bm, u_segments=20, v_segments=10, radius=r, matrix=Matrix.Translation(p))


def pick_spread(cands, cf, feasible, gap_offset=0.0):
    """Connector sites: among sites where a bridge of at least min_radius_mm fits inside both parts
    (feasible(c) -> radius or None), take the closest gap, then up to max_per_shell spread-out sites
    whose gap is within max_extra_gap_mm of it: short hidden bridges, never struts or rim-edge stubs.
    Gaps longer than max_bridge_gap_mm (after subtracting gap_offset) are never bridged.
    cands are sorted by gap. Returns [(cand, radius)]."""
    cands = [c for c in cands if c[0] - gap_offset <= cf["max_bridge_gap_mm"]]
    ok = []
    for c in cands:
        r = feasible(c)
        if r is not None and r >= cf["min_radius_mm"]:
            ok.append((c, r))
        if ok and c[0] > ok[0][0][0] + cf["max_extra_gap_mm"]:
            break  # sorted by gap: nothing further can qualify
    if not ok:
        return []
    near = [o for o in ok if o[0][0] <= ok[0][0][0] + cf["max_extra_gap_mm"]]
    picked = [near[0]]
    while len(picked) < cf["max_per_shell"]:
        cand = max(near, key=lambda o: min((o[0][1].co - k[0][1].co).length for k in picked))
        if min((cand[0][1].co - k[0][1].co).length for k in picked) < cf["min_spacing_mm"]:
            break
        picked.append(cand)
    return picked


def local_thickness(bvh, co, normal, max_mm=3.0):
    """Distance from a surface point straight through its own part to the far wall."""
    hit, _, _, d = bvh.ray_cast(co - normal * 1e-4, -normal, max_mm)
    return d if hit is not None else 0.0


def source_floating_connectors(objs, cf, extra):
    """Bridge source parts that are not solidly joined to the main body.

    Game meshes are built from overlapping parts. Some only graze each other (shoulder pads sit on
    the torso by a hair), and Catmull-Clark shrinkage or displacement then leaves them floating or
    joined by a hairline. Test on the subdivided surface with every part eroded by erode_mm: parts
    overlapping by less than 2 x erode_mm fall apart. Groups not linked to the main group get
    capsule connectors to their nearest neighbour, sunk past the relief into both sides.
    """
    dg = bpy.context.evaluated_depsgraph_get()
    parts = []
    for ob in objs:
        tmp = ob.modifiers.new("ProbeSubdiv", "SUBSURF")
        tmp.subdivision_type = "SIMPLE" if G["subdiv"] == "simple" else "CATMULL_CLARK"
        tmp.levels = 2
        dg.update()
        me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg), depsgraph=dg)
        ob.modifiers.remove(tmp)
        bm = bmesh.new()
        bm.from_mesh(me)
        bpy.data.meshes.remove(me)
        bmesh.ops.triangulate(bm, faces=bm.faces)
        for sh in face_shells(bm):
            pb = bmesh.new()
            vmap = {}
            for f in sh:
                for v in f.verts:
                    if v not in vmap:
                        vmap[v] = pb.verts.new(v.co)
                try:
                    pb.faces.new([vmap[v] for v in f.verts])
                except ValueError:
                    pass
            pb.normal_update()
            er = pb.copy()
            er.normal_update()
            for v in er.verts:
                v.co -= v.normal * cf["erode_mm"]
            parts.append({"name": ob.name, "bm": pb, "bvh": BVHTree.FromBMesh(pb), "bvh_er": BVHTree.FromBMesh(er),
                          "vol": abs(shell_volume(list(pb.faces)))})
            er.free()
        bm.free()

    # connected groups of parts that still overlap after erosion
    n = len(parts)
    group = list(range(n))

    def root(i):
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if parts[i]["bvh_er"].overlap(parts[j]["bvh_er"]):
                group[root(i)] = root(j)
    groups = {}
    for i in range(n):
        groups.setdefault(root(i), []).append(i)
    main = max(groups.values(), key=lambda g: sum(parts[i]["vol"] for i in g))

    for g in groups.values():
        if g is main:
            continue
        vol = sum(parts[i]["vol"] for i in g)
        names = sorted({parts[i]["name"] for i in g})
        if vol < cf["min_shell_volume_mm3"]:
            continue  # tiny bits (pull-rings etc.): rely on the remesh union / debris cleanup
        best = []
        for i in g:
            for v in parts[i]["bm"].verts:
                for j in range(n):
                    if j in g:
                        continue
                    loc, nrm, _, d = parts[j]["bvh"].find_nearest(v.co)
                    if loc is not None and d < cf["max_gap_mm"] + 2 * cf["erode_mm"]:
                        best.append((d, v, loc, nrm))
        verts = [v for i in g for v in parts[i]["bm"].verts]
        centre = sum((v.co for v in verts), Vector()) / len(verts)
        if not best:
            fix("source_part_left_separate", objects=names, volume_mm3=round(vol, 2),
                centre_mm=[round(x, 2) for x in centre])
            continue
        best.sort(key=lambda b: b[0])
        radius = min(cf["radius_mm"], cf["radius_per_size"] * vol ** (1 / 3))
        depth = cf["erode_mm"] + cf["embed_mm"]
        part_of = {v: i for i in g for v in parts[i]["bm"].verts}
        walls = {}

        def feasible(c):
            # a bridge must stay inside both parts: size it by their local wall thickness
            d, v, loc, nrm = c
            ta = local_thickness(parts[part_of[v]]["bvh"], v.co, v.normal)
            tb = min((local_thickness(parts[j]["bvh"], loc, nrm) for j in range(n)
                      if j not in g and parts[j]["bvh"].find_nearest(loc, 1e-4)[0] is not None), default=0.0)
            walls[id(c)] = (ta, tb)
            return min(radius, 0.45 * min(ta, tb))

        picked = pick_spread(best, cf, feasible)
        placed = []
        for c, r in picked:
            d, v, loc, nrm = c
            ta, tb = walls[id(c)]
            capsule(extra, v.co - v.normal * min(depth, ta / 2), loc - nrm * min(depth, tb / 2), r)
            placed.append(([round(x, 2) for x in v.co], round(r, 3), round(d, 3)))
        if not placed and cf.get("axis_bridge", False):
            others = [parts[j]["bvh"] for j in range(n) if j not in g]

            def cast(end, dvec, max_mm):
                hits = [h for h in (b.ray_cast(end + dvec * 0.02, dvec, max_mm) for b in others) if h[0] is not None]
                if not hits:
                    return None
                h = min(hits, key=lambda h: h[3])
                return h[0], h[3] + 0.02
            ab = axis_bridge([v for i in g for v in parts[i]["bm"].verts], cast, cf)
            if ab is not None:
                capsule(extra, ab[0], ab[1], ab[2])
                fix("connect_source_part_axis", objects=names, volume_mm3=round(vol, 2), centre_mm=[round(x, 2) for x in centre],
                    axis_gap_mm=round(ab[3], 3), radius_mm=round(ab[2], 3), start_mm=[round(x, 2) for x in ab[0]],
                    end_mm=[round(x, 2) for x in ab[1]])
                continue
        tried = [{"gap": round(c[0], 3), "walls": [round(w, 3) for w in walls[id(c)]]} for c in best if id(c) in walls][:4]
        fix("connect_source_part" if placed else "source_part_no_bridge_fits", objects=names, parts=len(g),
            volume_mm3=round(vol, 2), centre_mm=[round(x, 2) for x in centre], min_gap_mm=round(best[0][0], 3),
            connectors=[p[0] for p in placed], radii_mm=[p[1] for p in placed], gaps_mm=[p[2] for p in placed],
            **({} if placed else {"tried": tried}))
    for p in parts:
        p["bm"].free()


def axis_bridge(verts, cast, cf, grow=0.0):
    """Elongated floating part (antenna, rod, spike) whose end sits short of the body: continue the part along
    its own principal axis until it reaches real geometry. The connector reads as the rod carrying on into the
    part it was plugged into, unlike a sideways strut. cast(origin, direction, max_mm) -> (hit, gap_mm) or None
    finds the nearest other surface; grow = how much thicker the real part is than verts (eroded probe).
    Returns (start, end, radius, gap) or None."""
    pts = np.array([v.co[:] for v in verts])
    c = pts.mean(axis=0)
    _, sv, vt = np.linalg.svd(pts - c, full_matrices=False)
    proj = (pts - c) @ vt.T
    ext = proj.max(axis=0) - proj.min(axis=0)
    if ext[0] < cf.get("axis_min_aspect", 3.0) * max(ext[1], 1e-6):
        return None
    axis = Vector(vt[0])
    width = float(ext[1]) + grow
    r = min(cf["radius_mm"], cf.get("axis_radius_frac", 0.4) * width)
    if r < cf.get("axis_min_radius_mm", 0.2):
        return None
    best = None
    for sgn in (1, -1):
        t = proj[:, 0] * sgn
        end = Vector(pts[t >= t.max() - 0.3].mean(axis=0))
        d = axis * sgn
        res = cast(end, d, cf.get("max_axis_gap_mm", 3.0))
        if res is None:
            continue
        hit, gap = res
        if best is None or gap < best[3]:
            sink = min(1.0, 0.25 * float(ext[0]))
            best = (end - d * sink, hit + d * max(cf["embed_mm"], 0.5), r, gap)
    return best


def cast_skipping_self(bvh_all, bvh_self, skin):
    """cast() for axis_bridge on the eroded probe: bvh_all is the real surface, which also holds the part's own
    real skin (within skin of the eroded part): rays pass through that and stop at the next surface."""
    def cast(end, d, max_mm):
        o, travelled = end + d * 0.02, 0.02
        for _ in range(6):
            h, _, _, dist = bvh_all.ray_cast(o, d, max_mm + skin - travelled)
            if h is None:
                return None
            travelled += dist
            if bvh_self.find_nearest(h, skin + 0.1)[0] is None:
                return h, travelled - skin
            o = h + d * 0.02
            travelled += 0.02
        return None
    return cast


def subdiv_levels(ob):
    """geometry.levels, or with adaptive_subdiv just enough levels for edges of ~subdiv_edge_voxels voxels:
    finer subdivision than the voxel remesh can hold only costs memory (v1 at 0.03 mm keeps level 4)."""
    if not G.get("adaptive_subdiv"):
        return G["levels"]
    me = ob.data
    if not me.edges:
        return G["levels"]
    L = sum((me.vertices[e.vertices[0]].co - me.vertices[e.vertices[1]].co).length for e in me.edges) / len(me.edges)
    lv = math.ceil(math.log2(max(L, 1e-6) / (G.get("subdiv_edge_voxels", 2.3) * G["voxel_mm"])))
    lv = max(1, min(G["levels"], lv))
    if lv != G["levels"]:
        log(f"{ob.name}: mean edge {L:.2f} mm -> subdivision level {lv}")
    return lv


# ---------------------------------------------------------------- build
objs, info = import_baked(cfg)
stats["scale"] = info
textures = {}
extra = bmesh.new()  # connectors + base, unioned by the final remesh
for ob in objs:
    cap_open_loops(ob)
if cfg["connect_floating"]["enabled"]:
    source_floating_connectors(objs, cfg["connect_floating"], extra)
    if cfg.get("debug_stop_after") == "source_connectors":  # quick test of the source-part bridges
        log("debug stop after source connectors")
        sys.exit(0)
if cfg.get("debug_stop_after") == "connectors":
    log("debug stop after connectors")
    sys.exit(0)
LEVELS = {ob.name: subdiv_levels(ob) for ob in objs}
if G.get("adaptive_subdiv") and G.get("max_subdiv_faces"):
    # memory scales with the subdivided face count (several copies are probed): drop the finest objects one
    # level at a time until the whole model fits the budget
    def total():
        return sum(len(ob.data.polygons) * 4 ** LEVELS[ob.name] for ob in objs)
    while total() > G["max_subdiv_faces"] and max(LEVELS.values()) > 1:
        top = max(LEVELS.values())
        for ob in objs:
            if LEVELS[ob.name] == top:
                LEVELS[ob.name] -= 1
    fix("subdiv_budget", faces=total(), budget=G["max_subdiv_faces"], levels=sorted(set(LEVELS.values())))
for ob in objs:
    mods_note = []
    sub = ob.modifiers.new("Subdiv", "SUBSURF")
    sub.subdivision_type = "SIMPLE" if G["subdiv"] == "simple" else "CATMULL_CLARK"
    sub.levels = sub.render_levels = LEVELS[ob.name]
    mat = ob["mat"]
    if mat and mat not in textures:
        textures[mat] = height_texture(mat)
    tex = textures.get(mat)
    strength = cfg["materials"].get(mat, {}).get("displace_mm", G["displace_mm"])  # per-material override
    if tex and strength > 0 and ob.data.uv_layers:
        d = ob.modifiers.new("Height", "DISPLACE")
        d.texture, d.texture_coords, d.uv_layer = tex, "UV", ob.data.uv_layers[0].name
        d.direction, d.mid_level, d.strength = "NORMAL", 0.5, strength
        mods_note.append(f"height {strength} mm")
    for io in G["inflate_objects"]:
        if name_matches(ob.name, io["name"]):
            add_offset(ob, "Inflate", io["offset_mm"])
            fix("inflate_object", object=ob.name, offset_mm=io["offset_mm"], note=io.get("note", ""))
    for i, reg in enumerate(G["inflate_regions"]):
        vg, hit = region_group(ob, f"inflate_{i}", reg["centre_mm"], reg["radius_mm"])
        if hit:
            add_offset(ob, f"InflateRegion{i}", reg["offset_mm"], vg.name)
            fix("inflate_region", object=ob.name, centre_mm=reg["centre_mm"], radius_mm=reg["radius_mm"],
                offset_mm=reg["offset_mm"], verts=hit, note=reg.get("note", ""))
    if G["solidify_mm"] > 0:
        so = ob.modifiers.new("Solidify", "SOLIDIFY")
        so.thickness, so.offset, so.use_even_offset = G["solidify_mm"], -1.0, False  # even offset spikes
    log(f"{ob.name}: {', '.join(mods_note) or 'no height'}")

pre = bm_from_objects(objs)
pre_outer = None  # same surface without Solidify's inward copy: that copy faces inward and would
if cfg["connect_floating"]["enabled"]:  # move *outward* when eroded, defeating the connectivity test
    for ob in objs:
        for m in ob.modifiers:
            if m.type == "SOLIDIFY":
                m.show_viewport = False
    bm_o = bm_from_objects(objs)  # kept as a compact Mesh (about a quarter of a BMesh) until the erosion test
    pre_outer = bpy.data.meshes.new("pre_outer")
    bm_o.to_mesh(pre_outer)
    bm_o.free()
stats["tris_subdivided"] = len(pre.faces)
for ob in objs:  # the subdivided/displaced copies now live in pre / pre_outer: free the evaluated sources
    drop_object(ob)
objs = []
log(f"subdivided + displaced: {len(pre.faces):,} faces ({time.time() - T0:.0f}s)")
stage("displace", faces=len(pre.faces), seconds=round(time.time() - T0))
lo = Vector([min(v.co[i] for v in pre.verts) for i in range(3)])
hi = Vector([max(v.co[i] for v in pre.verts) for i in range(3)])

cf = cfg["connect_floating"]
at = cfg["auto_thicken"]
probe = None
if cf["enabled"] or at["enabled"]:
    probe = voxel_remesh(pre, "probe")  # what the final remesh would give: measure on this (pre is not changed)
    cleanup(probe)
    probe.normal_update()

if at["enabled"]:
    for it in range(at["iterations"]):
        if it > 0:  # re-measure what the first push left thin, connectors included this time
            probe.free()
            probe = voxel_remesh_plus(pre, extra, f"probe_{it + 1}")  # same mesh as pre.copy() + extra, no copy
            cleanup(probe)
            probe.normal_update()
        # Measure walls on the probe, then push the pre-remesh surface apart where they are too thin.
        probe.verts.ensure_lookup_table()
        bvh_p = BVHTree.FromBMesh(probe)
        fn_p = [f.normal.copy() for f in probe.faces]
        thin = {}
        for v in probe.verts:
            hit, _, fi, d = bvh_p.ray_cast(v.co - v.normal * 1e-4, -v.normal, at["target_mm"])
            if hit is not None and fn_p[fi].dot(v.normal) < -0.5:
                thin[v.index] = d
        log(f"auto_thicken: measured {len(probe.verts):,} probe verts, {len(thin):,} thin ({time.time() - T0:.0f}s)")
        seen, kept, n_clusters = set(), [], 0
        for i in thin:
            if i in seen:
                continue
            stack, mem = [probe.verts[i]], []
            seen.add(i)
            while stack:
                cur = stack.pop()
                mem.append(cur)
                for e in cur.link_edges:
                    o = e.other_vert(cur)
                    if o.index in thin and o.index not in seen:
                        seen.add(o.index)
                        stack.append(o)
            c = sum((v.co for v in mem), Vector()) / len(mem)
            if 2 * max((v.co - c).length for v in mem) >= at["min_extent_mm"]:
                kept += mem
                n_clusters += 1
        # Smooth offset field: thin-wall deficits on a dense grid, dilated so peaks survive, then blurred
        # so the push fades out gradually (a blocky field stamps visible box edges into the surface).
        cell = at["cell_mm"]
        pad = 4 * cell
        origin = np.array([lo.x, lo.y, lo.z]) - pad
        dims = (np.ceil((np.array([hi.x, hi.y, hi.z]) + pad - origin) / cell) + 1).astype(int)
        grid = np.zeros(dims, dtype=np.float32)
        if kept:
            pts = np.array([v.co[:] for v in kept])
            offs = np.array([min(at["max_offset_mm"], (at["target_mm"] - thin[v.index]) / 2 + 0.02) for v in kept])
            idx = np.clip(np.floor((pts - origin) / cell).astype(int), 0, dims - 1)
            np.maximum.at(grid, (idx[:, 0], idx[:, 1], idx[:, 2]), offs)
        for _ in range(2):  # dilate ~2 cells (6-neighbourhood, twice)
            g = grid.copy()
            for ax in range(3):
                g = np.maximum(g, np.roll(grid, 1, ax))
                g = np.maximum(g, np.roll(grid, -1, ax))
            grid = g
        for _ in range(3):  # binomial blur ~ gaussian sigma 1.2 cells
            for ax in range(3):
                grid = 0.25 * np.roll(grid, 1, ax) + 0.5 * grid + 0.25 * np.roll(grid, -1, ax)
        log(f"auto_thicken: {n_clusters} clusters, {len(kept):,} verts, grid {tuple(dims)} ({time.time() - T0:.0f}s)")

        def sample(p):
            """Trilinear sample of the grid at points p (N x 3)."""
            f = (p - origin) / cell - 0.5
            i0 = np.clip(np.floor(f).astype(int), 0, dims - 2)
            t = np.clip(f - i0, 0, 1)
            out = np.zeros(len(p), dtype=np.float32)
            for dx in (0, 1):
                for dy in (0, 1):
                    for dz in (0, 1):
                        w = (t[:, 0] if dx else 1 - t[:, 0]) * (t[:, 1] if dy else 1 - t[:, 1]) * (t[:, 2] if dz else 1 - t[:, 2])
                        out += w * grid[i0[:, 0] + dx, i0[:, 1] + dy, i0[:, 2] + dz]
            return out

        def apply_field(bm):
            own = not isinstance(bm, bpy.types.Mesh)
            if own:
                me = bpy.data.meshes.new("thicken")
                bm.to_mesh(me)
            else:
                me = bm
            n = len(me.vertices)
            co = np.empty(n * 3, dtype=np.float32)
            nrm = np.empty(n * 3, dtype=np.float32)
            me.vertices.foreach_get("co", co)
            me.vertex_normals.foreach_get("vector", nrm)
            co, nrm = co.reshape(-1, 3), nrm.reshape(-1, 3)
            off = sample(co.astype(np.float64))
            co += nrm * off[:, None]
            me.vertices.foreach_set("co", co.ravel())
            if own:
                bm.clear()
                bm.from_mesh(me)
                bpy.data.meshes.remove(me)
            else:
                me.update()
            return int((off > 1e-4).sum()), float(off.max(initial=0))

        moved, peak = apply_field(pre)
        if pre_outer is not None:
            apply_field(pre_outer)
        fix("auto_thicken", iteration=it + 1, target_mm=at["target_mm"], thin_clusters=n_clusters, thin_probe_verts=len(kept),
            moved_verts=moved, max_offset_mm=round(peak, 3))


if cf["enabled"]:
    # Connectivity is judged on an eroded copy: parts that only touch through a contact thinner than
    # 2 x erode_mm separate there, so they get real connectors instead of a hairline join.
    probe.free()
    eroded = bmesh.new()
    eroded.from_mesh(pre_outer)
    bpy.data.meshes.remove(pre_outer)
    if len(extra.faces):  # include connectors already placed, so their parts are not bridged twice
        me_x = bpy.data.meshes.new("extra_probe")
        extra.to_mesh(me_x)
        eroded.from_mesh(me_x)
        bpy.data.meshes.remove(me_x)
    eroded.normal_update()
    for v in eroded.verts:
        v.co -= v.normal * cf["erode_mm"]
    probe = voxel_remesh(eroded, "probe_eroded")
    eroded.free()  # only the remesh of it is used below (memory: one BMesh of the whole model less)
    cleanup(probe)
    probe.normal_update()
    shells = sorted(face_shells(probe), key=lambda s: -abs(shell_volume(s)))
    main = shells[0]
    # distance queries against the main shell, straight from its polygons (no BMesh copy of it)
    probe.verts.index_update()
    mverts = {}
    mpolys = []
    for f in main:
        mpolys.append([mverts.setdefault(v.index, len(mverts)) for v in f.verts])
    probe.verts.ensure_lookup_table()
    mco = [None] * len(mverts)
    for vi, k in mverts.items():
        mco[k] = probe.verts[vi].co.copy()
    bvh = BVHTree.FromPolygons(mco, mpolys)
    del mverts, mpolys, mco
    log(f"connectivity: {len(shells)} shells on the eroded probe; main {abs(shell_volume(main)):.0f} mm^3")
    bvh_pre = None  # real (un-eroded) surface, built only when an axis bridge is tried
    for sh in shells[1:]:
        vol = abs(shell_volume(sh))
        if vol < cf.get("eroded_volume_factor", 1.0) * cf["min_shell_volume_mm3"]:
            continue  # debris (eroded shells are smaller than the final ones: big classes test a fraction)
        radius = min(cf["radius_mm"], cf["radius_per_size"] * vol ** (1 / 3))
        verts = list({v for f in sh for v in f.verts})
        gaps = [(bvh.find_nearest(v.co), v) for v in verts]
        reach = cf["max_gap_mm"] + 2 * cf["erode_mm"]  # gaps grow by the erosion on both sides
        cand = [(g[3], v, g[0], g[1]) for g, v in gaps if g[0] is not None and g[3] < reach]
        centre = sum((v.co for v in verts), Vector()) / len(verts)
        min_gap = min(g[3] for g, v in gaps if g[0] is not None)
        sverts = list({v for f in sh for v in f.verts})
        sidx = {v: i for i, v in enumerate(sverts)}
        bvh_sh = BVHTree.FromPolygons([v.co.copy() for v in sverts], [[sidx[v] for v in f.verts] for f in sh])

        def try_axis():
            global bvh_pre
            if bvh_pre is None:
                bvh_pre = BVHTree.FromBMesh(pre)
            ab = axis_bridge(sverts, cast_skipping_self(bvh_pre, bvh_sh, cf["erode_mm"]), cf, grow=2 * cf["erode_mm"])
            if ab is None:
                return False
            capsule(extra, ab[0], ab[1], ab[2])
            fix("connect_floating_shell_axis", volume_mm3=round(vol, 2), centre_mm=[round(x, 2) for x in centre],
                min_gap_mm=round(min_gap, 3), axis_gap_mm=round(ab[3], 3), radius_mm=round(ab[2], 3),
                start_mm=[round(x, 2) for x in ab[0]], end_mm=[round(x, 2) for x in ab[1]])
            return True
        if not cand:
            if not (cf.get("axis_bridge", False) and try_axis()):
                fix("floating_shell_left_separate", volume_mm3=round(vol, 2), centre_mm=[round(x, 2) for x in centre],
                    min_gap_mm=round(min_gap, 3))
            continue
        cand.sort(key=lambda c: c[0])
        walls = {}

        def feasible(c):
            # eroded surfaces sit erode_mm inside the real ones: the real wall is 2 x erode_mm thicker
            gap, v, loc, nrm = c
            ta = local_thickness(bvh_sh, v.co, v.normal) + 2 * cf["erode_mm"]
            tb = local_thickness(bvh, loc, nrm) + 2 * cf["erode_mm"]
            walls[id(c)] = (ta, tb)
            return min(radius, 0.45 * min(ta, tb))

        picked = pick_spread(cand, cf, feasible, gap_offset=2 * cf["erode_mm"])  # eroded gaps are wider
        placed = []
        for c, r in picked:
            gap, v, loc, nrm = c
            ta, tb = walls[id(c)]
            capsule(extra, v.co - v.normal * min(cf["embed_mm"], ta / 2 - cf["erode_mm"]),
                    loc - nrm * min(cf["embed_mm"], tb / 2 - cf["erode_mm"]), r)
            placed.append(([round(x, 2) for x in v.co], round(r, 3)))
        if not placed and cf.get("axis_bridge", False) and try_axis():
            continue
        kind = "connect_floating_shell"
        if not placed and cf.get("thin_wall_fallback", False):
            # no bridge fits inside the walls (the part sits on a thin hull plate): a loose part always fails, a
            # min_radius connector that may bulge slightly behind a thin wall is the lesser evil (logged)
            ok = [c for c in cand if c[0] - 2 * cf["erode_mm"] <= cf["max_bridge_gap_mm"]]
            if ok:
                c = ok[0]
                if id(c) not in walls:
                    feasible(c)
                gap, v, loc, nrm = c
                ta, tb = walls[id(c)]
                r = cf["min_radius_mm"]
                capsule(extra, v.co - v.normal * max(0.05, min(cf["embed_mm"], ta / 2 - cf["erode_mm"])),
                        loc - nrm * max(0.05, min(cf["embed_mm"], tb / 2 - cf["erode_mm"])), r)
                placed.append(([round(x, 2) for x in v.co], round(r, 3)))
                kind = "connect_floating_shell_thin_wall"
        fix(kind if placed else "floating_shell_no_bridge_fits", volume_mm3=round(vol, 2),
            centre_mm=[round(x, 2) for x in centre], min_gap_mm=round(min_gap, 3),
            connectors=[p[0] for p in placed], radii_mm=[p[1] for p in placed])
    bvh_pre = None
    bvh = None
    shells = main = None
    probe.free()
    pre_outer = None

stage("repair_pre", seconds=round(time.time() - T0))  # thicken + connectors done; final remesh repairs below

bc = cfg.get("base")
shape = (bc or {}).get("shape", "round") if bc else "none"
socket = None
if bc and shape in ("round", "oval"):
    # centre the base on the smallest circle around the footprint (everything up to footprint_height_mm)
    fh = bc.get("footprint_height_mm", 3.0)
    feet = [(round(v.co.x, 3), round(v.co.y, 3)) for v in pre.verts if v.co.z < lo.z + fh]
    hull = basemod.hull2d(feet)
    (cx_, cy_), foot_r = basemod.min_circle(hull)
    cxy = Vector((cx_, cy_))
    if bc.get("centre_on") == "plan":
        # centre under the whole model (a bike with the front wheel up touches down only at the back) as long
        # as the footprint still fits the top
        xs = [v.co.x for v in pre.verts]
        ys = [v.co.y for v in pre.verts]
        pc = Vector(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
        ok = basemod.fit(bc, pc, hull)[0] if shape == "oval" else max((Vector(h) - pc).length for h in hull) <= bc["top_diameter_mm"] / 2
        log(f"base centre: plan-view centre {'used' if ok else 'rejected (footprint would not fit)'}")
        if ok:
            cxy = pc
    sf = bc.get("sole_fill")
    top = bc.get("top") or ("terrain" if bc.get("terrain") else "flat")
    if top == "dome":
        import base_dome
        dinfo = base_dome.base_dome(extra, cxy, bc, pre)
        fix("base_dome", **dinfo)
        stats["base_dome"] = dinfo
        sf = None
    elif top == "terrain":
        fx = list(set((round(x, 1), round(y, 1)) for x, y in feet))
        mound = None
        if sf:  # on terrain the gaps close with a ground mound, not prisms (those read as a plinth)
            t0 = time.time()
            mound, minfo = basemod.sole_mound(pre, cxy, basemod.axes(bc, "top"), bc["sink_mm"], sf)
            log(f"sole mound in {time.time() - t0:.0f}s")
            fix("sole_mound", **minfo, slope=sf.get("slope", 1.0), blur_passes=sf.get("blur_passes", 3))
        tinfo = basemod.base_terrain(extra, cxy, bc, fx, mound)
        fix("base_terrain", **tinfo, seed=bc["terrain"].get("seed", 1))
    else:
        basemod.base_disc(extra, cxy, bc)
    if sf and top == "flat":
        n_fill, worst = basemod.sole_fill(pre, extra, bc["sink_mm"], sf["max_gap_mm"], bc["sink_mm"] - sf["embed_mm"],
                                          sf.get("prism_min_down", 0.7))
        fix("sole_fill", prisms=n_fill, largest_gap_filled_mm=round(worst, 2), max_gap_mm=sf["max_gap_mm"])
    if cfg.get("debug_stop_after") == "base":  # preview: base + sole fill only
        me_dbg = bpy.data.meshes.new("debug_base")
        extra.to_mesh(me_dbg)
        ob_dbg = mesh_object("debug_base", me_dbg)
        for o in bpy.context.scene.objects:
            o.select_set(o is ob_dbg)
        bpy.ops.wm.stl_export(filepath=out_path(cfg, "debug_base.stl"), export_selected_objects=True)
        log("debug stop after base")
        sys.exit(0)
    if shape == "round":
        top_r = bc["top_diameter_mm"] / 2
        fits, margin = foot_r <= top_r, top_r - foot_r
    else:
        fits, margin, _ = basemod.fit(bc, cxy, hull)
    fix("base", shape=shape, size=cfg.get("base_size_used"),
        **{k: bc[k] for k in ("bottom_diameter_mm", "top_diameter_mm", "bottom_mm", "top_mm", "height_mm", "sink_mm") if k in bc},
        centre_mm=[round(cxy.x, 2), round(cxy.y, 2)], footprint_diameter_mm=round(2 * foot_r, 2),
        footprint_fits_top=fits, margin_mm=round(margin, 2))
    if not fits:
        log(f"WARNING footprint {2 * foot_r:.2f} mm does not fit the base top")
    stats["footprint_diameter_mm"] = round(2 * foot_r, 2)
    stats["footprint_fits_base"] = fits
elif bc and shape == "flying_socket":
    # stand socket under the centre of the plan-view footprint, from the lowest surface up into a boss
    xs = [v.co.x for v in pre.verts]
    ys = [v.co.y for v in pre.verts]
    cxy = Vector(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
    bvh_pre = BVHTree.FromBMesh(pre)
    hit = bvh_pre.ray_cast(Vector((cxy.x, cxy.y, lo.z - 1.0)), Vector((0, 0, 1)), hi.z - lo.z + 2)
    z_under = hit[0].z if hit[0] is not None else lo.z
    basemod.flying_boss(extra, cxy, z_under, bc)
    socket = {"centre": [cxy.x, cxy.y], "z_under": z_under}
    fix("flying_socket_boss", centre_mm=[round(cxy.x, 2), round(cxy.y, 2)], underside_z_mm=round(z_under, 2),
        socket_diameter_mm=bc["socket_diameter_mm"], socket_depth_mm=bc["socket_depth_mm"])
else:
    fix("no_base", reason=f"base shape {shape} (GW: use hull)" if bc else "no base configured")
stage("base", shape=shape)

me_extra = bpy.data.meshes.new("extra")
extra.to_mesh(me_extra)
pre.from_mesh(me_extra)
bpy.data.meshes.remove(me_extra)
extra.free()
# The final voxel remesh runs in its own process (finalize.py): the probes, the subdivided copies and Blender's
# freed-but-kept memory of this process are gone there, so the solid stage fits the runner's memory cap.
me_u = bpy.data.meshes.new("union_input")
pre.to_mesh(me_u)
pre.free()
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob)
ob_u = mesh_object("union_input", me_u)
bpy.data.orphans_purge(do_local_ids=True, do_recursive=True)
bpy.ops.wm.save_as_mainfile(filepath=out_path(cfg, "union_input.blend"), compress=False)
stats["seconds_part1"] = round(time.time() - T0)
with open(out_path(cfg, "build_part1.json"), "w") as f:
    json.dump({"stats": stats, "info": info, "socket": socket, "lo": list(lo), "hi": list(hi)}, f, indent=1)
stage("union_saved", faces=len(me_u.polygons), seconds=round(time.time() - T0))
log(f"union input saved: {len(me_u.polygons):,} faces")
