"""
content_model.py — walk a PDF page's content stream and expose the "units"
a structure plan refers to:

  * text lines  (pN.L#): consecutive text-showing operators inside one BT/ET
                         that share a baseline
  * graphics    (pN.G#): one painted path / image / shading / form XObject
  * annotations (pN.A#): links, form fields, comments …

Every unit keeps its operator indices, decoded Unicode text, bounding box in
user space (`bbox`) and in display space (`dbox`: CropBox origin, /Rotate
applied, y up), effective font size, fill colour, visibility, and — when the
source already had tags — the role it had there (`hint`).

Only depends on pikepdf (+ pdfminer.six tables for fonts without ToUnicode).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

import pikepdf

try:  # glyph-name / encoding tables and standard-14 metrics
    from pdfminer.encodingdb import EncodingDB, name2unicode
    from pdfminer.fontmetrics import FONT_METRICS
except Exception:  # noqa: BLE001
    EncodingDB = None
    FONT_METRICS = {}

    def name2unicode(n):  # type: ignore
        raise KeyError(n)

# --------------------------------------------------------------------------
# operator families
# --------------------------------------------------------------------------
PATH_CONSTRUCT = {"m", "l", "c", "v", "y", "h", "re"}
PATH_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
CLIP = {"W", "W*"}
TEXT_SHOW = {"Tj", "TJ", "'", '"'}
TEXT_STATE = {"Tc", "Tw", "Tz", "TL", "Tf", "Tr", "Ts"}
TEXT_POS = {"Td", "TD", "Tm", "T*"}
MARKED = {"BDC", "BMC", "EMC", "MP", "DP"}
XOBJ_OR_SHADING = {"Do", "sh", "INLINE IMAGE", "BI"}

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
STD14_ALIASES = {"Arial": "Helvetica", "Arial,Bold": "Helvetica-Bold", "Arial,Italic": "Helvetica-Oblique",
                 "Arial,BoldItalic": "Helvetica-BoldOblique", "TimesNewRoman": "Times-Roman",
                 "TimesNewRoman,Bold": "Times-Bold", "TimesNewRoman,Italic": "Times-Italic",
                 "TimesNewRoman,BoldItalic": "Times-BoldItalic", "CourierNew": "Courier"}


def mat_mul(a, b):
    """a × b for PDF 3x2 matrices given as 6-tuples."""
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def scale_of(m):
    sx = math.hypot(m[0], m[1])
    sy = math.hypot(m[2], m[3])
    return math.sqrt(abs(sx * sy)) if sx and sy else max(sx, sy)


def bbox_of_points(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------
class FontInfo:
    def __init__(self, name: str, font: pikepdf.Dictionary):
        self.name = name
        self.obj = font
        self.subtype = str(font.get("/Subtype", ""))
        self.basefont = str(font.get("/BaseFont", ""))[1:] if font.get("/BaseFont") is not None else ""
        self.two_byte = self.subtype == "/Type0"
        self.cmap = self._tounicode(font)
        self.first = int(font.get("/FirstChar", 0))
        self.widths = [float(w) for w in font.get("/Widths", [])]
        fm = font.get("/FontMatrix")
        self.font_matrix = tuple(float(x) for x in fm) if fm is not None else (0.001, 0, 0, 0.001, 0, 0)
        fd = font.get("/FontDescriptor")
        self.supported = True
        if self.two_byte:
            desc = font.DescendantFonts[0]
            fd = desc.get("/FontDescriptor")
            self.dw = float(desc.get("/DW", 1000))
            self.cid_widths = self._cid_widths(desc.get("/W"))
            enc = font.get("/Encoding")
            self.supported = isinstance(enc, pikepdf.Name) and str(enc) in ("/Identity-H", "/Identity-V")
        self.fd = fd
        std = self.basefont.split("+")[-1]
        std = STD14_ALIASES.get(std, std)
        self.std14 = std if std in FONT_METRICS else None
        asc = float(fd.get("/Ascent", 0)) if fd is not None else 0
        dsc = float(fd.get("/Descent", 0)) if fd is not None else 0
        if not asc and self.std14:
            asc, dsc = FONT_METRICS[self.std14][0].get("Ascent", 718), FONT_METRICS[self.std14][0].get("Descent", -207)
        self.ascent = asc / 1000 if asc else 0.8
        self.descent = dsc / 1000 if dsc else -0.2
        flags = int(fd.get("/Flags", 0)) if fd is not None else 0
        weight = float(fd.get("/FontWeight", 0)) if fd is not None and fd.get("/FontWeight") is not None else 0
        low = self.basefont.lower()
        self.bold = bool(re.search(r"bold|black|heavy|semibold|demi|medium|extrabold|ultra", low)) or weight >= 600 \
            or bool(flags & (1 << 18))
        self.italic = bool(re.search(r"italic|oblique", low)) or bool(flags & (1 << 6))
        self.embedded = self.subtype == "/Type3" or (fd is not None and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3")))
        self.encoding_map = self._encoding_map(font) if not self.two_byte else {}
        self.invisible_glyphs = self._type3_is_invisible()

    # ---------------------------------------------------------------- unicode
    @staticmethod
    def _tounicode(font):
        cmap = {}
        tu = font.get("/ToUnicode")
        if tu is None or not isinstance(tu, pikepdf.Stream):
            return cmap
        s = tu.read_bytes().decode("latin-1")
        for block in re.findall(r"beginbfchar(.*?)endbfchar", s, re.S):
            for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
                cmap[int(src, 16)] = bytes.fromhex(dst).decode("utf-16-be", "replace")
        for block in re.findall(r"beginbfrange(.*?)endbfrange", s, re.S):
            for a, b, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", block):
                a, b = int(a, 16), int(b, 16)
                if rest.startswith("["):
                    for i, d in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", rest)):
                        cmap[a + i] = bytes.fromhex(d).decode("utf-16-be", "replace")
                else:
                    base = bytes.fromhex(rest[1:-1]).decode("utf-16-be", "replace")
                    for i in range(a, b + 1):
                        cmap[i] = base[:-1] + chr(ord(base[-1]) + i - a) if base else ""
        return cmap

    def _encoding_map(self, font):
        """code -> (glyph name, unicode) from /Encoding (base + Differences); used when ToUnicode lacks a code."""
        out = {}
        if EncodingDB is None:
            return out
        enc = font.get("/Encoding")
        base = "StandardEncoding"
        if self.std14 in ("Symbol", "ZapfDingbats"):
            base = self.std14
        diffs = []
        if isinstance(enc, pikepdf.Name):
            base = str(enc)[1:]
        elif isinstance(enc, pikepdf.Dictionary):
            if enc.get("/BaseEncoding") is not None:
                base = str(enc.BaseEncoding)[1:]
            if enc.get("/Differences") is not None:
                diffs = list(enc.Differences)
        elif self.subtype == "/TrueType" and not enc:
            base = "WinAnsiEncoding"
        try:
            table = dict(EncodingDB.get_encoding(base))
        except Exception:  # noqa: BLE001
            table = {}
        for code, uni in table.items():
            out[code] = (None, uni)
        code = 0
        for d in diffs:
            if isinstance(d, pikepdf.Name):
                gname = str(d)[1:]
                try:
                    out[code] = (gname, name2unicode(gname))
                except Exception:  # noqa: BLE001
                    out[code] = (gname, None)
                code += 1
            else:
                code = int(d)
        return out

    @staticmethod
    def _cid_widths(w):
        out = {}
        if w is None:
            return out
        w = list(w)
        i = 0
        while i < len(w):
            first = int(w[i])
            nxt = w[i + 1]
            if isinstance(nxt, pikepdf.Array):
                for k, val in enumerate(nxt):
                    out[first + k] = float(val)
                i += 2
            else:
                last, val = int(nxt), float(w[i + 2])
                for c in range(first, last + 1):
                    out[c] = val
                i += 3
        return out

    def _type3_is_invisible(self):
        if self.subtype != "/Type3":
            return False
        cps = self.obj.get("/CharProcs")
        if cps is None:
            return False
        for k in cps.keys():
            body = cps[k].read_bytes()
            if re.sub(rb"[-0-9.\s]+d[01]", b"", body).strip():
                return False
        return True

    def codes(self, raw: bytes):
        if self.two_byte:
            return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]
        return list(raw)

    def width(self, code):
        """Glyph advance in text space units (before font size)."""
        if self.two_byte:
            return self.cid_widths.get(code, self.dw) / 1000.0
        idx = code - self.first
        if self.widths and 0 <= idx < len(self.widths):
            w = self.widths[idx]
        elif self.std14:
            u = self.to_unicode(code) or ""
            w = FONT_METRICS[self.std14][1].get(u[:1], 500) if u else 500
        else:
            w = 0.0
        if self.subtype == "/Type3":
            return w * self.font_matrix[0]
        return w / 1000.0

    def to_unicode(self, code):
        """Unicode for a code: ToUnicode first, then encoding + glyph names. None when unknown."""
        u = self.cmap.get(code)
        if u is not None:
            return u
        if not self.two_byte:
            e = self.encoding_map.get(code)
            if e and e[1]:
                return e[1]
        return None

    def unicode_source(self, code):
        if code in self.cmap:
            return "tounicode"
        e = self.encoding_map.get(code)
        if e and e[1]:
            return "encoding"
        return None


# --------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------
@dataclass
class TextShow:
    op_index: int
    font: str
    size: float
    tm: tuple
    ctm: tuple
    text: str
    width: float
    operands: list
    operator: str
    state: dict
    missing_unicode: list = field(default_factory=list)
    x0: float = 0.0
    x1: float = 0.0
    mcid: Optional[int] = None
    glyphs: list = field(default_factory=list)   # (unicode, x_start, x_end) in text space, relative to tm
    cm_chain: tuple = ()                         # the cm operands in effect, verbatim, outermost first
    replay: tuple = ()                           # cm and clip operators in effect, verbatim, in order:
                                                 # replaying them rebuilds the exact CTM and clipping path


@dataclass
class TextLine:
    id: str
    shows: list
    bt_index: int
    baseline: float
    bbox: tuple = (0, 0, 0, 0)
    dbox: tuple = (0, 0, 0, 0)
    dbaseline: float = 0.0
    direction: int = 0          # text direction in display space: 0, 90, 180, 270
    rbox: tuple = (0, 0, 0, 0)  # bbox in the page's reading frame (dominant text direction made horizontal)
    rbaseline: float = 0.0
    size: float = 0.0
    fill: Optional[tuple] = None
    visible: bool = True
    clipped: bool = False
    visible_fraction: float = 1.0   # share of the line box inside a rectangular clip (1.0 when unknown)
    occluded: bool = False      # visible glyphs fully covered by an opaque shape painted later
    painted_over: bool = False  # some later graphic overlaps it (unsafe to move to the end of the stream)
    bold: bool = False
    italic: bool = False
    hint: Optional[dict] = None

    @property
    def text(self):
        return "".join(s.text for s in self.shows)

    @property
    def op_indices(self):
        return [s.op_index for s in self.shows]


@dataclass
class Graphic:
    id: str
    kind: str            # path | image | shading | form
    op_indices: list
    bbox: tuple
    fill: Optional[tuple]
    stroke: Optional[tuple]
    paint_op: str
    name: Optional[str] = None
    gsnames: tuple = ()
    dbox: tuple = (0, 0, 0, 0)
    rbox: tuple = (0, 0, 0, 0)
    hint: Optional[dict] = None


@dataclass
class Annot:
    id: str
    subtype: str
    rect: tuple
    dbox: tuple
    obj: pikepdf.Object
    uri: Optional[str] = None
    contents: Optional[str] = None
    field_name: Optional[str] = None
    tooltip: Optional[str] = None
    hidden: bool = False
    quads: Optional[list] = None


# --------------------------------------------------------------------------
# existing tags (hints)
# --------------------------------------------------------------------------
STANDARD = set("""Document Part Art Sect Div BlockQuote Caption TOC TOCI Index NonStruct Private
P H H1 H2 H3 H4 H5 H6 L LI Lbl LBody Table TR TH TD THead TBody TFoot Span Quote Note Reference
BibEntry Code Link Annot Ruby RB RT RP Warichu WT WP Figure Formula Form Title""".split())


def existing_tag_index(pdf):
    """(page_index, mcid) -> {"role", "path", "alt", "lang", "actual"} from the source's own tree."""
    st = pdf.Root.get("/StructTreeRoot")
    out = {}
    if st is None:
        return out
    rolemap = {str(k)[1:]: str(v)[1:] for k, v in st.get("/RoleMap", {}).items()} if st.get("/RoleMap") is not None else {}
    page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}

    def std(s):
        seen = set()
        while s not in STANDARD and s in rolemap and s not in seen:
            seen.add(s); s = rolemap[s]
        return s

    def visit(e, path, pg, alt, lang, depth=0, owner=None):
        if depth > 200 or not isinstance(e, pikepdf.Dictionary):
            return
        if e.get("/Type") == "/MCR":
            p = e.get("/Pg", pg)
            if p is not None:
                out[(page_index.get(p.objgen), int(e.MCID))] = dict(role=path[-1] if path else None, path=path, alt=alt,
                                                                     lang=lang, eid=owner)
            return
        if e.get("/Type") == "/OBJR":
            return
        s = std(str(e.get("/S", ""))[1:])
        a = str(e.Alt) if e.get("/Alt") is not None else alt
        lg = str(e.Lang) if e.get("/Lang") is not None else lang
        pg = e.get("/Pg", pg)
        k = e.get("/K")
        eid = f"{e.objgen[0]}" if e.is_indirect else f"d{id(e)}"
        for c in (list(k) if isinstance(k, pikepdf.Array) else ([] if k is None else [k])):
            if isinstance(c, pikepdf.Dictionary):
                visit(c, path + [s], pg, a, lg, depth + 1, owner=eid)
            else:
                try:
                    if pg is not None:
                        out[(page_index.get(pg.objgen), int(c))] = dict(role=s, path=path + [s], alt=a, lang=lg, eid=eid)
                except Exception:  # noqa: BLE001
                    pass
    k = st.get("/K")
    for c in (list(k) if isinstance(k, pikepdf.Array) else ([] if k is None else [k])):
        visit(c, [], None, None, None)
    return out


