# SPDX-License-Identifier: GPL-3.0-or-later
"""Read a Relic Essence 1 .whm (chunky) without Blender: meshes, tris, shells, hashes, xrefs, skeleton, markers,
animations (with per-mesh visibility keys) and textures; .whe motion trees; the default appearance rule.

Layout follows blender_dow 0.15 importer.py (amorgun, GPL-3). Std-lib only.
    info = read_whm(path_bytes, resolver)      # resolver finds .rsh/.rtx/.wtp/.whm by game path
"""
import hashlib, json, re, struct, sys
from collections import defaultdict
from pathlib import Path

STR_RE = re.compile(rb"[ -~]{4,}")
MODEL_RE = re.compile(r"^art/ebps/races/([^/]+)/(troops|structures|strucures)/(.+)\.whm$")


class R:
    """Little-endian cursor over bytes."""
    __slots__ = ("b", "o")

    def __init__(self, b, o=0):
        self.b, self.o = b, o

    def u(self, fmt):
        v = struct.unpack_from(fmt, self.b, self.o)
        self.o += struct.calcsize(fmt)
        return v

    def i(self):
        v = struct.unpack_from("<l", self.b, self.o)[0]
        self.o += 4
        return v

    def f(self):
        v = struct.unpack_from("<f", self.b, self.o)[0]
        self.o += 4
        return v

    def s(self):
        n = self.i()
        v = self.b[self.o:self.o + n].decode("utf8", "ignore")
        self.o += n
        return v


def chunks(b, off, end):
    """Yield (typeid, version, name, body_offset, size) for chunks in [off, end)."""
    while off + 20 <= end:
        tid, ver, size, nlen = struct.unpack_from("<8slll", b, off)
        name = b[off + 20: off + 20 + nlen].rstrip(b"\0").decode("utf8", "ignore")
        body = off + 20 + nlen
        yield tid.decode("ascii", "replace"), ver, name, body, size
        off = body + size


def find(b, off, end, typeid):
    for c in chunks(b, off, end):
        if c[0] == typeid:
            return c
    return None


class Resolver:
    """Case-insensitive game-path lookup over unpacked archive folders [(root, tag)] in load order (first wins)."""

    def __init__(self, roots):
        self.index = {}
        for root, tag in roots:
            root = Path(root)
            for p in root.rglob("*"):
                if p.is_file():
                    self.index.setdefault(p.relative_to(root).as_posix().lower(), (p, tag))

    def find(self, game_path):
        return self.index.get(game_path.replace("\\", "/").lower().lstrip("/"))


def texture_info(b):
    """(name, fmt, w, h) for each FOLDTXTR in an .rsh/.rtx; .rsh also lists FOLDSHDR channel textures."""
    out = []
    for tid, _, name, body, size in chunks(b, 24, len(b)):
        if tid in ("FOLDSHRF", "FOLDTXTR"):
            subs = [(tid, name, body, size)] if tid == "FOLDTXTR" else \
                   [(t, n, bo, s) for t, _, n, bo, s in chunks(b, body, body + size) if t == "FOLDTXTR"]
            for _, tname, tb, ts in subs:
                imag = find(b, tb, tb + ts, "FOLDIMAG")
                if imag:
                    attr = find(b, imag[3], imag[3] + imag[4], "DATAATTR")
                    if attr:
                        fmt, w, h, mips = struct.unpack_from("<4l", b, attr[3])
                        out.append({"name": tname, "fmt": fmt, "w": w, "h": h})
    return out


def wtp_info(b):
    tp = find(b, 24, len(b), "FOLDTPAT")
    if not tp:
        return None
    info = find(b, tp[3], tp[3] + tp[4], "DATAINFO")
    w, h = struct.unpack_from("<2L", b, info[3]) if info else (0, 0)
    layers = sum(1 for c in chunks(b, tp[3], tp[3] + tp[4]) if c[0] == "DATAPTLD")
    return {"w": w, "h": h, "layers": layers}


def shells(positions, faces):
    """Connected components over position-welded vertices."""
    weld, parent = {}, []
    remap = []
    for p in positions:
        k = (round(p[0], 4), round(p[1], 4), round(p[2], 4))
        if k not in weld:
            weld[k] = len(parent)
            parent.append(len(parent))
        remap.append(weld[k])

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    used = set()
    for a, b_, c in faces:
        if max(a, b_, c) >= len(remap):
            continue
        ra, rb, rc = root(remap[a]), root(remap[b_]), root(remap[c])
        parent[rb] = ra
        parent[rc] = ra
        used.update((ra,))
    return len({root(remap[i]) for f in faces for i in f if i < len(remap)})


