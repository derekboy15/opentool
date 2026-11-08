# SPDX-License-Identifier: GPL-3.0-or-later
"""Domed base: a gentle dome with a very gradual texture, each planted boot pressed into it (no rocks, no
grooves, no ground mound rising round the feet). Used when base.top == "dome"."""
import math

import bmesh
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from base import axes


def base_dome(bm, centre_xy, bc, pre):
    """bc["dome"]: dome_mm (rise at the centre over the rim), texture_mm (+-), texture_wavelength_mm, press_mm (how
    far the soles sit below the surface), pocket_fade_mm (the pocket round each boot fades out over this),
    max_raise_mm (the surface may rise at most this much to meet a sole), grid_mm, seed.
    The rim height is chosen so that the planted soles (model z ~ 0) end up press_mm under the domed surface.
    Returns info for build.json."""
    D = bc["dome"]
    at, bt = axes(bc, "top")
    ab_bot = axes(bc, "bottom")
    rng = __import__("random").Random(D.get("seed", 7))
    lam = D.get("texture_wavelength_mm", 1.8)
    waves = []
    for _ in range(6):
        ang = rng.uniform(0, 2 * math.pi)
        L = lam * rng.uniform(0.7, 1.4)
        waves.append((math.cos(ang) / L, math.sin(ang) / L, rng.uniform(0, 2 * math.pi), rng.uniform(0.5, 1.0)))
    wsum = sum(w[3] for w in waves)

    def dome(x, y):
        r2 = (x / at) ** 2 + (y / bt) ** 2
        return D["dome_mm"] * np.clip(1 - r2, 0, 1)

    def tex(x, y):
        x, y = np.asarray(x, float), np.asarray(y, float)
        z = np.zeros_like(x)
        for kx, ky, ph, a in waves:
            z = z + a / wsum * np.sin(2 * math.pi * (kx * x + ky * y) + ph)
        r = np.sqrt((x / at) ** 2 + (y / bt) ** 2)
        edge = np.clip((1 - r) * min(at, bt) / 1.0, 0, 1)  # fades to 0 over the last 1 mm
        return D["texture_mm"] * z * edge

    # soles: rays up from under the model that first hit a downward face near the ground
    s = D.get("grid_mm", 0.08)
    pre.normal_update()
    pre.verts.index_update()
    faces = [f for f in pre.faces if min(v.co.z for v in f.verts) < 1.2]
    n = int(math.ceil(max(at, bt) / s))
    g = np.arange(-n, n + 1) * s
    X, Y = np.meshgrid(g, g, indexing="ij")
    sole = np.full(X.shape, np.nan)
    if faces:
        bvh = BVHTree.FromPolygons([v.co for v in pre.verts], [[v.index for v in f.verts] for f in faces])
        nz = np.array([f.normal.z for f in faces])
        fx = [v.co.x - centre_xy.x for f in faces for v in f.verts]
        fy = [v.co.y - centre_xy.y for f in faces for v in f.verts]
        for i, x in enumerate(g):
            if not min(fx) - s <= x <= max(fx) + s:
                continue
            for j, y in enumerate(g):
                if not min(fy) - s <= y <= max(fy) + s or (x / at) ** 2 + (y / bt) ** 2 > 1:
                    continue
                loc, _, k, _ = bvh.ray_cast(Vector((centre_xy.x + x, centre_xy.y + y, -1.0)), Vector((0, 0, 1)), 2.2)
                if loc is not None and nz[k] < -0.3:
                    sole[i, j] = loc.z
    have = ~np.isnan(sole)
    planted = have & (sole < (np.nanmin(sole) if have.any() else 0) + 0.9)
    base_field = dome(X, Y) + tex(X, Y)
    if planted.any():
        off = float(np.mean(sole[planted] + D["press_mm"] - base_field[planted]))
    else:
        off = bc["sink_mm"] - D["dome_mm"]
    field = base_field + off
    gaps = []
    if planted.any():
        # pockets: inside each sole the surface goes to sole + press (down where the dome is higher, up by at
        # most max_raise_mm where it is lower), fading back to the dome over pocket_fade_mm
        target = np.where(planted, sole + D["press_mm"], 0.0)
        t = np.where(planted, np.minimum(target - field, D.get("max_raise_mm", 0.3)), 0.0)
        gaps = [float(x) for x in (target - field - t)[planted] if x > 1e-6]
        dist = _edt(~planted) * s
        w = np.clip(1 - dist / D.get("pocket_fade_mm", 1.0), 0, 1)
        w = w * w * (3 - 2 * w)
        field = field + w * _nearest_fill(t, planted, passes=int(math.ceil(D.get("pocket_fade_mm", 1.0) / s)) + 2)

    def h(x, y):  # bilinear sample of the raster
        x, y = np.asarray(x, float), np.asarray(y, float)
        fi, fj = np.clip((x - g[0]) / s, 0, g.size - 1.001), np.clip((y - g[0]) / s, 0, g.size - 1.001)
        i0, j0 = fi.astype(int), fj.astype(int)
        ti, tj = fi - i0, fj - j0
        return ((1 - ti) * (1 - tj) * field[i0, j0] + ti * (1 - tj) * field[i0 + 1, j0]
                + (1 - ti) * tj * field[i0, j0 + 1] + ti * tj * field[i0 + 1, j0 + 1])

    seg = max(720, int(math.ceil(2 * math.pi * max(at, bt) / s)))
    K = max(8, int(math.ceil(max(at, bt) / s)))
    ang = np.linspace(0, 2 * math.pi, seg, endpoint=False)
    z_rim = off
    z0 = z_rim - bc["height_mm"]
    centre = bm.verts.new((centre_xy.x, centre_xy.y, float(h([0.0], [0.0])[0])))
    rings = []
    for k in range(1, K + 1):
        xs, ys = (at * k / K) * np.cos(ang), (bt * k / K) * np.sin(ang)
        zs = h(xs, ys) if k < K else np.full(xs.shape, z_rim)
        rings.append([bm.verts.new((centre_xy.x + float(x), centre_xy.y + float(y), float(z))) for x, y, z in zip(xs, ys, zs)])
    fs = [bm.faces.new((centre, rings[0][i], rings[0][(i + 1) % seg])) for i in range(seg)]
    for a, b in zip(rings, rings[1:]):
        fs += [bm.faces.new((a[i], b[i], b[(i + 1) % seg], a[(i + 1) % seg])) for i in range(seg)]
    bottom = [bm.verts.new((centre_xy.x + ab_bot[0] * math.cos(t), centre_xy.y + ab_bot[1] * math.sin(t), z0)) for t in ang]
    fs += [bm.faces.new((bottom[i], bottom[(i + 1) % seg], rings[-1][(i + 1) % seg], rings[-1][i])) for i in range(seg)]
    fs.append(bm.faces.new(list(reversed(bottom))))
    bmesh.ops.recalc_face_normals(bm, faces=fs)
    return {"dome_mm": D["dome_mm"], "texture_mm": D["texture_mm"], "press_mm": D["press_mm"],
            "rim_top_z_mm": round(z_rim, 3), "bottom_z_mm": round(z0, 3), "planted_cells": int(planted.sum()),
            "sole_cells_left_above_surface": len(gaps), "largest_sole_gap_mm": round(max(gaps), 3) if gaps else 0.0}


