"""
prepare.py — the one entry point every script uses to open a PDF, so unit ids
(p1.L12, p2.G3, p1.A1) are identical in inspect, draft, build and validate.

Steps
  1. Inline Form XObjects that contain text into the page content stream
     (q · Matrix cm · BBox clip · renamed operators · Q), recursively, so their
     text can be tagged line by line. Skipped — and reported — when inlining
     could change rendering or semantics: transparency groups (/Group),
     optional content (/OC), forms that already carry structure
     (/StructParents), reference XObjects (/Ref), inline images using named
     colour spaces.
  2. Index the source's existing structure tree (role / alt / lang per MCID)
     so the draft plan can reuse decent existing tagging as hints.
  3. Build a PageModel per page.
"""
from __future__ import annotations

import pikepdf
from pikepdf import Name, Operator

from content_model import PageModel, TEXT_SHOW, existing_tag_index

RES_CATS = {"Tf": "/Font", "Do": "/XObject", "gs": "/ExtGState", "sh": "/Shading"}
DEVICE_CS = {"/DeviceRGB", "/DeviceGray", "/DeviceCMYK", "/Pattern"}


def page_resources(page_obj):
    node = page_obj
    while node is not None:
        r = node.get("/Resources")
        if r is not None:
            if node is not page_obj:          # inherited: give the page its own copy we can extend
                page_obj.Resources = pikepdf.Dictionary({k: r[k] for k in r.keys()})
                return page_obj.Resources
            return r
        node = node.get("/Parent")
    page_obj.Resources = pikepdf.Dictionary()
    return page_obj.Resources


def _form_has_text(xo, depth=0):
    if depth > 8:
        return False
    try:
        ops = pikepdf.parse_content_stream(xo)
    except Exception:  # noqa: BLE001
        return False
    res = xo.get("/Resources", pikepdf.Dictionary())
    for ins in ops:
        o = str(ins.operator)
        if o in TEXT_SHOW:
            return True
        if o == "Do":
            sub = res.get("/XObject", {}).get(ins.operands[0])
            if sub is not None and sub.get("/Subtype") == "/Form" and _form_has_text(sub, depth + 1):
                return True
    return False


def _inlinable(xo):
    if any(k in xo for k in ("/Group", "/OC", "/StructParents", "/StructParent", "/Ref")):
        return False
    return True


class _Renamer:
    def __init__(self, pdf, page_res, form_res, tag):
        self.pdf, self.page_res, self.form_res, self.tag = pdf, page_res, form_res, tag
        self.map = {}

    def name(self, cat, nm):
        key = (cat, str(nm))
        if key in self.map:
            return self.map[key]
        src = self.form_res.get(cat, {}).get(nm) if self.form_res is not None else None
        if src is None:                    # resource inherited from the page: keep the name
            self.map[key] = nm
            return nm
        dst_cat = self.page_res.get(cat)
        if dst_cat is None:
            self.page_res[cat] = pikepdf.Dictionary()
            dst_cat = self.page_res[cat]
        existing = dst_cat.get(nm)
        if existing is not None and existing.is_indirect and src.is_indirect and existing.objgen == src.objgen:
            self.map[key] = nm
            return nm
        new = Name(f"{str(nm)}_{self.tag}")
        dst_cat[new] = src
        self.map[key] = new
        return new


def _inline_ops(pdf, ops, page_res, res, counter, report, where, depth=0):
    """Return a new operator list where text-bearing forms used by `ops` are expanded."""
    out = []
    for ins in ops:
        o = str(ins.operator)
        if o == "Do" and depth < 8:
            nm = ins.operands[0]
            xo = res.get("/XObject", {}).get(nm) if res is not None else None
            if xo is not None and xo.get("/Subtype") == "/Form" and _form_has_text(xo):
                if not _inlinable(xo):
                    report.append(f"{where}: form {nm} holds text but was NOT inlined "
                                  f"({', '.join(k for k in ('/Group', '/OC', '/StructParents', '/Ref') if k in xo)}) "
                                  f"— tag it as a whole graphic with actual_text")
                    out.append(ins)
                    continue
                counter[0] += 1
                tag = f"fx{counter[0]}"
                fres = xo.get("/Resources")
                rn = _Renamer(pdf, page_res, fres if fres is not None else pikepdf.Dictionary(), tag)
                inner = pikepdf.parse_content_stream(xo)
                if any(str(i.operator) == "INLINE IMAGE" for i in inner):
                    report.append(f"{where}: form {nm} holds an inline image — not inlined")
                    out.append(ins)
                    continue
                renamed = []
                for i2 in inner:
                    op2 = str(i2.operator)
                    args = list(i2.operands)
                    if op2 in RES_CATS and args and isinstance(args[0], Name):
                        args[0] = rn.name(RES_CATS[op2], args[0])
                    elif op2 in ("cs", "CS") and args and str(args[0]) not in DEVICE_CS:
                        args[0] = rn.name("/ColorSpace", args[0])
                    elif op2 in ("scn", "SCN") and args and isinstance(args[-1], Name):
                        args[-1] = rn.name("/Pattern", args[-1])
                    elif op2 in ("BDC", "DP") and len(args) > 1 and isinstance(args[1], Name):
                        args[1] = rn.name("/Properties", args[1])
                    renamed.append(pikepdf.ContentStreamInstruction(args, i2.operator))
                # the renamed ops now resolve against page_res: recurse for nested forms
                nested = _inline_ops(pdf, renamed, page_res, page_res, counter, report, where, depth + 1)
                m = [float(x) for x in xo.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                bb = [float(x) for x in xo.get("/BBox", [0, 0, 0, 0])]
                out.append(pikepdf.ContentStreamInstruction([], Operator("q")))
                out.append(pikepdf.ContentStreamInstruction(m, Operator("cm")))
                out.append(pikepdf.ContentStreamInstruction([bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]], Operator("re")))
                out.append(pikepdf.ContentStreamInstruction([], Operator("W")))
                out.append(pikepdf.ContentStreamInstruction([], Operator("n")))
                out += nested
                out.append(pikepdf.ContentStreamInstruction([], Operator("Q")))
                report.append(f"{where}: inlined text-bearing form {nm} as {tag}")
                continue
        out.append(ins)
    return out


def inline_text_forms(pdf):
    report = []
    counter = [0]
    for pno, page in enumerate(pdf.pages, 1):
        res = page_resources(page.obj)
        xobjs = res.get("/XObject")
        if xobjs is None or not any(xobjs[k].get("/Subtype") == "/Form" and _form_has_text(xobjs[k]) for k in xobjs.keys()):
            continue
        ops = pikepdf.parse_content_stream(page)
        before = counter[0]
        new = _inline_ops(pdf, ops, res, res, counter, report, f"p{pno}")
        if counter[0] != before:
            page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new))
    return report


def load(path_or_pdf, inline_forms=True):
    """Open + prepare. Returns (pdf, [PageModel], tag_index, report)."""
    pdf = pikepdf.open(path_or_pdf) if not isinstance(path_or_pdf, pikepdf.Pdf) else path_or_pdf
    tag_index = existing_tag_index(pdf)
    report = inline_text_forms(pdf) if inline_forms else []
    pages = [PageModel(pdf, i, tag_index) for i in range(1, len(pdf.pages) + 1)]
    return pdf, pages, tag_index, report
