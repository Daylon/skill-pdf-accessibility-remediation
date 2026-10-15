# Validator checks

`validate_pdfua.py` prints three things. First comes a **checklist** that uses the same five items as common online checkers. Then come **detailed checks** (PASS / FAIL / WARN / MANUAL), each with severity, WCAG 2.2 success criterion, W3C PDF technique and PDF/UA-1 clause (Matterhorn checkpoint family). Last come the **reading order** and the **detected layout**. The exit code is 1 if any check FAILs. WARN and MANUAL never fail the run.

Severity uses the scale of the wcag-accessibility skill and of the online checkers: **Critical** blocks access, **Serious** causes major difficulty, **Moderate** is an obstacle, and **Minor** is an inconvenience. **Process** marks a safety check of this toolkit, not an accessibility criterion.

## Checklist (summary)

| Item | Severity | Built from |
|---|---|---|
| Document Title | Critical | Document title, XMP title, Display document title |
| Document Language | Critical | Document language, Language matches content |
| Tagged PDF | Critical | Tagged PDF |
| Structure Tree | Critical | Structure tree, ParentTree ↔ MCID consistency, Reading order vs layout |
| Bookmarks | Minor | Bookmarks (required from 20 pages) |

## Detailed checks

| Check | Severity | WCAG | Technique | PDF/UA-1 | What fails it | Fixed by |
|---|---|---|---|---|---|---|
| Document title | Critical | 2.4.2 | PDF18 | 7.1 | no Info /Title, or a file name used as the title | `metadata.title` |
| XMP title | Moderate | 2.4.2 | PDF18 | 7.1-8 | dc:title missing or different | builder (automatic) |
| Display document title | Moderate | 2.4.2 | PDF18 | 7.1-10 | ViewerPreferences/DisplayDocTitle not true | builder |
| Document language | Critical | 3.1.1 | PDF16 | 7.2 | no catalog /Lang, or not BCP 47 | `metadata.lang` |
| Language matches content | Critical | 3.1.1 | PDF16 | — | /Lang says one language, the text reads as another | `metadata.lang` |
| Language of parts | Minor | 3.1.2 | PDF19 | 7.2 | invalid /Lang on an element (FAIL); blocks that read as another language without /Lang (WARN, so check for names and jargon) | node `lang` |
| XMP language | Minor | 3.1.1 | PDF16 | — | dc:language differs from /Lang | builder |
| Tagged PDF | Critical | 1.3.1 | — | 7.1-11 | MarkInfo /Marked not true, or /Suspects true | builder |
| Structure tree | Critical | 1.3.1, 1.3.2 | PDF3, PDF9 | 7.1 | no StructTreeRoot | builder |
| ParentTree ↔ MCID consistency | Critical | 1.3.1, 4.1.2 | — | 7.1 | an MCID with no owner, or an owner pointing to the wrong MCID or page | builder |
| Reading order vs layout | Critical | 1.3.2 | PDF3 | 7.2 | tag order jumps between detected regions, interleaves columns, revisits a region or goes back up inside one. For an untagged file, the same test is applied to the paint order. | plan order |
| Layout regions in tags | Moderate | 1.3.1, 1.3.2 | PDF3 | — | a detected column is split across subtrees or mixed with another column (WARN) | region `Div`s |
| Standard structure types | Serious | 1.3.1 | — | 7.1 | a non-standard tag without a RoleMap entry | plan `tag` |
| Headings | Moderate | 1.3.1, 2.4.6 | PDF9 | 7.4 | H mixed with H1–H6, first heading not H1, or a skipped level (FAIL); no headings (WARN) | plan |
| Lists | Moderate | 1.3.1 | PDF21 | 7.6 | L without LI, LI outside L, or LI without Lbl/LBody | `items` |
| Figures | Critical | 1.1.1 | PDF1, PDF4 | 7.3 | Figure or Formula without /Alt, or alt that is a file name or placeholder | `alt` |
| Tables | Serious | 1.3.1 | PDF6 | 7.5 | TH without Scope (WARN), or a malformed TR/TH/TD hierarchy | `rows`, `header_rows`, `header_cols` |
| Alternate / actual text encoding | Serious | 1.1.1, 4.1.2 | — | 7.3 | /Alt or /ActualText not a text string | builder |
| Content is tagged or artifact | Serious | 1.3.1 | PDF4 | 7.1-3 | painted content outside both a tagged sequence and an /Artifact | builder (every line placed or declared) |
| Marked-content nesting | Serious | 1.3.1 | — | 7.1 | tagged content inside an artifact, or nested MCIDs | builder |
| Hidden text | Serious | 1.3.1 | — | — | a tagged line is covered by an opaque shape (WARN) | `artifacts.lines`, unless intended |
| Fonts & Unicode mapping | Critical | 1.1.1, 1.3.1 | — | 7.21 | glyphs with no Unicode mapping, or an undecodable encoding | `unicode_overrides` |
| Font embedding | Minor | — | — | 7.21.4 | a font is not embedded (PDF/UA only, WCAG unaffected) | the source; the claim is withheld |
| Links | Serious | 2.1.1, 2.4.4, 4.1.2 | PDF11, PDF13 | 7.18 | a Link annotation without a Link structure element or OBJR, without /Contents, or without /StructParent; a page without /Tabs /S | `Link` nodes |
| Form fields | Critical | 1.3.1, 4.1.2, 3.3.2 | PDF10, PDF12 | 7.18.4 | a Widget without a Form element, or without TU or a label | `Form` nodes |
| Text contrast (rendered) | Serious | 1.4.3 | — | — | a text line below 4.5:1 (3:1 for 18pt and up, or 14pt bold and up), measured on the render: background = dominant colour of the line's box, foreground = the most contrasting tenth of the glyph pixels | **design issue: report it, don't fix it** |
| Content order = tag order | Minor | 1.3.2 | PDF3 | — | content-stream order differs from tag order (WARN). Some old readers follow the stream. | `reorder_text` |
| PDF/UA identifier | Minor | — | — | 5 | pdfuaid:part in a malformed rdf:Description (FAIL); a claim despite blockers (FAIL); no claim (WARN) | `pdfua` |
| Bookmarks | Minor | 2.4.5 | PDF2 | — | 20 pages or more without outlines | `bookmarks` |
| Catalog hygiene | Minor | — | — | — | a stray /Info key in the catalog | builder |
| Encryption | Serious | — | — | 7.16 | encrypted without the accessibility permission | owner decision |
| Visual identity | Process | — | — | — | with `--compare`: any page whose 110 dpi render differs from the source | **must PASS before delivery** |
| Screen reader pass | MANUAL | 1.3.1, 2.4.4 | PDF9, PDF11, PDF21 | — | always listed | a person with NVDA + Acrobat Reader or VoiceOver |
| veraPDF PDF/UA-1 + PAC | MANUAL | — | — | all | always listed | run veraPDF (PDF/UA-1 profile) and PAC 2024 |

