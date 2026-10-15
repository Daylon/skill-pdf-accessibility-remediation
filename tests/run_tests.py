#!/usr/bin/env python3
"""
run_tests.py — end-to-end tests of the skill scripts on the fixture PDFs (standard library only).

Usage:
    python3 tests/run_tests.py            # all fixtures
    python3 tests/run_tests.py --quick    # skip the long fixtures
    python3 tests/run_tests.py -v         # unittest verbosity

For each fixture: inspect -> draft -> build (with the reviewed plan) -> validate against the source.
A fixture passes when the output is pixel-identical to the source and the validator fails exactly
the checks listed in tests/expected.json (design issues that tags cannot fix). Outputs go to
tests/_out/ for inspection.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
OUT = ROOT / "tests" / "_out"
EXPECTED = {k: v for k, v in json.loads((ROOT / "tests" / "expected.json").read_text(encoding="utf-8")).items()
            if not k.startswith("_")}
QUICK = "--quick" in sys.argv


def run(script, *args, timeout=900):
    cmd = [sys.executable, str(SCRIPTS / script), *map(str, args)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    fields, key = {}, None
    for line in m.group(1).splitlines():
        if re.match(r"^[a-z][\w-]*:", line):
            key, _, value = line.partition(":")
            fields[key] = value.strip().strip('"')
        elif key and line.startswith("  "):
            fields[key] += "\n" + line.strip()
    return fields


class SkillFormat(unittest.TestCase):
    """The SKILL.md frontmatter obeys the Agent Skills specification (agentskills.io)."""

    def test_frontmatter(self):
        fm = frontmatter((ROOT / "SKILL.md").read_text(encoding="utf-8"))
        name = fm.get("name", "")
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$", "name: lowercase letters, digits, single hyphens")
        self.assertLessEqual(len(name), 64)
        self.assertTrue(0 < len(fm.get("description", "")) <= 1024, "description: 1-1024 characters")
        if "compatibility" in fm:
            self.assertLessEqual(len(fm["compatibility"]), 500)
        if ROOT.name != name:
            print(f"\nnote: the folder is '{ROOT.name}' but the skill name is '{name}'. Agents need them equal "
                  f"once installed.", file=sys.stderr)

    def test_referenced_files_exist(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for ref in sorted(set(re.findall(r"\((references/[\w.-]+\.md)\)|`(scripts/[\w.-]+\.(?:py|mjs))`", text))):
            path = ref[0] or ref[1]
            self.assertTrue((ROOT / path).exists(), f"SKILL.md refers to a missing file: {path}")


class Fixtures(unittest.TestCase):
    pass


def make_test(name, exp):
    def test(self):
        if exp.get("slow") and QUICK:
            self.skipTest("--quick")
        out = OUT / name
        out.mkdir(parents=True, exist_ok=True)
        src = FIXTURES / f"{name}.pdf"

        code, log = run("inspect_pdf.py", src, "--json", out / "units.json", "--quiet")
        self.assertEqual(code, 0, log[-2000:])
        units = json.loads((out / "units.json").read_text(encoding="utf-8"))
        self.assertEqual(len(units["pages"]), exp["pages"])
        stop = [p for p in units["preflight"] if p["level"] == "STOP"]

        code, log = run("draft_plan.py", out / "units.json", out / "draft.plan.json")
        if exp.get("stop"):
            self.assertTrue(stop, "preflight should stop this file")
            self.assertNotEqual(code, 0, "the draft must refuse a file that preflight stops")
            return
        self.assertFalse(stop, stop)
        self.assertEqual(code, 0, log[-2000:])

        plan = ROOT / exp["plan"] if "plan" in exp else FIXTURES / f"{name}.plan.json"
        dst = out / f"{name}-accessible.pdf"
        code, build_log = run("build_tagged.py", src, plan, dst)
        self.assertEqual(code, 0, build_log[-3000:])
        self.assertNotIn("RENDERING DIFFERS", build_log)

        code, log = run("validate_pdfua.py", dst, "--compare", src, "--report", out / "report.json",
                        "--overlay", out / "order.png", "--text", out / "order.txt")
        items = json.loads((out / "report.json").read_text(encoding="utf-8"))
        status = {}
        for it in items:
            status.setdefault(it["check"], set()).add(it["status"])
        self.assertEqual(status.get("Visual identity"), {"PASS"}, "output must be pixel-identical to the source")
        fails = sorted(c for c, s in status.items() if "FAIL" in s)
        self.assertEqual(fails, sorted(exp["fails"]), "\n".join(
            f"{it['check']}: {it['detail'][:200]}" for it in items if it["status"] == "FAIL"))
        self.assertEqual(code, 1 if exp["fails"] else 0)

        if "pdfua_claim" in exp:
            self.assertEqual("PDF/UA claim=yes" in build_log, exp["pdfua_claim"], build_log[-500:])
        if exp.get("bookmarks"):
            self.assertIn("PASS", status.get("Bookmarks", set()))
            self.assertTrue(any("bookmarks for" in it["detail"] for it in items if it["check"] == "Bookmarks"))
        if "first_heading" in exp:
            order = (out / "order.txt").read_text(encoding="utf-8")
            first = next((ln for ln in order.splitlines() if re.search(r"\sH1\s", ln)), "")
            self.assertIn(exp["first_heading"], first)
    return test


for _name, _exp in EXPECTED.items():
    setattr(Fixtures, "test_" + _name.replace("-", "_"), make_test(_name, _exp))


if __name__ == "__main__":
    argv = [a for a in sys.argv if a != "--quick"]
    unittest.main(argv=argv)
