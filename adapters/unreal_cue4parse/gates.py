# SPDX-License-Identifier: GPL-3.0-or-later
"""Unreal gates: plain byte reads of the install, no reader needed, nothing written.

1. fingerprint: <Project>/Content/Paks with .pak and/or .utoc+.ucas (IoStore), the Shipping exe.
2. encryption: a .pak footer's bEncryptedIndex byte, each .utoc's container flag 0x2 (Encrypted). Any encrypted
   container -> the game is out (no keys, ever).
3. compression: IoStore compression method names. Oodle needs an Oodle DLL: the game's own oo2core_*.dll if the
   install has one, else one the user already has ([readers] oodle_dll). The tool never downloads or ships one.
4. versioning: a sample of IoStore package headers. PKG_UnversionedProperties (0x2000) -> a .usmap mappings file is
   needed ([games.<id>] usmap, the user's own); the tool never makes one.
"""
import glob
import os
import random
import struct
import zlib

TOC_MAGIC = b"-==--==--==--==-"
PAK_MAGIC = 0x5A6F12E1
PKG_UNVERSIONED = 0x2000


def find_paks(install):
    hits = [h for pat in ("Content", os.path.join("*", "Content"), os.path.join("*", "*", "Content"))
            for h in glob.glob(os.path.join(install, pat, "Paks"))]
    hits = [h for h in hits if glob.glob(os.path.join(h, "**", "*.pak"), recursive=True) or glob.glob(os.path.join(h, "**", "*.utoc"), recursive=True)]
    if not hits:
        raise FileNotFoundError(f"no <Project>/Content/Paks with .pak/.utoc under {install}")
    return max(hits, key=lambda h: sum(os.path.getsize(f) for f in glob.glob(os.path.join(h, "**", "*.*"), recursive=True)))