### Not covered (say so when relevant)

The validator does not check these, so they need a person:
- 1.4.1 (colour alone)
- 1.4.5 (images of text): it is flagged during review, not tested
- 1.4.11 (non-text contrast of icons and chart marks)
- 2.4.4 (whether link text actually makes sense): only checked for presence
- 3.3.x for form behaviour
- whether alt text is *meaningful*
- the full Matterhorn set (about 130 machine checks; veraPDF does them)

## Reading the overlay

`--overlay out.png` (one image per page, suffixed `-pN` on multi-page files) draws:
- **Dashed frames**: the detected layout regions. Rows have their tab on the top-left (`R1`, `R2` …) and columns on the top-right (`R1·C1` …).
- **Numbered badges**: each tagged block in tag order (`3 H2`, `4 P` …), coloured by role. A red badge is a block reached out of layout order.
- **Path**: grey arrows stay within a region, blue arrows move to the next region in layout order, and red arrows jump out of order.
- A legend at the bottom.

A good overlay shows each region's badges numbered consecutively, blue arrows only between regions in reading sequence, and no red. On an untagged source, the badges read `text` and follow the **paint order**, which is what tag-blind tools and many screen-reader fallbacks read. Compare `before.png` (the source's own tags, or its paint order) with `after.png` when explaining the change to the user.

`--text order.txt` writes the same order as plain text, one block per line with its role. Reading it top to bottom is the fastest way to hear the document the way a screen reader will.

## Second opinions

- `pdfjs_a11y_tree.mjs`: the tree, language, alt and link names that pdf.js (Firefox) exposes. It is useful to confirm that a real consumer parses the tree.
- `pdftotext -layout` against the reading-order text: a quick check that no text was lost.
- When reachable, veraPDF (`verapdf --flavour ua1 file.pdf`) and PAC 2024 (Windows) are the reference validators. This environment may block their downloads. In that case, list them under manual checks.
