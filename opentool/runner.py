# SPDX-License-Identifier: GPL-3.0-or-later
"""The capped runner: every Blender call (and every other heavy process) goes through here.

PC-wide limits, which the user can lower but not switch off:
  - at most `runner.max_jobs` (<= 3) jobs at once on this PC, across every tool and session that shares
    `runner.slot_dir` (lock files; Windows drops a lock when its process dies);
  - each job is capped at `runner.mem_gb` (default 16, hard max 24) of committed memory by a Windows job
    object: a job that grows past it is stopped by Windows and reported as "memory cap hit" instead of taking
    the PC down. A cap hit means the voxel/grid size is wrong for the part: fix the input, never raise the cap;
  - a job starts only when the PC has its cap + free_margin_gb (>= 4) of memory free.
Blender always runs `-b --factory-startup --python-exit-code 1`.
"""
import ctypes
import datetime as dt
import json
import os
import subprocess
import sys
import time

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import msvcrt


class RunError(RuntimeError):
    def __init__(self, msg, result=None):
        super().__init__(msg)
        self.result = result or {}


# ---------------------------------------------------------------- slots and free memory
def _take_slot(slot_dir, n_slots, say):
    os.makedirs(slot_dir, exist_ok=True)
    said = False
    while True:
        for i in range(n_slots):
            fh = open(os.path.join(slot_dir, f"slot{i}.lock"), "a+")
            try:
                fh.seek(0)
                if IS_WINDOWS:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - the tool targets Windows (job-object cap)
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except OSError:
                fh.close()
        if not said:
            say(f"all {n_slots} job slots busy; waiting")
            said = True
        time.sleep(2)


def _wait_for_memory(need_gb, say):
    try:
        import psutil
    except ImportError:
        say("psutil missing: cannot check free memory before starting (pip install psutil)")
        return
    said = False
    while psutil.virtual_memory().available / 2**30 < need_gb:
        if not said:
            say(f"waiting for {need_gb:.0f} GB free memory")
            said = True
        time.sleep(5)


