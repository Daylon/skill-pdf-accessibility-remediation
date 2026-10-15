#!/usr/bin/env python3
"""
setup_env.py — install and check what the skill-pdf-accessibility-remediation scripts need. Safe to re-run.

Usage:
    python3 scripts/setup_env.py            # install into this interpreter (or a private venv if it is locked)
    python3 scripts/setup_env.py --check    # only report what is missing
    python3 scripts/setup_env.py --venv     # always use a private venv (~/.cache/pdf-accessibility/venv)
    python3 scripts/setup_env.py --no-pdfjs # skip the optional pdf.js second opinion

The last line printed is `PYTHON=<interpreter>`: run every other script with that interpreter.
Works on Linux, macOS and Windows. Needs Python 3.9+ and, outside Python, poppler (pdftoppm, pdftotext).
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import venv
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

REQUIRED = ["pikepdf", "numpy", "pillow", "pdfminer.six"]
OPTIONAL = ["lingua-language-detector"]          # better language detection on short strings; stop-words otherwise
IMPORTS = {"pikepdf": "pikepdf", "numpy": "numpy", "pillow": "PIL", "pdfminer.six": "pdfminer",
           "lingua-language-detector": "lingua"}
CACHE = Path(os.environ.get("PDF_A11Y_CACHE", Path.home() / ".cache" / "pdf-accessibility"))


def missing(python, packages):
    out = []
    for pkg in packages:
        r = subprocess.run([python, "-c", f"import {IMPORTS[pkg]}"], capture_output=True)
        if r.returncode:
            out.append(pkg)
    return out


def pip_install(python, packages):
    cmd = [python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *packages]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout)


def private_venv():
    d = CACHE / "venv"
    py = d / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        print(f"creating a private virtual environment in {d}")
        venv.EnvBuilder(with_pip=True).create(d)
    return str(py)


def poppler_hint():
    system = platform.system()
    if system == "Darwin":
        return "brew install poppler"
    if system == "Windows":
        return "conda install -c conda-forge poppler  (or: choco install poppler / scoop install poppler), then add its bin to PATH"
    return "apt install poppler-utils  (Fedora: dnf install poppler-utils, Alpine: apk add poppler-utils)"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report only, install nothing")
    ap.add_argument("--venv", action="store_true", help="always install into a private venv")
    ap.add_argument("--no-pdfjs", action="store_true", help="skip the optional pdf.js install")
    args = ap.parse_args()

    if sys.version_info < (3, 9):
        sys.exit(f"Python 3.9+ required (this is {platform.python_version()})")
    python = private_venv() if args.venv else sys.executable
    problems = []

    need = missing(python, REQUIRED)
    if need and not args.check:
        ok, err = pip_install(python, need + missing(python, OPTIONAL))
        if not ok:
            ok, err = pip_install(python, need)          # retry without the optional extras
        if not ok and python == sys.executable:
            # externally managed (PEP 668), read-only, or no pip at all: use a private venv instead
            print("this Python does not accept packages here: using a private venv")
            python = private_venv()
            ok, err = pip_install(python, REQUIRED + OPTIONAL)
            if not ok:
                ok, err = pip_install(python, REQUIRED)
        if not ok:
            print(err.strip()[-1500:])
        need = missing(python, REQUIRED)
    elif not need and not args.check:
        opt = missing(python, OPTIONAL)
        if opt:
            pip_install(python, opt)
    if need:
        problems.append(f"Python packages missing: {', '.join(need)} (pip install {' '.join(need)})")
    if missing(python, OPTIONAL):
        print("note: lingua-language-detector not installed — language detection falls back to stop-word counts")

    for tool in ("pdftoppm", "pdftotext"):
        if not shutil.which(tool):
            problems.append(f"{tool} not found (poppler): {poppler_hint()}")

    if not args.no_pdfjs and not args.check:
        npm = shutil.which("npm")
        d = CACHE / "pdfjs"
        if npm and not (d / "node_modules" / "pdfjs-dist").exists():
            d.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([npm, "install", "--silent", "--prefix", str(d), "pdfjs-dist"], capture_output=True)
            print("pdf.js ready (optional second opinion)" if r.returncode == 0 else "note: pdf.js install failed (optional)")
        elif not npm:
            print("note: npm not found — the optional pdf.js check (pdfjs_a11y_tree.mjs) is unavailable")

    if problems:
        print("NOT READY:")
        for p in problems:
            print("  -", p)
        print(f"PYTHON={python}")
        sys.exit(1)
    print("skill-pdf-accessibility-remediation: ready")
    print(f"PYTHON={python}")


if __name__ == "__main__":
    main()
