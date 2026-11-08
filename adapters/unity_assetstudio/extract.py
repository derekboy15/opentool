# SPDX-License-Identifier: GPL-3.0-or-later
"""Extract stage (per unit): unit rig + skinned meshes + clips + textures, and each weapon model + textures.

work/<faction>/<unit>/extract/
  animator/FBX_Animator/<animator>/<animator>.fbx (+ textures)   AssetStudio Animator export, clips bound
  weapons/<weapon>/<mesh>.fbx + <material>_N.png ...              weapon model + its texture set
  manifest.json   anim_fbx, materials {key: normal png}, weapons {name: {fbx, mat_key, normal}}

Unit models whose clips live in another faction's bundle (Ultramarines units use Blood Angels clips) are
exported from a staging folder holding copies of both bundles (the CLI takes one input path).
Weapon models are GameObjects inside model FBX assets; AssetStudio's splitObjects mode exports a whole
bundle (it ignores filters), so each bundle is split once into work/_split/<bundle>/ and only the weapon
FBX files are kept (the rest is deleted).
"""
import glob
import json
import os
import shutil
from collections import defaultdict

from . import activity, assetstudio, gamedump

def bundle_list(U, bundle):
    return gamedump.read_list(U, bundle)


def textures_near(U, container, bundle):
    """Texture2D assets filed in the same model folder as a mesh container (the weapon's texture set)."""
    folder = os.path.dirname(container)
    return [(n, p) for t, n, c, p in bundle_list(U, bundle) if t == "Texture2D" and os.path.dirname(c) == folder]


def export_animator(U, variant, out, log):
    bundles = sorted({variant["bundle"], *variant.get("clip_bundles", [])})
    inp = assetstudio.input_for(U, bundles, "+".join(gamedump.bundle_key(b) for b in bundles))
    assetstudio.run(U, inp, ["-m", "animator", "--filter-by-container", variant["prefab"].rsplit("/", 1)[-1].replace(".prefab", ""),
                          "--fbx-animation", "auto"], out, log)
    return pick_animator_fbx(out, variant)


def pick_animator_fbx(out, variant):
    """The unit's own rig among the exported animators: prefabs also carry ability/FX animators
    (FX_..., ZealousFrenzy...). Prefer the game's rig naming (<SET>@<UNIT>), then the largest file."""
    fbxs = [f for f in glob.glob(os.path.join(out, "FBX_Animator", "*", "*.fbx"))
            if not os.path.basename(f).startswith("FX_")]
    if not fbxs:
        raise RuntimeError(f"animator export produced no FBX in {out}")
    wanted = [a for a in variant.get("animators", []) if not a.startswith("FX_")]
    fbxs.sort(key=lambda f: ("@" in os.path.basename(f), os.path.splitext(os.path.basename(f))[0] in wanted,
                             os.path.getsize(f)), reverse=True)
    return fbxs[0]


def prefab_name(path):
    return os.path.splitext(os.path.basename(path))[0]


def model_name(container):
    """Weapon model files are named after the model asset (the FBX in the container path)."""
    return os.path.splitext(os.path.basename(container))[0]


def split_bundle(U, bundle, keep_names, log):
    """Weapon model FBXs of a bundle. AssetStudio's splitObjects exports the whole bundle (filters are
    ignored), so it runs once per bundle into _split/<bundle>/raw; the FBX files named in keep_names (every
    weapon model the inventory has in that bundle) are copied to _split/<bundle>/keep and raw is deleted."""
    with assetstudio.lock_for("split:" + bundle):
        return _split_bundle(U, bundle, keep_names, log)


def _split_bundle(U, bundle, keep_names, log):
    root = os.path.join(U.work, "_split", gamedump.bundle_key(bundle))
    keep = os.path.join(root, "keep")
    index = os.path.join(root, "index.json")
    have = json.load(open(index)) if os.path.exists(index) else None
    if have is None or not set(keep_names) <= set(have["requested"]):
        raw = os.path.join(root, "raw")
        if not os.path.exists(os.path.join(root, "raw.done")):
            shutil.rmtree(raw, ignore_errors=True)
            U.say(f"  splitting {bundle} once for its weapon models (long)")
            assetstudio.run(U, U.bundle_path(bundle), ["-m", "splitObjects"], raw, log, timeout=6 * 3600)
            open(os.path.join(root, "raw.done"), "w").close()
        os.makedirs(keep, exist_ok=True)
        found = set(have["found"]) if have else set()
        for f in glob.glob(os.path.join(raw, "**", "*.fbx"), recursive=True):
            n = os.path.splitext(os.path.basename(f))[0]
            if n in keep_names and n not in found:
                shutil.copy2(f, os.path.join(keep, n + ".fbx"))
                found.add(n)
        requested = sorted(set(keep_names) | set(have["requested"] if have else []))
        with open(index, "w") as fh:
            json.dump({"requested": requested, "found": sorted(found)}, fh, indent=1)
        shutil.rmtree(raw, ignore_errors=True)
        os.remove(os.path.join(root, "raw.done"))
    return {n: os.path.join(keep, n + ".fbx") for n in keep_names if os.path.exists(os.path.join(keep, n + ".fbx"))}


