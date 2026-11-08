# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical rig roles -> bones of the loaded rig (cfg["rig_map"]: role -> game bone name, matched as a suffix).

Roles: pelvis, spine, neck, head, eye, brow, shoulder/elbow/wrist/hip/knee/ankle/ball.L|R, weapon.<n>.
A missing role returns None; each stage switches the part that needs it off and says so.
"""
from anim import find_bone

SIDES = ("L", "R")


def role_name(rig_map, role, side=None):
    return (rig_map or {}).get(f"{role}.{side}" if side else role)


def bone(arm, rig_map, role, side=None, required=False):
    name = role_name(rig_map, role, side)
    b = find_bone(arm, name) if name else None
    if b is None and required:
        raise SystemExit(f"rig role {role}{'.' + side if side else ''} ({name!r}) not found on the rig")
    return b


def sides_with(arm, rig_map, role):
    return [s for s in SIDES if bone(arm, rig_map, role, s)]


def subtree_names(pb):
    return {pb.name} | {c.name for c in pb.bone.children_recursive}


def hand_names(arm, rig_map, patterns=()):
    """Bone names that count as 'the hand': each wrist bone and everything below it (fingers, thumb, props
    joints). A game profile may instead give name patterns (substrings) as its games' tools always did."""
    out = set()
    if patterns:
        return {b.name for b in arm.pose.bones if any(p in b.name for p in patterns)}
    for s in SIDES:
        w = bone(arm, rig_map, "wrist", s)
        if w:
            out |= subtree_names(w)
    return out