def read_mslc(b, body, size, name):
    """One FOLDMSLC mesh."""
    dd = find(b, body, body + size, "DATADATA")
    r = R(b, dd[3])
    r.u("<lbll")
    nskin = r.i()
    for _ in range(nskin):
        r.s(); r.i()
    nv = r.i(); r.i()
    pos = list(struct.iter_unpack("<3f", b[r.o:r.o + 12 * nv]))
    r.o += 12 * nv
    if nskin:
        r.o += 16 * nv
    r.o += 12 * nv + 8 * nv + 4
    nmat = r.i()
    faces, mats = [], []
    for _ in range(nmat):
        tex = r.s()
        nidx = r.i()
        idx = struct.unpack_from(f"<{nidx}H", b, r.o)
        r.o += 2 * nidx + 8
        faces.extend(zip(idx[0::3], idx[1::3], idx[2::3]))
        mats.append({"texture": tex, "tris": nidx // 3})
    h = hashlib.sha1(b[body:body + size]).hexdigest()
    lo = [min(p[i] for p in pos) for i in range(3)] if pos else [0, 0, 0]
    hi = [max(p[i] for p in pos) for i in range(3)] if pos else [0, 0, 0]
    return {"name": name, "verts": nv, "tris": len(faces), "skinned_bones": nskin, "materials": mats,
            "shells": shells(pos, faces), "hash": h,
            # game axes: x, y (up), z -> bbox in game units
            "bbox_min": [round(v, 4) for v in lo], "bbox_max": [round(v, 4) for v in hi]}


def read_anim(b, body, size, ver_dd):
    """Animation DATADATA: frames, bone count, per-mesh visibility (force flag + keys)."""
    r = R(b, body)
    nframes = r.i(); dur = r.f()
    nb = r.i()
    for _ in range(nb):
        r.s()
        kp = r.i(); r.o += 16 * kp
        kr = r.i(); r.o += 20 * kr
        r.o += 1
    vis = {}
    nm = r.i()
    for _ in range(nm):
        mname = r.s(); mode = r.i()
        if mode == 2:
            r.o += 8
            keys = r.i() - 1
            r.o += 4
            force = r.f()
            ks = [r.u("<2f") for _ in range(keys)]
            vis[mname] = {"force": force, "keys": [[round(a, 3), round(v, 3)] for a, v in ks]}
        elif mode == 0:
            r.o += 4; r.i(); kt = r.i(); r.o += 8 * kt
        else:
            break  # unknown entry type; stop reading this anim's mesh list
    return {"frames": nframes, "duration": round(dur, 3), "bones": nb, "mesh_vis": vis}


def read_whm(b, resolver=None, want_anim_vis=True):
    out = {"materials": [], "textures": [], "meshes": [], "mesh_list": [], "xrefs": [], "bones": [],
           "markers": 0, "animations": {}, "warnings": []}
    top = find(b, 24, len(b), "FOLDRSGM")
    if not top:
        raise ValueError("no FOLDRSGM")
    out["root_name"] = top[2]
    for tid, ver, name, body, size in chunks(b, top[3], top[3] + top[4]):
        if tid == "DATASSHR":
            out["materials"].append(R(b, body).s())
        elif tid in ("FOLDTXTR", "FOLDSTXT", "FOLDSHDR"):
            out.setdefault("internal_" + tid.lower(), []).append(name)
        elif tid == "DATASKEL":
            r = R(b, body)
            for _ in range(r.i()):
                bn = r.s(); par = r.i(); r.o += 28
                out["bones"].append(bn)
        elif tid == "FOLDMSGR":
            for t2, v2, n2, b2, s2 in chunks(b, body, body + size):
                if t2 == "FOLDMSLC":
                    try:
                        out["meshes"].append(read_mslc(b, b2, s2, n2))
                    except Exception as e:  # noqa: BLE001
                        out["warnings"].append(f"mesh {n2}: {e!r}")
                elif t2 == "DATADATA":
                    r = R(b, b2)
                    for _ in range(r.i()):
                        mn = r.s(); mp = r.s(); par = r.i()
                        out["mesh_list"].append({"name": mn, "xref": mp or None, "parent_bone": par})
                        if mp:
                            out["xrefs"].append({"mesh": mn, "file": mp})
        elif tid == "DATAMARK":
            out["markers"] = struct.unpack_from("<l", b, body)[0]
        elif tid == "FOLDANIM":
            dd = find(b, body, body + size, "DATADATA")
            if dd:
                try:
                    a = read_anim(b, dd[3], dd[4], dd[1]) if want_anim_vis else {}
                except Exception as e:  # noqa: BLE001
                    a = {"error": repr(e)}
                out["animations"][dd[2]] = a
    if resolver is not None:
        seen = set()
        for m in out["materials"]:
            hit = resolver.find(m + ".rsh")
            if not hit:
                out["warnings"].append(f"missing material {m}.rsh")
                continue
            tb = hit[0].read_bytes()
            texs = texture_info(tb)
            for t in texs:
                if t["name"].lower() not in seen:
                    seen.add(t["name"].lower())
                    out["textures"].append({**t, "material": m, "from": hit[1]})
            for t2, _, n2, bo2, s2 in chunks(tb, 24, len(tb)):
                if t2 == "FOLDSHRF":
                    for t3, _, n3, _, _ in chunks(tb, bo2, bo2 + s2):
                        if t3 == "FOLDSTXT":  # external .rtx
                            h2 = resolver.find(n3 + ".rtx")
                            if h2:
                                for t in texture_info(h2[0].read_bytes()):
                                    if t["name"].lower() not in seen:
                                        seen.add(t["name"].lower())
                                        out["textures"].append({**t, "material": m, "from": h2[1]})
                            else:
                                out["warnings"].append(f"missing texture {n3}.rtx")
            wtp = resolver.find(m + "_default.wtp")
            if wtp:
                wi = wtp_info(wtp[0].read_bytes())
                if wi:
                    out["textures"].append({"name": m + "_default.wtp", "fmt": "wtp", "w": wi["w"], "h": wi["h"],
                                            "layers": wi["layers"], "material": m, "from": wtp[1]})
    out["totals"] = {"meshes": len(out["meshes"]), "tris": sum(m["tris"] for m in out["meshes"]),
                     "verts": sum(m["verts"] for m in out["meshes"]),
                     "shells": sum(m["shells"] for m in out["meshes"]), "bones": len(out["bones"]),
                     "animations": len(out["animations"]), "textures": len(out["textures"])}
    out["mesh_hash"] = hashlib.sha1("|".join(sorted(f"{m['name'].lower()}:{m['hash']}" for m in out["meshes"])).encode()).hexdigest()
    return out




# ---------------------------------------------------------------- classification

def tokens(name):
    return [t for t in re.split(r"[^a-z]+", name.lower()) if t]

WEAPON = set("""gun guns rifle bolter bolters boltgun flamer melta plasma cannon launcher sword swords axe blade blades knife
claw claws fist fists hammer pistol stubber missile lascannon autocannon shoota slugga choppa staff spear bow catapult lance
maul scythe glaive whip shuriken splinter blaster carbine weapon weapons sniper stake mace flail halberd chainsaw shotgun
rokkit burna deffgun lasgun hellgun autogun webber warscythe gauss tesla railgun pulse fusion bayonet cleaver chainsword
chainaxe powerfist thunderhammer crozius sabre saber falchion dagger katana spatha glaive kustom zzap lobba kannon
heavybolter storm stormbolter combi meltagun plasmagun flamethrower grenadelauncher lasrifle laspistol bolt_pistol
needler bolt hellpistol autopistol multimelta multilaser assault twinlinked scatter shuriken deathspitter devourer
spinefist boneswords talons scythes lashwhip venom barbed rending""".split())
EFFECT = set("fx muzzle flash glow smoke beam flame fire spark sparks steam trail shadow light sfx blur aura warp energy exhaust smoke selfillum hit".split())
HEAD = set("head heads helmet helm face hair hood mask".split())
BANNER = set("banner flag standard rod pole".split())

WALKER = set("dreadnought dread sentinel kan kans killa defiler walker wraithlord penitent grinder knight titan onager crisis broadside battlesuit riptide talos dominus armiger warhound reaver wraithknight dred stompa deffdread warwalker".split())
BIKE = set("bike bikes jetbike biker bikers outrider outriders warbike warbikes jetbikes skyboard".split())
FLYER = set("bomber bomba fighter thunderbolt valkyrie vulture nightwing talon tempest barracuda lightning avenger marauder stormtalon stormraven thunderhawk orca manta nightscythe doomscythe xiphon stormbird harrier heldrake razorwing voidraven aquila flyer fighta dakkajet".split())
MONSTER = set("avatar carnifex bloodthirster keeper unclean prince tyrant trygon mawloc squiggoth nightbringer deceiver tervigon tyrannofex haruspex exocrine toxicrex maleceptor swarmlord daemonprince greater".split())
VEHICLE = set("""tank rhino razorback predator vindicator whirlwind land raider landraider chimera basilisk leman russ baneblade hellhound
falcon fireprism serpent wave vyper battlewagon trukk looted wagon hammerhead devilfish skyray piranha raider ravager monolith
destroyer immolator exorcist repressor shadowsword stormlord medusa griffon manticore deathstrike hydra hornet hellhammer
stormhammer testudo malcador macharius taurox trojan centaur salamander scout_sentinel spider wraith lord annihilation barge
ark ghost forgefiend maulerfiend vehicle transport apc truck buggy artillery""".split())


def model_class(name, folder, armour):
    t = set(tokens(name))
    if folder.startswith("struc"):
        return "building"
    if t & FLYER:
        return "flyer"
    if t & BIKE:
        return "bike"
    if t & WALKER:
        return "walker"
    if armour:
        if armour.startswith("air"):
            return "flyer"
        if armour.startswith("vehicle"):
            return "vehicle"
        if armour.startswith("monster"):
            return "monster"
        if armour.startswith(("infantry", "commander", "builder")):
            return "infantry"
        if armour.startswith(("building", "structure")):
            return "building"
    if t & MONSTER:
        return "monster"
    if t & VEHICLE:
        return "vehicle"
    return "other"


def mesh_role(name):
    t = tokens(name)
    ts = set(t)
    if ts & EFFECT and not ts & WEAPON:
        return "effect"
    if ts & WEAPON or any(x.endswith(("gun", "cannon", "sword", "blade")) for x in t):
        return "weapon"
    if ts & HEAD:
        return "head"
    if ts & BANNER:
        return "banner"
    return "part"

# ---------------------------------------------------------------- .whe and default set

def whe_motions(b):
    """motion name -> referenced names (animations / events), from FOLDMTRE DATAMTON chunks."""
    top = find(b, 24, len(b), "FOLDREBP")
    if not top:
        return {}
    mt = find(b, top[3], top[3] + top[4], "FOLDMTRE")
    out = {}
    if not mt:
        return out
    for tid, _, name, body, size in chunks(b, mt[3], mt[3] + mt[4]):
        if tid != "DATAMTON":
            continue
        seg, strs, o = b[body:body + size], [], 0
        while o + 4 <= len(seg):
            n = int.from_bytes(seg[o:o + 4], "little")
            if 2 <= n <= 100 and o + 4 + n <= len(seg) and re.fullmatch(rb"[A-Za-z0-9_\-\. ]+", seg[o + 4:o + 4 + n]):
                strs.append(seg[o + 4:o + 4 + n].decode()); o += 4 + n
            else:
                o += 1
        out[name] = strs
    return out


def group_key(anim):
    """vis_head01 / vis_head_1 / vis_gear_combo_head_01 -> ('head', 1)."""
    m = re.match(r"^vis_(?:gear_combo_)?(.*?)[_]?(\d+)$", anim.lower())
    return (m.group(1), int(m.group(2))) if m else (None, None)


BASE_RE = re.compile(r"no_weapon|base|body|main|all\b|_all|ref\b|default|common|always|unit\b|normal|standard|chassis|hull|torso|legs|krootnest|full"
                     r"|^vis_master_?\d*$")  # Unification keeps the body in vis_master (316 models, Stage 3 check)
NOT_DEFAULT_RE = re.compile(r"no_(?!weapon)|upgrade|destroy|dead|damage|death|wreck|broken|_off\b|hide|hidden|_none|nothing")


def _shows(x):
    return x.get("force", 0) >= 0.5 or any(k[1] >= 0.5 for k in x["keys"])


def default_set(info, motions):
    """Return (visible mesh names, rule, used vis anims, confidence). Heuristic over vis_* animations + .whe motions."""
    anims = info["animations"]
    vis = {k: v for k, v in anims.items() if k.lower().startswith("vis") and "mesh_vis" in v}
    meshes = [m["name"] for m in info["meshes"]]
    if not vis:
        return meshes, "no vis animations: all meshes", [], "high"
    shows = {a: {mn.lower() for mn, x in v["mesh_vis"].items() if _shows(x)} for a, v in vis.items()}
    toggled = set().union(*shows.values())
    never = [m for m in meshes if m.lower() not in toggled]
    lower = {k.lower(): k for k in vis}
    mot = {k.lower(): [r.lower() for r in refs] for k, refs in motions.items()}
    used, parts, conf = [], [], "medium"
    combos = sorted(k for k, refs in mot.items() if re.match(r"^vis_random_?0*1$", k) and sum(r in lower for r in refs) >= 3)
    if combos:
        used = [lower[r] for r in mot[combos[0]] if r in lower]
        parts.append(f"loadout motion {combos[0]}")
        conf = "high"
    else:
        base = [a for a in vis if BASE_RE.search(a.lower()) and not NOT_DEFAULT_RE.search(a.lower())]
        used += base
        if base:
            parts.append("base " + ",".join(sorted(base)))
        weapon_pick = []
        for mname in ("vis_range", "vis_melee", "vis_weapon", "vis_default", "vis_ranged", "vis_weapons"):
            if mname in mot:
                weapon_pick += [lower[r] for r in mot[mname] if r in lower]
        if not weapon_pick:
            cands = sorted(k for k in mot if re.match(r"^vis_(range|ranged|melee|weapon)", k) and not NOT_DEFAULT_RE.search(k))
            if cands:
                weapon_pick = [lower[r] for r in mot[cands[0]] if r in lower]
        if not weapon_pick:  # weapon-named vis anim not marked as an upgrade
            wa = sorted(a for a in vis if set(tokens(a)) & WEAPON and not NOT_DEFAULT_RE.search(a.lower()) and a not in base)
            if wa:
                weapon_pick = [wa[0]]
                conf = "low"
        if weapon_pick:
            used += weapon_pick
            parts.append("weapon " + ",".join(weapon_pick))
        groups = defaultdict(list)
        for a in vis:
            g, n = group_key(a)
            if g and not NOT_DEFAULT_RE.search(g) and "random" not in g:
                groups[g].append((n, a))
        used += [min(lst)[1] for lst in groups.values()]
        if groups:
            parts.append("first of " + ",".join(sorted(groups)))
        if base and weapon_pick:
            conf = "high" if conf != "low" else "medium"
    shown = set().union(*(shows[a] for a in used)) if used else set()
    names = [m for m in meshes if m.lower() in shown or m in never]
    if not [m for m in names if m not in never]:
        # nothing recognised: take the first vis anim that is not an upgrade/destroyed state
        pick = sorted(a for a in vis if not NOT_DEFAULT_RE.search(a.lower()) and shows[a])
        if pick:
            used = [pick[0]]
            names = [m for m in meshes if m.lower() in shows[pick[0]] or m in never]
            parts.append(f"fallback first vis anim {pick[0]}")
        else:
            names = meshes
            parts.append("fallback all meshes")
        conf = "low"
    # body recovery: the largest non-weapon part must show. Its anim has a unit-specific name (vis_master_gravis,
    # vis_valkyrie, vis_horse, vis_head...). Skipped for a second head when one is already shown.
    part = [m for m in info["meshes"] if mesh_role(m["name"]) not in ("weapon", "effect", "banner")]
    if part:
        big = max(part, key=lambda m: m["tris"])
        lname = {n.lower() for n in names}
        two_heads = mesh_role(big["name"]) == "head" and any(mesh_role(n) == "head" for n in names)
        if big["name"].lower() not in lname and not two_heads:
            src = sorted(a for a in vis if big["name"].lower() in shows[a] and not NOT_DEFAULT_RE.search(a.lower()))
            if src:
                used = list(used) + [src[0]]
                shown = set().union(*(shows[a] for a in used))
                names = [m for m in meshes if m.lower() in shown or m in never]
                parts.append(f"body from {src[0]}")
                conf = "medium" if conf == "high" else conf
    return names, "; ".join(parts) + f"; +{len(never)} never-toggled", sorted(set(used)), conf

# ---------------------------------------------------------------- ebps links

def ebps_links(res):
    """ebps game path -> {'model': art path or None, 'armour': str, 'archive': tag}."""
    out = {}
    for key, (p, tag) in res.index.items():
        if not (key.startswith("attrib/ebps/") and key.endswith(".rgd")):
            continue
        ss = [s.decode() for s in STR_RE.findall(p.read_bytes())]
        model = None
        for s in ss:
            if re.match(r"(?i)^races[\\/]", s):
                cand = "art/ebps/" + s.replace("\\", "/").lower() + ".whm"
                if cand in res.index:
                    model = cand
                    break
        armour = next((m.group(1) for s in ss for m in [re.search(r"type_armour\\tp_(\w+)\.lua", s)]
                       if m and m.group(1) != "armour"), None)
        out[key] = {"model": model, "armour": armour, "archive": tag}
    return out

