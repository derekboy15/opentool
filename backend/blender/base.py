"""Bases (build stage, sub-stage "base"): Citadel round/oval with terrain + ground fill, none, flying socket.

Shapes (configs/bases.json):
  round          bottom_diameter_mm -> top_diameter_mm, height_mm, sink_mm (feet sink in)
  oval           bottom_mm [long, short] -> top_mm, ...; the long axis runs front-back (model y)
  none           ground vehicles (GW "use hull"): nothing added
  flying_socket  flyers: a boss inside the hull underside plus a socket hole for a flying-stand peg
For a round base every formula is the one v1 used (the Intercessor regression depends on it).
"""
import math

import bmesh
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree


def axes(bc, which):
    """Semi-axes (x, y) of the top or bottom outline."""
    if bc["shape"] == "oval":
        L, W = bc[f"{which}_mm"]
        return W / 2, L / 2
    r = bc[f"{which}_diameter_mm"] / 2
    return r, r


def terrain_field(T, ab, rng):
    """Height function h(x, y) (mm, relative to the base top, centre-relative coords) for a ground texture:
    low undulation, grooves/cracks along random-walk polylines, faded to 0 near the rim."""
    a_, b_ = ab
    waves = []
    for _ in range(4):
        ang = rng.uniform(0, 2 * math.pi)
        lam = T["undulation_wavelength_mm"] * rng.uniform(0.6, 1.5)
        waves.append((math.cos(ang) / lam, math.sin(ang) / lam, rng.uniform(0, 2 * math.pi), rng.uniform(0.5, 1.0)))
    wsum = sum(w[3] for w in waves)
    cracks = []
    for _ in range(T["grooves"]):
        a = rng.uniform(0, 2 * math.pi)
        u = math.sqrt(rng.uniform(0, 1))
        p = (u * a_ * 0.8 * math.cos(a), u * b_ * 0.8 * math.sin(a))
        d = rng.uniform(0, 2 * math.pi)
        pts = [p]
        for _ in range(rng.randint(2, 5)):
            d += rng.uniform(-0.6, 0.6)
            L = rng.uniform(1.2, 3.5)
            p = (p[0] + L * math.cos(d), p[1] + L * math.sin(d))
            pts.append(p)
        cracks.append((pts, T["groove_depth_mm"] * rng.uniform(0.7, 1.0), T["groove_width_mm"] * rng.uniform(0.8, 1.2)))

    def h(x, y):
        x, y = np.asarray(x, float), np.asarray(y, float)
        z = np.zeros_like(x)
        for kx, ky, ph, a in waves:
            z += a / wsum * np.sin(2 * math.pi * (kx * x + ky * y) + ph)
        z *= T["undulation_mm"]
        for pts, depth, width in cracks:
            dmin = np.full_like(x, 1e9)
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                vx, vy = bx - ax, by - ay
                t = np.clip(((x - ax) * vx + (y - ay) * vy) / (vx * vx + vy * vy), 0, 1)
                dmin = np.minimum(dmin, np.hypot(x - ax - t * vx, y - ay - t * vy))
            z -= depth * 0.5 * (1 + np.cos(np.pi * np.clip(dmin / (width / 2), 0, 1)))  # rounded groove
        if a_ == b_:
            to_rim = a_ - np.hypot(x, y)
        else:  # approximate distance to the ellipse rim
            to_rim = (1 - np.hypot(x / a_, y / b_)) * min(a_, b_)
        edge = np.clip(to_rim / T["edge_margin_mm"], 0, 1)
        return z * edge * edge * (3 - 2 * edge)
    return h, len(cracks)


