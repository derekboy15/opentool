# SPDX-License-Identifier: GPL-3.0-or-later
"""Adapter contract: canonical rig roles, a small JSON-schema checker (the subset our schemas use) and the
file-level part of `opentool adapter-check`. The Blender part (rig roles resolve on the real rig, a clip frame
poses) is backend/blender/ingest.py."""
import json
import os
import re

from . import CONTRACT_VERSION

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")

SIDED = ["shoulder", "elbow", "wrist", "hip", "knee", "ankle", "ball"]
ROLES = ["pelvis", "spine", "neck", "head", "eye", "brow"] + [f"{r}.{s}" for r in SIDED for s in ("L", "R")]
# what each stage needs; a missing role switches that part off with a warning instead of failing silently
ROLE_USES = {
    "ankle.L": "feet onto the base (leg IK), foot planting in pose select",
    "ankle.R": "feet onto the base (leg IK), foot planting in pose select",
    "ball.L": "foot planting", "ball.R": "foot planting",
    "knee.L": "leg IK chain, knee close-ups", "knee.R": "leg IK chain, knee close-ups",
    "wrist.L": "hand close-ups, held props", "wrist.R": "hand close-ups, held props",
    "pelvis": "props to the belt, pose signature root",
    "head": "eye-height scale fallback, head close-up",
    "eye": "eye-height scale (sole to pupil line)",
}
CLIP_ROLES = ["idle", "aim", "shoot", "move", "melee", "die", "other"]
CLASSES = ["infantry", "walker", "bike", "monster", "vehicle", "flyer", "other"]


def load_schema(name):
    with open(os.path.join(SCHEMA_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def validate(data, schema, path="$"):
    """Errors as 'path: message' strings (type, required, properties, items, enum, pattern)."""
    errs = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float), "integer": int}
    if t and not isinstance(data, types[t]):
        return [f"{path}: expected {t}, got {type(data).__name__}"]
    if "enum" in schema and data not in schema["enum"]:
        errs.append(f"{path}: {data!r} not one of {schema['enum']}")
    if "pattern" in schema and isinstance(data, str) and not re.search(schema["pattern"], data):
        errs.append(f"{path}: {data!r} does not match {schema['pattern']}")
    if isinstance(data, dict):
        for k in schema.get("required", []):
            if k not in data:
                errs.append(f"{path}: missing {k!r}")
        for k, sub in schema.get("properties", {}).items():
            if k in data:
                errs += validate(data[k], sub, f"{path}.{k}")
    if isinstance(data, list) and "items" in schema:
        for i, x in enumerate(data):
            errs += validate(x, schema["items"], f"{path}[{i}]")
    return errs


def _rel_ok(p):
    return isinstance(p, str) and p and not os.path.isabs(p) and ":" not in p and "\\" not in p and ".." not in p.split("/")


def check_package(pkg_dir):
    """File-level checks. Returns (errors, warnings, package dict)."""
    errs, warns = [], []
    pj = os.path.join(pkg_dir, "package.json")
    if not os.path.exists(pj):
        return [f"no package.json in {pkg_dir}"], [], None
    with open(pj, encoding="utf-8") as f:
        pkg = json.load(f)
    errs += validate(pkg, load_schema("package.schema.json"))
    if errs:
        return errs, warns, pkg
    if pkg["contract"].split(".")[0] != CONTRACT_VERSION.split(".")[0]:
        errs.append(f"contract {pkg['contract']} is not compatible with {CONTRACT_VERSION}")
    files = [("model", pkg["model"])] + [(f"weapon {w['id']}", w["file"]) for w in pkg["weapons"] if w.get("file")] + \
            [(f"texture {t.get('material', '')}", t["file"]) for t in pkg["textures"]]
    for what, p in files:
        if not _rel_ok(p):
            errs.append(f"{what}: path {p!r} must be relative, forward slashes, inside the package")
        elif not os.path.exists(os.path.join(pkg_dir, p)):
            errs.append(f"{what}: {p} missing")
    ids = {w["id"] for w in pkg["weapons"]}
    groups = {g["name"] for g in pkg["mesh_groups"]}
    lo_ids = [lo["id"] for lo in pkg["loadouts"]]
    if pkg["default_loadout"] not in lo_ids:
        errs.append(f"default_loadout {pkg['default_loadout']!r} is not a loadout ({lo_ids})")
    for lo in pkg["loadouts"]:
        for w in lo["weapons"]:
            m = w["mount"]
            if m.startswith("mesh_group:") and m.split(":", 1)[1] not in groups:
                errs.append(f"loadout {lo['id']}: mesh group {m} not in mesh_groups")
            if m.startswith(("joint:", "socket:")) or m == "bonemerge":
                if w["weapon_id"] not in ids:
                    errs.append(f"loadout {lo['id']}: weapon {w['weapon_id']} has a {m.split(':')[0]} mount but no weapon file")
                elif m == "bonemerge" and not next(x for x in pkg["weapons"] if x["id"] == w["weapon_id"]).get("mount_joint"):
                    errs.append(f"loadout {lo['id']}: bonemerge weapon {w['weapon_id']} needs mount_joint (the model's empty on the shared bone)")
        for g in lo.get("mesh_groups", []):
            if g not in groups:
                errs.append(f"loadout {lo['id']}: shows mesh group {g!r} that is not in mesh_groups")
    for role in pkg["rig_map"]:
        if role not in ROLES and not role.startswith("weapon."):
            warns.append(f"rig_map role {role!r} is not canonical (known: {', '.join(ROLES)}, weapon.<n>)")
    for role, use in ROLE_USES.items():
        if role not in pkg["rig_map"]:
            warns.append(f"rig role {role} missing: {use} switched off")
    if not any(c["role"] == "idle" for c in pkg["clips"]):
        warns.append("no clip with role idle")
    for t in pkg["textures"]:
        if t["role"] == "normal" and "green" not in t:
            warns.append(f"normal map {t['file']}: green convention not given (assuming opengl)")
    return errs, warns, pkg
