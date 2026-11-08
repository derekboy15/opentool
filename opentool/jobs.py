# SPDX-License-Identifier: GPL-3.0-or-later
"""Resumable job queue: one JSON state file per job and per unit task under <workspace>/<game>/state/.

Job = one print: (unit, loadout, pose N). Its stages run in order; each records status, start, end, seconds,
log and error. A restart resumes at the first stage not "done"; a stage left "running" by a dead process goes
back to "pending". Job status: pending -> running -> done | flagged | failed | duplicate | obsolete.
  flagged   = built, but the checks still find problems (listed, never silently fixed)
  duplicate = prints the same geometry as `duplicate_of` (loadouts the game shows identically)
Unit tasks (ingest, scale, pose_select) are shared by every job of a unit.
"""
import datetime
import json
import os
import threading

UNIT_STAGES = ["ingest", "scale", "pose_select"]
JOB_STAGES = ["pose", "weapons", "prepare", "relief", "build", "check", "review", "package"]
_lock = threading.RLock()


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def safe(rid):
    return rid.replace("/", "__").replace("\\", "__").replace(" ", "_").replace(":", "_")


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class Queue:
    def __init__(self, game_dir):
        self.state = os.path.join(game_dir, "state")
        self.logs = os.path.join(game_dir, "logs")

    def event(self, name, **info):
        rec = {"t": now(), "event": name, **info}
        os.makedirs(self.logs, exist_ok=True)
        with _lock:
            with open(os.path.join(self.logs, "events.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")

    def record(self, kind, rid, spec=None):
        return Record(self, kind, rid, UNIT_STAGES if kind == "unit" else JOB_STAGES, spec)

    def all(self, kind):
        d = os.path.join(self.state, kind + "s")
        if not os.path.isdir(d):
            return []
        out = []
        for f in sorted(os.listdir(d)):
            if f.endswith(".json"):
                r = read_json(os.path.join(d, f))
                if r:
                    out.append(r)
        return out

    def lock(self):
        return RunnerLock(self)


class Record:
    def __init__(self, q, kind, rid, stages, spec=None):
        self.q, self.kind, self.id, self.stages = q, kind, rid, stages
        self.path = os.path.join(q.state, kind + "s", safe(rid) + ".json")
        d = read_json(self.path)
        if d is None:
            d = {"id": rid, "kind": kind, "status": "pending", "created": now(), "attempts": 0, "spec": spec or {},
                 "stages": {s: {"status": "pending"} for s in stages}, "flags": [], "result": {}}
        elif spec is not None:
            d["spec"] = spec
        for s in stages:
            d["stages"].setdefault(s, {"status": "pending"})
        self.d = d

    def save(self):
        self.d["updated"] = now()
        with _lock:
            write_json(self.path, self.d)

    @property
    def status(self):
        return self.d["status"]

    def next_stage(self):
        return next((s for s in self.stages if self.d["stages"][s]["status"] != "done"), None)

    def recover(self):
        changed = False
        for s, st in self.d["stages"].items():
            if st["status"] == "running":
                st["status"], st["note"] = "pending", f"reset after interrupted run ({st.get('started')})"
                changed = True
        if self.d["status"] == "running":
            self.d["status"], changed = "pending", True
        if changed:
            self.save()
            self.q.event("recovered", kind=self.kind, id=self.id)
        return changed

    def start_stage(self, s, log=None):
        self.d["status"] = "running"
        self.d["stages"][s] = {"status": "running", "started": now(), "log": log, "pid": os.getpid()}
        self.save()
        self.q.event("stage_start", kind=self.kind, id=self.id, stage=s)

    def end_stage(self, s, ok=True, seconds=None, error=None, **info):
        st = self.d["stages"][s]
        st.update({"status": "done" if ok else "failed", "finished": now(), "seconds": seconds, **info})
        if error:
            st["error"] = str(error)[-2000:]
        self.save()
        self.q.event("stage_end", kind=self.kind, id=self.id, stage=s, ok=ok, seconds=seconds,
                     **({"error": str(error)[:300]} if error else {}))

    def reset(self, from_stage=None):
        hit = from_stage is None
        for s in self.stages:
            hit = hit or s == from_stage
            if hit:
                self.d["stages"][s] = {"status": "pending"}
        self.d["status"], self.d["flags"] = "pending", []
        self.d.pop("error", None)
        self.save()


class RunnerLock:
    """One queue runner per game workspace (a stale lock from a dead process is taken over)."""

    def __init__(self, q):
        self.q = q
        self.path = os.path.join(q.state, "runner.lock")

    def __enter__(self):
        old = read_json(self.path)
        if old and pid_alive(old.get("pid")):
            raise SystemExit(f"another runner is active on this workspace (pid {old['pid']}, since {old.get('started')})")
        write_json(self.path, {"pid": os.getpid(), "started": now()})
        return self

    def __exit__(self, *a):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


def pid_alive(pid):
    if not pid:
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259
    except Exception:
        return False
