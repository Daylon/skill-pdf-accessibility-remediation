#!/usr/bin/env python3
"""
validate_pdfua.py — review a tagged PDF against WCAG 2.2 (via the W3C PDF
techniques) and the machine-checkable part of PDF/UA-1 (Matterhorn), with a
layout-aware reading-order check.

Usage:
    python3 validate_pdfua.py tagged.pdf [--compare source.pdf] [--report report.json]
                              [--overlay order.png] [--text reading-order.txt] [--rtl]

Output: a checklist summary (same headings and severities as common online
checkers: Document Title, Document Language, Tagged PDF, Structure Tree,
Bookmarks), then every detailed check with severity, WCAG SC, PDF technique
and PDF/UA clause, then the reading order and the detected layout.

Exit code 1 when any check FAILs. WARN and MANUAL never fail the run.
Not a substitute for veraPDF / PAC + a screen-reader pass.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pikepdf

sys.path.insert(0, str(Path(__file__).parent))
from content_model import PageModel, TEXT_SHOW, PATH_PAINT, hexcolor  # noqa: E402
from raster import render, measure_contrast, pixel_diff, draw_reading_order  # noqa: E402
from layout import Box, analyze, compare_order, describe, regions_with_columns  # noqa: E402
import lang_detect  # noqa: E402

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

STANDARD = set("""Document Part Art Sect Div BlockQuote Caption TOC TOCI Index NonStruct Private
P H H1 H2 H3 H4 H5 H6 L LI Lbl LBody Table TR TH TD THead TBody TFoot Span Quote Note Reference
BibEntry Code Link Annot Ruby RB RT RP Warichu WT WP Figure Formula Form""".split())
PAINT = TEXT_SHOW | PATH_PAINT | {"Do", "sh", "INLINE IMAGE"}
BCP47 = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
BLOCK = {"P", "H1", "H2", "H3", "H4", "H5", "H6", "H", "LI", "Figure", "Caption", "TD", "TH", "BlockQuote"}

# severity scale shared with the wcag-accessibility skill and the external checker
SEVERITY = {
    "Document title": "Critical", "XMP title": "Moderate", "Display document title": "Moderate",
    "Document language": "Critical", "Language matches content": "Critical", "Language of parts": "Minor",
    "XMP language": "Minor",
    "Tagged PDF": "Critical",
    "Structure tree": "Critical", "ParentTree ↔ MCID consistency": "Critical", "Reading order vs layout": "Critical",
    "Layout regions in tags": "Moderate",
    "Bookmarks": "Minor",
    "Standard structure types": "Serious", "Headings": "Moderate", "Lists": "Moderate", "Figures": "Critical",
    "Tables": "Serious", "Alternate / actual text encoding": "Serious", "Content is tagged or artifact": "Serious",
    "Marked-content nesting": "Serious", "Fonts & Unicode mapping": "Critical", "Links": "Serious",
    "Text contrast (rendered)": "Serious", "Content order = tag order": "Minor", "PDF/UA identifier": "Minor",
    "Catalog hygiene": "Minor", "Encryption": "Serious", "Visual identity": "Process",
    "Form fields": "Critical", "Hidden text": "Serious", "Font embedding": "Minor",
}
CHECKLIST = [
    ("Document Title", "Critical", "The PDF must have a descriptive document title set in metadata.",
     ["Document title", "XMP title", "Display document title"]),
    ("Document Language", "Critical", "The PDF must declare the primary language so screen readers use correct pronunciation.",
     ["Document language", "Language matches content"]),
    ("Tagged PDF", "Critical", "The PDF must be tagged (MarkInfo/Marked = true) for screen readers to interpret its structure.",
     ["Tagged PDF"]),
    ("Structure Tree", "Critical", "The PDF must contain a structure tree root that defines the logical reading order.",
     ["Structure tree", "ParentTree ↔ MCID consistency", "Reading order vs layout"]),
    ("Bookmarks", "Minor", "Documents with 20 or more pages should have bookmarks for easier navigation.",
     ["Bookmarks"]),
]


class Report:
    def __init__(self):
        self.items = []

    def add(self, status, check, detail, wcag="", technique="", ua=""):
        self.items.append(dict(status=status, check=check, severity=SEVERITY.get(check, "Moderate"),
                               detail=detail, wcag=wcag, technique=technique, ua=ua))

    def ok(self, *a, **k): self.add("PASS", *a, **k)
    def fail(self, *a, **k): self.add("FAIL", *a, **k)
    def warn(self, *a, **k): self.add("WARN", *a, **k)
    def manual(self, *a, **k): self.add("MANUAL", *a, **k)


def tag_of(elem, rolemap):
    s = str(elem.get("/S", ""))[1:]
    seen = set()
    while s not in STANDARD and s in rolemap and s not in seen:
        seen.add(s)
        s = rolemap[s]
    return s


def kids_of(e):
    k = e.get("/K")
    return list(k) if isinstance(k, pikepdf.Array) else ([] if k is None else [k])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--compare")
    ap.add_argument("--report")
    ap.add_argument("--overlay")
    ap.add_argument("--text")
    ap.add_argument("--rtl", action="store_true")
    args = ap.parse_args()

    R = Report()
    pdf = pikepdf.open(args.pdf)
    root = pdf.Root
    n_pages = len(pdf.pages)

    # ================================================================ document
    lang = root.get("/Lang")
    lang = str(lang) if lang is not None else None
    if not lang or not lang.strip():
        R.fail("Document language", "Catalog has no /Lang", "3.1.1", "PDF16", "7.2")
    elif not BCP47.match(lang):
        R.fail("Document language", f"/Lang {lang} is not a valid BCP 47 tag", "3.1.1", "PDF16", "7.2")
    else:
        R.ok("Document language", f"/Lang = {lang}", "3.1.1", "PDF16", "7.2")

    title = pdf.docinfo.get("/Title")
    vp = root.get("/ViewerPreferences")
    ddt = bool(vp.get("/DisplayDocTitle", False)) if vp is not None else False
    with pdf.open_metadata() as m:
        xmp_title = m.get("dc:title")
        xmp_lang = m.get("dc:language")
        ua_part = m.get("pdfuaid:part")
    if not title or not str(title).strip():
        R.fail("Document title", "Info /Title missing" + (f" (XMP has {xmp_title!r})" if xmp_title else ""), "2.4.2", "PDF18")
    elif re.search(r"\.(pdf|indd|fig|docx)$|^[\w-]+--\d+$|untitled", str(title), re.I):
        R.fail("Document title", f"Title is a file name, not descriptive: {str(title)!r}", "2.4.2", "PDF18")
    else:
        R.ok("Document title", f"Info /Title = {str(title)!r}", "2.4.2", "PDF18")
    if not xmp_title:
        R.fail("XMP title", "dc:title missing from XMP", "2.4.2", "PDF18", "7.1-8")
    elif str(xmp_title) != str(title):
        R.warn("XMP title", f"dc:title {xmp_title!r} differs from Info /Title", "2.4.2", "PDF18")
    else:
        R.ok("XMP title", "dc:title matches Info /Title", "2.4.2", "PDF18", "7.1-8")
    (R.ok if ddt else R.fail)("Display document title", f"DisplayDocTitle = {ddt}", "2.4.2", "PDF18", "7.1-10")
    if xmp_lang and lang and lang not in {str(x) for x in xmp_lang}:
        R.warn("XMP language", f"dc:language {sorted(map(str, xmp_lang))} differs from /Lang {lang}", "3.1.1", "PDF16")
    mi = root.get("/MarkInfo")
    marked = bool(mi.get("/Marked", False)) if mi is not None else False
    suspects = bool(mi.get("/Suspects", False)) if mi is not None else False
    (R.ok if marked and not suspects else R.fail)("Tagged PDF", f"MarkInfo Marked={marked} Suspects={suspects}",
                                                 "1.3.1", "", "7.1-11")
    raw_xmp = root.Metadata.read_bytes().decode("utf-8", "replace") if root.get("/Metadata") is not None else ""
    if str(ua_part) == "1":
        R.ok("PDF/UA identifier", "pdfuaid:part = 1 in a well-formed rdf:Description", "", "", "5")
    elif "pdfuaid:part" in raw_xmp:
        R.fail("PDF/UA identifier", "pdfuaid:part sits in a malformed rdf:Description (no rdf:about): "
               "the file claims PDF/UA in a way most parsers ignore", "", "", "5")
    else:
        R.warn("PDF/UA identifier", "No pdfuaid:part — the file does not claim PDF/UA", "", "", "5")
    if "/Info" in root:
        R.warn("Catalog hygiene", "Catalog contains a non-standard /Info key")
    if pdf.is_encrypted:
        R.warn("Encryption", "File is encrypted: check that the accessibility permission bit is set", "", "", "7.16")

    # bookmarks (PDF2): required from 20 pages, like the external checker
    outlines = root.get("/Outlines")
    n_bm = 0
    if outlines is not None:
        stack = [outlines.get("/First")]
        while stack:
            it = stack.pop()
            while it is not None:
                n_bm += 1
                if it.get("/First") is not None:
                    stack.append(it.First)
                it = it.get("/Next")
    if n_pages >= 20 and n_bm == 0:
        R.fail("Bookmarks", f"{n_pages} pages and no bookmarks", "2.4.5", "PDF2")
    elif n_bm:
        R.ok("Bookmarks", f"{n_bm} bookmarks for {n_pages} page(s)", "2.4.5", "PDF2")
    else:
        R.ok("Bookmarks", f"Not required: {n_pages} page(s) (< 20)", "2.4.5", "PDF2")

    # ================================================================ structure tree
    st = root.get("/StructTreeRoot")
    if st is None:
        R.fail("Structure tree", "No StructTreeRoot: assistive technology gets no structure or reading order",
               "1.3.1 / 1.3.2", "PDF3/PDF9", "7.1")
        finish(R, args, None, untagged_order(args, pdf, R))
        return
    rolemap = {str(k)[1:]: str(v)[1:] for k, v in st.get("/RoleMap", {}).items()} if st.get("/RoleMap") is not None else {}
    page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}

    elems = []                          # (elem, depth, std_tag) in pre-order
    parent_of = {}                      # objgen -> parent elem (or None)
    mcid_refs = defaultdict(list)       # (page_i, mcid) -> [elem]
    mcid_seq = []                       # (page_i, mcid, owner elem) in tag order
    objr_refs = {}
    nonstandard = []

    def visit(e, depth, parent, page_ctx):
        if not isinstance(e, pikepdf.Dictionary):
            return
        if e.get("/Type") == "/MCR":
            pg = e.get("/Pg", page_ctx)
            key = (page_index.get(pg.objgen) if pg is not None else None, int(e.MCID))
            mcid_refs[key].append(parent); mcid_seq.append((*key, parent))
            return
        if e.get("/Type") == "/OBJR":
            objr_refs[e.Obj.objgen] = parent
            return
        std = tag_of(e, rolemap)
        if std not in STANDARD:
            nonstandard.append(str(e.get("/S", ""))[1:])
        elems.append((e, depth, std))
        parent_of[e.objgen] = parent
        pg = e.get("/Pg", page_ctx)
        for c in kids_of(e):
            if isinstance(c, pikepdf.Dictionary):
                visit(c, depth + 1, e, pg)
            else:
                try:
                    key = (page_index.get(pg.objgen) if pg is not None else None, int(c))
                except Exception:  # noqa: BLE001
                    continue
                mcid_refs[key].append(e); mcid_seq.append((*key, e))

    for c in kids_of(st):
        visit(c, 0, None, None)

    tags_in_order = [t for _, _, t in elems]
    (R.ok if elems else R.fail)("Structure tree",
                                f"StructTreeRoot with {len(elems)} elements under {tags_in_order[0] if elems else '—'}; "
                                f"{len(mcid_seq)} content references" if elems else "StructTreeRoot is empty",
                                "1.3.1 / 1.3.2", "PDF3/PDF9", "7.1")
    if nonstandard:
        R.fail("Standard structure types", f"Not role-mapped to a standard type: {sorted(set(nonstandard))}", "1.3.1", "", "7.1")
    else:
        R.ok("Standard structure types", f"{len(elems)} elements, all standard or role-mapped", "1.3.1", "", "7.1")

    hs = [int(t[1]) for t in tags_in_order if re.fullmatch(r"H[1-6]", t)]
    if "H" in tags_in_order and hs:
        R.fail("Headings", "Both H and H1-H6 used", "1.3.1", "PDF9", "7.4.4")
    if not hs:
        R.warn("Headings", "No numbered headings: screen-reader users cannot jump between sections", "1.3.1 / 2.4.6", "PDF9", "7.4")
    else:
        skips = [(a, b) for a, b in zip([0] + hs, hs) if b > a + 1]
        if hs[0] != 1:
            R.fail("Headings", f"First heading is H{hs[0]}", "1.3.1", "PDF9", "7.4.2")
        elif skips:
            R.fail("Headings", f"Skipped levels: {skips}", "1.3.1", "PDF9", "7.4.2")
        else:
            R.ok("Headings", f"Outline H{'→H'.join(map(str, sorted(set(hs))))}, {len(hs)} headings, no skipped level",
                 "1.3.1 / 2.4.6", "PDF9", "7.4.2")

    bad_list = []
    for e, d, t in elems:
        kt = [tag_of(c, rolemap) for c in kids_of(e) if isinstance(c, pikepdf.Dictionary) and c.get("/S") is not None]
        if t == "L" and any(x not in ("LI", "Caption", "L") for x in kt):
            bad_list.append(f"L contains {kt}")
        if t == "LI" and any(x not in ("Lbl", "LBody") for x in kt):
            bad_list.append(f"LI contains {kt}")
    if bad_list:
        R.fail("Lists", "; ".join(bad_list), "1.3.1", "PDF21", "7.6")
    elif "L" in tags_in_order:
        R.ok("Lists", f"{tags_in_order.count('L')} lists, {tags_in_order.count('LI')} items, L>LI>(Lbl|LBody) respected",
             "1.3.1", "PDF21", "7.6")

    figs = [e for e, _, t in elems if t in ("Figure", "Formula")]
    nofig_alt = [e for e in figs if e.get("/Alt") is None and e.get("/ActualText") is None]
    if nofig_alt:
        R.fail("Figures", f"{len(nofig_alt)} Figure/Formula without /Alt", "1.1.1", "PDF1", "7.3")
    else:
        R.ok("Figures", f"{len(figs)} figures with alternate text" if figs else
             "No informative images; all graphics are artifacts", "1.1.1", "PDF1/PDF4", "7.3")

    if any(t in ("Table", "TR", "TH", "TD") for t in tags_in_order):
        ths = [e for e, _, t in elems if t == "TH"]
        noscope = [e for e in ths if e.get("/A") is None]
        (R.warn if noscope else R.ok)("Tables", f"{len(ths)} TH, {len(noscope)} without Scope", "1.3.1", "PDF6", "7.5")

    bad_at = []
    for e, _, t in elems:
        for key in ("/ActualText", "/Alt"):
            v = e.get(key)
            if v is not None:
                s = str(v)
                if any((ord(ch) < 32 and ch not in "\n\t") for ch in s) or "�" in s:
                    bad_at.append(f"{t} {key}={s!r}")
    (R.fail if bad_at else R.ok)("Alternate / actual text encoding",
                                 "; ".join(bad_at[:6]) if bad_at else "No control characters or U+FFFD in /Alt or /ActualText",
                                 "1.1.1 / 4.1.2", "", "7.3")

    # ================================================================ content vs tags
    pt = st.get("/ParentTree")
    pt_map = {}
    if pt is not None:
        def nt(node):
            if "/Nums" in node:
                arr = list(node.Nums)
                for a, b in zip(arr[0::2], arr[1::2]):
                    pt_map[int(a)] = b
            for kid in node.get("/Kids", []):
                nt(kid)
        nt(pt)

    untagged, nested_bad, missing_owner, orphan_refs = [], [], [], []
    not_embedded = set()
    mcid_text = {}
    mcid_graphics = {}
    font_problems = []
    pages_models = []
    for pi, page in enumerate(pdf.pages):
        pm = PageModel(pdf, pi + 1)
        pages_models.append(pm)
        sp = page.obj.get("/StructParents")
        arr = pt_map.get(int(sp)) if sp is not None else None
        show_by_op = {s.op_index: s for ln in pm.lines for s in ln.shows}
        g_by_op = {g.op_indices[-1]: g for g in pm.graphics}
        stack, seen_mcids = [], set()
        for i, ins in enumerate(pm.ops):
            o = str(ins.operator)
            if o == "BMC":
                stack.append(("ART",) if str(ins.operands[0]) == "/Artifact" else ("OTHER",))
            elif o == "BDC":
                tagname = str(ins.operands[0])
                props = ins.operands[1]
                if isinstance(props, pikepdf.Name):
                    props = page.obj.Resources.get("/Properties", {}).get(props, pikepdf.Dictionary())
                if tagname == "/Artifact":
                    stack.append(("ART",))
                elif isinstance(props, pikepdf.Dictionary) and "/MCID" in props:
                    mc = int(props.MCID)
                    if any(s[0] == "ART" for s in stack):
                        nested_bad.append(f"p{pi+1} MCID {mc} inside an Artifact")
                    if any(s[0] == "MC" for s in stack):
                        nested_bad.append(f"p{pi+1} MCID {mc} nested in another MCID")
                    stack.append(("MC", mc)); seen_mcids.add(mc)
                else:
                    stack.append(("OTHER",))
            elif o == "EMC":
                if stack:
                    stack.pop()
            elif o in PAINT:
                ctx = next((s for s in reversed(stack) if s[0] in ("ART", "MC")), None)
                if ctx is None:
                    untagged.append(f"p{pi+1} op#{i} {o}")
                elif ctx[0] == "MC" and o in TEXT_SHOW and i in show_by_op:
                    mcid_text.setdefault((pi, ctx[1]), []).append(show_by_op[i])
                elif ctx[0] == "MC" and i in g_by_op:
                    mcid_graphics.setdefault((pi, ctx[1]), []).append(g_by_op[i])
        if pt is None:
            missing_owner.append("no ParentTree")
        for mc in sorted(seen_mcids):
            try:
                owner = arr[mc] if arr is not None else None
            except IndexError:
                owner = None
            if owner is None or not isinstance(owner, pikepdf.Dictionary):
                missing_owner.append(f"p{pi+1} MCID {mc}")
            elif owner.objgen not in {e.objgen for e in mcid_refs.get((pi, mc), []) if e is not None}:
                missing_owner.append(f"p{pi+1} MCID {mc}: ParentTree owner does not list it")
        for (p_, mc) in list(mcid_refs):
            if p_ == pi and mc not in seen_mcids:
                orphan_refs.append(f"p{pi+1} MCID {mc} referenced by tree but absent from content")
        for name, f in pm.fonts.items():
            if not f.cmap and f.subtype not in ("/Type1", "/TrueType"):
                font_problems.append(f"{name} ({f.subtype}) has no ToUnicode")
            if not f.embedded and not f.invisible_glyphs:
                not_embedded.add(f.basefont or name)
            if f.subtype == "/Type3":
                enc, cps, diffs = f.obj.get("/Encoding"), f.obj.get("/CharProcs"), {}
                if enc is not None and enc.get("/Differences") is not None:
                    code = 0
                    for x in enc.Differences:
                        if isinstance(x, pikepdf.Name):
                            diffs[code] = str(x); code += 1
                        else:
                            code = int(x)
                for ln in pm.lines:
                    for s in ln.shows:
                        if s.font != name:
                            continue
                        for item in (s.operands[0] if s.operator == "TJ" else [s.operands[-1]]):
                            if isinstance(item, pikepdf.String):
                                for c in bytes(item):
                                    g = diffs.get(c)
                                    if g is None or cps is None or g not in cps:
                                        font_problems.append(f"{name} code {c} has no glyph (.notdef)")
        for ln in pm.lines:
            bad = [c for s in ln.shows for c in s.missing_unicode]
            if bad:
                font_problems.append(f"{ln.id} codes without Unicode: {sorted(set(bad))}")

    (R.fail if untagged else R.ok)("Content is tagged or artifact",
                                   f"{len(untagged)} painting operators outside marked content" + (f": {untagged[:5]}" if untagged else ""),
                                   "1.3.1", "PDF4", "7.1-3")
    (R.fail if nested_bad else R.ok)("Marked-content nesting", "; ".join(nested_bad[:5]) or "No tagged content inside artifacts, no nested MCIDs",
                                     "1.3.1", "", "7.1")
    (R.fail if (missing_owner or orphan_refs) else R.ok)(
        "ParentTree ↔ MCID consistency",
        "; ".join((missing_owner + orphan_refs)[:6]) or f"{len(mcid_text)} MCIDs, each owned by exactly the element that references it",
        "1.3.1 / 4.1.2", "", "7.1")
    if not_embedded:
        (R.fail if str(ua_part) == "1" else R.warn)(
            "Font embedding", f"not embedded: {sorted(not_embedded)} — WCAG is unaffected (text stays real text); "
            "PDF/UA-1 requires embedding, which would mean changing how glyphs are drawn", "", "", "7.21.4")
    else:
        R.ok("Font embedding", "every font used is embedded", "", "", "7.21.4")
    (R.fail if font_problems else R.ok)("Fonts & Unicode mapping", "; ".join(sorted(set(font_problems))[:8]) or
                                        "All fonts map every used code to Unicode; no .notdef glyphs",
                                        "1.1.1 / 1.3.1", "", "7.21")

    # ================================================================ annotations
    annot_issues, form_issues, n_links, n_widgets, n_other = [], [], 0, 0, 0
    for pi, page in enumerate(pdf.pages):
        annots = page.obj.get("/Annots", []) or []
        if len(annots) and page.obj.get("/Tabs") != "/S":
            annot_issues.append(f"p{pi+1} has annotations but /Tabs is not /S")
        for a in annots:
            if not isinstance(a, pikepdf.Dictionary):
                continue
            st_ = str(a.get("/Subtype"))
            flags = int(a.get("/F", 0))
            if st_ == "/Popup" or flags & 2 or flags & 32:
                continue
            owner = objr_refs.get(a.objgen)
            if owner is None:
                annot_issues.append(f"p{pi+1} {st_} annotation not referenced by an OBJR")
            sp = a.get("/StructParent")
            if sp is None or pt_map.get(int(sp)) is None:
                annot_issues.append(f"p{pi+1} {st_} annotation missing /StructParent in ParentTree")
            if st_ == "/Link":
                n_links += 1
                if owner is not None and tag_of(owner, rolemap) != "Link":
                    annot_issues.append("Link annotation not nested in a Link element")
                if not str(a.get("/Contents", "")).strip():
                    annot_issues.append("Link annotation without /Contents")
            elif st_ == "/Widget":
                n_widgets += 1
                if owner is not None and tag_of(owner, rolemap) != "Form":
                    form_issues.append("form field not nested in a Form element")
                f, tu = a, None
                while f is not None and tu is None:
                    tu = f.get("/TU")
                    f = f.get("/Parent")
                if tu is None or not str(tu).strip() or str(tu).startswith("TODO"):
                    form_issues.append(f"p{pi+1} form field without an accessible name (/TU)")
            else:
                n_other += 1
                if owner is not None and tag_of(owner, rolemap) not in ("Annot", "Link", "Form"):
                    annot_issues.append(f"{st_} annotation not nested in an Annot element")
    if n_links or n_other or annot_issues:
        (R.fail if annot_issues else R.ok)("Links", "; ".join(sorted(set(annot_issues))[:5]) or
                                           f"{n_links} link and {n_other} other annotation(s): tagged, OBJR + /Contents + StructParent, /Tabs /S",
                                           "2.1.1 / 2.4.4 / 4.1.2", "PDF11/PDF13", "7.18")
    if n_widgets or form_issues:
        (R.fail if form_issues else R.ok)("Form fields", "; ".join(sorted(set(form_issues))[:5]) or
                                          f"{n_widgets} field(s) in Form elements with an accessible name (/TU)",
                                          "1.3.1 / 4.1.2 / 3.3.2", "PDF10/PDF12", "7.18.4")

    # ================================================================ blocks, text, lines in tag order
    line_of_show = {}
    for pi, pm in enumerate(pages_models):
        for ln in pm.lines:
            for s in ln.shows:
                line_of_show[(pi, s.op_index)] = ln

    def mcids_under(e):
        out = []
        pg = e.get("/Pg")
        for c in kids_of(e):
            if isinstance(c, pikepdf.Dictionary):
                if c.get("/Type") == "/MCR":
                    p = c.get("/Pg", pg)
                    out.append((page_index.get(p.objgen) if p is not None else 0, int(c.MCID)))
                elif c.get("/S") is not None:
                    out += mcids_under(c)
            else:
                try:
                    out.append((page_index.get(pg.objgen) if pg is not None else 0, int(c)))
                except Exception:  # noqa: BLE001
                    pass
        return out

    def lines_of(keys):
        out = []
        for key in keys:
            for s in mcid_text.get(key, []):
                ln = line_of_show.get((key[0], s.op_index))
                if ln is not None and ln not in out:
                    out.append(ln)
        return out

    def eff_lang(e):
        x = e
        while x is not None:
            if x.get("/Lang") is not None:
                return str(x.Lang)
            x = parent_of.get(x.objgen)
        return lang

    def joined_text(keys):
        """Text of marked-content sequences in tag order; a line break reads as a space (as screen readers do)."""
        parts, prev = [], None
        for k in keys:
            t = "".join(s.text for s in mcid_text.get(k, []))
            if not t:
                continue
            ls = lines_of([k])
            cur = ls[0] if ls else None
            if parts:
                new_line = prev is None or cur is None or abs(cur.rbaseline - prev.rbaseline) > 1.5
                if new_line and not parts[-1].endswith((" ", "-", "\u00ad", "/")) and not t.startswith(" "):
                    parts.append(" ")
            parts.append(t)
            prev = cur or prev
        return "".join(parts)

    blocks = []
    for e, d, t in elems:
        if t not in BLOCK:
            continue
        keys = mcids_under(e)
        lines = lines_of(keys)
        txt = str(e.ActualText) if e.get("/ActualText") is not None else joined_text(keys)
        gfx = [g for k in keys for g in mcid_graphics.get(k, [])]
        blocks.append(dict(elem=e, tag=t, depth=d, text=re.sub(r"\s+", " ", txt).strip(), lines=lines, graphics=gfx,
                           page=keys[0][0] if keys else 0, lang=eff_lang(e), own_lang=e.get("/Lang")))

    # the most prominent text of the first page is almost always a heading (the document's subject)
    first = [b for b in blocks if b["lines"] and b["text"]]
    if first:
        p0 = first[0]["page"]
        sizes = sorted(ln.size for b in first if b["page"] == p0 for ln in b["lines"])
        med = sizes[len(sizes) // 2] if sizes else 0
        big = max((b for b in first if b["page"] == p0), key=lambda b: max(ln.size for ln in b["lines"]))
        big_size = max(ln.size for ln in big["lines"])
        if med and big_size >= 1.4 * med and not re.fullmatch(r"H[1-6]?", big["tag"]):
            R.warn("Headings", f"the largest text on page {p0 + 1} ({big_size:g}pt, {big['text'][:40]!r}) is tagged "
                   f"{big['tag']}, not a heading — check it is not the document's title", "1.3.1 / 2.4.6", "PDF9", "7.4")

    reading = []
    for n, b in enumerate(blocks, 1):
        extra = []
        if b["own_lang"] is not None:
            extra.append(f"lang={b['own_lang']}")
        if b["elem"].get("/Alt") is not None:
            extra.append(f"alt={str(b['elem'].Alt)!r}")
        reading.append(f"{n:>3} p{b['page'] + 1} {'  ' * max(0, b['depth'] - 1)}{b['tag']:<7} {b['text']}"
                       + (f"   [{', '.join(extra)}]" if extra else ""))

    # alt text quality
    alt_issues = []
    for e, _, t in elems:
        if e.get("/Alt") is not None:
            a = str(e.Alt).strip()
            if a.upper().startswith("TODO") or not a and t == "Figure":
                alt_issues.append(f"{t}: placeholder alt {a[:40]!r}")
            elif re.search(r"\.(png|jpe?g|gif|svg|tiff?|bmp)$|^(image|img|figure|picture|graphic)\s*\d*$", a, re.I):
                alt_issues.append(f"{t}: alt looks like a file name or a generic word: {a!r}")
    if alt_issues:
        R.fail("Figures", "; ".join(alt_issues[:4]), "1.1.1", "PDF1", "7.3")

    # hidden text exposed to AT
    hidden = [ln.id for pm in pages_models for ln in pm.lines if ln.occluded and any(s.mcid is not None and s.mcid >= 0 for s in ln.shows)]
    hidden = [h for h in hidden if any(k for k in mcid_text if any(line_of_show.get((k[0], s.op_index)) and line_of_show[(k[0], s.op_index)].id == h for s in mcid_text[k]))]
    if hidden:
        R.warn("Hidden text", f"{len(hidden)} line(s) covered by an opaque shape are tagged as content: {hidden[:5]} — "
               "readers hear text sighted users cannot see; make them artifacts unless intended", "1.3.1", "", "")

    # ================================================================ language
    all_text = " ".join(b["text"] for b in blocks)
    inferred, conf = lang_detect.primary(all_text) if all_text.strip() else (None, 0.0)
    if lang and inferred and conf >= 0.6 and lang.split("-")[0].lower() != inferred:
        R.fail("Language matches content", f"/Lang={lang} but the text reads as '{inferred}' "
               f"(confidence {conf:.2f}, {lang_detect.engine()}): screen readers will mispronounce it", "3.1.1", "PDF16", "7.2")
    elif inferred:
        R.ok("Language matches content", f"text reads as '{inferred}' ({conf:.2f}, {lang_detect.engine()}), matches /Lang={lang}",
             "3.1.1", "PDF16")
    base_lang = lang
    if inferred and conf >= 0.6 and (not lang or lang.split("-")[0].lower() != inferred):
        base_lang = inferred
    units = [(i, b["text"]) for i, b in enumerate(blocks) if b["own_lang"] is None and b["lang"] == lang]
    untagged_foreign = [f"#{i+1} {t[:40]!r} reads as {l} ({c})" for i, t, l, c in lang_detect.foreign_passages(units, base_lang)]
    tagged_parts = [(b["tag"], str(b["own_lang"]), b["text"][:30]) for b in blocks if b["own_lang"] is not None]
    bad_parts = [x for x in tagged_parts if not BCP47.match(x[1])]
    if bad_parts:
        R.fail("Language of parts", f"invalid /Lang: {bad_parts}", "3.1.2", "PDF19", "7.2")
    elif untagged_foreign:
        R.warn("Language of parts", f"{len(untagged_foreign)} block(s) may be in another language without /Lang "
               "(exempt if proper names or jargon): " + "; ".join(untagged_foreign[:4]), "3.1.2", "PDF19", "7.2")
    else:
        R.ok("Language of parts", f"{len(tagged_parts)} passage(s) with their own /Lang; no other block reads as another language",
             "3.1.2", "PDF19", "7.2")

    # ================================================================ reading order vs layout (page by page)
    # tables, figures and formulas are judged as one unit each (their inside is read row by row / not at all)
    unit_of = {}          # line id -> (representative id, [members])  for lines inside tables / figures
    gunits = {}           # page -> [(rep id, rbox, first mcid position)] for figures made of graphics only
    for e, _, t in elems:
        if t in ("Table", "Figure", "Formula"):
            keys = mcids_under(e)
            by_page = defaultdict(list)
            for ln in lines_of(keys):
                by_page[ln.id.split(".")[0]].append(ln)
            for ls in by_page.values():
                for ln in ls:
                    unit_of.setdefault(ln.id, (ls[0].id, ls))
            if t != "Table" and not by_page:
                gs_ = [g for k in keys for g in mcid_graphics.get(k, [])]
                if gs_:
                    gunits.setdefault(keys[0][0], []).append((gs_[0].id, gs_, keys[0][1]))
    layouts, order_msgs, region_msgs, region_ok_count = {}, [], [], 0
    for pi, pm in enumerate(pages_models):
        tagged_lines = []
        for (p_, mc, _owner) in mcid_seq:
            if p_ != pi:
                continue
            for ln in lines_of([(p_, mc)]):
                if ln not in tagged_lines:
                    tagged_lines.append(ln)
        seq, boxes, seen = [], [], set()
        # graphic-only figures enter the sequence at the position of their first MCID
        gu = sorted(gunits.get(pi, []), key=lambda u: u[2])
        mc_pos = {}
        for (p_, mc, _o) in mcid_seq:
            if p_ == pi:
                mc_pos.setdefault(mc, len(mc_pos))
        line_mc = {}
        for (p_, mc, _o) in mcid_seq:
            if p_ == pi:
                for ln in lines_of([(p_, mc)]):
                    line_mc.setdefault(ln.id, mc_pos[mc])
        pending = [(mc_pos.get(u[2], 1e9), u) for u in gu]

        def flush_units(limit):
            while pending and pending[0][0] <= limit:
                _, (rep, gs_, _mc) = pending.pop(0)
                rb = [pm.to_reading(g.dbox) for g in gs_]
                boxes.append(Box(rep, min(b[0] for b in rb), min(b[1] for b in rb), max(b[2] for b in rb), max(b[3] for b in rb),
                                 baseline=max(b[3] for b in rb) - 1))
                seq.append(rep)
        for ln in tagged_lines:
            flush_units(line_mc.get(ln.id, 1e9))
            if not ln.text.strip():
                continue
            rep, group = unit_of.get(ln.id, (ln.id, [ln]))
            if rep in seen:
                continue
            seen.add(rep)
            seq.append(rep)
            g = [x for x in group if x.text.strip()] or group
            boxes.append(Box(rep, min(x.rbox[0] for x in g), min(x.rbox[1] for x in g), max(x.rbox[2] for x in g),
                             max(x.rbox[3] for x in g), baseline=max(x.rbaseline for x in g),
                             n=len({round(x.rbaseline) for x in g}),
                             lh=sorted(x.rbox[3] - x.rbox[1] for x in g)[len(g) // 2]))
        flush_units(1e9)
        if not boxes:
            continue
        lay = analyze(boxes, rtl=args.rtl)
        layouts[pi] = lay
        cmp = compare_order(seq, lay)
        lay.compare = cmp
        txt = {ln.id: ln.text.strip() for ln in tagged_lines}
        if not cmp["ok"]:
            detail = []
            for b_id, reg in cmp["revisits"][:3]:
                detail.append(f"returns to {reg} at {txt.get(b_id, b_id)[:28]!r}")
            for a, b, ra, rb in cmp["region_jumps"][:3]:
                detail.append(f"jumps back from {ra} to {rb} ({txt.get(a, a)[:20]!r} → {txt.get(b, b)[:20]!r})")
            for a, b, reg in cmp["inversions"][:3]:
                detail.append(f"goes up inside {reg} ({txt.get(a, a)[:20]!r} → {txt.get(b, b)[:20]!r})")
            order_msgs.append(f"p{pi+1}: regions read {' → '.join(cmp['region_sequence'])}; "
                              f"layout says {' → '.join(cmp['expected_region_sequence'])}. " + "; ".join(detail))
        g_rep = {g.id: rep for (rep, gs_, _mc) in gunits.get(pi, []) for g in gs_}
        line_sets = {}
        for e, _, t in elems:
            ks = [k for k in mcids_under(e) if k[0] == pi]
            line_sets[e.objgen] = (t, {unit_of.get(ln.id, (ln.id,))[0] for ln in lines_of(ks)}
                                   | {g_rep[g.id] for k in ks for g in mcid_graphics.get(k, []) if g.id in g_rep})
        for container in regions_with_columns(lay):
            for col in [c for c in container.children if c.kind == "column"]:
                ids = set(col.ordered_ids())
                cands = [(len(s_), t, s_) for t, s_ in line_sets.values() if ids <= s_]
                if not cands:
                    region_msgs.append(f"p{pi+1} {col.label}: no element holds all its lines")
                    continue
                _size, t, s_ = min(cands, key=lambda c: c[0])
                if s_ == ids:
                    region_ok_count += 1
                else:
                    region_msgs.append(f"p{pi+1} {col.label}: smallest enclosing element is {t} which also holds "
                                       f"{len(s_ - ids)} lines from other regions")
    if order_msgs:
        R.fail("Reading order vs layout", " | ".join(order_msgs[:4]), "1.3.2", "PDF3", "7.2 (logical order)")
    elif layouts:
        seqs = [f"p{pi+1}: " + " → ".join(l.compare["region_sequence"]) for pi, l in layouts.items()]
        R.ok("Reading order vs layout", "tag order follows the layout (rows, then columns) — " + " | ".join(seqs[:6])
             + (" …" if len(seqs) > 6 else ""), "1.3.2", "PDF3", "7.2")
    if region_msgs:
        R.warn("Layout regions in tags", "; ".join(region_msgs[:4]) +
               " — wrap each row/column in its own Div so reflow and AT keep regions together", "1.3.1 / 1.3.2", "PDF3", "")
    elif region_ok_count:
        R.ok("Layout regions in tags", f"each of the {region_ok_count} detected columns is one self-contained subtree",
             "1.3.1 / 1.3.2", "PDF3", "")

    # ================================================================ contrast (rendered), every page
    low, rows = [], []
    renders = {}
    for pi, pm in enumerate(pages_models):
        page_blocks = [b for b in blocks if b["page"] == pi]
        if not page_blocks:
            continue
        img = render(args.pdf, pi + 1, 200)
        renders[pi] = img
        for b in page_blocks:
            for ln in b["lines"]:
                if not ln.text.strip() or ln.visible_fraction < 0.5:
                    continue                      # fragments cut by a page or frame edge: no readable text to measure
                m = measure_contrast(img, ln.dbox, pm.disp_h, 200)
                if m:
                    fg, bg, ratio = m
                    need = 3.0 if (ln.size >= 18 or (ln.size >= 14 and ln.bold)) else 4.5
                    rows.append((ln.id, ln.size, hexcolor(fg), hexcolor(bg), round(ratio, 2), need))
                    if ratio < need:
                        low.append(f"{ln.id} {ln.text.strip()[:30]!r} {ratio:.2f}:1 < {need}:1 at {ln.size}pt")
    if rows:
        mn = min(rows, key=lambda r: r[4] / r[5])
        (R.fail if low else R.ok)("Text contrast (rendered)", (f"{len(low)} line(s) below threshold: " + "; ".join(low[:4])) if low else
                                  f"{len(rows)} lines measured; tightest {mn[0]} {mn[2]} on {mn[3]} = {mn[4]}:1 at {mn[1]}pt (needs {mn[5]}:1)",
                                  "1.4.3", "", "")

    # content order (what tools that ignore tags read) vs tag order
    diffs = 0
    for pi, pm in enumerate(pages_models):
        content_order = []
        for ln in pm.lines:
            for s in ln.shows:
                if s.mcid is not None and s.mcid >= 0 and (not content_order or content_order[-1] != s.mcid):
                    content_order.append(s.mcid)
        tag_order = [mc for (p_, mc, _o) in mcid_seq if p_ == pi]
        co = [m for m in dict.fromkeys(content_order)]
        to = [m for m in dict.fromkeys(tag_order) if m in set(co)]
        if co != to:
            diffs += 1
    (R.ok if not diffs else R.warn)("Content order = tag order",
                                     "content streams paint tagged text in reading order" if not diffs else
                                     f"{diffs} page(s) paint text in another order than the tags (tools ignoring tags — "
                                     "copy/paste, some ATS parsers — will read that order)", "1.3.2", "PDF3", "")

    # PDF/UA claim must be backed
    if str(ua_part) == "1":
        blockers = []
        for pm in pages_models:
            for name, f in pm.fonts.items():
                uses = [s for ln in pm.lines for s in ln.shows if s.font == name]
                if uses and not f.embedded and not all(s.state["Tr"] in (3, 7) for s in uses):
                    blockers.append(f"font {f.basefont or name} not embedded")
        if font_problems:
            blockers.append("glyphs without Unicode")
        if annot_issues or form_issues:
            blockers.append("annotations not fully tagged")
        if nonstandard:
            blockers.append("non-standard structure types")
        if blockers:
            R.fail("PDF/UA identifier", "claims PDF/UA-1 but: " + "; ".join(sorted(set(blockers))[:5]), "", "", "5")

    if args.compare:
        src = pikepdf.open(args.compare)
        changed = []
        for pi in range(len(pdf.pages)):
            if pi >= len(src.pages):
                changed.append(f"p{pi+1} missing in source")
                continue
            a, b = render(args.compare, pi + 1, 110), render(args.pdf, pi + 1, 110)
            d = pixel_diff(a, b)
            if not d.get("same_size") or d["pixels_changed"]:
                changed.append(f"p{pi+1}: {d}")
        (R.ok if not changed else R.fail)("Visual identity", f"{len(pdf.pages)} page(s) rendered at 110 dpi are pixel-identical to the source"
                                          if not changed else "; ".join(changed[:4]), "", "", "")

    R.manual("Screen reader pass", "NVDA + Acrobat Reader (Windows) or VoiceOver (macOS): headings list, list counts, link names, reading order", "1.3.1 / 2.4.4", "PDF9/PDF11/PDF21")
    R.manual("veraPDF PDF/UA-1 + PAC", "Run veraPDF (PDF/UA-1 profile) and PAC 2024 for the full Matterhorn machine checks", "", "", "all")

    # ================================================================ overlays (one per page)
    if args.overlay:
        outp = Path(args.overlay)
        for pi, lay in layouts.items():
            target = outp if len(layouts) == 1 else outp.with_name(f"{outp.stem}-p{pi + 1}{outp.suffix}")
            page_blocks = [b for b in blocks if b["page"] == pi and (b["lines"] or b["graphics"])]
            page_overlay(args, pages_models[pi], pi, lay, page_blocks, unit_of, target)

    if args.text:
        Path(args.text).write_text("\n".join(reading) + "\n", encoding="utf-8")
    finish(R, args, reading, {pi: describe(l) for pi, l in layouts.items()})


def overlay_regions(lay, pm):
    """Rows and columns of a layout tree, as (label, kind, display bbox) for the overlay."""
    regions = []

    def collect(r, inside_column=False):
        has_cols = any(c.kind == "column" for c in r.children)
        if r.kind == "column" or (r.kind == "row" and (not inside_column or has_cols)):
            regions.append((r.label, r.kind, r.bbox))
        for c in r.children:
            collect(c, inside_column or r.kind == "column")
    collect(lay)
    # regions were computed in the reading frame: map their boxes back to display space
    inv_rot = {0: lambda x, y: (x, y), 90: lambda x, y: (pm.disp_w - y, x),
               270: lambda x, y: (y, pm.disp_h - x), 180: lambda x, y: (pm.disp_w - x, pm.disp_h - y)}[pm.text_dir]

    def back(b):
        pts = [inv_rot(x, y) for x, y in ((b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3]))]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    return [(lab, kind, back(bb)) for lab, kind, bb in regions]


def page_overlay(args, pm, pi, lay, page_blocks, unit_of, target):
    """Draw the reading path of page_blocks ({tag, lines, graphics}) over the page, against the layout."""
    cmp = lay.compare
    rmap = {i: r for i, r in cmp["path"]}
    exp_rank = {r: k for k, r in enumerate(cmp["expected_region_sequence"])}
    regions = overlay_regions(lay, pm)
    items, kinds = [], []
    prev_reg, visited, prev_last = None, [], None
    for n, b in enumerate(page_blocks, 1):
        ls = b["lines"]
        boxes_ = [l.dbox for l in ls] + [g.dbox for g in b["graphics"]]
        bb = (min(x[0] for x in boxes_), min(x[1] for x in boxes_), max(x[2] for x in boxes_), max(x[3] for x in boxes_))
        rep = unit_of.get(ls[0].id, (ls[0].id,))[0] if ls else b["graphics"][0].id
        reg = rmap.get(rep)
        kind = "same"
        if prev_reg is not None and reg != prev_reg:
            kind = "bad" if (reg in visited or exp_rank.get(reg, 0) < exp_rank.get(prev_reg, 0)) else "next"
        elif ls and prev_last is not None and ls[0].rbaseline > prev_last.rbaseline + 2 and rep not in unit_of:
            a_, b_ = prev_last.rbox, ls[0].rbox
            h_ = min(a_[3] - a_[1], b_[3] - b_[1])
            beside = (b_[0] >= a_[2] - 1) if not args.rtl else (b_[2] <= a_[0] + 1)
            if not (beside and h_ > 0 and min(a_[3], b_[3]) - max(a_[1], b_[1]) >= 0.5 * h_):
                kind = "bad"              # goes back up, unless it continues the same visual line
        if reg not in visited:
            visited.append(reg)
        items.append((n, b["tag"], bb))
        kinds.append(kind)
        prev_reg = reg
        if ls:
            prev_last = ls[-1]
    draw_reading_order(render(args.pdf, pi + 1, 144), regions, items, kinds, pm.disp_h, 144, str(target))


def untagged_order(args, pdf, R):
    """No structure tree: tag-blind tools (and most screen readers' fallbacks) read the paint order.
    Compare it with the layout and draw it, so the 'before' picture of an untagged file is meaningful."""
    layouts, seqs, bad = {}, [], []
    outp = Path(args.overlay) if args.overlay else None
    per_page = []
    for pi in range(len(pdf.pages)):
        pm = PageModel(pdf, pi + 1)
        lines = [ln for ln in pm.lines if ln.text.strip()]
        if not lines:
            continue
        lay = analyze([Box(ln.id, *ln.rbox, baseline=ln.rbaseline) for ln in lines], rtl=args.rtl)
        cmp = compare_order([ln.id for ln in lines], lay)
        lay.compare = cmp
        layouts[pi] = lay
        rmap = dict(cmp["path"])
        # blocks: consecutive lines that stay in one region and keep going down the page
        blocks, cur = [], None
        for ln in lines:
            if cur and rmap.get(ln.id) == rmap.get(cur[-1].id) and ln.rbaseline <= cur[-1].rbaseline + 2:
                cur.append(ln)
            else:
                cur = [ln]
                blocks.append(cur)
        per_page.append((pi, pm, lay, [{"tag": "text", "lines": b, "graphics": []} for b in blocks]))
        seqs.append(f"p{pi+1}: " + " → ".join(str(r) for r in cmp["region_sequence"]))
        if not cmp["ok"]:
            txt = {ln.id: ln.text.strip() for ln in lines}
            detail = [f"returns to {reg} at {txt.get(b_id, b_id)[:28]!r}" for b_id, reg in cmp["revisits"][:2]]
            detail += [f"jumps back from {ra} to {rb} ({txt.get(a, a)[:20]!r} → {txt.get(b, b)[:20]!r})"
                       for a, b, ra, rb in cmp["region_jumps"][:2]]
            detail += [f"goes up inside {reg} ({txt.get(a, a)[:20]!r} → {txt.get(b, b)[:20]!r})"
                       for a, b, reg in cmp["inversions"][:2]]
            same = cmp["region_sequence"] == cmp["expected_region_sequence"]
            bad.append(f"p{pi+1}: " + ("regions in layout order, but " if same else
                                       f"paint order {' → '.join(map(str, cmp['region_sequence']))} vs layout "
                                       f"{' → '.join(map(str, cmp['expected_region_sequence']))}; ") + "; ".join(detail))
    if bad:
        R.fail("Reading order vs layout", "no tags, and the paint order (what tag-blind tools read) departs from the "
               "layout: " + " | ".join(bad[:4]), "1.3.2", "PDF3", "7.2")
    elif seqs:
        R.warn("Reading order vs layout", "no tags; the paint order happens to follow the layout: " + " | ".join(seqs[:4]),
               "1.3.2", "PDF3", "7.2")
    if outp:
        for pi, pm, lay, blocks in per_page:
            target = outp if len(per_page) == 1 else outp.with_name(f"{outp.stem}-p{pi + 1}{outp.suffix}")
            page_overlay(args, pm, pi, lay, blocks, {}, target)
    return {pi: describe(l) for pi, l in layouts.items()}


def finish(R, args, reading=None, layouts=None):
    icon = {"PASS": "✔", "FAIL": "✘", "WARN": "!", "MANUAL": "?"}
    by_name = defaultdict(list)
    for it in R.items:
        by_name[it["check"]].append(it)
    print("Checklist")
    for name, sev, desc, subs in CHECKLIST:
        st = [i["status"] for s in subs for i in by_name.get(s, [])]
        status = "FAIL" if "FAIL" in st else ("WARN" if "WARN" in st else ("PASS" if st else "—"))
        print(f"  {icon.get(status, '·')} {name:<18} [{sev}]  {desc}")
    print("\nDetailed checks")
    for it in R.items:
        ref = " ".join(x for x in (f"WCAG {it['wcag']}" if it["wcag"] else "", it["technique"],
                                   f"UA {it['ua']}" if it["ua"] else "") if x)
        print(f"  {icon[it['status']]} {it['status']:6} {it['severity']:<8} {it['check']:<32} {it['detail']}" + (f"   [{ref}]" if ref else ""))
    fails = defaultdict(int)
    for it in R.items:
        if it["status"] == "FAIL":
            fails[it["severity"]] += 1
    print("\nSummary: " + (", ".join(f"{v} {k}" for k, v in fails.items()) + " failing" if fails else "no failing checks"))
    if layouts:
        for pi, lines in layouts.items():
            print(f"\nLayout, page {pi+1} (rows first, then columns, from whitespace):")
            print("\n".join("  " + x for x in lines))
    if reading:
        print("\nReading order (tag tree):")
        print("\n".join(reading))
    if args.report:
        Path(args.report).write_text(json.dumps(R.items, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(1 if any(i["status"] == "FAIL" for i in R.items) else 0)


if __name__ == "__main__":
    main()