def export_textures(U, bundle, pathids, out, log):
    if not pathids:
        return
    assetstudio.run(U, U.bundle_path(bundle), ["-m", "export", "-t", "tex2d", "--filter-by-pathid",
                                                ",".join(str(p) for p in pathids), "-g", "none"], out, log)


def materials_from_folder(folder):
    """{material key: normal map} from <key>_N.png files."""
    return {os.path.basename(f)[:-6]: f for f in sorted(glob.glob(os.path.join(folder, "*_N.png")))}


def run(U, unit, weapons, log, all_weapons=None, variant_index=0):
    """unit: inventory record; weapons: {weapon id: inventory weapon record} needed by its jobs;
    all_weapons: every inventory weapon (so a bundle is split once for every unit that needs it)."""
    out = os.path.join(U.work, unit["faction"], unit["name"], "extract")
    os.makedirs(out, exist_ok=True)
    manifest = {"unit": unit["name"], "unit_id": unit["id"], "models": {}, "weapons": {}}
    for mi, model in enumerate(unit["models"]):
        if not model["variants"]:
            continue
        v = model["variants"][variant_index if variant_index < len(model["variants"]) else 0]
        mdir = os.path.join(out, f"model{model['id']}")
        fbx = export_animator(U, v, mdir, log)
        act = activity.prefab_activity(U, v["bundle"], v["prefab"], mdir, log)
        manifest["models"][str(model["id"])] = {
            "prefab": v["prefab"], "anim_fbx": fbx, "materials": materials_from_folder(os.path.dirname(fbx)),
            "weapon_joints": v["weapon_joints"], "clips": v["clips"],
            "hidden": act["inactive"] + act["renderer_off"], "hidden_detail": act}
    # weapons: the weapon prefab's own FBX when the split of its bundle could resolve it (all its parts, prefab
    # offsets), else the model asset FBX from the bundle holding the mesh (e.g. Bolt Rifle: prefab in
    # imperiumshared, mesh in the Blood Angels bundle). Each bundle is split once for every weapon it holds.
    keep = defaultdict(set)
    for w in (all_weapons or {}).values():
        if w.get("meshes"):
            keep[w["meshes"][0]["bundle"]].add(model_name(w["meshes"][0]["container"]))
            if w.get("prefab") and w.get("prefab_bundle"):
                keep[w["prefab_bundle"]].add(prefab_name(w["prefab"]))
    for wid, w in weapons.items():
        if w.get("meshes"):
            keep[w["meshes"][0]["bundle"]].add(model_name(w["meshes"][0]["container"]))
            if w.get("prefab_bundle"):
                keep[w["prefab_bundle"]].add(prefab_name(w["prefab"]))
    for wid, w in weapons.items():
        if not w.get("meshes"):
            continue
        m = w["meshes"][0]
        wdir = os.path.join(out, "weapons", w["name"])
        os.makedirs(wdir, exist_ok=True)
        cands = []
        if w.get("prefab_bundle"):
            pf = split_bundle(U, w["prefab_bundle"], sorted(keep[w["prefab_bundle"]]), log).get(prefab_name(w["prefab"]))
            if pf and os.path.getsize(pf) > 20000:  # a prefab whose mesh lives in another bundle exports empty
                cands.append(("prefab", pf, w["prefab_bundle"]))
        mf = split_bundle(U, m["bundle"], sorted(keep[m["bundle"]]), log).get(model_name(m["container"]))
        if mf:
            cands.append(("model", mf, m["bundle"]))
        tex = textures_near(U, m["container"], m["bundle"])
        if not any(n.endswith("_N") for n, p in tex):
            stem = "_".join(m["mesh"].split("_")[:3])
            tex = [(n, p) for t, n, c, p in bundle_list(U, m["bundle"]) if t == "Texture2D" and n.startswith(stem)]
        export_textures(U, m["bundle"], [p for n, p in tex if n.endswith("_N")], wdir, log)
        rec = {"mesh": m["mesh"], "fbx": None, "source": None, "hidden": [], "materials": materials_from_folder(wdir)}
        if cands:
            kind, src, sb = cands[0]
            dst = os.path.join(wdir, os.path.basename(src))
            shutil.copy2(src, dst)
            rec.update(fbx=dst, source=kind)
            if kind == "prefab":
                act = activity.prefab_activity(U, sb, w["prefab"], wdir, log)
                rec["hidden"] = act["inactive"] + act["renderer_off"]
        rec["mat_key"] = next(iter(rec["materials"]), None)
        manifest["weapons"][w["name"]] = rec
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    return manifest
