"""Geometric comparison of two STLs (regression test).

Run: blender -b --factory-startup -P compare_stl.py -- <reference.stl> <candidate.stl> <out.json>
Reports triangles, shells, volume, bounding box and the surface distance both ways (every vertex of one
mesh to the nearest point of the other): mean, p99, max.
"""
import json
import os
import sys

import bmesh
import bpy
import numpy as np
from mathutils.bvhtree import BVHTree

argv = sys.argv[sys.argv.index("--") + 1:]
ref, cand, out = (os.path.abspath(a) for a in argv[:3])


def load(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.stl_import(filepath=path)
    ob = bpy.context.selected_objects[0]
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bm.normal_update()
    co = np.array([v.co[:] for v in bm.verts])
    vol = bm.calc_volume(signed=False)
    seen, shells = set(), 0
    bm.verts.ensure_lookup_table()
    for v in bm.verts:
        if v.index in seen:
            continue
        shells += 1
        stack = [v]
        seen.add(v.index)
        while stack:
            c = stack.pop()
            for e in c.link_edges:
                o = e.other_vert(c)
                if o.index not in seen:
                    seen.add(o.index)
                    stack.append(o)
    info = {"tris": len(bm.faces), "verts": len(bm.verts), "shells": shells, "volume_mm3": round(vol, 3),
            "min_mm": co.min(0).round(3).tolist(), "max_mm": co.max(0).round(3).tolist(),
            "dims_mm": (co.max(0) - co.min(0)).round(3).tolist()}
    return bm, co, info


bm_a, co_a, ia = load(ref)
bvh_a = BVHTree.FromBMesh(bm_a)
bm_b, co_b, ib = load(cand)
bvh_b = BVHTree.FromBMesh(bm_b)


def dist(co, bvh):
    d = np.array([bvh.find_nearest(p)[3] for p in co])
    return {"mean_mm": round(float(d.mean()), 5), "p99_mm": round(float(np.percentile(d, 99)), 5),
            "max_mm": round(float(d.max()), 5)}


res = {"reference": ia, "candidate": ib, "ref_to_cand": dist(co_a, bvh_b), "cand_to_ref": dist(co_b, bvh_a),
       "volume_rel_diff": round(abs(ib["volume_mm3"] - ia["volume_mm3"]) / ia["volume_mm3"], 6),
       "dims_diff_mm": [round(abs(x - y), 4) for x, y in zip(ia["dims_mm"], ib["dims_mm"])]}
with open(out, "w") as f:
    json.dump(res, f, indent=2)
print("[pipe] COMPARE " + json.dumps(res))
