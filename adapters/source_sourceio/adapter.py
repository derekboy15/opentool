# SPDX-License-Identifier: GPL-3.0-or-later
"""Source 1 adapter (Left 4 Dead 2, Half-Life 2, Team Fortress 2 era .mdl v44-v49) on SourceIO (MIT).

Reader the user downloads (README, "Readers"): the SourceIO Blender add-on release zip; [readers] sourceio = the zip
or the unpacked add-on folder. It runs inside Blender through the capped runner, enabled for that run only, and
reads the game's VPKs in place (nothing is unpacked). VPKs are not encrypted; the adapter only reads.

Game profile "source": game_dir (the folder holding gameinfo.txt, relative to the install), unit_folders (model
path prefixes to scan), skip_model (regex: animation, gesture, gib and part models), group_from (regex with a
"group" capture), units [hand-made entries: id, name, group, class, model, loadouts, default_loadout,
clip_activity (regex on the game's activity names), credits], weapons {weapon_id: {model, bone}}, attachments
(names kept as mount points), eye_attachment, activity_roles [[regex on ACT_*, role]], max_clips.

list:   VPK directory names (plain reads) -> candidate .mdl files -> one Blender scan for their skeletons and
        sequences (cached in raw/scan.json): profile units first (high confidence), every other skinned model in
        the unit folders as a low-confidence unit with no weapon.
export: blender_export.py (capped runner) -> package/model/<unit>.glb + weapons/*.glb + textures + package.json.
Clips are the model's sequences (with $includemodel files) that are not deltas and play one animation; roles
come from each sequence's activity (ACT_IDLE_RIFLE -> idle), which is cleaner than guessing from names.
"""
import json
import os
import re
import shutil
import zipfile

from opentool import CONTRACT_VERSION

from . import vpk

ADAPTER = {"name": "source_sourceio", "version": "0.1.0"}
HERE = os.path.dirname(os.path.abspath(__file__))
ROLE_ORDER = ["idle", "aim", "shoot", "melee", "move", "other", "die"]


def addon_parent(ctx):
    """Folder holding the SourceIO add-on folder. A release .zip (top folder SourceIO-master/) is extracted once
    into the workspace as readers/addons_<zip stem>/SourceIO/ (the add-on imports itself as `SourceIO`)."""
    p = ctx.reader("sourceio")
    if p.lower().endswith(".zip"):
        dest = os.path.join(ctx.gdir, "readers", "addons_" + os.path.splitext(os.path.basename(p))[0])
        if not os.path.exists(os.path.join(dest, "SourceIO", "__init__.py")):
            tmp = dest + ".tmp"
            if os.path.exists(tmp):
                shutil.rmtree(tmp)
            with zipfile.ZipFile(p) as z:
                z.extractall(tmp)
            tops = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
            src = tmp if os.path.exists(os.path.join(tmp, "__init__.py")) else os.path.join(tmp, tops[0]) if len(tops) == 1 else None
            if src is None or not os.path.exists(os.path.join(src, "__init__.py")):
                raise FileNotFoundError(f"[readers] sourceio = {p}: no add-on (__init__.py) in the zip")
            os.makedirs(dest, exist_ok=True)
            shutil.move(src, os.path.join(dest, "SourceIO"))
            shutil.rmtree(tmp, ignore_errors=True)
        p = dest
    if os.path.exists(os.path.join(p, "SourceIO", "__init__.py")):
        return p
    if os.path.exists(os.path.join(p, "__init__.py")) and os.path.basename(os.path.normpath(p)) == "SourceIO":
        return os.path.dirname(os.path.normpath(p))
    raise FileNotFoundError(f"[readers] sourceio = {p}: no SourceIO/__init__.py there (folder must be named SourceIO)")


def game_dir(ctx):
    d = os.path.join(ctx.game["install"], ctx.profile["source"]["game_dir"])
    if not os.path.exists(os.path.join(d, "gameinfo.txt")):
        raise SystemExit(f"no gameinfo.txt in {d}: is [games.{ctx.game['id']}] install the game folder?")
    return d


def run_blender(ctx, script, cfg, name, log):
    work = os.path.join(ctx.gdir, "work", "_runs")
    os.makedirs(work, exist_ok=True)
    cp = os.path.join(work, f"{name}_config.json")
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    return ctx.runner.blender(os.path.join(HERE, script), [cp], name=name, log_path=log)