def _edt(mask):
    """Distance in cells from each True cell to the nearest False cell (two-pass chamfer; Blender has no scipy)."""
    INF = 1e9
    d = np.where(mask, INF, 0.0)
    H, W = d.shape
    r2 = math.sqrt(2)
    for i in range(H):
        for j in range(W):
            if d[i, j]:
                m = d[i, j]
                if i:
                    m = min(m, d[i - 1, j] + 1)
                    if j:
                        m = min(m, d[i - 1, j - 1] + r2)
                    if j + 1 < W:
                        m = min(m, d[i - 1, j + 1] + r2)
                if j:
                    m = min(m, d[i, j - 1] + 1)
                d[i, j] = m
    for i in range(H - 1, -1, -1):
        for j in range(W - 1, -1, -1):
            if d[i, j]:
                m = d[i, j]
                if i + 1 < H:
                    m = min(m, d[i + 1, j] + 1)
                    if j + 1 < W:
                        m = min(m, d[i + 1, j + 1] + r2)
                    if j:
                        m = min(m, d[i + 1, j - 1] + r2)
                if j + 1 < W:
                    m = min(m, d[i, j + 1] + 1)
                d[i, j] = m
    return d


def _nearest_fill(values, mask, passes=20):
    """Spread values from mask cells outward, one cell ring per pass (enough for the pocket fade)."""
    v = np.where(mask, values, 0.0)
    have = mask.copy()
    for _ in range(passes):
        P, Hm = np.pad(v, 1), np.pad(have, 1)
        acc, cnt = np.zeros_like(v), np.zeros_like(v)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                sl = (slice(1 + di, 1 + di + v.shape[0]), slice(1 + dj, 1 + dj + v.shape[1]))
                acc += np.where(Hm[sl], P[sl], 0.0)
                cnt += Hm[sl]
        new = ~have & (cnt > 0)
        if not new.any():
            break
        v = np.where(new, acc / np.maximum(cnt, 1), v)
        have |= new
    return v
