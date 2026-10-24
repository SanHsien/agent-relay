#!/usr/bin/env python3
"""Check every relative link in the repository's Markdown."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent

FENCE_RE = re.compile(r"(?ms)^(`{3,4}|~{3,4})[^\n]*\n.*?^\1\s*$")
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HTML_SRC_PATTERN = re.compile(r"""(?is)<img[^>]+src=["']([^"']+)["']""")

SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "#", "$", "conversation://", "sidecar://")


def iter_documents(root: Path = ROOT) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        return sorted(
            path for path in root.rglob("*.md") if ".git" not in path.parts
        )
    return sorted(root / line for line in result.stdout.splitlines() if line)


def missing_target(path: Path, target: str, root: Path = ROOT) -> str | None:
    target = target.strip().strip("<>")
    if not target or target.startswith(SKIP_PREFIXES):
        return None
    file_part = unquote(target.split()[0].split("#", 1)[0])
    if not file_part:
        return None
    resolved = (path.parent / file_part).resolve()
    if resolved.exists():
        return None
    try:
        shown = resolved.relative_to(root)
    except ValueError:
        shown = resolved
    return f"{target} -> missing {shown}"


def check_document(path: Path, root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    raw = path.read_text(encoding="utf-8")
    text = INLINE_CODE_RE.sub("", FENCE_RE.sub("", raw))
    for pattern in (LINK_PATTERN, IMAGE_PATTERN, HTML_SRC_PATTERN):
        for match in pattern.finditer(text):
            problem = missing_target(path, match.group(1), root)
            if problem:
                problems.append(problem)
    return problems


def main() -> int:
    documents = iter_documents(ROOT)
    if not documents:
        print("FAIL: no Markdown source files were found")
        return 1

    failures = 0
    for path in documents:
        relative = path.relative_to(ROOT)
        problems = check_document(path, ROOT)
        if problems:
            failures += 1
            for problem in problems:
                print(f"FAIL {relative}: {problem}")
        else:
            print(f"OK   {relative}")

    print(f"\n{len(documents)} Markdown files checked, {failures} with broken links.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
