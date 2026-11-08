# SPDX-License-Identifier: GPL-3.0-or-later
"""opentool: game models -> printable 28 mm miniatures.

    python -I ot.py doctor                          check opentool.toml, Blender 5.2, readers, runner caps
    python -I ot.py list <game>                     adapter `list` -> <workspace>/<game>/units.json
    python -I ot.py units <game> [--find TEXT]      show the unit list
    python -I ot.py export <game> <unit>            adapter `export` -> <workspace>/<game>/packages/<group>/<unit>/
    python -I ot.py adapter-check <game> <unit>     package schema, files, rig roles on the real rig, a clip poses
    python -I ot.py plan <game> <unit> [--loadout L] [--poses N] [--clip C --frame F]
    python -I ot.py run <game> [--job GLOB] [--retry-failed] [--stop-after STAGE]
    python -I ot.py status <game> [--list]
    python -I ot.py reset <game> --job GLOB [--from STAGE]
    python -I ot.py regress <game> [--reference STL]   the game profile's regression job + comparison
    python -I ot.py compare <reference.stl> <candidate.stl>

<game> is a [games.<id>] entry of opentool.toml. The game install is only ever read.
"""
import argparse
import fnmatch
import importlib
import json
import os
import sys

from . import CONTRACT_VERSION, __version__
from . import config as C
from . import contract, jobs, plan
from .pipeline import BL, Pipeline, StageError
from .runner import Runner, RunError


def say(m):
    print(m, flush=True)


def load_adapter(name):
    return importlib.import_module(f"adapters.{name}.adapter")


class Ctx:
    """What an adapter gets: settings, game, profile, workspace folder, the capped runner."""

    def __init__(self, settings, game_id):
        self.s = settings
        self.game = settings.game(game_id)
        self.profile = C.game_profile(self.game["profile"])
        self.gdir = C.game_dir(settings, game_id)
        self.runner = Runner(settings, os.path.join(self.gdir, "logs", "runner"), say=lambda m: say(f"[runner] {m}"))
        self.say = say

    def reader(self, key):
        return self.s.reader(key)


def units_path(gdir):
    return os.path.join(gdir, "units.json")


def find_unit(units, key):
    hits = [u for u in units["units"] if key in (u["id"], u["name"], f"{u['group']}/{u['name']}")]
    if not hits:
        hits = [u for u in units["units"] if key.lower() in u["name"].lower()]
    if len(hits) != 1:
        raise SystemExit(f"{len(hits)} units match {key!r}" + (": " + ", ".join(f"{u['group']}/{u['name']}" for u in hits[:20]) if hits else ""))
    return hits[0]


def cmd_doctor(a):
    s = C.Settings()
    ok = True
    print(f"opentool {__version__}, contract {CONTRACT_VERSION}, config {s.path}")
    print(f"runner: <= {s.max_jobs} jobs per PC, {s.mem_gb:g} GB per job, starts with {s.mem_gb + s.free_margin_gb:g} GB free; slots {s.slot_dir}")
    try:
        print("workspace:", s.require_workspace())
    except C.ConfigError as e:
        ok = False
        print("PROBLEM:", e)
    r = Runner(s, os.path.join(s.workspace or os.path.expanduser("~"), "_logs"))
    try:
        res = r.blender(os.path.join(BL, "version_check.py"), [])
        print("blender:", (res["result"] or {}).get("version"), "ok")
    except (RunError, OSError) as e:
        ok = False
        print("PROBLEM blender:", str(e)[:400])
    for k, v in s.readers.items():
        print(f"reader {k}: {v or '(not set)'} {'ok' if v and os.path.exists(v) else 'MISSING'}")
    for g, d in s.games.items():
        print(f"game {g}: {d.get('install')} {'ok' if d.get('install') and os.path.isdir(d['install']) else 'MISSING'}")
    sys.exit(0 if ok else 1)


def cmd_list(a):
    ctx = Ctx(C.Settings(), a.game)
    ad = load_adapter(ctx.profile["adapter"])
    units = ad.list_units(ctx)
    errs = contract.validate(units, contract.load_schema("units.schema.json"))
    if errs:
        raise SystemExit("units.json fails the contract:\n  " + "\n  ".join(errs[:30]))
    jobs.write_json(units_path(ctx.gdir), units)
    by = {}
    for u in units["units"]:
        by.setdefault(u["class_hint"], 0)
        by[u["class_hint"]] += 1
    print(f"{len(units['units'])} units -> {units_path(ctx.gdir)}  ({', '.join(f'{k} {v}' for k, v in sorted(by.items()))})")


def cmd_units(a):
    s = C.Settings()
    u = jobs.read_json(units_path(C.game_dir(s, a.game)))
    if not u:
        raise SystemExit(f"no unit list yet: run `list {a.game}`")
    for x in u["units"]:
        if not a.find or a.find.lower() in (x["group"] + "/" + x["name"]).lower():
            print(f"{x['group']}/{x['name']:40} {x['class_hint']:9} {x['confidence']:6} loadouts {len(x['loadouts'])} clips {len(x['clips'])}")


