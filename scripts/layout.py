"""
layout.py — derive reading order from the page geometry.

Rows-first recursive XY-cut on text-line boxes:
  * a horizontal whitespace band that spans the whole region and is at least
    `row_gap` high splits the region into ROWS (read top → bottom);
  * otherwise a vertical band that spans the whole region and is at least
    `col_gap` wide splits it into COLUMNS (read left → right, or right → left
    for RTL documents);
  * otherwise the region is a leaf: lines are read top → bottom, then by x.

Rows are tried before columns at every level, which is what gives
"header row (2 columns) then body row (2 columns)" instead of reading a whole
left column before the header's right-hand block. Gap thresholds scale with
the median line height so they follow the document's own rhythm of spacing.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass
class Box:
    id: str
    x0: float
    y0: float
    x1: float
    y1: float
    baseline: float = 0.0
    n: int = 1                  # number of text lines this box stands for (collapsed tables / figures)
    lh: float = 0.0             # height of one of those lines (0 = the box height)


@dataclass
class Region:
    kind: str                     # page | row | column | leaf
    bbox: tuple
    children: list = field(default_factory=list)
    boxes: list = field(default_factory=list)
    label: str = ""
    gap: float = 0.0              # whitespace that separated this region from its previous sibling

    def ordered_ids(self):
        if self.kind == "leaf":
            return [b.id for b in self.boxes]
        return [i for c in self.children for i in c.ordered_ids()]


def _bbox(boxes):
    return (min(b.x0 for b in boxes), min(b.y0 for b in boxes), max(b.x1 for b in boxes), max(b.y1 for b in boxes))


def _bands(intervals):
    """Merge 1-D intervals; return list of (start, end, members) clusters, sorted by start."""
    iv = sorted(intervals, key=lambda t: t[0])
    out = []
    for a, b, m in iv:
        if out and a <= out[-1][1]:
            s, e, ms = out[-1]
            out[-1] = (s, max(e, b), ms + [m])
        else:
            out.append((a, b, [m]))
    return out


def _split_rows(boxes, min_gap):
    clusters = _bands([(b.y0, b.y1, b) for b in boxes])           # bottom → top
    if len(clusters) < 2:
        return None
    cuts = [i for i in range(len(clusters) - 1) if clusters[i + 1][0] - clusters[i][1] >= min_gap]
    if not cuts:
        return None
    groups, start, above = [], 0, []          # built bottom → top
    for i in cuts:
        groups.append([b for c in clusters[start:i + 1] for b in c[2]]); start = i + 1
        above.append(clusters[i + 1][0] - clusters[i][1])   # gap between this group and the one above
    groups.append([b for c in clusters[start:] for b in c[2]])
    above.append(0.0)                                        # top group has nothing above
    # top → bottom; each group carries the gap separating it from the group above it
    top_down = list(reversed(groups))
    gaps_top_down = [0.0] + list(reversed(above[:-1]))
    return list(zip(top_down, gaps_top_down))


def _aligned(a, b, tol=1.5):
    """(share, count) of the smaller side's baselines that line up with a baseline on the other side."""
    small, big = (a, b) if len({round(x.baseline) for x in a}) <= len({round(x.baseline) for x in b}) else (b, a)
    bs = sorted({x.baseline for x in big})
    sb = {x.baseline for x in small}
    if not sb:
        return 0.0, 0
    hit = sum(1 for y in sb if any(abs(y - z) <= tol for z in bs))
    return hit / len(sb), hit


def _is_column(group, h_med, region_w):
    width = max(b.x1 for b in group) - min(b.x0 for b in group)
    lines = len({round(b.baseline) for b in group}) + sum(b.n - 1 for b in group)
    return lines >= 2 and width >= max(4 * h_med, 0.12 * region_w)


def _split_cols(boxes, min_gap, h_med):
    clusters = _bands([(b.x0, b.x1, b) for b in boxes])           # left → right
    if len(clusters) < 2:
        return None
    region_w = clusters[-1][1] - clusters[0][0]
    groups = [list(clusters[0][2])]
    gaps = [0.0]
    for prev, cur in zip(clusters, clusters[1:]):
        gap = cur[0] - prev[1]
        g_prev, g_cur = groups[-1], list(cur[2])
        w_prev = max(b.x1 for b in g_prev) - min(b.x0 for b in g_prev)
        w_cur = cur[1] - cur[0]
        share, count = _aligned(g_prev, g_cur)
        table_like = share >= 0.8 and count >= 3 and gap > 0.5 * min(w_prev, w_cur)
        if gap >= min_gap and not table_like and _is_column(g_prev, h_med, region_w) and _is_column(g_cur, h_med, region_w):
            groups.append(g_cur); gaps.append(gap)
        else:
            groups[-1] = g_prev + g_cur                  # same column: keep reading row by row
    if len(groups) < 2:
        return None
    return list(zip(groups, gaps))


