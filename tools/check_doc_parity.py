#!/usr/bin/env python3
"""Guard bilingual public docs from drifting out of sync."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
H2_RE = re.compile(r"^##\s+", re.MULTILINE)
H3_RE = re.compile(r"^###\s+", re.MULTILINE)
FENCE_RE = re.compile(r"(?ms)^(`{3,4}|~{3,4})[^\n]*\n.*?^\1\s*$")


@dataclass(frozen=True)
class DocPair:
    label: str
    english: str
    traditional_chinese: str


DOC_PAIRS = (
    DocPair("README", "README.en.md", "README.md"),
    DocPair("SPEC", "SPEC.en.md", "SPEC.md"),
    DocPair(
        "docs/antigravity-app-mailbox",
        "docs/antigravity-app-mailbox.en.md",
        "docs/antigravity-app-mailbox.md",
    ),
    DocPair(
        "docs/templates/escalation-prompt",
        "docs/templates/escalation-prompt.en.md",
        "docs/templates/escalation-prompt.md",
    ),
    DocPair(
        "docs/nostr-nip01-evaluation",
        "docs/nostr-nip01-evaluation.en.md",
        "docs/nostr-nip01-evaluation.md",
    ),
)


def strip_fenced_blocks(text: str) -> str:
    return FENCE_RE.sub("", text)


def check_pair(root: Path, pair: DocPair) -> list[str]:
    errors: list[str] = []
    en_path = root / pair.english
    zh_path = root / pair.traditional_chinese

    if not en_path.is_file():
        errors.append(f"{pair.label}: missing English file {pair.english}")
    if not zh_path.is_file():
        errors.append(
            f"{pair.label}: missing Traditional Chinese file {pair.traditional_chinese}"
        )
    if errors:
        return errors

    en_raw = en_path.read_text(encoding="utf-8")
    zh_raw = zh_path.read_text(encoding="utf-8")

    en_name = en_path.name
    zh_name = zh_path.name
    if f"({zh_name})" not in en_raw.splitlines()[0]:
        errors.append(
            f"{pair.label}: {pair.english} first line must link to {zh_name}"
        )
    if f"({en_name})" not in zh_raw.splitlines()[0]:
        errors.append(
            f"{pair.label}: {pair.traditional_chinese} first line must link to {en_name}"
        )

    en_stripped = strip_fenced_blocks(en_raw)
    zh_stripped = strip_fenced_blocks(zh_raw)

    en_h2 = len(H2_RE.findall(en_stripped))
    zh_h2 = len(H2_RE.findall(zh_stripped))
    if en_h2 != zh_h2:
        errors.append(
            f"{pair.label}: ## heading count mismatch "
            f"({pair.english}={en_h2}, {pair.traditional_chinese}={zh_h2})"
        )

    en_h3 = len(H3_RE.findall(en_stripped))
    zh_h3 = len(H3_RE.findall(zh_stripped))
    if en_h3 != zh_h3:
        errors.append(
            f"{pair.label}: ### heading count mismatch "
            f"({pair.english}={en_h3}, {pair.traditional_chinese}={zh_h3})"
        )

    return errors


def check_all(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for pair in DOC_PAIRS:
        errors.extend(check_pair(root, pair))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check that Traditional Chinese and English public docs stay in sync."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to inspect.",
    )
    args = parser.parse_args()
    errors = check_all(args.root.resolve())
    if errors:
        print("FAIL: bilingual document parity check failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"PASS: bilingual document parity check passed ({len(DOC_PAIRS)} pairs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