# ---------------------------------------------------------------- Windows job object (memory cap)
if IS_WINDOWS:
    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class _Extended(ctypes.Structure):
        _fields_ = [("Basic", _Basic), ("Io", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _K32.CreateJobObjectW.restype = ctypes.c_void_p
    _K32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    _K32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _K32.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.c_void_p]
    _K32.CloseHandle.argtypes = [ctypes.c_void_p]
    _EXTENDED = 9
    _LIMIT_JOB_MEMORY, _KILL_ON_CLOSE = 0x200, 0x2000


def _cap_memory(proc, gb):
    if not IS_WINDOWS:  # pragma: no cover
        raise OSError("the memory cap needs Windows (job objects)")
    job = _K32.CreateJobObjectW(None, None)
    info = _Extended()
    info.Basic.LimitFlags = _LIMIT_JOB_MEMORY | _KILL_ON_CLOSE
    info.JobMemoryLimit = int(gb * 2**30)
    ok = _K32.SetInformationJobObject(job, _EXTENDED, ctypes.byref(info), ctypes.sizeof(info))
    ok = ok and _K32.AssignProcessToJobObject(job, ctypes.c_void_p(int(proc._handle)))
    if not ok:
        proc.kill()
        raise OSError(f"could not cap the job's memory (error {ctypes.get_last_error()})")
    return job


def _peak_gb(job):
    info = _Extended()
    _K32.QueryInformationJobObject(job, _EXTENDED, ctypes.byref(info), ctypes.sizeof(info), None)
    return info.PeakJobMemoryUsed / 2**30


def isolation_flags():
    """-I inside the tool's own venv (README); outside one, -E -P: no PYTHON* variables, no script or current folder
    on sys.path (a planted json.py next to game files is never imported), but the user's site-packages stay."""
    return ["-I"] if sys.prefix != getattr(sys, "base_prefix", sys.prefix) or sys.flags.isolated else ["-E", "-P"]


# ---------------------------------------------------------------- run
class Runner:
    def __init__(self, settings, log_dir, say=None):
        self.s = settings
        self.log_dir = log_dir
        self.say = say or (lambda m: print(f"[runner] {m}", file=sys.stderr, flush=True))

    def run(self, cmd, log_path=None, timeout=None, name="job", env=None, cwd=None):
        """Run one capped process. Returns a result dict (ok, code, peak_mem_gb, log, markers, result, tail)."""
        timeout = timeout or self.s.timeout_s
        os.makedirs(self.log_dir, exist_ok=True)
        if log_path is None:
            log_path = os.path.join(self.log_dir, f"{name}_{dt.datetime.now():%Y%m%d_%H%M%S_%f}.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        head = " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd) + "\n\n"
        slot = _take_slot(self.s.slot_dir, self.s.max_jobs, self.say)
        try:
            _wait_for_memory(self.s.mem_gb + self.s.free_margin_gb, self.say)
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(head + f"--- RUNNING since {dt.datetime.now():%H:%M:%S} (cap {self.s.mem_gb:g} GB) ---\n")
            t0 = time.time()
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                proc = subprocess.Popen([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT, env=env, cwd=cwd)
                job = _cap_memory(proc, self.s.mem_gb)
                try:
                    code = proc.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code, timed_out = proc.wait(), True
                peak = _peak_gb(job)
                _K32.CloseHandle(ctypes.c_void_p(job))
            seconds = round(time.time() - t0, 1)
        finally:
            slot.close()
        capped = code != 0 and peak > 0.97 * self.s.mem_gb
        with open(log_path, "a", encoding="utf-8") as f:
            if timed_out:
                f.write(f"\n--- TIMEOUT after {timeout}s ---")
            f.write(f"\n--- peak memory {peak:.1f} GB of {self.s.mem_gb:g} GB cap; {seconds} s ---\n")
            if capped:
                f.write("--- MEMORY CAP HIT: Windows stopped this job. Check the voxel/grid size against the part's size. ---\n")
        text = open(log_path, encoding="utf-8", errors="replace").read()
        markers, result = [], None
        for line in text.splitlines():
            if line.startswith("[stage] "):
                try:
                    markers.append(json.loads(line[8:]))
                except json.JSONDecodeError:
                    pass
            elif line.startswith("RESULT_JSON:"):
                try:
                    result = json.loads(line[len("RESULT_JSON:"):])
                except json.JSONDecodeError:
                    result = {"ok": False, "errors": ["bad RESULT_JSON"]}
        traceback_seen = "Traceback (most recent call last)" in text
        ok = code == 0 and not timed_out and not traceback_seen and (result is None or result.get("ok", True))
        errors = []
        if timed_out:
            errors.append(f"timed out after {timeout} s")
        if capped:
            errors.append(f"memory cap hit ({self.s.mem_gb:g} GB): voxel/grid too fine for this part?")
        if code != 0:
            errors.append(f"exit code {code}")
        if traceback_seen:
            errors.append("Python traceback in the log")
        tail = "\n".join(l for l in text.splitlines()[-40:] if l.strip())
        return {"ok": ok, "code": code, "peak_mem_gb": round(peak, 2), "memory_cap_hit": capped, "seconds": seconds,
                "log": log_path, "markers": markers, "result": result, "errors": errors, "tail": tail[-2500:]}

    def blender(self, script, args=(), blend=None, log_path=None, timeout=None, name=None):
        """Blender headless with the fixed flags. Raises RunError when the job did not succeed."""
        exe = self.s.blender
        if not exe or not os.path.exists(exe):
            raise RunError(f"Blender not found at {exe!r} (opentool.toml [paths] blender)")
        cmd = [exe, "-b"] + ([blend] if blend else []) + ["--factory-startup", "--python-exit-code", "1",
                                                          "--python", script, "--", *args]
        r = self.run(cmd, log_path=log_path, timeout=timeout, name=name or os.path.splitext(os.path.basename(script))[0])
        if not r["ok"]:
            raise RunError(f"{os.path.basename(script)} failed ({'; '.join(r['errors'])}), log {r['log']}\n{r['tail'][-1500:]}", r)
        return r

    def python(self, args, log_path=None, timeout=None, name="python", python=None):
        """A heavy venv-side step (FFT, voxel work) as its own capped process: python -I <args>."""
        r = self.run([python or sys.executable, *isolation_flags(), *args], log_path=log_path, timeout=timeout, name=name)
        if not r["ok"]:
            raise RunError(f"{name} failed ({'; '.join(r['errors'])}), log {r['log']}\n{r['tail'][-1500:]}", r)
        return r