def scan(ctx, refresh=False):
    S = ctx.profile["source"]
    raw = os.path.join(ctx.gdir, "raw")
    os.makedirs(raw, exist_ok=True)
    p = os.path.join(raw, "scan.json")
    if refresh or not os.path.exists(p):
        gd = game_dir(ctx)
        files = vpk.list_files(gd, tuple(S["unit_folders"]))
        skip = S.get("skip_model", "$^")
        models = sorted(f for f in files if not re.search(skip, f, re.I))
        ctx.say(f"  {len(files)} models in the unit folders, {len(models)} candidates; reading their skeletons and sequences")
        run_blender(ctx, "blender_scan.py", {"addon_parent": addon_parent(ctx), "game_dir": gd, "models": models,
                                             "out": p + ".part"}, "source_scan", os.path.join(ctx.gdir, "logs", "source_scan.log"))
        d = json.load(open(p + ".part", encoding="utf-8"))
        d["sources"] = {m: files[m] for m in d["models"]}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.remove(p + ".part")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def act_role(seq, rules):
    for rx, role in rules:
        if re.search(rx, seq["activity"] or seq["name"], re.I):
            return role
    return "other"


def clips_for(ctx, m, activity=None):
    """Sequences that hold a pose on their own: not deltas (layers), one animation, an activity."""
    S = ctx.profile["source"]
    rules = S.get("activity_roles") or [[".*", "other"]]
    out = []
    for s in m["sequences"]:
        if s["delta"] or s["blends"] != 1 or not s["anim"] or not s["activity"] or s["frames"] < 1:
            continue
        if activity and not re.search(activity, s["activity"], re.I):
            continue
        out.append({"name": s["name"], "role": act_role(s, rules), "activity": s["activity"], "anim": s["anim"],
                    "fps": s["fps"], "frames": [0, max(0, s["frames"] - 1)]})
    return out


def list_units(ctx):
    S = ctx.profile["source"]
    sc = scan(ctx)
    models = sc["models"]
    units, used = [], set()
    for pu in S.get("units", []):
        m = models.get(pu["model"].lower())
        if m is None:
            ctx.say(f"  profile unit {pu['id']}: {pu['model']} not found, skipped")
            continue
        used.add(pu["model"].lower())
        clips = clips_for(ctx, m, pu.get("clip_activity"))
        units.append({"id": pu["id"], "name": pu["name"], "group": pu["group"], "class_hint": pu.get("class", "infantry"),
                      "confidence": "high", "models": [{"id": pu["model"], "bones": len(m["bones"]),
                                                        "bodygroups": len(m["bodygroups"])}],
                      "loadouts": pu["loadouts"], "default_loadout": pu.get("default_loadout", pu["loadouts"][0]["id"]),
                      "clips": [{k: c[k] for k in ("name", "role")} for c in clips],
                      "provenance": {"archive": sc["sources"].get(pu["model"].lower()), "path": pu["model"],
                                     "include": m["include"], "source": "profile"},
                      "credits": pu.get("credits", []), "model": pu["model"]})
    grp = S.get("group_from", r"models/(?P<group>[^/]+)/")
    for path, m in sorted(models.items()):
        if path in used or len(m["bones"]) < S.get("min_bones", 10):
            continue
        clips = clips_for(ctx, m)
        g = re.search(grp, path)
        name = os.path.splitext(os.path.basename(path))[0]
        units.append({"id": path, "name": name, "group": g.group("group") if g else "misc",
                      "class_hint": "infantry" if clips else "other", "confidence": "low",
                      "models": [{"id": path, "bones": len(m["bones"]), "bodygroups": len(m["bodygroups"])}],
                      "loadouts": [{"id": "default", "weapons": []}], "default_loadout": "default",
                      "clips": [{k: c[k] for k in ("name", "role")} for c in clips],
                      "provenance": {"archive": sc["sources"].get(path), "path": path, "include": m["include"],
                                     "source": "model scan (weapons live in game scripts: loadout unknown)"},
                      "credits": [], "model": path})
    if sc.get("errors"):
        ctx.say(f"  {len(sc['errors'])} models could not be read (raw/scan.json lists them)")
    ctx.say(f"  {len(units)} units ({sum(u['confidence'] == 'high' for u in units)} from the profile, the rest model-only)")
    return {"contract": CONTRACT_VERSION, "game": ctx.game["id"], "adapter": ADAPTER, "units": units}


def shown(mesh_groups, lo):
    """Mesh groups a loadout shows: per multi-choice body group, the loadout's body_groups {name: choice}, else 0."""
    pick = lo.get("body_groups", {})
    return [g["name"] for g in mesh_groups if g["choice"] == pick.get(g["body_group"], 0)]


