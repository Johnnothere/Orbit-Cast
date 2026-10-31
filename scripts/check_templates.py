#!/usr/bin/env python3
"""Syntax-check the JavaScript embedded in the Jinja templates.

Why this exists: a duplicate `const` in templates/index.html once took the
whole app down. A SyntaxError in an inline script aborts that entire script,
so boot() never runs and every page renders as a bare navbar over an empty
body - while the server stays perfectly healthy and answers 200. Nothing in
the Python test suite can see it, because none of this is Python.

Run it before any push that touches a template:

    python3 scripts/check_templates.py

Requires node on PATH. Exits non-zero if any block fails to parse.
"""
import os
import re
import subprocess
import sys
import tempfile

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates")


def check(path):
    """Returns the number of script blocks that failed to parse."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    bad = 0
    for i, (attrs, body) in enumerate(
            re.findall(r"<script\b([^>]*)>(.*?)</script>", src, re.S | re.I)):
        # External scripts have no body to check; JSON blocks are not JS.
        if "src=" in attrs.lower() or "json" in attrs.lower():
            continue
        # Jinja expressions are not JavaScript. Substituting a string literal
        # keeps the surrounding syntax intact, which is what we are checking.
        js = re.sub(r"\{\{.*?\}\}", "'JINJA'", body, flags=re.S)
        js = re.sub(r"\{%.*?%\}", "", js, flags=re.S)

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as tmp:
            tmp.write(js)
            name = tmp.name
        try:
            result = subprocess.run(["node", "--check", name],
                                    capture_output=True, text=True)
        finally:
            os.unlink(name)

        if result.returncode:
            bad += 1
            line = src[:src.index(body)].count("\n") + 1
            print(f"{path}: script block #{i} (starts near line {line})")
            print(result.stderr.strip()[:1500])
    return bad


def main():
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        print("node is not on PATH; cannot check template JavaScript.")
        return 2

    bad = 0
    names = sorted(n for n in os.listdir(TEMPLATES) if n.endswith(".html"))
    for name in names:
        bad += check(os.path.join(TEMPLATES, name))

    # templates/email/ is server-rendered and carries no script.
    print(f"{len(names)} templates checked, {bad} script blocks with syntax errors")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