def _leaf_order(boxes, tol=2.0):
    """Visual lines top to bottom, left to right inside a line. Boxes share a visual line when their
    baselines match, or when they sit side by side (no horizontal overlap) with vertical extents that
    overlap by half the smaller height — e.g. a 22pt title and a 9pt note centred on the same line."""
    rows = []
    for b in sorted(boxes, key=lambda b: -b.baseline):
        if rows:
            base, r = rows[-1]
            if abs(base - b.baseline) <= tol:
                r.append(b)
                continue
            lo, hi = min(x.y0 for x in r), max(x.y1 for x in r)
            h = min(b.y1 - b.y0, min(x.y1 - x.y0 for x in r))
            side = all(b.x1 <= x.x0 or b.x0 >= x.x1 for x in r)
            if side and h > 0 and min(hi, b.y1) - max(lo, b.y0) >= 0.5 * h:
                r.append(b)
                continue
        rows.append([b.baseline, [b]])
    return [b for _, r in rows for b in sorted(r, key=lambda b: b.x0)]


def _xrange(group):
    return min(b.x0 for b in group), max(b.x1 for b in group)


def _side_by_side(bs, min_gap):
    """Two boxes on the same baseline separated by a wide gap: several flows share this band."""
    by = {}
    for b in bs:
        by.setdefault(round(b.baseline), []).append(b)
    for row in by.values():
        row = sorted(row, key=lambda b: b.x0)
        if any(nb.x0 - pb.x1 >= min_gap for pb, nb in zip(row, row[1:])):
            return True
    return False


def _gutter_rows(bs, sbs_gap):
    """
    Several flows share this region but something crosses the gutter (a full-width title, an intro):
    find the gutter from side-by-side lines, then cut rows around the lines that cross it.
    """
    by = {}
    for b in bs:
        by.setdefault(round(b.baseline), []).append(b)
    pairs = []
    for row in by.values():
        row = sorted(row, key=lambda b: b.x0)
        pairs += [(pb, nb) for pb, nb in zip(row, row[1:]) if nb.x0 - pb.x1 >= sbs_gap]
    if not pairs:
        return None
    # candidate gutters = where the right-hand flow of each side-by-side pair starts; keep the best scoring one
    h = statistics.median(max(b.y1 - b.y0, 1.0) for b in bs)
    best = None
    for g1 in sorted({round(nb.x0) for _, nb in pairs}):
        right = [b for b in bs if b.x0 >= g1 - 2]
        left = [b for b in bs if b.x1 <= g1 - 4]
        span = [b for b in bs if b.x0 < g1 - 2 and b.x1 > g1 - 4]
        if not span or not left or not right:
            continue
        g0 = max(b.x1 for b in left)
        if g1 - g0 < 4:
            continue
        substantial = lambda side: len({round(b.baseline) for b in side if b.x1 - b.x0 >= 3 * h})
        if substantial(left) < 2 or substantial(right) < 2:
            continue
        score = min(len(left), len(right)) - 2 * len(span)
        if best is None or score > best[0]:
            best = (score, g1, left, right, span)
    if best is None:
        return None
    _score, g1, left, right, span = best
    ivs = _bands([(b.y0, b.y1, b) for b in span])            # bottom → top
    cuts = []                                                   # y-intervals occupied by crossing lines
    for y0, y1, _m in ivs:
        cuts.append((y0, y1))
    groups = {}
    for b in bs:
        cy = (b.y0 + b.y1) / 2
        key = None
        for k, (y0, y1) in enumerate(cuts):
            if y0 - 0.5 <= cy <= y1 + 0.5:
                key = ("span", k)
                break
        if key is None:
            above = sum(1 for (y0, y1) in cuts if y0 > cy)        # number of crossing bands above → which gap
            key = ("flow", above)
        groups.setdefault(key, []).append(b)

    # a flow group that holds the column band: left-only lines sitting above the right flow's first line
    # (e.g. the wrapped end of an intro paragraph) belong to the crossing band just above them
    for key in [k for k in groups if k[0] == "flow"]:
        g = groups[key]
        rs = [b for b in g if b.x0 >= g1 - 2]
        if not rs:
            continue
        top_right = max(b.y1 for b in rs)
        above = [b for b in g if b.x1 <= g1 - 4 and b.y0 > top_right]
        # cuts are stored bottom → top; the nearest crossing band above this flow group is cuts[len(cuts) - above]
        span_key = ("span", len(cuts) - key[1]) if key[1] >= 1 else None
        if above and span_key in groups:
            groups[span_key] += above
            groups[key] = [b for b in g if b not in above]

    def top(g):
        return max(x.y1 for x in g)
    rows = sorted([g for g in groups.values() if g], key=top, reverse=True)
    if len(rows) < 2:
        return None
    return [(g, 0.0) for g in rows]


