# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract and config tests. No meshes, no game data. Run: python -I -m unittest discover -s tests (repo folder)."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opentool import config as C  # noqa: E402
from opentool import contract  # noqa: E402
from adapters.relic_essence1 import archives, whm  # noqa: E402


def minimal_package(d):
    os.makedirs(os.path.join(d, "model"))
    open(os.path.join(d, "model", "m.glb"), "wb").close()
    pkg = {"contract": "1.0", "adapter": {"name": "t", "version": "0"}, "reader": {"name": "r", "version": "0"},
           "game": "g", "unit": {"id": "u", "name": "u", "group": "g", "class_hint": "infantry", "class_confidence": "high"},
           "model": "model/m.glb", "format": "glb", "units": {"metres_per_unit": 1.0}, "up_axis": "Z", "handedness": "right",
           "bind_pose_ok": True, "rig_map": {"pelvis": "hips"}, "clips": [{"name": "idle", "role": "idle"}],
           "mesh_groups": [{"name": "body", "role": "body"}, {"name": "gun", "role": "weapon"}], "hidden": [], "weapons": [],
           "loadouts": [{"id": "default", "mesh_groups": ["body"], "weapons": [{"slot": 1, "weapon_id": "gun", "mount": "mesh_group:gun"}]}],
           "default_loadout": "default", "textures": [], "provenance": {}, "credits": [], "warnings": []}
    return pkg


def write(d, pkg):
    with open(os.path.join(d, "package.json"), "w") as f:
        json.dump(pkg, f)


