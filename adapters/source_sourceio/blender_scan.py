# SPDX-License-Identifier: GPL-3.0-or-later
"""Source scan (runs in Blender 5.2 through the capped runner): headers of many .mdl files, no geometry.

cfg: addon_parent, game_dir, models [paths], out. For each model: bones, attachments, body groups, include models
and its sequences (with the game's activity names, from the model and its $includemodel files). Writes cfg.out.
"""
import json
import os
import sys

import bpy

assert bpy.app.version[:2] == (5, 2), f"Blender 5.2 needed, this is {bpy.app.version_string}"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sio  # noqa: E402

cfg = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
sio.enable(cfg["addon_parent"])
cm = sio.mount(cfg["game_dir"])
res, errs = {}, {}
for p in cfg["models"]:
    try:
        m, _ = sio.read_mdl(cm, p)
        if m is None:
            errs[p] = "not mounted"
            continue
        res[p] = {"bones": [b.name for b in m.bones],
                  "attachments": [[a.name, m.bones[a.parent_bone].name if 0 <= a.parent_bone < len(m.bones) else None]
                                  for a in m.attachments],
                  "bodygroups": [[bp.name, [md.name for md in bp.models]] for bp in m.body_parts],
                  "include": list(m.include_models), "sequences": sio.sequences(cm, m, p)}
    except Exception as e:  # noqa: BLE001
        errs[p] = repr(e)[:300]
with open(cfg["out"], "w", encoding="utf-8") as f:
    json.dump({"models": res, "errors": errs}, f)
print("RESULT_JSON:" + json.dumps({"ok": True, "models": len(res), "errors": len(errs)}))
