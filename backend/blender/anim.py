# SPDX-License-Identifier: GPL-3.0-or-later
"""Animation helpers for the pose stages: load a package model with its clips, switch clips, attach weapons.

FBX (Unity readers): AssetStudioModCLI's Animator export with bound clips, one FBX "take" per clip; Blender
imports each take as one action per animated object ("<object>|<clip>|Base Layer").
glTF (other adapters): one action per clip, with one slot per animated object (Blender 5.2 slotted actions).
Weapons are attached the way the game does it: the weapon prefab sits on an attach joint
(e.g. weapon1_AuxSHJnt), so world = joint @ weapon-root-local.
"""
import re
from collections import defaultdict

import bpy
from mathutils import Matrix


def _norm_bone(name):
    return re.sub(r"legs_", "leg_", name.lower())


def find_bone(arm, suffix):
    """None for no name. Pose bone whose name ends in suffix; else the same ignoring case and leg/legs (Tyranid rigs:
    HiveTyrant_l_legs_AnkleSHJnt for the usual l_Leg_AnkleSHJnt)."""
    if not suffix:
        return None
    b = next((b for b in arm.pose.bones if b.name.endswith(suffix)), None)
    if b is None:
        k = _norm_bone(suffix)
        b = next((b for b in arm.pose.bones if _norm_bone(b.name).endswith(k)), None)
    return b


def bone_matches(name, suffix):
    return name.endswith(suffix) or _norm_bone(name).endswith(_norm_bone(suffix))


def import_model(path):
    """Import a package model (FBX or glTF) into an empty scene, exactly as every stage does."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if path.lower().endswith((".glb", ".gltf")):
        bpy.ops.import_scene.gltf(filepath=path, import_shading="NORMALS", bone_heuristic="BLENDER",
                                  guess_original_bind_pose=False)
        shapes = {pb.custom_shape for o in bpy.data.objects if o.type == "ARMATURE" for pb in o.pose.bones if pb.custom_shape}
        for o in bpy.data.objects:  # the importer's bone display shape (an Icosphere) is not part of the model
            if o.type == "ARMATURE":
                for pb in o.pose.bones:
                    pb.custom_shape = None
        for sh in shapes:
            bpy.data.objects.remove(sh, do_unlink=True)
        for o in bpy.data.objects:  # glTF importers keep animations on NLA tracks; we switch actions by hand
            if o.animation_data:
                for t in list(o.animation_data.nla_tracks):
                    o.animation_data.nla_tracks.remove(t)
                o.animation_data.action = None
    else:
        bpy.ops.import_scene.fbx(filepath=path, automatic_bone_orientation=True)


def load_animated(path, fps=25):
    """Import the animated model. Returns (armature, {clip: {object_name: action}})."""
    import_model(path)
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    clips = defaultdict(dict)
    gltf = path.lower().endswith((".glb", ".gltf"))
    for a in bpy.data.actions:
        if gltf:
            for sl in a.slots:
                ob = bpy.data.objects.get(sl.name_display)
                if ob is not None:
                    clips[a.name][ob.name] = a
            if not clips.get(a.name) and arm is not None:
                clips[a.name][arm.name] = a
            continue
        parts = a.name.split("|")
        if len(parts) >= 2:
            clips[parts[1]][parts[0]] = a
    bpy.context.scene.render.fps = fps
    return arm, dict(clips)


def clip_range(clips, clip, arm):
    acts = clips[clip]
    a = acts.get(arm.name, next(iter(acts.values())))
    return int(round(a.frame_range[0])), int(round(a.frame_range[1]))


def set_clip(clips, clip):
    """Assign the clip's action to every object it animates."""
    for name, act in clips[clip].items():
        ob = bpy.data.objects.get(name)
        if ob is None:
            continue
        if ob.animation_data is None:
            ob.animation_data_create()
        ob.animation_data.action = act
        if hasattr(ob.animation_data, "action_slot") and act.slots:
            sl = next((x for x in act.slots if x.name_display == ob.name), None)
            ob.animation_data.action_slot = sl or act.slots[0]


def base_name(o):
    n = o.name
    return n.rsplit(".", 1)[0] if "." in n and n.rsplit(".", 1)[1].isdigit() else n


def obj_path(o, depth=4):
    """Names from the object up its parents (objects, then bones for bone-parented objects), nearest last,
    to compare with the prefab's GameObject paths."""
    names, cur = [base_name(o)], o
    while cur.parent is not None and len(names) < depth:
        if cur.parent_type == "BONE" and cur.parent_bone:
            b = cur.parent.data.bones.get(cur.parent_bone)
            while b is not None and len(names) < depth:
                names.append(b.name)
                b = b.parent
            break
        cur = cur.parent
        names.append(base_name(cur))
    return names[::-1]


