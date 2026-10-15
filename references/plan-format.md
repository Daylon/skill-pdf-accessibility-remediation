# Plan format

A plan is one JSON file. `build_tagged.py` reads it, `draft_plan.py` writes a first version, and `plan_outline.py` prints it as an outline. Keys that start with `_` (`_review`, `_note`, `_about`) are comments. The builder ignores them.

```json
{
 "metadata": {...},
 "options": {...},
 "artifacts": {"lines": [...]},
 "unicode_overrides": {...},
 "structure": [{"tag": "Document", "children": [...]}]
}
```

## Contents
- [Unit ids](#unit-ids)
- [metadata](#metadata)
- [options](#options)
- [artifacts](#artifacts)
- [unicode_overrides](#unicode_overrides)
- [structure: nodes](#structure-nodes)
- [Regions](#regions)
- [Lists](#lists)
- [Links, form fields, other annotations](#links-form-fields-other-annotations)
- [Tables](#tables)
- [Figures and formulas](#figures-and-formulas)
- [What the builder refuses](#what-the-builder-refuses)

## Unit ids

These ids come from `units.json` and are stable between inspect, draft, build and validate, because all four open the file through `prepare.load()`.

| id | what |
|---|---|
| `p1.L12` | text line 12 on page 1, in content-stream order |
| `p1.L9..14` or `p1.L9..L14` | range shorthand for L9 to L14 |
| `p2.G3` | graphic 3 on page 2: a path group, an image, a form XObject or a shading |
| `p1.A2` | annotation 2 on page 1 (Link, Widget, …) |

Each line and each listed graphic becomes one marked-content sequence (MCID). Anything that paints but is not listed in `structure` becomes an `/Artifact`. A graphic needs no declaration for that, but a text line does (see `artifacts`).

## metadata

```json
"metadata": {
  "title": "Annual accessibility report 2025 — Northwind Public Services",
  "lang": "en-US",
  "author": "Northwind Public Services",
  "subject": "One-sentence description of the document.",
  "keywords": ["accessibility", "annual report"]
}
```

- `title` is **required**. It is written to Info `/Title` and XMP `dc:title`, and `DisplayDocTitle` is set so viewers show it. Make it descriptive. A value starting with `TODO` is refused.
- `lang` is a BCP 47 tag for the catalog `/Lang` and XMP `dc:language`. If it is missing, the inferred language is used with its default region (`fr`→`fr-FR`, `en`→`en-US`, see `lang_detect.DEFAULT_VARIANT`), and the builder warns. If the text cannot be inferred, the plan is refused.
- `author`, `subject` and `keywords` are optional (Info and XMP). Leave out what you do not know rather than inventing it.

The XMP packet is rebuilt as one clean `rdf:Description`. It carries over `xmp:CreateDate`, `xmpMM:DocumentID`, `xmpRights` and any PDF/A identification from the source, and it adds `pdfuaid:part 1` when PDF/UA is claimed.

## options

| key | values | default | effect |
|---|---|---|---|
| `reorder_text` | `"auto"`, `true`, `false` | `"auto"` | Moves each tagged text line to the end of the content stream in tag order, so content order = tag order (what copy/paste and tag-blind tools read). Each moved line is replayed with the exact CTM, clipping path, ExtGState, colour and text state it was painted with. Lines a later shape paints over, stroked text and clipping text stay in place. Pages that render differently after the move are rebuilt without it. `false` never moves anything. |
| `pdfua` | `"auto"`, `true`, `false` | `"auto"` | Writes the PDF/UA-1 identifier. `"auto"` claims it only when no known blocker exists (non-embedded font, glyph without Unicode, unsupported encoding). `true` forces the claim and is rarely right. |
| `bookmarks` | `"auto"`, `"always"`, `false` | `"auto"` | `"auto"` keeps existing outlines and generates them from H1–H6 when the document has 20 pages or more and none exist. `"always"` generates them and replaces existing ones. `false` leaves outlines as they are. |
| `rtl` | `true`, `false` | `false` | Right-to-left reading for the order check. |
| `verify` | `true`, `false` | `true` | Renders source and output at 72 dpi and compares them. Leave it on. |
| `producer`, `creator_tool` | string | from source | Override the Info `/Producer` and XMP `xmp:CreatorTool`. |

## artifacts

```json
"artifacts": {"lines": ["p1.L1", "p1.L54..55", "p2.L1"]}
```

These are text lines that carry no content: running headers and footers, page numbers, watermarks, decorative letters. They are painted inside `/Artifact` and stay invisible to assistive technology. Graphics need no list here, because a graphic not placed in `structure` is an artifact.

## unicode_overrides

```json
"unicode_overrides": {"p1:/F3": {"136": "•", "15": "–"}}
```

This maps a character code (decimal) of a font resource on a page to the Unicode text it shows. The builder rewrites the font's ToUnicode CMap. Use it when inspect reports "code N has no Unicode". Check every guess against the render. The draft proposes overrides for glyphs that sit at the start of a list line.

## structure: nodes

`structure` is a list of nodes, normally one `Document`. A node is a JSON object:

| key | meaning |
|---|---|
| `tag` | a standard structure type: `Document Part Art Sect Div BlockQuote Caption TOC TOCI Index NonStruct P H1…H6 L LI Lbl LBody Table TR TH TD THead TBody TFoot Span Quote Note Reference BibEntry Code Link Annot Figure Formula Form`. It can be omitted when another key implies it: `region` → Div, `link`/`annot` → Link, `rows` → Table. |
| `lines` | line ids placed in this element, in reading order |
| `graphics` | graphic ids placed in this element (Figure, Formula, decorative-but-meaningful marks) |
| `content` | an **ordered** mix of line ids, graphic ids and nested nodes. Use it when inline children sit between lines, such as a `Link` in the middle of a paragraph. |
| `children` | nested nodes, placed after `content`/`lines`/`graphics` |
| `lang` | `/Lang` for this element (language of parts) |
| `alt` | `/Alt` (required on Figure and Formula) |
| `actual_text` | `/ActualText`: what the element says when the glyphs don't (ligatures, decorative letters, a split word) |
| `expansion` | `/E`: the expansion of an abbreviation |
| `title` | `/T`: a title for the element (shown by some tools) |
| `id` | `/ID` |

The order of the plan is the reading order. The builder assigns MCIDs by walking the tree in document order.

```json
{"tag": "P", "content": ["p1.L3", "p1.L4", {"tag": "Link", "annot": "p1.A1", "lines": ["p1.L5"], "contents": "Methodology (example.org)"}, "p1.L6"]}
```

## Regions

```json
{"region": "R2", "label": "Row 2 · body", "children": [
  {"region": "R2·C1", "label": "Row 2 · column 1 (experience)", "children": [ ... ]},
  {"region": "R2·C2", "label": "Row 2 · column 2 (education)", "children": [ ... ]}
]}
```

A region is a `Div` with `/T` = `label`. Use one per layout row and one per column inside a row, matching the labels the inspector prints (`R1`, `R1·C2`, …). Region labels are for people reviewing the tree, so make them short and descriptive. They are not announced as headings.

## Lists

```json
{"tag": "L", "numbering": "Disc", "items": [
  {"lines": ["p1.L13"]},
  {"label": ["p1.L14"], "lines": ["p1.L15..16"]},
  {"content": ["p1.L17", {"tag": "Link", "annot": "p1.A3", "lines": ["p1.L18"]}]},
  {"lines": ["p1.L19"], "children": [{"tag": "L", "numbering": "Circle", "items": [ ... ]}]},
  {"lines": ["p1.L20"], "lang": "en-US"}
]}
```

- `numbering` is one of `None` (no visible marker, as in a contact block), `Disc`, `Circle`, `Square`, `Decimal`, `UpperRoman`, `LowerRoman`, `UpperAlpha` or `LowerAlpha`. It is written as the list attribute `ListNumbering`.
- Each item becomes `LI` > (`Lbl`) + `LBody`. `label` holds the line ids of a marker that is text, when that marker sits on its own line. A marker drawn as a shape is a graphic and stays an artifact.
- Item keys: `lines`, `content`, `children` (nested lists go here), `label`, `lang`, and `link`/`annot`/`contents` to make the whole item body a link.

## Links, form fields, other annotations

**New link** for visible text with no annotation yet. The annotation is added over the lines' box, so nothing visible changes:
```json
{"link": {"uri": "mailto:camille@example.org", "contents": "Envoyer un e-mail à camille@example.org"}, "lines": ["p1.L3"]}
```

**Existing link annotation**: `annot` is its id from `units.json`, which also lists the text under it:
```json
{"tag": "Link", "annot": "p1.A1", "lines": ["p1.L6"], "contents": "Methodology (example.org)"}
```

- `contents` becomes the annotation's `/Contents`, which is what a screen reader announces as the link's purpose (PDF/UA 7.18.5, WCAG 2.4.4). Write it in the document's language and say where the link goes or what it does. If it is omitted, the existing `/Contents`, then the linked text, then the URI is used.
- A link that wraps across two lines is one `Link` with both lines.

**Form field** (Widget annotation):
```json
{"tag": "Form", "annot": "p1.A4", "label": "Postal code"}
```
`label` becomes the field's `/TU` tooltip, its accessible name.

**Other annotations** (note, stamp, file attachment):
```json
{"tag": "Annot", "annot": "p2.A1", "contents": "Reviewer note: …"}
```

Every annotation on a page must appear exactly once in the plan, except hidden ones and popups, which the builder skips.

## Tables

```json
{"tag": "Table", "header_rows": 1, "header_cols": 0,
 "children": [{"tag": "Caption", "lines": ["p1.L44"]}],
 "rows": [
   ["p1.L45", "p1.L46", "p1.L47"],
   [["p1.L48"], ["p1.L49", "p1.L50"], {"lines": ["p1.L51"], "colspan": 1}],
   [{"lines": ["p2.L2"], "header": true}, "p2.L3", {"content": [], "colspan": 1}]
 ]}
```

- A cell is a line id, a list of ids, or an object `{lines, content, colspan, rowspan, header}`. An empty object (`{"content": []}`) gives an empty cell.
- Cells in the first `header_rows` rows become `TH` with Scope Column. Cells in the first `header_cols` columns become `TH` with Scope Row. `"header": true/false` overrides this per cell.
- A table that continues across pages stays one `Table`. Its rows simply reference lines on both pages, and any repeated header row on the next page goes into `artifacts`.
- `children` (captions) come before the rows.

## Figures and formulas

```json
{"tag": "Figure", "graphics": ["p2.G7"], "alt": "Bar chart of accessible PDFs per year: 12 in 2022, 19 in 2023, 26 in 2024, 31 in 2025.",
 "children": [{"tag": "Caption", "lines": ["p2.L5"]}]}
{"tag": "Formula", "lines": ["p3.L20..22"], "alt": "E equals m c squared"}
```

- `alt` is required. Graphics in a figure are the image, form XObject or path groups that make up the picture (`units.json` → `vector_figures` groups nearby paths). Text drawn inside a chart (axis labels, values) can be placed in the Figure's `lines` so it is not read twice. The alt text then has to carry it.
- A decorative graphic is not placed anywhere, which makes it an artifact.

## What the builder refuses

The builder exits with `PLAN ERRORS` when:
- the title is missing or TODO, or the language is missing and cannot be inferred
- a tag is not standard
- a TODO is left in alt, actual_text or a form label, or a Figure/Formula has no alt
- an id is unknown or used twice, or a text line is neither placed nor listed as an artifact
- an annotation is unplaced, placed twice or unknown
- a heading level is skipped (H2 → H4)

It warns, but still builds, when:
- the first heading is not H1
- the plan order departs from the detected layout (look at the overlay: sometimes the plan is right, for example a sidebar read last on purpose)
- `/Lang` disagrees with the text
- a line hidden under an opaque shape is tagged
- the PDF/UA claim is withheld
- the source claims PDF/A (the claim is kept; re-validate it)
- the rendering differs after the safety net
