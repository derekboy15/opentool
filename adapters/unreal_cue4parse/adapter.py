# SPDX-License-Identifier: GPL-3.0-or-later
"""Unreal adapter (UE4.2x and UE5) on CUE4Parse (Apache-2.0) through opentool's small CLI `otue`.

Readers the user builds/downloads (README, "Readers"): `otue.exe` from adapters/unreal_cue4parse/cli (pinned NuGet
packages) plus, for ACL-compressed animations, CUE4Parse-Natives.dll next to it (built from source, ACL only).
[readers] ue_cli = the otue.exe path. Optional per game: [games.<id>] usmap = your own mappings file;
[readers] oodle_dll = an Oodle DLL you already have (never downloaded or shipped by the tool).

Game profile "unreal": game (CUE4Parse EGame, e.g. GAME_UE4_27), mesh_prefixes / anim_prefixes (package path
prefixes to scan), skip_mesh (regex: part meshes that are not units, e.g. heads/armour pieces), group_from (regex
with a "group" capture on the package path), units [hand-made entries: id, name, group, class, mesh, loadouts,
default_loadout, clip_include, credits] - Blueprints decide loadouts in Unreal games, so the profile names them.
clip_include / clip_exclude: regexes on clip names; additive clips are always skipped (they are deltas).

list:   gates (encryption, Oodle, versioning: plain byte reads), then `otue classes` over the prefixes once
        (cached in raw/classes.json): profile units first, then every other skeletal mesh as a low-confidence unit.
export: `otue export --format usd` of the mesh, its clips and its weapons (capped runner), then blender_export.py
        (capped runner) -> package/model/<unit>.glb + weapons/*.glb + textures + package.json.
"""
import glob
import json
import os
import re
import shutil

from opentool import CONTRACT_VERSION

from . import gates

ADAPTER = {"name": "unreal_cue4parse", "version": "0.1.0"}
HERE = os.path.dirname(os.path.abspath(__file__))
TEX_ROLES = [(r"(_N|_NORM|_Normal|_NM)$", "normal"), (r"(_C|_D|_ALBD|_BaseColor|_Albedo|_Diffuse|_BC)$", "diffuse")]


class UE:
    def __init__(self, ctx):
        self.ctx = ctx
        self.P = ctx.profile["unreal"]
        self.install = ctx.game["install"]
        self.usmap = ctx.game.get("usmap", "") or ""
        try:
            oodle = ctx.reader("oodle_dll")
        except Exception:  # noqa: BLE001  (optional reader)
            oodle = ""
        self.gate = gates.probe(self.install, usmap=self.usmap, oodle_dll=oodle)
        self.paks = self.gate["paks"]
        self.raw = os.path.join(ctx.gdir, "raw")
        os.makedirs(self.raw, exist_ok=True)
        with open(os.path.join(self.raw, "gates.json"), "w", encoding="utf-8") as f:
            json.dump(self.gate, f, indent=1)
        if not self.gate["ok"]:
            raise SystemExit("this Unreal game cannot be read:\n  - " + "\n  - ".join(self.gate["stops"]))
        for n in self.gate["notes"]:
            ctx.say(f"  gate: {n}")
        self.cli = ctx.reader("ue_cli")
        if os.path.isdir(self.cli):
            self.cli = os.path.join(self.cli, "otue.exe")
        if not os.path.exists(self.cli):
            raise FileNotFoundError(f"[readers] ue_cli: no otue.exe at {self.cli} (README: build the Unreal reader)")

    def otue(self, cmd, *args, name="otue", log=None):
        a = [self.cli, cmd, "--paks", self.paks, "--game", self.P["game"]]
        if self.usmap:
            a += ["--usmap", self.usmap]
        if self.gate.get("oodle_dll"):
            a += ["--oodle", self.gate["oodle_dll"]]
        r = self.ctx.runner.run(a + list(args), name=name, log_path=log)
        if not r["ok"] or not (r["result"] or {}).get("ok"):
            raise RuntimeError(f"otue {cmd} failed ({'; '.join(r['errors'])}): {r['tail'][-800:]}")
        return r["result"]

    def classes(self, refresh=False):
        p = os.path.join(self.raw, "classes.json")
        if refresh or not os.path.exists(p):
            prefixes = self.P.get("mesh_prefixes", []) + self.P.get("anim_prefixes", []) + self.P.get("weapon_prefixes", [])
            self.ctx.say(f"  scanning {len(prefixes)} package folders for skeletal meshes, skeletons and clips")
            self.otue("classes", "--prefix", ",".join(prefixes), "--out", p, name="otue_classes")
        with open(p, encoding="utf-8") as f:
            return json.load(f)