def is_hidden(o, hidden_paths, k=3):
    """True if the object's path tail (name + up to k-1 parents) ends one of the prefab's hidden paths."""
    p = obj_path(o, k)
    for h in hidden_paths:
        h = h if isinstance(h, list) else [h]
        n = min(len(p), len(h))
        if n and h[-n:] == p[-n:]:
            return True
    return False


def remove_tree(o):
    for c in list(o.children):
        remove_tree(c)
    bpy.data.objects.remove(o)


def mesh_objects(o):
    """o itself if it is a mesh, plus every mesh below it."""
    out = [o] if o.type == "MESH" else []
    for c in o.children_recursive:
        if c.type == "MESH":
            out.append(c)
    return out


def attach_weapon(arm, fbx, joint, name, keep_root_offset=False, hidden=()):
    """Import a weapon FBX and parent it to an attach joint like Unity does.

    The game mounts the weapon by its own pivot on the joint. The exported weapon FBX carries an extra root
    translation (3.9 mm toward the stock for the bolt rifle) that the game does not apply; keeping it puts the
    magazine in the trigger hand and the stock into the chest. Drop it by default.
    A single-mesh FBX (model asset) is mounted as the mesh itself; a weapon prefab FBX (several parts, empties)
    is mounted at its root with the parts' prefab offsets kept, minus the objects the prefab keeps inactive.
    The import matrix is stored on the mounted object ("import_matrix") so weapon-frame points can be mapped.
    """
    before = set(bpy.data.objects)
    if fbx.lower().endswith((".glb", ".gltf")):
        bpy.ops.import_scene.gltf(filepath=fbx)
    else:
        bpy.ops.import_scene.fbx(filepath=fbx)
    new = [o for o in bpy.data.objects if o not in before]
    hidden = set(hidden)
    names = [o.name for o in new]
    for nm in [o.name for o in new if is_hidden(o, hidden)]:
        if nm in bpy.data.objects:
            remove_tree(bpy.data.objects[nm])
    new = [bpy.data.objects[nm] for nm in names if nm in bpy.data.objects]
    meshes = [o for o in new if o.type == "MESH"]
    roots = [o for o in new if o.parent is None]
    if len(meshes) == 1 and meshes[0] in roots:
        weapon = meshes[0]  # model asset FBX: the mesh is the root (v1 path)
        for o in new:
            if o is not weapon:
                bpy.data.objects.remove(o)
    else:
        weapon = roots[0] if len(roots) == 1 else max(roots, key=lambda r: len(mesh_objects(r)))
        for r in roots:
            if r is not weapon:
                remove_tree(r)
    weapon.name = name
    # both imports share the same axis/unit conversion Cg (= the armature root's world matrix);
    # the weapon's import matrix is Cg @ L, so its local offset on the joint is Cg^-1 @ import matrix
    Cg = arm.matrix_world.copy()
    R0 = weapon.matrix_world.copy()
    weapon["import_matrix"] = [x for row in R0 for x in row]
    local = Cg.inverted() @ R0
    if not keep_root_offset:
        local.translation = (0, 0, 0)
    j = bpy.data.objects[joint] if joint in bpy.data.objects else None
    weapon.parent = j
    weapon.matrix_parent_inverse = Matrix.Identity(4)
    weapon.matrix_basis = local
    return weapon


def weapon_point(weapon, q):
    """Weapon import-frame point (world units, as measured on the imported FBX) -> current world."""
    R0 = Matrix([weapon["import_matrix"][i * 4:i * 4 + 4] for i in range(4)])
    return weapon.matrix_world @ R0.inverted() @ q


def apply_mesh_groups(cfg, log=print):
    """Mesh groups (DoW / Source bodygroups): keep the groups the loadout shows, delete the rest. Objects that are
    not a declared mesh group always stay. Returns (shown, removed)."""
    groups = {g["name"] for g in cfg.get("mesh_groups") or []}
    if not groups:
        return [], []
    show = set(cfg.get("show_groups") or [])
    removed = []
    for o in list(bpy.data.objects):
        if o.type == "MESH" and o.name in groups and o.name not in show:
            removed.append(o.name)
            remove_tree(o)
    shown = sorted(g for g in groups if g in show and g in bpy.data.objects)
    log(f"mesh groups: showing {len(shown)} ({', '.join(shown)}), removed {len(removed)}")
    return shown, sorted(removed)