# --------------------------------------------------------------------------
# page model
# --------------------------------------------------------------------------
class PageModel:
    def __init__(self, pdf: pikepdf.Pdf, page_no: int, tag_index: Optional[dict] = None):
        self.pdf = pdf
        self.page_no = page_no
        self.page = pdf.pages[page_no - 1]
        self.ops = pikepdf.parse_content_stream(self.page)
        res = self.page.obj.get("/Resources", pikepdf.Dictionary())
        self.resources = res
        fonts = res.get("/Font", pikepdf.Dictionary())
        self.fonts = {str(k): FontInfo(str(k), fonts[k]) for k in fonts.keys()}
        # geometry
        mb = [float(x) for x in self.page.obj.get("/MediaBox", [0, 0, 612, 792])]
        cb = self.page.obj.get("/CropBox")
        cb = [float(x) for x in cb] if cb is not None else mb
        self.crop = (max(cb[0], mb[0]), max(cb[1], mb[1]), min(cb[2], mb[2]), min(cb[3], mb[3]))
        self.rotate = int(self.page.obj.get("/Rotate", 0)) % 360
        w, h = self.crop[2] - self.crop[0], self.crop[3] - self.crop[1]
        self.disp_w, self.disp_h = (h, w) if self.rotate in (90, 270) else (w, h)
        self.lines: list[TextLine] = []
        self.graphics: list[Graphic] = []
        self.annots: list[Annot] = []
        self.clip_ops: set = set()
        self.marked_ops: set = set()
        self.existing_marked: list = []
        self.tag_index = tag_index or {}
        self._walk()
        self._annots()

    # ------------------------------------------------------------ geometry
    def to_display_pt(self, x, y):
        x -= self.crop[0]; y -= self.crop[1]
        w, h = self.crop[2] - self.crop[0], self.crop[3] - self.crop[1]
        if self.rotate == 90:
            return (y, w - x)
        if self.rotate == 180:
            return (w - x, h - y)
        if self.rotate == 270:
            return (h - y, x)
        return (x, y)

    def to_reading_pt(self, x, y):
        d = getattr(self, "text_dir", 0)
        W, H = self.disp_w, self.disp_h
        if d == 90:
            return (y, W - x)
        if d == 270:
            return (H - y, x)
        if d == 180:
            return (W - x, H - y)
        return (x, y)

    def to_reading(self, dbox):
        x0, y0, x1, y1 = dbox
        return bbox_of_points([self.to_reading_pt(x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))])

    def to_display(self, bbox):
        x0, y0, x1, y1 = bbox
        return bbox_of_points([self.to_display_pt(x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))])

    # ------------------------------------------------------------ walk
    def _walk(self):
        P = f"p{self.page_no}"
        gs = dict(ctm=IDENTITY, cm_chain=(), replay=(), fill=("DeviceGray", (0.0,)), stroke=("DeviceGray", (0.0,)),
                  Tc=0.0, Tw=0.0, Tz=100.0, TL=0.0, Ts=0.0, Tr=0, font=None, size=0.0,
                  gsnames=(), clipped=False, clip_box=None, clip_rect=True)
        stack = []
        mc_stack = []
        tm = tlm = IDENTITY
        bt_index = -1
        cur_line: Optional[TextLine] = None
        pending_path = []
        pending_clip = False
        line_no = 0
        g_no = 0

        def flush_line():
            nonlocal cur_line
            if cur_line is not None:
                self._finish_line(cur_line)
                self.lines.append(cur_line)
                cur_line = None

        def cur_mcid():
            for m in reversed(mc_stack):
                if m is not None:
                    return m
            return None

        def add_clip(idxs):
            """W / W* then n (or a paint operator): intersect the clip, remember how to replay it."""
            gs["clipped"] = True
            cb = self._path_bbox(idxs, gs["ctm"])
            old = gs["clip_box"]
            gs["clip_box"] = cb if old is None else (max(old[0], cb[0]), max(old[1], cb[1]), min(old[2], cb[2]), min(old[3], cb[3]))
            gs["clip_rect"] = gs["clip_rect"] and self._is_rect_path(idxs)
            ops_ = [(tuple(self.ops[k].operands), str(self.ops[k].operator)) for k in idxs
                    if str(self.ops[k].operator) in PATH_CONSTRUCT or str(self.ops[k].operator) in CLIP]
            gs["replay"] = gs["replay"] + tuple(ops_) + (((), "n"),)

        for i, (operands, op) in enumerate(self.ops):
            o = str(op)
            if o in MARKED:
                self.marked_ops.add(i)
                if o in ("BDC", "BMC"):
                    self.existing_marked.append((i, str(operands[0])))
                    mcid = None
                    if o == "BDC" and len(operands) > 1:
                        props = operands[1]
                        if isinstance(props, pikepdf.Name):
                            props = self.resources.get("/Properties", {}).get(props)
                        if isinstance(props, pikepdf.Dictionary) and "/MCID" in props:
                            mcid = int(props.MCID)
                    if str(operands[0]) == "/Artifact":
                        mcid = -1
                    mc_stack.append(mcid)
                elif o == "EMC" and mc_stack:
                    mc_stack.pop()
                continue
            if o == "q":
                stack.append(dict(gs))
            elif o == "Q":
                if stack:
                    gs = stack.pop()
            elif o == "cm":
                gs["ctm"] = mat_mul(tuple(float(x) for x in operands), gs["ctm"])
                gs["cm_chain"] = gs["cm_chain"] + (tuple(operands),)
                gs["replay"] = gs["replay"] + ((tuple(operands), "cm"),)
            elif o == "gs":
                gs["gsnames"] = gs["gsnames"] + (str(operands[0]),)
            elif o == "cs":
                gs["fill"] = (str(operands[0]), ())
            elif o == "CS":
                gs["stroke"] = (str(operands[0]), ())
            elif o in ("sc", "scn"):
                gs["fill"] = (gs["fill"][0], tuple(float(x) for x in operands if not isinstance(x, pikepdf.Name)))
            elif o in ("SC", "SCN"):
                gs["stroke"] = (gs["stroke"][0], tuple(float(x) for x in operands if not isinstance(x, pikepdf.Name)))
            elif o == "g":
                gs["fill"] = ("DeviceGray", (float(operands[0]),))
            elif o == "G":
                gs["stroke"] = ("DeviceGray", (float(operands[0]),))
            elif o == "rg":
                gs["fill"] = ("DeviceRGB", tuple(float(x) for x in operands))
            elif o == "RG":
                gs["stroke"] = ("DeviceRGB", tuple(float(x) for x in operands))
            elif o == "k":
                gs["fill"] = ("DeviceCMYK", tuple(float(x) for x in operands))
            elif o == "K":
                gs["stroke"] = ("DeviceCMYK", tuple(float(x) for x in operands))
            elif o == "BT":
                bt_index = i
                tm = tlm = IDENTITY
            elif o == "ET":
                flush_line()
            elif o in TEXT_STATE:
                if o == "Tf":
                    gs["font"] = str(operands[0]); gs["size"] = float(operands[1])
                elif o == "Tr":
                    gs["Tr"] = int(operands[0])
                else:
                    gs[o] = float(operands[0])
            elif o in TEXT_POS:
                if o == "Tm":
                    tm = tlm = tuple(float(x) for x in operands)
                elif o in ("Td", "TD"):
                    tx, ty = float(operands[0]), float(operands[1])
                    if o == "TD":
                        gs["TL"] = -ty
                    tlm = mat_mul((1, 0, 0, 1, tx, ty), tlm); tm = tlm
                elif o == "T*":
                    tlm = mat_mul((1, 0, 0, 1, 0, -gs["TL"]), tlm); tm = tlm
            elif o in TEXT_SHOW:
                if o in ("'", '"'):
                    if o == '"':
                        gs["Tw"], gs["Tc"] = float(operands[0]), float(operands[1])
                    tlm = mat_mul((1, 0, 0, 1, 0, -gs["TL"]), tlm); tm = tlm
                show, tm = self._show(i, o, operands, gs, tm)
                show.mcid = cur_mcid()
                trm = mat_mul(show.tm, gs["ctm"])
                base = round(apply(trm, 0, 0)[1], 1)
                if (cur_line is None or abs(cur_line.baseline - base) > 0.5 or cur_line.bt_index != bt_index
                        or (cur_line.shows and cur_line.shows[-1].mcid != show.mcid and self.tag_index)):
                    flush_line()
                    line_no += 1
                    cur_line = TextLine(id=f"{P}.L{line_no}", shows=[], bt_index=bt_index, baseline=base)
                if cur_line.shows and show.text and not show.text[0].isspace():
                    prev = cur_line.shows[-1]
                    pend = apply(mat_mul(prev.tm, prev.ctm), prev.width, 0)
                    pstart = apply(trm, 0, 0)
                    gap = math.hypot(pstart[0] - pend[0], pstart[1] - pend[1]) * (1 if pstart[0] >= pend[0] else -1)
                    if gap >= 0.15 * show.size * scale_of(trm) and prev.text and not prev.text[-1].isspace():
                        show.text = " " + show.text   # inferred word gap between two show operators
                cur_line.shows.append(show)
            elif o in PATH_CONSTRUCT:
                pending_path.append(i)
            elif o in CLIP:
                pending_clip = True
                pending_path.append(i)
            elif o == "n":
                pending_path.append(i)
                self.clip_ops.update(pending_path)
                if pending_clip:
                    add_clip(pending_path)
                pending_path = []; pending_clip = False
            elif o in PATH_PAINT:
                pending_path.append(i)
                if pending_clip:
                    add_clip(pending_path)
                g_no += 1
                fill = gs["fill"] if o not in ("S", "s") else None
                stroke = gs["stroke"] if o in ("S", "s", "B", "B*", "b", "b*") else None
                g = Graphic(id=f"{P}.G{g_no}", kind="path", op_indices=pending_path,
                            bbox=self._path_bbox(pending_path, gs["ctm"]), fill=fill, stroke=stroke, paint_op=o,
                            gsnames=gs["gsnames"])
                g.hint = self._hint(cur_mcid())
                self.graphics.append(g)
                pending_path = []; pending_clip = False
            elif o in XOBJ_OR_SHADING:
                g_no += 1
                kind = {"Do": "xobject", "sh": "shading"}.get(o, "image")
                name = str(operands[0]) if operands and o != "INLINE IMAGE" else None
                c = gs["ctm"]
                unit = ((0, 0), (1, 0), (0, 1), (1, 1))
                if o == "Do":
                    xo = res_get(self.resources, "/XObject", name)
                    if xo is not None and xo.get("/Subtype") == "/Form":
                        kind = "form"
                        bb = [float(x) for x in xo.get("/BBox", [0, 0, 1, 1])]
                        fm = tuple(float(x) for x in xo.get("/Matrix", IDENTITY))
                        c = mat_mul(fm, c)
                        unit = ((bb[0], bb[1]), (bb[2], bb[1]), (bb[0], bb[3]), (bb[2], bb[3]))
                    else:
                        kind = "image"
                pts = [apply(c, x, y) for x, y in unit]
                g = Graphic(id=f"{P}.G{g_no}", kind=kind, op_indices=[i], bbox=bbox_of_points(pts),
                            fill=None, stroke=None, paint_op=o, name=name, gsnames=gs["gsnames"])
                g.hint = self._hint(cur_mcid())
                self.graphics.append(g)
        flush_line()
        for g in self.graphics:
            g.dbox = self.to_display(g.bbox)
        # reading frame: rotate display space so the dominant text direction is horizontal (layout runs there)
        weight = {}
        for ln in self.lines:
            weight[ln.direction] = weight.get(ln.direction, 0) + len(ln.text.strip())
        self.text_dir = max(weight, key=weight.get) if weight else 0
        self.read_w, self.read_h = (self.disp_h, self.disp_w) if self.text_dir in (90, 270) else (self.disp_w, self.disp_h)
        for g in self.graphics:
            g.rbox = self.to_reading(g.dbox)
        self._occlusion()
        for ln in self.lines:
            ln.rbox = self.to_reading(ln.dbox)
            s0 = ln.shows[0]
            p = self.to_display_pt(*apply(mat_mul(s0.tm, s0.ctm), s0.x0, 0))
            ln.rbaseline = round(self.to_reading_pt(*p)[1], 1)

    def _is_rect_path(self, idxs):
        """A single axis-aligned rectangle: one `re`, or m/l…h whose points are all corners of their bbox."""
        ops = [str(self.ops[k][1]) for k in idxs]
        if any(o in ("c", "v", "y") for o in ops):
            return False
        if ops.count("re") == 1 and not any(o in ("m", "l") for o in ops):
            return True
        if "re" in ops or ops.count("m") != 1:
            return False
        pts = []
        for k in idxs:
            operands, op = self.ops[k]
            if str(op) in ("m", "l"):
                pts.append((float(operands[0]), float(operands[1])))
        if len(pts) < 4 or len(pts) > 5:
            return False
        xs = sorted({round(x, 2) for x, _ in pts}); ys = sorted({round(y, 2) for _, y in pts})
        return len(xs) == 2 and len(ys) == 2

    def _translucent(self, gsnames):
        egs = self.resources.get("/ExtGState", {})
        for n in gsnames:
            d = egs.get(n)
            if d is not None and (float(d.get("/ca", 1)) < 1 or d.get("/SMask") not in (None, "/None")):
                return True
        return False

    def _occlusion(self):
        for ln in self.lines:
            if not ln.visible or not ln.text.strip():
                continue
            last = max(ln.op_indices)
            a = max((ln.bbox[2] - ln.bbox[0]) * (ln.bbox[3] - ln.bbox[1]), 1e-6)
            for g in self.graphics:
                if g.op_indices[-1] <= last:
                    continue
                ox0, oy0 = max(g.bbox[0], ln.bbox[0]), max(g.bbox[1], ln.bbox[1])
                ox1, oy1 = min(g.bbox[2], ln.bbox[2]), min(g.bbox[3], ln.bbox[3])
                if ox1 <= ox0 or oy1 <= oy0:
                    continue
                cover = (ox1 - ox0) * (oy1 - oy0) / a
                if cover >= 0.15 or (g.fill is not None and g.paint_op not in ("S", "s") and cover >= 0.05) or g.kind != "path":
                    ln.painted_over = True
                opaque = (g.kind == "image" or (g.fill is not None and g.paint_op not in ("S", "s"))) \
                    and not self._translucent(g.gsnames)
                if cover >= 0.9 and opaque and g.kind == "path" and len(g.op_indices) <= 6:
                    ln.occluded = True

    def _hint(self, mcid):
        if mcid is None or mcid < 0 or not self.tag_index:
            return {"role": "Artifact"} if mcid == -1 else None
        return self.tag_index.get((self.page_no - 1, mcid))

    # ------------------------------------------------------------ text
    def _show(self, i, o, operands, gs, tm):
        font = self.fonts.get(gs["font"])
        size, tc, tw, th = gs["size"], gs["Tc"], gs["Tw"], gs["Tz"] / 100.0
        start_tm = tm
        text, missing = [], []
        glyphs = []
        adv_total = 0.0
        first_x, last_x = None, 0.0
        items = list(operands[0]) if o == "TJ" else [operands[-1]]
        for it in items:
            if isinstance(it, pikepdf.String):
                raw = bytes(it)
                for code in (font.codes(raw) if font else raw):
                    u = font.to_unicode(code) if font else None
                    if u is None:
                        missing.append(code)
                        u = "�"
                    text.append(u)
                    w = font.width(code) if font else 0.5
                    adv = (w * size + tc + (tw if (not (font and font.two_byte) and code == 32) else 0)) * th
                    if first_x is None and not u.isspace():
                        first_x = adv_total
                    glyphs.append((u, adv_total, adv_total + adv))
                    adv_total += adv
                    if not u.isspace():
                        last_x = adv_total
                    tm = mat_mul((1, 0, 0, 1, adv, 0), tm)
            else:
                adv = -float(it) / 1000.0 * size * th
                if adv >= 0.15 * size and text and not text[-1].isspace():
                    text.append(" ")            # word gap expressed as positioning (pdfTeX & co.): analysis only
                adv_total += adv
                tm = mat_mul((1, 0, 0, 1, adv, 0), tm)
        state = dict(Tc=tc, Tw=tw, Tz=gs["Tz"], Ts=gs["Ts"], Tr=gs["Tr"], fill=gs["fill"],
                     gsnames=gs["gsnames"], clipped=gs["clipped"], clip_box=gs["clip_box"], clip_rect=gs["clip_rect"])
        return TextShow(op_index=i, font=gs["font"], size=size, tm=start_tm, ctm=gs["ctm"],
                        text="".join(text), width=adv_total, operands=list(operands), operator=o,
                        state=state, missing_unicode=missing,
                        x0=first_x if first_x is not None else 0.0, x1=last_x, glyphs=glyphs,
                        cm_chain=gs["cm_chain"], replay=gs["replay"]), tm

    def _finish_line(self, line: TextLine):
        pts, sizes, vis = [], [], []
        for s in line.shows:
            font = self.fonts.get(s.font)
            asc = font.ascent if font else 0.8
            dsc = font.descent if font else -0.2
            trm = mat_mul(s.tm, s.ctm)
            sizes.append(s.size * scale_of(trm))
            invisible = s.state["Tr"] in (3, 7) or (font is not None and font.invisible_glyphs)
            vis.append(not invisible)
            if s.x1 <= s.x0:
                continue
            for x, y in ((s.x0, dsc * s.size), (s.x1, dsc * s.size), (s.x0, asc * s.size), (s.x1, asc * s.size)):
                pts.append(apply(trm, x, y))
        if not pts:   # whitespace-only line: a zero-width box at its origin
            s = line.shows[0]
            pts = [apply(mat_mul(s.tm, s.ctm), 0, 0)] * 2
        line.bbox = bbox_of_points(pts)
        line.dbox = self.to_display(line.bbox)
        s0 = line.shows[0]
        t0 = mat_mul(s0.tm, s0.ctm)
        o = self.to_display_pt(*apply(t0, 0, 0))
        e = self.to_display_pt(*apply(t0, 1, 0))
        ang = math.degrees(math.atan2(e[1] - o[1], e[0] - o[0])) % 360
        line.direction = int(round(ang / 90.0)) % 4 * 90
        line.dbaseline = round(self.to_display_pt(*apply(t0, s0.x0, 0))[1], 1)
        line.size = round(max(sizes), 2) if sizes else 0
        line.fill = s0.state["fill"]
        line.visible = any(vis)
        # clipped only if a clip can actually cut the glyphs: a rectangular clip that contains the line is harmless
        def cuts(st):
            if not st["clipped"]:
                return False
            cb = st.get("clip_box")
            if cb is None or not st.get("clip_rect", False):
                return True
            b = line.bbox
            return not (cb[0] <= b[0] + 0.5 and cb[1] <= b[1] + 0.5 and cb[2] >= b[2] - 0.5 and cb[3] >= b[3] - 0.5)
        line.clipped = any(cuts(s.state) for s in line.shows)
        if line.clipped:
            fr = []
            for s_ in line.shows:
                cb, b = s_.state.get("clip_box"), line.bbox
                if s_.state.get("clipped") and cb is not None and s_.state.get("clip_rect", False):
                    ix = max(0.0, min(cb[2], b[2]) - max(cb[0], b[0]))
                    iy = max(0.0, min(cb[3], b[3]) - max(cb[1], b[1]))
                    fr.append(ix * iy / max((b[2] - b[0]) * (b[3] - b[1]), 1e-6))
            if fr:
                line.visible_fraction = round(max(fr), 3)
        fonts = [self.fonts.get(s.font) for s in line.shows if s.text.strip()]
        line.bold = bool(fonts) and all(f is not None and f.bold for f in fonts)
        line.italic = bool(fonts) and all(f is not None and f.italic for f in fonts)
        line.hint = self._hint(s0.mcid)

    def _path_bbox(self, idxs, ctm):
        pts = []
        for i in idxs:
            operands, op = self.ops[i]
            o = str(op)
            nums = [float(x) for x in operands]
            if o == "re":
                x, y, w, h = nums
                pts += [(x, y), (x + w, y + h)]
            else:
                pts += list(zip(nums[0::2], nums[1::2]))
        if not pts:
            return (0, 0, 0, 0)
        return bbox_of_points([apply(ctm, x, y) for x, y in pts])

    # ------------------------------------------------------------ annotations
    def _annots(self):
        for k, a in enumerate(self.page.obj.get("/Annots", []) or [], 1):
            if not isinstance(a, pikepdf.Dictionary):
                continue
            st = str(a.get("/Subtype", ""))[1:]
            r = [float(x) for x in a.get("/Rect", [0, 0, 0, 0])]
            rect = (min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3]))
            uri = None
            act = a.get("/A")
            if act is not None and act.get("/URI") is not None:
                uri = str(act.URI)
            flags = int(a.get("/F", 0))
            fname = tip = None
            if st == "Widget":
                f = a
                parts = []
                while f is not None and len(parts) < 20:
                    if f.get("/T") is not None:
                        parts.append(str(f.T))
                    tip = tip or (str(f.TU) if f.get("/TU") is not None else None)
                    f = f.get("/Parent")
                fname = ".".join(reversed(parts)) or None
            self.annots.append(Annot(id=f"p{self.page_no}.A{k}", subtype=st, rect=rect, dbox=self.to_display(rect), obj=a,
                                     uri=uri, contents=str(a.Contents) if a.get("/Contents") is not None else None,
                                     field_name=fname, tooltip=tip, hidden=bool(flags & 2) or bool(flags & 32),
                                     quads=[float(x) for x in a.QuadPoints] if a.get("/QuadPoints") is not None else None))


