# Changelog

## 1.0.0 — 2026-10-15

First release.

- Rebuilds the tag tree of an existing PDF from its rendered layout: reading order by rows and columns, headings H1–H6, lists, tables, figures with alternative text, links, artifacts.
- Sets the title, the language (default regions fr-FR and en-US), the XMP metadata and, when nothing blocks it, the PDF/UA-1 identifier.
- Moves the text into reading order in the content streams and keeps each page pixel-identical to the source (verified by rendering).
- Validator with the five checklist items of online checkers, detailed WCAG 2.2 / PDF/UA-1 checks and before/after reading-order overlays.
- Stops on scanned PDFs (OCR first) and on XFA forms.
- Runs on Python 3.9+ with poppler. Works with any agent that reads Agent Skills (`SKILL.md`).
