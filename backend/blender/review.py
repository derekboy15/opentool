# SPDX-License-Identifier: GPL-3.0-or-later
"""Review renders (stage 13): numbered views of the finished STL at ONE scale, plus a flat-black silhouette.

cfg["review"]: resolution, views [[name, yaw_deg, elevation_deg]]; cfg["output_name"]; cfg["out_dir"].
Optional cfg["review_marks"]: [{"name", "z_mm", "colour"}] horizontal lines drawn on the front/side views
(e.g. the eye line used for scaling), in STL coordinates.
Writes <out>/review/<nn>_<view>.png and <out>/review/views.json (the scale in px per mm).
"""
import json
import math
import os
import sys

import bmesh
import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_cfg, log, out_path, stage  # noqa: E402

cfg = load_cfg()
R = cfg["review"]
res = R.get("resolution", 900)
stl = out_path(cfg, cfg["output_name"] + ".stl")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.stl_import(filepath=stl)
ob = bpy.context.selected_objects[0]
for p in ob.data.polygons:
    p.use_smooth = False
pts = [ob.matrix_world @ v.co for v in ob.data.vertices]
lo = Vector([min(p[i] for p in pts) for i in range(3)])
hi = Vector([max(p[i] for p in pts) for i in range(3)])
ctr = (lo + hi) / 2
size = hi - lo
span = max(size) * 1.12  # one ortho scale for every view: panels compare 1:1
sc = bpy.context.scene
sc.render.engine = "BLENDER_WORKBENCH"
sc.render.resolution_x = sc.render.resolution_y = res
sc.display.render_aa = "8"
sh = sc.display.shading
sh.light, sh.color_type, sh.show_cavity, sh.cavity_type = "STUDIO", "SINGLE", True, "BOTH"
sh.single_color = (0.62, 0.62, 0.64)
sh.show_shadows, sh.shadow_intensity = False, 0.3
wd = bpy.data.worlds.new("W")
wd.color = (0.96, 0.96, 0.96)
sc.world = wd
sc.view_settings.view_transform = "Standard"
cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
cam.data.type, cam.data.ortho_scale = "ORTHO", span
cam.data.clip_start, cam.data.clip_end = 0.1, span * 20
sc.collection.objects.link(cam)
sc.camera = cam

marks = cfg.get("review_marks") or []  # drawn by the sheet as 2D lines (rows found per view below)
from bpy_extras.object_utils import world_to_camera_view  # noqa: E402

os.makedirs(out_path(cfg, "review"), exist_ok=True)
views = []


def shoot(n, name, yaw, elev, silhouette=False):
    a, e = math.radians(yaw), math.radians(elev)
    d = Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)))
    cam.location = ctr + d * span * 6
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    if silhouette:
        sh.color_type, sh.light, sh.show_cavity = "SINGLE", "FLAT", False
        sh.single_color = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    rows = {}
    if abs(elev) < 20 and not silhouette:
        for m in marks:
            v = world_to_camera_view(sc, cam, Vector((ctr.x, ctr.y, m["z_mm"])))
            rows[m["name"]] = round((1 - v.y) * res, 1)
    path = out_path(cfg, "review", f"{n:02d}_{name}.png")
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    views.append({"n": n, "name": name, "yaw": yaw, "elev": elev, "file": os.path.basename(path), "mark_rows": rows})


n = 0
for name, yaw, elev in R["views"]:
    n += 1
    shoot(n, name, yaw, elev)
n += 1
shoot(n, "silhouette_front", 0, 5, silhouette=True)
info = {"views": views, "px_per_mm": round(res / span, 3), "dims_mm": [round(x, 2) for x in size],
        "marks": cfg.get("review_marks") or []}
with open(out_path(cfg, "review", "views.json"), "w") as f:
    json.dump(info, f, indent=1)
log(f"review: {len(views)} views at {info['px_per_mm']} px/mm")
stage("review", views=len(views))
