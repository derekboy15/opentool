# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared back end (DESIGN.md section 4): unit tasks and job stages, driven only by an export package.

Unit tasks (once per unit): ingest (1), scale (3), pose_select (4).        [plan (2) is plan.py]
Job stages (one print): pose (5), weapons (6), prepare (bake to mm, UV survey), relief (8, normal -> height),
build (7 cleanup + 8 displace + 10 thicken + 11 base, then 9 solid in its own process), check (12),
review (13), package (14).
Every Blender stage runs through the capped runner; every stage reads its inputs and writes new files (it never
overwrites its input). Workspace layout under <workspace>/<game>/:
  packages/<group>/<unit>/          the adapter's export package
  work/<group>/<unit>/{ingest,scale,poses,jobs/<loadout>_pose<N>}/
  output/<group>/<unit>/<unit>_<loadout>_pose<N>.stl, _sheet.png, _report.json
  units/<group>/<unit>.json         your own per-unit fixes (merged over the game profile's)
  state/, logs/
"""
import glob
import hashlib
import json
import math
import os
import shutil
import sys
import threading
import time
import traceback

from . import config as C
from . import contract, jobs, sheet
from .runner import Runner, RunError

BL = os.path.join(C.BACKEND, "blender")


class StageError(RuntimeError):
    pass


def rel(p, root):
    try:
        return os.path.relpath(p, root).replace("\\", "/")
    except ValueError:
        return os.path.basename(p)


class Pipeline:
    def __init__(self, settings, game_id, say=None):
        self.s = settings
        self.game = settings.game(game_id)
        self.profile = C.game_profile(self.game["profile"])
        self.gdir = C.game_dir(settings, game_id)
        self.q = jobs.Queue(self.gdir)
        self.say = say or (lambda m: print(m, flush=True))
        self.runner = Runner(settings, os.path.join(self.gdir, "logs", "runner"), say=lambda m: self.say(f"[runner] {m}"))
        self._pose_locks, self._guard = {}, threading.Lock()

    # ------------------------------------------------------------ paths
    def package_dir(self, group, unit):
        return os.path.join(self.gdir, "packages", group, unit)

    def unit_paths(self, group, unit):
        d = os.path.join(self.gdir, "work", group, unit)
        return {"dir": d, "ingest": os.path.join(d, "ingest"), "scale": os.path.join(d, "scale"),
                "poses": os.path.join(d, "poses"), "logs": os.path.join(self.gdir, "logs", "units", group, unit)}

    def job_paths(self, spec):
        u = self.unit_paths(spec["group"], spec["unit"])
        tag = f"{spec['tag']}_" if spec.get("tag") else ""
        name = f"{tag}{spec['loadout']}_pose{spec['pose']}"
        out = os.path.join(self.gdir, "output", spec["group"], spec["unit"]) if not spec.get("tag") else \
            os.path.join(self.gdir, "output", "_" + spec["tag"], spec["unit"])
        return {**u, "job": os.path.join(u["dir"], "jobs", name), "name": name,
                "job_logs": os.path.join(self.gdir, "logs", "jobs", spec["group"], spec["unit"], name), "output": out}

    def package(self, group, unit):
        p = os.path.join(self.package_dir(group, unit), "package.json")
        d = jobs.read_json(p)
        if d is None:
            raise StageError(f"no export package for {group}/{unit}: run `opentool export {self.game['id']} {unit}` first")
        return d

    # ------------------------------------------------------------ config
    def unit_cfg(self, spec, pkg, job=None, weapons=()):
        cfg = C.build(self.s, self.game, spec["class"], spec["group"], spec["unit"], weapons=weapons, job=job,
                      ws_game_dir=self.gdir)
        pdir = self.package_dir(spec["group"], spec["unit"])
        cfg["anim_fbx"] = os.path.join(pdir, pkg["model"])
        cfg["rig_map"] = {**pkg["rig_map"], **(cfg.get("rig_map") or {})}
        cfg["hidden"] = pkg.get("hidden", [])
        cfg["mesh_groups"] = pkg.get("mesh_groups", [])
        cfg["metres_per_unit"] = pkg["units"]["metres_per_unit"]
        cfg["clip_roles"] = {c["name"]: c["role"] for c in pkg["clips"]}
        return cfg

    def loadout(self, pkg, key):
        lo = next((l for l in pkg["loadouts"] if l["id"] == key), None)
        if lo is None:
            raise StageError(f"loadout {key!r} not in the package ({[l['id'] for l in pkg['loadouts']]})")
        return lo

    def show_groups(self, pkg, lo):
        groups = {g["name"] for g in pkg.get("mesh_groups", [])}
        shown = set(lo.get("mesh_groups", []))
        shown |= {w["mount"].split(":", 1)[1] for w in lo["weapons"] if w["mount"].startswith("mesh_group:")}
        return sorted(g for g in groups if g in shown)

    def weapons_for(self, cfg, pkg, lo, pdir):
        """joint/socket/bonemerge-mounted weapons of a loadout -> attach entries (the other mounts are mesh groups).
        Bonemerge (Source): the adapter wrote the weapon in its shared bone's frame and put an empty on the model's
        bone of that name (`mount_joint`), so it mounts like a joint."""
        byid = {w["id"]: w for w in pkg["weapons"]}
        out = []
        for w in lo["weapons"]:
            m = w["mount"]
            if not (m.startswith(("joint:", "socket:")) or m == "bonemerge"):
                continue
            wp = byid.get(w["weapon_id"])
            if not wp or not wp.get("file"):
                continue
            joint = wp.get("mount_joint") if m == "bonemerge" else m.split(":", 1)[1]
            if not joint:
                continue
            wcfg = (cfg.get("weapon_mounts") or {}).get(w["weapon_id"], {})
            out.append({"fbx": os.path.join(pdir, wp["file"]), "joint": joint, "name": w["weapon_id"],
                        "mat_key": wp.get("mat_key"), "hidden": wp.get("hidden", []), **wcfg})
        for w in [w for w in out if w.get("show_in_host")]:  # a part the host weapon already carries (bayonet)
            h = w["show_in_host"]
            host = next((x for x in out if x["name"] == h["host"]), None)
            if host is not None:
                host["hidden"] = [x for x in host["hidden"] if (x if isinstance(x, str) else x[-1]) != h["object"]]
                out.remove(w)
        return out

    def materials(self, cfg, pkg, pdir):
        mats = {}
        for t in pkg["textures"]:
            if t["role"] == "normal" and t.get("material"):
                mats[t["material"]] = {"normal": os.path.join(pdir, t["file"]), "encoding": t.get("encoding", "auto"),
                                       "green": t.get("green", "opengl")}
        return C.deep_merge(mats, {k: v for k, v in (cfg.get("materials") or {}).items() if k in mats})

    def adapt_voxel(self, cfg, scale):
        """Memory-safe solid stage: the voxel never so fine that the predicted remesh peak passes the cap."""
        G = cfg["geometry"]
        A = G.get("adaptive_voxel") or {}
        x, y, z = scale["rest_size_mm"]
        area = 2 * (x * y + y * z + x * z)
        G["surface_ratio"] = round(area / A.get("ref_area_mm2", 5806.0), 3)
        if not A.get("enabled"):
            return
        budget = self.s.mem_gb * A.get("cap_fraction", 0.8) - A.get("fixed_gb", 3.0)
        mtris_ref = A["ref_mtris"] * area / A["ref_area_mm2"]
        need = A["ref_voxel_mm"] * math.sqrt(max(mtris_ref * A["gb_per_mtri"], 1e-9) / max(budget, 0.5))
        step = A.get("round_to_mm", 0.005)
        need = math.ceil(need / step - 1e-9) * step
        v = G["voxel_mm"]
        if need > v:
            G["voxel_mm"] = round(need, 4)
            cfg["auto_thicken"]["cell_mm"] = max(cfg["auto_thicken"]["cell_mm"], round(4 * need, 3))
        pred = mtris_ref * (A["ref_voxel_mm"] / G["voxel_mm"]) ** 2
        G["voxel_note"] = (f"surface {area:.0f} mm2 ({G['surface_ratio']}x ref): voxel {G['voxel_mm']} mm, predicted remesh "
                           f"{pred:.1f} Mtris ~ {A['fixed_gb'] + pred * A['gb_per_mtri']:.1f} GB of the {self.s.mem_gb:g} GB cap")

    # ------------------------------------------------------------ Blender helper
    def blender(self, script, cfg, cfg_path, log_path, blend=None):
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        jobs.write_json(cfg_path, cfg)
        try:
            return self.runner.blender(os.path.join(BL, script), [cfg_path], blend=blend, log_path=log_path)
        except RunError as e:
            raise StageError(str(e)) from None

    # ------------------------------------------------------------ unit stages
    def st_ingest(self, task):
        sp = task.d["spec"]
        pdir = self.package_dir(sp["group"], sp["unit"])
        errs, warns, pkg = contract.check_package(pdir)
        if errs:
            raise StageError("package fails adapter-check: " + "; ".join(errs[:10]))
        p = self.unit_paths(sp["group"], sp["unit"])
        lo = self.loadout(pkg, pkg["default_loadout"])
        cfg = self.unit_cfg(sp, pkg)
        cfg.update({"out_dir": p["ingest"], "clips": pkg["clips"], "show_groups": self.show_groups(pkg, lo)})
        r = self.blender("ingest.py", cfg, os.path.join(p["ingest"], "config.json"), os.path.join(p["logs"], "ingest.log"))
        res = jobs.read_json(os.path.join(p["ingest"], "ingest.json"))
        return {"totals": res["totals"], "bones": res["bones"], "warnings": warns + res["warnings"],
                "roles_missing": [k for k, v in res["roles"].items() if v is None], "peak_gb": r["peak_mem_gb"]}

    def st_scale(self, task):
        sp = task.d["spec"]
        pkg = self.package(sp["group"], sp["unit"])
        p = self.unit_paths(sp["group"], sp["unit"])
        lo = self.loadout(pkg, pkg["default_loadout"])
        cfg = self.unit_cfg(sp, pkg)
        cfg.update({"out_dir": p["scale"], "show_groups": self.show_groups(pkg, lo)})
        os.makedirs(p["scale"], exist_ok=True)
        self.blender("unit_scale.py", cfg, os.path.join(p["scale"], "config.json"), os.path.join(p["logs"], "scale.log"))
        return jobs.read_json(os.path.join(p["scale"], "scale.json"))

    def st_pose_select(self, task):
        sp = task.d["spec"]
        pkg = self.package(sp["group"], sp["unit"])
        p = self.unit_paths(sp["group"], sp["unit"])
        pdir = self.package_dir(sp["group"], sp["unit"])
        cfg0 = self.unit_cfg(sp, pkg)
        PS = cfg0["pose_select"]
        factor = task.d["result"]["scale"]["factor"]
        groups = {}
        for lo in pkg["loadouts"]:
            gkey = self.pose_group(pkg, lo)
            if gkey in groups:
                continue
            excl_roles = set(PS.get("exclude_roles", []))
            excl = [e.lower() for e in PS.get("clip_exclude", [])]
            clips = [c["name"] for c in pkg["clips"] if c["role"] not in excl_roles
                     and not any(e in c["name"].lower() for e in excl)]
            cfg = dict(cfg0, clips=clips, weapons=self.weapons_for(cfg0, pkg, lo, pdir), pose_count=cfg0["pose"].get("count", 4),
                       feet_enabled=cfg0["pose"]["feet"].get("enabled", True), show_groups=self.show_groups(pkg, lo),
                       pinned=(cfg0.get("poses") or []) if lo["id"] == pkg["default_loadout"] else [],
                       out_dir=os.path.join(p["poses"], f"select_{gkey}"))
            cfg["scale"] = dict(cfg["scale"], factor=factor, mode="factor")
            self.blender("pose_select.py", cfg, os.path.join(cfg["out_dir"], "config.json"),
                         os.path.join(p["logs"], f"pose_select_{gkey}.log"))
            groups[gkey] = jobs.read_json(os.path.join(cfg["out_dir"], "poses.json"))["poses"]
        return {"groups": groups}

    def pose_group(self, pkg, lo):
        """Loadouts that show the same mesh groups share their poses (and the pose stage's output)."""
        g = self.show_groups(pkg, lo)
        return "default" if not g else "g" + hashlib.md5("|".join(g).encode()).hexdigest()[:6]

    UNIT_FUNCS = {"ingest": "st_ingest", "scale": "st_scale", "pose_select": "st_pose_select"}

    # ------------------------------------------------------------ job config
    def job_config(self, spec, utask):
        pkg = self.package(spec["group"], spec["unit"])
        pdir = self.package_dir(spec["group"], spec["unit"])
        p = self.job_paths(spec)
        lo = self.loadout(pkg, spec["loadout"])
        wnames = [w["weapon_id"] for w in lo["weapons"]]
        cfg = self.unit_cfg(spec, pkg, job=spec.get("overrides"), weapons=wnames)
        scale = utask.d["result"]["scale"]
        cfg["scale"] = dict(cfg["scale"], factor=scale["factor"], mode="factor")
        cfg["scale_info"] = {k: scale.get(k) for k in ("rule", "why", "eye", "rest_height_mm")}
        self.adapt_voxel(cfg, scale)
        weapons = self.weapons_for(cfg, pkg, lo, pdir)
        cfg["materials"] = self.materials(cfg, pkg, pdir)
        cfg["show_groups"] = self.show_groups(pkg, lo)
        cfg["pose"] = C.deep_merge(cfg["pose"], {"clip": spec["clip"], "frame": spec["frame"], "weapons": weapons})
        cfg["name"] = cfg["output_name"] = f"{spec['unit']}_{spec['loadout']}_pose{spec['pose']}"
        cfg["out_dir"] = p["job"]
        gkey = self.pose_group(pkg, lo)
        cfg["pose_dir"] = os.path.join(p["poses"], gkey, f"{spec['clip']}_f{spec['frame']}" + (f"_{spec['tag']}" if spec.get("tag") else ""))
        cfg["pose_blend"] = os.path.join(cfg["pose_dir"], "pose.blend")
        cfg["fbx"] = os.path.join(p["job"], "posed.blend")
        cfg["job_id"] = spec["id"]
        return cfg, pkg, p

    # ------------------------------------------------------------ job stages
    def st_pose(self, job, cfg, p):
        os.makedirs(cfg["pose_dir"], exist_ok=True)
        done = os.path.join(cfg["pose_dir"], "pose.done")
        with self._guard:
            lock = self._pose_locks.setdefault(os.path.normcase(cfg["pose_dir"]), threading.Lock())
        with lock:  # shared by every loadout that uses this frame and these mesh groups
            if not os.path.exists(done):
                self.blender("pose.py", dict(cfg, out_dir=cfg["pose_dir"]), os.path.join(cfg["pose_dir"], "config.json"),
                             os.path.join(p["job_logs"], "pose.log"))
                open(done, "w").close()
        os.makedirs(p["job"], exist_ok=True)
        shutil.copy2(os.path.join(cfg["pose_dir"], "pose.json"), os.path.join(p["job"], "pose.json"))
        return jobs.read_json(os.path.join(p["job"], "pose.json"))["feet"]

    def st_weapons(self, job, cfg, p):
        self.blender("attach.py", cfg, os.path.join(p["job"], "resolved_config.json"), os.path.join(p["job_logs"], "weapons.log"))
        return {"weapons": [w["name"] for w in cfg["pose"]["weapons"]], "mesh_groups": cfg["show_groups"],
                "geometry_sig": jobs.read_json(os.path.join(p["job"], "pose.json")).get("geometry_sig")}

    def st_prepare(self, job, cfg, p):
        self.blender("prepare.py", cfg, os.path.join(p["job"], "resolved_config.json"), os.path.join(p["job_logs"], "prepare.log"))
        prep = jobs.read_json(os.path.join(p["job"], "prep.json"))
        return {"materials": sorted(prep["texel"]), "unmatched": [o["object"] for o in prep["objects"] if not o["material"]]}

    def st_relief(self, job, cfg, p):
        prep = jobs.read_json(os.path.join(p["job"], "prep.json"))
        skip = set((cfg.get("relief") or {}).get("skip_materials", []))
        mats = {k: v for k, v in cfg["materials"].items() if k in prep["texel"] and v.get("normal") and k not in skip}
        hc = dict(cfg, materials=mats)
        cp = os.path.join(p["job"], "relief_config.json")
        jobs.write_json(cp, hc)
        if mats:
            try:  # FFT integration: a heavy step, so it runs capped like Blender
                self.runner.python([os.path.join(C.BACKEND, "heights.py"), cp], log_path=os.path.join(p["job_logs"], "relief.log"),
                                   name="relief")
            except RunError as e:
                raise StageError(str(e)) from None
        return {"height_maps": sorted(mats), "skipped": sorted(skip & set(cfg["materials"]))}

    def st_build(self, job, cfg, p):
        retry = None
        for attempt in (0, 1):
            try:
                r1 = self.blender("build.py", cfg, os.path.join(p["job"], "resolved_config.json"), os.path.join(p["job_logs"], "build.log"))
                r2 = self.blender("finalize.py", cfg, os.path.join(p["job"], "resolved_config.json"),
                                  os.path.join(p["job_logs"], "finalize.log"), blend=os.path.join(p["job"], "union_input.blend"))
                break
            except StageError as e:
                if attempt or "memory cap hit" not in str(e):
                    raise
                G = cfg["geometry"]  # the cap held: fix the input (coarser voxel), never the cap
                old = G["voxel_mm"]
                G["voxel_mm"] = round(old * G.get("voxel_retry_factor", 1.2), 4)
                cfg["auto_thicken"]["cell_mm"] = max(cfg["auto_thicken"]["cell_mm"], round(4 * G["voxel_mm"], 3))
                retry = {"memory_cap_hit_at_voxel_mm": old, "retried_at_voxel_mm": G["voxel_mm"]}
                self.say(f"  memory cap hit at voxel {old} mm: retrying once at {G['voxel_mm']} mm")
        b = jobs.read_json(os.path.join(p["job"], "build.json"))
        if not cfg.get("keep_intermediates"):
            for f in ("union_input.blend", "union_input.blend1"):
                fp = os.path.join(p["job"], f)
                if os.path.exists(fp):
                    os.remove(fp)
        return {"substages": [m["stage"] for m in r1["markers"] + r2["markers"]], "shells": b["shells"],
                "non_manifold": b["non_manifold_edges_reimported"], "dims_mm": b["dims_mm"],
                "peak_gb": max(r1["peak_mem_gb"], r2["peak_mem_gb"]), "peak_gb_build": r1["peak_mem_gb"],
                "peak_gb_finalize": r2["peak_mem_gb"], "voxel_mm": cfg["geometry"]["voxel_mm"],
                "voxel_note": cfg["geometry"].get("voxel_note"), "voxel_retry": retry}

    def flags_for(self, cfg, qa, build):
        F, out = cfg["flag"], []
        vols = qa.get("shell_volumes_mm3") or []
        specks = sum(1 for v in vols if v < F.get("min_shell_mm3", 0.05))
        shells = qa["shells"] - specks
        if shells > F["max_shells"]:
            out.append(f"{shells} shells (expected {F['max_shells']})")
        if qa["non_manifold_edges"] > F["max_non_manifold_edges"]:
            out.append(f"{qa['non_manifold_edges']} non-manifold edges")
        if qa["boundary_edges"] > F["max_boundary_edges"]:
            out.append(f"{qa['boundary_edges']} open (boundary) edges")
        thin = [t for t in qa["thin_parts"] if 2 * t["extent_mm"] >= F["thin_part_min_extent_mm"]
                and t.get("protrusion_mm", 1e9) >= F.get("thin_part_min_protrusion_mm", 0.0)]
        if thin:
            out.append(f"{len(thin)} thin part(s) under {qa['min_thickness_mm']} mm reaching out up to "
                       f"{max(t.get('protrusion_mm', 0) for t in thin):.2f} mm, largest {max(2 * t['extent_mm'] for t in thin):.2f} mm across")
        lim = F["max_overhang_region_mm2"] * max(1.0, cfg["geometry"].get("surface_ratio", 1.0))
        big = [r for r in qa["overhang_regions"] if r["area_mm2"] > lim]
        if big:
            out.append(f"{len(big)} unsupported overhang region(s) over {lim:.0f} mm2 (largest {big[0]['area_mm2']})")
        if F.get("fit_build_volume"):
            bv = cfg["printer"]["build_volume_mm"]
            if any(d > v for d, v in zip(sorted(qa["dims_mm"][:2]) + [qa["dims_mm"][2]], sorted(bv[:2]) + [bv[2]])):
                out.append(f"does not fit the {cfg['printer']['name']} build volume {bv} (size {qa['dims_mm']})")
        if build.get("footprint_fits_base") is False:
            out.append("footprint does not fit the base top")
        dome = build.get("base_dome")
        if dome and dome.get("largest_sole_gap_mm", 0) > cfg.get("contact", {}).get("band_mm", 0.5):
            out.append(f"a planted sole is {dome['largest_sole_gap_mm']} mm above the base surface")
        if cfg.get("base_size_is_fallback") and cfg.get("base"):
            out.append(f"base size {cfg.get('base_size_used')} is the class fallback (no unit/game base size given)")
        return out

    def st_check(self, job, cfg, p):
        self.blender("qa.py", dict(cfg, qa_mode="check"), os.path.join(p["job"], "qa_config.json"), os.path.join(p["job_logs"], "check.log"))
        qa = jobs.read_json(os.path.join(p["job"], "qa.json"))
        build = jobs.read_json(os.path.join(p["job"], "build.json"))
        flags = self.flags_for(cfg, qa, build)
        vr = (job.d["result"].get("build") or {}).get("voxel_retry")
        if vr:
            flags.append(f"memory cap hit at voxel {vr['memory_cap_hit_at_voxel_mm']} mm: built at {vr['retried_at_voxel_mm']} mm")
        job.d["flags"] = flags
        return {"flags": flags, "shells": qa["shells"], "non_manifold": qa["non_manifold_edges"],
                "boundary_edges": qa["boundary_edges"], "thin_parts": len(qa["thin_parts"]),
                "overhang_area_mm2": qa["overhang_area_mm2"], "dims_mm": qa["dims_mm"]}

    def st_review(self, job, cfg, p):
        self.blender("qa.py", dict(cfg, qa_mode="render"), os.path.join(p["job"], "qa_config.json"), os.path.join(p["job_logs"], "render.log"))
        build = jobs.read_json(os.path.join(p["job"], "build.json"))
        marks = []
        eye = (cfg.get("scale_info") or {}).get("eye") or {}
        if eye.get("raw") is not None and (cfg.get("scale_info") or {}).get("rule") == "eye_height":
            # the eye line the scale was set from, in STL coordinates
            off = build["stl_offset_mm"]
            sole = (jobs.read_json(os.path.join(self.unit_paths(job.d["spec"]["group"], job.d["spec"]["unit"])["scale"], "scale.json")) or {}).get("sole_raw", 0)
            z = (eye["raw"] - sole) * cfg["scale"]["factor"] + off[2]
            marks.append({"name": "eye_line", "z_mm": round(z, 3), "colour": [0.85, 0.1, 0.1]})
        rc = dict(cfg, review_marks=marks)
        self.blender("review.py", rc, os.path.join(p["job"], "review_config.json"), os.path.join(p["job_logs"], "review.log"))
        qa = jobs.read_json(os.path.join(p["job"], "qa.json"))
        sp = job.d["spec"]
        lines = self.summary_lines(cfg, qa, build, job.d.get("flags", []))
        out = os.path.join(p["job"], "sheet.png")
        sheet.compose(p["job"], out, f"{sp['unit']}  |  {sp['loadout']}  |  pose {sp['pose']} ({cfg['pose']['clip']} f{cfg['pose']['frame']})",
                      lines, changed=sp.get("changed"))
        return {"sheet": out}

    def summary_lines(self, cfg, qa, build, flags):
        si = cfg.get("scale_info") or {}
        eye = si.get("eye") or {}
        L = [f"Scale: {si.get('rule')}, {cfg['scale']['factor']:.4f} mm per model unit" + (
            f"; eye line {eye.get('height_mm')} mm above the soles (red line), {'ESTIMATED from the head mesh' if eye.get('estimated') else eye.get('how')}"
            if si.get("rule") == "eye_height" and eye.get("height_mm") is not None else ""),
             f"Size {qa['dims_mm'][0]:.1f} x {qa['dims_mm'][1]:.1f} x {qa['dims_mm'][2]:.1f} mm; base {cfg.get('base_size_used')} "
             f"({(cfg.get('base') or {}).get('top', 'none') if cfg.get('base') else 'none'}); voxel {cfg['geometry']['voxel_mm']} mm; "
             f"{build.get('tris_final', '?')} tris",
             f"Checks: shells {qa['shells']}, non-manifold {qa['non_manifold_edges']}, open edges {qa['boundary_edges']}, "
             f"thin parts {len(qa['thin_parts'])}, overhang {qa['overhang_area_mm2']} mm2",
             ]
        if flags:
            L += [f"FLAG: {f}" for f in flags]
        else:
            L.append("All checks pass.")
        return L

    def st_package(self, job, cfg, p):
        out = p["output"]
        os.makedirs(out, exist_ok=True)
        name = cfg["output_name"]
        shutil.copy2(os.path.join(p["job"], name + ".stl"), os.path.join(out, name + ".stl"))
        if os.path.exists(os.path.join(p["job"], "sheet.png")):
            shutil.copy2(os.path.join(p["job"], "sheet.png"), os.path.join(out, name + "_sheet.png"))
        pkg = self.package(job.d["spec"]["group"], job.d["spec"]["unit"])
        build = jobs.read_json(os.path.join(p["job"], "build.json"))
        report = {"job": job.id, "stl": name + ".stl", "flags": job.d["flags"],
                  "provenance": pkg["provenance"], "credits": pkg["credits"],
                  "personal_use_only": bool(pkg["credits"]) or bool(pkg["provenance"].get("mod")),
                  "adapter": pkg["adapter"], "reader": pkg["reader"],
                  "scale": cfg.get("scale_info"), "factor": cfg["scale"]["factor"],
                  "qa": jobs.read_json(os.path.join(p["job"], "qa.json")),
                  "build": {k: v for k, v in build.items() if k != "fixes"}, "fixes": build["fixes"],
                  "pose": jobs.read_json(os.path.join(p["job"], "pose.json"))}
        jobs.write_json(os.path.join(out, name + "_report.json"), report)
        if not cfg.get("keep_intermediates"):
            for pat in ("posed.blend", "posed.blend1", "height_*.png", "normal_fixed_*.png", "uv_*.json"):
                for f in glob.glob(os.path.join(p["job"], pat)):
                    os.remove(f)
        return {"output": rel(os.path.join(out, name + ".stl"), self.gdir)}

    JOB_FUNCS = {"pose": "st_pose", "weapons": "st_weapons", "prepare": "st_prepare", "relief": "st_relief",
                 "build": "st_build", "check": "st_check", "review": "st_review", "package": "st_package"}

    # ------------------------------------------------------------ queue
    def ensure_unit(self, uid, retry_failed=False):
        task = self.q.record("unit", uid)
        task.recover()
        if task.status == "failed" and not retry_failed:
            raise StageError(f"unit task {uid} failed earlier: {task.d.get('error')} (use --retry-failed)")
        for s in jobs.UNIT_STAGES:
            if task.d["stages"][s]["status"] == "done":
                continue
            task.start_stage(s)
            t0 = time.time()
            self.say(f"  unit {uid}: {s}")
            try:
                res = getattr(self, self.UNIT_FUNCS[s])(task)
            except Exception as e:
                task.end_stage(s, ok=False, seconds=round(time.time() - t0), error=e)
                task.d["status"], task.d["error"] = "failed", str(e)[:2000]
                task.d["attempts"] += 1
                task.save()
                raise
            task.d["result"][s] = res
            task.end_stage(s, ok=True, seconds=round(time.time() - t0))
        task.d["status"] = "done"
        task.d.pop("error", None)
        task.save()
        return task

    def run_job(self, jid, retry_failed=False, stop_after=None):
        job = self.q.record("job", jid)
        job.recover()
        spec = job.d["spec"]
        if job.status in ("done", "flagged", "duplicate", "obsolete") or (job.status == "failed" and not retry_failed):
            return job.status
        try:
            utask = self.ensure_unit(spec["unit_task"], retry_failed)
            if spec.get("clip") is None:
                pkg = self.package(spec["group"], spec["unit"])
                gkey = self.pose_group(pkg, self.loadout(pkg, spec["loadout"]))
                poses = utask.d["result"]["pose_select"]["groups"].get(gkey) or []
                if spec["pose"] > len(poses):
                    raise StageError(f"unit has {len(poses)} poses for this loadout, job wants pose {spec['pose']}")
                pz = poses[spec["pose"] - 1]
                spec = dict(spec, clip=pz["clip"], frame=pz["frame"],
                            overrides=C.deep_merge(spec.get("overrides") or {}, pz.get("overrides") or {}))
            cfg, pkg, p = self.job_config(spec, utask)
        except Exception as e:
            job.d["status"], job.d["error"] = "failed", f"setup: {e}"
            job.d["attempts"] += 1
            job.save()
            self.q.event("job_failed", id=jid, stage="setup", error=str(e)[:300])
            self.say(f"  job {jid}: setup failed: {e}")
            return "failed"
        job.d.update(status="running", resolved={"clip": spec["clip"], "frame": spec["frame"], "out_dir": rel(p["job"], self.gdir),
                                                 "voxel_mm": cfg["geometry"]["voxel_mm"]})
        job.d["attempts"] += 1
        job.save()
        for s in jobs.JOB_STAGES:
            if job.d["stages"][s]["status"] == "done":
                continue
            if stop_after and jobs.JOB_STAGES.index(s) > jobs.JOB_STAGES.index(stop_after):
                break
            job.start_stage(s, log=rel(p["job_logs"], self.gdir))
            t0 = time.time()
            self.say(f"  job {jid}: {s}")
            try:
                res = getattr(self, self.JOB_FUNCS[s])(job, cfg, p)
            except Exception as e:
                job.end_stage(s, ok=False, seconds=round(time.time() - t0), error=f"{e}\n{traceback.format_exc()[-1500:]}")
                job.d["status"], job.d["error"] = "failed", f"{s}: {str(e)[:1500]}"
                job.save()
                self.say(f"  job {jid}: {s} FAILED: {str(e)[:800]}")
                return "failed"
            job.d["result"][s] = res
            job.end_stage(s, ok=True, seconds=round(time.time() - t0))
            if s == "weapons" and res.get("geometry_sig"):
                dup = self.duplicate_of(job, res["geometry_sig"])
                if dup:
                    job.d["status"], job.d["duplicate_of"] = "duplicate", dup
                    job.save()
                    return "duplicate"
        if job.next_stage() is None:
            job.d["status"] = "flagged" if job.d["flags"] else "done"
            job.d.pop("error", None)
        else:
            job.d["status"] = "pending"
        job.save()
        return job.d["status"]

    def duplicate_of(self, job, sig):
        job.save()
        s = job.d["spec"]
        for d in self.q.all("job"):
            o = d["spec"]
            if (d["id"] != job.d["id"] and d["status"] not in ("failed", "duplicate", "obsolete")
                    and (o["unit_task"], o["pose"], o.get("tag")) == (s["unit_task"], s["pose"], s.get("tag"))
                    and d["stages"]["weapons"]["status"] == "done"
                    and (d.get("result") or {}).get("weapons", {}).get("geometry_sig") == sig):
                return d["id"]
        return None

    def run(self, only=None, retry_failed=False, workers=1, max_jobs=None, stop_after=None):
        with self.q.lock():
            queue = [d["id"] for d in self.q.all("job") if (only is None or only(d))
                     and d["status"] not in ("done", "flagged", "obsolete", "duplicate")
                     and (d["status"] != "failed" or retry_failed)]
            if max_jobs:
                queue = queue[:max_jobs]
            results, ql = {}, threading.Lock()

            def worker():
                while True:
                    with ql:
                        if not queue:
                            return
                        jid = queue.pop(0)
                    results[jid] = self.run_job(jid, retry_failed, stop_after)
            ts = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(workers, self.s.max_jobs)))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            return results
