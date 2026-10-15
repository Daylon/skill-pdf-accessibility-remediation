<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/LABEL_AI_white.svg">
  <img src="docs/images/LABEL_AI_black.svg" alt="AI-generated content" width="64" height="64">
</picture>

_**AI-generated content.** An AI model (Claude, by Anthropic) wrote the code and the documentation of this skill. A person gave the instructions, examined the results, and accepted them._

----

# A note from the author

Much software makes PDF files without structure: invoices, resumes, letters, and exports from design tools. These files have no tags or incorrect tags, so a screen reader cannot find the headings, the lists, or the correct reading order. [According to the World Health Organization](https://www.who.int/news-room/fact-sheets/detail/blindness-and-visual-impairment), more than 2.2 billion people have a near or distance vision impairment.

Since 28 June 2025, the European Accessibility Act applies to many digital products and services in the EU. But we cannot change all the software and server processes that make these files.

This skill repairs the PDF files that other software made. Today, most repair tools, online or on-premises, cost money, and/or cover only part of WCAG, and/or give little guidance on what to fix. This skill works with the AI agent you already use.

As a bonus, other tools will read these remediated PDFs correctly too: copy and paste, search, and resume parsers (ATS) get the text in the right order. Even more, your AI agents will thank you as well: with clean text, they spend fewer tokens making sense of a rendered page.

## References

- [Directive (EU) 2019/882, European Accessibility Act](https://eur-lex.europa.eu/eli/dir/2019/882/oj/eng): applies since 28 June 2025 to e-books, e-commerce, banking, and transport services.
- [Directive (EU) 2016/2102, Web Accessibility Directive](https://eur-lex.europa.eu/eli/dir/2016/2102/oj/eng): applies to the websites, the mobile apps, and the documents of public sector bodies.
- [EN 301 549 v4.1.1 (September 2026)](https://accessible-eu-centre.ec.europa.eu/content-corner/news/european-accessibility-standard-en-301-549-has-been-updated-2026-09-07_en): the European standard for ICT accessibility, now based on WCAG 2.2.
- [WCAG 2.2](https://www.w3.org/TR/WCAG22/), also published as [ISO/IEC 40500:2025](https://www.w3.org/WAI/news/2025-10-21/wcag22-iso/).
- [WCAG 2.2 techniques for PDF](https://www.w3.org/WAI/WCAG22/Techniques/)
- [ISO 14289, PDF/UA](https://pdfa.org/iso-14289-2-pdfua-2/): PDF/UA-1 (2014) and PDF/UA-2 (2024).
- [RGAA](https://accessibilite.numerique.gouv.fr/) (France): version 4.1.2 applies. Version 5, with WCAG 2.2, is due at the end of 2026.

----

# skill-pdf-accessibility-remediation

This agent skill makes PDF files accessible (WCAG 2.2, PDF/UA-1). It does not change the content or the layout of the pages.

The skill adds the data that assistive technology reads. This data has the tags, the headings, the lists, the tables, and the reading order. It also has the alternative text, the language, and the title. Then the skill makes sure that each page of the output is the same as the source page (pixel comparison).

The skill uses the [Agent Skills](https://agentskills.io/specification) format. Agents that read `SKILL.md` files can use it.

## How it works

The skill reads the PDF content and finds the lines of text, the space between the columns, and the text that is larger or bold. From this data, it finds the headings, the lists, the tables, and the reading order. Then an AI agent examines the pages and corrects the errors. The skill writes the tags into the PDF, but it does NOT change the content, thus the pages stay visually the same.
Real visual understanding comes in the review step, from the AI model doing the review.

## Necessary software

- Python 3.9 or later
- Poppler (`pdftoppm` and `pdftotext`)
- Node.js (optional, for a second check with pdf.js)

## Install

1. Put this folder in the skills folder of your agent. Keep the folder name.

   | Agent | Project folder | User folder |
   |---|---|---|
   | Claude Code | `.claude/skills/` | `~/.claude/skills/` |
   | Codex, GitHub Copilot, Cursor, Gemini CLI | `.agents/skills/` | `~/.agents/skills/` |

   ```bash
   git clone https://github.com/daylon/skill-pdf-accessibility-remediation \
     ~/.agents/skills/skill-pdf-accessibility-remediation
   ```

2. Install the Python packages:

   ```bash
   python3 ~/.agents/skills/skill-pdf-accessibility-remediation/scripts/setup_env.py
   ```

   The script shows the Python interpreter for the other scripts (`PYTHON=…`).

Each release also has a `.skill` file for Claude apps. If your agent cannot read skills, tell it: "Read `SKILL.md` and obey its instructions."

## Use

Tell the agent what to do, for example: "Make report.pdf accessible."

The agent examines the layout, writes a structure plan, and makes the tagged PDF. You get these items:

- The accessible PDF (`report-accessible.pdf`)
- A validation report
- Two images of the reading order (before and after)
- A list of decisions to examine (title, headings, alternative text)

## Limits

- The skill does not do optical character recognition (OCR). For a scanned PDF, do OCR first.
- The skill does not change colors. The report shows each contrast problem.
- After you use the skill, do these checks: veraPDF (PDF/UA-1 profile) or PAC 2024, and a test with a screen reader.

## Development

```bash
python3 -m pip install -r scripts/requirements.txt
python3 tests/run_tests.py --quick
```

To make a release, push a version tag (for example, `v1.0.1`). The release workflow attaches the ZIP file and the `.skill` file.

## License

This repository has no license at this time.