def text_in_rect(lines, rect, quads=None):
    """Characters of `lines` whose glyph centre lies inside `rect` — or inside one of the annotation's
    QuadPoints when present (wrapped links): the visible text of a link."""
    boxes = [rect]
    if quads and len(quads) >= 8:
        boxes = []
        for k in range(0, len(quads) - 7, 8):
            xs, ys = quads[k:k + 8:2], quads[k + 1:k + 8:2]
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
    out = []
    for ln in lines:
        for s in ln.shows:
            trm = mat_mul(s.tm, s.ctm)
            for u, xa, xb in s.glyphs:
                cx, cy = apply(trm, (xa + xb) / 2, 0.3 * s.size)
                if any(b[0] - 0.5 <= cx <= b[2] + 0.5 and b[1] - 0.5 <= cy <= b[3] + 0.5 for b in boxes):
                    out.append(u)
        out.append(" ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def res_get(resources, cat, name):
    try:
        d = resources.get(cat)
        return d.get(name) if d is not None else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# colour helpers
# --------------------------------------------------------------------------
def to_srgb(color, pdf_page=None):
    if color is None:
        return None
    space, comps = color
    n = len(comps)
    if n == 1:
        return (comps[0],) * 3
    if n == 3:
        return tuple(comps)
    if n == 4:
        c, m, y, k = comps
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return None


def rel_luminance(rgb):
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(rgb1, rgb2):
    l1, l2 = rel_luminance(rgb1), rel_luminance(rgb2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def hexcolor(rgb):
    return "#" + "".join(f"{round(max(0, min(1, c)) * 255):02X}" for c in rgb)


def overlap(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
