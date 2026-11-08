# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Blender-side helpers for the Source adapter: enable SourceIO for this run only and mount the game.

SourceIO (REDxEYE, MIT) is a Blender add-on; it is never installed into the user's Blender. Its folder's parent
goes on sys.path and the add-on is enabled for this process. Its content manager mounts the game from
gameinfo.txt (the game's own search paths and VPKs) and reads every file in place: nothing is unpacked.
"""
import sys
import traceback


def enable(addon_parent):
    sys.path.insert(0, addon_parent)
    import addon_utils
    if not addon_utils.enable("SourceIO", default_set=True, handle_error=lambda e: traceback.print_exc()):
        raise RuntimeError("SourceIO failed to enable ([readers] sourceio = the SourceIO add-on folder or its release zip)")
    import logging
    logging.raiseExceptions = False  # SourceIO logs into a Blender text block that is gone at exit
    patch_frame_anims()


def patch_frame_anims():
    """SourceIO 5.5.4 reads a STUDIO_FRAMEANIM block (Left 4 Dead 2 / Alien Swarm-era animations) as *either*
    constants *or* per-frame data, and leaves bones' unflagged channels at zero. Valve's mstudio_frame_anim_t holds
    both: per bone a flag byte, constants (RAWROT Quaternion48 / CONST_ROT2 Quaternion48S, then RAWPOS Vector48 /
    CONST_POS2 Vector) at constantsoffset, and per frame (ANIMROT Quaternion48 / ANIM_ROT2 Quaternion48S, then
    ANIMPOS Vector48 / FULLANIMPOS Vector) at frameoffset + frame * framelength. A channel with no flag keeps the
    bone's own base position / rotation. Patched at run time; the add-on stays as downloaded."""
    import numpy as np
    from SourceIO.library.models.mdl.structs import local_animation as LA
    from SourceIO.library.models.mdl.structs.compressed_vectors import Quat48, Quat48S
    from SourceIO.library.models.mdl.structs.frame_anim import StudioFrameAnim
    F = LA.AniBoneFlags

    def read_frame_animations(self, buffer, bones, n):
        entry = buffer.tell()
        fa = StudioFrameAnim.from_buffer(buffer)
        flags = [int(buffer.read_uint8()) for _ in bones]
        out = {}
        for b in bones:
            a = np.zeros((n,), LA.ANIM_DTYPE)
            a["pos"] = tuple(b.position)
            a["rot"] = tuple(b.quat)
            out[b.name] = a
        if fa.constant_offset > 0:
            buffer.seek(entry + fa.constant_offset)
            for b, f in zip(bones, flags):
                if f & F.RAW_ROT:
                    out[b.name]["rot"] = Quat48.read(buffer)
                if f & F.CONST_ROT2:
                    out[b.name]["rot"] = Quat48S.read(buffer)
                if f & F.RAW_POS:
                    out[b.name]["pos"] = buffer.read_fmt("3e")
                if f & F.CONST_POS2:
                    out[b.name]["pos"] = buffer.read_fmt("3f")
        if fa.frame_offset > 0 and fa.frame_length > 0:
            for k in range(n):
                buffer.seek(entry + fa.frame_offset + k * fa.frame_length)
                for b, f in zip(bones, flags):
                    if f & F.ANIM_ROT:
                        out[b.name][k]["rot"] = Quat48.read(buffer)
                    if f & F.ANIM_ROT2:
                        out[b.name][k]["rot"] = Quat48S.read(buffer)
                    if f & F.ANIM_POS:
                        out[b.name][k]["pos"] = buffer.read_fmt("3e")
                    if f & F.FULL_ANIM_POS:
                        out[b.name][k]["pos"] = buffer.read_fmt("3f")
        return out

    LA.StudioAnimDesc._read_frame_animations = read_frame_animations


def mount(game_dir):
    from SourceIO.library.shared.content_manager import ContentManager
    from SourceIO.library.utils.tiny_path import TinyPath
    cm = ContentManager()
    cm.scan_for_content(TinyPath(game_dir) / "gameinfo.txt")
    return cm


def read_mdl(cm, path):
    """Parsed MDL (v44-v49 parser; L4D2/TF2/HL2 models are v44-v49) or None if the file is not mounted."""
    from SourceIO.library.models.mdl.v49 import MdlV49
    from SourceIO.library.utils.tiny_path import TinyPath
    b = cm.find_file(TinyPath(path))
    if b is None:
        return None, None
    ident, version = b.read_fmt("4sI")
    b.seek(0)
    if ident != b"IDST" or not 44 <= version <= 49:
        raise ValueError(f"{path}: {ident!r} v{version} is not an MDL v44-v49")
    return MdlV49.from_buffer(b), b


def sequences(cm, mdl, own_path):
    """[{name, activity, flags, anim (anim desc name), blends, fps, frames, delta, source}] over the model and its
    $includemodel files (where survivors keep their animations), first definition of a name wins."""
    out, seen = [], set()

    def add(m, src):
        for s in m.sequences:
            if s.name.lower() in seen:
                continue
            seen.add(s.name.lower())
            idx = [i for i in s.anim_desc_indices if 0 <= i < len(m.anim_descs)]
            ad = m.anim_descs[idx[0]] if idx else None
            delta = bool(int(s.flags) & 0x4) or bool(ad is not None and int(ad.flags) & 0x4)
            out.append({"name": s.name, "activity": s.activity_name or "", "flags": int(s.flags),
                        "anim": ad.name if ad else None, "blends": len(idx), "fps": round(float(ad.fps), 3) if ad else 30.0,
                        "frames": int(ad.frame_count) if ad else 0, "delta": delta, "source": src})

    add(mdl, own_path)
    for inc in mdl.include_models:
        try:
            im, _ = read_mdl(cm, inc)
        except Exception:  # noqa: BLE001  (an include the game itself fails on is skipped)
            im = None
        if im is not None:
            add(im, inc)
    return out