def _merge_continuations(rows, col_gap, h_med):
    """A lower group that sits inside ONE column of the group above continues that column (unequal columns)."""
    rows = list(rows)
    k = 0
    while k < len(rows) - 1:
        upper, gp = rows[k]
        lower, _ = rows[k + 1]
        cols = _split_cols(upper, col_gap, h_med)
        merged = False
        if cols:
            lx0, lx1 = _xrange(lower)
            ltop = max(b.y1 for b in lower)
            bottoms = [min(b.y0 for b in cg) for cg, _g in cols]
            for (cg, _g), cbot in zip(cols, bottoms):
                cx0, cx1 = _xrange(cg)
                # the column it continues must be the one ending closest above it (the others ended earlier/higher)
                closest = all(cbot <= ob + 0.5 for ob in bottoms)
                if lx0 >= cx0 - 6 and lx1 <= cx1 + 6 and closest and cbot - ltop < 4 * h_med:
                    rows[k] = (upper + lower, gp)
                    del rows[k + 1]
                    merged = True
                    break
        if not merged:
            k += 1
    return rows


def analyze(boxes, row_gap=None, col_gap=None, rtl=False, max_depth=8):
    """Return the Region tree for a list of Box (one page)."""
    boxes = [b for b in boxes if b.x1 >= b.x0 and b.y1 >= b.y0]
    if not boxes:
        r = Region("page", (0, 0, 0, 0))
        r.params = {"row_gap": 0, "col_gap": 0, "median_line_height": 0}
        return r
    h_med = statistics.median([max(b.lh or (b.y1 - b.y0), 1.0) for b in boxes for _ in range(max(b.n, 1))])
    row_gap = row_gap if row_gap is not None else 2.5 * h_med
    col_gap = col_gap if col_gap is not None else max(0.6 * h_med, 4.0)

    sbs_gap = 2.5 * h_med

    def rec(bs, kind, depth, gap=0.0):
        r = Region(kind, _bbox(bs), gap=gap)
        if depth < max_depth and len(bs) > 1:
            rows = _split_rows(bs, row_gap)
            if rows:
                rows = _merge_continuations(rows, col_gap, h_med)
            if rows and len(rows) >= 2:
                r.children = [rec(g, "row", depth + 1, gp) for g, gp in rows]
                return r
            cols = _split_cols(bs, col_gap, h_med)
            if cols:
                # a block at the foot of one column that sits closer to where the OTHER columns end than to its
                # own column's text is a row after the band (a page note), not the end of that column
                pulled = []
                for k, (cg, _gp) in enumerate(cols):
                    sub = _split_rows(cg, row_gap)
                    if not sub or len(sub) < 2:
                        continue
                    last = sub[-1][0]
                    last_top = max(b.y1 for b in last)
                    own_gap = min(b.y0 for b in sub[-2][0]) - last_top
                    others = [min(b.y0 for b in og) for j, (og, _g) in enumerate(cols) if j != k]
                    if others and all(ob > last_top for ob in others):
                        other_gap = min(ob - last_top for ob in others)
                        if other_gap < own_gap:
                            pulled += last
                if pulled:
                    band = [b for b in bs if b not in pulled]
                    r.children = [rec(band, "row", depth + 1, 0.0), rec(pulled, "row", depth + 1, 0.0)]
                    return r
                if rtl:
                    cols = list(reversed(cols))
                r.children = [rec(g, "column", depth + 1, gp) for g, gp in cols]
                return r
            grows = _gutter_rows(bs, sbs_gap)
            if grows and len(grows) >= 2:
                grows = _merge_continuations(grows, col_gap, h_med)
                if len(grows) >= 2:
                    r.children = [rec(g, "row", depth + 1, gp) for g, gp in grows]
                    return r
        r.boxes = _leaf_order(bs)
        if kind not in ("row", "column"):
            r.kind = "leaf"
        else:
            r.children = [Region("leaf", r.bbox, boxes=r.boxes)]
            r.boxes = []
        return r

    root = rec(boxes, "page", 0)
    _label(root)
    root.params = {"row_gap": round(row_gap, 1), "col_gap": round(col_gap, 1), "median_line_height": round(h_med, 1)}
    return root


