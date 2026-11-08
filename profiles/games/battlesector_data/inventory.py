# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage 1: inventory of every unit, squad-member model, weapon and animation clip, from game data.

Writes data/inventory/inventory.json (machine-readable, drives job planning) and
data/inventory/inventory_report.md (human summary per faction).
"""
import json
import os
import re
from collections import Counter, defaultdict

from . import classify
from .gamedata import PREFAB_RX, GameData

MODEL_SUFFIX = re.compile(r"Model\d+$")


def unit_name_from_prefab(path):
    m = PREFAB_RX.match(path)
    return MODEL_SUFFIX.sub("", m.group(2)) if m else os.path.splitext(os.path.basename(path))[0]


def faction_from_prefab(path):
    m = PREFAB_RX.match(path)
    return m.group(1) if m else None


def weapon_record(g, wid):
    w = g.weapons.get(wid, {})
    pf = g.weapon_prefab(wid)
    rec = {"id": wid, "prefab": pf["path"] if pf else None, "prefab_bundle": pf["bundle"] if pf else None,
           "name": os.path.splitext(os.path.basename(pf["path"]))[0] if pf else f"weapon{wid}",
           "category": w.get("WeaponCategory"), "pistol": bool(w.get("Pistol")), "heavy": bool(w.get("Heavy")),
           "use_animation_from": w.get("UseAnimationFrom") or None,
           "does_not_require_slot": bool(w.get("DoesNotRequireSlot")), "meshes": []}
    if pf:
        cands = g.weapon_meshes(pf["path"])
        if cands:
            rec["meshes"] = cands[:1]
            rec["mesh_candidates"] = sorted({c["mesh"] for c in cands})
    rec["has_model"] = bool(rec["meshes"])
    rec["integral"] = bool(pf) and not rec["meshes"]  # modelled into the unit body (no separate mesh)
    return rec


def model_record(g, mid):
    m = g.models.get(mid, {})
    variants = []
    for vp in g.model_prefabs(mid):
        info = g.prefab_info(vp["path"]) or {}
        ctrl = g.prefab_controller(vp["path"])
        clips = g.controller_clips.get(ctrl, []) if ctrl else []
        variants.append({
            "prefab": vp["path"], "bundle": vp["bundle"], "name": os.path.splitext(os.path.basename(vp["path"]))[0],
            "member_mesh_index": info.get("member_mesh_index"), "weapon_joints": info.get("weapon_joints", {}),
            "animators": g.prefab_animators(vp["path"]), "controller": ctrl,
            "clips": sorted({c[0] for c in clips}),
            "clip_bundles": sorted({c[2] for c in clips}),
            "meshes": len(g.prefab_meshes(vp["path"])),
        })
    wanims = {}
    for wa in m.get("WeaponAnimationClips", []):
        names = lambda lst: sorted({g.by_pathid[p["m_PathID"]][1] for p in lst if p["m_PathID"] in g.by_pathid})
        wanims[wa["WeaponType"]] = {"aim": names(wa.get("AimClips", [])), "shoot": names(wa.get("ShootClips", []))}
    return {"id": mid, "animation_set": m.get("AnimationSetName"), "variants": variants, "weapon_animations": wanims,
            "can_walk_and_fly": bool(m.get("CanWalkAndFly"))}


def fill_missing_clips(g, units):
    """Variants whose controller was not found by container/name: borrow the clip list of a controller that
    matches without the sub-faction prefix (MephritNecronWarrior -> NecronWarrior) or of another unit sharing
    the animation set. Inventory only: the extract stage exports whatever the prefab's Animator really uses."""
    by_set = {}
    for u in units:
        for m in u["models"]:
            for v in m["variants"]:
                if v["clips"] and m["animation_set"]:
                    by_set.setdefault(m["animation_set"], v)
    for u in units:
        for m in u["models"]:
            for v in m["variants"]:
                if v["clips"]:
                    continue
                name = re.sub(r"Model\d+$", "", v["name"])
                cands = [c for c in g.controller_clips if name.endswith(c) and len(c) >= 8]
                if cands:
                    c = max(cands, key=len)
                    v["controller"], v["clips"] = c, sorted({x[0] for x in g.controller_clips[c]})
                    v["clip_bundles"] = sorted({x[2] for x in g.controller_clips[c]})
                    v["clips_source"] = f"controller {c} (name without sub-faction prefix)"
                elif m["animation_set"] in by_set:
                    src = by_set[m["animation_set"]]
                    v["controller"], v["clips"], v["clip_bundles"] = src["controller"], src["clips"], src["clip_bundles"]
                    v["clips_source"] = f"animation set {m['animation_set']} (shared with {src['name']})"


def build(U):
    g = GameData(U)
    units = []
    weapons_used = {}
    for uid, u in g.units.items():
        if u.get("NotRealUnit") or u.get("Disable"):
            pass  # kept, but marked below
        model_ids = [u["DefaultModel"]] + [o["Model"] for o in u.get("ModelOverrideList", []) if o["Model"] != u["DefaultModel"]]
        models = [m for m in (model_record(g, mid) for mid in dict.fromkeys(model_ids)) if m["variants"]]
        prefab0 = models[0]["variants"][0]["prefab"] if models and models[0]["variants"] else None
        slots = []
        for i, s in enumerate(u.get("WeaponSlots", [])):
            ws = []
            for w in s["Weapons"]:
                if w["Weapon"] not in weapons_used:
                    weapons_used[w["Weapon"]] = weapon_record(g, w["Weapon"])
                ws.append(w["Weapon"])
            slots.append({"slot": i + 1, "weapons": ws, "combined_with_other_slot": bool(s.get("CombinedWithOtherSlot"))})
        overrides = {o["Model"]: o["Weapons"] for o in u.get("ModelOverrideList", [])}
        rec = {
            "id": uid, "name": unit_name_from_prefab(prefab0) if prefab0 else f"unit{uid}",
            "faction": faction_from_prefab(prefab0) if prefab0 else None, "faction_id": u["Faction"],
            "not_real_unit": bool(u.get("NotRealUnit")), "disabled": bool(u.get("Disable")),
            "keywords": u.get("Keywords", []), "members": u.get("Members"), "is_large": bool(u.get("IsLarge")),
            "movement_mode": u.get("DefaultMovementMode"), "flying_height_idle": u.get("FlyingHeightIdle"),
            "armor": {k: u.get(k) for k in ("ArmorProfile", "SingularArmor", "ArmorFront", "ArmorRear")},
            "max_health": u.get("MaxHealth"), "unit_height": u.get("UnitHeight"),
            "default_model": u["DefaultModel"], "models": models, "weapon_slots": slots,
            "model_weapon_overrides": overrides,
        }
        rec["_bones"] = g.prefab_bones(prefab0) if prefab0 else set()
        rec["class"], rec["class_reason"] = classify.classify(rec)
        del rec["_bones"]
        units.append(rec)
    fill_missing_clips(g, units)
    inv = {"units": sorted(units, key=lambda r: (r["faction"] or "~", r["name"])),
           "weapons": {str(k): v for k, v in sorted(weapons_used.items())}}
    with open(os.path.join(U.data, "inventory.json"), "w") as f:
        json.dump(inv, f, indent=1)
    return inv
