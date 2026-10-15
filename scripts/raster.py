"""
raster.py — render pages with poppler and measure what is actually visible.

Used for:
  * text contrast (WCAG 1.4.3) measured on rendered pixels, so it works for
    real text, outlined text and text over images alike
  * pixel-identity check between the source and the tagged output
  * reading-order overlay images for human review
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from content_model import rel_luminance, contrast


def render(pdf_path, page_no=1, dpi=288) -> Image.Image:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "r"
        subprocess.run(["pdftoppm", "-cropbox", "-r", str(dpi), "-f", str(page_no), "-l", str(page_no),
                        "-png", "-singlefile", str(pdf_path), str(out)], check=True)
        return Image.open(str(out) + ".png").convert("RGB").copy()


def to_px(bbox, page_h, dpi, pad=0.0):
    """bbox in DISPLAY space (CropBox origin, rotation applied, y up); page_h = display height."""
    k = dpi / 72.0
    x0, y0, x1, y1 = bbox
    return (int((x0 - pad) * k), int((page_h - y1 - pad) * k), int((x1 + pad) * k), int((page_h - y0 + pad) * k))


def measure_contrast(img: Image.Image, bbox, page_h, dpi=288):
    """Return (fg_rgb, bg_rgb, ratio) for the text drawn in bbox, or None."""
    box = to_px(bbox, page_h, dpi, pad=1.0)
    crop = np.asarray(img.crop(box), dtype=np.float64) / 255.0
    if crop.size == 0:
        return None
    px = crop.reshape(-1, 3)
    q = np.round(px * 255).astype(np.int64)
    packed = (q[:, 0] << 16) | (q[:, 1] << 8) | q[:, 2]          # 1-D keys: much faster than unique(axis=0)
    keys, counts = np.unique(packed, return_counts=True)
    k = int(keys[counts.argmax()])
    bg = np.array([(k >> 16) & 255, (k >> 8) & 255, k & 255]) / 255.0
    lin = np.where(px <= 0.04045, px / 12.92, ((px + 0.055) / 1.055) ** 2.4)
    lum = lin @ np.array([0.2126, 0.7152, 0.0722])
    lbg = rel_luminance(tuple(bg))
    ratio = np.maximum(lum + 0.05, lbg + 0.05) / np.minimum(lum + 0.05, lbg + 0.05)
    cand = ratio > 1.3
    if cand.sum() < 8:
        return None
    # glyph cores: the most contrasting decile of non-background pixels
    thr = np.quantile(ratio[cand], 0.9)
    core = px[cand & (ratio >= thr)]
    fg = tuple(np.median(core, axis=0))
    return fg, tuple(bg), contrast(fg, tuple(bg))


def pixel_diff(a: Image.Image, b: Image.Image):
    aa = np.asarray(a, dtype=np.int16)
    bb = np.asarray(b, dtype=np.int16)
    if aa.shape != bb.shape:
        return {"same_size": False}
    d = np.abs(aa - bb)
    return {"same_size": True, "max_channel_delta": int(d.max()),
            "pixels_changed": int((d.max(axis=2) > 0).sum()), "total_pixels": int(d.shape[0] * d.shape[1])}


PALETTE = {
    "H1": (180, 0, 120), "H2": (200, 40, 40), "H3": (220, 110, 0), "H4": (170, 140, 0),
    "H5": (120, 140, 0), "H6": (80, 140, 0),
    "P": (30, 100, 200), "LI": (0, 150, 120), "LBody": (0, 150, 120), "Lbl": (0, 120, 90),
    "Link": (120, 60, 200), "Figure": (200, 0, 200), "Caption": (90, 90, 90),
}


def draw_overlay(img: Image.Image, items, page_h, dpi=144, out_path=None):
    """items: list of (order, tag, bbox). Draws numbered, colour-coded boxes."""
    im = img.copy().convert("RGBA")
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", max(10, int(dpi / 9)))
    except OSError:
        font = ImageFont.load_default()
    for order, tag, bbox in items:
        col = PALETTE.get(tag, (60, 60, 60))
        x0, y0, x1, y1 = to_px(bbox, page_h, dpi, pad=1.5)
        d.rectangle([x0, y0, x1, y1], outline=col + (255,), width=max(1, dpi // 72), fill=col + (28,))
        label = "↗ link" if order == "link" else f"{order} {tag}"
        tw = d.textlength(label, font=font)
        th = font.size + 2
        if order == "link":                 # links: label on the right, so the LI number stays visible
            lx, ly = x1 + 3, y0
        else:
            lx = max(0, x0 - tw - 4) if x0 - tw - 4 > 0 else x0
            ly = y0
        d.rectangle([lx, ly, lx + tw + 4, ly + th], fill=col + (235,))
        d.text((lx + 2, ly), label, fill=(255, 255, 255, 255), font=font)
    out = Image.alpha_composite(im, layer).convert("RGB")
    if out_path:
        out.save(out_path)
    return out


# --------------------------------------------------------------------------
# reading-order review image: layout regions + tag boxes + reading path
# --------------------------------------------------------------------------
PATH_STYLE = {
    "same": ((110, 110, 110), 2, False),   # stays in its region
    "next": ((30, 90, 220), 3, True),      # moves on to the next region in layout order
    "bad": ((225, 30, 30), 5, True),       # goes back to an earlier region / back up
}


def _dashed_rect(d, box, color, width, dash=10, gap=6):
    x0, y0, x1, y1 = box
    for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
        length = max(abs(bx - ax), abs(by - ay))
        if length == 0:
            continue
        n = int(length // (dash + gap)) + 1
        for k in range(n):
            s = k * (dash + gap) / length
            e = min(1.0, (k * (dash + gap) + dash) / length)
            d.line([ax + (bx - ax) * s, ay + (by - ay) * s, ax + (bx - ax) * e, ay + (by - ay) * e], fill=color, width=width)


def _arrow(d, a, b, color, width, head=True):
    import math
    d.line([a, b], fill=color, width=width)
    if head:
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        L = 9 + width * 2
        p1 = (b[0] - L * math.cos(ang - 0.45), b[1] - L * math.sin(ang - 0.45))
        p2 = (b[0] - L * math.cos(ang + 0.45), b[1] - L * math.sin(ang + 0.45))
        d.polygon([b, p1, p2], fill=color)


def draw_reading_order(img: Image.Image, regions, items, kinds, page_h, dpi=144, out_path=None):
    """
    regions: [(label, kind 'row'|'column', bbox)]   detected from whitespace
    items:   [(n, tag, bbox)]                          blocks in tag order
    kinds:   ['same'|'next'|'bad'] per item            transition INTO that item
    """
    im = img.copy().convert("RGBA")
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", max(10, int(dpi / 9)))
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", max(9, int(dpi / 11)))
    except OSError:
        font = small = ImageFont.load_default()

    # 1. regions
    for label, kind, bbox in regions:
        pad = 9 if kind == "row" else 4
        col = (60, 60, 80) if kind == "row" else (0, 140, 140)
        x0, y0, x1, y1 = to_px(bbox, page_h, dpi, pad=pad)
        if kind == "column":
            d.rectangle([x0, y0, x1, y1], fill=col + (14,))
        _dashed_rect(d, (x0, y0, x1, y1), col + (230,), 2 if kind == "row" else 2)
        tag = f"{'row' if kind == 'row' else 'column'} {label}"
        tw = d.textlength(tag, font=small)
        if kind == "row":            # rows: tab on the top-left; columns: tab on the top-right
            d.rectangle([x0, y0 - small.size - 4, x0 + tw + 8, y0], fill=col + (235,))
            d.text((x0 + 4, y0 - small.size - 3), tag, fill=(255, 255, 255, 255), font=small)
        else:
            d.rectangle([x1 - tw - 8, y0 - small.size - 4, x1, y0], fill=col + (235,))
            d.text((x1 - tw - 4, y0 - small.size - 3), tag, fill=(255, 255, 255, 255), font=small)

    # 2. badge geometry
    badges = []
    for n, tag, bbox in items:
        x0, y0, x1, y1 = to_px(bbox, page_h, dpi, pad=1.5)
        label = f"{n} {tag}"
        tw = d.textlength(label, font=font)
        th = font.size + 2
        lx = x0 - tw - 4 if x0 - tw - 4 > 0 else x0
        badges.append(((x0, y0, x1, y1), (lx, y0, lx + tw + 4, y0 + th), label, tag))

    # 3. path (under the badges)
    for k in range(1, len(badges)):
        a = badges[k - 1][1]; b = badges[k][1]
        ca = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2)
        cb = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        color, width, head = PATH_STYLE[kinds[k]]
        _arrow(d, ca, cb, color + ({"same": 190, "next": 150, "bad": 200}[kinds[k]],), width, head)

    # 4. boxes + badges
    for (box, badge, label, tag), kind in zip(badges, kinds):
        col = PALETTE.get(tag, (60, 60, 60))
        d.rectangle(box, outline=col + (255,), width=max(1, dpi // 72), fill=col + (24,))
        bcol = (225, 30, 30) if kind == "bad" else col
        d.rectangle(badge, fill=bcol + (240,))
        d.text((badge[0] + 2, badge[1]), label, fill=(255, 255, 255, 255), font=font)

    # 5. legend
    W, H = im.size
    lx, ly = int(0.07 * W), H - int(0.045 * H)
    for kind, text in (("same", "within a region"), ("next", "to the next region (layout order)"),
                       ("bad", "out of layout order")):
        color, width, head = PATH_STYLE[kind]
        _arrow(d, (lx, ly), (lx + 34, ly), color + (255,), width, head)
        d.text((lx + 42, ly - small.size // 2 - 1), text, fill=(40, 40, 40, 255), font=small)
        lx += 54 + int(d.textlength(text, font=small)) + 18
    out = Image.alpha_composite(im, layer).convert("RGB")
    if out_path:
        out.save(out_path)
    return out
