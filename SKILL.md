---
name: skill-pdf-accessibility-remediation
description: Make an existing PDF accessible (WCAG 2.2 and PDF/UA-1) without changing how it looks. Rebuilds the tag tree from the rendered layout (reading order across rows and columns, headings H1–H6, lists, tables, figures with alt text, links), sets title, language and metadata, and checks that every page stays pixel-identical. Use it whenever someone wants to tag, remediate, fix or audit the accessibility of a PDF (CVs, reports, brochures, forms, exports from Figma, InDesign, Word, LibreOffice, LaTeX or a browser), or mentions screen readers, reading order, untagged PDFs, PAC, veraPDF, the Acrobat checker or an accessibility checker's report on a PDF, even if they never say "PDF/UA". It is not for creating new PDFs, editing their content or layout, or OCR (it detects scans and says OCR comes first).
compatibility: Needs Python 3.9+ with pip (or venv), and poppler-utils (pdftoppm, pdftotext) on PATH. Node.js/npm is optional (pdf.js cross-check). Works offline once dependencies are installed. Viewing images helps with the review step.
metadata:
  version: "1.0.0"
  ai-generated: "true"
  generated-with: "Claude (Anthropic), under human direction and review"
---

# PDF accessibility remediation

A PDF has two layers: what is painted on the page, and the logical layer that assistive technology reads. That second layer holds the structure tree (tags), marked content, reading order, language, title and link descriptions. This skill rebuilds only the logical layer, from evidence on the page, and proves the painted layer did not change.

It works on arbitrary PDFs. Never assume which tool produced the file. Let the layout, the type, any existing tags and the page renders decide.

## Ground rules

1. **Appearance is off-limits.** The layout is the author's decision. Never rasterize, reflow, move, recolor or re-typeset anything. The builder renders every page before and after the change and undoes its own text reordering on any page that differs. The validator compares the output with the source. If "Visual identity" is not PASS, do not deliver: find out why.
2. **Leave the wording alone.** Do not rewrite, correct spelling, or comment on phrasing or content quality. That is not what the user asked for. The only text you write is what the logical layer needs: the title, alt text, link descriptions, form labels, region labels and abbreviation expansions. Write it factually, in the document's language.
3. **Report what tags cannot fix.** Some problems live in the painted layer: low contrast, text baked into images, meaning carried by colour alone. Report each one once, with numbers, as a design issue for the author. Do not "fix" it by changing the look.
4. **Claim only what is true.** With `pdfua: "auto"`, the builder writes the PDF/UA-1 identifier only when nothing it knows of blocks it, such as a non-embedded font or a glyph without Unicode. These scripts are not veraPDF or PAC, so always tell the user which checks are still manual.

## Running the scripts

`SKILL_DIR` below means the directory that contains this SKILL.md. Commands use POSIX shell syntax. On Windows, run the same commands with the same arguments in PowerShell.

```bash
python3 "$SKILL_DIR/scripts/setup_env.py"
```

`setup_env.py` installs pikepdf, numpy, Pillow, pdfminer.six and lingua, and checks for poppler. If the system Python refuses packages, it creates a private virtual environment instead. Its last line is `PYTHON=<interpreter>`. Use that interpreter (written `$PY` below) for every other script. If it prints `NOT READY`, follow its hint (usually installing poppler) or tell the user what is missing.

Work on a copy in a scratch directory (`w/` below) and keep the source untouched. Name the output `<stem>-accessible.pdf`. Inspect and validate render every page, at roughly 1–2.5 s per page, so give long documents a generous timeout. If your environment offers a plan or to-do tool, track these steps: inspect → draft → review → build → validate → deliver.

## Workflow

### 1. Inspect: is it fixable, and what is on the page?

```bash
$PY "$SKILL_DIR/scripts/inspect_pdf.py" in.pdf --json w/units.json --assets w/assets --quiet
$PY "$SKILL_DIR/scripts/validate_pdfua.py" in.pdf --overlay w/before.png --report w/before.json
```

The second command produces the "before" picture: the source's own tag order, or its paint order when the file is untagged. Multi-page files get one image per page (`before-p1.png` …).

Read the preflight first:
- **STOP** means do not tag. A scanned page (no text, only a page-sized image) needs OCR first (WCAG technique PDF7). The same goes for dynamic XFA forms and files with no text. Explain the reason and stop. Never invent a text layer.
- **FIX**: the declared `/Lang` contradicts the text. The plan uses the inferred language.
- **WARN**: encryption (the output is saved unencrypted, so confirm with the owner), fonts that are not embedded (this blocks the PDF/UA claim, not WCAG), and glyphs without Unicode (the draft guesses `unicode_overrides`; check them against the render).

