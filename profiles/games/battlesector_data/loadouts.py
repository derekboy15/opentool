# SPDX-License-Identifier: GPL-3.0-or-later
"""Loadouts from the game data (ported from the Battlesector pipeline's plan.py)."""


def weapon_name(inv, wid):
    w = inv["weapons"].get(str(wid))
    return w["name"] if w else f"weapon{wid}"


def _attached(inv, wid):
    w = inv["weapons"].get(str(wid))
    return bool(w and w["meshes"])


def loadouts(inv, u):
    slots = [s for s in u["weapon_slots"] if s["weapons"]]
    models = {m["id"] for m in u["models"]}
    if not models:
        return []
    default = [s["weapons"][0] for s in slots]
    overrides = {int(k): v for k, v in u.get("model_weapon_overrides", {}).items() if int(k) in models}

    def with_weapons(ws):
        """Default loadout with the given weapons put in the slots that offer them."""
        c = list(default)
        for w in ws:
            for i, s in enumerate(slots):
                if w in s["weapons"]:
                    c[i] = w
                    break
        return c

    def model_for(c):
        best, score = u["default_model"] if u["default_model"] in models else min(models), 0
        for m, ws in overrides.items():
            k = len(set(ws) & set(c))
            if k > score:
                best, score = m, k
        return best

    combos = [(u["default_model"] if u["default_model"] in models else model_for(default), default)]
    for m, ws in overrides.items():
        combos.append((m, with_weapons(ws)))
    for i, s in enumerate(slots):
        for w in s["weapons"][1:]:
            if _attached(inv, w) or _attached(inv, default[i]):
                c = list(default)
                c[i] = w
                combos.append((model_for(c), c))
    tags = body_tags(u)
    out, seen = [], set()
    for model, c in combos:
        meshes = tuple(sorted(inv["weapons"][str(w)]["meshes"][0]["mesh"] for w in c if _attached(inv, w)))
        sig = (model, meshes)
        if sig in seen:
            continue
        seen.add(sig)
        names = [weapon_name(inv, w) for w in c]
        parts = ([tags[model]] if len(tags) > 1 else []) + [n for n, w in zip(names, c) if _attached(inv, w)]
        key = "-".join(dict.fromkeys(p for p in parts if p)) or (tags.get(model) or "default")
        out.append({"slots": [s["slot"] for s in slots], "weapon_ids": c, "weapons": names, "model": model, "key": key,
                    "pose_group": pose_group(u, model, c)[0]})
    keys = [l["key"] for l in out]
    for l in out:  # keep file names unique
        if keys.count(l["key"]) > 1:
            l["key"] = f"{l['key']}-m{l['model']}"
    return out


def body_tags(u):
    """Short name per body model: its prefab name without the shared unit prefix and ModelNN
    (UltramarinesTerminatorsHeavyFlamerChainFistModel00 -> HeavyFlamerChainFist)."""
    import os
    import re
    names = {m["id"]: re.sub(r"Model\d+$", "", m["variants"][0]["name"]) for m in u["models"] if m["variants"]}
    if len(names) < 2:
        return {k: "" for k in names}
    prefix = os.path.commonprefix(list(names.values()))
    while prefix and prefix[-1].islower() and any(len(n) > len(prefix) and n[len(prefix)].islower() for n in names.values()):
        prefix = prefix[:-1]  # cut back to a word boundary
    return {k: (n[len(prefix):] or "Standard") for k, n in names.items()}


def pose_group(u, model_id, weapon_ids):
    """Weapon animation group of a loadout on a body model: weapons with their own aim/shoot clips in the game
    data (UnitModelData.WeaponAnimationClips, e.g. heavy weapons) are posed from those clips.
    Returns (key, clips this loadout may use, clips reserved for other weapons)."""
    m = next((m for m in u["models"] if m["id"] == model_id), None)
    wa = (m or {}).get("weapon_animations") or {}
    own, every = set(), set()
    for wt, a in wa.items():
        cl = set(a.get("aim", [])) | set(a.get("shoot", []))
        every |= cl
        if int(wt) in weapon_ids:
            own |= cl
    others = every - own
    import hashlib
    key = "default" if not others else "a" + hashlib.md5("|".join(sorted(own)).encode()).hexdigest()[:6]
    return key, sorted(own), sorted(others)


def merged_units(inv):
    """Unit entries grouped by (faction, name): campaign copies of one model become one buildable unit."""
    groups = {}
    for u in inv["units"]:
        if not u["faction"] or not u["models"]:
            continue
        groups.setdefault((u["faction"], u["name"]), []).append(u)
    out = []
    for (f, n), us in groups.items():
        main = min(us, key=lambda x: (x["disabled"], x["not_real_unit"], x["id"]))
        if len(us) > 1:
            main = dict(main)
            slots = {}
            for x in us:
                for s in x["weapon_slots"]:
                    cur = slots.setdefault(s["slot"], dict(s, weapons=[]))
                    cur["weapons"] += [w for w in s["weapons"] if w not in cur["weapons"]]
            main["weapon_slots"] = [slots[k] for k in sorted(slots)]
            main["merged_ids"] = [x["id"] for x in us]
        out.append(main)
    return out
