# SPDX-License-Identifier: GPL-3.0-or-later
"""opentool doctor: is this Blender 5.2 and does it run headless?"""
import json

import bpy

ok = bpy.app.version[:2] == (5, 2)
print("RESULT_JSON:" + json.dumps({"ok": ok, "version": bpy.app.version_string,
                                   "errors": [] if ok else [f"Blender 5.2.x needed, found {bpy.app.version_string}"]}))