The printed layout tree is the hypothesis for reading order: rows split by large vertical gaps, columns split by vertical gutters. `w/assets/` holds the page renders (`page-N.png`) and crops of images (`fig-pN-GM.png`) and of charts drawn with paths (`vecfig-pN-K.png`). If you can view images, look at the renders now: they show the page the way a sighted reader sees it.

### 2. Draft a plan

```bash
$PY "$SKILL_DIR/scripts/draft_plan.py" w/units.json w/plan.json
$PY "$SKILL_DIR/scripts/plan_outline.py" w/plan.json w/units.json
```

The plan is JSON that maps each text line (`p1.L12`) and graphic (`p1.G3`) to a structure element. The full format is in [references/plan-format.md](references/plan-format.md). The draft is a starting point: it prints `_review` notes for everything it guessed. Read the whole outline, even when the notes look harmless. A wrong merge, such as a title swallowed into the paragraph beside it, only shows up in the outline.

### 3. Review the plan: the part that needs judgement

Compare the outline with the page, using the renders if you can view images and the inspector's per-line report if you cannot. Fix the plan where it disagrees with what a careful reader sees. [references/structure-decisions.md](references/structure-decisions.md) gives the reasoning and worked examples for each point:

- **Title**: says what the document is, and for whom or when. It must not be a file name. Write it in the document's language, and keep the user's title when they give one.
- **Language**: infer the primary language from the text. If neither the source nor the user gives a region, use **fr-FR for French and en-US for English** (other defaults are in `scripts/lang_detect.py`, `DEFAULT_VARIANT`). Mark a part in another language only when it is a real phrase in that language. Names, brands, jargon and established loanwords do not count.
- **Reading order**: follows the visual layout. Rows go top to bottom. Inside a row, columns go in reading direction, and each column is read to its end before the next one starts. Keep the region `Div`s (`R1`, `R1·C1` …) the draft creates. They keep each column a self-contained subtree, and the validator checks against them.
- **Headings by meaning**: H1 is what the document is about (a CV's person, a report's title). H2 marks the main sections, H3 their subsections, and so on, with no skipped levels. Type size is only a hint. A short line that introduces the content below it is a heading. A styled line that is itself data (a date, a place, a job period) is not.
- **Lists**: anything that looks like a list (bullets, numbers, or stacked parallel items such as contact details) becomes `L` > `LI`, with the right `numbering`.
- **Tables**: only real data grids get table tags, with header rows and/or columns. A layout grid is regions, not a table.
- **Figures**: a meaningful image or chart becomes `Figure` with alt text that says what it conveys, not what it is ("Bar chart: 12 in 2022 … 31 in 2025"). Decorative graphics (rules, backgrounds, ornaments) stay artifacts by simply not being listed. If you cannot view the image, write alt text only from evidence in the document (caption, surrounding text, labels drawn inside the figure). If there is none, ask the user, or tell them which alt texts need checking. Never describe what you have not seen.
- **Links**: every link annotation sits in a `Link`. Its `contents` gives the purpose in the document's language ("Envoyer un e-mail à …", "Email …"). Keep the visible e-mails and URLs the draft turned into links. Adding the annotation does not change the look.
- **Artifacts**: running headers, footers, page numbers, watermarks, repeated decoration, and fragments of lines cut by a page edge (the draft lists the ones it found).
- **Every text line** must be either tagged or listed in `artifacts.lines`. The builder refuses anything left over, so nothing is silently dropped.

If the user is there, confirm the decisions that could reasonably go either way (title, heading levels, alt text) before building. If you work unattended, take the most reasonable reading and list those decisions in your summary.

### 4. Build

```bash
$PY "$SKILL_DIR/scripts/build_tagged.py" in.pdf w/plan.json out/<stem>-accessible.pdf
```

If the builder stops with `PLAN ERRORS` (a TODO left in, an unknown or unused id, a skipped heading level, a figure without alt, an unplaced annotation), fix the plan and run it again. Read every warning. "Plan order departs from the layout", a language mismatch and "PDF/UA-1 not claimed because …" all deserve a look. "RENDERING DIFFERS" is a blocker.

The defaults in `options` are right for most files:
- `reorder_text: "auto"` moves text into reading order inside the content stream, so that content order matches tag order (copy/paste and tag-blind tools read that order). Each line is replayed with its exact matrix, clip and graphics state, and the result is checked by rendering.
- `pdfua: "auto"`.
- `bookmarks: "auto"` keeps existing bookmarks and generates them from headings for documents of 20 pages or more.

