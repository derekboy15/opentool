# SPDX-License-Identifier: GPL-3.0-or-later
"""Unreal adapter tests on synthetic files (no game data): the encryption / compression / versioning gates."""
import os
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from adapters.unreal_cue4parse import adapter, gates  # noqa: E402


def write_container(paks, name, flags=0, method="None", unversioned=False, ue5=False):
    """A one-package IoStore container: .utoc header + chunk table + one block holding a package header."""
    ver = 6 if ue5 else 3
    hdr = bytearray(24)
    pkg_flags = 0x80000000 | (gates.PKG_UNVERSIONED if unversioned else 0)
    struct.pack_into("<I", hdr, 16, pkg_flags)
    raw = bytes(hdr) + bytes(40)
    data = zlib.compress(raw) if method == "Zlib" else raw
    nmeth = 0 if method == "None" else 1
    h = bytearray(144)
    h[:16] = gates.TOC_MAGIC
    h[16] = ver
    struct.pack_into("<9I", h, 20, 144, 1, 1, 12, nmeth, 32, 65536, 0, 1)
    h[80] = flags
    chunk_id = bytearray(12)
    chunk_id[11] = 1 if ue5 else 2  # ExportBundleData
    offl = (0).to_bytes(5, "big") + len(raw).to_bytes(5, "big")
    block = (0).to_bytes(5, "little") + len(data).to_bytes(3, "little") + len(raw).to_bytes(3, "little") + bytes([nmeth])
    names = (method.encode().ljust(32, b"\0")) if nmeth else b""
    with open(os.path.join(paks, name + ".utoc"), "wb") as f:
        f.write(bytes(h) + bytes(chunk_id) + offl + block + names)
    with open(os.path.join(paks, name + ".ucas"), "wb") as f:
        f.write(data)


class Gates(unittest.TestCase):
    def game(self, **kw):
        d = tempfile.mkdtemp()
        paks = os.path.join(d, "Game", "Content", "Paks")
        os.makedirs(paks)
        write_container(paks, "pakchunk0-Windows", **kw)
        return d

    def test_plain_versioned_ok(self):
        r = gates.probe(self.game(method="Zlib"))
        self.assertTrue(r["ok"], r["stops"])
        self.assertEqual(r["versioning_sample"]["versioned"], 1)

    def test_encrypted_out(self):
        r = gates.probe(self.game(flags=2))
        self.assertFalse(r["ok"])
        self.assertIn("encrypted", r["stops"][0])

    def test_unversioned_needs_usmap(self):
        g = self.game(unversioned=True, ue5=True)
        r = gates.probe(g)
        self.assertFalse(r["ok"])
        self.assertIn(".usmap", r["stops"][0])
        us = os.path.join(g, "mine.usmap")
        open(us, "wb").close()
        self.assertTrue(gates.probe(g, usmap=us)["ok"])

    def test_oodle_without_dll_out(self):
        r = gates.probe(self.game(method="Oodle"))
        self.assertFalse(r["ok"])
        self.assertIn("Oodle", r["stops"][0])


class Helpers(unittest.TestCase):
    def test_game_path(self):
        self.assertEqual(adapter.game_path("/Game/A/B.B", "Moria/Content"), "Moria/Content/A/B.B")
        self.assertEqual(adapter.game_path("Moria/Content/A/B.B", "Moria/Content"), "Moria/Content/A/B.B")

    def test_clip_role(self):
        rules = [["Idle", "idle"], ["Atk", "melee"], [".*", "other"]]
        self.assertEqual(adapter.clip_role("Orc_Swd2H_Combat_Atk_Kick", rules), "melee")
        self.assertEqual(adapter.clip_role("X_Idle_Loop", rules), "idle")
        self.assertEqual(adapter.clip_role("Whatever", rules), "other")


if __name__ == "__main__":
    unittest.main()