def game_path(object_path, content_root):
    """'/Game/X/Y.Y' -> 'Moria/Content/X/Y.Y' (CUE4Parse file paths)."""
    return content_root + object_path[len("/Game"):] if object_path.startswith("/Game/") else object_path.lstrip("/")


def clip_role(name, rules):
    for rx, role in rules:
        if re.search(rx, name, re.I):
            return role
    return "other"


def clips_for(ue, cls, skeleton, inc=None, exc=None):
    rules = ue.ctx.profile.get("clip_roles") or [[".*", "other"]]
    inc = inc or ue.P.get("clip_include") or ".*"
    exc = exc or ue.P.get("clip_exclude") or "$^"
    out = []
    for path, exps in sorted(cls.items()):
        for e in exps:
            if e.get("class") != "AnimSequence" or e.get("skeleton") != skeleton or e.get("additive", "AAT_None") != "AAT_None":
                continue
            if not re.search(inc, e["name"], re.I) or re.search(exc, e["name"], re.I):
                continue
            out.append({"name": e["name"], "role": clip_role(e["name"], rules), "frames": [0, max(0, e.get("frames", 1) - 1)],
                        "fps": e.get("rate", 30.0), "path": e["path"]})
    return out


def list_units(ctx):
    ue = UE(ctx)
    cls = ue.classes()
    root = ue.P["content_root"]
    meshes = {e["path"]: e for exps in cls.values() for e in exps if e.get("class") == "SkeletalMesh"}
    units, used = [], set()
    for pu in ue.P.get("units", []):
        mpath = "/Game" + pu["mesh"][len(root):] if pu["mesh"].startswith(root) else pu["mesh"]
        m = meshes.get(mpath)
        if m is None:
            ctx.say(f"  profile unit {pu['id']}: mesh {pu['mesh']} not found, skipped")
            continue
        used.add(mpath)
        clips = clips_for(ue, cls, m.get("skeleton"), pu.get("clip_include"), pu.get("clip_exclude"))
        units.append({"id": pu["id"], "name": pu["name"], "group": pu["group"], "class_hint": pu.get("class", "infantry"),
                      "confidence": "high", "models": [{"id": mpath, "bones": m.get("bones"), "materials": m.get("materials")}],
                      "loadouts": pu["loadouts"], "default_loadout": pu.get("default_loadout", pu["loadouts"][0]["id"]),
                      "clips": [{k: c[k] for k in ("name", "role")} for c in clips],
                      "provenance": {"archive": os.path.basename(ue.paks), "path": mpath, "skeleton": m.get("skeleton"),
                                     "source": "profile"},
                      "credits": pu.get("credits", []), "mesh": mpath})
    skip = ue.P.get("skip_mesh", "$^")
    grp = ue.P.get("group_from", r"/Game/(?P<group>[^/]+)/")
    for mpath, m in sorted(meshes.items()):
        if mpath in used or re.search(skip, mpath, re.I):
            continue
        g = re.search(grp, mpath)
        clips = clips_for(ue, cls, m.get("skeleton"))
        units.append({"id": mpath, "name": mpath.rsplit(".", 1)[-1], "group": g.group("group") if g else "misc",
                      "class_hint": "infantry" if clips else "other", "confidence": "low",
                      "models": [{"id": mpath, "bones": m.get("bones"), "materials": m.get("materials")}],
                      "loadouts": [{"id": "default", "weapons": []}], "default_loadout": "default",
                      "clips": [{k: c[k] for k in ("name", "role")} for c in clips],
                      "provenance": {"archive": os.path.basename(ue.paks), "path": mpath, "skeleton": m.get("skeleton"),
                                     "source": "mesh scan (no Blueprint read: loadout unknown)"},
                      "credits": [], "mesh": mpath})
    ctx.say(f"  {len(units)} units ({sum(u['confidence'] == 'high' for u in units)} from the profile, the rest mesh-only)")
    return {"contract": CONTRACT_VERSION, "game": ctx.game["id"], "adapter": ADAPTER, "units": units}