def sole_mound(pre, centre_xy, ab, z_top, M):
    """Ground that rises to meet the soles: h(x, y) (mm above the base top, centre-relative), 0 away from the feet.
    Each sole point hovering at most max_gap_mm above the top gets ground up to embed_mm into the boot; from
    there it slopes down at `slope` (1 = 45 deg) like earth the foot is planted in. Soles are rays cast up from
    under the base that first hit a downward face (normal z < -min_down, so not the boot's curved rim)."""
    a_, b_ = ab
    s, max_gap, emb, slope = M.get("grid_mm", 0.05), M["max_gap_mm"], M["embed_mm"], M.get("slope", 1.0)
    pre.normal_update()
    pre.verts.index_update()  # earlier edits leave stale indices; the BVH below indexes by them
    faces = [f for f in pre.faces if min(v.co.z for v in f.verts) < z_top + max_gap + 0.3]
    nz = np.array([f.normal.z for f in faces])
    bvh = BVHTree.FromPolygons([v.co for v in pre.verts], [[v.index for v in f.verts] for f in faces])
    n = int(math.ceil(max(a_, b_) / s))
    g = (np.arange(-n, n + 1) * s)
    D = np.full((g.size, g.size), -np.inf)
    worst = 0.0
    # only cells under the low faces can hit anything
    if faces:
        fx = [v.co.x - centre_xy.x for f in faces for v in f.verts]
        fy = [v.co.y - centre_xy.y for f in faces for v in f.verts]
        xr, yr = (min(fx) - s, max(fx) + s), (min(fy) - s, max(fy) + s)
    else:
        xr = yr = (1, -1)
    for i, x in enumerate(g):
        if not xr[0] <= x <= xr[1]:
            continue
        for j, y in enumerate(g):
            if not yr[0] <= y <= yr[1]:
                continue
            if (x * x + y * y > a_ * a_) if a_ == b_ else ((x / a_) ** 2 + (y / b_) ** 2 > 1):
                continue
            loc, _, k, _ = bvh.ray_cast(Vector((centre_xy.x + x, centre_xy.y + y, z_top - 1.0)), Vector((0, 0, 1)), 1.0 + max_gap)
            if loc is None or nz[k] > -M.get("min_down", 0.7):
                continue
            D[i, j] = loc.z - z_top + emb
            worst = max(worst, loc.z - z_top)
    # cone dilation: mound(p) = max_q D(q) - slope * |p - q|
    R = int(math.ceil((max_gap + emb) / slope / s))
    H = D.copy()
    P = np.pad(D, R, constant_values=-np.inf)
    for di in range(-R, R + 1):
        for dj in range(-R, R + 1):
            d = math.hypot(di, dj) * s
            if 0 < d <= R * s:
                H = np.maximum(H, P[R + di:R + di + g.size, R + dj:R + dj + g.size] - slope * d)
    H = np.maximum(H, 0.0)
    for _ in range(M.get("blur_passes", 3)):  # soften the stair-stepped sole outline (3x3 box, ~0.1 mm)
        P = np.pad(H, 1, mode="edge")
        H = sum(P[1 + di:1 + di + g.size, 1 + dj:1 + dj + g.size] for di in (-1, 0, 1) for dj in (-1, 0, 1)) / 9
    covered = float((H > 0.02).sum()) * s * s

    def h(x, y):  # bilinear sample of the raster
        x, y = np.asarray(x, float), np.asarray(y, float)
        fi, fj = np.clip((x - g[0]) / s, 0, g.size - 1.001), np.clip((y - g[0]) / s, 0, g.size - 1.001)
        i0, j0 = fi.astype(int), fj.astype(int)
        ti, tj = fi - i0, fj - j0
        return ((1 - ti) * (1 - tj) * H[i0, j0] + ti * (1 - tj) * H[i0 + 1, j0]
                + (1 - ti) * tj * H[i0, j0 + 1] + ti * tj * H[i0 + 1, j0 + 1])
    return h, {"largest_gap_filled_mm": round(worst, 2), "mound_area_mm2": round(covered, 1), "max_gap_mm": max_gap}