def cmd_export(a):
    ctx = Ctx(C.Settings(), a.game)
    units = jobs.read_json(units_path(ctx.gdir))
    if not units:
        raise SystemExit(f"no unit list yet: run `list {a.game}` first")
    u = find_unit(units, a.unit)
    ad = load_adapter(ctx.profile["adapter"])
    out = os.path.join(ctx.gdir, "packages", u["group"], u["name"])
    pkg = ad.export_unit(ctx, u, out)
    errs, warns, _ = contract.check_package(out)
    for w in warns:
        print("warning:", w)
    if errs:
        raise SystemExit("package fails adapter-check:\n  " + "\n  ".join(errs))
    print(f"exported {u['group']}/{u['name']} -> {out} ({len(pkg['loadouts'])} loadouts, {len(pkg['clips'])} clips)")


def cmd_adapter_check(a):
    s = C.Settings()
    pl = Pipeline(s, a.game, say=say)
    units = jobs.read_json(units_path(pl.gdir))
    u = find_unit(units, a.unit)
    pdir = pl.package_dir(u["group"], u["name"])
    errs, warns, pkg = contract.check_package(pdir)
    for w in warns:
        print("warning:", w)
    if errs:
        print("FAIL:\n  " + "\n  ".join(errs))
        sys.exit(1)
    spec = {"id": f"{u['group']}/{u['name']}", "group": u["group"], "unit": u["name"], "class": pkg["unit"]["class_hint"]}
    task = jobs.Record(pl.q, "unit", "_check/" + spec["id"], jobs.UNIT_STAGES, spec)
    res = pl.st_ingest(task)
    print(json.dumps(res, indent=1))
    print("adapter-check: PASS" if not res["roles_missing"] else "adapter-check: PASS with missing roles (stages switched off)")


def cmd_plan(a):
    s = C.Settings()
    pl = Pipeline(s, a.game, say=say)
    units = jobs.read_json(units_path(pl.gdir))
    u = find_unit(units, a.unit)
    pin = {"clip": a.clip, "frame": a.frame} if a.clip else None
    made = plan.plan_unit(pl, u["group"], u["name"], loadouts=a.loadout, poses=a.poses, pin=pin, changed=a.changed)
    print(f"planned {len(made)} job(s):")
    for j in made:
        print("  ", j)


def job_filter(a):
    def ok(d):
        return not a.job or any(fnmatch.fnmatch(d["id"], j) for j in a.job)
    return ok


def cmd_run(a):
    pl = Pipeline(C.Settings(), a.game, say=say)
    res = pl.run(only=job_filter(a), retry_failed=a.retry_failed, workers=a.workers, max_jobs=a.max_jobs, stop_after=a.stop_after)
    for k, v in res.items():
        print(f"{v:9} {k}")
    if not res:
        print("nothing to run (plan first, or --retry-failed)")


def cmd_status(a):
    pl = Pipeline(C.Settings(), a.game, say=say)
    recs = [d for d in pl.q.all("job") if job_filter(a)(d)]
    from collections import Counter
    print(dict(Counter(d["status"] for d in recs)))
    if a.list:
        for d in recs:
            nxt = next((s for s in jobs.JOB_STAGES if d["stages"][s]["status"] != "done"), "-")
            print(f"  {d['status']:9} {d['id']:60} next={nxt} {'; '.join(d.get('flags', []))[:150]} {d.get('error', '')[:200]}")


def cmd_reset(a):
    pl = Pipeline(C.Settings(), a.game, say=say)
    n = 0
    frm = a.from_stage
    if frm in ("prepare", "relief", "build", "check", "review"):
        frm = "weapons"  # packaging deleted posed.blend and the height maps; weapons rebuilds them (the pose is cached)
    for d in pl.q.all("job"):
        if job_filter(a)(d):
            pl.q.record("job", d["id"]).reset(frm)
            n += 1
    print(f"reset {n} job(s)")


def compare(runner, ref, cand, out):
    runner.blender(os.path.join(BL, "compare_stl.py"), [ref, cand, out], name="compare_stl")
    return jobs.read_json(out)


def judge(c, T, ref_metrics=None):
    checks = {
        "p99 distance": max(c["ref_to_cand"]["p99_mm"], c["cand_to_ref"]["p99_mm"]) <= T["p99_mm"],
        "max distance": max(c["ref_to_cand"]["max_mm"], c["cand_to_ref"]["max_mm"]) <= T["max_mm"],
        "volume": c["volume_rel_diff"] <= T["volume_rel"],
        "dims": max(c["dims_diff_mm"]) <= T["dims_mm"],
        "shells": c["candidate"]["shells"] == c["reference"]["shells"],
    }
    return checks