def _label(r, prefix=""):
    ri = ci = 0
    for c in r.children:
        if c.kind == "row":
            ri += 1; c.label = f"{prefix}R{ri}"
        elif c.kind == "column":
            ci += 1; c.label = f"{prefix}C{ci}"
        else:
            c.label = prefix.rstrip("·") or "body"
        _label(c, c.label + "·" if c.kind in ("row", "column") else prefix)


def regions_with_columns(root):
    """Rows (or the page) that are split into ≥2 columns: these are the containers worth tagging."""
    out = []

    def rec(r):
        if any(c.kind == "column" for c in r.children):
            out.append(r)
        for c in r.children:
            rec(c)
    rec(root)
    return out


def leaf_regions(root):
    """Innermost row/column regions (the units a reader stays inside)."""
    out = []

    def rec(r):
        kids = [c for c in r.children if c.kind in ("row", "column")]
        if not kids:
            if r.kind != "leaf":
                out.append(r)
            return
        for c in kids:
            rec(c)
    rec(root)
    if not out:
        out = [root]
    return out


def region_map(root):
    """id -> label of the innermost column/row that holds it."""
    m = {}
    for reg in leaf_regions(root):
        for i in reg.ordered_ids():
            m[i] = reg.label or "page"
    return m


def describe(root, indent=0, texts=None):
    lines = []
    pad = "  " * indent
    if root.kind == "leaf":
        ids = root.ordered_ids()
        span = f"{ids[0]} … {ids[-1]}" if len(ids) > 2 else ", ".join(ids)
        lines.append(f"{pad}· {len(ids)} lines: {span}")
        return lines
    bb = tuple(round(v) for v in root.bbox)
    gap = f", gap {root.gap:.0f}pt" if root.gap else ""
    lines.append(f"{pad}{root.kind.upper()} {root.label} bbox={bb}{gap}")
    for c in root.children:
        lines += describe(c, indent + 1, texts)
    return lines


def compare_order(actual, root):
    """
    actual: line ids in tag order. Returns dict with
      expected: ids in layout order
      revisits: regions entered again after having been left
      inversions: consecutive pairs that go backwards inside a region
      path: [(id, region)] in actual order
    """
    expected = root.ordered_ids()
    pos = {i: k for k, i in enumerate(expected)}
    rmap = region_map(root)
    seq = [i for i in actual if i in pos]
    visited, revisits, inversions = [], [], []
    last_region = None
    for a, b in zip([None] + seq, seq):
        rb = rmap.get(b)
        if rb != last_region:
            if rb in visited:
                revisits.append((b, rb))
            visited.append(rb)
            last_region = rb
        if a is not None and rmap.get(a) == rb and pos[b] < pos[a]:
            inversions.append((a, b, rb))
    region_seq = []
    for i in seq:
        r = rmap.get(i)
        if not region_seq or region_seq[-1] != r:
            region_seq.append(r)
    exp_regions = []
    for i in expected:
        r = rmap.get(i)
        if not exp_regions or exp_regions[-1] != r:
            exp_regions.append(r)
    # backward jumps between regions: entering a region that the layout places before the one we leave
    reg_rank = {r: k for k, r in enumerate(exp_regions)}
    region_jumps = []
    for a, b in zip(seq, seq[1:]):
        ra, rb = rmap.get(a), rmap.get(b)
        if ra != rb and reg_rank.get(rb, 0) < reg_rank.get(ra, 0):
            region_jumps.append((a, b, ra, rb))
    ok = not revisits and not inversions and not region_jumps and region_seq == exp_regions
    return {"ok": ok, "expected": expected, "revisits": revisits, "inversions": inversions,
            "region_jumps": region_jumps,
            "region_sequence": region_seq, "expected_region_sequence": exp_regions,
            "path": [(i, rmap.get(i)) for i in seq]}