def base_terrain(bm, centre_xy, bc, feet_xy, mound=None):
    """Citadel-style base whose top is a textured ground (terrain_field) plus rocks, pebbles and broken slabs.
    feet_xy: footprint points (absolute), kept clear of loose bits. mound: optional h(x, y) raised under the feet.
    Feature counts scale with the top area (reference = v1's 30 mm top), the rim gets enough segments."""
    T = dict(bc["terrain"])
    at, bt = axes(bc, "top")
    ab_bot = axes(bc, "bottom")
    ratio = math.pi * at * bt / T.get("density_ref_top_area_mm2", math.pi * at * bt)
    for k in ("grooves", "rocks", "pebbles", "slabs"):
        T[k] = int(round(T[k] * ratio))
    rng = __import__("random").Random(T.get("seed", 1))
    h_, z1 = bc["height_mm"], bc["sink_mm"]
    z0 = z1 - h_
    field_, n_cracks = terrain_field(T, (at, bt), rng)
    field = field_ if mound is None else (lambda x, y: np.maximum(field_(x, y), mound(x, y)))
    grid = T.get("grid_mm", 0.15)
    seg = max(T.get("segments", 720), int(math.ceil(2 * math.pi * max(at, bt) / grid)))
    K = max(8, int(math.ceil(max(at, bt) / grid)))
    ang = np.linspace(0, 2 * math.pi, seg, endpoint=False)
    # top: polar grid (centre vertex + K rings); the outer ring sits at height 0 (field fades to 0 at the rim)
    centre = bm.verts.new((centre_xy.x, centre_xy.y, z1 + float(field([0.0], [0.0])[0])))
    rings = []
    for k in range(1, K + 1):
        xs, ys = (at * k / K) * np.cos(ang), (bt * k / K) * np.sin(ang)
        zs = field(xs, ys)
        rings.append([bm.verts.new((centre_xy.x + float(x), centre_xy.y + float(y), z1 + float(z))) for x, y, z in zip(xs, ys, zs)])
    faces = [bm.faces.new((centre, rings[0][i], rings[0][(i + 1) % seg])) for i in range(seg)]
    for a, b in zip(rings, rings[1:]):
        faces += [bm.faces.new((a[i], b[i], b[(i + 1) % seg], a[(i + 1) % seg])) for i in range(seg)]
    bottom = [bm.verts.new((centre_xy.x + ab_bot[0] * math.cos(t), centre_xy.y + ab_bot[1] * math.sin(t), z0)) for t in ang]
    faces += [bm.faces.new((bottom[i], bottom[(i + 1) % seg], rings[-1][(i + 1) % seg], rings[-1][i])) for i in range(seg)]
    faces.append(bm.faces.new(list(reversed(bottom))))
    bmesh.ops.recalc_face_normals(bm, faces=faces)

    # loose bits: rocks, pebbles, slabs - inside the rim margin, clear of the feet, sunk into the ground
    fx = np.array([p[0] for p in feet_xy]) - centre_xy.x if feet_xy else np.zeros(0)
    fy = np.array([p[1] for p in feet_xy]) - centre_xy.y if feet_xy else np.zeros(0)
    placed = []
    kinds = ([("rock", T["rock_size_mm"])] * T["rocks"] + [("pebble", T["pebble_size_mm"])] * T["pebbles"]
             + [("slab", T["slab_size_mm"])] * T["slabs"])
    counts = {"rock": 0, "pebble": 0, "slab": 0}
    for kind, (smin, smax) in kinds:
        size = rng.uniform(smin, smax)
        for _ in range(60):
            a = rng.uniform(0, 2 * math.pi)
            u = math.sqrt(rng.uniform(0, 1))
            x = u * (at - T["edge_margin_mm"] - size / 2) * math.cos(a)
            y = u * (bt - T["edge_margin_mm"] - size / 2) * math.sin(a)
            if fx.size and np.min(np.hypot(fx - x, fy - y)) < size / 2 + T["avoid_feet_mm"]:
                continue
            if any(math.hypot(x - px, y - py) < (size + ps) / 2 + 0.15 for px, py, ps in placed):
                continue
            break
        else:
            continue
        placed.append((x, y, size))
        zg = z1 + float(field([x], [y])[0])
        yaw, tilt = rng.uniform(0, 2 * math.pi), rng.uniform(-0.25, 0.25)
        R = Matrix.Rotation(yaw, 4, "Z") @ Matrix.Rotation(tilt, 4, "X")
        if kind == "slab":
            dims = (size, size * rng.uniform(0.55, 0.8), rng.uniform(0.3, 0.45))
            M = Matrix.Translation((centre_xy.x + x, centre_xy.y + y, zg + dims[2] * 0.1)) @ R @ Matrix.Diagonal((*dims, 1))
            bmesh.ops.create_cube(bm, size=1.0, matrix=M)
        else:
            sq = (1.0, rng.uniform(0.7, 0.95), rng.uniform(0.5, 0.75))
            M = Matrix.Translation((centre_xy.x + x, centre_xy.y + y, zg + size * sq[2] * 0.15)) @ R @ \
                Matrix.Diagonal((size / 2 * sq[0], size / 2 * sq[1], size / 2 * sq[2], 1))
            res = bmesh.ops.create_icosphere(bm, subdivisions=2 if kind == "rock" else 1, radius=1.0, matrix=M)
            for v in res["verts"]:  # lumpy, not round
                v.co += (v.co - Vector(M.translation)) * rng.uniform(-0.15, 0.15)
        counts[kind] += 1
    return {"grooves": n_cracks, **counts, "grid_rings": K, "segments": seg}


