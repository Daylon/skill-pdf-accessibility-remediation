#!/usr/bin/env python3
"""
draft_plan.py — turn an inventory (units.json from inspect_pdf.py) into a first
structure plan (plan.json) that the builder accepts.

Usage:
    python3 draft_plan.py units.json plan.json [--title "…"] [--lang fr-FR]

The draft is a starting point, never the answer. It is deliberately explicit
about what it guessed: every guess that needs a human / model check is listed
in plan["_review"] and printed. The usual edits after drafting:
  * heading levels by MEANING (size tiers are only a proxy),
  * alt text for figures (left as "TODO: …" — the builder refuses TODOs),
  * merging / splitting blocks the spacing heuristics got wrong,
  * language of parts, link descriptions, the document title.

Heuristics (in priority order)
  1. Existing tags of the source (same element → same block, its role wins
     when it is meaningful: H1–H6, LI, TD/TH, Caption, Figure alt …).
  2. Layout regions (rows split into columns → a Div per row and per column).
  3. Running headers / footers / page numbers → artifacts.
  4. Tables (ruled or aligned) → Table with a header row.
  5. Bullets (typed or drawn) → lists; continuation lines by indentation.
  6. Type tiers → headings (bigger / bolder than body, short, standalone).
  7. Everything else → paragraphs split on style changes and extra spacing.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
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

HEADING_ROLES = {"H1", "H2", "H3", "H4", "H5", "H6", "H", "Title"}
LINK_WORDS = {
    "fr": {"mailto": "Envoyer un e-mail à {t}", "tel": "Appeler le {t}", "web": "Ouvrir {t}", "fedi": "Profil {t}"},
    "en": {"mailto": "Email {t}", "tel": "Call {t}", "web": "Open {t}", "fedi": "Profile {t}"},
    "de": {"mailto": "E-Mail an {t}", "tel": "{t} anrufen", "web": "{t} öffnen", "fedi": "Profil {t}"},
    "es": {"mailto": "Enviar un correo a {t}", "tel": "Llamar al {t}", "web": "Abrir {t}", "fedi": "Perfil {t}"},
}
CAPTION_RE = re.compile(r"^\s*(figure|fig\.|table|tableau|abbildung|tabelle|figura|tabla|chart|graph|image)\s*\d*", re.I)
NUMBERING = {"text": "Disc", "drawn": "Disc", "number": "Decimal"}


def link_contents(uri, text, lang):
    w = LINK_WORDS.get((lang or "en").split("-")[0], LINK_WORDS["en"])
    t = re.sub(r"\s+", " ", text).strip()
    if uri.startswith("mailto:"):
        return w["mailto"].format(t=uri[7:])
    if uri.startswith("tel:"):
        return w["tel"].format(t=t)
    if re.search(r"/@[\w]+$", uri) and t.startswith("@"):
        return w["fedi"].format(t=t)
    return w["web"].format(t=t)


def numbering_for(line_text, kind):
    if kind != "number":
        ch = line_text.strip()[:1]
        return {"◦": "Circle", "○": "Circle", "▪": "Square", "■": "Square", "□": "Square"}.get(ch, "Disc")
    m = re.match(r"^\s*\(?([0-9]+|[a-z]|[A-Z]|[ivxlc]+|[IVXLC]+)[.)]", line_text)
    if not m:
        return "Decimal"
    v = m.group(1)
    if v.isdigit():
        return "Decimal"
    if re.fullmatch(r"[ivxlc]+", v) and len(v) > 1:
        return "LowerRoman"
    if re.fullmatch(r"[IVXLC]+", v) and len(v) > 1:
        return "UpperRoman"
    return "LowerAlpha" if v.islower() else "UpperAlpha"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("units")
    ap.add_argument("plan")
    ap.add_argument("--title")
    ap.add_argument("--lang")
    args = ap.parse_args()
    inv = json.loads(Path(args.units).read_text(encoding="utf-8"))
    review = []
    stops = [p["message"] for p in inv.get("preflight", []) if p["level"] == "STOP"]
    if stops:
        raise SystemExit("Preflight blocks tagging:\n  " + "\n  ".join(stops))

    lang = args.lang or inv["language"].get("suggested") or inv["language"].get("declared")
    if not lang:
        review.append("Language could not be inferred: set metadata.lang")

    all_lines = {l["id"]: l for p in inv["pages"] for l in p["lines"]}
    for l in all_lines.values():               # role family from the whole ancestor path (TD > P, LI > LBody > P …)
        path = l.get("hint_path") or []
        if any(r in ("TD", "TH") for r in path):
            l["hint_role"] = "TD"
        elif any(r in ("LI", "LBody", "Lbl") for r in path) and l.get("hint_role") not in HEADING_ROLES:
            l["hint_role"] = "LI"
        elif any(r in ("Figure", "Formula") for r in path):
            l["hint_role"] = next(r for r in path if r in ("Figure", "Formula"))
    # existing tags are only trusted when they carry real structure (not the flat P/Span trees many tools emit)
    roles_seen = {l.get("hint_role") for l in all_lines.values() if l.get("hint_role")}
    use_hints = bool(roles_seen & {"H1", "H2", "H3", "H4", "H5", "H6", "H", "L", "LI", "LBody", "Lbl", "Table", "TD", "TH", "Caption"})
    if not use_hints:
        for l in all_lines.values():
            l["hint_role"] = l["hint_eid"] = None
    elif roles_seen:
        review.append(f"existing tags used as hints (roles {sorted(r for r in roles_seen if r)}): still check them")
    content = [l for l in all_lines.values() if l["text"].strip() and not l["artifact"]]
    # body size = the size carrying most characters
    by_size = Counter()
    for l in content:
        by_size[round(l["size"])] += len(l["text"].strip())
    body = by_size.most_common(1)[0][0] if by_size else 10

    def style(l):
        return (round(l["size"], 1), l["bold"], l["italic"], l.get("fg"))

    def beside(a, b):
        """b continues a's text on the same visual line: it starts right where a ends (no real gap) and
        overlaps it vertically — superscripts, footnote marks, a word in another size. A separate item
        placed beside (a note centred next to a title) has a visible gap and stays its own block."""
        ra, rb = a["rbox"], b["rbox"]
        h = min(ra[3] - ra[1], rb[3] - rb[1])
        gap = rb[0] - ra[2]
        return (-1 <= gap <= max(2.0, 0.3 * min(a["size"], b["size"])) and h > 0
                and min(ra[3], rb[3]) - max(ra[1], rb[1]) >= 0.5 * h)

    def color_change(a, b):
        if not a or not b:
            return False
        ca = [int(a[k:k + 2], 16) for k in (1, 3, 5)]
        cb = [int(b[k:k + 2], 16) for k in (1, 3, 5)]
        return sum(abs(x - y) for x, y in zip(ca, cb)) > 24

    # heading tiers: styles bigger than body (or bold at body size) used on short standalone lines
    def heading_like(l):
        t = l["text"].strip()
        letters = len(re.findall(r"[^\W\d_]", t))
        if letters < 2 or len(t.split()) > 16 or t.endswith((".", ",", ";")):
            return False
        if re.search(r"@|https?://|www\.|\+?\d[\d\s().-]{7,}\d", t):
            return False                      # addresses, links, phone numbers are never headings
        return l["size"] >= body * 1.15 or (l["bold"] and l["size"] >= body * 0.98 and len(t.split()) <= 10)
    tiers = sorted({(round(l["size"]), l["bold"]) for l in content if heading_like(l)}, key=lambda k: (-k[0], not k[1]))
    tier_level = {k: min(i + 1, 6) for i, k in enumerate(tiers)}

    structure = []
    artifacts = []
    last_table = [None]          # table at the end of the previous page (rows may continue on the next one)
    for page in inv["pages"]:
        P = page["page"]
        L = {l["id"]: l for l in page["lines"]}
        for l in page["lines"]:
            if l["text"].strip() and l["artifact"]:
                artifacts.append(l["id"])
        order = [i for i in page["layout"]["reading_order"] if i in L and not L[i]["artifact"]]
        if not order:
            continue
        # column path of every line: [(container label, column label), …] outer → inner
        colpath = defaultdict(list)
        for c in sorted(page["layout"]["containers"], key=lambda c: len(c["label"])):
            for col in c["columns"]:
                for i in col["lines"]:
                    colpath[i].append((c["label"], col["label"]))
        # tables: first line → table, others skipped
        table_of = {}
        tables = []
        for t in page["tables"]:
            ids = [i for r in t["rows"] for c in r for i in c]
            if not ids or any(i in table_of for i in ids):
                continue
            tables.append(t)
            for i in ids:
                table_of[i] = t
        # link annotations and candidates
        link_of = {}
        for a in page["annots"]:
            if a["subtype"] == "Link" and a["lines"]:
                for i in a["lines"]:
                    link_of[i] = ("annot", a)
                if a["line_cover"] < 0.6:
                    review.append(f"{a['id']} link covers only part of {a['lines']}: the whole line becomes the link text")
        for c in page["link_candidates"]:
            if c["whole_line"] and c["line"] not in link_of:
                link_of[c["line"]] = ("new", c)
        # figures and positioned annotations (form fields, notes): anchor them in reading order
        floats = []
        for g in page["graphics"]:
            if g["role"] == "figure?":
                alt = g.get("hint_alt")
                floats.append({"kind": "figure", "box": g["dbox"], "graphics": [g["id"]], "lines": [],
                               "alt": alt or f"TODO: describe {g['id']} (see {g.get('crop', 'the page render')}) "
                                                f"or mark it decorative by deleting this node",
                               "existing_alt": bool(alt)})
        for v in page.get("vector_figures", []):
            floats.append({"kind": "figure", "box": v["dbox"], "graphics": v["graphics"], "lines": v["lines_inside"],
                           "alt": f"TODO: describe the chart/diagram drawn with {len(v['graphics'])} paths "
                                  f"(see {v.get('crop', 'the page render')}) or delete this node if decorative",
                           "existing_alt": False})
        for a in page["annots"]:
            if a["hidden"] or a["subtype"] in ("Link", "Popup"):
                continue
            floats.append({"kind": "annot", "box": a["dbox"], "annot": a})
        in_float = {i for f in floats for i in f.get("lines", [])}

        # column boxes (to give floats the right column)
        col_box = {}
        for c in page["layout"]["containers"]:
            for col in c["columns"]:
                bs = [L[i]["dbox"] for i in col["lines"] if i in L]
                if bs:
                    col_box[(c["label"], col["label"])] = (min(b[0] for b in bs), min(b[1] for b in bs),
                                                           max(b[2] for b in bs), max(b[3] for b in bs))

        def float_path(box):
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            best = []
            for c in sorted(page["layout"]["containers"], key=lambda c: len(c["label"])):
                for col in c["columns"]:
                    b = col_box.get((c["label"], col["label"]))
                    if b and b[0] - 8 <= cx <= b[2] + 8 and b[1] - 40 <= cy <= b[3] + 40:
                        best = best + [(c["label"], col["label"])]
            return best

        def anchor(f):
            """index in `order` before which a floating object belongs"""
            box = f["box"]
            if f["kind"] == "annot":                       # a field goes right after its label on the same row
                row = [k for k, i in enumerate(order) if L[i]["dbox"][1] < box[3] and L[i]["dbox"][3] > box[1]
                       and L[i]["dbox"][2] <= box[0] + 2]
                if row:
                    return row[-1] + 1
            fp = f["path"]
            cands = [k for k, i in enumerate(order) if colpath.get(i, []) == fp] or list(range(len(order)))
            for k in cands:
                if L[order[k]]["dbox"][3] <= box[3] + 1:
                    return k
            return cands[-1] + 1 if cands else len(order)
        float_at = defaultdict(list)
        unit_pos = {u: k for k, u in enumerate(page["layout"]["reading_order"])}
        line_pos = [unit_pos.get(i, 0) for i in order]
        for f in floats:
            uid = f["graphics"][0] if f["kind"] == "figure" and len(f["graphics"]) == 1 else (f["annot"]["id"] if f["kind"] == "annot" else None)
            if uid in unit_pos:                    # placed by the layout analysis itself
                f["path"] = colpath.get(uid, [])
                k = next((n for n, lp in enumerate(line_pos) if lp > unit_pos[uid]), len(order))
                float_at[k].append(f)
            else:
                f["path"] = float_path(f["box"])
                float_at[anchor(f)].append(f)

        drawn = [L[i] for i in order if L[i]["bullet"] == "drawn"]
        for l in drawn:
            if not any(o is not l and o.get("region") == l.get("region") and abs(o["dbox"][0] - l["dbox"][0]) <= 3 for o in drawn):
                l["bullet"] = None                 # a single "drawn bullet" is usually a stray mark, not a list
                review.append(f"{l['id']}: ignored an isolated drawn-bullet guess")
        # ---- build blocks in reading order
        items = []            # (colpath, node)
        cur = None            # current block being filled: dict(kind, lines, style, x0, …)

        def flush():
            nonlocal cur
            if cur is not None:
                items.append((cur["path"], cur))
                cur = None
        done_tables = set()
        for k, i in enumerate(order + [None]):
            for f in float_at.get(k, []):
                flush()
                items.append((f["path"], {"float": f}))
            if i is None:
                break
            if i in in_float:
                continue
            l = L[i]
            path = colpath.get(i, [])
            if i in table_of:
                t = table_of[i]
                if id(t) not in done_tables:
                    flush()
                    done_tables.add(id(t))
                    items.append((path, {"table": t}))
                continue
            st = style(l)
            hint_eid = l.get("hint_eid")
            same_line = cur is not None and (abs(cur["last"]["baseline"] - l["baseline"]) <= 1.5 or beside(cur["last"], l))
            pitch = (cur["last"]["baseline"] - l["baseline"]) if cur is not None else 0
            new = cur is None or path != cur["path"]
            if not new and not same_line:
                if l["bullet"] and l.get("hint_role") not in HEADING_ROLES:
                    new = True
                elif hint_eid and cur.get("eid") and hint_eid != cur["eid"]:
                    new = True
                elif cur["kind"] == "li":
                    # continuation lines of an item are indented past its label (typed bullets) or aligned with its
                    # text (drawn bullets); anything else ends the item
                    tol = max(3.0, 0.5 * l["size"])
                    if cur["text_x"] is None:
                        cont = l["dbox"][0] > cur["x0"] + 1.5
                    else:
                        cont = abs(l["dbox"][0] - cur["text_x"]) <= tol
                    new = (not cont) or pitch > 1.75 * max(l["size"], 1) or abs(st[0] - cur["style"][0]) > 0.6
                elif hint_eid and cur.get("eid") == hint_eid:
                    new = False                       # same element in a meaningful source tree
                elif st[:2] != cur["style"][:2] or abs(st[0] - cur["style"][0]) > 0.6:
                    new = True
                elif st[2] != cur["last_style"][2] and re.search(r"[.:!?»”\"]\s*$", cur["last"]["text"]):
                    new = True                        # italic switches after a sentence end: a quote, a note
                elif pitch > 1.75 * max(l["size"], 1):
                    new = True
                elif heading_like(cur["first"]) and cur["n"] == 1 and not heading_like(l):
                    new = True
                elif heading_like(l) != heading_like(cur["first"]) and color_change(st[3], cur["style"][3]):
                    new = True                        # same size, other colour: a different kind of line
            if new:
                flush()
                kind = "li" if (l["bullet"] and l.get("hint_role") not in HEADING_ROLES) else "block"
                text_x = l["dbox"][0]
                cur = {"path": path, "kind": kind, "lines": [i], "style": st, "last_style": st, "first": l, "last": l, "n": 1,
                       "eid": hint_eid, "bullet": l["bullet"], "text_x": text_x, "x0": l["dbox"][0]}
                if l["bullet"] in ("text", "number"):
                    cur["text_x"] = None              # learn it from the first continuation line
            else:
                if cur["kind"] == "li" and cur["text_x"] is None and not same_line:
                    cur["text_x"] = l["dbox"][0]
                cur["lines"].append(i)
                cur["last"] = l
                cur["last_style"] = st
                if not same_line:
                    cur["n"] += 1
        flush()

        # ---- classify blocks and assemble nodes (lists merged, links wrapped)
        nodes = []          # (colpath, node)
        placed_annots = set()
        prev_list = None
        for path, it in items:
            if "float" in it:
                f = it["float"]
                prev_list = None
                if f["kind"] == "figure":
                    node = {"tag": "Figure", "graphics": f["graphics"], "alt": f["alt"]}
                    if f["lines"]:
                        node["lines"] = f["lines"]
                    if not f["existing_alt"]:
                        review.append(f"alt text needed for {', '.join(f['graphics'][:3])}{'…' if len(f['graphics']) > 3 else ''}")
                else:
                    a = f["annot"]
                    if a["subtype"] == "Widget":
                        node = {"tag": "Form", "annot": a["id"]}
                        if not a["tooltip"]:
                            node["label"] = f"TODO: accessible name for field {a['field']}"
                            review.append(f"form field {a['field']} ({a['id']}) has no tooltip: set node.label")
                    else:
                        node = {"tag": "Annot", "annot": a["id"]}
                nodes.append((path, node))
                continue
            if "table" in it:
                prev_list = None
                t = it["table"]
                node = {"tag": "Table", "header_rows": 1, "rows": [[c for c in r] for r in t["rows"]]}
                review.append(f"table at {t['box']} ({t['kind']}): confirm header row/column and cell boundaries")
                nodes.append((path, node))
                continue
            b = it
            first = b["first"]
            fam = {"Lbl": "LI", "LBody": "LI", "LI": "LI", "L": "LI", "TD": "TD", "TH": "TD", "TR": "TD"}
            hint_roles = {fam.get(L[i].get("hint_role"), L[i].get("hint_role")) for i in b["lines"]} - {None, "Span", "Link"}
            hint = next(iter(hint_roles)) if len(hint_roles) == 1 else \
                next((r for r in ("H1", "H2", "H3", "H4", "H5", "H6", "LI") if r in hint_roles), None)
            text = " ".join(L[i]["text"].strip() for i in b["lines"])
            content_items = []
            for i in b["lines"]:
                if i in link_of and not (link_of[i][0] == "annot" and link_of[i][1]["id"] in placed_annots
                                         and not (content_items and isinstance(content_items[-1], dict)
                                                  and content_items[-1].get("annot") == link_of[i][1]["id"])):
                    kind_, obj = link_of[i]
                    if kind_ == "annot":
                        placed_annots.add(obj["id"])
                        if content_items and isinstance(content_items[-1], dict) and content_items[-1].get("annot") == obj["id"]:
                            content_items[-1]["lines"].append(i)
                        else:
                            vis = obj.get("text") or ""
                            if not vis:
                                host = re.sub(r"^(https?://|mailto:|tel:)", "", obj["uri"] or "").rstrip("/")
                                contents = link_contents(obj["uri"] or "", host, lang)
                                review.append(f"{obj['id']}: link text could not be isolated (no QuadPoints) — "
                                              f"contents set to {contents!r}; the Link element wraps whole lines")
                            elif obj.get("line_cover", 1) < 0.95:
                                contents = vis
                            else:
                                contents = link_contents(obj["uri"] or "", vis, lang)
                            content_items.append({"tag": "Link", "annot": obj["id"], "lines": [i],
                                                  "contents": obj.get("contents") or contents})
                    else:
                        content_items.append({"tag": "Link", "link": {"uri": obj["uri"],
                                              "contents": link_contents(obj["uri"], obj["text"], lang)}, "lines": [i]})
                        review.append(f"added a link on {i} → {obj['uri']} (visible text looked like a link)")
                else:
                    content_items.append(i)
            # a row of a table that continues from the previous page / block
            tprev = nodes[-1][1] if nodes and nodes[-1][1].get("tag") == "Table" and nodes[-1][0] == path else \
                (last_table[0] if not nodes and last_table[0] is not None else None)
            if hint == "TD" and tprev is not None:
                tnode = tprev
                xs = tnode.setdefault("_colx", [min(all_lines[i]["dbox"][0] for i in c) for c in tnode["rows"][0] if c])
                row = [[] for _ in xs]
                for i in b["lines"]:
                    k = min(range(len(xs)), key=lambda j: abs(xs[j] - L[i]["dbox"][0]))
                    row[k].append(i)
                tnode["rows"].append(row)
                continue
            if b["kind"] == "li" or hint == "LI":
                num = numbering_for(first["text"], b["bullet"]) if b["bullet"] else "None"
                li = {"content": content_items}
                if prev_list is not None and prev_list[0] == path and prev_list[1]["numbering"] in (num, "None") \
                        and abs(prev_list[2] - b["x0"]) <= 4:
                    prev_list[1]["items"].append(li)
                    continue
                node = {"tag": "L", "numbering": num, "items": [li]}
                nodes.append((path, node))
                prev_list = (path, node, b["x0"])
                continue
            prev_list = None
            if hint in HEADING_ROLES and hint not in ("Title", "H"):
                tag = "H" + str(int(hint[1])) if hint != "H" else "H2"
            elif hint in ("Caption",) or (CAPTION_RE.match(text) and nodes and nodes[-1][1].get("tag") in ("Figure", "Table")):
                tag = "Caption"
            elif hint in ("Formula", "Code", "BlockQuote", "Quote", "Note"):
                tag = hint
            elif b["n"] <= 3 and all(heading_like(L[i]) for i in b["lines"]):
                tag = f"H{tier_level.get((round(first['size']), first['bold']), 6)}"
            else:
                tag = "P"
            node = {"tag": tag, "content": content_items}
            # language of parts: whole block confidently in another language
            fl = [c for c in inv["language"].get("foreign_candidates", []) if c["id"] in b["lines"]]
            if fl:
                # judge the whole block, not line by line: short wrapped lines are unreliable on their own
                btext = " ".join(L[i]["text"].strip() for i in b["lines"])
                doc_base = (lang or "").split("-")[0].lower()
                sc = lang_detect.scores(btext)
                if sc and sc[0][0] != doc_base and sc[0][0] not in lang_detect.IGNORED_PARTS and sc[0][1] >= 0.8 and \
                        sc[0][1] - (sc[1][1] if len(sc) > 1 else 0) >= 0.3:
                    node["lang"] = lang_detect.with_variant(sc[0][0])
                    review.append(f"tagged {b['lines'][0]} as lang={node['lang']}: confirm it is a phrase in that "
                                  f"language, not a proper name or jargon")
                else:
                    review.append(f"{b['lines'][0]}: parts may be in {fl[0]['lang']} — if a whole phrase is, give it "
                                  f"its own node with lang")
            if tag == "Caption" and nodes and nodes[-1][1].get("tag") in ("Figure", "Table"):
                nodes[-1][1].setdefault("children", []).append(node)
                continue
            nodes.append((path, node))

        last_table[0] = nodes[-1][1] if nodes and nodes[-1][1].get("tag") == "Table" else None
        # single-item numbered "lists" are usually initials or numbered headings: keep them as text
        for k, (path_, node) in enumerate(nodes):
            if node.get("tag") == "L" and len(node["items"]) == 1 and node["numbering"] not in ("Disc", "Circle", "Square", "None"):
                nodes[k] = (path_, {"tag": "P", "content": node["items"][0]["content"]})
        # ---- nest into region Divs following the column paths
        root_children = []
        stack = [([], root_children)]          # (path prefix, children list)
        for path, node in nodes:
            # pop until the stack prefix is a prefix of path
            while len(stack) > 1 and stack[-1][0] != path[:len(stack[-1][0])]:
                stack.pop()
            # open containers/columns missing for this path
            while len(stack[-1][0]) < len(path):
                depth = len(stack[-1][0])
                cont, col = path[depth]
                parent_children = stack[-1][1]
                # reuse the open container Div if the last child is it
                if parent_children and parent_children[-1].get("region") == f"{cont}" and depth == len(stack[-1][0]):
                    row_div = parent_children[-1]
                else:
                    row_div = {"region": cont, "label": f"p{P} " + (f"row {cont}" if cont != "page" else "columns"), "children": []}
                    parent_children.append(row_div)
                col_div = {"region": col, "label": f"p{P} column {col}", "children": []}
                row_div["children"].append(col_div)
                stack.append((path[:depth + 1], col_div["children"]))
            stack[-1][1].append(node)
        structure += root_children

    # merge lists that continue across a page break into one list (same numbering)
    merged = []
    for node in structure:
        if merged and node.get("tag") == "L" and merged[-1].get("tag") == "L" and node["numbering"] == merged[-1]["numbering"]:
            merged[-1]["items"] += node["items"]
        else:
            merged.append(node)
    structure = merged

    # heading normalisation: first heading H1, no skipped levels
    heads = []

    def walk(ns):
        for n in ns:
            if isinstance(n, dict):
                if re.fullmatch(r"H[1-6]", n.get("tag", "")):
                    heads.append(n)
                walk(n.get("children", []))
                for it in n.get("items", []):
                    walk(it.get("children", []))
    walk(structure)
    if heads:
        used = sorted({int(h["tag"][1]) for h in heads})
        remap = {lv: i + 1 for i, lv in enumerate(used)}
        prev = 0
        for h in heads:
            lv = remap[int(h["tag"][1])]
            if lv > prev + 1:
                lv = prev + 1
            h["tag"] = f"H{lv}"
            prev = lv
        tier_names = [f"{s}pt" + (" bold" if b else "") for s, b in tiers]
        review.append(f"heading levels come from type size tiers {tier_names}: "
                      f"re-level them by meaning (H1 = document subject, H2 = sections …)")

    facts = inv["facts"]
    title = args.title
    if not title:
        it = facts.get("info_title") or ""
        if it and not re.search(r"\.(pdf|docx?|indd|fig)$|^[\w-]+--?\d+$|^untitled|^document\d*$|^\s*$", it, re.I) \
                and len(it) > 3 and len(it.split()) >= 2:          # one bare word is a file or template name
            title = it
        elif heads:
            h1 = heads[0]
            ids = [c for c in h1.get("content", []) if isinstance(c, str)]
            title = " ".join(all_lines[i]["text"].strip() for i in ids if i in all_lines) or None
        review.append(f"title {title!r}: make it descriptive (what the document is, for whom/when)")
    if title:
        title = re.sub(r"\s+", " ", title).strip()

    def strip_private(n):
        if isinstance(n, dict):
            for k in [k for k in n if k.startswith("_")]:
                del n[k]
            for v in n.values():
                if isinstance(v, list):
                    for x in v:
                        strip_private(x)
    for n in structure:
        strip_private(n)
    plan = {
        "metadata": {"title": title or "TODO: document title", "lang": lang, "author": None, "subject": None, "keywords": []},
        "options": {"reorder_text": "auto", "pdfua": "auto", "bookmarks": "auto"},
        "artifacts": {"lines": artifacts},
        "structure": [{"tag": "Document", "children": structure}],
        "_review": review,
    }
    sugg = inv.get("unicode_suggestions") or {}
    if sugg:
        plan["unicode_overrides"] = sugg
        review.append(f"glyphs without Unicode mapped by guess {sugg}: check them against the render")
    for p in inv.get("preflight", []):
        if "has no Unicode" in p["message"] and not sugg:
            review.append(p["message"] + " → plan.unicode_overrides {\"pN:/Font\": {\"code\": \"char\"}}")
    Path(args.plan).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    n_nodes = sum(1 for _ in json.dumps(structure).split('"tag"')) - 1
    print(f"draft written to {args.plan}: ~{n_nodes} elements, {len(artifacts)} artifact lines, lang={lang}, title={title!r}")
    print("Review before building:")
    for r in review:
        print("  -", r)


if __name__ == "__main__":
    main()
