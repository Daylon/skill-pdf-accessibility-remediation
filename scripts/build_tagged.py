#!/usr/bin/env python3
"""
build_tagged.py — rebuild the logical structure of a PDF from a JSON plan,
without changing what is painted on the page.

Usage:
    python3 build_tagged.py input.pdf plan.json output.pdf

  1. Opens the PDF through prepare.load() (same unit ids as inspect/draft:
     text-bearing form XObjects inlined, existing tags indexed).
  2. Drops the old structure tree, marked content, ActualText and ParentTree.
  3. Re-segments every content stream: each planned text line / graphic gets
     its own marked-content sequence (BDC /Tag <</MCID n>>); everything else
     that paints becomes /Artifact.
  4. Optionally moves text lines to the end of the stream in reading order
     (options.reorder_text: "auto" | true | false). Only lines that cannot
     change the rendering are moved: invisible text, or text that is neither
     clipped nor painted over by a later shape.
  5. Builds StructTreeRoot / ParentTree / StructParents, OBJR for links, form
     fields and other annotations, list and table attributes, /Lang, /Alt,
     /E, /ActualText, bookmarks.
  6. Writes catalog /Lang, MarkInfo, DisplayDocTitle, Info and a clean XMP
     packet; claims PDF/UA-1 only when nothing known blocks it
     (options.pdfua: "auto" | true | false).

Plan format: references/plan-format.md
"""
from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator, String

sys.path.insert(0, str(Path(__file__).parent))
from content_model import PageModel, TEXT_SHOW  # noqa: E402
from layout import Box, analyze, compare_order, regions_with_columns  # noqa: E402
from prepare import load  # noqa: E402
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

STANDARD_TAGS = {
    "Document", "Part", "Art", "Sect", "Div", "BlockQuote", "Caption", "TOC", "TOCI", "Index",
    "NonStruct", "Private", "P", "H", "H1", "H2", "H3", "H4", "H5", "H6", "L", "LI", "Lbl", "LBody",
    "Table", "TR", "TH", "TD", "THead", "TBody", "TFoot", "Span", "Quote", "Note", "Reference",
    "BibEntry", "Code", "Link", "Annot", "Ruby", "RB", "RT", "RP", "Warichu", "WT", "WP",
    "Figure", "Formula", "Form",
}
TEXT_STATE_KEEP = {"Tf", "Tc", "Tw", "Tz", "TL", "Ts", "Tr"}
NUMBERINGS = {"None", "Disc", "Circle", "Square", "Decimal", "UpperRoman", "LowerRoman", "UpperAlpha", "LowerAlpha"}


# ============================================================================ plan model
@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)   # Node | ("line", id) | ("graphic", id)
    link: Optional[dict] = None                    # new link annotation {uri, contents}
    annot: Optional[str] = None                    # existing annotation id (pN.A#)
    annot_contents: Optional[str] = None
    form_label: Optional[str] = None
    region: Optional[str] = None
    table: Optional[dict] = None
    obj: Optional[pikepdf.Object] = None


RANGE = re.compile(r"^(p\d+)\.L(\d+)\.\.(?:L)?(\d+)$")