def sole_fill(pre, extra, z_top, max_gap, z_bottom, min_down=0.7):
    """Close gaps under the feet on a flat base: a vertical prism from every downward-facing face within max_gap
    of the base top down into the base (the final remesh unions them)."""
    pre.normal_update()
    n, worst, made = 0, 0.0, []
    for f in pre.faces:
        if f.normal.z > -min_down:  # soles only, not the boot's curved rim
            continue
        zs = [v.co.z for v in f.verts]
        if max(zs) > z_top + max_gap or max(zs) < z_top - 0.05:
            continue
        worst = max(worst, min(zs) - z_top)
        top = [extra.verts.new(v.co) for v in f.verts]
        bot = [extra.verts.new((v.co.x, v.co.y, z_bottom)) for v in f.verts]
        fs = [extra.faces.new(top), extra.faces.new(list(reversed(bot)))]
        k = len(top)
        for i in range(k):
            j = (i + 1) % k
            fs.append(extra.faces.new((top[j], top[i], bot[i], bot[j])))
        made += fs
        n += 1
    bmesh.ops.recalc_face_normals(extra, faces=made)  # once for all prisms (per-prism calls are very slow)
    return n, worst


def base_disc(bm, centre_xy, bc):
    """Plain round/oval base, straight taper from the bottom to the top outline; top at z=sink."""
    n = bc.get("segments", 256)
    h, z1 = bc["height_mm"], bc["sink_mm"]
    z0 = z1 - h
    profile = ((axes(bc, "bottom"), z0), (axes(bc, "top"), z1))
    rings = []
    for (ax, ay), z in profile:
        rings.append([bm.verts.new((centre_xy.x + ax * math.cos(2 * math.pi * i / n),
                                    centre_xy.y + ay * math.sin(2 * math.pi * i / n), z)) for i in range(n)])
    new_faces = []
    for lo, hi in zip(rings, rings[1:]):
        for i in range(n):
            new_faces.append(bm.faces.new((lo[i], lo[(i + 1) % n], hi[(i + 1) % n], hi[i])))
    new_faces.append(bm.faces.new(list(reversed(rings[0]))))
    new_faces.append(bm.faces.new(rings[-1]))
    bmesh.ops.recalc_face_normals(bm, faces=new_faces)


def hull2d(pts):
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def min_circle(P):
    """Smallest enclosing circle (Welzl, iterative) of 2D points -> ((x, y), r)."""
    import random

    def c2(a, b):
        c = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        return c, math.dist(a, c)

    def c3(a, b, c):
        ax, ay = a
        bx, by = b
        cx, cy = c
        d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(d) < 1e-12:
            return None
        ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
        uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
        return (ux, uy), math.dist(a, (ux, uy))

    def inside(c, p):
        return math.dist(p, c[0]) <= c[1] * (1 + 1e-9)
    P = list(P)
    random.Random(1).shuffle(P)
    c = None
    for i, p in enumerate(P):
        if c is None or not inside(c, p):
            c = (p, 0.0)
            for j in range(i):
                if not inside(c, P[j]):
                    c = c2(p, P[j])
                    for k in range(j):
                        if not inside(c, P[k]):
                            c = c3(p, P[j], P[k]) or c
    return c


def fit(bc, centre, hull):
    """Footprint fit on the top outline: (fits, margin_mm, footprint size)."""
    at, bt = axes(bc, "top")
    if at == bt:
        (cx, cy), r = min_circle(hull)
        return r <= at, at - r, 2 * r
    worst = max(((x - centre.x) / at) ** 2 + ((y - centre.y) / bt) ** 2 for x, y in hull) ** 0.5
    return worst <= 1, (1 - worst) * min(at, bt), worst


def flying_boss(extra, centre, z_under, bc):
    """Solid cylinder reaching up into the hull from its underside: material for the stand socket."""
    r = bc["socket_diameter_mm"] / 2 + 1.5
    depth = bc["socket_depth_mm"] + 1.0
    bmesh.ops.create_cone(extra, cap_ends=True, segments=48, radius1=r, radius2=r, depth=depth,
                          matrix=Matrix.Translation((centre.x, centre.y, z_under + depth / 2)))
