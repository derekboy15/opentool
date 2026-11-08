"""Prepare stage: bake the FBX to mm, dump UV polygons per material and survey open loops / shells."""
import json
import os
import sys

import bmesh

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import boundary_loops, import_baked, load_cfg, log, out_path  # noqa: E402

cfg = load_cfg()
objs, info = import_baked(cfg)

def uv_area(pts):
    return abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts)))) / 2


uv_polys = {}
texel = {}  # per material: 3D surface area (mm^2) vs UV area (fraction of one tile) -> texel density
survey = []
for ob in objs:
    me = ob.data
    mat = ob["mat"]
    if mat and me.uv_layers:
        uv = me.uv_layers[0].data
        polys = [[tuple(uv[i].uv) for i in p.loop_indices] for p in me.polygons]
        uv_polys.setdefault(mat, []).extend(polys)
        t = texel.setdefault(mat, {"area_mm2": 0.0, "uv_area": 0.0})
        t["area_mm2"] += sum(p.area for p in me.polygons)
        t["uv_area"] += sum(uv_area(p) for p in polys)
    bm = bmesh.new()
    bm.from_mesh(me)
    loops = boundary_loops(bm)
    survey.append({"object": ob.name, "material": mat, "verts": len(bm.verts), "tris": sum(len(f.verts) - 2 for f in bm.faces),
                   "open_loops": [{"edges": len(e), "perimeter_mm": round(p, 3), "centre_mm": [round(x, 2) for x in c]}
                                  for e, p, c in sorted(loops, key=lambda l: -l[1])]})
    bm.free()
    log(f"{ob.name}: material={mat or '-'} tris={survey[-1]['tris']} open loops={len(loops)}")

for mat, polys in uv_polys.items():
    with open(out_path(cfg, f"uv_{mat}.json"), "w") as f:
        json.dump(polys, f)
for mat, t in texel.items():
    t["mm_per_tile"] = (t["area_mm2"] / max(t["uv_area"], 1e-9)) ** 0.5  # mm covered by one UV unit
    log(f"{mat}: {t['area_mm2']:.0f} mm^2 on {t['uv_area']:.3f} UV -> {t['mm_per_tile']:.1f} mm per UV tile")
info["texel"] = texel
info["objects"] = survey
with open(out_path(cfg, "prep.json"), "w") as f:
    json.dump(info, f, indent=2)
log("wrote prep.json and UV polygons for", ", ".join(uv_polys))