def expand(ids):
    out = []
    for s in ids:
        m = RANGE.match(s)
        if m:
            out += [f"{m.group(1)}.L{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        else:
            out.append(s)
    return out


def unit(s):
    return ("graphic", s) if re.match(r"^p\d+\.G\d+$", s) else ("line", s)


def parse_content(items):
    out = []
    for it in items:
        if isinstance(it, str):
            out += [unit(x) for x in expand([it])]
        elif isinstance(it, dict):
            out.append(parse_node(it))
    return out


def parse_node(spec) -> Node:
    spec = {k: v for k, v in spec.items() if not k.startswith("_")}
    tag = spec.get("tag")
    if tag is None and ("link" in spec or ("annot" in spec and "label" not in spec)):
        tag = "Link"
    if tag is None and "region" in spec:
        tag = "Div"
    if tag is None and "rows" in spec:
        tag = "Table"
    if tag is None:
        raise ValueError(f"node without tag: {spec}")
    n = Node(tag=tag)
    for k_plan, k_pdf in (("lang", "/Lang"), ("alt", "/Alt"), ("actual_text", "/ActualText"),
                          ("expansion", "/E"), ("title", "/T"), ("id", "/ID")):
        if spec.get(k_plan):
            n.attrs[k_pdf] = spec[k_plan]
    if "region" in spec:
        n.region = spec["region"]
        n.attrs["/T"] = spec.get("label", spec["region"])
    if tag == "L":
        num = spec.get("numbering", "None")
        if num not in NUMBERINGS:
            raise ValueError(f"unknown list numbering {num}")
        n.attrs["__numbering"] = num
    if "annot" in spec:
        n.annot = spec["annot"]
        n.annot_contents = spec.get("contents")
    if "link" in spec:
        n.link = spec["link"]
    if tag == "Form" and spec.get("label"):
        n.form_label = spec["label"]
    for k in ("scope", "rowspan", "colspan"):
        if k in spec and tag in ("TH", "TD"):
            n.attrs[f"__table_{k}"] = spec[k]
    # ---- table shorthand
    if tag == "Table" and "rows" in spec:
        for c in spec.get("children", []):          # captions first
            n.children.append(parse_node(c))
        hr, hc = int(spec.get("header_rows", 1)), int(spec.get("header_cols", 0))
        for r_i, row in enumerate(spec["rows"]):
            tr = Node(tag="TR")
            for c_i, cell in enumerate(row):
                is_h = r_i < hr or c_i < hc
                if isinstance(cell, dict):
                    cn = Node(tag="TH" if cell.get("header", is_h) else "TD")
                    for k in ("colspan", "rowspan"):
                        if k in cell:
                            cn.attrs[f"__table_{k}"] = cell[k]
                    cn.children += parse_content(cell.get("content", [])) + [unit(x) for x in expand(cell.get("lines", []))]
                else:
                    ids = cell if isinstance(cell, list) else [cell]
                    cn = Node(tag="TH" if is_h else "TD")
                    cn.children += [unit(x) for x in expand(ids)]
                if cn.tag == "TH":
                    cn.attrs["__table_scope"] = "Column" if r_i < hr else "Row"
                tr.children.append(cn)
            n.children.append(tr)
        return n
    # ---- list shorthand
    if "items" in spec:
        for it in spec["items"]:
            li = Node(tag="LI")
            if it.get("lang"):
                li.attrs["/Lang"] = it["lang"]
            if "label" in it:
                li.children.append(Node(tag="Lbl", children=[unit(x) for x in expand(it["label"])]))
            body = Node(tag="LBody")
            if "content" in it:
                body.children += parse_content(it["content"])
            if "link" in it or "annot" in it:
                link = Node(tag="Link", link=it.get("link"), annot=it.get("annot"),
                            annot_contents=it.get("contents"),
                            children=[unit(x) for x in expand(it.get("lines", []))])
                body.children.append(link)
            else:
                body.children += [unit(x) for x in expand(it.get("lines", []))]
            body.children += [parse_node(c) for c in it.get("children", [])]
            li.children.append(body)
            n.children.append(li)
    if "content" in spec:
        n.children += parse_content(spec["content"])
    n.children += [unit(x) for x in expand(spec.get("lines", []))]
    n.children += [("graphic", g) for g in spec.get("graphics", [])]
    n.children += [parse_node(c) for c in spec.get("children", [])]
    return n


def walk(node, fn, parent=None):
    fn(node, parent)
    for c in node.children:
        if isinstance(c, Node):
            walk(c, fn, node)


# ============================================================================ content streams
def color_ops(fill):
    space, comps = fill
    comps = [_num(float(c)) for c in comps]
    if space == "DeviceGray":
        return [([*comps], Operator("g"))]
    if space == "DeviceRGB":
        return [([*comps], Operator("rg"))]
    if space == "DeviceCMYK":
        return [([*comps], Operator("k"))]
    if space.startswith("/"):
        ops = [([Name(space)], Operator("cs"))]
        if comps:
            ops.append(([*comps], Operator("scn")))
        return ops
    return []


def _num(v):
    """A number written with full precision (pikepdf writes Python floats with 6 decimals)."""
    if isinstance(v, float):
        return int(v) if v.is_integer() else Decimal(repr(v))
    return v


def rewrite_page(pm: PageModel, line_seq: dict, graphic_seq: dict, movable: set, order: list):
    """
    line_seq:    line id -> (tag, mcid)       graphics/lines not listed = artifact
    movable:     line ids to move to the end of the stream (reading order), each replayed with the exact
                 CTM / clip / ExtGState / colour / text state it was painted with
    order:       planned line ids in logical order
    """
    op2line = {i: ln for ln in pm.lines for i in ln.op_indices}
    op2graphic = {i: g for g in pm.graphics for i in g.op_indices}
    moved = {i for ln in pm.lines if ln.id in movable for i in ln.op_indices}

    empty_bt = set()
    if moved:
        bt_start, shows_left = None, 0
        for i, ins in enumerate(pm.ops):
            o = str(ins.operator)
            if o == "BT":
                bt_start, shows_left = i, 0
            elif o in TEXT_SHOW and i not in moved:
                shows_left += 1
            elif o == "ET" and bt_start is not None:
                if shows_left == 0:
                    empty_bt.update(range(bt_start, i + 1))
                bt_start = None

    def want(i):
        if i in op2line:
            ln = op2line[i]
            return ("MC",) + line_seq[ln.id] if ln.id in line_seq else ("ART",)
        if i in op2graphic:
            g = op2graphic[i]
            return ("MC",) + graphic_seq[g.id] if g.id in graphic_seq else ("ART",)
        return None

    out = []
    state = {"open": None}
    depth = 0
    in_bt = False

    def close():
        out.append(([], Operator("EMC")))
        state["open"] = None

    def open_seq(seq):
        if seq[0] == "ART":
            out.append(([Name("/Artifact")], Operator("BMC")))
        else:
            out.append(([Name("/" + seq[1]), Dictionary(MCID=seq[2])], Operator("BDC")))
        state["open"] = (seq, depth, in_bt)

    for i, ins in enumerate(pm.ops):
        o = str(ins.operator)
        if i in pm.marked_ops or i in moved:
            continue
        if i in empty_bt:
            if o in TEXT_STATE_KEEP:
                out.append(ins)
            continue
        if o == "BT":
            if state["open"]:
                close()
            out.append(ins); in_bt = True
            continue
        if o == "ET":
            if state["open"]:
                close()
            out.append(ins); in_bt = False
            continue
        if o == "q":
            out.append(ins); depth += 1
            continue
        if o == "Q":
            if state["open"] and state["open"][1] >= depth:
                close()
            out.append(ins); depth = max(0, depth - 1)
            continue
        seq = want(i)
        if seq is not None and (state["open"] is None or state["open"][0] != seq):
            if state["open"]:
                close()
            open_seq(seq)
        out.append(ins)
    if state["open"]:
        close()

    if moved:
        out = [([], Operator("q"))] + out + [([], Operator("Q"))] * (depth + 1)
        by_id = {ln.id: ln for ln in pm.lines}
        for lid in order:
            if lid not in movable:
                continue
            ln = by_id[lid]
            tag, mcid = line_seq[lid]
            out.append(([Name("/" + tag), Dictionary(MCID=mcid)], Operator("BDC")))
            groups = []
            for s in ln.shows:
                if groups and groups[-1][0] == s.replay and groups[-1][1] == s.state["gsnames"]:
                    groups[-1][2].append(s)
                else:
                    groups.append((s.replay, s.state["gsnames"], [s]))
            for replay, gsn, shows in groups:
                out.append(([], Operator("q")))
                # replay the original cm and clip operators verbatim: a recomputed matrix rounds differently
                # and can move glyphs across a pixel boundary; the clip keeps cut-off text cut off
                for operands, operator in replay:
                    out.append((list(operands), Operator(operator)))
                for g in gsn:
                    out.append(([Name(g)], Operator("gs")))
                out += color_ops(shows[0].state["fill"])
                out.append(([], Operator("BT")))
                cur = {}
                for s in shows:
                    st = s.state
                    for k in ("Tr", "Tc", "Tw", "Tz", "Ts"):
                        default = 100.0 if k == "Tz" else 0
                        if cur.get(k, default) != st[k]:
                            out.append(([_num(st[k])], Operator(k)))
                            cur[k] = st[k]
                    if st["fill"] != shows[0].state["fill"]:
                        out += color_ops(st["fill"])
                    out.append(([Name(s.font), _num(s.size)], Operator("Tf")))
                    out.append(([_num(v) for v in s.tm], Operator("Tm")))
                    if s.operator in ("Tj", "TJ"):
                        out.append((s.operands, Operator(s.operator)))
                    else:
                        out.append(([s.operands[-1]], Operator("Tj")))
                out.append(([], Operator("ET")))
                out.append(([], Operator("Q")))
            out.append(([], Operator("EMC")))
    return pikepdf.unparse_content_stream(out)


# ============================================================================ fonts
def apply_unicode_overrides(pdf, pages, overrides, report):
    """overrides: {"pN:/F1": {"136": "•"}} → add/extend the font's ToUnicode CMap."""
    for key, mapping in (overrides or {}).items():
        m = re.match(r"^p(\d+):(/\S+)$", key)
        if not m:
            raise SystemExit(f"unicode_overrides key {key!r}: expected 'pN:/FontName'")
        pm = pages[int(m.group(1)) - 1]
        f = pm.fonts.get(m.group(2))
        if f is None:
            raise SystemExit(f"unicode_overrides: no font {m.group(2)} on page {m.group(1)}")
        cmap = dict(f.cmap)
        for code, ch in mapping.items():
            cmap[int(code)] = ch
            f.cmap[int(code)] = ch
        width = 4 if f.two_byte else 2
        lines = []
        items = sorted(cmap.items())
        for k in range(0, len(items), 100):
            chunk = items[k:k + 100]
            lines.append(f"{len(chunk)} beginbfchar")
            for c, u in chunk:
                lines.append(f"<{c:0{width}X}> <{u.encode('utf-16-be').hex().upper()}>")
            lines.append("endbfchar")
        rng = "<0000> <FFFF>" if f.two_byte else "<00> <FF>"
        cm = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
              "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
              "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
              f"1 begincodespacerange\n{rng}\nendcodespacerange\n" + "\n".join(lines) +
              "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
        f.obj.ToUnicode = pdf.make_stream(cm.encode("latin-1"))
        report.append(f"ToUnicode for {key}: {len(mapping)} override(s)")
    # line texts change with the overrides: recompute missing lists
    for pm in pages:
        for ln in pm.lines:
            for s in ln.shows:
                f = pm.fonts.get(s.font)
                if f is not None and s.missing_unicode:
                    s.missing_unicode = [c for c in s.missing_unicode if f.to_unicode(c) is None]


# ============================================================================ metadata
def _x(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


CARRY = {  # existing XMP properties worth keeping: qualified name -> (prefix, ns)
    "{http://www.aiim.org/pdfa/ns/id/}part": ("pdfaid:part", "http://www.aiim.org/pdfa/ns/id/"),
    "{http://www.aiim.org/pdfa/ns/id/}conformance": ("pdfaid:conformance", "http://www.aiim.org/pdfa/ns/id/"),
    "{http://ns.adobe.com/xap/1.0/}CreateDate": ("xmp:CreateDate", "http://ns.adobe.com/xap/1.0/"),
    "{http://ns.adobe.com/xap/1.0/mm/}DocumentID": ("xmpMM:DocumentID", "http://ns.adobe.com/xap/1.0/mm/"),
    "{http://ns.adobe.com/xap/1.0/rights/}Marked": ("xmpRights:Marked", "http://ns.adobe.com/xap/1.0/rights/"),
    "{http://ns.adobe.com/xap/1.0/rights/}WebStatement": ("xmpRights:WebStatement", "http://ns.adobe.com/xap/1.0/rights/"),
}


def build_xmp(meta, lang, keywords, kw_str, producer, creator_tool, now, pdfua, pdf_version, carried):
    iso = now.isoformat()
    ns = {"dc": "http://purl.org/dc/elements/1.1/", "pdf": "http://ns.adobe.com/pdf/1.3/",
          "xmp": "http://ns.adobe.com/xap/1.0/", "pdfuaid": "http://www.aiim.org/pdfua/ns/id/"}
    for qn, (_v, uri) in carried.items():
        ns.setdefault(qn.split(":")[0], uri)
    parts = ['<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>', '<x:xmpmeta xmlns:x="adobe:ns:meta/">',
             ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
             '  <rdf:Description rdf:about=""'] + [f'    xmlns:{p}="{u}"' for p, u in ns.items()]
    parts[-1] += ">"
    parts.append(f'   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{_x(meta["title"])}</rdf:li></rdf:Alt></dc:title>')
    if meta.get("author"):
        parts.append(f'   <dc:creator><rdf:Seq><rdf:li>{_x(meta["author"])}</rdf:li></rdf:Seq></dc:creator>')
    if meta.get("subject"):
        parts.append(f'   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{_x(meta["subject"])}</rdf:li></rdf:Alt></dc:description>')
    if keywords:
        parts.append("   <dc:subject><rdf:Bag>" + "".join(f"<rdf:li>{_x(k)}</rdf:li>" for k in keywords) + "</rdf:Bag></dc:subject>")
        parts.append(f"   <pdf:Keywords>{_x(kw_str)}</pdf:Keywords>")
    parts += [f"   <dc:language><rdf:Bag><rdf:li>{_x(lang)}</rdf:li></rdf:Bag></dc:language>",
              "   <dc:format>application/pdf</dc:format>",
              f"   <pdf:Producer>{_x(producer)}</pdf:Producer>",
              f"   <pdf:PDFVersion>{_x(pdf_version)}</pdf:PDFVersion>",
              f"   <xmp:CreatorTool>{_x(creator_tool)}</xmp:CreatorTool>",
              f"   <xmp:ModifyDate>{iso}</xmp:ModifyDate>",
              f"   <xmp:MetadataDate>{iso}</xmp:MetadataDate>"]
    for qn, (v, _uri) in carried.items():
        parts.append(f"   <{qn}>{_x(v)}</{qn}>")
    if pdfua:
        parts.append("   <pdfuaid:part>1</pdfuaid:part>")
    parts += ["  </rdf:Description>", " </rdf:RDF>", "</x:xmpmeta>", "\n".join([" " * 100] * 20), '<?xpacket end="w"?>']
    return "\n".join(parts).encode("utf-8")


# ============================================================================ build
def build(src, plan_path, dst):
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    meta = {k: v for k, v in plan.get("metadata", {}).items() if v not in (None, "", [])}
    opts = plan.get("options", {})
    errors, warnings, report = [], [], []

    pdf, page_list, _tags, prep = load(src)
    report += prep
    pages = {f"p{pm.page_no}": pm for pm in page_list}
    apply_unicode_overrides(pdf, page_list, plan.get("unicode_overrides"), report)
    all_lines = {ln.id: (pfx, ln) for pfx, pm in pages.items() for ln in pm.lines}
    all_graphics = {g.id: (pfx, g) for pfx, pm in pages.items() for g in pm.graphics}
    all_annots = {a.id: (pfx, a) for pfx, pm in pages.items() for a in pm.annots}

    # ---- metadata checks
    title = meta.get("title", "")
    if not title or title.startswith("TODO"):
        errors.append("metadata.title is missing or TODO (WCAG 2.4.2 / PDF18)")
    alltext = " ".join(ln.text for pm in page_list for ln in pm.lines)
    inferred, conf = lang_detect.primary(alltext) if alltext.strip() else (None, 0.0)
    lang = meta.get("lang")
    if not lang:
        if not inferred or conf < 0.6:
            errors.append("metadata.lang missing and the language cannot be inferred (WCAG 3.1.1)")
        else:
            lang = lang_detect.with_variant(inferred)
            warnings.append(f"metadata.lang not set: using inferred {lang} ({conf:.2f})")
    elif inferred and conf >= 0.6 and lang.split("-")[0].lower() != inferred:
        warnings.append(f"metadata.lang={lang} but the text reads as '{inferred}' ({conf:.2f})")

    # ---- parse plan
    roots = [parse_node(s) for s in plan["structure"]]
    root = roots[0] if len(roots) == 1 and roots[0].tag == "Document" else Node(tag="Document", children=roots)

    used, used_annots = {}, {}
    next_mcid = {p: 0 for p in pages}
    line_seq = {p: {} for p in pages}
    graphic_seq = {p: {} for p in pages}
    order = {p: [] for p in pages}
    mcid_owner = {p: {} for p in pages}
    heading_levels = []

    def check(node):
        if node.tag not in STANDARD_TAGS:
            errors.append(f"non-standard tag {node.tag}")
        if re.fullmatch(r"H[1-6]", node.tag):
            heading_levels.append(int(node.tag[1]))
        for key in ("/Alt", "/ActualText"):
            if str(node.attrs.get(key, "")).startswith("TODO"):
                errors.append(f"{node.tag} still has a TODO {key[1:].lower()}: {node.attrs[key][:60]!r}")
        if node.tag in ("Figure", "Formula") and "/Alt" not in node.attrs and "/ActualText" not in node.attrs:
            errors.append(f"{node.tag} without alt (WCAG 1.1.1 / PDF1)")
        if node.form_label and node.form_label.startswith("TODO"):
            errors.append(f"form field {node.annot} still has a TODO label")
        if node.annot:
            if node.annot not in all_annots:
                errors.append(f"unknown annotation {node.annot}")
            elif node.annot in used_annots:
                errors.append(f"annotation {node.annot} placed twice")
            used_annots[node.annot] = node.tag

    def assign(node, c):
        kind, cid = c
        table = all_lines if kind == "line" else all_graphics
        if cid not in table:
            errors.append(f"unknown {kind} id {cid}")
            return
        if cid in used:
            errors.append(f"{cid} used twice ({used[cid]} and {node.tag})")
        used[cid] = node.tag
        pfx, u = table[cid]
        mcid = next_mcid[pfx]; next_mcid[pfx] += 1
        mcid_owner[pfx][mcid] = node
        if kind == "line":
            line_seq[pfx][cid] = (node.tag, mcid)
            order[pfx].append(cid)
            if u.occluded:
                warnings.append(f"{cid} is hidden under an opaque shape but tagged as content")
        else:
            graphic_seq[pfx][cid] = (node.tag, mcid)

    def traverse(node):
        """document order: MCIDs follow the order of /K arrays exactly"""
        check(node)
        for c in node.children:
            if isinstance(c, Node):
                traverse(c)
            else:
                assign(node, c)
    traverse(root)

    artifact_lines = set(expand(plan.get("artifacts", {}).get("lines", [])))
    for lid, (pfx, ln) in all_lines.items():
        if lid not in used and lid not in artifact_lines and ln.text.strip():
            errors.append(f"text line {lid} {ln.text.strip()[:50]!r} is neither tagged nor declared an artifact")
    for aid, (pfx, a) in all_annots.items():
        if aid not in used_annots and a.subtype != "Popup" and not a.hidden:
            errors.append(f"annotation {aid} ({a.subtype}) is not placed in the plan (Link/Form/Annot node with \"annot\")")
    prev = 0
    for lvl in heading_levels:
        if prev == 0 and lvl != 1:
            warnings.append(f"first heading is H{lvl}, expected H1")
        if prev and lvl > prev + 1:
            errors.append(f"heading level skipped: H{prev} -> H{lvl} (WCAG 1.3.1 / PDF9)")
        prev = lvl

    # ---- plan vs layout (advice only). Same units as the validator: a table, figure or formula is one
    # block per page, so the order of its cells does not count as a departure from the layout.
    group_of = {}

    def collect_groups(node, parent):
        if node.tag in ("Table", "Figure", "Formula"):
            ids = []
            walk(node, lambda n, _p: ids.extend(c[1] for c in n.children if not isinstance(c, Node) and c[0] == "line"))
            for lid in ids:
                group_of.setdefault(lid, (node, [x for x in ids if all_lines[x][0] == all_lines[lid][0]]))
    walk(root, collect_groups)
    for pfx, pm in pages.items():
        by_id = {ln.id: ln for ln in pm.lines}
        seq, boxes, seen = [], [], set()
        for lid in order[pfx]:
            ln = by_id[lid]
            if not ln.text.strip():
                continue
            grp = [by_id[x] for x in group_of[lid][1]] if lid in group_of else [ln]
            grp = [x for x in grp if x.text.strip()] or grp
            rep = grp[0].id
            if rep in seen:
                continue
            seen.add(rep)
            seq.append(rep)
            boxes.append(Box(rep, min(x.rbox[0] for x in grp), min(x.rbox[1] for x in grp),
                             max(x.rbox[2] for x in grp), max(x.rbox[3] for x in grp),
                             baseline=max(x.rbaseline for x in grp), n=len({round(x.rbaseline) for x in grp}),
                             lh=sorted(x.rbox[3] - x.rbox[1] for x in grp)[len(grp) // 2]))
        if len(boxes) < 2:
            continue
        cmp = compare_order(seq, analyze(boxes, rtl=bool(opts.get("rtl", False))))
        if not cmp["ok"]:
            warnings.append(f"{pfx}: plan order departs from the layout (regions {cmp['region_sequence']} vs "
                            f"{cmp['expected_region_sequence']}) — fine for tables/sidebars you ordered on purpose")
    if errors:
        print("PLAN ERRORS:\n  " + "\n  ".join(errors))
        raise SystemExit(1)

    # ---- PDF/UA blockers
    blockers = []
    for pm in page_list:
        for name, f in pm.fonts.items():
            uses = [s for ln in pm.lines for s in ln.shows if s.font == name]
            if not uses:
                continue
            if not f.embedded and not all(s.state["Tr"] in (3, 7) for s in uses):
                blockers.append(f"font {f.basefont or name} not embedded (7.21.4)")
            if not f.supported:
                blockers.append(f"font {f.basefont or name}: unsupported CID encoding")
        for ln in pm.lines:
            if ln.id in line_seq[f"p{pm.page_no}"] and any(s.missing_unicode for s in ln.shows):
                blockers.append(f"{ln.id}: glyphs without Unicode (7.21.7)")
    blockers = sorted(set(blockers))
    want_ua = opts.get("pdfua", "auto")
    claim = (want_ua is True) or (want_ua == "auto" and not blockers)
    if blockers and want_ua is not False:
        warnings.append(("PDF/UA-1 not claimed: " if not claim else "PDF/UA-1 claimed despite: ") + "; ".join(blockers[:6]))

    # ---- wipe old structure
    for key in ("/StructTreeRoot", "/Info", "/MarkInfo"):
        if key in pdf.Root:
            del pdf.Root[key]
    for pm in page_list:
        if "/StructParents" in pm.page.obj:
            del pm.page.obj["/StructParents"]
        for a in pm.page.obj.get("/Annots", []) or []:
            if isinstance(a, pikepdf.Dictionary) and "/StructParent" in a:
                del a["/StructParent"]

    # ---- rewrite content streams
    reorder = opts.get("reorder_text", "auto")
    moved_total, kept = 0, []
    page_movable = {}
    for i, (pfx, pm) in enumerate(pages.items()):
        movable = set()
        if reorder in (True, "auto"):
            for ln in pm.lines:
                if ln.id not in line_seq[pfx]:
                    continue
                # text that adds to the clip (Tr 4-7) or is stroked (needs line width, stroke colour…) stays put
                special = any(s_.state["Tr"] in (1, 2, 4, 5, 6, 7) for s_ in ln.shows)
                if not special and (not ln.visible or not ln.painted_over):
                    movable.add(ln.id)       # clipped text moves too: its clip is replayed with it
                else:
                    kept.append(ln.id)
        moved_total += len(movable)
        page_movable[pfx] = movable
        data = rewrite_page(pm, line_seq[pfx], graphic_seq[pfx], movable, order[pfx])
        pm.page.obj.Contents = pdf.make_stream(data)
        pm.page.obj.StructParents = i
        pm.page.obj.Tabs = Name("/S")
    if kept and reorder in (True, "auto"):
        report.append(f"{len(kept)} line(s) left in place (painted over by a later shape, stroked or clipping text): {kept[:6]}")

    # ---- structure tree
    page_objs = {pfx: pm.page.obj for pfx, pm in pages.items()}
    str_root = pdf.make_indirect(Dictionary(Type=Name("/StructTreeRoot")))
    parent_tree = []
    annot_key = [len(pages)]
    counts = {"links": 0, "annots": 0}

    def page_of(child):
        kind, cid = child
        return (all_lines if kind == "line" else all_graphics)[cid][0]

    def attach_annot(node, obj, a_pfx, annot):
        annot.StructParent = annot_key[0]
        parent_tree.append((annot_key[0], obj))
        annot_key[0] += 1
        return Dictionary(Type=Name("/OBJR"), Obj=annot, Pg=page_objs[a_pfx])

    def make(node: Node, parent_obj):
        d = Dictionary(Type=Name("/StructElem"), S=Name("/" + node.tag), P=parent_obj)
        for k, v in node.attrs.items():
            if not k.startswith("__"):
                d[k] = String(str(v))
        if node.tag == "L":
            d.A = Dictionary(O=Name("/List"), ListNumbering=Name("/" + node.attrs["__numbering"]))
        tbl = {k[8:]: v for k, v in node.attrs.items() if k.startswith("__table_")}
        if tbl:
            a = Dictionary(O=Name("/Table"))
            if "scope" in tbl:
                a.Scope = Name("/" + str(tbl["scope"]).capitalize())
            if "rowspan" in tbl:
                a.RowSpan = int(tbl["rowspan"])
            if "colspan" in tbl:
                a.ColSpan = int(tbl["colspan"])
            d.A = a
        obj = pdf.make_indirect(d)
        node.obj = obj
        kids = Array()
        content = [c for c in node.children if not isinstance(c, Node)]
        pg = page_of(content[0]) if content else None
        if pg:
            obj.Pg = page_objs[pg]
        for c in node.children:
            if isinstance(c, Node):
                kids.append(make(c, obj))
            else:
                pfx = page_of(c)
                kind, cid = c
                mcid = (line_seq[pfx].get(cid) or graphic_seq[pfx].get(cid))[1]
                kids.append(mcid if pfx == pg else Dictionary(Type=Name("/MCR"), Pg=page_objs[pfx], MCID=mcid))
        if node.annot:
            a_pfx, a = all_annots[node.annot]
            ann = a.obj
            if node.tag == "Link":
                text = node.annot_contents or a.contents or " ".join(
                    all_lines[c[1]][1].text.strip() for c in content if c[0] == "line") or (a.uri or "link")
                ann.Contents = String(text)
                counts["links"] += 1
            elif node.tag == "Form":
                if node.form_label:
                    ann.TU = String(node.form_label)
                counts["annots"] += 1
            else:
                if node.annot_contents:
                    ann.Contents = String(node.annot_contents)
                counts["annots"] += 1
            if not pg:
                obj.Pg = page_objs[a_pfx]
            kids.append(attach_annot(node, obj, a_pfx, ann))
        elif node.tag == "Link" and node.link:
            boxes = [all_lines[c[1]][1].bbox for c in content if c[0] == "line"]
            pfx = pg
            x0 = min(b[0] for b in boxes) - 1; y0 = min(b[1] for b in boxes) - 1
            x1 = max(b[2] for b in boxes) + 1; y1 = max(b[3] for b in boxes) + 1
            ann = pdf.make_indirect(Dictionary(
                Type=Name("/Annot"), Subtype=Name("/Link"), Rect=[x0, y0, x1, y1], Border=[0, 0, 0], F=4,
                Contents=String(node.link.get("contents") or node.link["uri"]),
                A=Dictionary(Type=Name("/Action"), S=Name("/URI"), URI=String(node.link["uri"])), P=page_objs[pfx]))
            if "/Annots" not in page_objs[pfx]:
                page_objs[pfx].Annots = Array()
            page_objs[pfx].Annots.append(ann)
            kids.append(attach_annot(node, obj, pfx, ann))
            counts["links"] += 1
        obj.K = kids
        return obj

    str_root.K = Array([make(root, str_root)])
    for i, pfx in enumerate(pages):
        parent_tree.append((i, pdf.make_indirect(Array([mcid_owner[pfx][m].obj for m in range(next_mcid[pfx])]))))
    parent_tree.sort(key=lambda t: t[0])
    nums = Array()
    for k, v in parent_tree:
        nums.append(k); nums.append(v)
    str_root.ParentTree = pdf.make_indirect(Dictionary(Nums=nums))
    str_root.ParentTreeNextKey = annot_key[0]
    pdf.Root.StructTreeRoot = str_root

    # ---- bookmarks: keep the producer's, generate from headings for long documents
    bm = opts.get("bookmarks", "auto")
    has_outlines = pdf.Root.get("/Outlines") is not None and pdf.Root.Outlines.get("/First") is not None
    if bm == "always" or bm == "replace" or (bm == "auto" and not has_outlines and len(pages) >= 20):
        heads = []

        def collect(n, par):
            if re.fullmatch(r"H[1-6]", n.tag):
                ls = [c[1] for c in n.children if not isinstance(c, Node) and c[0] == "line"]
                if ls:
                    pfx, ln = all_lines[ls[0]]
                    heads.append((int(n.tag[1]), n.attrs.get("/ActualText") or
                                  " ".join(" ".join(all_lines[x][1].text.split()) for x in ls), pfx, ln.bbox[3]))
        walk(root, collect)
        if heads:
            outlines = pdf.make_indirect(Dictionary(Type=Name("/Outlines")))
            stack = [(0, outlines)]
            for lvl, t, pfx, top in heads:
                while stack and stack[-1][0] >= lvl:
                    stack.pop()
                parent = stack[-1][1]
                item = pdf.make_indirect(Dictionary(Title=String(t), Parent=parent,
                                                    Dest=Array([page_objs[pfx], Name("/XYZ"), 0, top + 4, 0])))
                if "/First" not in parent:
                    parent.First = item
                else:
                    parent.Last.Next = item
                    item.Prev = parent.Last
                parent.Last = item
                stack.append((lvl, item))

            def count(node):
                n, c = 0, node.get("/First")
                while c is not None:
                    n += 1 + count(c)
                    c = c.get("/Next")
                if node is not outlines and n:
                    node.Count = n
                return n
            outlines.Count = count(outlines)
            pdf.Root.Outlines = outlines
            pdf.Root.PageMode = Name("/UseOutlines")
            report.append(f"bookmarks generated from {len(heads)} headings")

    # ---- catalog + metadata
    pdf.Root.Lang = String(lang)
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    vp = pdf.Root.get("/ViewerPreferences", Dictionary())
    vp.DisplayDocTitle = True
    pdf.Root.ViewerPreferences = vp
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    creator_tool = str(pdf.docinfo.get("/Creator", "")) or opts.get("creator_tool") or "unknown"
    old_producer = str(pdf.docinfo.get("/Producer", "")) if pdf.docinfo.get("/Producer") is not None else ""
    producer = opts.get("producer") or ((old_producer + "; " if old_producer else "") + "tagged with skill-pdf-accessibility-remediation (pikepdf)")
    kw = meta.get("keywords", [])
    kw = kw if isinstance(kw, list) else [kw]
    kw_str = ", ".join(kw)
    carried = {}
    try:
        with pdf.open_metadata() as m:
            for k, (qn, _uri) in CARRY.items():
                v = m.get(k)
                if isinstance(v, str) and v:
                    carried[qn] = (v, _uri)
    except Exception:  # noqa: BLE001
        pass
    if "pdfaid:part" in carried:
        warnings.append(f"source claims PDF/A-{carried['pdfaid:part'][0]}: claim kept — re-validate PDF/A with veraPDF")
    pdf.Root.Metadata = pdf.make_stream(
        build_xmp(meta | {"title": title}, lang, kw, kw_str, producer, creator_tool, now, claim, pdf.pdf_version,
                  {k: v for k, v in carried.items()}),
        Type=Name("/Metadata"), Subtype=Name("/XML"))
    info = pdf.docinfo
    info.Title = String(title)
    for k_plan, k_pdf in (("author", "/Author"), ("subject", "/Subject")):
        if meta.get(k_plan):
            info[k_pdf] = String(meta[k_plan])
    if kw_str:
        info.Keywords = String(kw_str)
    info.Creator = String(creator_tool)
    info.Producer = String(producer)
    info.ModDate = String(now.strftime("D:%Y%m%d%H%M%SZ"))

    pdf.save(dst, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)

    # ---- safety net: the rendering must not change. Pages that differ are rebuilt without reordering.
    if opts.get("verify", True):
        from raster import render, pixel_diff
        changed = []
        for k, (pfx, pm) in enumerate(pages.items(), 1):
            d = pixel_diff(render(src, k, 72), render(dst, k, 72))
            if not d.get("same_size") or d["pixels_changed"]:
                changed.append(pfx)
        retry = [p_ for p_ in changed if page_movable.get(p_)]
        if retry:
            for pfx in retry:
                pm = pages[pfx]
                pm.page.obj.Contents = pdf.make_stream(rewrite_page(pm, line_seq[pfx], graphic_seq[pfx], set(), order[pfx]))
                moved_total -= len(page_movable[pfx])
            pdf.save(dst, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
            report.append(f"reordering reverted on {retry}: it changed pixels there")
            changed = []
            for k, (pfx, pm) in enumerate(pages.items(), 1):
                d = pixel_diff(render(src, k, 72), render(dst, k, 72))
                if not d.get("same_size") or d["pixels_changed"]:
                    changed.append(pfx)
        if changed:
            warnings.append(f"RENDERING DIFFERS on {changed} even without reordering — inspect before delivering "
                            f"(form inlining? set options.verify=false only to debug)")
    for w in warnings:
        print("warning:", w)
    for r in report:
        print("note:", r)
    print(f"wrote {dst}: {sum(next_mcid.values())} tagged units, {counts['links']} links, {counts['annots']} other "
          f"annotations, {moved_total} lines moved into reading order, lang={lang}, "
          f"PDF/UA claim={'yes' if claim else 'no'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("plan"); ap.add_argument("dst")
    a = ap.parse_args()
    build(a.src, a.plan, a.dst)


if __name__ == "__main__":
    main()
