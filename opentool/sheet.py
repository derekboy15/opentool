# SPDX-License-Identifier: GPL-3.0-or-later
"""Review sheet (stage 13): one PNG per job with numbered panels at one scale, the silhouette, the close-ups
(hands, knees, weapon grip/muzzle, feet, any flagged spot, thin-wall heat map) and a header with what the
checks found. Built with Pillow from the renders of backend/blender/review.py and qa.py."""
import glob
import json
import os

from PIL import Image, ImageDraw, ImageFont


def _font(size):
    for f in ("arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def compose(job_dir, out_png, title, lines, changed=None, panel=420):
    """job_dir/review/NN_*.png (main views) + job_dir/renders/*.png (close-ups, heat maps)."""
    main = sorted(glob.glob(os.path.join(job_dir, "review", "[0-9][0-9]_*.png")))
    vj = os.path.join(job_dir, "review", "views.json")
    marks = {}
    if os.path.exists(vj):
        marks = {v["file"]: v.get("mark_rows") or {} for v in json.load(open(vj, encoding="utf-8"))["views"]}
    extra = sorted(glob.glob(os.path.join(job_dir, "renders", "*.png")))
    tiles = [(os.path.basename(p)[3:-4].replace("_", " "), p) for p in main]
    tiles += [(os.path.basename(p)[:-4].replace("_", " "), p) for p in extra]
    cols = 4
    rows = (len(tiles) + cols - 1) // cols
    f_t, f_b, f_s = _font(30), _font(19), _font(17)
    header_lines = [l for l in lines if l]
    head_h = 60 + 26 * (len(header_lines) + (1 if changed else 0))
    W, H = cols * panel, head_h + rows * (panel + 30)
    sheet = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    d.text((16, 12), title, fill=(0, 0, 0), font=f_t)
    y = 56
    if changed:
        d.text((16, y), f"Changed since last round: {changed}", fill=(20, 60, 160), font=f_b)
        y += 26
    for l in header_lines:
        col = (170, 20, 20) if l.startswith(("FLAG", "FAIL")) else (30, 30, 30)
        d.text((16, y), l, fill=col, font=f_b)
        y += 26
    for i, (label, p) in enumerate(tiles):
        r, c = divmod(i, cols)
        x0, y0 = c * panel, head_h + r * (panel + 30)
        try:
            im = Image.open(p).convert("RGB")
            mrows = marks.get(os.path.basename(p), {})
            if mrows:  # e.g. the eye line used for the scale, as a thin red line across the view
                dd = ImageDraw.Draw(im)
                for nm, yrow in mrows.items():
                    dd.line([(0, yrow), (im.width, yrow)], fill=(220, 20, 20), width=max(2, im.width // 300))
                    dd.text((6, yrow - 22), nm.replace("_", " "), fill=(220, 20, 20), font=_font(max(14, im.width // 45)))
            im.thumbnail((panel - 8, panel - 8))
            sheet.paste(im, (x0 + (panel - im.width) // 2, y0 + 30 + (panel - 8 - im.height) // 2))
        except OSError:
            d.text((x0 + 10, y0 + 40), "(missing render)", fill=(170, 20, 20), font=f_s)
        d.text((x0 + 8, y0 + 4), f"{i + 1}. {label}", fill=(0, 0, 0), font=f_s)
        d.rectangle([x0 + 2, y0 + 26, x0 + panel - 3, y0 + panel + 26], outline=(210, 210, 210))
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    sheet.save(out_png, optimize=True)
    return {"sheet": out_png, "panels": len(tiles)}


def contact_sheet(sheets, out_png, cols=3, width=1400):
    """Several job sheets side by side (gallery overview)."""
    ims = [Image.open(s).convert("RGB") for s in sheets]
    if not ims:
        return None
    w = width // cols
    thumbs = []
    for im in ims:
        im = im.copy()
        im.thumbnail((w, w * 3))
        thumbs.append(im)
    rows = (len(thumbs) + cols - 1) // cols
    h = max(t.height for t in thumbs)
    out = Image.new("RGB", (cols * w, rows * h), (255, 255, 255))
    for i, t in enumerate(thumbs):
        r, c = divmod(i, cols)
        out.paste(t, (c * w, r * h))
    out.save(out_png, optimize=True)
    return out_png


if __name__ == "__main__":
    import sys
    a = json.loads(open(sys.argv[1], encoding="utf-8").read())
    print(json.dumps(compose(**a)))