def pak_encrypted(path):
    """True/False from the pak footer (searched in the last 1 KiB for the magic; bEncryptedIndex precedes it
    in v7+ footers: [EncryptionKeyGuid 16][bEncryptedIndex 1][Magic 4][Version 4]...)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - 1024))
        tail = f.read()
    i = tail.rfind(struct.pack("<I", PAK_MAGIC))
    if i < 1:
        return None
    return tail[i - 1] == 1


def _toc(path):
    with open(path, "rb") as f:
        h = f.read(144)
        if h[:16] != TOC_MAGIC:
            raise ValueError(f"{path}: not an IoStore .utoc")
        ver = h[16]
        hsize, nent, nblk, _, nmeth, methlen, cblock, _, _ = struct.unpack_from("<9I", h, 20)
        seeds = struct.unpack_from("<I", h, 84)[0] if ver >= 4 else 0
        nowph = struct.unpack_from("<I", h, 96)[0] if ver >= 5 else 0
        f.seek(hsize)
        ids = f.read(nent * 12)
        offl = f.read(nent * 10)
        f.seek(seeds * 4 + nowph * 4, 1)
        blocks = f.read(nblk * 12)
        names = ["None"] + [f.read(methlen).split(b"\0")[0].decode("ascii", "replace") for _ in range(nmeth)]
    return {"version": ver, "flags": h[80], "entries": nent, "ids": ids, "offl": offl, "blocks": blocks,
            "methods": names, "cblock": cblock}


def _block(ucas, t, i):
    b = t["blocks"][i * 12:(i + 1) * 12]
    off, csz, usz = int.from_bytes(b[0:5], "little"), int.from_bytes(b[5:8], "little"), int.from_bytes(b[8:11], "little")
    m = t["methods"][b[11]]
    ucas.seek(off)
    raw = ucas.read(csz)
    if m == "None":
        return raw[:usz]
    if m.lower() == "zlib":
        return zlib.decompress(raw)
    return None  # Oodle and others: not sampled here


def sample_versioning(utoc, n=200):
    """(unversioned, versioned, unreadable) counts over a sample of package headers in one container."""
    t = _toc(utoc)
    bundle = 2 if t["version"] <= 3 else 1  # EIoChunkType ExportBundleData: 2 in UE4.26/4.27, 1 in UE5
    idx = [i for i in range(t["entries"]) if t["ids"][i * 12 + 11] == bundle]
    random.Random(1).shuffle(idx)
    unv = ver = unk = 0
    with open(utoc[:-5] + ".ucas", "rb") as ucas:
        for i in idx[:n]:
            o = int.from_bytes(t["offl"][i * 10:i * 10 + 5], "big")
            d = _block(ucas, t, o // t["cblock"])
            hdr = d[o % t["cblock"]:o % t["cblock"] + 24] if d else b""
            if len(hdr) < 20:
                unk += 1
                continue
            flags = struct.unpack_from("<I", hdr, 16)[0]
            if flags & PKG_UNVERSIONED:
                unv += 1
            else:
                ver += 1
    return unv, ver, unk


def probe(install, usmap="", oodle_dll=""):
    """Everything the gates find. ok=False with reasons when the game cannot be read (and why)."""
    paks = find_paks(install)
    res = {"paks": paks, "pak_files": [], "utoc_files": [], "encrypted": [], "methods": set(), "ok": True, "stops": [],
           "notes": []}
    for p in sorted(glob.glob(os.path.join(paks, "**", "*.pak"), recursive=True)):
        enc = pak_encrypted(p)
        res["pak_files"].append({"file": os.path.basename(p), "size": os.path.getsize(p), "encrypted_index": enc})
        if enc:
            res["encrypted"].append(os.path.basename(p))
    biggest = None
    for u in sorted(glob.glob(os.path.join(paks, "**", "*.utoc"), recursive=True)):
        t = _toc(u)
        ucas = u[:-5] + ".ucas"
        sz = os.path.getsize(ucas) if os.path.exists(ucas) else 0
        enc = bool(t["flags"] & 2)
        used = {t["methods"][t["blocks"][i * 12 + 11]] for i in range(0, len(t["blocks"]) // 12, 97)}
        res["utoc_files"].append({"file": os.path.basename(u), "ucas_size": sz, "toc_version": t["version"],
                                  "encrypted": enc, "methods": sorted(used)})
        res["methods"] |= used
        if enc:
            res["encrypted"].append(os.path.basename(u))
        if sz and (biggest is None or sz > biggest[1]):
            biggest = (u, sz)
    res["methods"] = sorted(res["methods"])
    res["io_store"] = bool(res["utoc_files"])
    res["engine_hint"] = ("UE5" if any(f["toc_version"] >= 5 for f in res["utoc_files"]) else
                          "UE4.26/4.27" if res["utoc_files"] else "UE4 (pak only)")
    if res["encrypted"]:
        res["ok"] = False
        res["stops"].append(f"encrypted containers ({', '.join(res['encrypted'][:6])}): out. The tool never uses keys.")
    if "Oodle" in res["methods"]:
        own = glob.glob(os.path.join(install, "**", "oo2core_*win64.dll"), recursive=True)
        if own:
            res["oodle_dll"] = own[0]
            res["notes"].append(f"Oodle compression: using the game's own {os.path.basename(own[0])} (read only)")
        elif oodle_dll and os.path.exists(oodle_dll):
            res["oodle_dll"] = oodle_dll
            res["notes"].append("Oodle compression: using your [readers] oodle_dll")
        else:
            res["ok"] = False
            res["stops"].append("Oodle-compressed and the install has no oo2core_*.dll (Oodle is linked into the exe). "
                                "Set [readers] oodle_dll to an Oodle DLL you already have (e.g. from your own Unreal Engine "
                                "install); the tool never downloads or ships one.")
    if biggest and not res["encrypted"] and "Oodle" not in res["methods"]:
        unv, ver, unk = sample_versioning(biggest[0])
        res["versioning_sample"] = {"container": os.path.basename(biggest[0]), "unversioned": unv, "versioned": ver,
                                    "unreadable": unk}
        res["unversioned"] = unv > ver
        if res["unversioned"]:
            if usmap and os.path.exists(usmap):
                res["notes"].append(f"unversioned properties: using your mappings file {os.path.basename(usmap)}")
            else:
                res["ok"] = False
                res["stops"].append("cooked with unversioned properties: reading anything needs a .usmap mappings file. "
                                    "Set usmap in this game's [games.<id>] block to a mappings file you already have; "
                                    "the tool never makes one (that needs a dumper injected into the running game).")
    elif biggest:
        res["notes"].append("versioning not sampled (Oodle/encrypted container); the first reader run will tell")
    return res
