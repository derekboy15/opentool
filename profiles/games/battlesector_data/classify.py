# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit class: infantry | walker | bike | monster | vehicle | flyer | other.

Decides pose handling (feet planting) and the fallback base type; the GW base guide (Stage 4) has the
final say on the base. Rules use the game's own data first:
  keyword 3 = vehicle, keyword 20012 = FLY (with DefaultMovementMode 3 = hover), IsLarge,
  rig bone names (legs vs tracks/wheels), then unit names for bikes/cavalry.
Anything decided by a name pattern or a weak signal carries "verify" in the reason.
"""
import re

KW_VEHICLE, KW_FLY, KW_MONSTERISH = 3, 20012, 1
BIKE_RX = re.compile(r"(Bike|Wartrike|Dragsta|TombBlade|Jetbike)", re.I)
CAVALRY_RX = re.compile(r"(Bloodcrusher|RoughRider|Juggernaut)", re.I)
FLYER_RX = re.compile(r"(Speeder|Barge|Piranha|Deffkopta|Stormraven|Stormhawk|Valkyrie|Drone)", re.I)
LEG_RX = re.compile(r"(Leg|Knee|Thigh|Calf|Ankle|Foot|Toe)", re.I)
TRACK_RX = re.compile(r"(Track|Wheel|Tread)", re.I)


def classify(u):
    name, kw = u["name"], set(u["keywords"])
    bones = u.get("_bones", set())
    legs = sum(1 for b in bones if LEG_RX.search(b))
    tracks = sum(1 for b in bones if TRACK_RX.search(b))
    if u["not_real_unit"] or not u["models"] or not u["models"][0]["variants"]:
        return "other", "game marks it NotRealUnit (deployable, companion or placeholder) or it has no model"
    vehicle = KW_VEHICLE in kw
    fly = KW_FLY in kw or u["movement_mode"] == 3
    if vehicle and BIKE_RX.search(name):
        return "bike", "vehicle keyword + bike name (verify)"
    if vehicle and fly:
        return "flyer", "vehicle + FLY keyword / hover movement"
    if vehicle and FLYER_RX.search(name):
        return "flyer", "vehicle + flyer name (verify)"
    if vehicle:
        # walkers carry the game's foot-grounding IK rig; crew legs inside tanks/trucks do not count
        leg_ik = any(b in bones for b in ("FootIKSetup", "LeftLegIK", "RightLegIK"))
        if leg_ik and not tracks:
            return "walker", "vehicle keyword + foot-grounding leg IK"
        if tracks:
            return "vehicle", f"vehicle keyword + {tracks} track/wheel bones"
        return "vehicle", "vehicle keyword, no walking legs (verify)"
    if CAVALRY_RX.search(name):
        return "bike", "cavalry name (verify)"
    if fly and FLYER_RX.search(name):
        return "flyer", "FLY + drone/skimmer name (verify)"
    if u["is_large"] or (KW_MONSTERISH in kw and (u.get("unit_height") or 0) >= 4.5):
        return "monster", "IsLarge, not a vehicle" if u["is_large"] else "keyword 1 + tall (verify)"
    return "infantry", "no vehicle keyword, not large"
