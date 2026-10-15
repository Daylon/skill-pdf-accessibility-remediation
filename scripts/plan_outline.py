#!/usr/bin/env python3
"""plan_outline.py — print a plan as an indented outline with the text of each node (for reviewing drafts).
Usage: python3 plan_outline.py plan.json units.json"""
import json, re, sys
from pathlib import Path

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
plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
inv = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
L = {l["id"]: l for p in inv["pages"] for l in p["lines"]}
RANGE = re.compile(r"^(p\d+)\.L(\d+)\.\.(?:L)?(\d+)$")

def ids(lst):
    out = []
    for s in lst:
        m = RANGE.match(s) if isinstance(s, str) else None
        if m:
            out += [f"{m.group(1)}.L{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        elif isinstance(s, str):
            out.append(s)
    return out

def text(n):
    parts = []
    for c in n.get("content", []):
        parts.append(" ".join(L[i]["text"].strip() for i in ids([c])) if isinstance(c, str) else text(c))
    parts += [L[i]["text"].strip() for i in ids(n.get("lines", []))]
    return re.sub(r"\s+", " ", " ".join(parts))

def show(n, d=0):
    tag = n.get("tag") or ("Div" if "region" in n else "Link" if "link" in n else "?")
    extra = []
    for k in ("region", "lang", "annot", "numbering"):
        if k in n:
            extra.append(f"{k}={n[k]}")
    if "alt" in n:
        extra.append(f"alt={n['alt'][:50]!r}")
    if "link" in n:
        extra.append(f"uri={n['link']['uri']}")
    t = text(n)
    print(f"{'  ' * d}{tag}{(' [' + ', '.join(extra) + ']') if extra else ''}{(' — ' + t[:90]) if t else ''}")
    if tag == "Table":
        for r in n.get("rows", []):
            print(f"{'  ' * (d + 1)}| " + " | ".join(" ".join(L[i]["text"].strip() for i in (c if isinstance(c, list) else [c]))[:22] for c in r))
    for it in n.get("items", []):
        print(f"{'  ' * (d + 1)}LI — {text(it)[:90]}")
    for c in n.get("children", []):
        show(c, d + 1)
    for c in n.get("content", []):
        if isinstance(c, dict) and c.get("children"):
            show(c, d + 1)

m = plan["metadata"]
print(f"title={m['title']!r} lang={m['lang']} artifacts={len(plan.get('artifacts', {}).get('lines', []))}")
for n in plan["structure"]:
    show(n)