def cmd_regress(a):
    s = C.Settings()
    pl = Pipeline(s, a.game, say=say)
    R = pl.profile.get("regression")
    if not R:
        raise SystemExit(f"game profile {pl.game['profile']} has no regression job")
    units = jobs.read_json(units_path(pl.gdir))
    if not units:
        raise SystemExit(f"run `list {a.game}` and `export {a.game} {R['unit']}` first")
    u = find_unit(units, R["unit"])
    if not os.path.exists(os.path.join(pl.package_dir(u["group"], u["name"]), "package.json")):
        raise SystemExit(f"run `export {a.game} {R['unit']}` first")
    made = plan.plan_unit(pl, u["group"], u["name"], loadouts=[R["loadout"]], pin=R["pin"], tag="regression",
                          overrides=R.get("overrides"))
    jid = made[0]
    if not a.keep:
        pl.q.record("job", jid).reset()
    res = pl.run(only=lambda d: d["id"] == jid, retry_failed=True)
    st = res.get(jid)
    rec = pl.q.record("job", jid)
    if rec.status not in ("done", "flagged"):
        raise SystemExit(f"regression job did not finish ({st}): {rec.d.get('error')}")
    p = pl.job_paths(rec.d["spec"])
    stl = os.path.join(p["output"], f"{u['name']}_{R['loadout']}_pose1.stl")
    out = os.path.join(pl.gdir, "regression_compare.json")
    T = R["tolerance"]
    if a.reference:
        c = compare(pl.runner, a.reference, stl, out)
        checks = judge(c, T)
    else:  # no reference STL on this PC: the shipped metrics only (volume, dims, shells)
        c = compare(pl.runner, stl, stl, out)
        m = R["reference_metrics"]
        cand = c["candidate"]
        checks = {"volume": abs(cand["volume_mm3"] - m["volume_mm3"]) / m["volume_mm3"] <= T["volume_rel"],
                  "dims": max(abs(x - y) for x, y in zip(cand["dims_mm"], m["dims_mm"])) <= T["dims_mm"],
                  "shells": cand["shells"] == m["shells"]}
        c["reference"] = m
    c["tolerance"], c["checks"], c["pass"] = T, checks, all(checks.values())
    c["peak_gb"] = rec.d["result"].get("build", {}).get("peak_gb")
    c["voxel_mm"] = rec.d["result"].get("build", {}).get("voxel_mm")
    jobs.write_json(out, c)
    print(json.dumps({k: c.get(k) for k in ("ref_to_cand", "cand_to_ref", "volume_rel_diff", "dims_diff_mm", "voxel_mm",
                                            "peak_gb", "checks", "pass")}, indent=1))
    sys.exit(0 if c["pass"] else 1)


def cmd_compare(a):
    s = C.Settings()
    r = Runner(s, os.path.join(s.require_workspace(), "_logs"))
    out = a.out or os.path.join(s.require_workspace(), "compare.json")
    c = compare(r, a.reference, a.candidate, out)
    print(json.dumps({k: c[k] for k in ("reference", "candidate", "ref_to_cand", "cand_to_ref", "volume_rel_diff", "dims_diff_mm")}, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="opentool", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    for name in ("list", "units", "status", "run", "reset", "regress"):
        p = sub.add_parser(name)
        p.add_argument("game")
        if name == "units":
            p.add_argument("--find")
        if name in ("status", "run", "reset"):
            p.add_argument("--job", action="append")
        if name == "status":
            p.add_argument("--list", action="store_true")
        if name == "run":
            p.add_argument("--retry-failed", action="store_true")
            p.add_argument("--stop-after", choices=jobs.JOB_STAGES)
            p.add_argument("--workers", type=int, default=1)
            p.add_argument("--max-jobs", type=int)
        if name == "reset":
            p.add_argument("--from", dest="from_stage", choices=jobs.JOB_STAGES)
        if name == "regress":
            p.add_argument("--reference", help="your own locally built reference STL (never shipped)")
            p.add_argument("--keep", action="store_true", help="do not reset the job first (resume)")
    for name in ("export", "adapter-check", "plan"):
        p = sub.add_parser(name)
        p.add_argument("game")
        p.add_argument("unit")
        if name == "plan":
            p.add_argument("--loadout", action="append")
            p.add_argument("--poses", type=int)
            p.add_argument("--clip")
            p.add_argument("--frame", type=int)
            p.add_argument("--changed", help="one line for the review sheet: what changed since the last round")
    p = sub.add_parser("compare")
    p.add_argument("reference")
    p.add_argument("candidate")
    p.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        {"doctor": cmd_doctor, "list": cmd_list, "units": cmd_units, "export": cmd_export, "adapter-check": cmd_adapter_check,
         "plan": cmd_plan, "run": cmd_run, "status": cmd_status, "reset": cmd_reset, "regress": cmd_regress,
         "compare": cmd_compare}[a.cmd](a)
    except (C.ConfigError, StageError, RunError) as e:
        raise SystemExit(f"error: {e}")