def export_unit(ctx, u, out):
    S = ctx.profile["source"]
    sc = scan(ctx)
    m = sc["models"][u["model"].lower()]
    pu = next((x for x in S.get("units", []) if x["id"] == u["id"]), {})
    clips = clips_for(ctx, m, pu.get("clip_activity"))
    max_clips = S.get("max_clips", 40)
    if len(clips) > max_clips:
        ctx.say(f"  {len(clips)} clips; keeping {max_clips} (profile max_clips): idle/aim/shoot/melee/move first")
        pri = {r: i for i, r in enumerate(ROLE_ORDER)}
        clips = sorted(clips, key=lambda c: (pri.get(c["role"], 9), c["name"]))[:max_clips]
    wdefs = S.get("weapons", {})
    weapons = {}
    for lo in u["loadouts"]:
        for w in lo["weapons"]:
            d = wdefs.get(w["weapon_id"])
            if d is None and w["mount"] == "bonemerge":
                raise SystemExit(f"loadout {lo['id']}: weapon {w['weapon_id']} has no entry in the profile's source.weapons")
            if d is not None:
                weapons[w["weapon_id"]] = {"id": w["weapon_id"], "model": d["model"], "bone": d.get("bone", "ValveBiped.weapon_bone")}
    work = os.path.join(ctx.gdir, "work", u["group"], u["name"])
    logs = os.path.join(ctx.gdir, "logs", "units", u["group"], u["name"])
    os.makedirs(work, exist_ok=True)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    cfg = {"addon_parent": addon_parent(ctx), "game_dir": game_dir(ctx), "model": u["model"], "stem": u["name"],
           "out_dir": out, "clips": [{"name": c["name"], "anim": c["anim"]} for c in clips],
           "weapons": list(weapons.values()), "attachments": S.get("attachments", []),
           "eye_attachment": S.get("eye_attachment", "eyes")}
    ctx.say(f"  importing {u['model']} with SourceIO ({len(clips)} clips, {len(weapons)} weapons)")
    run_blender(ctx, "blender_export.py", cfg, "source_export", os.path.join(logs, "export.log"))
    ex = json.load(open(os.path.join(out, "export.json"), encoding="utf-8"))
    os.replace(os.path.join(out, "export.json"), os.path.join(work, "export.json"))

    P = ctx.profile
    rig = dict(P.get("rig_map") or {})
    if ex.get("eye_bone"):
        rig["eye"] = ex["eye_bone"]
    bones = set(ex["bone_names"])
    rig_missing = sorted(k for k, v in rig.items() if not k.startswith("_") and v not in bones)
    rig = {k: v for k, v in rig.items() if not k.startswith("_") and v in bones}
    warnings = list(ex["warnings"])
    if rig_missing:
        warnings.append(f"rig roles not on this rig: {rig_missing}")
    green = S.get("normal_green", "directx")
    textures = []
    for t in ex["textures"]:
        e = {"file": t["file"], "role": t["role"], "material": t["material"]}
        if t["role"] == "normal":
            e.update(encoding=S.get("normal_encoding", "auto"), green=green)
        textures.append(e)
    got = {c["name"]: c for c in ex["clips"]}
    pclips = [{"name": c["name"], "role": c["role"], "frames": got[c["name"]]["frames"], "fps": c["fps"],
               "activity": c["activity"]} for c in clips if c["name"] in got]
    wfiles = {w["id"]: w for w in ex["weapons"]}
    groups = [{"name": g["name"], "role": g["role"]} for g in ex["mesh_groups"]]
    pkg = {"contract": CONTRACT_VERSION, "adapter": ADAPTER,
           "reader": {"name": "SourceIO", "version": S.get("reader_version", "SourceIO 5.5.4")},
           "game": P.get("name", ctx.game["id"]),
           "unit": {"id": u["id"], "name": u["name"], "group": u["group"], "class_hint": u["class_hint"],
                    "class_confidence": u["confidence"]},
           "model": ex["model"], "format": "glb",
           "units": {"metres_per_unit": 1.0, "source_metres_per_unit": ex["scale_m_per_unit"]},
           "up_axis": "Z", "handedness": "right", "bind_pose_ok": True,
           "rig_map": rig, "clips": pclips, "mesh_groups": groups, "hidden": [],
           "weapons": [{"id": wid, "file": w["file"], "tris": w["tris"], "size_m": w["size_m"],
                        "mount_joint": w["mount_joint"], "bone": w["bone"], "source": weapons[wid]["model"]}
                       for wid, w in wfiles.items()],
           "loadouts": [dict({"id": lo["id"], "weapons": lo["weapons"]},
                             **({"mesh_groups": shown(ex["mesh_groups"], lo)} if groups else {}))
                        for lo in u["loadouts"]],
           "default_loadout": u["default_loadout"], "textures": textures,
           "provenance": dict(u["provenance"], extract_stats=ex["stats"], sockets=ex["sockets"],
                              game_dir=S["game_dir"]),
           "credits": u["credits"], "warnings": warnings}
    with open(os.path.join(out, "package.json"), "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=1)
    ctx.say(f"  extraction: {json.dumps(ex['stats'])[:300]}")
    return pkg
