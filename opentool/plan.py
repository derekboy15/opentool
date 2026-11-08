# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan (stage 2): unit x loadout x pose -> job records in the queue.

Loadouts come from the export package (the adapter's list + the game profile). Loadouts that build the same
geometry are merged when they get there: the weapons stage fingerprints what will print, and a later job with
the same fingerprint stops as `duplicate` of the first.
"""
from . import config as C


def unit_task_id(group, unit):
    return f"{group}/{unit}"


def plan_unit(pl, group, unit, loadouts=None, poses=None, pin=None, tag=None, overrides=None, changed=None):
    """Create/refresh the unit task and its jobs. pin = {"clip", "frame"} builds exactly that frame as pose 1."""
    pkg = pl.package(group, unit)
    cls = pkg["unit"]["class_hint"]
    uid = unit_task_id(group, unit)
    cfg = C.build(pl.s, pl.game, cls, group, unit, ws_game_dir=pl.gdir)
    pl.q.record("unit", uid, {"id": uid, "group": group, "unit": unit, "unit_id": pkg["unit"]["id"], "class": cls}).save()
    los = [l["id"] for l in pkg["loadouts"]] if not loadouts else loadouts
    n = 1 if pin else (poses or cfg["pose"].get("count", 4))
    made = []
    for lo in los:
        pl.loadout(pkg, lo)
        for k in range(1, n + 1):
            jid = f"{'_' + tag + '/' if tag else ''}{uid}/{lo}/pose{k}"
            spec = {"id": jid, "unit_task": uid, "group": group, "unit": unit, "unit_id": pkg["unit"]["id"],
                    "class": cls, "loadout": lo, "pose": k, "tag": tag, "overrides": overrides or {},
                    "clip": pin["clip"] if pin else None, "frame": pin["frame"] if pin else None, "changed": changed}
            r = pl.q.record("job", jid, spec)
            r.save()
            made.append(jid)
    pl.q.event("planned", unit=uid, jobs=len(made))
    return made