class PackageContract(unittest.TestCase):
    def test_minimal_package_passes(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, minimal_package(d))
            errs, warns, _ = contract.check_package(d)
            self.assertEqual(errs, [])
            self.assertTrue(any("ankle.L" in w for w in warns))  # missing roles are reported, not fatal

    def test_bad_mount_and_paths_fail(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = minimal_package(d)
            pkg["loadouts"][0]["weapons"][0]["mount"] = "glue:gun"
            write(d, pkg)
            self.assertTrue(contract.check_package(d)[0])
            pkg = minimal_package(tempfile.mkdtemp())
            pkg["model"] = "C:/abs/m.glb"
            write(d, pkg)
            self.assertTrue(contract.check_package(d)[0])

    def test_joint_mount_needs_weapon_file(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = minimal_package(d)
            pkg["loadouts"][0]["weapons"][0]["mount"] = "joint:hand_r"
            write(d, pkg)
            errs = contract.check_package(d)[0]
            self.assertTrue(any("no weapon file" in e for e in errs))

    def test_units_schema(self):
        s = contract.load_schema("units.schema.json")
        u = {"contract": "1.0", "game": "g", "adapter": {"name": "a", "version": "0"}, "units": [
            {"id": "x", "name": "x", "group": "g", "class_hint": "tank", "confidence": "high", "models": [], "loadouts": [],
             "default_loadout": "", "clips": [], "provenance": {}, "credits": []}]}
        self.assertTrue(any("tank" in e for e in contract.validate(u, s)))


class Config(unittest.TestCase):
    def test_deep_merge(self):
        a = {"g": {"voxel_mm": 0.05, "levels": 4}, "x": [1]}
        b = {"g": {"voxel_mm": 0.06}, "x": [2]}
        self.assertEqual(C.deep_merge(a, b), {"g": {"voxel_mm": 0.06, "levels": 4}, "x": [2]})

    def test_profiles_load(self):
        for g in ("battlesector", "dow1"):
            p = C.game_profile(g)
            self.assertIn(p["adapter"], ("unity_assetstudio", "relic_essence1"))
            for role in p["rig_map"]:
                self.assertTrue(role in contract.ROLES or role.startswith(("weapon.", "_doc")), role)
        self.assertIn("printer", C.printer_profile("p2s_0.2"))
        self.assertEqual(C.style_profile("derek")["base_finish"]["top"], "dome")

    def test_regression_metrics_only(self):
        """The repo ships the Intercessor's metrics, never the mesh."""
        r = C.game_profile("battlesector")["regression"]
        self.assertEqual(r["reference_metrics"]["shells"], 1)
        self.assertAlmostEqual(r["reference_metrics"]["volume_mm3"], 7939.454, places=2)

    def test_runner_caps_clamped(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.toml")
            with open(p, "w") as f:
                f.write('[paths]\nworkspace = ""\n[runner]\nmax_jobs = 9\nmem_gb = 64\nfree_margin_gb = 1\n')
            s = C.Settings(p)
            self.assertEqual((s.max_jobs, s.mem_gb, s.free_margin_gb), (3, 24.0, 4.0))

    def test_workspace_outside_repo(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.toml")
            with open(p, "w") as f:
                f.write(f'[paths]\nworkspace = "{os.path.join(ROOT, "ws").replace(os.sep, "/")}"\n')
            with self.assertRaises(C.ConfigError):
                C.Settings(p).require_workspace()


class Relic(unittest.TestCase):
    def test_module_order(self):
        with tempfile.TemporaryDirectory() as d:
            for mod, folder, arcs, req in (("A.module", "A", ["%LOCALE%\\ALoc", "AData", "AData-Whm-%MODEL-LEVEL%", "AData-Sound-Full"], ["B"]),
                                           ("B.module", "B", ["BData"], [])):
                with open(os.path.join(d, mod), "w") as f:
                    f.write(f"[global]\nModFolder = {folder}\n" + "".join(f"ArchiveFile.{i} = {a}\n" for i, a in enumerate(arcs, 1))
                            + "".join(f"RequiredMod.{i} = {r}\n" for i, r in enumerate(req, 1)))
                os.makedirs(os.path.join(d, folder), exist_ok=True)
                for a in arcs:
                    open(os.path.join(d, folder, a.split("\\")[-1].replace("%MODEL-LEVEL%", "High") + ".sga"), "w").close()
            os.makedirs(os.path.join(d, "Engine"))
            open(os.path.join(d, "Engine", "Engine.sga"), "w").close()
            tags = [t for _, t in archives.archive_order(d, "A.module")]
            self.assertEqual(tags, ["A__AData", "A__AData-Whm-High", "B__BData", "Engine__Engine"])

    def test_vis_group_key(self):
        self.assertEqual(whm.group_key("vis_head01"), ("head", 1))
        self.assertEqual(whm.mesh_role("guardsmen_lasgun"), "weapon")
        self.assertEqual(whm.mesh_role("guardsmen_head02"), "head")


class Source(unittest.TestCase):
    def test_gameinfo_order_and_vpk_names(self):
        import struct
        from adapters.source_sourceio import vpk
        with tempfile.TemporaryDirectory() as d:
            for sub in ("mod", "mod_dlc1", "hl2", "update"):
                os.makedirs(os.path.join(d, sub))
            with open(os.path.join(d, "mod", "gameinfo.txt"), "w") as f:
                f.write('"GameInfo" { FileSystem { SearchPaths {\n Game update\n Game mod_dlc1 // dlc\n'
                        ' Game |gameinfo_path|.\n Game hl2\n Game missing } } }')
            self.assertEqual([os.path.basename(p) for p in vpk.search_paths(os.path.join(d, "mod"))],
                             ["update", "mod_dlc1", "mod", "hl2"])

            def tree(entries):  # {ext: {folder: [names]}} -> VPK v2 directory tree bytes
                b = bytearray()
                for ext, folders in entries.items():
                    b += ext.encode() + b"\0"
                    for folder, names in folders.items():
                        b += folder.encode() + b"\0"
                        for n in names:
                            b += n.encode() + b"\0" + struct.pack("<IHHIIH", 0, 0, 0, 0, 0, 0xFFFF)
                        b += b"\0"
                    b += b"\0"
                return bytes(b + b"\0")
            for sub, entries in (("update", {"mdl": {"models/survivors": ["nick"]}}),
                                 ("mod", {"mdl": {"models/survivors": ["nick", "ellis"]}, "vtf": {"materials": ["a"]}})):
                t = tree(entries)
                with open(os.path.join(d, sub, "pak01_dir.vpk"), "wb") as f:
                    f.write(struct.pack("<III", vpk.VPK_SIG, 2, len(t)) + b"\0" * 16 + t)
            files = vpk.list_files(os.path.join(d, "mod"), ("models/",))
            self.assertEqual(files, {"models/survivors/nick.mdl": "update/pak01_dir.vpk",
                                     "models/survivors/ellis.mdl": "mod/pak01_dir.vpk"})

    def test_bonemerge_needs_mount_joint(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = minimal_package(d)
            os.makedirs(os.path.join(d, "weapons"))
            open(os.path.join(d, "weapons", "gun.glb"), "wb").close()
            pkg["weapons"] = [{"id": "gun", "file": "weapons/gun.glb"}]
            pkg["loadouts"][0]["weapons"][0]["mount"] = "bonemerge"
            write(d, pkg)
            self.assertTrue(any("mount_joint" in e for e in contract.check_package(d)[0]))
            pkg["weapons"][0]["mount_joint"] = "bm_ValveBiped.weapon_bone"
            write(d, pkg)
            self.assertEqual(contract.check_package(d)[0], [])

    def test_l4d2_profile(self):
        p = C.game_profile("l4d2")
        self.assertEqual(p["adapter"], "source_sourceio")
        from adapters.source_sourceio import adapter as A
        rules = p["source"]["activity_roles"]
        role = lambda act: A.act_role({"activity": act, "name": ""}, rules)  # noqa: E731
        self.assertEqual([role(a) for a in ("ACT_IDLE_RIFLE", "ACT_MELEE_Sweep_Rifle_IDLE", "ACT_RUN_RIFLE", "ACT_DIERAGDOLL")],
                         ["idle", "melee", "move", "die"])


if __name__ == "__main__":
    unittest.main()
