# SPDX-License-Identifier: GPL-3.0-or-later
"""Normal map -> height map stage (numpy/scipy/Pillow, runs in the venv).

Per material in cfg["materials"]:
  1. Detect the stored layout (Unity DXT5nm: X in alpha, Y in green; BC5: X in red) and rebuild
     a standard RGB tangent-space map (Z = sqrt(1 - x^2 - y^2)) -> normal_fixed_<mat>.png
  2. Rasterise the mesh's UV islands (from prep's uv_<mat>.json, wrapped into tile 0 because Unity
     textures repeat) as the integration mask.
  3. Frankot-Chellappa FFT integration of the OpenGL (Y+) slope field, masked high-pass, feathered
     island borders, optional per-model flatten rectangles -> height_<mat>.png (16-bit, 0.5 = no offset)
"""
import json
import math
import os

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage


def rebuild_normal(path, encoding="auto"):
    a = np.asarray(Image.open(path).convert("RGBA")).astype(np.float64) / 255.0
    r, g, b, al = (a[..., i] for i in range(4))
    if encoding == "dxt5nm":
        layout, nx, ny = "DXT5nm (X in alpha, Y in green; from the package)", al * 2 - 1, g * 2 - 1
    elif encoding == "bc5":
        layout, nx, ny = "BC5 (X in red, Y in green; from the package)", r * 2 - 1, g * 2 - 1
    elif encoding == "rgb":
        layout, nx, ny = "RGB (from the package)", r * 2 - 1, g * 2 - 1
    elif r.std() < 1e-3 and al.std() > 1e-3:
        layout, nx, ny = "DXT5nm (X in alpha, Y in green)", al * 2 - 1, g * 2 - 1
    elif b.std() < 1e-3 or np.abs(b - g).max() < 1e-3:
        layout, nx, ny = "BC5 (X in red, Y in green)", r * 2 - 1, g * 2 - 1
    else:
        layout, nx, ny = "RGB", r * 2 - 1, g * 2 - 1
    nz = np.sqrt(np.clip(1 - nx ** 2 - ny ** 2, 0, 1))
    return np.stack([nx, ny, nz], -1), layout


def curl_test(n):
    """Mean |curl| of the slope field under each Y convention; the smaller one is the right one."""
    nz = np.maximum(n[..., 2], 0.2)
    p = -n[..., 0] / nz
    res = {}
    for label, sign in (("OpenGL Y+", 1), ("DirectX Y-", -1)):
        q = sign * n[..., 1] / nz
        res[label] = float(np.abs(np.gradient(p, axis=0) - np.gradient(q, axis=1)).mean())
    return res


def uv_mask(polys, W, H):
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)
    for p in polys:
        # wrap each polygon into tile 0 (Unity wrap mode Repeat; mirrored halves sit in u=1..2)
        ou = math.floor(min(u for u, v in p) + 1e-6)
        ov = math.floor(min(v for u, v in p) + 1e-6)
        d.polygon([((u - ou) * W, (1 - (v - ov)) * H) for u, v in p], fill=255)
    return ndimage.binary_erosion(np.asarray(img) > 0, iterations=1)


def frankot_chellappa(p, q):
    H, W = p.shape
    P = np.block([[p, -p[:, ::-1]], [p[::-1, :], -p[::-1, ::-1]]])
    Q = np.block([[q, q[:, ::-1]], [-q[::-1, :], -q[::-1, ::-1]]])
    wy = np.fft.fftfreq(2 * H) * 2 * np.pi
    wx = np.fft.fftfreq(2 * W) * 2 * np.pi
    WX, WY = np.meshgrid(wx, wy)
    den = WX ** 2 + WY ** 2
    den[0, 0] = 1
    Z = (-1j * WX * np.fft.fft2(P) - 1j * WY * np.fft.fft2(Q)) / den
    Z[0, 0] = 0
    return np.real(np.fft.ifft2(Z))[:H, :W]