### 5. Validate

```bash
$PY "$SKILL_DIR/scripts/validate_pdfua.py" out/<stem>-accessible.pdf --compare in.pdf \
    --report w/after.json --overlay w/after.png --text w/after-order.txt
```

- The summary uses the same five items as common online checkers: Document Title, Document Language, Tagged PDF, Structure Tree and Bookmarks. Detailed checks follow, each with severity, WCAG success criterion, PDF technique and PDF/UA clause (see [references/checks.md](references/checks.md)). Exit code 0 means no FAIL.
- Read `after-order.txt` from top to bottom, as a screen reader would. If you can view images, check the overlay too: the numbered path must follow the way a person reads the page, with no red arrows. Dashed frames show the detected rows and columns.
- "Visual identity" must be PASS. A "Text contrast" FAIL is a design issue in the source: report it. A WARN on "Headings" saying the largest text is not a heading usually means the title was missed.
- Optional second opinion (needs npm, installed by setup): `node "$SKILL_DIR/scripts/pdfjs_a11y_tree.mjs" out/<stem>-accessible.pdf` prints what Firefox's PDF viewer exposes to assistive technology.

Go back to step 3 until only design issues remain.

### 6. Deliver

Hand over the accessible PDF together with the before and after overlays. Then write a short summary covering:
- what was wrong, in the checker's terms (title, language, tags, reading order …)
- what was done, in one or two lines (for example "2 rows × 2 columns, H1→H4, 3 lists, 5 described links, fr-FR")
- the decisions the user may want to confirm: title, language, heading levels, and alt text you wrote
- the issues tags cannot fix, with their measurements (for example "subtitle contrast 2.6:1, needs 4.5:1")
- the checks left to do by hand: veraPDF (PDF/UA-1 profile) or PAC 2024, then a screen reader pass (NVDA with Acrobat Reader, or VoiceOver)

Say nothing about the writing itself.

## Variants

- **Audit only** ("how accessible is this PDF?"): run inspect, then validate the source with `--overlay`. Report findings by severity (Critical, Serious, Moderate, Minor) with their WCAG criteria, then offer to fix them.
- **Already tagged source**: the same loop applies. The inspector reads the existing tags, and the draft reuses meaningful roles (H1–H6, LI, TD/TH, Figure alt) while ignoring junk trees. The old tree is always replaced as a whole, never patched.
- **Several files**: run the loop once per file. Drafts for simple Word or LibreOffice exports are often nearly right, but still read every outline.
- **Right-to-left scripts**: pass `--rtl` to inspect and validate, and set `options.rtl: true`.

## Limits (say so when they apply)

- The unit of tagging is a text line. A link or a language change in the middle of a line tags the whole line.
- Tables are detected heuristically (ruled or aligned). Check their headers and spans.
- Forms get only `Form` + `TU` labels. XFA is not supported, and scans need OCR first.
- The validator covers the machine-checkable core of WCAG 2.2 and PDF/UA-1. It does not replace veraPDF, PAC or a person with a screen reader.

## Files

| Path | Role |
|---|---|
| `scripts/setup_env.py` | installs and checks dependencies, prints the interpreter to use |
| `scripts/inspect_pdf.py` | preflight, language, layout regions, inventory of lines, graphics, annotations and tables (`units.json`), renders |
| `scripts/draft_plan.py` | first plan from the inventory, with `_review` notes |
| `scripts/plan_outline.py` | readable outline of a plan |
| `scripts/build_tagged.py` | writes the tagged PDF from the plan and verifies the rendering |
| `scripts/validate_pdfua.py` | checklist and detailed checks, reading-order overlay, pixel comparison |
| `scripts/content_model.py`, `prepare.py`, `layout.py`, `lang_detect.py`, `raster.py` | shared engine (content streams, XY-cut layout, language, rendering) |
| `scripts/pdfjs_a11y_tree.mjs` | accessibility tree as pdf.js sees it |
| `references/plan-format.md` | every plan key, with examples |
| `references/structure-decisions.md` | how to decide headings, lists, tables, figures, links, language and order, with worked examples |
| `references/checks.md` | validator checks ↔ severity ↔ WCAG ↔ PDF techniques ↔ PDF/UA, overlay legend |
| `examples/*.plan.json` | reviewed plans: a 2×2-region CV from Figma and a multi-page report with a table, a chart and links |
