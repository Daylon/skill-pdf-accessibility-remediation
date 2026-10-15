# Structure decisions

How to turn what is on the page into tags when the draft guessed wrong or could not decide. The test for every choice: *would a screen-reader user, navigating by headings, lists and links, get the same understanding and the same order as a sighted reader scanning the page?*

## Contents
- [Reading order](#reading-order)
- [Headings](#headings)
- [Paragraphs and line grouping](#paragraphs-and-line-grouping)
- [Lists](#lists)
- [Tables](#tables)
- [Figures, icons, logos](#figures-icons-logos)
- [Links](#links)
- [Language](#language)
- [Title and metadata](#title-and-metadata)
- [Artifacts](#artifacts)
- [Producer patterns](#producer-patterns)
- [Worked example: a 2×2 CV](#worked-example-a-22-cv)

## Reading order

The inspector runs a recursive XY-cut on each page, working in the reading frame, so rotated text and rotated pages are handled.

- **Rows first.** Large vertical gaps that span the full width split the page into rows (default: more than 2.5 × the median line height). Rows are read top to bottom.
- **Then columns.** Inside a row, vertical gutters that run through it split it into columns. Columns are read in reading direction, and each one is finished before the next starts.
- **Then recursion.** Each column can split again into rows and columns, up to 8 levels deep.
- **Continuations.** A paragraph that wraps around a short gap stays in its column instead of creating a fake row. A footnote or caption sitting under one column is pulled into that column.

Accept the detected order when the overlay path reads naturally. Override it in the plan (the builder warns, which is fine) when meaning wins over geometry:
- **Sidebar or pull quote** in the middle of the text: place it after the paragraph it relates to, or at the end of the section, but never in the middle of a sentence. If it is pure repetition of body text (a pull quote), it can become an artifact.
- **Two-page spreads or a column continued on the next page**: follow the article, not the page grid. Lines on several pages can share one element.
- **Labels beside values** (form-like layouts: "Date: … / Place: …"): read each label next to its value, row by row, even if the gutter detection saw two columns.
- **Footnotes**: at the end of the page's main text, or as `Note` right after the paragraph that references them.

Keep the region `Div`s even when they feel redundant. They make every column a self-contained subtree, which lets the validator confirm that no column's content leaks into another. The labels (`/T`) also help the next person who opens the tree.

## Headings

Levels come from the **document's logic**, not from point sizes. The draft maps size tiers to levels; re-level them by meaning.

- **H1**: what the whole document is about. That is the person's name on a CV, the title of a report or article, the name of the form. Use one H1, unless the PDF really bundles several independent documents.
- **H2**: the main sections ("Experience", "Education", "1 Introduction").
- **H3 and below**: subsections, or repeated entries that have their own content (an employer within Experience, a degree within Education).
- Never skip a level going down. Going back up any number of levels is fine.
- **A heading introduces content that follows it.** A styled line that *is* the content is not a heading: a date range, a location, a job period, a tagline under the name, a pull quote. Bold body labels like "Note:" are not headings either.
- **Nothing that is a link is a heading.** A heading made only of an e-mail address or URL is almost always a contact line.
- When a heading spans two lines (a wrapped title), put both lines in one `H` element.
- Group a heading with its content in a `Sect` when the document has real sections. This helps some screen readers and makes the tree easier to review. It is optional for flat documents.

## Paragraphs and line grouping

- Lines of one paragraph go in one `P` (`"lines": ["p1.L9..11"]`), so a screen reader reads it as one sentence flow and not as separate fragments.
- Split paragraphs where the page shows a break: extra spacing, an indent, or a change of style that marks a new block. A colour change within a paragraph (an emphasised word on its own line) does not split it.
- An address block, a signature block or a date-and-place line are each one `P`, even across several lines.
- Do not put block elements inside `P`. Inline `Link`, `Span` (with `lang`), `Quote`, `Code` and `Reference` are fine, through `content`.

## Lists

Use `L` whenever the page presents a sequence of parallel items, with or without visible markers:
- bulleted and numbered lists (with the matching `numbering`)
- stacked contact details (e-mail, phone, profile, city): `numbering: "None"`
- lists of skills, certifications, talks or publications where each line is one item
- a table of contents: a `TOC` with `TOCI` items is also acceptable, but `L` with links is simpler and well supported

An item that wraps over several lines is **one** `LI` with all its lines. A sub-list goes in the parent item's `children`. Do not turn a sequence of ordinary paragraphs into a list just because they are stacked.

Markers: a bullet that is a text glyph on the same line as the item text stays inside that line. The line is the unit, so the item reads "• Text", which screen readers handle well. A marker on its own line goes in `label`. Drawn bullets (small paths) are graphics and stay artifacts.

## Tables

Tag as `Table` only a grid where position carries meaning, so that a cell needs its row and column header to be understood: schedules, price lists, results, comparisons.
- Header row(s): `header_rows`. Header column, when the first column names each row: `header_cols: 1`.
- Spanning headers: `colspan` / `rowspan` on the cell object.
- Empty cells still need a cell so the columns line up.
- A layout grid (cards, side-by-side blocks, the CV's two columns) is **not** a table. Use regions and lists.
- A two-column "label: value" grid can be a table with `header_cols: 1`, or paragraphs. Prefer a table when there are many rows of the same kind.

## Figures, icons, logos

Decide by what the image adds that the text does not already say:

| Image | Tag | Alt text |
|---|---|---|
| Chart, diagram, map, photo that carries information | `Figure` | What it shows and its key data or point, in 1–3 sentences. Put long data in the text or a table, not in the alt. |
| Photo of a person on a CV | `Figure` | Short and factual: "Portrait of Camille Durand". |
| Logo, when the organisation is not named in nearby text | `Figure` | The organisation's name ("Northwind logo"). |
| Logo next to its name, icon next to the text it illustrates (envelope beside the e-mail address), divider, background, ornament | artifact | none |
| Icon that is the only carrier of meaning (a ✓ in a table, a phone icon with no "Phone" label) | `Figure` | Its meaning ("Yes", "Phone"). |
| Image of text | `Figure` | The text it contains. Also report it as a design issue (WCAG 1.4.5). |

Look at the crop (`assets/fig-pN-GM.png` or `vecfig-pN-K.png`) before writing. If you cannot view images, write the alt text only from evidence in the document: the caption, the surrounding text, or labels drawn inside the figure (its `lines_inside` in `units.json`). If that evidence is missing, ask the user, or flag the figure in your summary. Do not start with "Image of". Write in the document's language. Do not describe what is not there. Graphics from `vector_figures` (groups of nearby paths) usually form one figure together.

## Links

- Every existing link annotation becomes a `Link` (PDF/UA requires it). Its `contents` states the purpose in the document's language.
- Visible e-mails, phone numbers, URLs and fediverse handles that are not clickable are proposed as `link_candidates`. Making them links adds an invisible annotation and is a real usability gain, so keep them unless the user objects.
- Phrasing patterns (`draft_plan.LINK_WORDS`):

| | French | English |
|---|---|---|
| mailto | Envoyer un e-mail à x@y | Email x@y |
| tel | Appeler le +33 … | Call +1 … |
| web | Ouvrir example.org/path | Open example.org/path |
| profile | Profil Mastodon @a@b | Mastodon profile @a@b |

Use `tel:` URIs in E.164 form (`tel:+33100000000`), and keep the visible formatting in `contents`.

## Language

- **Primary language**: inferred from all the text (lingua, offline). When the region is unknown, use the defaults: **fr → fr-FR, en → en-US** (de-DE, es-ES, it-IT, nl-NL and pt-PT likewise). A region the source declares correctly, or that the user states, wins.
- **Language of parts** (WCAG 3.1.2): put `lang` on an element, or on a `Span` within `content`, when it holds a real phrase or sentence in another language, such as a quotation, a course title or a certification name written in English inside a French CV. The validator lists candidates.
- **Exempt**: proper names, brand and product names, technical terms and jargon used as such in the main language ("Design manager", "DesignOps", "UX"), and single loanwords. Do not tag these. That would make screen readers switch voice mid-sentence for no reason.
- **Detection limits**: the detector knows fr, en, de, es, it, nl, pt, ca, pl and sv, plus Latin for passages only. Lorem-ipsum placeholder text reads as Latin and is not reported. A language outside the set is reported as its closest neighbour (Galician as Portuguese, for example), so trust your reading over the label.
- The tagging unit is the line. If a foreign phrase shares a line with main-language text, tag the line only when the foreign part dominates it, and mention the limit in the summary.

## Title and metadata

- Give the title the shape **what – who/what for – (when)**: "Camille Durand – CV – Design manager", "Annual accessibility report 2025 — Northwind Public Services". Use the document's language and its own terms. A file name ("resume-final-v3.pdf") is never a title.
- Keep a meaningful existing title. Keep the user's title verbatim when they give one.
- `subject` is one neutral sentence about what the document contains. `author` is only set when known (the person on a CV, the organisation on a report).

## Artifacts

Mark as artifacts:
- running headers and footers, page numbers, "continued on next page", repeated table header rows on continuation pages
- watermarks, crop marks, background text, decorative large letters or numbers
- text hidden under an opaque shape (invisible to sighted readers). The validator flags it if it is tagged.
- fragments of a line cut by a page or frame edge, such as the descenders of the previous page's last line that browsers repeat at the top of the next page. The inspector marks lines that are mostly clipped away, and the draft lists them as artifacts so the text is not read twice.
- graphics that do not carry information: these are simply not listed

Do not mark as artifacts footnotes, captions, the copyright line on a cover, or a printed date. Those are content.

## Producer patterns

These patterns are evidence to recognise, not rules to rely on:
- **Figma or design tools exported "outlined"**: the visible glyphs are vector paths, and an invisible text layer (Type 3, render mode 3) carries the text. Tag the invisible text and leave the outline paths as artifacts. The source's tags, if any, are usually one flat `P` per text box with junk roles, so the draft ignores them.
- **Word, LibreOffice, InDesign**: often already tagged with reasonable roles, but reading order, lists (`LI > LBody > P`) and headings may be off. The draft reuses meaningful roles and rebuilds the rest.
- **LaTeX (pdfTeX)**: no tags, no `/Lang`, words positioned without spaces (the inspector rebuilds the spaces), bullets in symbol fonts without Unicode (`unicode_overrides`), and equations as text fragments (one `Formula` with alt per equation).
- **Browsers (Chrome, wkhtmltopdf)**: link annotations without structure, CSS backgrounds as paths, and sometimes form XObjects per text run (inlined by `prepare.py`).
- **Qt or ReportLab**: clipping paths drawn as polygons, and non-embedded base-14 fonts that block the PDF/UA claim (WCAG is not affected).

## Worked example: a 2×2 CV

A one-page CV exported from Figma. The page has a header row (name and title on the left, contacts on the right) and a body row (experience on the left, education, talks, certifications and profile on the right), separated only by whitespace. `examples/cv-two-rows-two-columns.plan.json` is the reviewed plan.

```
Document
├─ Div "Row 1 · header"
│  ├─ Div "Row 1 · column 1 (identity)"   H1 name · P job title
│  └─ Div "Row 1 · column 2 (contact)"    L(None): LI>Link mailto · LI>Link tel · LI>Link profile · LI city
└─ Div "Row 2 · body"
   ├─ Div "Row 2 · column 1 (experience)" Sect: H2 Experience
   │     └─ Sect per employer: H3 employer · P dates and role · H4 "Key achievements" · L(Disc) items · P
   └─ Div "Row 2 · column 2 (…)"          Sect H2 Education > Sect H3 school · P degree · P years
                                           Sect H2 Talks > L(None) · Sect H2 Certifications > L(None) [one item lang=en-US]
                                           Sect H2 Profile > P
```

Decisions behind it:
- The **name is H1**, not the title line under it. That line is a tagline, so it is a `P`.
- **Contact lines** are a list without markers. Each one becomes a link with spoken-friendly `contents` in French, and the visible text stays untouched. The city is an item without a link.
- **Dates and roles** under each employer are styled differently (colour, weight) but are data, so they are `P`, not headings.
- **One certification** has an English title inside the French CV, so the item gets `lang: "en-US"`. Terms like "Design manager" or "DesignOps" are jargon and are not tagged.
- **Reading order** is header row (identity, then contact), then body row (whole left column, then whole right column), which is exactly the dashed frames on the overlay. A plain top-to-bottom order would interleave the two columns line by line, and that is what the original export did.
- **Contrast** of the grey secondary text was measured on the render. It passed here. If it had failed, it would be reported, not changed.