def rect_mask(rect, feather, W, H):
    u0, v0, u1, v1 = rect
    m = np.zeros((H, W))
    m[int((1 - v1) * H):int(math.ceil((1 - v0) * H)), int(u0 * W):int(math.ceil(u1 * W))] = 1
    return np.clip(ndimage.gaussian_filter(m, feather / 2) * 2, 0, 1) if feather else m


def build_height(cfg, mat, info, out, texel):
    hc = cfg["height"]
    n, layout = rebuild_normal(info["normal"], info.get("encoding", "auto"))
    if info.get("green") == "directx":  # DirectX green (Y down): flip to the OpenGL convention used below
        n[..., 1] *= -1
        layout += ", DirectX green flipped"
    H, W = n.shape[:2]
    px_per_mm = W / texel["mm_per_tile"]
    sigma_px = hc["highpass_sigma_mm"] * px_per_mm
    feather_px = max(1.0, hc["feather_mm"] * px_per_mm)
    curl = curl_test(n)
    Image.fromarray((n * 127.5 + 127.5).round().astype(np.uint8), "RGB").save(os.path.join(out, f"normal_fixed_{mat}.png"))

    with open(os.path.join(out, f"uv_{mat}.json")) as f:
        mask = uv_mask(json.load(f), W, H)

    nz = np.clip(n[..., 2], hc["max_slope_nz"], 1)
    p = np.where(mask, -n[..., 0] / nz, 0.0)
    q = np.where(mask, n[..., 1] / nz, 0.0)  # OpenGL Y+: +V is up the image, rows run down
    h = frankot_chellappa(p, q)

    m = mask.astype(np.float64)

    def masked_blur(x, s):
        return ndimage.gaussian_filter(x * m, s) / np.maximum(ndimage.gaussian_filter(m, s), 1e-6)

    h = (h - masked_blur(h, sigma_px)) * m
    if hc["lowpass_sigma_mm"] > 0:
        h = masked_blur(h, hc["lowpass_sigma_mm"] * px_per_mm) * m
    t = np.clip(ndimage.distance_transform_edt(mask) / feather_px, 0, 1)
    h *= t * t * (3 - 2 * t)
    h -= h[mask].mean()
    h *= mask
    h /= np.percentile(np.abs(h[mask]), hc["clip_percentile"])

    flat = np.zeros_like(h)
    for fl in hc["flatten"].get(mat, []):
        flat = np.maximum(flat, rect_mask(fl["uv_rect"], fl.get("feather_mm", hc["feather_mm"]) * px_per_mm, W, H))
    h *= 1 - flat  # masked blend toward 0 (the 0.5 midlevel)

    out16 = 0.5 + 0.5 * np.clip(h, -1, 1)
    Image.fromarray((out16 * 65535 + 0.5).astype(np.uint16)).save(os.path.join(out, f"height_{mat}.png"))
    prev = Image.fromarray((out16 * 255 + 0.5).astype(np.uint8), "L").resize((1024, 1024), Image.LANCZOS)
    prev.save(os.path.join(out, f"height_{mat}_preview.png"))
    print(f"   {mat}: {layout}; {W}x{H}; {1000 / px_per_mm:.1f} um/texel; high-pass {sigma_px:.1f} px; "
          f"curl GL={curl['OpenGL Y+']:.4f} DX={curl['DirectX Y-']:.4f} -> {min(curl, key=curl.get)}; "
          f"UV coverage {mask.mean() * 100:.1f}%; flattened {(flat > 0.5).sum()} px")


def run(cfg):
    with open(os.path.join(cfg["out_dir"], "prep.json")) as f:
        texel = json.load(f)["texel"]
    for mat, info in cfg["materials"].items():
        if info.get("normal"):
            build_height(cfg, mat, info, cfg["out_dir"], texel[mat])


if __name__ == "__main__":  # run as its own (capped) process: python -I heights.py <config.json>
    import sys
    with open(sys.argv[1], encoding="utf-8") as f:
        run(json.load(f))
    print("RESULT_JSON:" + json.dumps({"ok": True}))
