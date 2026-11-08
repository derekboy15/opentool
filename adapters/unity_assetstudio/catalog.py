# SPDX-License-Identifier: GPL-3.0-or-later
"""Read Unity Addressables 2.x binary catalog (StreamingAssets/aa/catalog.bin).

Gives, for every key (asset GUID or address): its locations - internal id (asset path inside a
bundle, or a bundle file), provider, and the bundles it depends on. Used to resolve the game data
tables' AssetReference GUIDs (unit model prefabs, weapon prefabs) to asset paths and bundle files.

Format (BinaryStorageBuffer): every object is preceded by its byte size (uint32). Offsets carry
flags: 0x80000000 = UTF-16 string, 0x40000000 = dynamic string (linked list of parts, last part
first, joined by a separator). Header: magic, version, keysOffset, ... ; keys = array of
(keyObjectOffset, locationSetOffset); key object = (typeOffset, valueOffset); location =
(primaryKey, internalId, provider, dependencySet, dependencyHash, extraData, type).
"""
import struct

MASK = 0x3FFFFFFF
NONE = 0xFFFFFFFF


class Catalog:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.b = f.read()
        magic, version, keys_off = struct.unpack_from("<iiI", self.b, 0)
        if magic != 0x0DE38942:
            raise ValueError(f"not an Addressables binary catalog: {path}")
        self.version = version
        self._str_cache = {}
        self._loc_cache = {}
        self.keys = {}  # key string -> [location offset]
        words = self._u32array(keys_off)
        for i in range(0, len(words) - 1, 2):
            key = self._key(words[i])
            if key is None:
                continue
            self.keys.setdefault(key, []).extend(self._u32array(words[i + 1]))

    def _u32(self, o):
        return struct.unpack_from("<I", self.b, o)[0]

    def _u32array(self, off):
        if off == NONE:
            return []
        o = off & MASK
        n = self._u32(o - 4) // 4
        return list(struct.unpack_from(f"<{n}I", self.b, o))

    def _plain(self, off):
        o = off & MASK
        n = self._u32(o - 4)
        raw = self.b[o:o + n]
        return raw.decode("utf-16-le", "replace") if off & 0x80000000 else raw.decode("latin-1")

    def string(self, off, sep="/"):
        if off == NONE:
            return None
        k = (off, sep)
        if k in self._str_cache:
            return self._str_cache[k]
        if off & 0x40000000:
            parts, o = [], off & MASK
            while True:
                parts.append(self._plain(self._u32(o)))
                nxt = self._u32(o + 4)
                if nxt == NONE:
                    break
                o = nxt & MASK
            s = sep.join(reversed(parts))
        else:
            s = self._plain(off)
        self._str_cache[k] = s
        return s

    def _key(self, off):
        """Key object = (typeOffset, valueOffset). Only string keys matter here (GUIDs, addresses)."""
        t, v = self._u32(off & MASK), self._u32((off & MASK) + 4)
        try:
            tname = self.string(self._u32((t & MASK) + 4), ".")
        except Exception:
            return None
        if tname != "System.String":
            return None
        try:
            return self.string(self._u32(v & MASK))
        except Exception:
            return None

    def location(self, off):
        if off in self._loc_cache:
            return self._loc_cache[off]
        pk, iid, prov, deps = struct.unpack_from("<4I", self.b, off & MASK)
        loc = {
            "primary_key": self.string(pk),
            "internal_id": self.string(iid),
            "provider": (self.string(prov, ".") or "").rsplit(".", 1)[-1],
            "deps": [],
        }
        self._loc_cache[off] = loc
        loc["deps"] = [self.location(d)["internal_id"] for d in self._u32array(deps)]
        return loc

    def resolve(self, key):
        """GUID or address -> list of locations."""
        return [self.location(o) for o in self.keys.get(key, [])]

    def asset(self, key):
        """GUID -> (asset path, [bundle file names]) for an asset in a bundle, else None."""
        for loc in self.resolve(key):
            if loc["provider"] != "AssetBundleProvider":
                bundles = [d.replace("\\", "/").rsplit("/", 1)[-1] for d in loc["deps"]]
                return loc["internal_id"], bundles
        return None