def export_unit(ctx, u, out):
    ue = UE(ctx)
    cls = ue.classes()
    root = ue.P["content_root"]
    mpath = u["mesh"]
    skel = u["provenance"].get("skeleton")
    pu = next((x for x in ue.P.get("units", []) if x["id"] == u["id"]), {})
    clips = clips_for(ue, cls, skel, pu.get("clip_include"), pu.get("clip_exclude"))
    max_clips = ue.P.get("max_clips", 60)
    if len(clips) > max_clips:
        ctx.say(f"  {len(clips)} clips; keeping {max_clips} (profile max_clips): idle/aim/shoot/melee/move first")
        pri = {r: i for i, r in enumerate(["idle", "aim", "shoot", "melee", "move", "other", "die"])}
        clips = sorted(clips, key=lambda c: (pri.get(c["role"], 9), c["name"]))[:max_clips]
    weapons = {}
    for lo in u["loadouts"]:
        for w in lo["weapons"]:
            if w.get("mesh"):
                weapons[w["weapon_id"]] = w["mesh"]
    work = os.path.join(ctx.gdir, "work", u["group"], u["name"])
    usd = os.path.join(work, "usd")
    if os.path.exists(usd):
        shutil.rmtree(usd)
    os.makedirs(usd)
    objs = [game_path(mpath, root)] + [game_path(c["path"], root) for c in clips] + [game_path(p, root) for p in weapons.values()]
    logs = os.path.join(ctx.gdir, "logs", "units", u["group"], u["name"])
    ctx.say(f"  reading {len(objs)} assets (mesh, {len(clips)} clips, {len(weapons)} weapons) with otue")
    ue.otue("export", "--format", "usd", "--out", usd, *objs, name="otue_export", log=os.path.join(logs, "otue_export.log"))

    def usd_of(object_path):
        rel = game_path(object_path, root).rsplit(".", 1)[0] + ".usda"
        p = os.path.join(usd, *rel.split("/"))
        return p if os.path.exists(p) else None

    cfg = {"mesh_usd": usd_of(mpath), "stem": u["name"], "out_dir": out,
           "anims": [{"name": c["name"], "usd": usd_of(c["path"])} for c in clips if usd_of(c["path"])],
           "weapons": [{"id": wid, "usd": usd_of(p)} for wid, p in weapons.items() if usd_of(p)],
           "sockets": sorted({w["mount"].split(":", 1)[1] for lo in u["loadouts"] for w in lo["weapons"]
                              if w["mount"].startswith("socket:")})}
    if not cfg["mesh_usd"]:
        raise RuntimeError(f"otue wrote no USD for {mpath}")
    missing = [c["name"] for c in clips if not usd_of(c["path"])]
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    cp = os.path.join(work, "export_config.json")
    with open(cp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    ctx.runner.blender(os.path.join(HERE, "blender_export.py"), [cp], name="unreal_export",
                       log_path=os.path.join(logs, "export.log"))
    ex = json.load(open(os.path.join(out, "export.json"), encoding="utf-8"))
    os.replace(os.path.join(out, "export.json"), os.path.join(work, "export.json"))

    # textures: material JSONs from otue name each material's texture parameters
    os.makedirs(os.path.join(out, "textures"), exist_ok=True)
    textures = []
    for mat in ex["stats"]["materials"]:
        base = mat.split(".")[0]
        js = glob.glob(os.path.join(usd, "**", base + ".json"), recursive=True)
        if not js:
            continue
        tex = json.load(open(js[0], encoding="utf-8")).get("Textures", {})
        seen = set()
        for param, t in tex.items():
            if not isinstance(t, dict) or not t.get("ObjectPath"):
                continue
            stem = t["ObjectPath"].rsplit("/", 1)[-1].split(".")[0]
            role = next((r for rx, r in TEX_ROLES if re.search(rx, stem, re.I)), None)
            if role is None or stem in seen or (role, mat) in {(x["role"], x["material"]) for x in textures}:
                continue
            png = glob.glob(os.path.join(usd, "**", stem + ".png"), recursive=True)
            if not png:
                continue
            seen.add(stem)
            dst = f"textures/{stem}.png"
            shutil.copy2(png[0], os.path.join(out, *dst.split("/")))
            e = {"file": dst, "role": role, "material": mat, "param": param}
            if role == "normal":
                e.update(encoding=ue.P.get("normal_encoding", "auto"), green=ue.P.get("normal_green", "directx"))
            textures.append(e)
    P = ctx.profile
    rig = dict(P.get("rig_map") or {})
    bones = set(ex["bone_names"])
    rig_missing = sorted(k for k, v in rig.items() if not k.startswith("_") and v not in bones)
    rig = {k: v for k, v in rig.items() if not k.startswith("_") and v in bones}
    sockets = {s["name"] for s in ex["sockets"]}
    warnings = list(ex["warnings"])
    if rig_missing:
        warnings.append(f"rig roles not on this rig: {rig_missing}")
    if missing:
        warnings.append(f"{len(missing)} clips not exported: {missing[:8]}")
    wfiles = {w["id"]: w for w in ex["weapons"]}
    for lo in u["loadouts"]:
        for w in lo["weapons"]:
            m = w["mount"]
            if m.startswith("socket:") and m.split(":", 1)[1] not in sockets:
                warnings.append(f"loadout {lo['id']}: socket {m} not on the mesh")
    clip_info = {c["name"]: c for c in ex["clips"]}
    pclips = [{"name": c["name"], "role": c["role"], "frames": clip_info[c["name"]]["frames"],
               "fps": clip_info[c["name"]]["fps"]} for c in clips if c["name"] in clip_info]
    pkg = {"contract": CONTRACT_VERSION, "adapter": ADAPTER,
           "reader": {"name": "otue (CUE4Parse)", "version": ue.P.get("reader_version", "CUE4Parse 1.2.2.202610")},
           "game": P.get("name", ctx.game["id"]),
           "unit": {"id": u["id"], "name": u["name"], "group": u["group"], "class_hint": u["class_hint"],
                    "class_confidence": u["confidence"]},
           "model": ex["model"], "format": "glb", "units": {"metres_per_unit": 1.0},
           "up_axis": "Z", "handedness": "right", "bind_pose_ok": True,
           "rig_map": rig, "clips": pclips, "mesh_groups": [], "hidden": [],
           "weapons": [{"id": wid, "file": w["file"], "tris": w["tris"], "size_m": w["size_m"],
                        "source": weapons.get(wid)} for wid, w in wfiles.items()],
           "loadouts": [{"id": lo["id"], "weapons": [{k: v for k, v in w.items() if k != "mesh"} for w in lo["weapons"]]}
                        for lo in u["loadouts"]],
           "default_loadout": u["default_loadout"], "textures": textures,
           "provenance": dict(u["provenance"], extract_stats=ex["stats"], sockets=ex["sockets"], gates={
               k: ue.gate.get(k) for k in ("engine_hint", "methods", "versioning_sample", "encrypted")}),
           "credits": u["credits"], "warnings": warnings}
    with open(os.path.join(out, "package.json"), "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=1)
    ctx.say(f"  extraction: {json.dumps(ex['stats'])[:300]}")
    return pkg
