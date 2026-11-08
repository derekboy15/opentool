# SPDX-License-Identifier: GPL-3.0-or-later
"""Layered configuration (DESIGN.md section 5).

1. opentool.toml (per PC): Blender, workspace, readers, runner caps, game installs.
2. profiles/printers/<printer>.json: build volume, nozzle, print thresholds.
   (optional) profiles/styles/<style>.json: a named set of taste rules (scale rule, base style, ...).
3. profiles/games/<game>.json (+ profiles/games/<game>/units|weapons/...): adapter, rig map, clip roles,
   class rules, scale rule, per-unit fixes that ship with the tool.
4. <workspace>/<game>/units/<group>/<unit>.json: the user's own per-unit fixes, then job overrides.

A job's config is backend/defaults.json <- printer <- style <- game "backend" <- game class <- weapons
<- shipped unit file <- workspace unit file <- job overrides (deep merge: dicts merge, everything else replaces).
"""
import copy
import json
import os
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the repo folder
PROFILES = os.path.join(ROOT, "profiles")
BACKEND = os.path.join(ROOT, "backend")
ADAPTERS = os.path.join(ROOT, "adapters")
HARD_MAX_MEM_GB = 24.0


class ConfigError(RuntimeError):
    pass


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def strip_docs(d):
    """Drop "_doc*" keys (comments inside JSON files) so they never reach a stage."""
    if isinstance(d, dict):
        return {k: strip_docs(v) for k, v in d.items() if not k.startswith("_doc")}
    if isinstance(d, list):
        return [strip_docs(x) for x in d]
    return d


# ---------------------------------------------------------------- opentool.toml
class Settings:
    def __init__(self, path=None):
        self.path = path or os.environ.get("OPENTOOL_TOML") or os.path.join(ROOT, "opentool.toml")
        if not os.path.exists(self.path):
            raise ConfigError(f"no {self.path}: copy opentool.toml from the repo and fill in your paths")
        with open(self.path, "rb") as f:
            self.d = tomllib.load(f)
        p = self.d.get("paths", {})
        self.blender = p.get("blender") or ""
        self.workspace = p.get("workspace") or ""
        r = self.d.get("runner", {})
        self.max_jobs = max(1, min(int(r.get("max_jobs", 3)), 3))
        self.mem_gb = min(float(r.get("mem_gb", 16)), HARD_MAX_MEM_GB)
        self.free_margin_gb = max(float(r.get("free_margin_gb", 4)), 4.0)
        self.slot_dir = r.get("slot_dir") or os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                                                          "opentool", "slots")
        self.timeout_s = int(float(r.get("timeout_min", 240)) * 60)
        self.readers = self.d.get("readers", {})
        self.games = self.d.get("games", {})
        self.style = (self.d.get("style") or {}).get("profile") or ""
        self.printer = (self.d.get("printer") or {}).get("profile") or "p2s_0.2"

    def require_workspace(self):
        if not self.workspace:
            raise ConfigError("opentool.toml [paths] workspace is empty: set a folder outside the repo")
        ws = os.path.abspath(self.workspace)
        if os.path.normcase(ws).startswith(os.path.normcase(ROOT) + os.sep) or os.path.normcase(ws) == os.path.normcase(ROOT):
            raise ConfigError("the workspace must be outside the repo (nothing extracted may end up in it)")
        os.makedirs(ws, exist_ok=True)
        return ws

    def game(self, game_id):
        g = self.games.get(game_id)
        if g is None:
            raise ConfigError(f"game {game_id!r} is not in opentool.toml [games.*] (known: {', '.join(self.games) or 'none'})")
        install = g.get("install") or ""
        if not install or not os.path.isdir(install):
            raise ConfigError(f"[games.{game_id}] install = {install!r} is not a folder")
        return {"id": game_id, "install": os.path.abspath(install), "profile": g.get("profile") or game_id,
                **{k: v for k, v in g.items() if k not in ("install", "profile")}}

    def reader(self, key):
        v = self.readers.get(key) or ""
        if not v:
            raise ConfigError(f"opentool.toml [readers] {key} is empty (see README: readers are downloaded by you)")
        if not os.path.exists(v):
            raise ConfigError(f"[readers] {key} = {v!r} does not exist")
        return os.path.abspath(v)


def game_dir(settings, game_id):
    d = os.path.join(settings.require_workspace(), game_id)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- profiles
def game_profile(name):
    p = os.path.join(PROFILES, "games", f"{name}.json")
    d = read_json(p)
    if d is None:
        raise ConfigError(f"no game profile {p}")
    return d


def printer_profile(name):
    d = read_json(os.path.join(PROFILES, "printers", f"{name}.json"))
    if d is None:
        raise ConfigError(f"no printer profile profiles/printers/{name}.json")
    return d


def style_profile(name):
    if not name:
        return {}
    d = read_json(os.path.join(PROFILES, "styles", f"{name}.json"))
    if d is None:
        raise ConfigError(f"no style profile profiles/styles/{name}.json")
    return d


def unit_files(profile_name, ws_game_dir, group, unit):
    """Shipped per-unit fixes (profiles/games/<game>/units/...) then the user's (workspace)."""
    rel = os.path.join("units", group or "_", f"{unit}.json")
    return [read_json(os.path.join(PROFILES, "games", profile_name, rel), {}),
            read_json(os.path.join(ws_game_dir, rel), {}) if ws_game_dir else {}]


def weapon_file(profile_name, weapon):
    return read_json(os.path.join(PROFILES, "games", profile_name, "weapons", f"{weapon}.json"), {})


def build(settings, game, unit_class, group=None, unit=None, weapons=(), job=None, ws_game_dir=None, style=None):
    """Fully merged stage config for one unit / job."""
    prof = game_profile(game["profile"])
    cfg = read_json(os.path.join(BACKEND, "defaults.json"))
    cfg = deep_merge(cfg, printer_profile(settings.printer))
    st = style if style is not None else (game.get("style") if game.get("style") is not None else settings.style)
    cfg = deep_merge(cfg, style_profile(st))
    cfg = deep_merge(cfg, prof.get("backend", {}))
    ufiles = unit_files(game["profile"], ws_game_dir, group, unit) if unit else [{}, {}]
    cls = next((u.get("class") for u in reversed(ufiles) if u.get("class")), None) or unit_class
    cfg = deep_merge(cfg, (cfg.get("classes") or {}).get(cls, {}))
    cfg = deep_merge(cfg, (prof.get("classes") or {}).get(cls, {}))
    for w in weapons:
        cfg = deep_merge(cfg, weapon_file(game["profile"], w))
    for u in ufiles:
        cfg = deep_merge(cfg, u)
    cfg = deep_merge(cfg, job or {})
    cfg.pop("classes", None)
    cfg["class"] = cls
    cfg["style_profile"] = st or "neutral"
    cfg = resolve_base(cfg)
    return strip_docs(cfg)


def resolve_base(cfg):
    """base_size (unit/class/style) -> concrete base dict with the finish merged in."""
    if cfg.get("base") is False:
        cfg["base"] = None
        return cfg
    size = cfg.get("base_size") or cfg.get("base_size_fallback")
    cfg["base_size_used"] = size
    cfg["base_size_is_fallback"] = not cfg.get("base_size")
    spec = (cfg.get("base_sizes") or {}).get(size)
    if spec is None:
        cfg["base"] = None
        return cfg
    if spec.get("shape") in ("round", "oval"):
        cfg["base"] = deep_merge(deep_merge(cfg.get("base_finish") or {}, spec), cfg.get("base") or {})
    else:
        cfg["base"] = deep_merge(spec, cfg.get("base") or {})
    return cfg
