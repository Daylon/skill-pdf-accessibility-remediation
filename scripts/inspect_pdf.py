#!/usr/bin/env python3
"""
inspect_pdf.py — audit a PDF and inventory everything a structure plan needs.

Usage:
    python3 inspect_pdf.py input.pdf --json units.json [--assets DIR] [--rtl] [--quiet]

Writes units.json (consumed by draft_plan.py) and, with --assets, page renders
(DIR/page-N.png) and figure crops (DIR/fig-pN-GM.png) to look at while writing
alt text. Prints a human-readable report: preflight blockers, language,
layout regions, then one row per text line.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import lang_detect  # noqa: E402
from content_model import hexcolor, to_srgb, area, overlap, text_in_rect  # noqa: E402
from layout import Box, analyze, describe, leaf_regions, regions_with_columns  # noqa: E402
from prepare import load  # noqa: E402
from raster import render, measure_contrast, to_px  # noqa: E402

# print UTF-8 even when stdout is a pipe on a non-UTF-8 locale (Windows consoles, CI logs)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
try:                                    # quiet exit when the output is piped into `head`
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):    # no SIGPIPE on Windows
    pass

DPI = 200
BULLET_RE = re.compile(r"^\s*([•◦▪▫■□●○‣⁃∙·\-–—*➢►▶✓✔\ufffd\uf000-\uf0ff]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\(?[ivxlcIVXLC]{1,5}[.)])(?:\s+|$)")
PAGENUM_RE = re.compile(r"^\s*(?:[-–—]\s*)?(?:page|p\.|seite|página|pagina|pag\.)?\s*(\d{1,4}|[ivxlcdm]{1,7})"
                        r"(?:\s*(?:/|of|sur|von|de|di|van)\s*\d{1,4})?\s*(?:[-–—])?\s*$", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()]+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|fr|de|eu|io|gov|edu|design|dev|app|info|co\.uk|uk)(?:/[^\s<>()]*)?", re.I)
FEDI_RE = re.compile(r"(?<![\w.])@([\w]+)@([\w-]+(?:\.[\w-]+)+)")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


# ---------------------------------------------------------------------------- helpers
def doc_facts(pdf):
    r = pdf.Root
    f = {"pdf_version": pdf.pdf_version, "pages": len(pdf.pages)}
    f["lang"] = str(r.get("/Lang")) if r.get("/Lang") is not None else None
    mi = r.get("/MarkInfo")
    f["marked"] = bool(mi.get("/Marked", False)) if mi is not None else False
    vp = r.get("/ViewerPreferences")
    f["display_doc_title"] = bool(vp.get("/DisplayDocTitle", False)) if vp is not None else False
    f["info_title"] = str(pdf.docinfo.get("/Title")) if pdf.docinfo.get("/Title") is not None else None
    f["producer"] = str(pdf.docinfo.get("/Producer")) if pdf.docinfo.get("/Producer") is not None else None
    f["creator"] = str(pdf.docinfo.get("/Creator")) if pdf.docinfo.get("/Creator") is not None else None
    f["encrypted"] = pdf.is_encrypted
    f["has_struct_tree"] = r.get("/StructTreeRoot") is not None
    ol = r.get("/Outlines")
    f["has_outlines"] = ol is not None and ol.get("/First") is not None
    af = r.get("/AcroForm")
    f["acroform"] = af is not None
    f["xfa"] = af is not None and af.get("/XFA") is not None
    try:
        with pdf.open_metadata() as m:
            f["xmp_title"] = m.get("dc:title")
    except Exception:  # noqa: BLE001
        f["xmp_title"] = None
    return f


def drawn_bullet(img, ln, disp_h):
    """Is there a small isolated ink blob just left of the line start (a bullet drawn as a graphic)?"""
    if ln.size <= 0:
        return False
    x0, y0, x1, y1 = ln.dbox
    h = y1 - y0
    strip = (x0 - 2.4 * ln.size, y0 + 0.2 * h, x0 - 0.8, y1 - 0.2 * h)
    if strip[2] <= strip[0] or strip[0] < 0:
        return False
    box = to_px(strip, disp_h, DPI)
    crop = np.asarray(img.crop(box).convert("L"), dtype=np.float64)
    if crop.size == 0 or crop.shape[1] < 5:
        return False
    bg = np.median(np.concatenate([crop[:, :2].ravel(), crop[:, -2:].ravel()]))
    ink = np.abs(crop - bg) > 60
    if ink.sum() < 3:
        return False
    ys, xs = np.nonzero(ink)
    k = DPI / 72.0
    w_pt, h_pt = (xs.max() - xs.min() + 1) / k, (ys.max() - ys.min() + 1) / k
    gap_pt = (crop.shape[1] - 1 - xs.max()) / k
    left_clear = xs.min() / k >= 1.0
    return w_pt <= 0.7 * ln.size and h_pt <= 0.7 * ln.size and gap_pt >= 1.0 and left_clear


def thin_rules(graphics):
    """Horizontal / vertical rule segments from thin paths (table borders, separators)."""
    hs, vs = [], []
    for g in graphics:
        if g.kind != "path":
            continue
        x0, y0, x1, y1 = g.rbox
        w, h = x1 - x0, y1 - y0
        if h <= 2.5 and w >= 8:
            hs.append((x0, x1, (y0 + y1) / 2, g.id))
        elif w <= 2.5 and h >= 6:
            vs.append((y0, y1, (x0 + x1) / 2, g.id))
        elif g.stroke is not None and w > 8 and h > 6 and g.paint_op in ("S", "s", "B", "b") and len(g.op_indices) <= 3:
            hs += [(x0, x1, y0, g.id), (x0, x1, y1, g.id)]
            vs += [(y0, y1, x0, g.id), (y0, y1, x1, g.id)]
    return hs, vs


def _merge(vals, tol=2.5):
    out = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def ruled_tables(lines, graphics):
    hs, vs = thin_rules(graphics)
    if len(hs) < 2 or len(vs) < 2:
        return []
    segs = [("h", a, b, c, gid) for a, b, c, gid in hs] + [("v", a, b, c, gid) for a, b, c, gid in vs]

    def box(s):
        return (s[1], s[3] - 1, s[2], s[3] + 1) if s[0] == "h" else (s[3] - 1, s[1], s[3] + 1, s[2])
    parent = list(range(len(segs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i in range(len(segs)):
        a = box(segs[i])
        for j in range(i + 1, len(segs)):
            b = box(segs[j])
            if a[0] - 2 <= b[2] and b[0] - 2 <= a[2] and a[1] - 2 <= b[3] and b[1] - 2 <= a[3]:
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i, s in enumerate(segs):
        groups[find(i)].append(s)
    tables = []
    for g in groups.values():
        ys = _merge([s[3] for s in g if s[0] == "h"])
        xs = _merge([s[3] for s in g if s[0] == "v"])
        if len(ys) < 2 or len(xs) < 2:
            continue
        x0, x1, y0, y1 = xs[0], xs[-1], ys[0], ys[-1]
        rows = []
        for r in range(len(ys) - 1, 0, -1):            # top → bottom
            top, bot = ys[r], ys[r - 1]
            row = []
            for c in range(len(xs) - 1):
                left, right = xs[c], xs[c + 1]
                cell = [ln.id for ln in lines if left <= (ln.rbox[0] + ln.rbox[2]) / 2 <= right
                        and bot <= (ln.rbox[1] + ln.rbox[3]) / 2 <= top]
                row.append(cell)
            rows.append(row)
        filled = sum(1 for r in rows for c in r if c)
        if len(rows) >= 2 and len(xs) >= 3 and filled >= 3:
            tables.append({"kind": "ruled", "box": [round(v, 1) for v in (x0, y0, x1, y1)], "rows": rows,
                           "rules": sorted({s[4] for s in g})})
    return tables


def aligned_tables(lines, rmap=None):
    """Borderless tables: ≥3 consecutive baselines each holding ≥2 fragments separated by wide gaps, columns aligned."""
    if rmap:                                   # one detection per layout region (a neighbour column must not interfere)
        groups = defaultdict(list)
        for ln in lines:
            groups[rmap.get(ln.id, "")].append(ln)
        if len(groups) > 1:
            return [t for g in groups.values() for t in aligned_tables(g)]
    by_base = defaultdict(list)
    for ln in lines:
        by_base[round(ln.rbaseline)].append(ln)
    rows = []
    for base in sorted(by_base, reverse=True):
        frs = sorted(by_base[base], key=lambda l: l.rbox[0])
        multi = len(frs) >= 2 and all(b.rbox[0] - a.rbox[2] >= 2.0 * max(a.size, 1) for a, b in zip(frs, frs[1:]))
        rows.append((base, frs if multi else None))
    out, run = [], []
    for base, frs in rows + [(None, None)]:
        if frs is not None and (not run or len(frs) == len(run[-1][1])):
            run.append((base, frs))
            continue
        if len(run) >= 3:
            n = len(run[0][1])
            ok = all(min(abs(r[1][c].rbox[0] - run[0][1][c].rbox[0]), abs(r[1][c].rbox[2] - run[0][1][c].rbox[2])) <= 6
                     for r in run for c in range(n))
            if ok:
                ls = [l for _, frs_ in run for l in frs_]
                out.append({"kind": "aligned",
                            "box": [round(min(l.rbox[0] for l in ls), 1), round(min(l.rbox[1] for l in ls), 1),
                                     round(max(l.rbox[2] for l in ls), 1), round(max(l.rbox[3] for l in ls), 1)],
                            "rows": [[[l.id] for l in frs_] for _, frs_ in run], "rules": []})
        run = [(base, frs)] if frs is not None else []
    return out


def link_target(text):
    t = text.strip().rstrip(".,;")
    m = FEDI_RE.search(t)
    if m:
        return f"https://{m.group(2)}/@{m.group(1)}", m.group(0)
    m = EMAIL_RE.search(t)
    if m:
        return "mailto:" + m.group(0), m.group(0)
    m = URL_RE.search(t)
    if m:
        u = m.group(0).rstrip(".,;")
        return (u if u.lower().startswith("http") else "https://" + u), u
    m = PHONE_RE.search(t)
    if m and sum(ch.isdigit() for ch in m.group(0)) >= 9:
        raw = m.group(0)
        digits = re.sub(r"\((0)\)", "", raw)
        digits = re.sub(r"[^\d+]", "", digits)
        return "tel:" + digits, raw
    return None, None


def band_of(ln, read_h):
    if ln.rbox[3] >= 0.92 * read_h:
        return "top"
    if ln.rbox[1] <= 0.08 * read_h:
        return "bottom"
    return None


# ---------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--json", required=True)
    ap.add_argument("--assets")
    ap.add_argument("--rtl", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="do not print every line")
    args = ap.parse_args()

    pdf, pages, tag_index, prep_report = load(args.pdf)
    facts = doc_facts(pdf)
    render_src = args.pdf
    if prep_report:                      # render what will actually be tagged (forms inlined)
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        pdf.save(tmp.name)
        render_src = tmp.name
    assets = Path(args.assets) if args.assets else None
    if assets:
        assets.mkdir(parents=True, exist_ok=True)

    inv = {"source": str(args.pdf), "facts": {k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
                                             for k, v in facts.items()},
           "prepare": prep_report, "pages": []}
    pre = []
    if facts["xfa"]:
        pre.append(("STOP", "XFA form: dynamic XFA content cannot be tagged with this toolkit"))
    if facts["encrypted"]:
        pre.append(("WARN", "Encrypted file: the output will be saved without encryption — confirm with the owner"))
    for msg in prep_report:
        pre.append(("INFO" if "inlined text-bearing" in msg else "WARN", msg))

    norm_count = Counter()
    store = []
    font_issues = defaultdict(set)
    for pm in pages:
        img = render(render_src, pm.page_no, DPI)
        if assets:
            img.resize((int(img.width * 0.5), int(img.height * 0.5))).save(assets / f"page-{pm.page_no}.png")
        texty = [ln for ln in pm.lines if ln.text.strip()]
        if not texty and any(g.kind in ("image", "form") and area(g.dbox) > 0.5 * pm.disp_w * pm.disp_h for g in pm.graphics):
            pre.append(("STOP", f"page {pm.page_no} has no text, only a page-size image: scanned — run OCR first (WCAG PDF7)"))
        for name, f in pm.fonts.items():
            if not any(s.font == name for ln in pm.lines for s in ln.shows):
                continue
            if not f.embedded and not f.invisible_glyphs:
                font_issues[f"font {f.basefont or name} not embedded (blocks PDF/UA 7.21.4; WCAG unaffected)"].add(pm.page_no)
            if not f.supported:
                font_issues[f"font {f.basefont or name}: CID encoding {f.obj.get('/Encoding')} not decoded"].add(pm.page_no)
        for ln in pm.lines:
            for s in ln.shows:
                for c in s.missing_unicode:
                    fi = pm.fonts.get(s.font)
                    font_issues[f"font {s.font} ({(fi.basefont or fi.subtype) if fi else '?'}) code {c} has no Unicode "
                                f"(add unicode_overrides)"].add(pm.page_no)
        store.append((pm, img))
        for ln in texty:
            b = band_of(ln, pm.read_h)
            if b:
                norm_count[(b, re.sub(r"\d+", "#", ln.text.strip().lower()), round(ln.rbox[1] / 6))] += 1
    for issue, pgs in font_issues.items():
        pre.append(("WARN", f"{issue} — pages {sorted(pgs)[:8]}"))
    # glyphs without Unicode that open a line followed by text are bullets in practice (e.g. LaTeX itemize)
    sugg = {}
    for pm in pages:
        for ln in pm.lines:
            s0 = ln.shows[0] if ln.shows else None
            if s0 and s0.missing_unicode and ln.text.startswith("\ufffd") and len(ln.text.strip()) > 3:
                code = s0.missing_unicode[0]
                sugg.setdefault(f"p{pm.page_no}:{s0.font}", {})[str(code)] = "\u2022"
    inv["unicode_suggestions"] = sugg

    n_pages = len(pages)
    all_text, units_for_lang = [], []
    for pm, img in store:
        P = pm.page_no
        texty = [ln for ln in pm.lines if ln.text.strip()]
        arts = {}
        for ln in texty:
            b = band_of(ln, pm.read_h)
            if not b:
                continue
            norm = re.sub(r"\d+", "#", ln.text.strip().lower())
            repeated = n_pages >= 2 and norm_count[(b, norm, round(ln.rbox[1] / 6))] >= max(2, 0.5 * n_pages)
            if PAGENUM_RE.match(ln.text):
                arts[ln.id] = f"page number ({b})"
            elif repeated:
                arts[ln.id] = f"running {'header' if b == 'top' else 'footer'}"
        for ln in texty:
            if ln.id not in arts and ln.visible and ln.visible_fraction < 0.5:
                arts[ln.id] = "mostly clipped away (a fragment of a line cut by a page or frame edge)"
        content = [ln for ln in texty if ln.id not in arts]
        # figures, charts drawn with paths and form fields take part in the layout as units
        page_area = pm.disp_w * pm.disp_h
        text_boxes_all = [l.dbox for l in texty]
        pre_roles = {}
        for g in pm.graphics:
            a_ = area(g.dbox)
            tc = sum(overlap(g.dbox, tb) for tb in text_boxes_all) / max(a_, 1e-6)
            if g.kind in ("image", "form", "shading"):
                pre_roles[g.id] = "figure?" if 24 * 24 <= a_ < 0.9 * page_area else "artifact"
            elif tc >= 0.5 and len(g.op_indices) > 20:
                pre_roles[g.id] = "text-outline"
            else:
                pre_roles[g.id] = "artifact"
        unit_boxes = []
        for g in pm.graphics:
            if pre_roles[g.id] == "figure?":
                unit_boxes.append(Box(g.id, *g.rbox, baseline=g.rbox[3] - 1))
        for a in pm.annots:
            if a.hidden or a.subtype in ("Link", "Popup"):
                continue
            rb = pm.to_reading(a.dbox)
            row = [l for l in content if l.rbox[1] < rb[3] and l.rbox[3] > rb[1]]
            base = row[0].rbaseline if row else rb[1] + 0.25 * (rb[3] - rb[1])
            unit_boxes.append(Box(a.id, *rb, baseline=base))
        lay = analyze([Box(ln.id, *ln.rbox, baseline=ln.rbaseline) for ln in content] + unit_boxes, rtl=args.rtl)
        rmap = {i: (reg.label or "page") for reg in leaf_regions(lay) for i in reg.ordered_ids()}
        unit_ids = {b.id for b in unit_boxes}
        tables = ruled_tables(content, pm.graphics)
        in_tbl = {l for t in tables for r in t["rows"] for c in r for l in c}
        col_of = {i: x.label for r in regions_with_columns(lay) for x in r.children if x.kind == "column" for i in x.ordered_ids()}
        tables += aligned_tables([l for l in content if l.id not in in_tbl], col_of)
        annots = []
        for a in pm.annots:
            ov = [ln.id for ln in content if overlap(ln.dbox, a.dbox) >= 0.3 * max(area(ln.dbox), 1e-6)]
            cover = 0.0
            if ov:
                lb = [l for l in content if l.id in ov]
                lw = sum(l.dbox[2] - l.dbox[0] for l in lb) or 1
                cover = sum(max(0.0, min(l.dbox[2], a.dbox[2]) - max(l.dbox[0], a.dbox[0])) for l in lb) / lw
            covered = text_in_rect([l for l in content if l.id in ov], a.rect, a.quads) if ov else ""
            if a.subtype == "Link" and not a.quads and len(ov) >= 2 and cover >= 0.95:
                covered = ""                      # rectangle spans whole wrapped lines: the link text is unknown
            annots.append({"id": a.id, "subtype": a.subtype, "dbox": [round(v, 1) for v in a.dbox], "uri": a.uri,
                           "contents": a.contents, "field": a.field_name, "tooltip": a.tooltip, "hidden": a.hidden,
                           "lines": ov, "line_cover": round(cover, 2), "text": covered})
        linked = {l for a in annots if a["subtype"] == "Link" for l in a["lines"]}
        link_cands = []
        for ln in content:
            if ln.id in linked:
                continue
            uri, match = link_target(ln.text)
            if uri:
                share = len(match) / max(1, len(ln.text.strip()))
                link_cands.append({"line": ln.id, "uri": uri, "text": match, "whole_line": share >= 0.6})
        graphics = []
        for g in pm.graphics:
            role = pre_roles[g.id]
            gi = {"id": g.id, "kind": g.kind, "dbox": [round(v, 1) for v in g.dbox],
                  "fill": hexcolor(to_srgb(g.fill)) if g.fill and to_srgb(g.fill) else None,
                  "ops": len(g.op_indices), "role": role,
                  "hint_role": (g.hint or {}).get("role"), "hint_alt": (g.hint or {}).get("alt")}
            if role == "figure?" and assets:
                fp = assets / f"fig-{g.id.replace('.', '-')}.png"
                img.crop(to_px(g.dbox, pm.disp_h, DPI, pad=2)).save(fp)
                gi["crop"] = str(fp)
            graphics.append(gi)
        roles = {g["id"]: g["role"] for g in graphics}
        tbl_rules = {r for t in tables for r in t.get("rules", [])}
        vec = [g for g in pm.graphics if g.kind == "path" and roles[g.id] == "artifact" and g.id not in tbl_rules
               and area(g.dbox) < 0.5 * page_area]
        # group paths that touch or nearly touch (6pt), transitively: an axis line joins the bars of a chart
        clusters = [{"ids": [g.id], "box": tuple(g.dbox)} for g in sorted(vec, key=lambda g: -g.dbox[3])]
        merged = True
        while merged:
            merged = False
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    a, b = clusters[i]["box"], clusters[j]["box"]
                    if a[0] <= b[2] + 6 and a[2] >= b[0] - 6 and a[1] <= b[3] + 6 and a[3] >= b[1] - 6:
                        clusters[i] = {"ids": clusters[i]["ids"] + clusters[j]["ids"],
                                       "box": (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))}
                        del clusters[j]
                        merged = True
                        break
                if merged:
                    break
        vfigs = []
        for c in clusters:
            cb = c["box"]
            text_cover = sum(overlap(l.dbox, cb) for l in content) / max(area(cb), 1e-6)
            # many paths (diagrams, logos) or a few shapes with little text over them (simple charts, icons)
            many = len(c["ids"]) >= 15 and area(cb) >= 3000
            few = (len(c["ids"]) >= 3 and area(cb) >= 2500 and text_cover < 0.2
                   and (cb[2] - cb[0]) < 0.6 * pm.disp_w and (cb[3] - cb[1]) < 0.6 * pm.disp_h)
            if many or few:
                vf = {"graphics": c["ids"], "dbox": [round(v, 1) for v in c["box"]],
                      "lines_inside": [l.id for l in content if overlap(l.dbox, c["box"]) >= 0.8 * max(area(l.dbox), 1e-6)]}
                if assets:
                    fp = assets / f"vecfig-p{P}-{len(vfigs) + 1}.png"
                    img.crop(to_px(c["box"], pm.disp_h, DPI, pad=2)).save(fp)
                    vf["crop"] = str(fp)
                vfigs.append(vf)
        lines_out = []
        for ln in pm.lines:
            m = measure_contrast(img, ln.dbox, pm.disp_h, DPI) if ln.text.strip() else None
            fg, bg, ratio = (m if m else (None, None, None))
            large = ln.size >= 18 or (ln.size >= 14 and ln.bold)
            bullet = None
            if BULLET_RE.match(ln.text):
                bullet = "number" if re.match(r"^\s*\(?(\d{1,3}|[a-zA-Z]|[ivxlcIVXLC]{1,5})[.)](\s|$)", ln.text) else "text"
            elif ln.text.strip() and ln.id not in arts and pm.text_dir == 0 and drawn_bullet(img, ln, pm.disp_h):
                bullet = "drawn"
            hint = ln.hint or {}
            lines_out.append({
                "id": ln.id, "text": ln.text, "size": ln.size, "bold": ln.bold, "italic": ln.italic,
                "dbox": [round(v, 2) for v in ln.dbox], "rbox": [round(v, 2) for v in ln.rbox], "baseline": ln.rbaseline,
                "visible": ln.visible, "clipped": ln.clipped,
                "fg": hexcolor(fg) if fg else None, "bg": hexcolor(bg) if bg else None,
                "contrast": round(ratio, 2) if ratio else None, "large_text": large,
                "region": rmap.get(ln.id), "artifact": arts.get(ln.id) or ("hidden under a later opaque shape" if ln.occluded else None),
                "occluded": ln.occluded, "bullet": bullet,
                "hint_role": hint.get("role"), "hint_path": hint.get("path"), "hint_lang": hint.get("lang"),
                "hint_eid": hint.get("eid"),
                "missing_unicode": sorted({c for s in ln.shows for c in s.missing_unicode}),
            })
        inv["pages"].append({
            "page": P, "disp_w": round(pm.disp_w, 2), "disp_h": round(pm.disp_h, 2), "rotate": pm.rotate,
            "text_dir": pm.text_dir,
            "lines": lines_out, "graphics": graphics, "vector_figures": vfigs, "annots": annots,
            "tables": tables, "link_candidates": link_cands,
            "layout": {"params": lay.params, "reading_order": lay.ordered_ids(),
                       "containers": [{"label": r.label or "page",
                                       "columns": [{"label": c.label, "lines": c.ordered_ids()}
                                                   for c in r.children if c.kind == "column"]}
                                      for r in regions_with_columns(lay)],
                       "regions": [{"label": r.label, "kind": r.kind, "lines": r.ordered_ids()} for r in leaf_regions(lay)],
                       "tree": describe(lay)},
        })
        all_text += [ln.text for ln in content]
        units_for_lang += [(ln.id, ln.text) for ln in content]

    lang, conf = lang_detect.primary(" ".join(all_text)) if all_text else (None, 0.0)
    suggested = lang_detect.with_variant(lang) if lang and conf >= 0.6 else None
    declared = facts["lang"]
    if declared and lang and conf >= 0.6 and declared.split("-")[0].lower() != lang:
        pre.append(("FIX", f"declared /Lang {declared} but the text reads as '{lang}' ({conf:.2f})"))
    cands = lang_detect.foreign_passages(units_for_lang, lang) if lang else []
    inv["language"] = {"inferred": lang, "confidence": round(conf, 2), "suggested": suggested, "declared": declared,
                       "engine": lang_detect.engine(),
                       "foreign_candidates": [{"id": i, "text": t, "lang": l, "confidence": c} for i, t, l, c in cands]}
    if not all_text and not any(p[0] == "STOP" for p in pre):
        pre.append(("STOP", "no text found in the document"))
    inv["preflight"] = [{"level": a, "message": b} for a, b in pre]
    Path(args.json).write_text(json.dumps(inv, ensure_ascii=False, indent=1), encoding="utf-8")

    f = facts
    print(f"== {args.pdf}: {f['pages']} page(s), PDF {f['pdf_version']}, producer {f['producer']!r}, "
          f"tagged={f['has_struct_tree']}, /Lang={f['lang']}, title={f['info_title']!r}, outlines={f['has_outlines']}")
    print("== Preflight")
    for a, b in pre or [("OK", "nothing blocking")]:
        print(f"  [{a}] {b}")
    li = inv["language"]
    print(f"== Language ({li['engine']}): text reads as {li['inferred']} ({li['confidence']}), "
          f"declared {li['declared']}, use {li['suggested']}")
    for c in li["foreign_candidates"][:12]:
        print(f"  other-language candidate {c['id']} {c['lang']} ({c['confidence']}): {c['text'][:60]!r}")
    for p in inv["pages"]:
        print(f"\n== Page {p['page']}  display {p['disp_w']}×{p['disp_h']}pt rotate={p['rotate']} text-direction={p['text_dir']}  "
              f"lines={len(p['lines'])} graphics={len(p['graphics'])} annots={len(p['annots'])}")
        print("  Layout: " + "\n          ".join(p["layout"]["tree"]))
        for c in p["layout"]["containers"]:
            print(f"  Container {c['label']}: " + ", ".join(f"{col['label']} ({len(col['lines'])} lines)" for col in c["columns"]))
        for t in p["tables"]:
            print(f"  Table candidate ({t['kind']}) {len(t['rows'])}×{len(t['rows'][0])} at {t['box']}: rows {t['rows'][:2]}…")
        for a in p["annots"]:
            print(f"  Annotation {a['id']} {a['subtype']} uri={a['uri']} field={a['field']} tooltip={a['tooltip']!r} "
                  f"lines={a['lines']} cover={a['line_cover']}{' HIDDEN' if a['hidden'] else ''}")
        for c in p["link_candidates"]:
            print(f"  Link candidate {c['line']} → {c['uri']} ({'whole line' if c['whole_line'] else 'inside a longer line'})")
        for g in p["graphics"]:
            if g["role"] == "figure?":
                print(f"  Figure candidate {g['id']} {g['kind']} {g['dbox']} existing alt={g['hint_alt']!r} crop={g.get('crop')}")
        for v in p["vector_figures"]:
            print(f"  Vector figure candidate {len(v['graphics'])} paths at {v['dbox']} crop={v.get('crop')}")
        n_outline = sum(1 for g in p["graphics"] if g["role"] == "text-outline")
        if n_outline:
            print(f"  {n_outline} path(s) are outlined text drawn under an invisible text layer → artifacts")
        if not args.quiet:
            print("  Lines: id, size, style, contrast, region, flags, text")
            for l in p["lines"]:
                flags = " ".join(x for x in (
                    "INVIS" if not l["visible"] else "", "CLIP" if l["clipped"] else "", "HIDDEN" if l["occluded"] else "",
                    f"bullet:{l['bullet']}" if l["bullet"] else "", f"ART:{l['artifact']}" if l["artifact"] else "",
                    f"was:{l['hint_role']}" if l["hint_role"] else "",
                    f"NOUNI:{l['missing_unicode']}" if l["missing_unicode"] else "",
                    ("LOW-CONTRAST" if l["contrast"] and l["contrast"] < (3 if l["large_text"] else 4.5) else "")) if x)
                style = ("B" if l["bold"] else "") + ("I" if l["italic"] else "")
                print(f"    {l['id']:9s} {l['size']:5.1f} {style:2s} {str(l['contrast'] or '-'):>5} {str(l['region'] or '-'):9s} "
                      f"{flags:30s} {l['text'].strip()[:90]!r}")
    print(f"\nInventory written to {args.json}")


if __name__ == "__main__":
    main()
